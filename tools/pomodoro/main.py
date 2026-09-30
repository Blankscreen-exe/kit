"""Pomodoro focus timer: a mouse-clickable terminal app with session history."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from kitlib import KIT_HOME, die, style
from kitlib.settings import SettingsError, set_value, tool_settings
from kitlib.settings import load as load_settings
from kitlib.theme import ACCENT, BAD, DIM, GOOD, MUTED, TEXT

try:
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Center, Grid, Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Footer, Input, Label, ProgressBar, Static

    from kitlib.tui import BigDigits, KitApp, brand, title
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

PHASE_NAMES = {"focus": "Focus", "short": "Short break", "long": "Long break"}
ACCENTS = {"focus": ACCENT, "short": GOOD, "long": GOOD}


# --- settings and history ------------------------------------------------------

@dataclass
class Settings:
    focus: int = 25
    short: int = 5
    long: int = 15
    every: int = 4

    def validate(self) -> str | None:
        limits = {"focus": (1, 240), "short": (1, 120), "long": (1, 240), "every": (1, 12)}
        for key, (low, high) in limits.items():
            value = getattr(self, key)
            if not low <= value <= high:
                return f"{key} must be between {low} and {high}"
        return None


def data_path() -> Path:
    override = os.environ.get("KIT_POMODORO_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "pomodoro.json"


class History:
    """Completed focus sessions and saved settings, in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"sessions": [], "settings": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # Keep the unreadable file instead of silently overwriting it on the next save.
            try:
                path.replace(path.with_suffix(".json.bak"))
            except OSError:
                pass

    def sessions(self) -> list[tuple[datetime, int, str]]:
        parsed = []
        for entry in self.data.get("sessions", []):
            try:
                when = datetime.fromisoformat(entry["date"]).astimezone()
                parsed.append((when, int(entry["minutes"]), str(entry.get("task", ""))))
            except (KeyError, TypeError, ValueError):
                continue
        return parsed

    def add(self, minutes: int, task: str) -> None:
        self.data.setdefault("sessions", []).append(
            {"date": datetime.now().astimezone().isoformat(timespec="seconds"), "minutes": minutes, "task": task}
        )
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def totals(self, since: datetime | None = None) -> tuple[int, int]:
        chosen = [m for when, m, _ in self.sessions() if since is None or when >= since]
        return len(chosen), sum(chosen)


def start_of_today() -> datetime:
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def format_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


# --- timer logic (no UI, so it can be tested directly) -------------------------

class Timer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.phase = "focus"
        self.cycle_done = 0  # focus sessions finished since the last long break
        self.running = False
        self.total = self.remaining = self.phase_seconds()

    def phase_seconds(self) -> float:
        return getattr(self.settings, self.phase) * 60.0

    @property
    def started(self) -> bool:
        return self.running or self.remaining < self.total

    @property
    def elapsed(self) -> float:
        return self.total - self.remaining

    def advance(self, seconds: float) -> bool:
        """Count down while running. True when the phase has just run out."""
        if not self.running or self.remaining <= 0:
            return False
        self.remaining = max(0.0, self.remaining - seconds)
        return self.remaining == 0

    def adjust(self, seconds: float) -> None:
        elapsed = self.elapsed
        self.remaining = max(0.0, self.remaining + seconds)
        self.total = max(1.0, elapsed + self.remaining)

    def reset(self) -> None:
        self.running = False
        self.total = self.remaining = self.phase_seconds()

    def next_phase(self, completed: bool) -> None:
        if self.phase == "focus":
            if completed:
                self.cycle_done += 1
            self.phase = "long" if completed and self.cycle_done >= self.settings.every else "short"
        else:
            if self.phase == "long":
                self.cycle_done = 0
            self.phase = "focus"
        self.reset()

    def clock(self) -> str:
        seconds = math.ceil(self.remaining)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"


def desktop_alert(title: str, message: str) -> None:
    """Best-effort OS-level nudge; the in-app notification and bell always happen."""
    try:
        if sys.platform.startswith("win"):
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif sys.platform == "darwin":
            script = f'display notification "{message}" with title "{title}"'
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "kit pomodoro", title, message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# --- UI --------------------------------------------------------------------------

