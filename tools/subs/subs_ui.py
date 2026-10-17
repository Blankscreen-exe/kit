"""The app side of kit subs: a table of subscriptions, totals, what's due soon, and forms to change them."""

from __future__ import annotations

from dataclasses import replace

from kitlib import KIT_HOME, die
from kitlib.theme import ACCENT, BAD, DIM, GOOD, MUTED, TEXT

try:
    from rich.text import Text
    from textual import on
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
    from textual.coordinate import Coordinate
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Input, Label, Static

    from kitlib.tui import KitApp, brand, title
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

from subs_data import (CYCLE_HELP, Cycle, Subscription, check, days_text, due_items, money, parse_currency, parse_date,
                       parse_price, step)

SORTS = ("next", "cost", "name")

CSS = f"""
#topbar {{ height: 1; padding: 0 1; }}
#brand {{ width: auto; }}
#summary {{ width: 1fr; content-align: right middle; }}
#table {{ height: 1fr; scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; }}
#panels {{ height: auto; max-height: 12; }}
#due, #cats {{ width: 1fr; height: auto; max-height: 12; padding: 0 1; }}

.dialog {{ width: 72; max-width: 95%; height: auto; max-height: 100%; }}
.fields {{ grid-size: 2; grid-columns: 16 1fr; grid-rows: 3; grid-gutter: 0 1; height: auto; }}
.fields Label {{ height: 3; width: 100%; content-align: left middle; text-style: bold; }}
.hint {{ color: {MUTED}; height: auto; }}
.error {{ color: {BAD}; height: auto; }}
.buttons {{ width: 100%; height: 3; align: right middle; margin-top: 1; }}
.buttons Button {{ margin: 0 0 0 1; }}
"""

FORM_FIELDS = [
    ("name", "Name", "Netflix"),
    ("price", "Price", "9.99"),
    ("currency", "Currency", "USD"),
    ("every", "Billed", CYCLE_HELP),
    ("next", "Next renewal", "YYYY-MM-DD, +1m (empty: a cycle from today)"),
    ("category", "Category", "streaming, software, cloud..."),
    ("trial", "Trial ends", "optional: YYYY-MM-DD"),
    ("notes", "Notes", "plan, account, how to cancel"),
]


