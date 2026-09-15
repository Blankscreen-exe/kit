"""A big, mouse-clickable countdown timer in the terminal, with an alert when time is up."""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

from kitlib import KIT_HOME, die, style

try:
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Center, Horizontal, Vertical
    from textual.widgets import Button, Digits, Footer, ProgressBar, Static
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

MAX_SECONDS = 99 * 3600
ALERT_EVERY = 5.0
ACCENT = "#d97757"
TEXT = "#e8e3dc"
MUTED = "#8a847c"
DIM = "#4a4642"


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

# Windows toast through PowerShell's built-in WinRT access: no modules, title and text passed via
# environment variables and inserted as XML text nodes, so the message can't break the script.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:KIT_TOAST_TITLE)) > $null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:KIT_TOAST_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
if ($env:KIT_TOAST_DRYRUN) { 'toast ready'; exit 0 }
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def windows_toast_command() -> list[str]:
    shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", TOAST_SCRIPT]


def desktop_alert(title: str, message: str, notification: bool) -> None:
    """System sound, plus a desktop notification when `notification` is true. Best effort, never raises."""
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if sys.platform.startswith("win"):
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            if notification:
                env = dict(os.environ, KIT_TOAST_TITLE=title, KIT_TOAST_BODY=message)
                subprocess.Popen(windows_toast_command(), env=env, creationflags=subprocess.CREATE_NO_WINDOW, **quiet)
        elif sys.platform == "darwin":
            if notification:
                script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]
                subprocess.Popen(["osascript", *script, title, message], **quiet)
        elif notification and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-u", "critical", "-a", "kit countdown", title, message], **quiet)
    except Exception:
        pass


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
                line = f"{style(format_clock(remaining), 'bold')}  {style(full * filled, 'yellow')}{style(empty * (width - filled), 'dim')}{label}"
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

    done = style("time's up", "bold", "yellow") + (f" - {message}" if message else "")
    if tty:
        sys.stdout.write(f"\r{done}\x1b[K\n\a")
    else:
        print(f"time's up{f' - {message}' if message else ''}\a")
    sys.stdout.flush()
    if notify:
        desktop_alert("Time's up", message or "Your countdown has finished.", notification=True)
    return 0


# --- TUI ----------------------------------------------------------------------------------------

CSS = """
Screen {
    background: #1a1918;
    color: #e8e3dc;
    align: center middle;
}

#topbar { width: 72; max-width: 100%; height: 1; margin-bottom: 1; padding: 0 1; }
#brand { width: 1fr; }
#ends { width: auto; }

#card {
    width: 72;
    max-width: 100%;
    height: auto;
    border: round #3d3935;
    border-title-color: #d97757;
    border-title-style: bold;
    border-subtitle-color: #6b655e;
    background: #1a1918;
    padding: 1 2 0 2;
}
#status { width: 100%; height: 1; content-align: center middle; color: #d97757; text-style: bold; }
#clock { width: auto; color: #f5f0e8; margin: 1 0 1 0; }
#message { width: 100%; height: auto; content-align: center middle; text-align: center; color: #e8e3dc; margin-bottom: 1; }
#progress { width: auto; height: 1; margin-bottom: 1; }
#progress Bar { width: 50; }
#progress Bar > .bar--bar { color: #d97757; background: #2e2b28; }
#progress Bar > .bar--complete { color: #d97757; }
#progress PercentageStatus { color: #8a847c; }

#controls { width: 100%; height: 3; align: center middle; margin-bottom: 1; }
Button {
    min-width: 7;
    width: auto;
    height: 3;
    margin: 0 1 0 0;
    padding: 0 1;
    border: round #3d3935;
    background: #1a1918;
    color: #e8e3dc;
    text-style: none;
}
Button:hover { background: #1a1918; border: round #8a847c; color: #ffffff; text-style: none; }
Button.-active { background: #262422; border: round #8a847c; }
Button:focus { text-style: none; }
#toggle { width: 16; }
#quit { margin: 0; }
Button.primary { border: round #d97757; color: #d97757; text-style: bold; }
Button.primary:hover { border: round #f0a07e; color: #f0a07e; text-style: bold; }

.-paused #clock { color: #8a847c; }
.-paused #status { color: #8a847c; }
.-paused #progress Bar > .bar--bar { color: #6b655e; }

.-done #card { border: round #d97757; }
.-done #clock { color: #d97757; }
.-done #progress Bar > .bar--bar, .-done #progress Bar > .bar--complete { color: #d97757; }
.-flash #card { border: heavy #f0a07e; background: #2b1f19; }
.-flash #clock { color: #ffb38f; }
.-flash #status { color: #ffb38f; }
.-flash #message, .-flash #progress, .-flash Button { background: #2b1f19; }

Footer { background: #1a1918; color: #8a847c; }
Footer FooterKey { background: #1a1918; }
Footer FooterKey .footer-key--key { background: #1a1918; color: #d97757; text-style: bold; }
Footer FooterKey .footer-key--description { color: #8a847c; }
Footer FooterKey:hover { background: #262422; }
"""


