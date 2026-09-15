"""See what's using network ports, stop it, and find free ports."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys

from kitlib import KIT_HOME, die, error, style, warn

try:
    import psutil
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

IS_WINDOWS = os.name == "nt"
ALL_INTERFACES = ("0.0.0.0", "::")
# Stopping these usually takes down something bigger (Docker, WSL, databases, the OS itself).
CAUTION = {
    "system", "svchost", "lsass", "services", "wininit", "com.docker.backend", "com.docker.proxy",
    "wslrelay", "vpnkit", "dockerd", "docker-proxy", "containerd", "sshd", "systemd", "launchd", "postgres", "mysqld",
}


# --- data --------------------------------------------------------------------------

def connections(kind: str) -> list:
    try:
        return psutil.net_connections(kind=kind)
    except psutil.AccessDenied:
        die("not allowed to read network connections - run with sudo")


def process_details(pid: int | None, cache: dict[int, tuple[str, str]]) -> tuple[str, str]:
    if not pid:
        return "-", ""
    if pid not in cache:
        try:
            process = psutil.Process(pid)
            name = process.name()
            try:
                command = " ".join(process.cmdline())
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                command = ""
        except psutil.NoSuchProcess:
            name, command = "(exited)", ""
        except psutil.AccessDenied:
            name, command = "(access denied)", ""
        cache[pid] = (name, command)
    return cache[pid]


def protocol(conn) -> str:
    return "tcp" if conn.type == socket.SOCK_STREAM else "udp"


def address(addr) -> str:
    if not addr:
        return "-"
    return f"[{addr.ip}]:{addr.port}" if ":" in addr.ip else f"{addr.ip}:{addr.port}"


def note_hidden_processes(conns: list) -> None:
    if not IS_WINDOWS and any(c.pid is None for c in conns) and hasattr(os, "geteuid") and os.geteuid() != 0:
        warn("some sockets belong to other users, so their process isn't shown - run with sudo to see everything")


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        die("can't ask for confirmation in a non-interactive shell - add --yes")
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print()
        die("no answer (input was closed) - add --yes to skip the question")


# --- output ------------------------------------------------------------------------

Cell = str | tuple[str, tuple[str, ...]]


def print_table(headers: list[str], rows: list[list[Cell]]) -> None:
    """Aligned columns; the last column is shortened to fit the terminal."""
    def text(cell: Cell) -> str:
        return cell if isinstance(cell, str) else cell[0]

    widths = [max(len(h), *(len(text(r[i])) for r in rows)) for i, h in enumerate(headers)]
    fixed = sum(widths[:-1]) + 2 * (len(headers) - 1) + 2
    last_width = max(12, shutil.get_terminal_size((120, 30)).columns - fixed - 1)

    print("  " + "  ".join(style(h.ljust(widths[i]) if i < len(headers) - 1 else h, "bold") for i, h in enumerate(headers)))
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            value, styles = (cell, ()) if isinstance(cell, str) else cell
            if i == len(row) - 1:
                value = value if len(value) <= last_width else value[: last_width - 3] + "..."
            else:
                value = value.ljust(widths[i])
            cells.append(style(value, *styles) if styles else value)
        print(("  " + "  ".join(cells)).rstrip())


def short_addresses(addresses: list[str]) -> str:
    """At most two addresses, then a count, so one busy socket doesn't blow up the table."""
    text = ", ".join(addresses[:2])
    return f"{text} +{len(addresses) - 2}" if len(addresses) > 2 else text


# --- commands ----------------------------------------------------------------------

def cmd_list(include_udp: bool) -> int:
    conns = [c for c in connections("tcp") if c.status == psutil.CONN_LISTEN]
    if include_udp:
        conns += [c for c in connections("udp") if c.laddr]

    grouped: dict[tuple, list[str]] = {}
    for conn in conns:
        addresses = grouped.setdefault((conn.laddr.port, protocol(conn), conn.pid), [])
        if conn.laddr.ip not in addresses:
            addresses.append(conn.laddr.ip)

    if not grouped:
        print("nothing is listening")
        note_hidden_processes(conns)
        return 0

    cache: dict[int, tuple[str, str]] = {}
    rows = []
    for (port, proto, pid), addresses in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or 0)):
        name, command = process_details(pid, cache)
        exposed = any(ip in ALL_INTERFACES for ip in addresses)
        rows.append([
            (proto, ("dim",)),
            (str(port), ("bold", "green")),
            (short_addresses(addresses), ("yellow",) if exposed else ()),
            str(pid or "-"),
            (name, ("cyan",)),
            (command, ("dim",)),
        ])
    print_table(["PROTO", "PORT", "ADDRESS", "PID", "PROCESS", "COMMAND"], rows)
    print()
    print(style("0.0.0.0 / :: = reachable from other machines; 127.0.0.1 / ::1 = this machine only", "dim"))
    print(style("details: kit port <port>    stop it: kit port <port> --kill    free port: kit port free", "dim"))
    note_hidden_processes(conns)
    return 0


