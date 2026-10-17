"""Subscription tracker: what you pay for, what it all costs a month, and what renews next."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from kitlib import die, style
from kitlib.settings import tool_settings

from subs_data import (CYCLE_HELP, DATE_HELP, Cycle, Store, StoreError, Subscription, check, data_path, days_text,
                       due_items, money, parse_currency, parse_date, parse_price, step)

SORTS = ("next", "cost", "name")


class Context:
    def __init__(self) -> None:
        settings = tool_settings()
        try:
            self.base = parse_currency(settings.get("currency") or "USD")
        except ValueError as exc:
            die(f"subs.currency: {exc}")
        self.due_days = int(settings.get("due_days") or 7)
        self.today = date.today()
        try:
            self.store = Store(data_path())
        except StoreError as exc:
            die(str(exc))

    def save(self) -> None:
        try:
            self.store.save()
        except OSError as exc:
            die(f"couldn't save {self.store.path}: {exc}")

    def find(self, query: str) -> Subscription:
        try:
            return self.store.find(query)
        except ValueError as exc:
            die(str(exc))

    def monthly_in_base(self, sub: Subscription) -> float | None:
        return self.store.convert(sub.monthly, sub.currency, self.base)


def sort_subs(ctx: Context, subs: list[Subscription], order: str) -> list[Subscription]:
    if order == "name":
        return sorted(subs, key=lambda s: s.name.casefold())
    if order == "cost":
        return sorted(subs, key=lambda s: -(ctx.monthly_in_base(s) or s.monthly))
    return sorted(subs, key=lambda s: (not s.active, s.next_renewal(ctx.today), s.name.casefold()))


# --- printing ---------------------------------------------------------------------------------

def print_table(headers: list[str], rows: list[list[tuple[str, tuple[str, ...]]]], right: set[int]) -> None:
    """Rows of (text, styles) cells, padded on the plain text so colours don't throw the columns off."""
    widths = [max([len(h)] + [len(row[i][0]) for row in rows]) for i, h in enumerate(headers)]

    def pad(text: str, i: int) -> str:
        return text.rjust(widths[i]) if i in right else text.ljust(widths[i])

    print("  " + "  ".join(style(pad(h, i), "bold", "muted") for i, h in enumerate(headers)).rstrip())
    for row in rows:
        print("  " + "  ".join(style(pad(text, i), *styles) for i, (text, styles) in enumerate(row)).rstrip())


def unconverted_text(unconverted: dict[str, float], suffix: str) -> str:
    return " + ".join(money(amount, cur) + suffix for cur, amount in sorted(unconverted.items()))


def print_totals_line(ctx: Context, subs: list[Subscription]) -> None:
    active = [s for s in subs if s.active]
    monthly, unconverted = ctx.store.totals(active, ctx.base)
    line = f"{len(active)} active  ·  {money(monthly, ctx.base)}/mo  ·  {money(monthly * 12, ctx.base)}/yr"
    print(style(line, "bold"))
    if unconverted:
        print(style(f"  + {unconverted_text(unconverted, '/mo')} with no exchange rate - set one with: "
                    f"kit subs rate <currency> <value>", "accent"))


