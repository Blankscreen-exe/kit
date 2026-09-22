"""The live view of kit port: what's listening, refreshed automatically."""
from __future__ import annotations

import os

import psutil

from kitlib import KIT_HOME, die

try:
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.coordinate import Coordinate
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Static
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

from port_scan import ALL_INTERFACES, CAUTION, IS_WINDOWS, connections, kit_share_map, process_details, protocol

ACCENT = "#d97757"
TEXT = "#e8e3dc"
MUTED = "#8a847c"
DIM = "#4a4642"
WARN = "#e0a44a"
SHARED_COLOR = "#7fb09b"

CSS = f"""
Screen {{ background: #1a1918; color: {TEXT}; }}
#topbar {{ height: 1; padding: 0 1; }}
#brand {{ width: auto; }}
#summary {{ width: 1fr; content-align: right middle; }}
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
Footer {{ background: #1a1918; color: {MUTED}; }}
Footer FooterKey {{ background: #1a1918; }}
Footer FooterKey .footer-key--key {{ background: #1a1918; color: {ACCENT}; text-style: bold; }}
Footer FooterKey .footer-key--description {{ color: {MUTED}; }}
Footer FooterKey:hover {{ background: #262422; }}

ModalScreen {{ align: center middle; background: rgba(0, 0, 0, 0.6); }}
.dialog {{
    width: 90; max-width: 95%; height: auto; max-height: 90%;
    border: round {ACCENT}; border-title-color: {ACCENT}; border-title-style: bold;
    border-subtitle-color: {MUTED}; background: #211f1d; padding: 1 2;
}}
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


def short_addresses(addresses: list[str]) -> str:
    text = ", ".join(addresses[:2])
    return f"{text} +{len(addresses) - 2}" if len(addresses) > 2 else text


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape,n", "cancel", "Cancel"), Binding("y", "ok", "Yes")]

    def __init__(self, title: str, body: str, ok_label: str) -> None:
        super().__init__()
        self.title_text, self.body, self.ok_label = title, body, ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = f" {self.title_text} "
            yield Static(self.body)
            with Horizontal(classes="buttons"):
                yield Button("Cancel (n)", id="cancel")
                yield Button(f"{self.ok_label} (y)", id="ok", classes="danger")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PortApp(App):
    TITLE = "kit port"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("u", "toggle_udp", "UDP"),
        Binding("k", "kill", "Stop"),
        Binding("r", "refresh_now", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, refresh: float = 2.0, include_udp: bool = False) -> None:
        super().__init__()
        self.refresh_every = refresh
        self.include_udp = include_udp
        self.rows: list[dict] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(Text.assemble(("● ", f"bold {ACCENT}"), ("kit port", f"bold {TEXT}")), id="brand")
            yield Static("", id="summary")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#table", DataTable).add_columns("Proto", "Port", "Address", "PID", "Process", "Shared", "Command")
        self.request_snapshot()
        self.set_interval(self.refresh_every, self.request_snapshot)
        self.query_one("#table", DataTable).focus()

    # --- data ---

    def request_snapshot(self) -> None:
        if not self._busy:
            self._busy = True
            self.take_snapshot()

    @work(thread=True, group="snapshot")
    def take_snapshot(self) -> None:
        rows: list[dict] | None
        try:
            conns = [c for c in connections("tcp") if c.status == psutil.CONN_LISTEN]
            if self.include_udp:
                conns += [c for c in connections("udp") if c.laddr]
            grouped: dict[tuple, list[str]] = {}
            for c in conns:
                key = (c.laddr.port, protocol(c), c.pid)
                ips = grouped.setdefault(key, [])
                if c.laddr.ip not in ips:
                    ips.append(c.laddr.ip)
            cache: dict[int, tuple[str, str]] = {}
            shared = kit_share_map()
            rows = []
            for (port, proto, pid), ips in sorted(grouped.items(), key=lambda i: (i[0][0], i[0][1], i[0][2] or 0)):
                name, command = process_details(pid, cache)
                rows.append({
                    "key": f"{port}:{proto}:{pid}", "port": port, "proto": proto, "pid": pid, "ips": ips,
                    "name": name, "command": command, "exposed": any(ip in ALL_INTERFACES for ip in ips),
                    "shared": shared.get(pid, ""),
                })
        except psutil.AccessDenied:
            self.call_from_thread(self.notify, "Not allowed to read all connections - run with sudo to see everything.",
                                  severity="warning")
            rows = None
        except Exception as exc:  # keep the view alive; show the problem
            self.call_from_thread(self.notify, f"Couldn't read ports: {exc}", severity="error")
            rows = None
        self.call_from_thread(self.apply_snapshot, rows)

    def apply_snapshot(self, rows: list[dict] | None) -> None:
        self._busy = False
        if rows is None:
            return
        self.rows = rows
        self.fill_table()
        self.update_summary()

    # --- table ---

    def fill_table(self) -> None:
        """Replaces the rows, keeping the cursor on the same item and the scroll position."""
        table = self.query_one("#table", DataTable)
        current = None
        if table.row_count and 0 <= table.cursor_row < table.row_count:
            try:
                current = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
            except Exception:
                current = None
        old_row, scroll_x, scroll_y = table.cursor_row, table.scroll_x, table.scroll_y
        table.clear()
        for r in self.rows:
            shared = Text(f"kit: {r['shared']}", style=SHARED_COLOR) if r["shared"] else Text("-", style=DIM)
            table.add_row(
                Text(r["proto"], style=MUTED),
                Text(str(r["port"]), style=f"bold {ACCENT}"),
                Text(short_addresses(r["ips"]), style=f"bold {WARN}" if r["exposed"] else TEXT),
                Text(str(r["pid"] or "-"), style=MUTED, justify="right"),
                Text(r["name"], style=TEXT),
                shared,
                Text(r["command"][:70], style=MUTED),
                key=r["key"],
            )
        target = old_row
        if current is not None:
            try:
                target = table.get_row_index(current)
            except Exception:
                target = old_row
        if table.row_count:
            table.move_cursor(row=min(max(target, 0), table.row_count - 1), scroll=False)
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False)
            table.call_after_refresh(table.scroll_to, x=scroll_x, y=scroll_y, animate=False)

    def update_summary(self) -> None:
        exposed = sum(1 for r in self.rows if r["exposed"])
        text = Text(f"{len(self.rows)} listening", style=TEXT)
        if exposed:
            text.append(f"  ·  {exposed} reachable from other machines", style=f"bold {WARN}")
        if self.include_udp:
            text.append("  ·  udp on", style=MUTED)
        self.query_one("#summary", Static).update(text)

    def current_row(self) -> dict | None:
        table = self.query_one("#table", DataTable)
        if not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
        except Exception:
            return None
        for r in self.rows:
            if r["key"] == key:
                return r
        return None

    # --- actions ---

    def action_toggle_udp(self) -> None:
        self.include_udp = not self.include_udp
        self.request_snapshot()

    def action_refresh_now(self) -> None:
        self.request_snapshot()

    def action_kill(self) -> None:
        row = self.current_row()
        if row is None or not row["pid"]:
            self.notify("Pick a listening process first.", severity="warning")
            return
        pid = row["pid"]
        if pid in (os.getpid(), os.getppid()):
            self.notify("That's kit port itself (or the kit launcher). Press q to quit instead.", severity="warning")
            return
        warning = ""
        if row["name"].lower().removesuffix(".exe") in CAUTION:
            warning = "\n\nThis looks like part of Docker, WSL, a database or the OS - stopping it may break more than this port."
        if row["shared"]:
            warning += f"\n\nThis is shared as '{row['shared']}' via kit - kit share stop {row['shared']} keeps that in sync."
        body = f"Stop {row['name']} (PID {pid}) listening on {row['proto']} {row['port']}?{warning}"

        def done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                process = psutil.Process(pid)
                process.terminate()
                gone, _ = psutil.wait_procs([process], timeout=3)
                if gone:
                    self.notify(f"Stopped PID {pid}.")
                else:
                    self.notify(f"PID {pid} is still running - try again from a terminal with --force if it won't stop.",
                                severity="warning")
            except psutil.NoSuchProcess:
                self.notify("It had already exited.")
            except psutil.AccessDenied:
                where = "an elevated (Run as administrator) terminal" if IS_WINDOWS else "sudo"
                self.notify(f"Access denied - try again from {where}.", severity="error")
            self.request_snapshot()

        self.push_screen(ConfirmScreen("Stop process", body, "Stop"), done)


def run_live(refresh: float, include_udp: bool) -> int:
    PortApp(refresh=refresh, include_udp=include_udp).run()
    return 0