# Focus is kit's yellow; a break turns the phase colour green.
CSS = f"""
Screen {{ align: center middle; }}

#topbar {{ width: 76; max-width: 100%; height: 1; margin-bottom: 1; }}
#brand {{ width: 1fr; }}
#today {{ width: auto; }}

#card {{ width: 76; max-width: 100%; height: auto; padding: 1 2 0 2; }}
#phase {{ width: 100%; height: 1; content-align: center middle; color: {ACCENT}; text-style: bold; }}
#clock {{ color: {TEXT}; margin: 1 0 1 0; }}
#progress {{ width: auto; height: 1; }}
#progress Bar {{ width: 50; }}
#dots {{ width: 100%; height: 1; content-align: center middle; margin: 1 0 1 0; }}
#task {{ margin: 0 0 1 0; }}

#controls {{ width: 100%; height: 3; align: center middle; margin-bottom: 1; }}
#controls Button {{ margin: 0 1 0 0; }}
#start {{ width: 14; }}
#settings {{ margin: 0; }}

.-break #phase {{ color: {GOOD}; }}
.-break #card {{ border-title-background: {GOOD}; }}
.-break #progress Bar > .bar--bar, .-break #progress Bar > .bar--complete {{ color: {GOOD}; }}
.-break Button.primary {{ background: {GOOD}; border: heavy {GOOD}; }}
.-break #task:focus {{ border: heavy {GOOD}; }}

#dialog {{ width: 50; max-width: 100%; height: auto; }}
#fields {{ grid-size: 2; grid-columns: 1fr 12; grid-rows: 3; grid-gutter: 0 1; height: auto; }}
#fields Label {{ height: 3; width: 100%; content-align: left middle; text-style: bold; }}
#settings-error {{ color: {BAD}; height: auto; }}
#dialog-buttons {{ width: 100%; height: 3; align: right middle; margin-top: 1; }}
#dialog-buttons Button {{ margin: 0 0 0 1; }}
"""

SETTING_FIELDS = [
    ("focus", "Focus (minutes)"),
    ("short", "Short break (minutes)"),
    ("long", "Long break (minutes)"),
    ("every", "Long break every N sessions"),
]


class SettingsScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="dialog") as dialog:
            dialog.border_title = title("Settings")
            with Grid(id="fields"):
                for key, label in SETTING_FIELDS:
                    yield Label(label)
                    yield Input(str(getattr(self.settings, key)), type="integer", id=f"set-{key}")
            yield Static("", id="settings-error")
            with Horizontal(id="dialog-buttons"):
                yield Button("CANCEL", id="cancel")
                yield Button("SAVE", id="save", classes="primary")

    def on_mount(self) -> None:
        self.query_one("#set-focus", Input).focus()

    @on(Button.Pressed, "#save")
    def save_clicked(self) -> None:
        self.save()

    @on(Input.Submitted)
    def field_submitted(self) -> None:
        self.save()

    def save(self) -> None:
        try:
            values = {key: int(self.query_one(f"#set-{key}", Input).value) for key, _ in SETTING_FIELDS}
        except ValueError:
            self.query_one("#settings-error", Static).update("Every value must be a whole number.")
            return
        settings = Settings(**values)
        problem = settings.validate()
        if problem:
            self.query_one("#settings-error", Static).update(problem.capitalize() + ".")
            return
        self.dismiss(settings)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PomodoroApp(KitApp):
    TITLE = "kit pomodoro"
    CSS = CSS
    AUTO_FOCUS = None
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle", "Start/Pause"),
        Binding("r", "reset", "Reset"),
        Binding("s", "skip", "Skip"),
        Binding("minus", "less", "-1 min"),
        Binding("plus,equals_sign", "more", "+1 min"),
        Binding("t", "edit_task", "Task"),
        Binding("comma,o", "settings", "Settings"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "blur", "Done", show=False),
    ]

    def __init__(self, settings: Settings, history: History, task: str = "", speed: float = 1.0) -> None:
        super().__init__()
        self.settings = settings
        self.history = history
        self.initial_task = task
        self.speed = speed
        self.timer = Timer(settings)
        self._last_tick = time.monotonic()

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(brand("kit pomodoro"), id="brand")
            yield Static("", id="today")
        with Vertical(id="card", classes="slab"):
            yield Static("", id="phase")
            with Center():
                yield BigDigits(self.timer.clock(), id="clock", short_below=30)
            with Center():
                yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Static("", id="dots")
            yield Input(self.initial_task, placeholder="What are you working on?", id="task")
            with Horizontal(id="controls"):
                yield Button("▶ START", id="start", classes="primary")
                yield Button("↺ RESET", id="reset")
                yield Button("» SKIP", id="skip")
                yield Button("−1M", id="less")
                yield Button("+1M", id="more")
                yield Button("⚙", id="settings")
        yield Footer()

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False  # keys go to the app's shortcuts; clicks still work
        self.set_interval(0.2, self.tick)
        self.refresh_view()

    # --- timing ---

    def tick(self) -> None:
        now = time.monotonic()
        delta, self._last_tick = (now - self._last_tick) * self.speed, now
        if self.timer.advance(delta):
            self.finish_phase()
        self.refresh_view()

    def finish_phase(self) -> None:
        finished = self.timer.phase
        if finished == "focus":
            self.history.add(round(self.timer.total / 60), self.query_one("#task", Input).value.strip())
        self.timer.next_phase(completed=True)
        upcoming = PHASE_NAMES[self.timer.phase]
        if finished == "focus":
            title, message = "Focus session done", f"Time for a {upcoming.lower()}. Press space to start it."
        else:
            title, message = "Break is over", "Ready to focus? Press space to start."
        self.notify(message, title=title, timeout=8)
        self.bell()
        desktop_alert(title, message)

    # --- view ---

    def refresh_view(self) -> None:
        timer = self.timer
        accent = ACCENTS[timer.phase]
        main = self.screen_stack[0]  # not self.screen: that is the settings dialog while it's open
        main.set_class(timer.phase != "focus", "-break")

        state = "running" if timer.running else "paused" if timer.started else "ready"
        phase = Text()
        phase.append(f"● {PHASE_NAMES[timer.phase].upper()}")
        phase.append("  ·  ", style=DIM)
        phase.append(state, style=f"not bold {MUTED}")
        self.query_one("#phase", Static).update(phase)

        clock = self.query_one("#clock", BigDigits)
        if clock.value != timer.clock():
            clock.update(timer.clock())
        self.query_one("#progress", ProgressBar).update(total=timer.total, progress=timer.elapsed)

        every = self.settings.every
        filled = every if timer.phase == "long" else min(timer.cycle_done, every)
        dots = Text()
        for index in range(every):
            dots.append("● " if index < filled else "○ ", style=accent if index < filled else DIM)
        position = min(timer.cycle_done + (1 if timer.phase == "focus" else 0), every)
        dots.append(f"  session {max(position, 1)} of {every}", style=MUTED)
        self.query_one("#dots", Static).update(dots)

        card = self.query_one("#card")
        card.border_title = title(PHASE_NAMES[timer.phase])
        card.border_subtitle = f" {self.settings.focus} · {self.settings.short} · {self.settings.long} min "

        start = self.query_one("#start", Button)
        label = "‖ PAUSE" if timer.running else ("▶ RESUME" if timer.started else "▶ START")
        if str(start.label) != label:
            start.label = label

        count, minutes = self.history.totals(start_of_today())
        today = Text()
        today.append("today  ", style=MUTED)
        today.append(f"{count} session{'s' if count != 1 else ''}", style=f"bold {TEXT}")
        today.append("  ·  ", style=DIM)
        today.append(format_minutes(minutes), style=f"bold {TEXT}")
        today.append(" focus", style=MUTED)
        self.query_one("#today", Static).update(today)

    # --- actions ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "start": self.action_toggle, "reset": self.action_reset, "skip": self.action_skip,
            "less": self.action_less, "more": self.action_more, "settings": self.action_settings,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def action_toggle(self) -> None:
        if self.timer.remaining <= 0:
            self.timer.reset()
        self.timer.running = not self.timer.running
        self._last_tick = time.monotonic()
        self.refresh_view()

    def action_reset(self) -> None:
        self.timer.reset()
        self.refresh_view()

    def action_skip(self) -> None:
        self.timer.next_phase(completed=False)
        self.notify(f"Skipped to {PHASE_NAMES[self.timer.phase].lower()}.", timeout=3)
        self.refresh_view()

    def action_less(self) -> None:
        self.timer.adjust(-60)
        self.refresh_view()

    def action_more(self) -> None:
        self.timer.adjust(60)
        self.refresh_view()

    def action_edit_task(self) -> None:
        self.query_one("#task", Input).focus()

    def action_blur(self) -> None:
        self.set_focus(None)

    @on(Input.Submitted, "#task")
    def task_submitted(self) -> None:
        self.set_focus(None)

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.settings), self.apply_settings)

    def apply_settings(self, settings: Settings | None) -> None:
        if settings is None:
            return
        self.settings = self.timer.settings = settings
        if not self.timer.started:
            self.timer.reset()
        try:
            for key, value in asdict(settings).items():
                set_value("pomodoro", key, value)
        except SettingsError as exc:
            self.notify(f"Couldn't save settings: {exc}", severity="error", timeout=8)
        else:
            self.notify("Settings saved.", timeout=3)
        self.refresh_view()


