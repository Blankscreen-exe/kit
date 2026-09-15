"""Runs a tool's entry point with the right interpreter and kit's environment."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from core.registry import KIT_HOME, Tool, current_platform


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
    try:
        return subprocess.call(command, env=tool_env(tool))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise RunError(f"could not start {command[0]}: {exc}") from exc
