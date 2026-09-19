"""The live view of kit top: processes, network and autostart tabs with details and actions."""

from __future__ import annotations

import os
import webbrowser
from datetime import datetime

import psutil

from kitlib import KIT_HOME, die
from kitlib.clipboard import copy_to_clipboard

try:
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.coordinate import Coordinate
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Input, Static, TabbedContent, TabPane
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

from top_rules import WINDOWS_SYSTEM_NAMES, level_for, total_score
from top_scan import (IS_WINDOWS, AutoRow, Engine, ProcRow, Snapshot, TrustStore, data_path, elevation_hint,
                      file_info, open_folder, virustotal_url)

ACCENT = "#d97757"
TEXT = "#e8e3dc"
MUTED = "#8a847c"
DIM = "#4a4642"
LEVEL_COLORS = {"high": "#e5534b", "medium": "#e0a44a", "low": "#7c9fd4", "ok": "#5f5a54"}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2, "ok": 3}
SORTS = {"score": "risk", "cpu": "CPU", "mem": "memory", "name": "name"}
CRITICAL = set(WINDOWS_SYSTEM_NAMES) | {"system", "registry", "memcompression", "init", "systemd", "kthreadd"}


def badge(level: str, trusted: bool = False) -> Text:
    if trusted:
        return Text("✓ trusted", style="#7fb09b")
    label = {"high": "● HIGH", "medium": "● MED", "low": "● LOW", "ok": "· ok"}[level]
    return Text(label, style=f"bold {LEVEL_COLORS[level]}" if level != "ok" else LEVEL_COLORS[level])


def human_bytes(value: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if unit in ("B", "K") else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}G"


