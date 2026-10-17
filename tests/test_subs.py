"""kit subs: billing cycles, renewal dates, currency totals, the data file, the CLI and the app."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "subs"
sys.path.insert(0, str(TOOL))

import subs_data  # noqa: E402
from subs_data import Cycle, Store, StoreError, Subscription, due_items, next_on_or_after, parse_date, parse_price  # noqa: E402

TODAY = date(2026, 10, 17)


def sub(name="Netflix", price=10.0, currency="USD", cycle=Cycle(), anchor=TODAY, **extra):
    return Subscription(name=name, price=price, currency=currency, cycle=cycle, anchor=anchor, **extra)


# --- parsing ---

@pytest.mark.parametrize("text, expected", [
    ("monthly", Cycle(1, "month")), ("Yearly", Cycle(1, "year")), ("quarterly", Cycle(3, "month")),
    ("3m", Cycle(3, "month")), ("2 weeks", Cycle(2, "week")), ("every 90 days", Cycle(90, "day")), ("y", Cycle(1, "year")),
])
def test_cycles_parse(text, expected):
    assert Cycle.parse(text) == expected


@pytest.mark.parametrize("cycle", [Cycle(1, "month"), Cycle(3, "month"), Cycle(2, "week"), Cycle(5, "month"), Cycle(90, "day")])
def test_cycle_names_read_back(cycle):
    assert Cycle.parse(str(cycle)) == cycle


@pytest.mark.parametrize("bad", ["3x", "", "every", "0m", "monthy", "dogs"])
def test_bad_cycles_explain_themselves(bad):
    with pytest.raises(ValueError, match="cycle"):
        Cycle.parse(bad)


def test_prices_and_dates():
    assert parse_price("9.99") == (9.99, None)
    assert parse_price("9,99 eur") == (9.99, "EUR")
    assert parse_price("GBP 4") == (4.0, "GBP")
    for bad in ("abc", "-3", "USD 1 EUR"):
        with pytest.raises(ValueError):
            parse_price(bad)
    assert parse_date("tomorrow", TODAY) == date(2026, 10, 18)
    assert parse_date("+1m", TODAY) == date(2026, 11, 17)
    assert parse_date("+2w", TODAY) == date(2026, 10, 31)
    assert parse_date("2027-01-31", TODAY) == date(2027, 1, 31)
    with pytest.raises(ValueError, match="date"):
        parse_date("next friday", TODAY)


# --- renewals ---

def test_a_bill_on_the_31st_stays_at_the_end_of_the_month():
    monthly = Cycle(1, "month")
    anchor = date(2026, 1, 31)
    assert next_on_or_after(anchor, monthly, date(2026, 2, 1)) == date(2026, 2, 28)
    assert next_on_or_after(anchor, monthly, date(2026, 3, 1)) == date(2026, 3, 31)  # not the 28th
    assert next_on_or_after(anchor, monthly, date(2026, 4, 1)) == date(2026, 4, 30)


def test_renewal_on_the_day_counts_as_today():
    assert next_on_or_after(date(2026, 9, 17), Cycle(1, "month"), TODAY) == TODAY


def test_renewals_far_in_the_past_and_future():
    assert next_on_or_after(date(2010, 2, 28), Cycle(1, "year"), TODAY) == date(2027, 2, 28)
    assert next_on_or_after(date(2001, 1, 1), Cycle(1, "week"), TODAY) >= TODAY
    assert next_on_or_after(date(2030, 5, 5), Cycle(1, "month"), TODAY) == date(2030, 5, 5)
    for days in range(0, 400, 7):  # never before today, never a whole cycle after it
        start = date.fromordinal(TODAY.toordinal() - days)
        found = next_on_or_after(start, Cycle(1, "month"), TODAY)
        assert TODAY <= found < subs_data.add_months(TODAY, 1)


def test_monthly_cost_of_every_cycle():
    assert sub(price=120, cycle=Cycle(1, "year")).monthly == pytest.approx(10)
    assert sub(price=30, cycle=Cycle(3, "month")).monthly == pytest.approx(10)
    assert sub(price=7, cycle=Cycle(1, "week")).monthly == pytest.approx(7 * 365.25 / 7 / 12)


def test_due_includes_trial_ends_and_skips_inactive():
    subs = [
        sub("Soon", anchor=date(2026, 10, 20)),
        sub("Later", anchor=date(2026, 12, 1)),
        sub("Trial", anchor=date(2026, 12, 1), trial_end=date(2026, 10, 19)),
        sub("Paused", anchor=date(2026, 10, 18), status="paused"),
    ]
    items = [(when, kind, s.name) for when, kind, s in due_items(subs, TODAY, 7)]
    assert items == [(date(2026, 10, 19), "trial", "Trial"), (date(2026, 10, 20), "renewal", "Soon")]


# --- money ---

def test_rates_work_both_ways_and_missing_ones_stay_separate(tmp_path):
    store = Store(tmp_path / "subs.json")
    store.subs = [sub("A", 10, "USD"), sub("B", 10, "EUR"), sub("C", 1000, "JPY"), sub("D", 5, "GBP")]
    store.set_rate("EUR", "USD", 1.1)
    store.rates["USD/GBP"] = 0.8  # stored the other way round
    total, unconverted = store.totals(store.subs, "USD")
    assert total == pytest.approx(10 + 11 + 5 / 0.8)
    assert unconverted == {"JPY": pytest.approx(1000)}
    store.set_rate("GBP", "USD", 1.25)  # replaces the reversed pair instead of keeping both
    assert "USD/GBP" not in store.rates and store.rate("GBP", "USD") == 1.25
    assert store.currencies("USD") == ["EUR", "GBP", "JPY"]


# --- the file ---

def test_store_round_trip(tmp_path):
    path = tmp_path / "deep" / "subs.json"
    store = Store(path)
    store.subs = [sub("Ünïcode ✓", 9.5, "EUR", Cycle(2, "week"), trial_end=date(2026, 11, 1), notes="n", category="c")]
    store.set_rate("EUR", "USD", 1.08)
    store.save()
    again = Store(path)
    assert again.subs == store.subs and again.rates == {"EUR/USD": 1.08}


def test_a_broken_file_is_never_overwritten(tmp_path):
    path = tmp_path / "subs.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StoreError, match="can't read"):
        Store(path)
    path.write_text(json.dumps({"subscriptions": [{"name": "x"}]}), encoding="utf-8")
    with pytest.raises(StoreError, match="doesn't understand"):
        Store(path)


def test_find_by_any_unique_part_of_a_name(tmp_path):
    store = Store(tmp_path / "subs.json")
    store.subs = [sub("Netflix"), sub("Net Guard"), sub("Spotify")]
    assert store.find("SPOT").name == "Spotify"
    assert store.find("netflix").name == "Netflix"
    assert store.find("flix").name == "Netflix"
    with pytest.raises(ValueError, match="matches Netflix, Net Guard"):
        store.find("net")
    with pytest.raises(ValueError, match="no subscription"):
        store.find("hulu")


# --- the command line, through kit ---

@pytest.fixture
def kit(tmp_path):
    env = dict(os.environ, KIT_SUBS_DATA=str(tmp_path / "subs.json"), KIT_CONFIG=str(tmp_path / "config.toml"),
               NO_COLOR="1", PYTHONIOENCODING="utf-8")
    (tmp_path / "config.toml").write_text('[subs]\ncurrency = "EUR"\n', encoding="utf-8")

    def run(*args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "kit.py"), "subs", *args], env=env, capture_output=True,
                                text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
        assert (result.returncode == 0) == ok, result.stdout + result.stderr
        return result.stdout + result.stderr

    run.path = tmp_path / "subs.json"
    return run


def test_cli_add_list_total_and_remove(kit):
    assert "renews" in kit("add", "Netflix", "15.49", "--category", "streaming", "--next", "+3d")
    kit("add", "Adobe", "240 USD", "--every", "yearly", "-t", "software")
    kit("add", "netflix", "1", ok=False)
    out = kit("list")
    assert "15.49 EUR" in out and "240.00 USD" in out and "no exchange rate" in out
    kit("rate", "USD", "0.9")
    assert "33.49 EUR/mo" in kit("total")  # 15.49 + 240 / 12 * 0.9
    assert "Netflix" in kit("due")
    kit("cancel", "adob")
    assert "Adobe" not in kit("list") and "Adobe" in kit("list", "--all")
    # no one to answer: refused ("add --yes") or asked and kept (Windows reports NUL as a terminal)
    out = kit("rm", "netflix", ok=False)
    assert "add --yes" in out or "Kept it" in out
    assert "Netflix" in kit("list")
    kit("rm", "netflix", "--yes")
    saved = json.loads(kit.path.read_text(encoding="utf-8"))
    assert [s["name"] for s in saved["subscriptions"]] == ["Adobe"] and saved["rates"] == {"USD/EUR": 0.9}


def test_bare_command_without_a_terminal_prints_the_list(kit):
    kit("add", "Netflix", "15.49")
    assert "Netflix" in kit()


# --- the app ---

def test_app_add_pause_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("KIT_SUBS_DATA", str(tmp_path / "subs.json"))
    import subs_ui
    from textual.widgets import DataTable, Input

    class Ctx:
        base, due_days, today = "USD", 7, date.today()
        store = Store(tmp_path / "subs.json")

        def monthly_in_base(self, s):
            return self.store.convert(s.monthly, s.currency, self.base)

    ctx = Ctx()
    ctx.store.subs = [sub("Existing", 5, anchor=date.today())]

    async def drive():
        app = subs_ui.SubsApp(ctx)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("a")
            form = app.screen
            form.query_one("#f-name", Input).value = "Hulu"
            form.query_one("#f-price", Input).value = "8 EUR"
            form.query_one("#f-every", Input).value = "quarterly"
            await pilot.click("#save")
            await pilot.pause()
            assert [s.name for s in ctx.store.subs] == ["Existing", "Hulu"]
            hulu = ctx.store.subs[1]
            assert (hulu.currency, hulu.cycle) == ("EUR", Cycle(3, "month"))
            assert app.current() is hulu  # the cursor follows the new row

            await pilot.press("p")
            assert hulu.status == "paused"

            await pilot.press("a")
            app.screen.query_one("#f-name", Input).value = "existing"
            await pilot.click("#save")
            await pilot.pause()
            assert "already" in str(app.screen.query_one("#form-error").render())
            await pilot.press("escape")

            app.query_one("#table", DataTable).move_cursor(row=0)
            target = app.current()
            await pilot.press("d", "y")
            await pilot.pause()
            assert target not in ctx.store.subs and len(ctx.store.subs) == 1
        return Store(tmp_path / "subs.json")

    saved = asyncio.run(drive())
    assert [s.name for s in saved.subs] == [s.name for s in ctx.store.subs]