def cmd_list(ctx: Context, args: argparse.Namespace) -> int:
    subs = ctx.store.subs if args.all else [s for s in ctx.store.subs if s.status != "cancelled"]
    if args.category:
        subs = [s for s in subs if s.category.casefold() == args.category.casefold()]
    subs = sort_subs(ctx, subs, args.sort)
    if args.json:
        out = []
        for sub in subs:
            item = sub.to_json()
            item["next_renewal"] = sub.next_renewal(ctx.today).isoformat()
            item["monthly"] = round(sub.monthly, 2)
            converted = ctx.monthly_in_base(sub)
            item[f"monthly_{ctx.base.lower()}"] = None if converted is None else round(converted, 2)
            out.append(item)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    if not subs:
        print("No subscriptions yet. Add one with: kit subs add <name> <price> [--every monthly]"
              if not ctx.store.subs else "Nothing to show (try --all).")
        return 0

    rows = []
    for sub in subs:
        dim = () if sub.active else ("dim",)
        renews = sub.next_renewal(ctx.today)
        days = (renews - ctx.today).days
        if not sub.active:
            when = (sub.status, ("muted",))
        elif days <= 1:
            when = (days_text(days), ("bold", "bad"))
        elif days <= ctx.due_days:
            when = (days_text(days), ("bold", "accent"))
        else:
            when = (days_text(days), dim)
        converted = ctx.monthly_in_base(sub)
        per_month = money(converted, ctx.base) if converted is not None else money(sub.monthly, sub.currency)
        status = f"trial to {sub.trial_end}" if sub.active and sub.in_trial(ctx.today) else ""
        rows.append([
            (sub.name, ("bold",) if sub.active else dim), (money(sub.price, sub.currency), dim),
            (str(sub.cycle), dim or ("muted",)), (per_month, dim if converted is not None else ("accent",)),
            (renews.isoformat(), dim), when, (sub.category, dim or ("muted",)), (status, ("accent",)),
        ])
    print_table(["NAME", "PRICE", "CYCLE", "PER MONTH", "RENEWS", "", "CATEGORY", ""], rows, right={1, 3})
    print()
    print_totals_line(ctx, subs)
    return 0


def cmd_show(ctx: Context, args: argparse.Namespace) -> int:
    sub = ctx.find(args.name)
    converted = ctx.monthly_in_base(sub)
    renews = sub.next_renewal(ctx.today)
    fields = [
        ("Price", f"{money(sub.price, sub.currency)} {sub.cycle}"),
        ("Per month", money(sub.monthly, sub.currency) + (f"  ({money(converted, ctx.base)})"
                                                          if converted is not None and sub.currency != ctx.base else "")),
        ("Per year", money(sub.monthly * 12, sub.currency)),
        ("Renews", f"{renews}  ({days_text((renews - ctx.today).days)})"),
        ("Status", sub.status),
        ("Category", sub.category or "-"),
        ("Trial ends", str(sub.trial_end) if sub.trial_end else "-"),
        ("Notes", sub.notes or "-"),
    ]
    print(style(sub.name, "bold", "accent"))
    for label, value in fields:
        print(f"  {style(label.ljust(11), 'muted')} {value}")
    return 0


def apply_edits(ctx: Context, sub: Subscription, args: argparse.Namespace) -> None:
    """Fields given on the command line, checked; dies with the reason on a bad value."""
    try:
        if getattr(args, "rename", None) is not None:
            sub.name = args.rename.strip()
        if args.price is not None:
            sub.price, currency = parse_price(args.price)
            sub.currency = currency or sub.currency
        if args.currency is not None:
            sub.currency = parse_currency(args.currency)
        if args.every is not None:
            sub.cycle = Cycle.parse(args.every)
        if args.next is not None:
            sub.anchor = parse_date(args.next, ctx.today)
        if args.category is not None:
            sub.category = args.category.strip()
        if args.trial_end is not None:
            sub.trial_end = None if args.trial_end.strip().lower() in ("", "none", "-") else parse_date(args.trial_end, ctx.today)
        if args.notes is not None:
            sub.notes = args.notes.strip()
        check(sub, ctx.store.subs)
    except ValueError as exc:
        die(str(exc))


def cmd_add(ctx: Context, args: argparse.Namespace) -> int:
    sub = Subscription(name=args.name.strip(), price=0.0, currency=ctx.base, cycle=Cycle(), anchor=ctx.today)
    apply_edits(ctx, sub, args)
    if args.next is None:
        # With no date: renews on the trial's end, or one cycle from today (as if you just paid).
        sub.anchor = sub.trial_end or step(ctx.today, sub.cycle, 1)
    ctx.store.subs.append(sub)
    ctx.save()
    renews = sub.next_renewal(ctx.today)
    print(f"Added {style(sub.name, 'bold')}: {money(sub.price, sub.currency)} {sub.cycle}, "
          f"renews {renews} ({days_text((renews - ctx.today).days)}).")
    return 0


