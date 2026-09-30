"""A big, mouse-clickable countdown timer in the terminal, with an alert when time is up."""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from datetime import datetime, timedelta

from kitlib import KIT_HOME, die, style
from kitlib.popup import beep, popup
from kitlib.settings import tool_settings
from kitlib.theme import ACCENT, BAD, DIM, EDGE, MUTED, TEXT

try:
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Center, Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Footer, Input, Label, ProgressBar, Static

    from kitlib.tui import BigDigits, KitApp, brand, title
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

MAX_SECONDS = 99 * 3600
ALERT_EVERY = 5.0


# --- parsing -------------------------------------------------------------------------

class ParseError(ValueError):
    pass


UNITS_RE = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*m(?:in(?:utes?|s)?)?)?\s*"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s(?:ec(?:onds?|s)?)?)?"
)
CLOCK_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")
UNTIL_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m?\.?)?", re.IGNORECASE)


def parse_duration(text: str) -> float:
    """'90s', '15m', '1h30m', '25:00', '1:30:00' or '25' (minutes) -> seconds."""
    value = text.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        seconds = float(value) * 60
    elif match := CLOCK_RE.fullmatch(value):
        hours, minutes, secs = match.groups()
        if int(secs) > 59 or (hours is not None and int(minutes) > 59):
            raise ParseError(f"'{text}': minutes and seconds must be 00-59")
        seconds = int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
    elif value and (match := UNITS_RE.fullmatch(value)) and any(match.groupdict().values()):
        seconds = float(match["h"] or 0) * 3600 + float(match["m"] or 0) * 60 + float(match["s"] or 0)
    else:
        raise ParseError(f"not a duration: '{text}' (try 90s, 15m, 1h30m, 25:00, 1:30:00 or 25)")
    if seconds <= 0:
        raise ParseError("the duration must be more than zero")
    if seconds > MAX_SECONDS:
        raise ParseError("the duration must be under 99 hours")
    return seconds


def parse_until(text: str, now: datetime) -> datetime:
    """'14:30', '2:30pm', '9am' -> the next moment the clock shows that time."""
    match = UNTIL_RE.fullmatch(text.strip())
    if not match:
        raise ParseError(f"not a clock time: '{text}' (try 14:30, 2:30pm or 9am)")
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), (match[3] or "").lower()
    if minute > 59:
        raise ParseError(f"'{text}': minutes must be 00-59")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ParseError(f"'{text}': 12-hour times need an hour from 1 to 12")
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif match[2] is None:
        raise ParseError(f"'{text}' is ambiguous - write {hour}:00 or add am/pm")
    elif hour > 23:
        raise ParseError(f"'{text}': the hour must be 0-23")
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def format_clock(seconds: float) -> str:
    total = max(0, math.ceil(seconds - 1e-9))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def format_span(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours}h"] if hours else []
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


# --- timer (no UI, so it can be tested directly) ----------------------------------------

class Countdown:
    def __init__(self, seconds: float) -> None:
        self.initial = seconds
        self.reset()

    def reset(self) -> None:
        self.total = self.remaining = self.initial
        self.running = True

    @property
    def elapsed(self) -> float:
        return self.total - self.remaining

    @property
    def done(self) -> bool:
        return self.remaining <= 0

    def advance(self, seconds: float) -> bool:
        """Count down while running. True when it has just reached zero."""
        if not self.running or self.done:
            return False
        self.remaining = max(0.0, self.remaining - seconds)
        return self.done

    def adjust(self, seconds: float) -> bool:
        """Add (or take away) time. True if that took it to zero."""
        if self.done:
            return False
        elapsed = self.elapsed
        self.remaining = max(0.0, self.remaining + seconds)
        self.total = max(1.0, elapsed + self.remaining)
        return self.done

    def snooze(self, seconds: float) -> None:
        self.total = self.remaining = seconds
        self.running = True


# --- alerts --------------------------------------------------------------------------------

