"""A plain scratch pad in the terminal: one text area that saves itself, for drafting messages and parking links."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from kitlib import KIT_HOME, die, style, warn
from kitlib.clipboard import copy_to_clipboard
from kitlib.settings import tool_settings

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
URL_TRAILING = ".,;:!?)]}>'\""
DEFAULTS = {"autosave": True, "line_numbers": False, "wrap": True, "default": "scratch"}
SAVE_DELAY = 0.7     # seconds of quiet typing before an autosave
WATCH_INTERVAL = 1.5  # how often the open pad checks whether the file changed on disk

ACCENT = "#d97757"
TEXT = "#e8e3dc"
MUTED = "#8a847c"
DIM = "#4a4642"


# --- pad files ---------------------------------------------------------------------

def pad_dir() -> Path:
    override = os.environ.get("KIT_PAD_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "pad"


def check_name(name: str) -> str | None:
    """A reason the pad name can't be used, or None."""
    if not NAME_RE.fullmatch(name) or name.endswith("."):
        return f"invalid pad name '{name}': use letters, digits, '.', '_' and '-' (up to 64, starting with a letter or digit)"
    if name.lower().endswith(".prev"):
        return f"invalid pad name '{name}': names ending in '.prev' are used for cleared text"
    if name.split(".")[0].lower() in WINDOWS_RESERVED:
        return f"invalid pad name '{name}': reserved by Windows"
    return None


def pad_path(name: str) -> Path:
    return pad_dir() / f"{name}.txt"


def prev_path(name: str) -> Path:
    return pad_dir() / f"{name}.prev.txt"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").lstrip("\ufeff")
    except FileNotFoundError:
        return ""


def write_text(path: Path, text: str) -> None:
    """Atomic write: a half-written pad never replaces a good one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    for attempt in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:  # Windows: another program has the file open for a moment
            if attempt == 9:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def clear_pad(name: str, text: str) -> None:
    """Empty the pad, keeping the old text as <name>.prev.txt (unless there was nothing to keep)."""
    if text.strip():
        write_text(prev_path(name), text)
    write_text(pad_path(name), "")


def appended(existing: str, addition: str) -> str:
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return existing + separator + addition.rstrip("\r\n") + "\n"


def counts(text: str) -> tuple[int, int, int]:
    lines = text.count("\n") + 1 if text else 0
    return lines, len(text.split()), len(text)


def url_at(line: str, column: int) -> str | None:
    """The link under the cursor, else the nearest one on the line."""
    best: tuple[int, str] | None = None
    for match in URL_RE.finditer(line):
        url = match.group().rstrip(URL_TRAILING)
        start, end = match.start(), match.start() + len(url)
        distance = 0 if start <= column <= end else min(abs(column - start), abs(column - end))
        if best is None or distance < best[0]:
            best = (distance, url)
    if best is None:
        return None
    url = best[1]
    return url if "://" in url else "https://" + url


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


# --- reading the system clipboard ------------------------------------------------------

def paste_windows() -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    if not user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
        return None
    for _ in range(20):  # another program may be holding the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(13)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def paste_from_clipboard() -> str | None:
    """Text on the system clipboard, or None when it can't be read."""
    try:
        if os.name == "nt":
            text = paste_windows()
            return text.replace("\r\n", "\n") if text is not None else None
        candidates = []
        if sys.platform == "darwin" and shutil.which("pbpaste"):
            candidates.append(["pbpaste"])
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            candidates.append(["wl-paste", "--no-newline"])
        if os.environ.get("DISPLAY"):
            if shutil.which("xclip"):
                candidates.append(["xclip", "-selection", "clipboard", "-o"])
            if shutil.which("xsel"):
                candidates.append(["xsel", "--clipboard", "--output"])
        for command in candidates:
            try:
                result = subprocess.run(command, capture_output=True, check=True, timeout=5)
                return result.stdout.decode("utf-8", errors="replace")
            except (subprocess.SubprocessError, OSError):
                continue
    except OSError:
        pass
    return None


# --- UI --------------------------------------------------------------------------

try:
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Footer, Label, TextArea
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

