"""kit update: pull the latest kit from its git remote, sync packages and run doctor.

Also the daily notice: at most once a day kit checks in the background (a detached `kit update --check
--background`) and, when new commits are waiting, prints one line after a command.

Used by kit hub too, through check(), apply() and notify_if_due(); keep their signatures and keys stable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from kitlib import error, style, warn

from core.paths import state_dir
from core.registry import KIT_HOME

FETCH_TIMEOUT = 45
GIT_TIMEOUT = 60
SYNC_TIMEOUT = 900
DOCTOR_TIMEOUT = 300
CHECK_INTERVAL = 24 * 3600      # a background check at most once a day
BACKGROUND_COOLDOWN = 3600      # never start two background checks within an hour
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

Log = Callable[[str], None]


# --- git -------------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"   # never ask for a username/password on the terminal
    env["GCM_INTERACTIVE"] = "never"   # nor through Git Credential Manager's pop-up
    env["LC_ALL"] = env["LANG"] = "C"  # English messages we can pass on
    return env


def _git(*args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(KIT_HOME), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=timeout, env=_git_env(), creationflags=NO_WINDOW,
    )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _git_problem(stderr: str) -> str:
    """The most useful line of a git error: its first "fatal:"/"error:" line, else the last line."""
    for line in (stderr or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("fatal:", "error:")):
            return stripped.split(":", 1)[1].strip()
    return _last_line(stderr) or "git failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- check -----------------------------------------------------------------------------

def check(fetch: bool = True) -> dict:
    """Where this copy of kit stands against its upstream branch. Never raises."""
    result = {
        "supported": False, "reason": None, "branch": "", "upstream": "",
        "behind": 0, "ahead": 0, "dirty": False, "dirty_files": [],
        "incoming": [], "error": None, "checked_at": _now_iso(),
    }
    try:
        _check(result, fetch)
    except subprocess.TimeoutExpired as exc:
        result["error"] = f"git took too long ({' '.join(exc.cmd[3:5])})"
    except Exception as exc:  # the contract is "never raises"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _check(result: dict, fetch: bool) -> None:
    if not shutil.which("git"):
        result["reason"] = "git isn't installed, so kit can't update itself - install git and try again"
        return
    if not (KIT_HOME / ".git").exists() or _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        result["reason"] = (f"{KIT_HOME} isn't a git clone (it may have been downloaded as a zip), so kit can't "
                            "update itself - clone the repository with git and run the installer again")
        return

    branch = _git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if not branch:
        result["reason"] = "no branch is checked out (detached HEAD) - check one out first, e.g.: git checkout main"
        return
    result["branch"] = branch
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        result["reason"] = (f"branch '{branch}' doesn't track a remote branch - set one with: "
                            f"git branch --set-upstream-to=origin/{branch}")
        return
    result["upstream"] = upstream.stdout.strip()
    result["supported"] = True

    if fetch:
        remote = _git("config", f"branch.{branch}.remote").stdout.strip() or "origin"
        try:
            fetched = _git("fetch", "--quiet", remote, timeout=FETCH_TIMEOUT)
            if fetched.returncode != 0:
                result["error"] = (f"couldn't fetch from '{remote}' ({_git_problem(fetched.stderr)}) - check your "
                                   f"internet connection and the remote address (git -C \"{KIT_HOME}\" remote -v)")
        except subprocess.TimeoutExpired:
            result["error"] = f"couldn't reach '{remote}' within {FETCH_TIMEOUT}s - check your internet connection"

    counts = _git("rev-list", "--left-right", "--count", "HEAD...@{u}").stdout.split()
    if len(counts) == 2:
        result["ahead"], result["behind"] = int(counts[0]), int(counts[1])

    dirty = [line[3:] for line in _git("status", "--porcelain", "--untracked-files=no").stdout.splitlines() if line.strip()]
    result["dirty"], result["dirty_files"] = bool(dirty), dirty

    if result["behind"]:
        result["incoming"] = _commits("HEAD..@{u}")


def _commits(revision_range: str) -> list[dict]:
    out = _git("log", "--format=%H%x1f%s%x1f%an%x1f%cI", revision_range).stdout
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"hash": parts[0], "subject": parts[1], "author": parts[2], "date": parts[3]})
    return commits


# --- apply -----------------------------------------------------------------------------

def apply(log: Log = print, sync: bool = True, doctor: bool = True) -> dict:
    """Fast-forward to the upstream branch, then uv sync and doctor. Streams progress through log(). Never raises."""
    result = {"ok": False, "error": None, "before": "", "after": "", "incoming": []}

    def say(line: str) -> None:
        try:
            log(line)
        except Exception:
            pass

    try:
        _apply(result, say, sync, doctor)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        say(f"error: {result['error']}")
    return result


def _fail(result: dict, say: Log, message: str, *hints: str) -> None:
    result["ok"], result["error"] = False, message
    say(f"error: {message}")
    for hint in hints:
        say(hint)


def _apply(result: dict, say: Log, sync: bool, doctor: bool) -> None:
    say("checking for updates...")
    state = check(fetch=True)
    _record_check(state)
    if not state["supported"]:
        return _fail(result, say, state["reason"] or "kit can't update itself here")
    if state["error"]:
        return _fail(result, say, state["error"])

    head = _git("rev-parse", "HEAD").stdout.strip()
    result["before"] = result["after"] = head
    upstream = state["upstream"]

    if not state["behind"]:
        message = f"kit is up to date with {upstream}"
        if state["ahead"]:
            message += f" ({_plural(state['ahead'], 'local commit')} not pushed yet)"
        say(message)
        result["ok"] = True
        return

    if state["dirty"]:
        return _fail(
            result, say, "you have uncommitted changes, so kit won't update over them:",
            *[f"  {name}" for name in state["dirty_files"]],
            f"commit or stash them (git -C \"{KIT_HOME}\" stash), then run kit update again",
        )
    if state["ahead"]:
        return _fail(
            result, say,
            f"your branch and {upstream} have diverged ({_plural(state['ahead'], 'local commit')}, "
            f"{_plural(state['behind'], 'new commit')}), so kit won't guess how to combine them",
            f"combine them yourself, e.g.: git -C \"{KIT_HOME}\" pull --rebase",
        )

    result["incoming"] = state["incoming"]
    say(f"{_plural(state['behind'], 'new commit')} on {upstream}:")
    for commit in state["incoming"]:
        say(f"  {commit['hash'][:7]}  {commit['subject']}  ({commit['author']}, {commit['date'][:10]})")

    merged = _git("merge", "--ff-only", "--quiet", "@{u}")
    if merged.returncode != 0:
        return _fail(result, say, f"couldn't fast-forward: {_last_line(merged.stderr) or 'git merge failed'}")
    result["after"] = _git("rev-parse", "HEAD").stdout.strip()
    result["ok"] = True

    if sync and not _sync(say):
        result["ok"] = False
        result["error"] = "kit's files were updated, but installing packages failed - run: uv sync"
    if doctor:
        _doctor(say)

    say(f"updated from {result['before'][:7]} to {result['after'][:7]}")
    _record_check(check(fetch=False))  # so the "update available" notice goes away


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _sync(say: Log) -> bool:
    uv = shutil.which("uv")
    if not uv:
        say("warning: uv isn't installed, so packages weren't updated - install uv, then run: uv sync")
        return True
    last = ""
    # Antivirus that inspects HTTPS breaks uv's built-in certificates; the system store usually works.
    for extra in ([], ["--system-certs"]):
        say("installing packages (uv sync" + (" --system-certs" if extra else "") + ")...")
        try:
            done = subprocess.run(
                [uv, "sync", "--project", str(KIT_HOME), "--quiet", *extra],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=SYNC_TIMEOUT, creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            last = f"timed out after {SYNC_TIMEOUT}s"
            continue
        if done.returncode == 0:
            say("packages are up to date")
            return True
        last = _last_line(done.stderr) or f"exit code {done.returncode}"
    say(f"error: uv sync failed: {last}")
    return False


def _doctor(say: Log) -> None:
    say("running kit doctor...")
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    try:
        done = subprocess.run(
            [sys.executable, str(KIT_HOME / "kit.py"), "doctor"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=DOCTOR_TIMEOUT, env=env, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        say("warning: kit doctor took too long")
        return
    for line in done.stdout.splitlines():
        if "[FAIL]" in line or "[warn]" in line:
            say(line.strip())
    summary = _last_line(done.stdout)
    say(f"doctor: {summary}" if summary else f"doctor exited with code {done.returncode}")


# --- state file and daily notice ---------------------------------------------------------

def state_path() -> Path:
    return state_dir() / "update-state.json"


def _read_all() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_state() -> dict:
    """State for this copy of kit (several clones can share one state file)."""
    entry = _read_all().get(str(KIT_HOME))
    return entry if isinstance(entry, dict) else {}


def _write_state(state: dict) -> None:
    path = state_path()
    data = _read_all()
    data[str(KIT_HOME)] = state
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _record_check(result: dict) -> None:
    try:
        state = _read_state()
        state["last_check"] = time.time()
        state["result"] = {key: result.get(key) for key in ("supported", "behind", "ahead", "upstream", "error", "checked_at")}
        _write_state(state)
    except Exception:
        pass


def notify_if_due() -> None:
    """Print the "update available" line once a day and start a background check when one is due.

    Reads a small JSON file, so it takes a millisecond or two; never raises.
    """
    try:
        _notify(sys.stderr)
    except Exception:
        pass


def _notify(stream, force_tty: bool = False) -> None:
    from kitlib import settings

    try:
        data = settings.load()
    except settings.SettingsError:
        return  # a broken settings file is already reported by the command and by doctor; stay quiet here
    if not settings.values_for("kit", settings.KIT_SCHEMA, data).get("update_check", True):
        return
    if not (KIT_HOME / ".git").exists():
        return

    state = _read_state()
    changed = False
    now = time.time()
    behind = (state.get("result") or {}).get("behind") or 0
    today = date.today().isoformat()
    if behind > 0 and state.get("notice_shown") != today and (force_tty or stream.isatty()):
        message = f"kit: an update is available ({_plural(behind, 'new commit')}) - run: kit update"
        print(style(message, "yellow", stream=stream), file=stream)
        state["notice_shown"] = today
        changed = True

    last_check = float(state.get("last_check") or 0)
    started = float(state.get("background_started") or 0)
    if now - last_check > CHECK_INTERVAL and now - started > BACKGROUND_COOLDOWN:
        state["background_started"] = now
        _write_state(state)  # record before spawning, so two kit commands can't both start one
        _spawn_background_check()
    elif changed:
        _write_state(state)


def _spawn_background_check() -> None:
    command = [sys.executable, str(KIT_HOME / "kit.py"), "update", "--check", "--background"]
    options: dict = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "cwd": str(KIT_HOME), "close_fds": True,
    }
    if sys.platform.startswith("win"):
        options["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)


# --- command line ------------------------------------------------------------------------

def _cli_log(line: str) -> None:
    if line.startswith("error: "):
        error(line[len("error: "):])
    elif line.startswith("warning: "):
        warn(line[len("warning: "):])
    elif line.startswith("updated from") or line.startswith("kit is up to date"):
        print(style(line, "green"))
    else:
        print(line, flush=True)


def _print_check(result: dict) -> int:
    if not result["supported"]:
        error(result["reason"] or "kit can't update itself here")
        return 1
    if result["error"]:
        error(result["error"])
        return 1
    print(f"branch {style(result['branch'], 'bold')} tracking {result['upstream']}")
    if result["behind"]:
        print(style(f"{_plural(result['behind'], 'new commit')} available:", "yellow"))
        for commit in result["incoming"]:
            detail = style(f"({commit['author']}, {commit['date'][:10]})", "dim")
            print(f"  {commit['hash'][:7]}  {commit['subject']}  {detail}")
    else:
        print(style("kit is up to date", "green"))
    if result["ahead"]:
        print(f"{_plural(result['ahead'], 'local commit')} not pushed yet")
    if result["dirty"]:
        sys.stdout.flush()  # keep the lines above in order when output is piped
        warn("uncommitted changes to kit's files (kit update will refuse until you commit or stash them):")
        for name in result["dirty_files"]:
            print(f"  {name}")
    if result["behind"]:
        print("run: kit update")
    return 0


def main(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit update", description="Update kit from its git remote, then sync packages and run doctor.")
    parser.add_argument("--check", action="store_true", help="only check for updates; change nothing")
    parser.add_argument("--no-sync", action="store_true", help="don't run uv sync after updating")
    parser.add_argument("--no-doctor", action="store_true", help="don't run kit doctor after updating")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)  # the silent daily check
    ns = parser.parse_args(args)

    if ns.background:
        _record_check(check(fetch=True))
        return 0
    if ns.check:
        print("checking for updates...", flush=True)
        result = check(fetch=True)
        _record_check(result)
        return _print_check(result)

    result = apply(log=_cli_log, sync=not ns.no_sync, doctor=not ns.no_doctor)
    return 0 if result["ok"] else 1