class SubForm(ModalScreen):
    """Add or edit one subscription; dismisses with the saved Subscription, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, sub: Subscription | None, others: list[Subscription], base: str, today) -> None:
        super().__init__()
        self.sub, self.others, self.base, self.today = sub, others, base, today

    def initial(self) -> dict[str, str]:
        sub = self.sub
        if sub is None:
            return {"currency": self.base, "every": "monthly"}
        return {
            "name": sub.name, "price": f"{sub.price:g}", "currency": sub.currency, "every": str(sub.cycle),
            "next": sub.next_renewal(self.today).isoformat(), "category": sub.category,
            "trial": sub.trial_end.isoformat() if sub.trial_end else "", "notes": sub.notes,
        }

    def compose(self) -> ComposeResult:
        values = self.initial()
        with VerticalScroll(classes="dialog") as dialog:  # scrolls in a short window, so SAVE stays reachable
            dialog.border_title = title("Edit subscription" if self.sub else "Add subscription")
            with Grid(classes="fields"):
                for key, label, placeholder in FORM_FIELDS:
                    yield Label(label)
                    yield Input(values.get(key, ""), placeholder=placeholder, id=f"f-{key}")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("CANCEL", id="cancel")
                yield Button("SAVE", id="save", classes="primary")

    def on_mount(self) -> None:
        self.query_one("#f-name", Input).focus()

    def value(self, key: str) -> str:
        return self.query_one(f"#f-{key}", Input).value.strip()

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    def save(self) -> None:
        try:
            price, price_currency = parse_price(self.value("price") or "0")
            cycle = Cycle.parse(self.value("every") or "monthly")
            trial = parse_date(self.value("trial"), self.today) if self.value("trial") else None
            if self.value("next"):
                anchor = parse_date(self.value("next"), self.today)
            elif self.sub and self.sub.cycle == cycle:
                anchor = self.sub.anchor
            else:
                anchor = trial or step(self.today, cycle, 1)
            fields = dict(
                name=self.value("name"), price=price, cycle=cycle, anchor=anchor, trial_end=trial,
                currency=parse_currency(price_currency or self.value("currency") or self.base),  # "8 EUR" wins
                category=self.value("category"), notes=self.value("notes"),
            )
            sub = replace(self.sub, **fields) if self.sub else Subscription(**fields)
            check(sub, self.others)
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc)[:1].upper() + str(exc)[1:] + ".")
            return
        self.dismiss(sub)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape,n", "cancel", "Cancel"), Binding("y", "ok", "Yes")]

    def __init__(self, heading: str, body: str, ok_label: str) -> None:
        super().__init__()
        self.heading, self.body, self.ok_label = heading, body, ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = title(self.heading)
            yield Static(self.body)
            with Horizontal(classes="buttons"):
                yield Button("CANCEL (N)", id="cancel")
                yield Button(f"{self.ok_label.upper()} (Y)", id="ok", classes="danger")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RatesScreen(ModalScreen):
    """Exchange rates into the main currency; dismisses with {currency: rate or None}, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, store, base: str) -> None:
        super().__init__()
        self.store, self.base = store, base
        self.currencies = store.currencies(base)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = title("Exchange rates")
            yield Static(f"What 1 of each currency costs in {self.base}. Leave one empty to remove its rate.",
                         classes="hint")
            with Grid(classes="fields"):
                for cur in self.currencies:
                    rate = self.store.rate(cur, self.base)
                    yield Label(f"1 {cur} =")
                    yield Input(f"{rate:.6g}" if rate is not None else "", placeholder=f"in {self.base}",
                                id=f"r-{cur}")
            yield Static("", id="rates-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("CANCEL", id="cancel")
                yield Button("SAVE", id="save", classes="primary")

    def on_mount(self) -> None:
        self.query(Input).first().focus()

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    def save(self) -> None:
        rates = {}
        for cur in self.currencies:
            text = self.query_one(f"#r-{cur}", Input).value.strip().replace(",", ".")
            try:
                rates[cur] = float(text) if text else None
            except ValueError:
                rates[cur] = -1.0
            if rates[cur] is not None and rates[cur] <= 0:
                self.query_one("#rates-error", Static).update(f"The {cur} rate must be a number above 0.")
                return
        self.dismiss(rates)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class SubsApp(KitApp):
    TITLE = "kit subs"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("p", "pause", "Pause/Resume"),
        Binding("c", "cancel_sub", "Cancel/Restore"),
        Binding("d,delete", "delete", "Delete"),
        Binding("s", "sort", "Sort"),
        Binding("h", "hidden", "Show cancelled"),
        Binding("r", "rates", "Rates"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.store = ctx.store
        self.sort = "next"
        self.show_cancelled = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(brand("kit subs"), id="brand")
            yield Static("", id="summary")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        with Horizontal(id="panels"):
            yield Static("", id="due", classes="slab")
            yield Static("", id="cats", classes="slab")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("NAME", "PRICE", "CYCLE", "PER MONTH", "RENEWS", "IN", "CATEGORY", "STATUS", "NOTES")
        self.query_one("#due").border_title = title(f"Due in {self.ctx.due_days} days")
        self.query_one("#cats").border_title = title("By category")
        self.refresh_all()
        table.focus()

    # --- view ---

    def visible(self) -> list[Subscription]:
        subs = [s for s in self.store.subs if self.show_cancelled or s.status != "cancelled"]
        today = self.ctx.today
        if self.sort == "name":
            return sorted(subs, key=lambda s: s.name.casefold())
        if self.sort == "cost":
            return sorted(subs, key=lambda s: -(self.ctx.monthly_in_base(s) or s.monthly))
        return sorted(subs, key=lambda s: (not s.active, s.next_renewal(today), s.name.casefold()))

    def refresh_all(self, keep: str | None = None) -> None:
        self.fill_table(keep)
        self.fill_summary()
        self.fill_due()
        self.fill_categories()

    def fill_table(self, keep: str | None = None) -> None:
        table = self.query_one("#table", DataTable)
        keep = keep or (self.current().id if self.current() else None)
        table.clear()
        today, base = self.ctx.today, self.ctx.base
        for sub in self.visible():
            renews = sub.next_renewal(today)
            days = (renews - today).days
            plain = TEXT if sub.active else DIM
            if not sub.active:
                when = Text("-", style=DIM)
            elif days <= 1:
                when = Text(days_text(days), style=f"bold {BAD}")
            elif days <= self.ctx.due_days:
                when = Text(days_text(days), style=f"bold {ACCENT}")
            else:
                when = Text(days_text(days), style=MUTED)
            converted = self.ctx.monthly_in_base(sub)
            per_month = (Text(money(converted, base), style=plain, justify="right") if converted is not None else
                         Text(money(sub.monthly, sub.currency) + " ?", style=ACCENT if sub.active else DIM, justify="right"))
            if sub.status != "active":
                status = Text(sub.status, style=MUTED if sub.status == "paused" else DIM)
            elif sub.in_trial(today):
                status = Text(f"trial · {days_text((sub.trial_end - today).days)}", style=ACCENT)
            else:
                status = Text("active", style=GOOD)
            table.add_row(
                Text(sub.name, style=f"bold {plain}"),
                Text(money(sub.price, sub.currency), style=plain, justify="right"),
                Text(str(sub.cycle), style=MUTED if sub.active else DIM),
                per_month,
                Text(renews.isoformat(), style=plain),
                when,
                Text(sub.category or "-", style=MUTED if sub.active else DIM),
                status,
                Text(sub.notes[:40], style=DIM),
                key=sub.id,
            )
        if table.row_count:
            row = 0
            if keep:
                try:
                    row = table.get_row_index(keep)
                except Exception:
                    row = min(table.cursor_row, table.row_count - 1)
            table.move_cursor(row=row)

    def fill_summary(self) -> None:
        base = self.ctx.base
        active = [s for s in self.store.subs if s.active]
        monthly, unconverted = self.store.totals(active, base)
        text = Text()
        text.append(f"{len(active)} active", style=MUTED)
        text.append("  ·  ", style=DIM)
        text.append(money(monthly, base), style=f"bold {TEXT}")
        text.append("/mo", style=MUTED)
        text.append("  ·  ", style=DIM)
        text.append(money(monthly * 12, base), style=f"bold {TEXT}")
        text.append("/yr", style=MUTED)
        if unconverted:
            extra = " + ".join(money(amount, cur) for cur, amount in sorted(unconverted.items()))
            text.append(f"  ·  + {extra}/mo without a rate (r)", style=f"bold {ACCENT}")
        text.append(f"  ·  sort: {self.sort}", style=DIM)
        self.query_one("#summary", Static).update(text)

    def fill_due(self) -> None:
        today = self.ctx.today
        items = due_items(self.store.subs, today, self.ctx.due_days)
        text = Text()
        if not items:
            text.append("Nothing renews soon.", style=MUTED)
        for when, kind, sub in items:
            days = (when - today).days
            if text:
                text.append("\n")
            text.append(f"{days_text(days):<10}", style=f"bold {BAD if days <= 1 else ACCENT}")
            text.append(f" {sub.name}", style=f"bold {TEXT}")
            text.append("  trial ends" if kind == "trial" else f"  {money(sub.price, sub.currency)}",
                        style=ACCENT if kind == "trial" else MUTED)
        self.query_one("#due", Static).update(text)

    def fill_categories(self) -> None:
        base = self.ctx.base
        groups: dict[str, list[Subscription]] = {}
        for sub in self.store.subs:
            if sub.active:
                groups.setdefault(sub.category or "(none)", []).append(sub)
        overall, _ = self.store.totals([s for s in self.store.subs if s.active], base)
        text = Text()
        if not groups:
            text.append("No active subscriptions.", style=MUTED)
        rows = sorted(((name, *self.store.totals(subs, base)) for name, subs in groups.items()), key=lambda r: -r[1])
        width = max((len(name) for name, *_ in rows), default=0)
        for name, monthly, unconverted in rows:
            if text:
                text.append("\n")
            share = monthly / overall if overall else 0
            text.append(f"{name:<{width}}  ", style=TEXT)
            if monthly or not unconverted:
                text.append(f"{money(monthly, base):>14}/mo  ", style=f"bold {TEXT}")
                text.append("█" * round(share * 12), style=ACCENT)
                text.append(f" {share:.0%}  ", style=MUTED)
            if unconverted:
                extra = " + ".join(money(amount, cur) for cur, amount in sorted(unconverted.items()))
                text.append(f"{'+ ' if monthly else ''}{extra}/mo ?", style=ACCENT)
        self.query_one("#cats", Static).update(text)

    def current(self) -> Subscription | None:
        table = self.query_one("#table", DataTable)
        if not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
        except Exception:
            return None
        return next((s for s in self.store.subs if s.id == key), None)

    # --- changes ---

    def persist(self, message: str, keep: str | None = None) -> None:
        try:
            self.store.save()
        except OSError as exc:
            self.notify(f"Couldn't save {self.store.path}: {exc}", severity="error", timeout=10)
        else:
            self.notify(message, timeout=3)
        self.refresh_all(keep)

    def action_add(self) -> None:
        def done(sub: Subscription | None) -> None:
            if sub is not None:
                self.store.subs.append(sub)
                self.persist(f"Added {sub.name}.", keep=sub.id)

        self.push_screen(SubForm(None, self.store.subs, self.ctx.base, self.ctx.today), done)

    def action_edit(self) -> None:
        current = self.current()
        if current is None:
            self.action_add()
            return

        def done(sub: Subscription | None) -> None:
            if sub is not None:
                index = self.store.subs.index(current)
                self.store.subs[index] = sub
                self.persist(f"Saved {sub.name}.", keep=sub.id)

        self.push_screen(SubForm(current, self.store.subs, self.ctx.base, self.ctx.today), done)

    @on(DataTable.RowSelected)
    def row_selected(self) -> None:
        self.action_edit()

    def action_pause(self) -> None:
        sub = self.current()
        if sub is not None:
            sub.status = "active" if sub.status == "paused" else "paused"
            self.persist(f"{sub.name} is now {sub.status}.", keep=sub.id)

    def action_cancel_sub(self) -> None:
        sub = self.current()
        if sub is not None:
            sub.status = "active" if sub.status == "cancelled" else "cancelled"
            hidden = sub.status == "cancelled" and not self.show_cancelled
            self.persist(f"{sub.name} is now {sub.status}." + (" Press h to see cancelled ones." if hidden else ""),
                         keep=sub.id)

    def action_delete(self) -> None:
        sub = self.current()
        if sub is None:
            return

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.store.subs.remove(sub)
                self.persist(f"Deleted {sub.name}.")

        body = f"Delete {sub.name} for good?\n\nTo keep it on record instead, cancel it (c)."
        self.push_screen(ConfirmScreen("Delete subscription", body, "Delete"), done)

    def action_sort(self) -> None:
        self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        self.fill_table()
        self.fill_summary()

    def action_hidden(self) -> None:
        self.show_cancelled = not self.show_cancelled
        self.fill_table()
        self.notify("Showing cancelled subscriptions." if self.show_cancelled else "Hiding cancelled subscriptions.",
                    timeout=2)

    def action_rates(self) -> None:
        if not self.store.currencies(self.ctx.base):
            self.notify(f"Everything is in {self.ctx.base} - no exchange rates needed.", timeout=4)
            return

        def done(rates: dict | None) -> None:
            if rates is not None:
                for cur, rate in rates.items():
                    self.store.set_rate(cur, self.ctx.base, rate)
                self.persist("Rates saved.")

        self.push_screen(RatesScreen(self.store, self.ctx.base), done)


def run_app(ctx) -> int:
    SubsApp(ctx).run()
    return 0