def cmd_edit(ctx: Context, args: argparse.Namespace) -> int:
    sub = ctx.find(args.name)
    apply_edits(ctx, sub, args)
    ctx.save()
    print(f"Updated {style(sub.name, 'bold')}.")
    return 0


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    sub = ctx.find(args.name)
    sub.status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[args.command]
    ctx.save()
    print(f"{sub.name} is now {sub.status}.")
    return 0


def cmd_rm(ctx: Context, args: argparse.Namespace) -> int:
    sub = ctx.find(args.name)
    if not args.yes:
        if not sys.stdin.isatty():
            die("can't ask for confirmation in a non-interactive shell - add --yes")
        try:
            answer = input(f"Remove {sub.name} for good? (kit subs cancel keeps it in the list) [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Kept it.")
            return 1
    ctx.store.subs.remove(sub)
    ctx.save()
    print(f"Removed {sub.name}.")
    return 0


def cmd_due(ctx: Context, args: argparse.Namespace) -> int:
    days = args.days if args.days is not None else ctx.due_days
    upcoming = due_items(ctx.store.subs, ctx.today, days)
    if not upcoming:
        print(f"Nothing renews in the next {days} days.")
        return 0
    rows = []
    for when, kind, sub in upcoming:
        n = (when - ctx.today).days
        colour = ("bold", "bad") if n <= 1 else ("bold", "accent")
        what = "trial ends" if kind == "trial" else money(sub.price, sub.currency)
        rows.append([(sub.name, ("bold",)), (what, ("accent",) if kind == "trial" else ()),
                     (when.isoformat(), ()), (days_text(n), colour)])
    print_table(["NAME", "AMOUNT", "DATE", ""], rows, right={1})
    charged, unconverted = ctx.store.charged([sub for _, kind, sub in upcoming if kind == "renewal"], ctx.base)
    extra = f" + {unconverted_text(unconverted, '')}" if unconverted else ""
    print()
    print(style(f"{money(charged, ctx.base)}{extra} due in the next {days} days", "bold"))
    return 0


def cmd_total(ctx: Context, args: argparse.Namespace) -> int:
    active = [s for s in ctx.store.subs if s.active]
    if not active:
        print("No active subscriptions.")
        return 0
    groups: dict[str, list[Subscription]] = {}
    for sub in active:
        groups.setdefault(sub.category or "(none)", []).append(sub)
    rows = []
    overall, _ = ctx.store.totals(active, ctx.base)
    for category, subs in sorted(groups.items(), key=lambda g: -ctx.store.totals(g[1], ctx.base)[0]):
        monthly, unconverted = ctx.store.totals(subs, ctx.base)
        share = f"{monthly / overall:.0%}" if overall else ""
        extra = f" + {unconverted_text(unconverted, '')}" if unconverted else ""
        rows.append([(category, ("bold",)), (str(len(subs)), ("muted",)), (money(monthly, ctx.base) + extra, ()),
                     (money(monthly * 12, ctx.base), ("muted",)), (share, ("accent",))])
    print_table(["CATEGORY", "#", "PER MONTH", "PER YEAR", "SHARE"], rows, right={1, 2, 3, 4})
    print()
    print_totals_line(ctx, active)
    return 0


def cmd_rate(ctx: Context, args: argparse.Namespace) -> int:
    if args.currency is None:
        currencies = ctx.store.currencies(ctx.base)
        if not currencies:
            print(f"Everything is in {ctx.base} - no exchange rates needed.")
            return 0
        for cur in currencies:
            rate = ctx.store.rate(cur, ctx.base)
            shown = f"1 {cur} = {rate:.6g} {ctx.base}" if rate is not None else style(f"{cur}: no rate", "accent")
            print(f"  {shown}")
        print(style(f"\nset one with: kit subs rate <currency> <value in {ctx.base}>", "muted"))
        return 0
    try:
        currency = parse_currency(args.currency)
    except ValueError as exc:
        die(str(exc))
    if currency == ctx.base:
        die(f"{currency} is your main currency (subs.currency) - it doesn't need a rate")
    if args.unset:
        ctx.store.set_rate(currency, ctx.base, None)
        ctx.save()
        print(f"Removed the {currency} rate.")
        return 0
    if args.value is None:
        die(f"give the value of 1 {currency} in {ctx.base}, e.g. kit subs rate {currency} 1.08")
    try:
        value = float(args.value.replace(",", "."))
    except ValueError:
        die(f"'{args.value}' isn't a number")
    if value <= 0:
        die("a rate must be more than 0")
    ctx.store.set_rate(currency, ctx.base, value)
    ctx.save()
    print(f"1 {currency} = {value:g} {ctx.base}")
    return 0


