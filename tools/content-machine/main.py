"""Start Content Machine, or run any of its `cm` commands, from kit.

Content Machine is its own project with its own environment, so this only finds that
project's `cm` and hands the arguments over; it imports nothing from it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from kitlib import die
from kitlib.settings import tool_settings


def find_cm(home: Path) -> Path:
    scripts = home / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    cm = scripts / ("cm.exe" if sys.platform == "win32" else "cm")
    if not cm.exists():
        die(f"no cm in {scripts} - run 'uv sync' in {home} first")
    return cm


def main() -> None:
    home = tool_settings()["home"]
    if not home:
        die("kit doesn't know where Content Machine is yet - run: "
            "kit config set content-machine.home <folder>")
    home = Path(home).expanduser()
    if not home.is_dir():
        die(f"content-machine.home is {home}, which isn't a folder - fix it with kit config set")

    args = sys.argv[1:]
    # No command, or only options for it (--no-open, --port, --lan), starts the app.
    if not args or args[0].startswith("-"):
        args = ["serve", *args]

    try:
        # From the project folder, so cm reads the same .env as when it is run there.
        sys.exit(subprocess.run([str(find_cm(home)), *args], cwd=home).returncode)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