def owners(port: int, conns: list) -> list[int]:
    """PIDs that own the port: listening TCP sockets and bound UDP sockets, not clients connecting to it."""
    return sorted({
        c.pid for c in conns
        if c.pid and c.laddr and c.laddr.port == port
        and (c.status == psutil.CONN_LISTEN or c.type == socket.SOCK_DGRAM)
    })


def cmd_port(port: int, kill: bool, force: bool, assume_yes: bool) -> int:
    conns = connections("inet")
    matched = [c for c in conns if (c.laddr and c.laddr.port == port) or (c.raddr and c.raddr.port == port)]
    if not matched:
        print(f"nothing is using port {port}")
        note_hidden_processes(conns)
        return 1 if kill else 0

    def order(conn) -> tuple:
        listening = conn.status == psutil.CONN_LISTEN or (conn.type == socket.SOCK_DGRAM and conn.laddr.port == port)
        return (not listening, conn.laddr.port != port, conn.pid or 0)

    cache: dict[int, tuple[str, str]] = {}
    rows = []
    for conn in sorted(matched, key=order):
        name, _ = process_details(conn.pid, cache)
        state = conn.status if protocol(conn) == "tcp" else "BOUND"
        state_style = ("bold", "green") if state == psutil.CONN_LISTEN else ("dim",)
        rows.append([
            (protocol(conn), ("dim",)),
            address(conn.laddr),
            address(conn.raddr),
            (state, state_style),
            str(conn.pid or "-"),
            (name, ("cyan",)),
        ])
    print(style(f"port {port}", "bold"))
    print_table(["PROTO", "LOCAL", "REMOTE", "STATE", "PID", "PROCESS"], rows)
    note_hidden_processes(matched)

    pids = owners(port, conns)
    if not kill:
        if pids:
            print()
            print(style(f"stop it with: kit port {port} --kill", "dim"))
        return 0
    return kill_processes(port, pids, conns, force, assume_yes, cache)


def kill_processes(port: int, pids: list[int], conns: list, force: bool, assume_yes: bool, cache: dict) -> int:
    if not pids:
        if any(c.pid is None for c in conns):
            die(f"the process on port {port} isn't visible to you - run with sudo")
        die(f"no process is listening on port {port}")

    print()
    verb = "Kill" if force else "Stop"
    plural = "es" if len(pids) > 1 else ""
    print(style(f"{verb} the process{plural} listening on port {port}:", "bold"))
    for pid in pids:
        name, command = process_details(pid, cache)
        print(f"  {pid}  {style(name, 'cyan')}  {style(command[:100], 'dim')}")
        if name.lower().removesuffix(".exe") in CAUTION:
            warn(f"{name} looks like part of Docker, WSL, a database or the OS - stopping it may break more than this port")
    if not confirm(f"{verb} {len(pids)} process{plural}?", assume_yes):
        print("nothing stopped")
        return 1

    targets, failed = [], False
    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.kill() if force else process.terminate()
            targets.append(process)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            failed = True
            where = "an elevated (Run as administrator) terminal" if IS_WINDOWS else "sudo"
            error(f"not allowed to stop PID {pid} - try again from {where}")

    gone, alive = psutil.wait_procs(targets, timeout=3)
    for process in gone:
        print(f"{style('stopped', 'bold', 'green')} PID {process.pid}")
    for process in alive:
        failed = True
        hint = "" if force else " - add --force to kill it"
        warn(f"PID {process.pid} is still running{hint}")
    return 1 if failed else 0


def can_bind(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def cmd_free(start: int) -> int:
    if not 1 <= start <= 65535:
        die(f"invalid port: {start}")
    try:
        busy = {c.laddr.port for c in psutil.net_connections(kind="inet") if c.laddr}
    except psutil.AccessDenied:
        busy = set()  # fall back to the bind test alone
    for port in range(start, 65536):
        if port not in busy and can_bind(port):
            print(port)
            return 0
    die(f"no free port found from {start} up")


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit port", description="See what's using network ports, stop it, and find free ports.")
    parser.add_argument("target", nargs="?", help="a port number, or 'free' to find an unused port")
    parser.add_argument("--udp", action="store_true", help="also list UDP ports")
    parser.add_argument("--kill", action="store_true", help="stop the process listening on the port")
    parser.add_argument("--force", action="store_true", help="with --kill: kill immediately instead of asking the process to exit")
    parser.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--from", dest="start", type=int, default=8000, help="with 'free': first port to try (default 8000)")
    args = parser.parse_args()

    if args.target is None:
        if args.kill:
            parser.error("--kill needs a port number")
        return cmd_list(args.udp)
    if args.target == "free":
        return cmd_free(args.start)
    if not args.target.isdigit() or not 1 <= int(args.target) <= 65535:
        parser.error(f"'{args.target}' is not a port number (1-65535) or 'free'")
    return cmd_port(int(args.target), args.kill, args.force, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