def desktop_alert(title: str, message: str, notification: bool) -> None:
    """The system sound, plus a desktop popup when `notification` is true. Never raises."""
    if notification:
        popup(title, message, sound=True, urgent=True, app="kit countdown")
    else:
        beep()


# --- plain mode ------------------------------------------------------------------------------

def run_plain(seconds: float, message: str, notify: bool, speed: float) -> int:
    tty = sys.stdout.isatty()
    fancy = True
    try:
        "█░".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        fancy = False
    full, empty = ("█", "░") if fancy else ("#", "-")
    label = f"  {message}" if message else ""
    ends = datetime.now() + timedelta(seconds=seconds / speed)
    if not tty:
        print(f"countdown {format_clock(seconds)} - ends at {ends:%H:%M:%S}{label}", flush=True)

    started = time.monotonic()
    last = ""
    try:
        while True:
            remaining = max(0.0, seconds - (time.monotonic() - started) * speed)
            if tty:
                width = 24
                filled = round((1 - remaining / seconds) * width)
                line = f"{style(format_clock(remaining), 'bold')}  {style(full * filled, 'accent')}{style(empty * (width - filled), 'dim')}{label}"
                if line != last:
                    sys.stdout.write(f"\r{line}\x1b[K")
                    sys.stdout.flush()
                    last = line
            if remaining <= 0:
                break
            time.sleep(max(0.01, min(0.2, remaining / speed)))
    except KeyboardInterrupt:
        if tty:
            sys.stdout.write("\n")
        print("cancelled", flush=True)
        return 130

    done = style("time's up", "bold", "accent") + (f" - {message}" if message else "")
    if tty:
        sys.stdout.write(f"\r{done}\x1b[K\n\a")
    else:
        print(f"time's up{f' - {message}' if message else ''}\a")
    sys.stdout.flush()
    if notify:
        desktop_alert("Time's up", message or "Your countdown has finished.", notification=True)
    return 0


# --- TUI ----------------------------------------------------------------------------------------

CSS = f"""
Screen {{ align: center middle; }}

#topbar {{ width: 72; max-width: 100%; height: 1; margin-bottom: 1; }}
#brand {{ width: 1fr; }}
#ends {{ width: auto; }}

#card {{ width: 72; max-width: 100%; height: auto; padding: 1 2 0 2; }}
#status {{ width: 100%; height: 1; content-align: center middle; color: {ACCENT}; text-style: bold; }}
#clock {{ color: {TEXT}; margin: 1 0 1 0; }}
#message {{ width: 100%; height: auto; content-align: center middle; text-align: center; margin-bottom: 1; }}
#progress {{ width: auto; height: 1; margin-bottom: 1; }}
#progress Bar {{ width: 50; }}

#controls {{ width: 100%; height: 3; align: center middle; margin-bottom: 1; }}
#controls Button {{ margin: 0 1 0 0; }}
#toggle {{ width: 14; }}
#quit {{ margin: 0; }}

.-paused #clock, .-paused #status {{ color: {MUTED}; }}
.-paused #progress Bar > .bar--bar {{ color: {DIM}; }}
.-done #card {{ border: heavy {ACCENT}; }}
.-done #clock {{ color: {ACCENT}; }}
.-flash #clock {{ color: {EDGE}; }}
.-flash #controls Button {{ border: heavy {ACCENT}; }}

#dialog {{ width: 64; max-width: 100%; height: auto; max-height: 100%; overflow-y: auto; }}
#presets {{ width: 100%; height: 3; margin-bottom: 1; align: center middle; }}
#presets Button {{ min-width: 0; margin: 0 1 0 0; }}
.field {{ width: 100%; height: 3; }}
.field Label {{ width: 12; height: 3; content-align: left middle; text-style: bold; }}
.field Input {{ width: 1fr; }}
#setup-hint {{ color: {MUTED}; height: auto; }}
#setup-error {{ color: {BAD}; height: auto; }}
#dialog-buttons {{ width: 100%; height: 3; align: right middle; margin-top: 1; }}
#dialog-buttons Button {{ margin: 0 0 0 1; }}
"""