# --- stats and entry point ---------------------------------------------------------

def print_stats(history: History) -> int:
    today = start_of_today()
    week = today - timedelta(days=today.weekday())
    rows = [("Today", history.totals(today)), ("This week", history.totals(week)), ("All time", history.totals())]
    print(style("POMODORO", "bold"))
    for label, (count, minutes) in rows:
        print(f"  {label:<10} {count:>4} session{'s' if count != 1 else ' '}  {format_minutes(minutes):>8}")
    recent = sorted(history.sessions(), key=lambda s: s[0])[-5:]
    if recent:
        print()
        print(style("RECENT", "bold"))
        for when, minutes, task in reversed(recent):
            print(f"  {when.strftime('%Y-%m-%d %H:%M')}  {minutes:>3} min  {task or style('(no task)', 'dim')}")
    print()
    print(style(f"history: {history.path}", "dim"))
    return 0


def migrate_json_settings(history: History) -> None:
    """Older versions kept settings in pomodoro.json. Move them to the kit settings file once."""
    old = history.data.pop("settings", None)
    if not isinstance(old, dict) or not old:
        return
    values = {key: old[key] for key, _ in SETTING_FIELDS if isinstance(old.get(key), int) and not isinstance(old.get(key), bool)}
    try:
        if values and not load_settings().get("pomodoro") and Settings(**values).validate() is None:
            for key, value in values.items():
                set_value("pomodoro", key, value)
    except SettingsError:
        history.data["settings"] = old  # keep them in the JSON so nothing is lost
        return
    history.save()


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit pomodoro", description="Pomodoro focus timer in the terminal.")
    parser.add_argument("--focus", type=int, help="focus length in minutes (default: the pomodoro.focus setting, 25)")
    parser.add_argument("--short", type=int, help="short break in minutes (default: the pomodoro.short setting, 5)")
    parser.add_argument("--long", type=int, help="long break in minutes (default: the pomodoro.long setting, 15)")
    parser.add_argument("--every", type=int, help="long break after this many focus sessions (default: the pomodoro.every setting, 4)")
    parser.add_argument("--task", default="", help="what you're working on")
    parser.add_argument("--stats", action="store_true", help="print your focus history and exit")
    parser.add_argument("--speed", type=float, default=1.0, help=argparse.SUPPRESS)  # testing: run the clock faster
    args = parser.parse_args()

    history = History(data_path())
    if args.stats:
        return print_stats(history)

    migrate_json_settings(history)
    conf = tool_settings()
    settings = Settings(**{key: conf[key] for key, _ in SETTING_FIELDS if key in conf})
    for key in ("focus", "short", "long", "every"):
        if getattr(args, key) is not None:
            setattr(settings, key, getattr(args, key))
    problem = settings.validate()
    if problem:
        die(problem)
    if args.speed <= 0:
        die("--speed must be positive")

    PomodoroApp(settings, history, task=args.task, speed=args.speed).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