class CountdownApp(App):
    TITLE = "kit countdown"
    CSS = CSS
    AUTO_FOCUS = None
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle", "Pause/Resume"),
        Binding("minus", "less", "-1 min"),
        Binding("plus,equals_sign", "more", "+1 min"),
        Binding("r", "reset", "Reset"),
        Binding("enter,escape", "dismiss_alert", "Dismiss", show=False),
        Binding("q", "quit_timer", "Quit"),
    ]

    def __init__(self, seconds: float, message: str = "", once: bool = False, notify_desktop: bool = True,
                 speed: float = 1.0, alert_every: float = ALERT_EVERY) -> None:
        super().__init__()
        self.timer = Countdown(seconds)
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
            yield Static(Text.assemble(("✻ ", f"bold {ACCENT}"), ("kit countdown", f"bold {TEXT}")), id="brand")
            yield Static("", id="ends")
        with Vertical(id="card"):
            yield Static("", id="status")
            with Center():
                yield Digits(format_clock(self.timer.remaining), id="clock")
            yield Static(self.message, id="message")
            with Center():
                yield ProgressBar(total=100, show_eta=False, id="progress")
            with Horizontal(id="controls"):
                yield Button("‖ Pause", id="toggle", classes="primary")
                yield Button("−1m", id="less")
                yield Button("+1m", id="more")
                yield Button("↺ Reset", id="reset")
                yield Button("✕ Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False  # keys go to the app's shortcuts; clicks still work
        self.query_one("#message").display = bool(self.message)
        self.set_interval(0.1, self.tick)
        self.refresh_view()

    # --- timing ---

    def tick(self) -> None:
        now = time.monotonic()
        delta, self._last_tick = (now - self._last_tick) * self.speed, now
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

        clock = self.query_one("#clock", Digits)
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
        card.border_title = " Time's up " if timer.done else " Countdown "
        card.border_subtitle = f" {format_span(timer.total)} "

        toggle = self.query_one("#toggle", Button)
        if timer.done:
            label = "✕ Dismiss" if self.alerting else "↺ Again"
        else:
            label = "‖ Pause" if timer.running else "▶ Resume"
        if str(toggle.label) != label:
            toggle.label = label

    # --- actions ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "toggle": self.action_toggle, "less": self.action_less, "more": self.action_more,
            "reset": self.action_reset, "quit": self.action_quit_timer,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

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

    def action_quit_timer(self) -> None:
        self.exit(return_code=0 if self.timer.done else 130)


# --- entry point -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="kit countdown", description="A big countdown timer in the terminal, with an alert when time is up.")
    parser.add_argument("duration", nargs="?", help="how long: 90s, 15m, 1h30m, 25:00, 1:30:00 or 25 (minutes)")
    parser.add_argument("message", nargs="*", help="what the countdown is for, shown on screen and in the alert")
    parser.add_argument("--until", metavar="TIME", help="count down to a clock time instead, e.g. 14:30 or 2:30pm")
    parser.add_argument("--once", action="store_true", help="alert once instead of repeating until dismissed")
    parser.add_argument("--no-notify", action="store_true", help="no desktop notification or system sound (the terminal still beeps)")
    parser.add_argument("--plain", action="store_true", help="a single updating line instead of the full-screen timer")
    parser.add_argument("--speed", type=float, default=1.0, help=argparse.SUPPRESS)  # testing: run the clock faster
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
        else:
            parser.error("give a duration (e.g. 15m) or --until TIME")
    except ParseError as exc:
        die(str(exc))
    if args.speed <= 0:
        die("--speed must be positive")
    message = " ".join(words).strip()

    if args.plain:
        return run_plain(seconds, message, notify=not args.no_notify, speed=args.speed)
    app = CountdownApp(seconds, message, once=args.once, notify_desktop=not args.no_notify, speed=args.speed)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