# --- entry point --------------------------------------------------------------------------------

def add_field_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--currency", "-c", help="three-letter code (new subscriptions: the subs.currency setting)")
    parser.add_argument("--every", "-e", help=f"billing cycle: {CYCLE_HELP} (new subscriptions: monthly)")
    parser.add_argument("--next", "-n", metavar="DATE", help=f"next renewal date: {DATE_HELP}")
    parser.add_argument("--category", "-t", help="a group for totals, e.g. streaming, software")
    parser.add_argument("--trial-end", metavar="DATE", help="when a free trial turns into a paid one ('none' clears it)")
    parser.add_argument("--notes", help="anything worth remembering: plan, account, how to cancel")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # a name the console can't show mustn't crash kit
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="kit subs", description="Track your subscriptions: costs, totals and renewals.")
    parser.add_argument("--where", action="store_true", help="print where the data file lives and exit")
    commands = parser.add_subparsers(dest="command", metavar="command")

    p = commands.add_parser("list", aliases=["ls"], help="list subscriptions (cancelled ones need --all)")
    p.add_argument("--all", "-a", action="store_true", help="include cancelled subscriptions")
    p.add_argument("--sort", "-s", choices=SORTS, default="next", help="order (default: next renewal)")
    p.add_argument("--category", "-t", help="only this category")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")

    p = commands.add_parser("add", help="add a subscription")
    p.add_argument("name")
    p.add_argument("price", help="e.g. 9.99, or 9.99 EUR for another currency")
    add_field_flags(p)

    p = commands.add_parser("edit", help="change a subscription's details")
    p.add_argument("name", help="the subscription (any unique part of its name)")
    p.add_argument("--rename", metavar="NAME", help="give it a new name")
    p.add_argument("--price", "-p", help="new price, e.g. 12.99")
    add_field_flags(p)

    p = commands.add_parser("show", help="everything about one subscription")
    p.add_argument("name")

    for name, text in (("pause", "pause one (left out of totals and due)"), ("resume", "make one active again"),
                       ("cancel", "mark one cancelled (kept for the record; hidden from list without --all)")):
        commands.add_parser(name, help=text).add_argument("name")

    p = commands.add_parser("rm", aliases=["remove"], help="delete a subscription for good")
    p.add_argument("name")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask")

    p = commands.add_parser("due", help="what renews soon, and trials about to end")
    p.add_argument("days", nargs="?", type=int, help="how far ahead to look (default: the subs.due_days setting)")

    commands.add_parser("total", aliases=["totals"], help="monthly and yearly cost, by category")

    p = commands.add_parser("rate", help="see or set exchange rates into your main currency")
    p.add_argument("currency", nargs="?", help="e.g. EUR")
    p.add_argument("value", nargs="?", help="what 1 of it costs in your main currency")
    p.add_argument("--unset", action="store_true", help="remove the rate")

    args = parser.parse_args()
    if args.where:
        print(data_path())
        return 0

    ctx = Context()
    if args.command is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return cmd_list(ctx, parser.parse_args(["list"]))
        from subs_ui import run_app
        return run_app(ctx)

    handlers = {
        "list": cmd_list, "ls": cmd_list, "add": cmd_add, "edit": cmd_edit, "show": cmd_show,
        "pause": cmd_status, "resume": cmd_status, "cancel": cmd_status, "rm": cmd_rm, "remove": cmd_rm,
        "due": cmd_due, "total": cmd_total, "totals": cmd_total, "rate": cmd_rate,
    }
    return handlers[args.command](ctx, args)


if __name__ == "__main__":
    raise SystemExit(main())
