"""What's listening, who owns it, and whether kit itself is managing it.

Shared by the one-shot CLI (main.py) and the live view (port_ui.py), the same way kit top
splits its scanning (top_scan.py) from its CLI and its live view.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import psutil

IS_WINDOWS = os.name == "nt"
ALL_INTERFACES = ("0.0.0.0", "::")
# Stopping these usually takes down something bigger (Docker, WSL, databases, the OS itself).
CAUTION = {
    "system", "svchost", "lsass", "services", "wininit", "com.docker.backend", "com.docker.proxy",
    "wslrelay", "vpnkit", "dockerd", "docker-proxy", "containerd", "sshd", "systemd", "launchd", "postgres", "mysqld",
}


# --- listening sockets --------------------------------------------------------------

def connections(kind: str) -> list:
    """Raises psutil.AccessDenied on Linux/macOS without enough privilege - callers decide how to handle it."""
    return psutil.net_connections(kind=kind)


def process_details(pid: int | None, cache: dict[int, tuple[str, str]]) -> tuple[str, str]:
    if not pid:
        return "-", ""
    if pid not in cache:
        try:
            process = psutil.Process(pid)
            name = process.name()
            try:
                command = " ".join(process.cmdline())
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                command = ""
        except psutil.NoSuchProcess:
            name, command = "(exited)", ""
        except psutil.AccessDenied:
            name, command = "(access denied)", ""
        cache[pid] = (name, command)
    return cache[pid]


def protocol(conn) -> str:
    return "tcp" if conn.type == socket.SOCK_STREAM else "udp"


def address(addr) -> str:
    if not addr:
        return "-"
    return f"[{addr.ip}]:{addr.port}" if ":" in addr.ip else f"{addr.ip}:{addr.port}"


def owners(port: int, conns: list) -> list[int]:
    """PIDs that own the port: listening TCP sockets and bound UDP sockets, not clients connecting to it."""
    return sorted({
        c.pid for c in conns
        if c.pid and c.laddr and c.laddr.port == port
        and (c.status == psutil.CONN_LISTEN or c.type == socket.SOCK_DGRAM)
    })


def can_bind(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# --- kit share cross-reference --------------------------------------------------------
#
# Tools don't import kit's own core.* package - they run as separate processes with only
# lib/kitlib on their path, by design (see README, "Adding a tool"). So this reads
# core.share's registry file directly, at the same well-known location core/paths.py
# resolves, rather than importing it. Best-effort and read-only: if the file is missing,
# unreadable, or its shape ever changes, this just stops finding matches, not crashes.

def _kit_state_dir() -> Path:
    override = os.environ.get("KIT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "kit"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kit"


def kit_share_map() -> dict[int, str]:
    """pid -> the name it's shared under (kit share start, or kit hub's Launch).

    Maps every descendant of each registered pid too, not just the pid itself: a tool's entry
    point is often a launcher that runs the real thing as a child (subprocess.call, not an
    exec) rather than becoming it, so the pid that actually holds the listening socket is
    frequently a grandchild of the one kit share recorded.
    """
    try:
        data = json.loads((_kit_state_dir() / "share.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[int, str] = {}
    for name, entry in data.items():
        if not (isinstance(entry, dict) and isinstance(entry.get("pid"), int)):
            continue
        label = entry.get("name") or name
        pid = entry["pid"]
        result[pid] = label
        try:
            for child in psutil.Process(pid).children(recursive=True):
                result[child.pid] = label
        except psutil.Error:
            pass
    return result
