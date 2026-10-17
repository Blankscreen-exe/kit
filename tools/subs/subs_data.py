"""kit subs's data: subscriptions, billing cycles, renewal dates, exchange rates and totals. No UI here.

Each subscription stores one known billing date (its anchor) and a cycle. The next renewal is always
worked out from the anchor, never stored, so it moves on by itself and a monthly bill on the 31st stays on
the last day of short months instead of drifting to the 28th.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

STATUSES = ("active", "paused", "cancelled")
UNITS = ("day", "week", "month", "year")
PER_YEAR = {"day": 365.25, "week": 365.25 / 7, "month": 12.0, "year": 1.0}
CYCLE_WORDS = {
    "daily": (1, "day"), "weekly": (1, "week"), "biweekly": (2, "week"), "fortnightly": (2, "week"),
    "monthly": (1, "month"), "quarterly": (3, "month"), "half-yearly": (6, "month"), "semiannual": (6, "month"),
    "yearly": (1, "year"), "annual": (1, "year"), "annually": (1, "year"),
}
CYCLE_NAMES = {(1, "day"): "daily", (1, "week"): "weekly", (2, "week"): "fortnightly",
               (1, "month"): "monthly", (3, "month"): "quarterly", (1, "year"): "yearly"}
UNIT_PATTERN = r"(d|days?|w|wks?|weeks?|m|mos?|months?|y|yrs?|years?)"  # group 1's first letter is the unit
UNIT_LETTERS = {"d": "day", "w": "week", "m": "month", "y": "year"}
CYCLE_HELP = "monthly, yearly, weekly, quarterly, or a count like 3m, 2w, 90d, 2y"
DATE_HELP = "YYYY-MM-DD, today, tomorrow, or +10d / +2w / +1m / +1y"


# --- parsing ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Cycle:
    every: int = 1
    unit: str = "month"

    @classmethod
    def parse(cls, text: str) -> Cycle:
        cleaned = text.strip().lower()
        if cleaned in CYCLE_WORDS:
            return cls(*CYCLE_WORDS[cleaned])
        match = re.fullmatch(r"(?:every\s*)?(\d+)?\s*" + UNIT_PATTERN, cleaned)
        if not match:
            raise ValueError(f"don't understand the cycle '{text}' - use {CYCLE_HELP}")
        every = int(match.group(1) or 1)
        if not 1 <= every <= 1000:
            raise ValueError("a cycle must be between 1 and 1000 units long")
        return cls(every, UNIT_LETTERS[match.group(2)[0]])

    def __str__(self) -> str:
        return CYCLE_NAMES.get((self.every, self.unit)) or f"every {self.every} {self.unit}s"

    @property
    def per_year(self) -> float:
        return PER_YEAR[self.unit] / self.every


def parse_date(text: str, today: date) -> date:
    cleaned = text.strip().lower()
    if cleaned == "today":
        return today
    if cleaned == "tomorrow":
        return today + timedelta(days=1)
    match = re.fullmatch(r"\+(\d+)\s*" + UNIT_PATTERN, cleaned)
    if match:
        return step(today, Cycle(1, UNIT_LETTERS[match.group(2)[0]]), int(match.group(1)))
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        raise ValueError(f"don't understand the date '{text}' - use {DATE_HELP}") from None


def parse_price(text: str) -> tuple[float, str | None]:
    """'9.99', '9,99', '9.99 EUR' or 'EUR 9.99' -> (9.99, 'EUR' or None)."""
    match = re.fullmatch(r"\s*([A-Za-z]{3})?\s*(\d+(?:[.,]\d+)?)\s*([A-Za-z]{3})?\s*", text)
    if not match or (match.group(1) and match.group(3)):
        raise ValueError(f"don't understand the price '{text}' - use a number like 9.99, optionally with a currency (9.99 EUR)")
    currency = match.group(1) or match.group(3)
    return float(match.group(2).replace(",", ".")), currency.upper() if currency else None


def parse_currency(text: str) -> str:
    code = text.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError(f"'{text}' isn't a currency code - use three letters like USD, EUR or GBP")
    return code


# --- dates -----------------------------------------------------------------------------------

def add_months(start: date, months: int) -> date:
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def step(anchor: date, cycle: Cycle, count: int) -> date:
    """The billing date `count` cycles after `anchor`."""
    if cycle.unit == "day":
        return anchor + timedelta(days=count * cycle.every)
    if cycle.unit == "week":
        return anchor + timedelta(weeks=count * cycle.every)
    return add_months(anchor, count * cycle.every * (12 if cycle.unit == "year" else 1))


def next_on_or_after(anchor: date, cycle: Cycle, today: date) -> date:
    if anchor >= today:
        return anchor
    longest = {"day": 1, "week": 7, "month": 31, "year": 366}[cycle.unit] * cycle.every
    count = (today - anchor).days // longest  # never past the answer; a few steps finish it
    while step(anchor, cycle, count) < today:
        count += 1
    return step(anchor, cycle, count)


def days_text(days: int) -> str:
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


def money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


# --- subscriptions ---------------------------------------------------------------------------

@dataclass
class Subscription:
    name: str
    price: float
    currency: str
    cycle: Cycle
    anchor: date
    category: str = ""
    status: str = "active"
    trial_end: date | None = None
    notes: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def next_renewal(self, today: date) -> date:
        return next_on_or_after(self.anchor, self.cycle, today)

    def in_trial(self, today: date) -> bool:
        return self.trial_end is not None and self.trial_end >= today

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def monthly(self) -> float:
        """The price spread evenly over months, in the subscription's own currency."""
        return self.price * self.cycle.per_year / 12

    def to_json(self) -> dict:
        data = {
            "id": self.id, "name": self.name, "price": self.price, "currency": self.currency,
            "every": self.cycle.every, "unit": self.cycle.unit, "anchor": self.anchor.isoformat(),
            "category": self.category, "status": self.status, "notes": self.notes,
        }
        if self.trial_end:
            data["trial_end"] = self.trial_end.isoformat()
        return data

    @classmethod
    def from_json(cls, data: dict) -> Subscription:
        unit = str(data.get("unit", "month"))
        status = str(data.get("status", "active"))
        if unit not in UNITS or status not in STATUSES:
            raise ValueError(f"unknown unit '{unit}' or status '{status}'")
        trial = data.get("trial_end")
        return cls(
            name=str(data["name"]), price=float(data["price"]), currency=parse_currency(str(data["currency"])),
            cycle=Cycle(int(data.get("every", 1)), unit), anchor=date.fromisoformat(data["anchor"]),
            category=str(data.get("category", "")), status=status,
            trial_end=date.fromisoformat(trial) if trial else None, notes=str(data.get("notes", "")),
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
        )


