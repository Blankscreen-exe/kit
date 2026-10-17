"""Write, find and fill reusable AI prompts in seconds, without managing files."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from kitlib import die, style
from kitlib.settings import tool_settings

CONSOLE_SCRIPT = "prompt-cache"
SOURCE_ENV = "PROMPT_CACHE_HOME"
REPO_URL = "https://github.com/Blankscreen-exe/prompt-cache"

DEFAULTS = {"data": ""}


def _from_path() -> list[str] | None:
    """The installed console script, if `uv tool install` has been run."""
    found = shutil.which(CONSOLE_SCRIPT)
    return [found] if found else None


def _from_source() -> list[str] | None:
    """A source checkout, run through uv.

    prompt-cache is a separate project with its own dependencies, so it is never
    imported into kit's environment — uv runs it in its own.
    """
    raw = os.environ.get(SOURCE_ENV, "").strip()
    if not raw:
        return None
    source = Path(raw).expanduser()
    if not (source / "pyproject.toml").is_file():
        die(f"${SOURCE_ENV} is set to {source}, which has no pyproject.toml")
    uv = shutil.which("uv")
    if not uv:
        die(f"${SOURCE_ENV} is set but uv is not on PATH, so the source cannot be run")
    return [uv, "run", "--project", str(source), CONSOLE_SCRIPT]


def _not_installed() -> None:
    die(
        "prompt-cache is not installed.\n\n"
        f"  {style('Install it:', 'bold')}\n"
        f"    git clone {REPO_URL}\n"
        f"    cd prompt-cache\n"
        "    uv tool install .\n\n"
        f"  {style('Or point kit at a checkout:', 'bold')}\n"
        f"    set {SOURCE_ENV} to the folder you cloned into"
    )


def main(argv: list[str]) -> int:
    command = _from_path() or _from_source()
    if command is None:
        _not_installed()

    env = os.environ.copy()
    # kit runs inside its own venv, and uv warns on every launch when VIRTUAL_ENV
    # points somewhere other than the project it is running. prompt-cache has its own
    # environment, so drop the inherited one rather than print noise each time.
    env.pop("VIRTUAL_ENV", None)

    settings = {**DEFAULTS, **tool_settings()}
    data_dir = str(settings.get("data") or "").strip()
    if data_dir:
        # Let `kit config` decide where the database lives, without prompt-cache
        # needing to know kit exists.
        env["PROMPT_CACHE_DATA"] = str(Path(data_dir).expanduser())

    try:
        return subprocess.call([*command, *argv], env=env)
    except FileNotFoundError:
        _not_installed()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
