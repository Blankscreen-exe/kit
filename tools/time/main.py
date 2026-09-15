"""Time arithmetic: span between clock times, duration differences and totals."""

from __future__ import annotations

import argparse
import re

from kitlib import die, style

MINUTES_PER_DAY = 24 * 60

CLOCK_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m\.?)?", re.IGNORECASE)
MERIDIEM_RE = re.compile(r"[ap]\.?m\.?", re.IGNORECASE)
HHMM_RE = re.compile(r"(\d+):(\d{2})")
UNITS_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+)m)?")


def parse_clock(text: str) -> int:
    """'9:15am', '9 pm', '17:30' -> minutes since midnight."""
    match = CLOCK_RE.fullmatch(text.strip())
    if not match:
        die(f"not a clock time: '{text}' (try 9:15am, 9pm or 17:30)")
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), (match[3] or "").lower()
    if minute > 59:
        die(f"'{text}': minutes must be 00-59")
    if meridiem:
        if not 1 <= hour <= 12:
            die(f"'{text}': 12-hour times need an hour from 1 to 12")
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif match[2] is None:
        die(f"'{text}' is ambiguous - write {hour}:00 or add am/pm")
    elif hour > 23:
        die(f"'{text}': hour must be 0-23")
    return hour * 60 + minute


def parse_duration(text: str) -> int:
    """'1:30', '1h30m', '90m', '1.5h' -> minutes."""
    compact = text.strip().lower().replace(" ", "")
    if match := HHMM_RE.fullmatch(compact):
        if int(match[2]) > 59:
            die(f"'{text}': minutes must be 00-59")
        return int(match[1]) * 60 + int(match[2])
    match = UNITS_RE.fullmatch(compact)
    if compact and match and (match[1] or match[2]):
        return round(float(match[1] or 0) * 60) + int(match[2] or 0)
    die(f"not a duration: '{text}' (try 1:30, 1h30m, 90m or 1.5h)")


def join_meridiems(tokens: list[str]) -> list[str]:
    """Lets clock times be typed unquoted: ['9:15', 'AM'] -> ['9:15AM']."""
    merged: list[str] = []
    for token in tokens:
        if merged and MERIDIEM_RE.fullmatch(token):
            merged[-1] += token
        else:
            merged.append(token)
    return merged


def show(minutes: int, short: bool) -> None:
    hours, mins = divmod(minutes, 60)
    if short:
        print(f"{hours}:{mins:02d}")
    else:
        print(f"{style(f'{hours}h {mins:02d}m', 'bold', 'green')}  {style(f'({hours}:{mins:02d} = {minutes / 60:.2f}h)', 'dim')}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit time", description="Time arithmetic for tracking hours.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    between = commands.add_parser("between", help="time from one clock time to another (wraps past midnight)")
    between.add_argument("times", nargs="+", metavar="TIME", help="start and end, e.g. 9:15am 5:30pm")
    diff = commands.add_parser("diff", help="difference between two durations")
    diff.add_argument("first", metavar="DURATION")
    diff.add_argument("second", metavar="DURATION")
    total = commands.add_parser("sum", help="add durations together")
    total.add_argument("durations", nargs="+", metavar="DURATION")
    for sub in (between, diff, total):
        sub.add_argument("-s", "--short", action="store_true", help="print only H:MM (for scripts)")

    args = parser.parse_args()

    if args.command == "between":
        times = join_meridiems(args.times)
        if len(times) != 2:
            between.error(f"expected a start and an end time, got {len(times)}: {' '.join(times)}")
        start, end = (parse_clock(t) for t in times)
        show((end - start) % MINUTES_PER_DAY, args.short)
    elif args.command == "diff":
        show(abs(parse_duration(args.first) - parse_duration(args.second)), args.short)
    else:
        show(sum(parse_duration(d) for d in args.durations), args.short)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