CSS = f"""
Screen {{ background: #1a1918; color: {TEXT}; }}

#pad {{
    height: 1fr;
    border: round #3d3935;
    border-title-color: {ACCENT};
    border-title-style: bold;
    border-subtitle-color: #6b655e;
    background: #1a1918;
    padding: 0 1;
}}
#pad:focus {{ border: round {ACCENT}; }}
#pad .text-area--cursor-line {{ background: #1f1d1b; }}
#pad .text-area--gutter {{ color: {DIM}; }}
#pad .text-area--cursor-gutter {{ color: {MUTED}; background: #1f1d1b; }}
#pad .text-area--selection {{ background: #4a3a30; }}

Footer {{ background: #1a1918; color: {MUTED}; }}
Footer FooterKey {{ background: #1a1918; }}
Footer FooterKey .footer-key--key {{ background: #1a1918; color: {ACCENT}; text-style: bold; }}
Footer FooterKey .footer-key--description {{ color: {MUTED}; }}
Footer FooterKey:hover {{ background: #262422; }}

QuitScreen {{ align: center middle; background: rgba(0, 0, 0, 0.6); }}
#dialog {{
    width: 52;
    max-width: 100%;
    height: auto;
    border: round {ACCENT};
    border-title-color: {ACCENT};
    border-title-style: bold;
    background: #211f1d;
    padding: 1 2;
}}
#dialog Label {{ width: 100%; color: {TEXT}; }}
#dialog-buttons {{ width: 100%; height: 3; align: right middle; margin-top: 1; }}
#dialog Button {{
    min-width: 8; width: auto; height: 3; margin: 0 0 0 1; padding: 0 1;
    border: round #3d3935; background: #211f1d; color: {TEXT}; text-style: none;
}}
#dialog Button:hover {{ background: #211f1d; border: round {MUTED}; color: #ffffff; }}
#dialog Button:focus {{ border: round {ACCENT}; text-style: none; }}
#dialog Button.primary {{ color: {ACCENT}; text-style: bold; }}
"""


class PadArea(TextArea):
    """TextArea with notepad-style select-all, copy-everything and system-clipboard paste."""

    BINDINGS = [
        Binding("ctrl+a", "select_all", "Select all", show=False),
    ]

    def action_copy(self) -> None:
        self.app.copy_text(self.selected_text or self.text, "selection" if self.selected_text else "everything")

    def action_paste(self) -> None:
        if self.read_only:
            return
        text = paste_from_clipboard()
        if text is None:
            text = self.app.clipboard
        if text and (result := self._replace_via_keyboard(text, *self.selection)):
            self.move_cursor(result.end_location)


class QuitScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as dialog:
            dialog.border_title = " Unsaved changes "
            yield Label("Save the pad before quitting?")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Discard", id="discard")
                yield Button("Save", id="save", classes="primary")

    def on_mount(self) -> None:
        self.query_one("#save", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_cancel(self) -> None:
        self.dismiss("cancel")


class PadApp(App):
    TITLE = "kit pad"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "copy", "Copy", show=True),
        Binding("ctrl+l", "copy_line", "Copy line"),
        Binding("ctrl+o", "open_link", "Open link"),
        Binding("ctrl+n", "clear", "Clear"),
        Binding("ctrl+r", "restore", "Restore"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, name: str, autosave: bool = True, line_numbers: bool = False, wrap: bool = True) -> None:
        super().__init__()
        self.pad_name = name
        self.path = pad_path(name)
        self.autosave = autosave
        self.line_numbers = line_numbers
        self.wrap = wrap
        self.saved_text = read_text(self.path)
        self.disk_mtime = mtime(self.path)
        self.state = "saved"
        self._save_timer = None

    def compose(self) -> ComposeResult:
        yield PadArea(
            self.saved_text, id="pad", soft_wrap=self.wrap, show_line_numbers=self.line_numbers,
            tab_behavior="indent", highlight_cursor_line=True,
        )
        yield Footer()

    @property
    def area(self) -> PadArea:
        return self.query_one("#pad", PadArea)

    def on_mount(self) -> None:
        area = self.area
        area.border_title = f" {self.pad_name} "
        area.move_cursor(area.document.end)
        area.focus()
        self.set_interval(WATCH_INTERVAL, self.check_disk)
        self.update_status()

    # --- status and saving ---

    @property
    def dirty(self) -> bool:
        return self.area.text != self.saved_text

    def update_status(self) -> None:
        lines, words, chars = counts(self.area.text)
        status = Text()
        status.append(f" {lines} line{'s' if lines != 1 else ''} · {words} word{'s' if words != 1 else ''} · {chars} char{'s' if chars != 1 else ''}  ")
        if self.state == "error":
            status.append("● not saved ", style="#e5857a")
        elif self.dirty:
            status.append("● saving… " if self.autosave else "● unsaved (ctrl+s) ", style=ACCENT)
        else:
            status.append("✓ saved ", style="#7fb09b")
        self.area.border_subtitle = status

    @on(TextArea.Changed, "#pad")
    def text_changed(self) -> None:
        if self.state == "error" and not self.autosave:
            self.state = "saved"
        if self.autosave:
            if self._save_timer is not None:
                self._save_timer.stop()
            self._save_timer = self.set_timer(SAVE_DELAY, self.save)
        self.update_status()

    def save(self) -> bool:
        self._save_timer = None
        text = self.area.text
        if text == self.saved_text and self.path.exists():
            self.state = "saved"
            self.update_status()
            return True
        try:
            write_text(self.path, text)
        except OSError as exc:
            self.state = "error"
            self.notify(f"Couldn't save {self.path}: {exc}", severity="error", timeout=8)
            self.update_status()
            return False
        self.saved_text = text
        self.disk_mtime = mtime(self.path)
        self.state = "saved"
        self.update_status()
        return True

    def check_disk(self) -> None:
        """Pick up changes made elsewhere (e.g. `kit pad --add`) while nothing is waiting to be saved."""
        current = mtime(self.path)
        if current == self.disk_mtime:
            return
        self.disk_mtime = current
        disk_text = read_text(self.path)
        if disk_text == self.area.text:
            self.saved_text = disk_text
            return
        if self.dirty:
            self.notify("The pad file changed on disk too; your version here will be saved over it.",
                        severity="warning", timeout=6)
            return
        area = self.area
        cursor = area.cursor_location
        area.load_text(disk_text)
        self.saved_text = disk_text
        row = min(cursor[0], area.document.line_count - 1)
        area.move_cursor((row, min(cursor[1], len(area.document[row]))))
        self.update_status()
        self.notify("Reloaded: the pad changed on disk.", timeout=3)

    # --- clipboard and links ---

    def copy_text(self, text: str, what: str) -> None:
        if not text:
            self.notify("Nothing to copy.", timeout=2)
            return
        system = copy_to_clipboard(text)
        self.copy_to_clipboard(text)  # Textual's own clipboard, plus OSC 52 for terminals over SSH
        note = "" if system else " (through the terminal)"
        self.notify(f"Copied {what}{note}.", timeout=2)

    def action_copy(self) -> None:
        self.area.action_copy()

    def action_copy_line(self) -> None:
        area = self.area
        line = area.document[area.cursor_location[0]].strip()
        self.copy_text(line, "line")

    def action_open_link(self) -> None:
        area = self.area
        row, column = area.cursor_location
        url = url_at(area.document[row], column)
        if url is None:
            self.notify("No link on this line.", timeout=2)
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            self.notify(f"Couldn't open the browser: {exc}", severity="error", timeout=6)
            return
        self.notify(f"Opened {url}", timeout=3)

    # --- clear and restore ---

    def action_clear(self) -> None:
        area = self.area
        text = area.text
        if not text:
            return
        try:
            if text.strip():
                write_text(prev_path(self.pad_name), text)
        except OSError as exc:
            self.notify(f"Couldn't keep the old text, pad not cleared: {exc}", severity="error", timeout=8)
            return
        area.clear()
        area.focus()
        if not self.autosave:
            self.save()  # the old text is kept in .prev, so clearing is always safe to write
        self.notify("Cleared. ctrl+r brings the text back.", timeout=3)

    def action_restore(self) -> None:
        previous_file = prev_path(self.pad_name)
        previous = read_text(previous_file)
        if not previous:
            self.notify("Nothing to restore.", timeout=2)
            return
        area = self.area
        current = area.text
        try:
            if current.strip():
                write_text(previous_file, current)  # swap, so restoring twice gets back where you were
            else:
                previous_file.unlink(missing_ok=True)
        except OSError as exc:
            self.notify(f"Couldn't restore: {exc}", severity="error", timeout=8)
            return
        area.load_text(previous)
        area.move_cursor(area.document.end)
        area.focus()
        if not self.autosave:
            self.save()
        self.notify("Restored the cleared text." if not current.strip() else "Swapped with the cleared text.", timeout=3)

    # --- save and quit ---

    def action_save(self) -> None:
        if self.save():
            self.notify(f"Saved to {self.path}", timeout=2)

    async def action_quit(self) -> None:
        if self.autosave:
            if self._save_timer is not None:
                self._save_timer.stop()
            if self.save():
                self.exit()
            return
        if not self.dirty:
            self.exit()
            return

        def answered(choice: str | None) -> None:
            if choice == "discard" or (choice == "save" and self.save()):
                self.exit()

        self.push_screen(QuitScreen(), answered)


