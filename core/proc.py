"""Killing a process tree, and checking whether one is still running, by pid alone.

`kit hub` tracks jobs through a live `subprocess.Popen`, but `kit share` has to check on
and stop processes from a later, separate `kit` invocation, where only the pid (recorded
in the registry) survives - so these take a bare pid rather than a Popen.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")


def kill_tree(pid: int) -> None:
    """Kill a process and its descendants, best-effort."""
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            parent = psutil.Process(pid)
            victims = parent.children(recursive=True) + [parent]
        except psutil.NoSuchProcess:
            return
        for victim in victims:
            try:
                victim.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(victims, timeout=5)
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def alive(pid: int, create_time: float | None = None) -> bool:
    """Whether pid is still running, and - if create_time is given - is still that same process.

    Without the create_time check, a pid that has since been reused by an unrelated process
    would be reported as still alive.
    """
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        if create_time is not None and abs(proc.create_time() - create_time) > 1:
            return False
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    if IS_WINDOWS:
        return True  # best effort without psutil
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
