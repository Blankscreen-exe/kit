"""Find a Chromium-based browser (Edge, Chrome, Chromium) and open pages in their own app window."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

BROWSER_PATHS = {
    "windows": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ],
    "macos": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}
BROWSER_COMMANDS = [
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "microsoft-edge", "microsoft-edge-stable", "msedge", "chrome",
]


def _platform() -> str:
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def configured_browser() -> str:
    """The [kit] browser setting ($KIT_BROWSER wins over the settings file); "" when not set."""
    try:
        from kitlib.settings import kit_settings
        return kit_settings().get("browser") or ""
    except Exception:
        return os.environ.get("KIT_BROWSER", "")


def find_browser(explicit: str | None = None) -> str | None:
    """`explicit` (a command name or path), else the [kit] browser setting / $KIT_BROWSER, else the first
    Edge/Chrome/Chromium found.

    Returns None when nothing usable is found (including when a chosen browser doesn't exist).
    """
    choice = explicit or configured_browser()
    if choice:
        return shutil.which(choice) or (choice if Path(choice).is_file() else None)

    for candidate in BROWSER_PATHS.get(_platform(), []):
        path = Path(os.path.expandvars(candidate))
        if path.is_file():
            return str(path)
    for name in BROWSER_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


def app_profile_dir() -> Path:
    """Separate browser profile for app windows, so they open even while the normal browser is running."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "kit" / "app-window"


def app_window_command(url: str, browser: str | None = None) -> list[str] | None:
    """The command that opens `url` in an app window, or None if no Chromium-based browser is installed."""
    browser = browser or find_browser()
    if browser is None:
        return None
    return [browser, f"--app={url}", f"--user-data-dir={app_profile_dir()}", "--no-first-run", "--no-default-browser-check"]


def open_app_window(url: str, browser: str | None = None) -> None:
    """Open `url` in a window without tabs or an address bar; falls back to a normal browser tab."""
    command = app_window_command(url, browser)
    if command is None:
        webbrowser.open(url)
        return
    app_profile_dir().mkdir(parents=True, exist_ok=True)
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
