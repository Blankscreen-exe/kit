"""kit top: a process list built to spot suspicious programs. Documented in README.md (see: kit help top)."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kitlib import die, style  # noqa: E402
from kitlib.settings import tool_settings  # noqa: E402

try:
    import psutil  # noqa: F401
except ImportError as exc:
    from kitlib import KIT_HOME
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

from top_scan import (AutoRow, Engine, ProcRow, Snapshot, TrustStore, data_path,  # noqa: E402
                      elevation_hint)

LEVEL_STYLES = {"high": ("bold", "red"), "medium": ("bold", "yellow"), "low": ("cyan",), "ok": ("dim",)}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2, "ok": 3}


# --- report ------------------------------------------------------------------------------

def badge(level: str) -> str:
    return style(level.upper().ljust(6), *LEVEL_STYLES[level])


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width // 2 - 1] + "…" + text[-(width - width // 2):]


def print_processes(snapshot: Snapshot, rows: list[ProcRow]) -> None:
    counts = snapshot.counts()
    flagged = counts["high"] + counts["medium"] + counts["low"]
    print(f"{style('PROCESSES', 'bold')}  {len(snapshot.rows)} running · {flagged} flagged")
    if snapshot.denied:
        print(style(f"  {snapshot.denied} couldn't be inspected - {elevation_hint()}", "yellow"))
    if not rows:
        print(style("  nothing suspicious found", "green"))
    for row in rows:
        where = row.exe or style("(path unknown)", "dim")
        trusted = style("  trusted", "green") if row.trusted else ""
        print(f"  {badge(row.level)} {row.pid:>7}  {style(row.name, 'bold')}  {shorten(str(where), 90)}{trusted}")
        for flag in row.flags:
            line = f"                  · {flag.reason}"
            print(style(line, "dim") if row.level == "ok" else line)
        if row.cmdline and row.level in ("high", "medium"):
            print(style(f"                  $ {shorten(' '.join(row.cmdline), 110)}", "dim"))
    print()


def print_autostart(rows: list[AutoRow], total: int) -> None:
    flagged = sum(1 for r in rows if r.level != "ok")
    print(f"{style('AUTOSTART', 'bold')}  {total} entries · {flagged} flagged")
    if not rows:
        print(style("  nothing suspicious found", "green"))
    for row in rows:
        e = row.entry
        state = style("  (disabled)", "dim") if e.disabled else ""
        trusted = style("  trusted", "green") if row.trusted else ""
        print(f"  {badge(row.level)} {e.kind}  {style(e.name, 'bold')}{state}{trusted}")
        print(style(f"         {shorten(e.command, 110)}", "dim"))
        print(style(f"         in {e.location}", "dim"))
        for flag in row.flags:
            print(f"         · {flag.reason}")
    print()


def sort_procs(rows: list[ProcRow]) -> list[ProcRow]:
    return sorted(rows, key=lambda r: (LEVEL_ORDER[r.level], -r.score, -r.cpu, r.name.lower()))


def run_scan(engine: Engine, args: argparse.Namespace) -> tuple[Snapshot, list[ProcRow], list[AutoRow] | None, list[AutoRow]]:
    autostart_result: list[list[AutoRow]] = []
    worker = None
    if not args.no_autostart:  # the slowest part (scheduled tasks), so it runs alongside the process pass
        worker = threading.Thread(target=lambda: autostart_result.append(engine.autostart_parallel()), daemon=True)
        worker.start()
    engine.prime()
    first = engine.snapshot(signatures=False)
    engine.presign({r.exe for r in first.rows if r.exe})
    time.sleep(max(0.0, 1.0 - (time.time() - first.taken)))
    snapshot = engine.snapshot()
    procs = sort_procs([r for r in snapshot.rows if args.all or r.level != "ok"])
    autostart = None
    if worker is not None:
        worker.join()
        autostart = autostart_result[0] if autostart_result else []
    auto_shown = [] if autostart is None else sorted(
        (r for r in autostart if args.all or r.level != "ok"),
        key=lambda r: (LEVEL_ORDER[r.level], -r.score, r.entry.kind, r.entry.name.lower()))
    return snapshot, procs, autostart, auto_shown


def scan(args: argparse.Namespace) -> int:
    engine = Engine(TrustStore(data_path()), wait_for_signatures=True)
    snapshot, procs, autostart, auto_shown = run_scan(engine, args)
    highs = sum(1 for r in snapshot.rows if r.level == "high") + sum(1 for r in autostart or [] if r.level == "high")

    if args.json:
        body = {
            "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "platform": sys.platform, "elevated": snapshot.elevated, "denied": snapshot.denied,
            "counts": snapshot.counts(),
            "processes": [r.to_dict() for r in procs],
            "autostart": None if autostart is None else [r.to_dict() for r in auto_shown],
        }
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 1 if highs else 0

    print_processes(snapshot, procs)
    if autostart is not None:
        print_autostart(auto_shown, len(autostart))
    counts = snapshot.counts()
    for row in autostart or []:
        counts[row.level] += 1
    summary = ", ".join(style(f"{counts[k]} {k}", *LEVEL_STYLES[k]) for k in ("high", "medium", "low"))
    print(f"{style('summary:', 'bold')} {summary}")
    if counts["high"] or counts["medium"]:
        print(style("next: open 'kit top' to look closer (details, VirusTotal lookup by hash, kill), and see\n"
                    "      'kit help top' for what to do if something really looks wrong.", "dim"))
    print(style("kit top is a triage aid, not an antivirus: flagged doesn't mean infected, and clean doesn't mean safe.", "dim"))
    return 1 if highs else 0


def watch(args: argparse.Namespace) -> int:
    engine = Engine(TrustStore(data_path()), wait_for_signatures=True)
    engine.prime()
    seen_procs: set[tuple] = set()
    seen_auto: set[tuple] = set()
    first = True
    next_autostart = 0.0  # the autostart check is slow, so it runs at most once a minute
    print(style(f"watching every {args.watch:g}s for new findings - Ctrl+C to stop", "dim"))
    try:
        while True:
            snapshot = engine.snapshot()
            now = datetime.now().strftime("%H:%M:%S")
            for row in sort_procs(snapshot.rows):
                key = (row.pid, row.started, row.level)
                if row.level in ("ok",) or key in seen_procs:
                    continue
                seen_procs.add(key)
                print(f"{style(now, 'dim')} {badge(row.level)} {row.pid:>7} {style(row.name, 'bold')}  {row.exe or ''}")
                print(style(f"         {row.why}", "dim"))
            if not args.no_autostart and time.time() >= next_autostart:
                next_autostart = time.time() + max(60.0, args.watch)
                for row in engine.autostart_parallel():
                    key = (row.entry.kind, row.entry.name, row.entry.command, row.level)
                    if row.level == "ok" or key in seen_auto:
                        continue
                    seen_auto.add(key)
                    label = "autostart" if first else "NEW autostart"
                    print(f"{style(now, 'dim')} {badge(row.level)} {label}: {row.entry.kind} {style(row.entry.name, 'bold')}")
                    print(style(f"         {row.entry.command}  ({row.why})", "dim"))
            first = False
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


# --- entry point ---------------------------------------------------------------------------

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    settings = tool_settings()
    parser = argparse.ArgumentParser(
        prog="kit top",
        description="A process list built to spot suspicious programs: odd locations, fake system names, "
                    "missing signatures, shady command lines, backdoor ports and autostart entries.")
    parser.add_argument("--scan", action="store_true", help="print a report and exit instead of opening the live view")
    parser.add_argument("--json", action="store_true", help="with --scan: print JSON")
    parser.add_argument("--all", action="store_true", help="with --scan: include processes and entries that look fine")
    parser.add_argument("--no-autostart", action="store_true", help="skip the autostart check")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="keep scanning and print new findings as they appear")
    parser.add_argument("--refresh", type=float, default=settings["refresh"],
                        help=f"live view refresh interval in seconds (default: the top.refresh setting, {settings['refresh']:g})")
    parser.add_argument("--flagged", action="store_true", default=settings["only_flagged"],
                        help="live view: start with only flagged processes shown (default: the top.only_flagged setting)")
    parser.add_argument("--untrust", metavar="PATH", help="remove a program from your trusted list and exit")
    parser.add_argument("--trusted", action="store_true", help="list the programs you marked as trusted and exit")
    args = parser.parse_args()

    if args.refresh < 0.5:
        die("--refresh must be at least 0.5 seconds")
    if args.json and not args.scan:
        die("--json only works with --scan")

    if args.trusted or args.untrust:
        store = TrustStore(data_path())
        if args.untrust:
            if not store.remove(args.untrust):
                die(f"not in the trusted list: {args.untrust}")
            print(f"{style('removed', 'bold', 'green')} {args.untrust}")
            return 0
        if not store.entries:
            print("no trusted programs yet (press t on a process in 'kit top' to add one)")
        for entry in store.entries.values():
            print(f"{entry['path']}\n  {style('sha256 ' + entry['sha256'] + '  added ' + entry.get('added', '?'), 'dim')}")
        print(style(f"list: {store.path}", "dim"))
        return 0

    if args.watch is not None:
        if args.watch < 1:
            die("--watch must be at least 1 second")
        return watch(args)
    if args.scan:
        return scan(args)

    if not sys.stdout.isatty():
        die("the live view needs a terminal - use 'kit top --scan' for a report")
    from top_ui import TopApp
    TopApp(refresh=args.refresh, only_flagged=args.flagged, show_autostart=not args.no_autostart).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