# --- command line -------------------------------------------------------------------

def list_pads() -> int:
    directory = pad_dir()
    pads = sorted(
        (p for p in directory.glob("*.txt") if not p.name.endswith(".prev.txt")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ) if directory.is_dir() else []
    if not pads:
        print(style("no pads yet - start one with: kit pad", "dim"))
        print(style(f"folder: {directory}", "dim"))
        return 0
    width = max(len(p.stem) for p in pads)
    print(style("PADS", "bold"))
    for path in pads:
        stat = path.stat()
        text = read_text(path)
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if len(first) > 50:
            first = first[:47] + "..."
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {path.stem.ljust(width)}  {human_size(stat.st_size):>8}  {style(modified, 'dim')}  {first or style('(empty)', 'dim')}")
    print()
    print(style(f"folder: {directory}", "dim"))
    return 0


def read_addition(text: str | None) -> str:
    if text is not None and text != "-":
        return text
    if sys.stdin is None or sys.stdin.isatty():
        die("give the text to add, e.g. kit pad --add \"https://example.com\" (or pipe it in)")
    data = sys.stdin.buffer.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:  # e.g. a Windows program writing in the ANSI code page
        text = data.decode(sys.stdin.encoding or "utf-8", errors="replace")
    return text.lstrip("\ufeff")


def main() -> int:
    settings = {**DEFAULTS, **tool_settings()}
    parser = argparse.ArgumentParser(
        prog="kit pad", description="A plain scratch pad in the terminal that saves itself.",
    )
    parser.add_argument("name", nargs="?", help=f"pad name (default: the pad.default setting, {settings['default']!r})")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true", help="list your pads")
    actions.add_argument("--print", action="store_true", help="print the pad's text")
    actions.add_argument("--add", nargs="?", const="-", metavar="TEXT",
                         help="add TEXT as a new line at the end (reads a pipe when TEXT is - or left out)")
    actions.add_argument("--copy", action="store_true", help="copy the pad's text to the clipboard")
    actions.add_argument("--clear", action="store_true", help="empty the pad (the old text is kept; ctrl+r in the pad brings it back)")
    actions.add_argument("--path", action="store_true", help="print the pad's file path")
    args = parser.parse_args()

    if args.list:
        if args.name:
            die("--list doesn't take a pad name")
        return list_pads()

    name = args.name or settings["default"] or "scratch"
    problem = check_name(name)
    if problem:
        die(problem)
    path = pad_path(name)

    if args.path:
        print(path)
        return 0
    if args.print:
        text = read_text(path)
        sys.stdout.write(text if not text or text.endswith("\n") else text + "\n")
        return 0
    if args.add is not None:
        addition = read_addition(args.add)
        if not addition.strip():
            die("nothing to add")
        write_text(path, appended(read_text(path), addition))
        print(style(f"added to {name}", "dim"), file=sys.stderr)
        return 0
    if args.copy:
        text = read_text(path)
        if not text:
            warn(f"pad '{name}' is empty - nothing copied")
            return 1
        if not copy_to_clipboard(text):
            die("couldn't copy: no clipboard available (Linux: install wl-clipboard, xclip or xsel)")
        print(style(f"copied {len(text)} characters from {name}", "dim"), file=sys.stderr)
        return 0
    if args.clear:
        clear_pad(name, read_text(path))
        print(style(f"cleared {name} - the old text is in {prev_path(name)}", "dim"), file=sys.stderr)
        return 0

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die("the pad needs a terminal - use --print, --add or --copy in scripts")
    PadApp(name, autosave=settings["autosave"], line_numbers=settings["line_numbers"], wrap=settings["wrap"]).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