def middle(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width // 2 - 1] + "…" + text[-(width - width // 2):]


def when(timestamp: float | None) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "?"


CSS = f"""
Screen {{ background: #1a1918; color: {TEXT}; }}
#topbar {{ height: 1; padding: 0 1; }}
#brand {{ width: auto; }}
#summary {{ width: 1fr; content-align: right middle; }}
TabbedContent {{ height: 1fr; }}
Tabs {{ background: #1a1918; }}
Tab {{ color: {MUTED}; }}
Tab.-active {{ color: {ACCENT}; text-style: bold; }}
Tab:hover {{ color: {TEXT}; }}
Underline > .underline--bar {{ color: {ACCENT}; background: #2e2b28; }}
TabPane {{ padding: 0; }}
DataTable {{
    background: #1a1918; height: 1fr; scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1;
    scrollbar-background: #1a1918; scrollbar-background-hover: #1a1918; scrollbar-background-active: #1a1918;
    scrollbar-color: #3d3935; scrollbar-color-hover: {MUTED}; scrollbar-color-active: {ACCENT}; scrollbar-corner-color: #1a1918;
}}
DataTable > .datatable--header {{ background: #1a1918; color: {MUTED}; text-style: bold; }}
DataTable > .datatable--cursor {{ background: #3d3935; color: #ffffff; }}
DataTable:focus > .datatable--cursor {{ background: #4a3a32; }}
DataTable > .datatable--hover {{ background: #262422; }}
DataTable > .datatable--even-row, DataTable > .datatable--odd-row {{ background: #1a1918; }}
#filter {{ display: none; border: round #3d3935; background: #1a1918; padding: 0 1; height: 3; }}
#filter.-shown {{ display: block; }}
#filter:focus {{ border: round {ACCENT}; background: #1a1918; }}
#why {{ height: 3; border-top: solid #3d3935; padding: 0 1; color: {MUTED}; }}
Footer {{ background: #1a1918; color: {MUTED}; }}
Footer FooterKey {{ background: #1a1918; }}
Footer FooterKey .footer-key--key {{ background: #1a1918; color: {ACCENT}; text-style: bold; }}
Footer FooterKey .footer-key--description {{ color: {MUTED}; }}
Footer FooterKey:hover {{ background: #262422; }}

ModalScreen {{ align: center middle; background: rgba(0, 0, 0, 0.6); }}
.dialog {{
    width: 100; max-width: 95%; height: auto; max-height: 90%;
    border: round {ACCENT}; border-title-color: {ACCENT}; border-title-style: bold;
    border-subtitle-color: {MUTED}; background: #211f1d; padding: 1 2;
}}
.dialog VerticalScroll {{ height: auto; max-height: 30; }}
.hint {{ color: {MUTED}; margin-top: 1; }}
.buttons {{ width: 100%; height: 3; align: right middle; margin-top: 1; }}
Button {{
    min-width: 8; width: auto; height: 3; margin: 0 0 0 1; padding: 0 1;
    border: round #3d3935; background: #211f1d; color: {TEXT}; text-style: none;
}}
Button:hover {{ background: #211f1d; border: round {MUTED}; color: #ffffff; text-style: none; }}
Button:focus {{ text-style: bold; border: round {MUTED}; }}
Button.danger {{ border: round #e5534b; color: #e5534b; }}
Button.primary {{ border: round {ACCENT}; color: {ACCENT}; }}
"""

HELP = f"""[b]What the levels mean[/b]
[bold {LEVEL_COLORS['high']}]● HIGH[/]  several strong signs, or one very strong one (fake system name, miner, reverse shell)
[bold {LEVEL_COLORS['medium']}]● MED[/]   worth a closer look (temp folder, broken signature, backdoor port…)
[bold {LEVEL_COLORS['low']}]● LOW[/]   a weak sign on its own; common for installers and developer tools
Flagged doesn't mean infected, and ok doesn't mean safe: this is a triage aid, not an antivirus.

[b]Keys[/b]
  enter      details (full path, command line, parents, signature, SHA-256, connections)
  /          filter        f  only flagged        s c m n  sort by risk / CPU / memory / name
  k          kill (asks)   p  suspend / resume    o  open the folder
  y          copy path     h  copy SHA-256        v  look the file up on VirusTotal (by hash only)
  t          trust / untrust: stop flagging this exact file
  r          refresh now (on the Autostart tab: scan again)
  1 2 3      switch tabs   q  quit

[b]If something looks wrong[/b]
  Look the hash up on VirusTotal (v), check who signed it, and where it was started from.
  Windows: run a Microsoft Defender offline scan (Start-MpWDOScan in an admin PowerShell).
  Linux: rkhunter, chkrootkit or ClamAV (clamscan) give a second opinion.
"""


# --- dialogs --------------------------------------------------------------------------------

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape,n", "cancel", "Cancel"), Binding("y", "ok", "Yes")]

    def __init__(self, title: str, body: str, ok_label: str, danger: bool = False) -> None:
        super().__init__()
        self.title_text, self.body, self.ok_label, self.danger = title, body, ok_label, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = f" {self.title_text} "
            yield Static(self.body)
            with Horizontal(classes="buttons"):
                yield Button("Cancel (n)", id="cancel")
                yield Button(f"{self.ok_label} (y)", id="ok", classes="danger" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#ok")
    def action_ok(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class InfoScreen(ModalScreen[None]):
    """Details or help. App keys (k, v, t, ...) still work and act on the item shown."""

    BINDINGS = [
        Binding("escape,enter,question_mark", "close", "Close"),
        # a modal screen doesn't pass keys on to the app, so forward the item actions explicitly
        Binding("k", "app.kill", "Kill"),
        Binding("p", "app.suspend", "Suspend"),
        Binding("o", "app.open_folder", "Folder"),
        Binding("y", "app.copy_path", "Copy path"),
        Binding("h", "app.copy_hash", "Copy hash"),
        Binding("v", "app.virustotal", "VirusTotal"),
        Binding("t", "app.trust", "Trust"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, title: str, body: Text | str, item: object = None) -> None:
        super().__init__()
        self.title_text, self.body, self.item = title, body, item

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = f" {self.title_text} "
            with VerticalScroll():
                yield Static(self.body, id="info-body")
            if self.item is not None:
                yield Static("esc close · k kill · p suspend · o folder · y copy path · h copy hash · "
                             "v VirusTotal · t trust", classes="hint")

    def update_body(self, body: Text) -> None:
        self.body = body
        self.query_one("#info-body", Static).update(body)

    def action_close(self) -> None:
        self.dismiss(None)


# --- app -------------------------------------------------------------------------------------

class TopApp(App):
    TITLE = "kit top"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("slash", "filter", "Filter"),
        Binding("f", "toggle_flagged", "Flagged only"),
        Binding("s", "sort('score')", "Sort", show=False),
        Binding("c", "sort('cpu')", "CPU", show=False),
        Binding("m", "sort('mem')", "Memory", show=False),
        Binding("n", "sort('name')", "Name", show=False),
        Binding("k", "kill", "Kill"),
        Binding("p", "suspend", "Suspend", show=False),
        Binding("o", "open_folder", "Folder", show=False),
        Binding("y", "copy_path", "Copy path", show=False),
        Binding("h", "copy_hash", "Copy hash", show=False),
        Binding("v", "virustotal", "VirusTotal"),
        Binding("t", "trust", "Trust"),
        Binding("r", "refresh_now", "Refresh", show=False),
        Binding("1", "tab('procs')", "Processes", show=False),
        Binding("2", "tab('net')", "Network", show=False),
        Binding("3", "tab('auto')", "Autostart", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape", "Back", show=False),
    ]

    def __init__(self, refresh: float = 2.0, only_flagged: bool = False, show_autostart: bool = True,
                 engine: Engine | None = None) -> None:
        super().__init__()
        self.refresh_every = refresh
        self.only_flagged = only_flagged
        self.show_autostart = show_autostart
        self.engine = engine or Engine(TrustStore(data_path()))
        self.snapshot: Snapshot | None = None
        self.by_pid: dict[int, ProcRow] = {}
        self.auto_rows: list[AutoRow] | None = None
        self.auto_error = ""
        self.sort_key = "score"
        self.filter_text = ""
        self._busy = False
        self._auto_busy = False
        self._net_keys: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(Text.assemble(("✻ ", f"bold {ACCENT}"), ("kit top", f"bold {TEXT}")), id="brand")
            yield Static("", id="summary")
        with TabbedContent(id="tabs", initial="procs"):
            with TabPane("Processes", id="procs"):
                yield Input(placeholder="filter by name, path, PID, user or reason - enter to keep, esc to clear",
                            id="filter")
                yield DataTable(id="proc-table", cursor_type="row", zebra_stripes=False)
            with TabPane("Network", id="net"):
                yield DataTable(id="net-table", cursor_type="row")
            with TabPane("Autostart", id="auto"):
                yield DataTable(id="auto-table", cursor_type="row")
        yield Static("", id="why")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#proc-table", DataTable).add_columns(
            "Risk", "PID", "Name", "CPU%", "Memory", "User", "Net", "Why", "Path")
        self.query_one("#net-table", DataTable).add_columns(
            "Risk", "PID", "Name", "Proto", "Local", "Remote", "State", "Note")
        self.query_one("#auto-table", DataTable).add_columns("Risk", "Kind", "Name", "Command", "Location", "Why")
        self.engine.prime()
        self.update_summary()
        self.request_snapshot()
        self.set_interval(self.refresh_every, self.request_snapshot)
        if self.show_autostart:
            self.scan_autostart()
        else:
            self.query_one("#auto-table", DataTable).add_row("", "", "autostart check is off (--no-autostart)")
        self.query_one("#proc-table", DataTable).focus()

    # --- data ---

    def request_snapshot(self) -> None:
        if not self._busy:
            self._busy = True
            self.take_snapshot()

    @work(thread=True, group="snapshot")
    def take_snapshot(self) -> None:
        try:
            snapshot = self.engine.snapshot()
        except Exception as exc:  # keep the view alive; show the problem
            self.call_from_thread(self.notify, f"Couldn't read processes: {exc}", severity="error")
            snapshot = None
        self.call_from_thread(self.apply_snapshot, snapshot)

    def apply_snapshot(self, snapshot: Snapshot | None) -> None:
        self._busy = False
        if snapshot is None:
            return
        self.snapshot = snapshot
        self.by_pid = {row.pid: row for row in snapshot.rows}
        self.fill_processes()
        self.fill_network()
        self.update_summary()
        self.update_why()
        if isinstance(self.screen, InfoScreen) and isinstance(self.screen.item, ProcRow):
            fresh = self.by_pid.get(self.screen.item.pid)
            if fresh is not None and fresh.started == self.screen.item.started:
                self.screen.item = fresh

    @work(thread=True, group="autostart")
    def scan_autostart(self) -> None:
        self._auto_busy = True
        self.call_from_thread(self.fill_autostart)
        try:
            rows, error = self.engine.autostart_parallel(), ""
        except Exception as exc:
            rows, error = [], str(exc)
        self.call_from_thread(self.apply_autostart, rows, error)

    def apply_autostart(self, rows: list[AutoRow], error: str) -> None:
        self._auto_busy = False
        self.auto_rows = sorted(rows, key=lambda r: (LEVEL_ORDER[r.level], -r.score, r.entry.kind, r.entry.name.lower()))
        self.auto_error = error
        self.fill_autostart()
        self.update_summary()

    # --- tables ---

    def visible_processes(self) -> list[ProcRow]:
        rows = self.snapshot.rows if self.snapshot else []
        if self.only_flagged:
            rows = [r for r in rows if r.level != "ok"]
        if self.filter_text:
            needle = self.filter_text.lower()
            rows = [r for r in rows if needle in " ".join(
                (str(r.pid), r.name, r.exe or "", r.username or "", r.why, " ".join(r.cmdline))).lower()]
        if self.sort_key == "cpu":
            return sorted(rows, key=lambda r: (-r.cpu, LEVEL_ORDER[r.level]))
        if self.sort_key == "mem":
            return sorted(rows, key=lambda r: -r.memory)
        if self.sort_key == "name":
            return sorted(rows, key=lambda r: (r.name.lower(), r.pid))
        return sorted(rows, key=lambda r: (LEVEL_ORDER[r.level], -r.score, -r.cpu, r.name.lower()))

    @staticmethod
    def _refill(table: DataTable, rows: list[tuple[str, tuple]]) -> None:
        """Replaces the rows, keeping the cursor on the same item and the scroll position."""
        current = None
        if table.row_count and 0 <= table.cursor_row < table.row_count:
            try:
                current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
            except Exception:
                current = None
        old_row, scroll_x, scroll_y = table.cursor_row, table.scroll_x, table.scroll_y
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
        target = old_row
        if current is not None:
            try:
                target = table.get_row_index(current)
            except Exception:
                target = old_row
        if table.row_count:
            table.move_cursor(row=min(max(target, 0), table.row_count - 1), scroll=False)
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False)
            # Column widths are only measured on the next refresh, so until then the table is too narrow
            # to scroll sideways and the x above gets clamped to 0: set it again once they're known.
            table.call_after_refresh(table.scroll_to, x=scroll_x, y=scroll_y, animate=False)

    def fill_processes(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        rows = []
        for r in self.visible_processes():
            name = Text(middle(r.name, 32), style=f"bold {TEXT}" if r.level in ("high", "medium") else TEXT)
            if r.suspended:
                name.append(" (suspended)", style=MUTED)
            why = ""
            if r.flags and not r.trusted:
                why = middle(r.flags[0].reason, 50) + (f" (+{len(r.flags) - 1})" if len(r.flags) > 1 else "")
            net = len(r.conns)
            rows.append((str(r.pid), (
                badge(r.level, r.trusted),
                Text(str(r.pid), style=MUTED, justify="right"),
                name,
                Text(f"{r.cpu:.1f}", justify="right", style=ACCENT if r.cpu >= 25 else TEXT),
                Text(human_bytes(r.memory), justify="right"),
                Text(middle(r.user_short, 16), style=MUTED),
                Text(str(net) if net else "", justify="right"),
                Text(why, style=LEVEL_COLORS[r.level] if r.level != "ok" else DIM),
                Text(middle(r.exe or ("(access denied)" if r.denied else ""), 60), style=MUTED),
            )))
        self._refill(table, rows)

    def fill_network(self) -> None:
        table = self.query_one("#net-table", DataTable)
        rows, self._net_keys = [], {}
        procs = sorted(self.by_pid.values(), key=lambda r: (LEVEL_ORDER[r.level], -r.score, r.name.lower()))
        for r in procs:
            for index, c in enumerate(r.conns):
                if not c.remote and c.status != "LISTEN":
                    continue
                note = ""
                for flag in r.flags:
                    if c.remote_port and f":{c.remote_port} " in flag.reason:
                        note = flag.reason
                if not note and c.status == "LISTEN":
                    note = "accepting connections"
                key = f"{r.pid}:{index}"
                self._net_keys[key] = r.pid
                rows.append((key, (
                    badge(r.level, r.trusted), Text(str(r.pid), style=MUTED, justify="right"), r.name,
                    c.kind, c.local, Text(c.remote, style=LEVEL_COLORS["high"] if "port often" in note else TEXT),
                    Text(c.status.lower(), style=MUTED), Text(note, style=MUTED),
                )))
        self._refill(table, rows)
        self.query_one("#tabs", TabbedContent).get_tab("net").label = f"Network ({len(rows)})"

    def fill_autostart(self) -> None:
        table = self.query_one("#auto-table", DataTable)
        tab = self.query_one("#tabs", TabbedContent).get_tab("auto")
        if self.auto_rows is None:
            table.clear()
            table.add_row(Text("…", style=MUTED), "", Text("checking autostart entries (this takes a few seconds)", style=MUTED))
            tab.label = "Autostart (…)"
            return
        rows = []
        if self.auto_error:
            rows.append(("error", (Text("error", style=LEVEL_COLORS["high"]), "", self.auto_error, "", "", "")))
        for index, r in enumerate(self.auto_rows):
            e = r.entry
            name = Text(middle(e.name, 36))
            if e.disabled:
                name.append(" (off)", style=MUTED)
            why = middle(r.flags[0].reason, 50) + (f" (+{len(r.flags) - 1})" if len(r.flags) > 1 else "") if r.flags and not r.trusted else ""
            rows.append((str(index), (
                badge(r.level, r.trusted), Text(e.kind, style=MUTED), name, Text(middle(e.command, 60)),
                Text(middle(e.location, 40), style=MUTED), Text(why, style=LEVEL_COLORS[r.level] if r.level != "ok" else DIM),
            )))
        self._refill(table, rows)
        flagged = sum(1 for r in self.auto_rows if r.level != "ok")
        tab.label = f"Autostart ({flagged} flagged)" if flagged else f"Autostart ({len(self.auto_rows)})"
        if self._auto_busy:
            tab.label = "Autostart (…)"

    def update_summary(self) -> None:
        summary = Text()
        if self.snapshot is None:
            summary.append("reading processes…", style=MUTED)
        else:
            counts = self.snapshot.counts()
            summary.append(f"{len(self.snapshot.rows)} processes", style=TEXT)
            for level in ("high", "medium", "low"):
                summary.append("  ·  ", style=DIM)
                summary.append(f"{counts[level]} {level}", style=LEVEL_COLORS[level] if counts[level] else MUTED)
            if self.snapshot.denied:
                summary.append("  ·  ", style=DIM)
                summary.append(f"{self.snapshot.denied} not inspectable ({elevation_hint()})", style=LEVEL_COLORS["medium"])
            if self.snapshot.signatures_pending:
                summary.append("  ·  ", style=DIM)
                summary.append(f"checking {self.snapshot.signatures_pending} signatures", style=MUTED)
        summary.append("  ·  ", style=DIM)
        summary.append(f"sort: {SORTS[self.sort_key]}" + ("  ·  flagged only" if self.only_flagged else ""), style=MUTED)
        if self.filter_text:
            summary.append(f"  ·  filter: {self.filter_text}", style=ACCENT)
        self.query_one("#summary", Static).update(summary)

    def update_why(self) -> None:
        item = self.current_item(from_table=True)
        text = Text()
        if isinstance(item, ProcRow):
            text.append(f"{item.name} ", style=f"bold {TEXT}")
            text.append(f"pid {item.pid}", style=MUTED)
            if item.exe:
                text.append(f"  {item.exe}", style=MUTED)
            text.append("\n")
            flags, level = item.flags, item.level
        elif isinstance(item, AutoRow):
            text.append(f"{item.entry.kind}: {item.entry.name}", style=f"bold {TEXT}")
            text.append(f"  {item.entry.location}\n", style=MUTED)
            flags, level = item.flags, item.level
        else:
            self.query_one("#why", Static).update("")
            return
        if getattr(item, "trusted", False):
            text.append("trusted by you - ", style="#7fb09b")
        if flags:
            text.append("; ".join(f.reason for f in flags), style=LEVEL_COLORS[level] if level != "ok" else MUTED)
        else:
            text.append("nothing unusual found", style=MUTED)
        self.query_one("#why", Static).update(text)

    @on(DataTable.RowHighlighted)
    def row_highlighted(self) -> None:
        self.update_why()

    @on(DataTable.RowSelected)
    def row_selected(self) -> None:
        self.action_details()

    @on(TabbedContent.TabActivated)
    def tab_changed(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        table_id = {"procs": "#proc-table", "net": "#net-table", "auto": "#auto-table"}.get(active)
        if table_id:
            self.query_one(table_id, DataTable).focus()
        self.update_why()

    # --- selection ---

    def current_item(self, from_table: bool = False) -> ProcRow | AutoRow | None:
        if not from_table and isinstance(self.screen, InfoScreen) and self.screen.item is not None:
            return self.screen.item
        active = self.query_one("#tabs", TabbedContent).active
        table = self.query_one({"procs": "#proc-table", "net": "#net-table", "auto": "#auto-table"}[active], DataTable)
        if not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
        except Exception:
            return None
        if active == "procs":
            return self.by_pid.get(int(key)) if key and key.isdigit() else None
        if active == "net":
            pid = self._net_keys.get(key or "")
            return self.by_pid.get(pid) if pid is not None else None
        if self.auto_rows is not None and key and key.isdigit() and int(key) < len(self.auto_rows):
            return self.auto_rows[int(key)]
        return None

    @staticmethod
    def item_path(item: ProcRow | AutoRow | None) -> str:
        if isinstance(item, ProcRow):
            return item.exe or ""
        if isinstance(item, AutoRow):
            return item.entry.target if item.entry.target and os.path.isabs(item.entry.target) else ""
        return ""

    def need_process(self) -> ProcRow | None:
        item = self.current_item()
        if not isinstance(item, ProcRow):
            self.notify("Pick a process first (this works on the Processes and Network tabs).", severity="warning")
            return None
        return item

    def need_file(self) -> str:
        path = self.item_path(self.current_item())
        if not path:
            self.notify("No program file is known for this entry.", severity="warning")
        elif not os.path.exists(path):
            self.notify(f"The file no longer exists: {path}", severity="warning")
            return ""
        return path

    # --- actions: view ---

    def action_tab(self, pane: str) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.query_one("#tabs", TabbedContent).active = pane

    def action_filter(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.query_one("#tabs", TabbedContent).active = "procs"
        box = self.query_one("#filter", Input)
        box.add_class("-shown")
        box.focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self.fill_processes()
        self.update_summary()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        box = self.query_one("#filter", Input)
        if not box.value.strip():
            box.remove_class("-shown")
        self.query_one("#proc-table", DataTable).focus()

    def action_escape(self) -> None:
        box = self.query_one("#filter", Input)
        if box.has_class("-shown"):
            box.value = ""
            box.remove_class("-shown")
            self.query_one("#proc-table", DataTable).focus()

    def action_toggle_flagged(self) -> None:
        self.only_flagged = not self.only_flagged
        self.fill_processes()
        self.update_summary()
        self.update_why()

    def action_sort(self, key: str) -> None:
        self.sort_key = key
        self.fill_processes()
        self.update_summary()
        self.update_why()

    def action_refresh_now(self) -> None:
        if self.query_one("#tabs", TabbedContent).active == "auto" and self.show_autostart:
            if not self._auto_busy:
                self.scan_autostart()
                self.notify("Checking autostart entries again…", timeout=3)
            return
        self.request_snapshot()

    def action_help(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.push_screen(InfoScreen("kit top - help", HELP))

    def action_details(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        item = self.current_item()
        if item is None:
            return
        path = self.item_path(item)
        sha = self.engine.files.cached_sha256(path) if path else None
        title = f"{item.name} (PID {item.pid})" if isinstance(item, ProcRow) else f"{item.entry.kind}: {item.entry.name}"
        screen = InfoScreen(middle(title, 80), self.detail_text(item, sha, hashing=bool(path and not sha)), item)
        self.push_screen(screen)
        if path and not sha:
            self.hash_for_details(screen, item, path)

    @work(thread=True, group="hash")
    def hash_for_details(self, screen: InfoScreen, item: ProcRow | AutoRow, path: str) -> None:
        sha = self.engine.files.sha256(path)
        if screen.is_attached:
            self.call_from_thread(screen.update_body, self.detail_text(item, sha or "(couldn't read the file)"))

    def detail_text(self, item: ProcRow | AutoRow, sha: str | None, hashing: bool = False) -> Text:
        text = Text()

        def field(label: str, value: str, style: str = TEXT) -> None:
            text.append(f"{label:<12}", style=MUTED)
            text.append(f"{value}\n", style=style)

        if isinstance(item, ProcRow):
            level, flags, trusted = item.level, item.flags, item.trusted
            text.append_text(badge(level, trusted))
            text.append(f"   score {item.score}\n\n", style=MUTED)
            field("program", item.exe or ("(access denied - " + elevation_hint() + ")" if item.denied else "(unknown)"))
            field("command", " ".join(item.cmdline) or "(not readable)")
            chain = self.engine.parent_chain(item, self.snapshot.rows if self.snapshot else [])
            field("parents", " ← ".join(f"{name} ({pid})" for pid, name in chain) or "(none)")
            field("started", when(item.started))
            field("user", item.username or "(unknown)")
            field("CPU / mem", f"{item.cpu:.1f}% of all CPUs · {human_bytes(item.memory)}")
            if item.suspended:
                field("state", "suspended by you (p to resume)", LEVEL_COLORS["medium"])
            path = item.exe or ""
        else:
            e = item.entry
            level, flags, trusted = item.level, item.flags, item.trusted
            text.append_text(badge(level, trusted))
            text.append(f"   score {item.score}\n\n", style=MUTED)
            field("kind", e.kind + (" (disabled)" if e.disabled else ""))
            field("name", e.name)
            field("command", e.command)
            field("found in", e.location)
            field("program", e.target or "(unknown)")
            if e.target_state == "missing":
                field("", "the program file doesn't exist", LEVEL_COLORS["medium"])
            path = e.target if e.target and os.path.isabs(e.target) else ""

        if path:
            info = file_info(path)
            if info.exists:
                label = "created" if IS_WINDOWS else "changed"
                field("file", f"{human_bytes(info.size)} · {label} {when(info.created)} · modified {when(info.modified)}"
                              + (" · hidden" if info.hidden else ""))
                if IS_WINDOWS:
                    sig = self.engine.files.signature(path, info, wait=False)
                    sig_style = {"trusted": "#7fb09b", "invalid": LEVEL_COLORS["high"],
                                 "untrusted": LEVEL_COLORS["medium"]}.get(sig.status if sig else "", TEXT)
                    field("signature", sig.label if sig else "checking…", sig_style)
                field("SHA-256", sha or ("computing…" if hashing else "?"))
            else:
                field("file", "missing" if not item_is_deleted(item) else "deleted while running", LEVEL_COLORS["high"])

        text.append("\nWhy it's flagged\n", style=f"bold {TEXT}")
        if trusted:
            text.append("  you marked this exact file as trusted (t to undo)\n", style="#7fb09b")
        for flag in flags:
            text.append(f"  +{flag.weight:<3}", style=MUTED)
            text.append(f"{flag.reason}\n", style=LEVEL_COLORS[level_for(flag.weight * 2)] if flag.weight >= 5 else TEXT)
        if not flags:
            text.append("  nothing unusual found\n", style=MUTED)

        if isinstance(item, ProcRow):
            text.append("\nConnections\n", style=f"bold {TEXT}")
            for c in item.conns[:40]:
                text.append(f"  {c.kind:<4} {c.local:<28} {c.remote or '-':<42} {c.status.lower()}\n", style=MUTED)
            if len(item.conns) > 40:
                text.append(f"  … and {len(item.conns) - 40} more\n", style=MUTED)
            if not item.conns:
                text.append("  none\n", style=MUTED)
        return text

    # --- actions: process ---

    def action_kill(self) -> None:
        row = self.need_process()
        if row is None:
            return
        if row.pid in (os.getpid(), os.getppid()):
            self.notify("That's kit top itself (or the kit launcher). Press q to quit instead.", severity="warning")
            return
        warning = ""
        if row.name.lower() in CRITICAL or row.pid in (0, 1, 4):
            warning = "\n\nThis looks like a core system process: killing it can crash or log out the computer."
        body = (f"Kill {row.name} (PID {row.pid})?\n{row.exe or ''}\n\n"
                f"It stops immediately; unsaved work in it is lost.{warning}")

        def done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                self.engine.process(row).kill()
            except psutil.NoSuchProcess:
                self.notify("It had already exited.")
            except psutil.AccessDenied:
                self.notify(f"Access denied - {elevation_hint()}.", severity="error")
            except OSError as exc:
                self.notify(f"Couldn't kill it: {exc}", severity="error")
            else:
                self.notify(f"Killed {row.name} ({row.pid}).")
                if isinstance(self.screen, InfoScreen):
                    self.screen.dismiss(None)
            self.request_snapshot()

        self.push_screen(ConfirmScreen("Kill process", body, "Kill", danger=True), done)

    def action_suspend(self) -> None:
        row = self.need_process()
        if row is None:
            return
        if row.pid in (os.getpid(), os.getppid()):
            self.notify("Suspending kit top itself would freeze this window.", severity="warning")
            return

        def apply(confirmed: bool | None = True) -> None:
            if not confirmed:
                return
            try:
                proc = self.engine.process(row)
                if row.suspended:
                    proc.resume()
                else:
                    proc.suspend()
            except psutil.NoSuchProcess:
                self.notify("It has already exited.")
            except psutil.AccessDenied:
                self.notify(f"Access denied - {elevation_hint()}.", severity="error")
            except OSError as exc:
                self.notify(f"Couldn't do that: {exc}", severity="error")
            else:
                self.engine.set_suspended(row, not row.suspended)
                self.notify(f"{'Resumed' if row.suspended else 'Suspended'} {row.name} ({row.pid})."
                            + ("" if row.suspended else " Press p again to resume it."))
            self.request_snapshot()

        if not row.suspended and (row.name.lower() in CRITICAL or row.pid in (0, 1, 4)):
            self.push_screen(ConfirmScreen("Suspend process", f"Suspend {row.name} ({row.pid})?\n\nThis is a core system "
                                           "process: pausing it can freeze the computer.", "Suspend", danger=True), apply)
        else:
            apply()

    def action_open_folder(self) -> None:
        path = self.need_file()
        if path:
            try:
                open_folder(path)
            except OSError as exc:
                self.notify(f"Couldn't open the folder: {exc}", severity="error")

    def copy(self, text: str, what: str) -> None:
        if copy_to_clipboard(text):
            self.notify(f"Copied {what}.", timeout=3)
        else:
            self.copy_to_clipboard(text)  # terminal clipboard (OSC 52) as a fallback
            self.notify(f"Copied {what} through the terminal (if it supports that).", timeout=4)

    def action_copy_path(self) -> None:
        path = self.item_path(self.current_item())
        if path:
            self.copy(path, "the path")
        else:
            self.notify("No program file is known for this entry.", severity="warning")

    def action_copy_hash(self) -> None:
        path = self.need_file()
        if path:
            self.with_hash(path, "copy")

    def action_virustotal(self) -> None:
        path = self.need_file()
        if path:
            self.with_hash(path, "virustotal")

    @work(thread=True, group="hash")
    def with_hash(self, path: str, then: str) -> None:
        sha = self.engine.files.cached_sha256(path)
        if sha is None:
            self.call_from_thread(self.notify, "Computing SHA-256…", timeout=2)
            sha = self.engine.files.sha256(path)
        if not sha:
            self.call_from_thread(self.notify, "Couldn't read the file to hash it.", severity="error")
        elif then == "copy":
            self.call_from_thread(self.copy, sha, "the SHA-256")
        elif then == "virustotal":
            webbrowser.open(virustotal_url(sha))
            self.call_from_thread(self.notify, "Opened VirusTotal in your browser (only the hash was sent, not the file).",
                                  timeout=5)
        elif then == "trust":
            self.call_from_thread(self.confirm_trust, path, sha)

    def action_trust(self) -> None:
        item = self.current_item()
        path = self.item_path(item)
        if not path:
            self.notify("No program file is known for this entry.", severity="warning")
            return
        if self.engine.trust.get(path):
            self.engine.trust.remove(path)
            self.notify(f"No longer trusted: {path}")
            self.after_trust_change()
            return
        if not os.path.exists(path):
            self.notify(f"The file no longer exists: {path}", severity="warning")
            return
        self.with_hash(path, "trust")

    def confirm_trust(self, path: str, sha: str) -> None:
        body = (f"Trust this file?\n{path}\n\nSHA-256 {sha}\n\n"
                "It won't be flagged again while the file stays exactly the same. If it changes (an update, "
                "or tampering), it is flagged again. Press t again to undo.")

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.engine.trust.add(path, sha)
                self.notify(f"Trusted: {path}")
                self.after_trust_change()

        self.push_screen(ConfirmScreen("Trust program", body, "Trust"), done)

    def after_trust_change(self) -> None:
        if self.auto_rows:
            for row in self.auto_rows:
                target = row.entry.target
                entry = self.engine.trust.get(target) if target else None
                row.trusted = bool(entry and row.flags and self.engine.files.cached_sha256(target) == entry["sha256"])
                row.score = 0 if row.trusted else total_score(row.flags)
                row.level = "ok" if row.trusted else level_for(row.score)
            self.fill_autostart()
        if isinstance(self.screen, InfoScreen):
            self.screen.dismiss(None)
        self.request_snapshot()


def item_is_deleted(item: object) -> bool:
    return isinstance(item, ProcRow) and any("deleted" in f.reason for f in item.flags)
