"""kit share: launch web tools in the background, tracked by kit, so they can be listed and
stopped centrally - from a terminal, without needing kit hub open.

A tiny JSON registry (see paths.state_dir()) records each launch's name, tool, pid and log
file. It's read fresh on every command, so `start` in one terminal and `stop`/`list` in
another - or after closing everything and coming back later - all see the same jobs. Each
job's own stdout/stderr goes straight to a log file rather than through a pipe: nothing is
left reading it once this command exits, and unlike a live-streamed job, a share is meant to
outlive the command that started it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

from kitlib import die, style, warn
from kitlib.qr import qr_lines

from core import paths, proc, registry, runner
from core.registry import KIT_HOME, Tool, current_platform

IS_WINDOWS = sys.platform.startswith("win")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
NAME_RE = re.compile(r"[\w.-]+")
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
URL_SCAN_LIMIT = 8_000
URL_POLL_ATTEMPTS = 6
URL_POLL_INTERVAL = 0.5


# --- registry -------------------------------------------------------------------------

def _registry_path() -> Path:
    return paths.state_dir() / "share.json"


def _log_dir() -> Path:
    return paths.state_dir() / "share-logs"


def _load() -> dict[str, dict]:
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _is_alive(entry: dict) -> bool:
    return proc.alive(entry["pid"], entry.get("create_time"))


# --- registration for launches kit starts on your behalf (e.g. kit hub's Launch button) ----
#
# `start` below is the CLI path (`kit share start ...`); these let another part of kit - kit
# hub, so far - add its own launches to the same registry, so `kit share list/stop` sees and
# can stop everything regardless of how it was started.

def register(name: str, tool: str, pid: int, cwd: str, log: str | None = None,
            url: str | None = None, create_time: float | None = None) -> None:
    data = _load()
    data[name] = {"name": name, "tool": tool, "args": [], "pid": pid, "started": time.time(),
                  "log": log, "url": url, "cwd": cwd, "create_time": create_time}
    _save(data)


def unregister(name: str) -> None:
    data = _load()
    if name in data:
        del data[name]
        _save(data)


def update_url(name: str, url: str) -> None:
    data = _load()
    if name in data:
        data[name]["url"] = url
        _save(data)


def unique_name(base: str) -> str:
    """base, or base plus a short suffix if base already names another still-running job."""
    existing = _load().get(base)
    if existing is None or not _is_alive(existing):
        return base
    return f"{base}-{secrets.token_hex(3)}"


# --- finding and launching a tool -------------------------------------------------------

def _discover() -> registry.Registry:
    from core.cli import RESERVED  # core.cli is the source of the built-in names

    return registry.discover(RESERVED)


def _find_tool(name: str) -> Tool:
    tool = _discover().resolve(name)
    if tool is None:
        die(f"unknown tool '{name}' - run 'kit list' to see what's available")
    if not tool.supported:
        die(f"'{tool.name}' isn't available on {current_platform()}")
    if not tool.web:
        die(f"'{tool.name}' isn't a web tool, so there's nothing to share - see: kit help {tool.name}")
    return tool


def _accepts_no_open(tool: Tool) -> bool:
    entry = tool.entry_path()
    try:
        return entry is not None and "--no-open" in entry.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _popen_options() -> dict:
    options: dict = {"stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True  # its own process group, so stop can end the whole tree
    return options


def _detect_url(log_path: Path) -> str | None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")[:URL_SCAN_LIMIT]
    except OSError:
        return None
    match = URL_RE.search(text)
    return match.group(0).rstrip(".,;)") if match else None


def _poll_for_url(log_path: Path) -> str | None:
    for _ in range(URL_POLL_ATTEMPTS):
        time.sleep(URL_POLL_INTERVAL)
        found = _detect_url(log_path)
        if found:
            return found
    return None


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


# --- tailscale --------------------------------------------------------------------------
#
# `tailscale serve` configures the already-running tailscaled daemon to proxy a port to
# this machine's tailnet address over HTTPS; it's a one-shot call, not a process to track -
# unlike a public tunnel (cloudflared etc.), which stays running and would need its own pid
# tracked alongside the app's. That's not built yet - see `kit help share`.

def _tailscale_share(local_url: str) -> str | None:
    """Point `tailscale serve` at local_url's port; returns the tailnet address for the
    same path and query, or None if that didn't work - never fatal: the app itself is
    already running by the time this runs, and stays running either way."""
    if not shutil.which("tailscale"):
        warn("tailscale isn't installed or not on PATH - see https://tailscale.com/download "
            "(the app itself is still running - share with --lan instead, or install tailscale and stop/start again)")
        return None
    parts = urlsplit(local_url)
    if not parts.port:
        warn(f"couldn't tell which port {local_url} is on, so there's nothing to point tailscale at")
        return None
    try:
        result = subprocess.run(["tailscale", "serve", "--bg", str(parts.port)],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"couldn't run tailscale: {exc}")
        return None
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        warn(f"tailscale serve failed:\n{output}")
        return None
    match = URL_RE.search(output)
    if not match:
        print(style("  tailscale serve ran, but no address was found in its output - "
                    "check: tailscale serve status", "dim"))
        return None
    tailnet = urlsplit(match.group(0).rstrip(".,;)"))
    return urlunsplit((tailnet.scheme, tailnet.netloc, parts.path, parts.query, ""))


# --- commands -------------------------------------------------------------------------

def cmd_start(tool_name: str, tool_args: list[str], name: str | None, tailscale: bool = False) -> int:
    tool = _find_tool(tool_name)
    name = name or tool.name
    if not NAME_RE.fullmatch(name):
        die("--name may only contain letters, digits, dots, dashes and underscores")

    data = _load()
    existing = data.get(name)
    if existing and _is_alive(existing):
        die(f"'{name}' is already running (pid {existing['pid']}) - stop it first, or pick another --name")

    args = list(tool_args)
    if "--no-open" not in args and _accepts_no_open(tool):
        args.append("--no-open")  # this is a background launch - nothing here to show a browser
    try:
        command = runner.build_command(tool, args)
    except runner.RunError as exc:
        die(f"{tool.name}: {exc}")

    log_path = _log_dir() / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = runner.tool_env(tool)
    env["KIT_SHARE"] = "1"
    cwd = str(KIT_HOME)
    with open(log_path, "w", encoding="utf-8") as log_file:
        try:
            process = subprocess.Popen(command, env=env, cwd=cwd, stdout=log_file,
                                       stderr=subprocess.STDOUT, **_popen_options())
        except OSError as exc:
            die(f"couldn't start {command[0]}: {exc}")

    entry = {"name": name, "tool": tool.name, "args": args, "pid": process.pid,
             "started": time.time(), "log": str(log_path), "url": None, "cwd": cwd}
    try:
        import psutil

        entry["create_time"] = psutil.Process(process.pid).create_time()
    except Exception:
        entry["create_time"] = None
    data[name] = entry
    _save(data)

    print(f"started {style(name, 'bold')}  (tool: {tool.name}, pid {process.pid})")
    url = _poll_for_url(log_path)
    if url:
        entry["url"] = url
        data[name] = entry
        _save(data)
        print(f"  {style(url, 'bold', 'green')}")
        if urlparse(url).hostname not in LOCAL_HOSTS:
            for line in qr_lines(url):
                print(" ", line)
    else:
        print(style(f"  no address detected yet - check: kit share logs {name}", "dim"))

    if tailscale and not url:
        warn(f"no local address was detected for '{name}', so there's nothing to point tailscale at "
            f"- check: kit share logs {name}")
    elif tailscale:
        shared = _tailscale_share(url)
        if shared:
            entry["url"] = shared
            data[name] = entry
            _save(data)
            print(f"  {style(shared, 'bold', 'green')}  (via tailscale)")
            for line in qr_lines(shared):
                print(" ", line)

    print(style(f"  stop it with: kit share stop {name}", "dim"))
    return 0


def cmd_stop(name: str | None, all_: bool) -> int:
    data = _load()
    if all_:
        targets = list(data.items())
        if not targets:
            print("nothing is currently shared")
            return 0
    else:
        entry = data.get(name)
        if entry is None:
            die(f"no shared app named '{name}' - see: kit share list")
        targets = [(name, entry)]

    for key, entry in targets:
        if _is_alive(entry):
            proc.kill_tree(entry["pid"])
        del data[key]
    _save(data)
    print(f"stopped {len(targets)} app(s)" if all_ else f"stopped '{name}'")
    return 0


def cmd_list() -> int:
    data = _load()
    changed = False
    rows = []
    for name in sorted(data):
        entry = data[name]
        if not _is_alive(entry):
            where = f" (log: {entry['log']})" if entry.get("log") else " (started from kit hub)"
            print(style(f"'{name}' is no longer running - removing it from the list{where}", "dim"))
            del data[name]
            changed = True
            continue
        if not entry.get("url") and entry.get("log"):
            found = _detect_url(Path(entry["log"]))
            if found:
                entry["url"] = found
                changed = True
        rows.append(entry)
    if changed:
        _save(data)

    if not rows:
        print("nothing is currently shared - start one with: kit share start <tool>")
        return 0

    name_width = max(len(r["name"]) for r in rows)
    tool_width = max(len(r["tool"]) for r in rows)
    for r in rows:
        uptime = _format_duration(time.time() - r["started"])
        url = r.get("url") or style("(no address detected yet)", "dim")
        print(f"  {style(r['name'].ljust(name_width), 'green')}  {r['tool'].ljust(tool_width)}  "
              f"pid {str(r['pid']).ljust(7)} up {uptime.ljust(8)} {url}")
    return 0


def cmd_logs(name: str, follow: bool) -> int:
    data = _load()
    entry = data.get(name)
    if entry is None:
        die(f"no shared app named '{name}' - see: kit share list")
    if not entry.get("log"):
        die(f"'{name}' was started from kit hub, not the command line - its output is in the hub's browser tab, "
            "not a log file")
    path = Path(entry["log"])
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            sys.stdout.write(f.read())
            sys.stdout.flush()
            if follow:
                try:
                    while True:
                        chunk = f.read()
                        if chunk:
                            sys.stdout.write(chunk)
                            sys.stdout.flush()
                        else:
                            time.sleep(0.4)
                except KeyboardInterrupt:
                    pass
    except OSError as exc:
        die(f"couldn't read {path}: {exc}")
    return 0


# --- main -------------------------------------------------------------------------------

def main(args: list[str]) -> int:
    tool_args: list[str] = []
    if "--" in args:
        index = args.index("--")
        args, tool_args = args[:index], args[index + 1:]

    parser = argparse.ArgumentParser(
        prog="kit share",
        description="Launch web tools in the background, tracked by kit, so you can list and stop them "
                    "centrally - from a terminal, without needing kit hub open.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("start", help="launch a web tool in the background")
    p.add_argument("tool")
    p.add_argument("--name", help="a name for this instance (default: the tool's name)")
    p.add_argument("--tailscale", action="store_true",
                   help="also point 'tailscale serve' at it, for devices on your tailnet (needs tailscale installed)")

    p = sub.add_parser("stop", help="stop a shared app")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true", help="stop every shared app")

    sub.add_parser("list", help="show what's currently shared (the default)")

    p = sub.add_parser("logs", help="show a shared app's output")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new output")

    ns = parser.parse_args(args)
    command = ns.command or "list"

    if command == "start":
        return cmd_start(ns.tool, tool_args, ns.name, ns.tailscale)
    if command == "stop":
        if not ns.all and not ns.name:
            parser.error("stop needs a name, or --all")
        return cmd_stop(ns.name, ns.all)
    if command == "logs":
        return cmd_logs(ns.name, ns.follow)
    return cmd_list()