PRESETS = ["1m", "5m", "10m", "15m", "25m", "45m", "1h"]


class SetupScreen(ModalScreen):
    """Pick a duration (or a clock time) and a message. Dismisses with (seconds, message) or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str = "") -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="dialog") as dialog:
            dialog.border_title = title("New countdown")
            with Horizontal(id="presets"):
                for preset in PRESETS:
                    yield Button(preset, id=f"preset-{preset}")
            with Horizontal(classes="field"):
                yield Label("How long")
                yield Input(placeholder="90s, 15m, 1h30m, 25:00 or 25", id="duration")
            with Horizontal(classes="field"):
                yield Label("Or until")
                yield Input(placeholder="a clock time: 14:30, 2:30pm, 9am", id="until")
            with Horizontal(classes="field"):
                yield Label("Message")
                yield Input(self.message, placeholder="what it's for (optional)", id="setup-message")
            yield Static("Click a preset to start straight away, or fill in a time and press enter.", id="setup-hint")
            yield Static("", id="setup-error")
            with Horizontal(id="dialog-buttons"):
                yield Button("CANCEL", id="cancel")
                yield Button("▶ START", id="start", classes="primary")

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False
        self.query_one("#duration", Input).focus()

    def message_text(self) -> str:
        return self.query_one("#setup-message", Input).value.strip()

    @on(Button.Pressed, "#presets Button")
    def preset_clicked(self, event: Button.Pressed) -> None:
        self.dismiss((parse_duration(str(event.button.label)), self.message_text()))

    @on(Button.Pressed, "#start")
    def start_clicked(self) -> None:
        self.start()

    @on(Input.Submitted)
    def field_submitted(self) -> None:
        self.start()

    def start(self) -> None:
        duration = self.query_one("#duration", Input).value.strip()
        until = self.query_one("#until", Input).value.strip()
        error = self.query_one("#setup-error", Static)
        if duration and until:
            error.update("Fill in either 'How long' or 'Or until', not both.")
            return
        if not duration and not until:
            error.update("Enter how long (e.g. 15m) or a clock time to count down to.")
            self.query_one("#duration", Input).focus()
            return
        try:
            if duration:
                seconds = parse_duration(duration)
            else:
                now = datetime.now()
                seconds = (parse_until(until, now) - now).total_seconds()
        except ParseError as exc:
            error.update(str(exc)[0].upper() + str(exc)[1:] + ".")
            return
        self.dismiss((seconds, self.message_text()))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class CountdownApp(KitApp):
    TITLE = "kit countdown"
    CSS = CSS
    AUTO_FOCUS = None
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle", "Pause/Resume"),
        Binding("minus", "less", "-1 min"),
        Binding("plus,equals_sign", "more", "+1 min"),
        Binding("r", "reset", "Reset"),
        Binding("n", "new_timer", "New"),
        Binding("enter,escape", "dismiss_alert", "Dismiss", show=False),
        Binding("q", "quit_timer", "Quit"),
    ]

    def __init__(self, seconds: float | None, message: str = "", once: bool = False, notify_desktop: bool = True,
                 speed: float = 1.0, alert_every: float = ALERT_EVERY) -> None:
        super().__init__()
        # None: nothing to count yet, so the setup dialog opens first
        self.timer = Countdown(seconds) if seconds is not None else None
        self.message = message
        self.once = once
        self.notify_desktop = notify_desktop
        self.speed = speed
        self.alert_every = alert_every
        self.alerting = False
        self.alert_count = 0
        self.finished_at: datetime | None = None
        self._since_alert = 0.0
        self._last_tick = time.monotonic()

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(brand("kit countdown"), id="brand")
            yield Static("", id="ends")
        with Vertical(id="card", classes="slab"):
            yield Static("", id="status")
            with Center():
                yield BigDigits(format_clock(self.timer.remaining) if self.timer else "--:--", id="clock", short_below=27)
            yield Static(self.message, id="message")
            with Center():
                yield ProgressBar(total=100, show_eta=False, id="progress")
            with Horizontal(id="controls"):
                yield Button("‖ PAUSE", id="toggle", classes="primary")
                yield Button("−1M", id="less")
                yield Button("+1M", id="more")
                yield Button("↺ RESET", id="reset")
                yield Button("+ NEW", id="new")
                yield Button("✕ QUIT", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False  # keys go to the app's shortcuts; clicks still work
        self.query_one("#message").display = bool(self.message)
        self.set_interval(0.1, self.tick)
        self.refresh_view()
        if self.timer is None:
            self.action_new_timer()

    # --- timing ---

    def tick(self) -> None:
        now = time.monotonic()
        delta, self._last_tick = (now - self._last_tick) * self.speed, now
        if self.timer is None:
            return
        if self.timer.advance(delta):
            self.finish()
        elif self.alerting and not self.once:
            self._since_alert += delta
            if self._since_alert >= self.alert_every:
                self.alert(first=False)
        self.refresh_view()

    def finish(self) -> None:
        self.finished_at = datetime.now()
        self.alerting = True
        self.alert(first=True)
        if self.once:
            self.alerting = False

    def alert(self, first: bool) -> None:
        self._since_alert = 0.0
        self.alert_count += 1
        self.bell()
        if first:
            self.notify(self.message or "Your countdown has finished.", title="Time's up", timeout=10)
        if self.notify_desktop:
            desktop_alert("Time's up", self.message or "Your countdown has finished.", notification=first)

    # --- view ---

    def refresh_view(self) -> None:
        timer = self.timer
        if timer is None:
            return
        screen = self.screen_stack[0]
        flashing = self.alerting and int(time.monotonic() * 2) % 2 == 0
        screen.set_class(not timer.running and not timer.done, "-paused")
        screen.set_class(timer.done, "-done")
        screen.set_class(flashing, "-flash")

        status = Text()
        if timer.done:
            status.append("✓ TIME'S UP")
            hint = "press space to dismiss" if self.alerting else "r to reset · + for one more minute"
            status.append("  ·  ", style=DIM)
            status.append(hint, style=f"not bold {MUTED}")
        elif timer.running:
            status.append("● RUNNING")
        else:
            status.append("‖ PAUSED")
        self.query_one("#status", Static).update(status)

        clock = self.query_one("#clock", BigDigits)
        text = format_clock(timer.remaining)
        if clock.value != text:
            clock.update(text)
        self.query_one("#progress", ProgressBar).update(total=timer.total, progress=timer.elapsed)

        ends = Text()
        if timer.done and self.finished_at:
            ends.append("ended at ", style=MUTED)
            ends.append(f"{self.finished_at:%H:%M:%S}", style=f"bold {TEXT}")
        elif timer.running:
            end = datetime.now() + timedelta(seconds=timer.remaining / self.speed)
            ends.append("ends at ", style=MUTED)
            ends.append(f"{end:%H:%M}" if timer.remaining >= 3600 else f"{end:%H:%M:%S}", style=f"bold {TEXT}")
        else:
            ends.append("paused", style=MUTED)
        self.query_one("#ends", Static).update(ends)

        card = self.query_one("#card")
        card.border_title = title("Time's up" if timer.done else "Countdown")
        card.border_subtitle = f" {format_span(timer.total)} "

        toggle = self.query_one("#toggle", Button)
        if timer.done:
            label = "✕ DISMISS" if self.alerting else "↺ AGAIN"
        else:
            label = "‖ PAUSE" if timer.running else "▶ RESUME"
        if str(toggle.label) != label:
            toggle.label = label

    # --- actions ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "toggle": self.action_toggle, "less": self.action_less, "more": self.action_more,
            "reset": self.action_reset, "new": self.action_new_timer, "quit": self.action_quit_timer,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Until a countdown exists, only "new" and "quit" make sense.
        if self.timer is None and action in ("toggle", "less", "more", "reset", "dismiss_alert"):
            return False
        return True

    def action_toggle(self) -> None:
        if self.timer.done:
            if self.alerting:
                self.action_dismiss_alert()
            else:
                self.action_reset()
            return
        self.timer.running = not self.timer.running
        self._last_tick = time.monotonic()
        self.refresh_view()

    def action_dismiss_alert(self) -> None:
        if self.alerting:
            self.alerting = False
            self.refresh_view()

    def action_less(self) -> None:
        if self.timer.adjust(-60):
            self.finish()
        self.refresh_view()

    def action_more(self) -> None:
        if self.timer.done:
            self.timer.snooze(60)  # one more minute
            self.alerting = False
            self.finished_at = None
        else:
            self.timer.adjust(60)
        self._last_tick = time.monotonic()
        self.refresh_view()

    def action_reset(self) -> None:
        self.timer.reset()
        self.alerting = False
        self.finished_at = None
        self._last_tick = time.monotonic()
        self.refresh_view()

    def action_new_timer(self) -> None:
        if isinstance(self.screen, SetupScreen):
            return
        self.push_screen(SetupScreen(self.message), self.start_timer)

    def start_timer(self, choice: tuple[float, str] | None) -> None:
        if choice is None:
            if self.timer is None:
                self.exit(return_code=130)  # cancelled before anything started
            return
        seconds, self.message = choice
        self.timer = Countdown(seconds)
        self.alerting = False
        self.finished_at = None
        self._last_tick = time.monotonic()
        message = self.query_one("#message", Static)
        message.update(self.message)
        message.display = bool(self.message)
        self.refresh_view()

    def action_quit_timer(self) -> None:
        self.exit(return_code=0 if self.timer and self.timer.done else 130)


# --- entry point -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="kit countdown", description="A big countdown timer in the terminal, with an alert when time is up.")
    parser.add_argument("duration", nargs="?", help="how long: 90s, 15m, 1h30m, 25:00, 1:30:00 or 25 (minutes)")
    parser.add_argument("message", nargs="*", help="what the countdown is for, shown on screen and in the alert")
    parser.add_argument("--until", metavar="TIME", help="count down to a clock time instead, e.g. 14:30 or 2:30pm")
    parser.add_argument("--once", action="store_true", help="alert once instead of repeating until dismissed (setting: countdown.once)")
    parser.add_argument("--repeat", dest="once", action="store_false", help="repeat the alert until dismissed")
    parser.add_argument("--no-notify", dest="notify", action="store_false",
                        help="no desktop notification or system sound, the terminal still beeps (setting: countdown.notify)")
    parser.add_argument("--notify", dest="notify", action="store_true", help="desktop notification and system sound when time is up")
    parser.add_argument("--plain", action="store_true", help="a single updating line instead of the full-screen timer")
    parser.add_argument("--speed", type=float, default=1.0, help=argparse.SUPPRESS)  # testing: run the clock faster
    conf = tool_settings()
    parser.set_defaults(once=conf.get("once", False), notify=conf.get("notify", True))
    args = parser.parse_args()

    words = list(args.message)
    try:
        if args.until:
            if args.duration:
                words.insert(0, args.duration)
            now = datetime.now()
            seconds = (parse_until(args.until, now) - now).total_seconds()
        elif args.duration:
            seconds = parse_duration(args.duration)
        elif args.plain:
            parser.error("--plain needs a duration (e.g. 15m) or --until TIME")
        else:
            seconds = None  # the full-screen timer asks for one
    except ParseError as exc:
        die(str(exc))
    if args.speed <= 0:
        die("--speed must be positive")
    message = " ".join(words).strip()

    if args.plain:
        return run_plain(seconds, message, notify=args.notify, speed=args.speed)
    app = CountdownApp(seconds, message, once=args.once, notify_desktop=args.notify, speed=args.speed)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
