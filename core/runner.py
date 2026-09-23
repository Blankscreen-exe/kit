"""Runs a tool's entry point with the right interpreter and kit's environment."""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import sys
import threading

from core.registry import KIT_HOME, Tool, current_platform

URL_RE = re.compile(r"https?://[^\s'\"<>]+")
URL_SCAN_LIMIT = 8_000


class RunError(Exception):
    pass


def _find_program(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise RunError(f"needs {' or '.join(names)} on PATH")


def build_command(tool: Tool, args: list[str]) -> list[str]:
    path = tool.entry_path()
    if path is None:
        raise RunError(f"no entry point defined for {current_platform()}")
    if not path.is_file():
        raise RunError(f"entry point not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(path), *args]
    if suffix == ".ps1":
        shell = _find_program("pwsh", "powershell")
        return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path), *args]
    if suffix == ".sh":
        return [_find_program("bash"), str(path), *args]
    if suffix in (".cmd", ".bat"):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(path), *args]
    return [str(path), *args]


def tool_env(tool: Tool) -> dict[str, str]:
    env = os.environ.copy()
    env["KIT_HOME"] = str(KIT_HOME)
    env["KIT_TOOL"] = tool.name
    env["KIT_TOOL_DIR"] = str(tool.dir)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(KIT_HOME / "lib"), env.get("PYTHONPATH")) if p)
    return env


def run(tool: Tool, args: list[str]) -> int:
    command = build_command(tool, args)
    env = tool_env(tool)
    if tool.web and _notify_direct_runs_enabled():
        return _run_and_watch(tool, command, env)
    try:
        return subprocess.call(command, env=env)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise RunError(f"could not start {command[0]}: {exc}") from exc


def _notify_direct_runs_enabled() -> bool:
    try:
        from kitlib.settings import kit_settings

        return bool(kit_settings()["notify_direct_runs"])
    except Exception:
        return False  # never let a settings problem block running the tool itself


def _run_and_watch(tool: Tool, command: list[str], env: dict[str, str]) -> int:
    """Like a plain subprocess.call, except output is relayed live rather than truly inherited,
    so a URL in it can be noticed - only web tools take this path, and only to (best-effort,
    silently-skipped-if-not-set-up) announce it over kit notify, the same as `kit share start`
    already does. Passthrough is byte-for-byte identical to what the terminal would have shown
    either way; FORCE_COLOR is set so tools don't lose their own colour output just because
    their stdout is now a pipe instead of a real tty.
    """
    env = dict(env)
    env.setdefault("FORCE_COLOR", "1")
    try:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise RunError(f"could not start {command[0]}: {exc}") from exc

    out = sys.stdout.buffer
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    scanned = ""
    notified = False
    stream = process.stdout
    assert stream is not None
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            if not notified and len(scanned) < URL_SCAN_LIMIT:
                scanned += decoder.decode(chunk)
                match = URL_RE.search(scanned)
                if match:
                    notified = True
                    _notify_about(tool.name, match.group(0).rstrip(".,;)"))
        return process.wait()
    except KeyboardInterrupt:
        # the child gets its own SIGINT directly (same process group) - just wait for it to
        # exit on its own, same as a plain subprocess.call does; don't force-kill it here.
        process.wait()
        return 130


def _notify_about(tool_name: str, url: str) -> None:
    """Deferred import: core.share already imports this module, so importing it back at load
    time would be circular - safe once both modules have actually finished loading."""
    from core import share

    threading.Thread(target=share.notify_share, args=(tool_name, url), daemon=True).start()