def due_items(subs: list[Subscription], today: date, days: int) -> list[tuple[date, str, Subscription]]:
    """(date, "renewal" or "trial", sub) for active subscriptions within `days`, soonest first."""
    items = []
    for sub in subs:
        if not sub.active:
            continue
        renews = sub.next_renewal(today)
        if (renews - today).days <= days:
            items.append((renews, "renewal", sub))
        if sub.in_trial(today) and (sub.trial_end - today).days <= days and sub.trial_end != renews:
            items.append((sub.trial_end, "trial", sub))
    return sorted(items, key=lambda item: (item[0], item[2].name.casefold()))


def check(sub: Subscription, others: list[Subscription]) -> None:
    """Raise ValueError if the subscription can't be saved next to `others`."""
    if not sub.name.strip():
        raise ValueError("a subscription needs a name")
    if sub.price < 0:
        raise ValueError("the price can't be negative")
    taken = next((o for o in others if o.id != sub.id and o.name.casefold() == sub.name.casefold()), None)
    if taken:
        raise ValueError(f"there's already a subscription called '{taken.name}'")


# --- the file --------------------------------------------------------------------------------

def data_path() -> Path:
    override = os.environ.get("KIT_SUBS_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "subs.json"


class StoreError(Exception):
    pass


class Store:
    """Every subscription and exchange rate, in one JSON file.

    Rates are kept as pairs: {"EUR/USD": 1.08} means 1 EUR costs 1.08 USD, and works both ways.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.subs: list[Subscription] = []
        self.rates: dict[str, float] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            # Refuse rather than start empty: the next save would overwrite what's there.
            raise StoreError(f"can't read {path}: {exc} - fix the file or move it away") from None
        try:
            self.subs = [Subscription.from_json(item) for item in raw.get("subscriptions", [])]
            self.rates = {str(pair): float(rate) for pair, rate in raw.get("rates", {}).items()}
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise StoreError(f"{path} has an entry kit subs doesn't understand ({exc}) - fix the file or move it away") from None

    def save(self) -> None:
        data = {"version": 1, "rates": self.rates, "subscriptions": [sub.to_json() for sub in self.subs]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def find(self, query: str) -> Subscription:
        """By name: exact (any case), then the start of a name, then anywhere in it. Raises ValueError."""
        wanted = query.strip().casefold()
        for test in (lambda name: name == wanted, lambda name: name.startswith(wanted), lambda name: wanted in name):
            matches = [sub for sub in self.subs if test(sub.name.casefold())]
            if len(matches) == 1:
                return matches[0]
            if matches:
                names = ", ".join(sub.name for sub in matches)
                raise ValueError(f"'{query}' matches {names} - be more specific")
        raise ValueError(f"no subscription called '{query}' - see them with: kit subs list --all")

    # --- money ---

    def rate(self, currency: str, base: str) -> float | None:
        """What 1 `currency` costs in `base`, or None if there's no rate for it."""
        if currency == base:
            return 1.0
        if f"{currency}/{base}" in self.rates:
            return self.rates[f"{currency}/{base}"]
        if self.rates.get(f"{base}/{currency}"):
            return 1 / self.rates[f"{base}/{currency}"]
        return None

    def set_rate(self, currency: str, base: str, value: float | None) -> None:
        self.rates.pop(f"{base}/{currency}", None)
        if value is None:
            self.rates.pop(f"{currency}/{base}", None)
        else:
            self.rates[f"{currency}/{base}"] = value

    def convert(self, amount: float, currency: str, base: str) -> float | None:
        rate = self.rate(currency, base)
        return None if rate is None else amount * rate

    def _sum(self, amounts: list[tuple[float, str]], base: str) -> tuple[float, dict[str, float]]:
        total, unconverted = 0.0, {}
        for amount, currency in amounts:
            converted = self.convert(amount, currency, base)
            if converted is None:
                unconverted[currency] = unconverted.get(currency, 0.0) + amount
            else:
                total += converted
        return total, unconverted

    def totals(self, subs: list[Subscription], base: str) -> tuple[float, dict[str, float]]:
        """Monthly cost of `subs` in `base`, plus monthly amounts in currencies with no rate."""
        return self._sum([(sub.monthly, sub.currency) for sub in subs], base)

    def charged(self, subs: list[Subscription], base: str) -> tuple[float, dict[str, float]]:
        """What one renewal of each of `subs` charges, in `base`, plus amounts in currencies with no rate."""
        return self._sum([(sub.price, sub.currency) for sub in subs], base)

    def currencies(self, base: str) -> list[str]:
        """Currencies other than `base` that subscriptions use or rates mention."""
        used = {sub.currency for sub in self.subs}
        for pair in self.rates:
            used.update(pair.split("/"))
        used.discard(base)
        return sorted(used)
