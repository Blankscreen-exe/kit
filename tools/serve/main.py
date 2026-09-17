"""Share a folder, or an app running on localhost, with other devices on the network."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import http.server
import os
import socket
import socketserver
import subprocess
import sys
import time
from pathlib import Path

from kitlib import KIT_HOME, die, style, warn
from kitlib.settings import tool_settings

try:
    import psutil

    from kitlib.browser import app_window_command, open_app_window
    from kitlib.qr import qr_lines
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

DEFAULT_PORT = 8000
PORT_SEARCH = 100
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
WILDCARD_HOSTS = {"0.0.0.0", ""}


def log(message: str) -> None:
    print(f"{style(time.strftime('%H:%M:%S'), 'dim')}  {message}", flush=True)


# --- ports and addresses -------------------------------------------------------------

def port_in_use(port: int) -> bool:
    """Something already accepts connections on this port (checked via localhost)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# First port tried when --port isn't given; main() sets it from the serve.port setting.
start_port = DEFAULT_PORT


def candidate_ports(requested: int | None) -> list[int]:
    return [requested] if requested else list(range(start_port, start_port + PORT_SEARCH))


def no_free_port() -> None:
    die(f"no free port between {start_port} and {start_port + PORT_SEARCH - 1} - pick one with --port")


def lan_addresses() -> list[tuple[str, str]]:
    """(IPv4 address, interface name) of network interfaces that are up; the default route's address comes first."""
    primary = None
    with contextlib.suppress(OSError):
        # Connecting a UDP socket sends nothing; it just makes the OS pick the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.254.254.254", 1))
            primary = sock.getsockname()[0]

    stats = psutil.net_if_stats()
    found = []
    for name, addresses in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for address in addresses:
            if address.family == socket.AF_INET and not address.address.startswith(("127.", "169.254.")):
                found.append((address.address, name))
    found.sort(key=lambda item: item[0] != primary)
    return found


def share_urls(host: str, port: int) -> tuple[str, list[tuple[str, str]]]:
    """(URL for this computer, [(network URL, interface name)])."""
    if host in WILDCARD_HOSTS:
        return f"http://127.0.0.1:{port}/", [(f"http://{ip}:{port}/", name) for ip, name in lan_addresses()]
    if host in LOOPBACK_HOSTS:
        return f"http://{host}:{port}/", []
    url = f"http://{host}:{port}/"
    return url, [(url, "")]


def print_banner(title: str, host: str, port: int, show_qr: bool) -> str:
    local, network = share_urls(host, port)
    lines = ["", f"{style('kit serve', 'bold', 'cyan')}  {title}", "", f"  {style('Local:'.ljust(9), 'bold')} {local}"]
    if network:
        for i, (url, interface) in enumerate(network):
            label = style("Network:".ljust(9), "bold") if i == 0 else " " * 9
            lines.append(f"  {label} {url}" + (style(f"  ({interface})", "dim") if interface else ""))
    else:
        lines.append(f"  {style('Network:'.ljust(9), 'bold')} " + style(f"not shared - listening on {host} only (use --host 0.0.0.0)", "dim"))

    main_url = network[0][0] if network else local
    if show_qr:
        lines.append("")
        lines += ["  " + line for line in qr_lines(main_url)]
        caption = "scan to open on your phone" if network else "this computer only"
        lines.append(style(f"  {caption}: {main_url}", "dim"))
    if network and os.name == "nt":
        lines += ["", style("  If Windows Firewall asks, allow access on Private networks so other devices can connect.", "dim")]
    lines += ["", style("Press Ctrl+C to stop.", "dim"), ""]
    print("\n".join(lines), flush=True)
    return local


def handle_window(args: argparse.Namespace, url: str) -> None:
    if args.print_window_command:
        command = app_window_command(url)
        shown = subprocess.list2cmdline(command) if command else f"(no Chromium browser) webbrowser.open({url})"
        print(f"window command: {shown}", flush=True)
    elif args.window:
        open_app_window(url)


# --- folder server ---------------------------------------------------------------------

class FolderServer(http.server.ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server silently share a busy port, so only use it elsewhere.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # skips HTTPServer's reverse-DNS lookup, which can be slow
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a client went away mid-response; not worth a traceback
        super().handle_error(request, client_address)


class FolderHandler(http.server.SimpleHTTPRequestHandler):
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        colour = "green" if status < 400 else "yellow" if status < 500 else "red"
        request = f"{self.command or '?'} {self.path if hasattr(self, 'path') else ''}"
        log(f"{self.client_address[0]:<15}  {request}  {style(status or code, colour)}")

    def log_error(self, format: str, *args) -> None:
        pass  # log_request already reports the status of failed requests


def serve_folder(args: argparse.Namespace, host: str) -> int:
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        die(f"not a folder: {args.folder}")
    handler = functools.partial(FolderHandler, directory=str(folder))

    server = None
    for port in candidate_ports(args.port):
        if port_in_use(port):
            if args.port:
                die(f"port {port} is already in use - pick another with --port, or leave it out to find a free one")
            continue
        try:
            server = FolderServer((host, port), handler)
            break
        except OSError as exc:
            if args.port:
                die(f"can't listen on {host}:{port} - {exc.strerror or exc}")
    if server is None:
        no_free_port()

    url = print_banner(f"sharing {folder}", host, server.server_address[1], not args.no_qr)
    handle_window(args, url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("stopped", flush=True)
    return 0


# --- app forwarder -----------------------------------------------------------------------

async def open_upstream(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the app on localhost, over IPv4 or IPv6 (Node often listens on ::1 only)."""
    error: OSError | None = None
    for address in ("127.0.0.1", "::1"):
        try:
            return await asyncio.open_connection(address, port)
        except OSError as exc:
            error = exc
    assert error is not None
    raise error


def app_listening(port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with contextlib.suppress(OSError), socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex((address, port)) == 0:
                return True
    return False


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes one way until end of stream, then pass the half-close on."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        writer.close()


class Forwarder:
    def __init__(self, target: int) -> None:
        self.target = target
        self.clients: set[str] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = (writer.get_extra_info("peername") or ("?",))[0]
        if client not in self.clients:
            self.clients.add(client)
            log(f"{client} connected")
        try:
            upstream_reader, upstream_writer = await open_upstream(self.target)
        except OSError:
            body = f"kit serve: nothing is answering on localhost:{self.target} - is the app running?\n".encode()
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
            writer.close()
            return

        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer), return_exceptions=True)
        for stream in (writer, upstream_writer):
            stream.close()
            with contextlib.suppress(ConnectionError, OSError):
                await stream.wait_closed()


async def serve_app(args: argparse.Namespace, host: str) -> int:
    forwarder = Forwarder(args.app)
    server = None
    for port in candidate_ports(args.port):
        if port_in_use(port):
            if args.port:
                die(f"port {port} is already in use - pick another with --port, or leave it out to find a free one")
            continue
        try:
            server = await asyncio.start_server(forwarder.handle, host, port)
            break
        except OSError as exc:
            if args.port:
                die(f"can't listen on {host}:{port} - {exc.strerror or exc}")
    if server is None:
        no_free_port()

    if not app_listening(args.app):
        warn(f"nothing is listening on localhost:{args.app} yet - start the app; kit serve will forward once it is up")
    port = server.sockets[0].getsockname()[1]
    url = print_banner(f"sharing the app on localhost:{args.app}", host, port, not args.no_qr)
    handle_window(args, url)
    async with server:
        while True:
            await asyncio.sleep(1)  # short sleeps keep Ctrl+C responsive on Windows


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    global start_port
    conf = tool_settings()
    start_port = conf.get("port", DEFAULT_PORT)

    parser = argparse.ArgumentParser(prog="kit serve", description="Share a folder or a local app with devices on your network.")
    parser.add_argument("folder", nargs="?", default=".", help="folder to share (default: current folder)")
    parser.add_argument("--app", type=int, metavar="PORT", help="share the app running on localhost:PORT instead of a folder")
    parser.add_argument("-p", "--port", type=int,
                        help=f"port to listen on (default: the serve.port setting or {DEFAULT_PORT}, or the next free one)")
    parser.add_argument("--host", default=conf.get("host", "0.0.0.0"),
                        help="address to listen on: 0.0.0.0 = every network, 127.0.0.1 = this computer only (setting: serve.host)")
    parser.add_argument("--window", action="store_true", help="also open the page in its own app window (setting: serve.window)")
    parser.add_argument("--no-window", dest="window", action="store_false", help="don't open an app window")
    parser.add_argument("--no-qr", action="store_true", help="don't print a QR code")
    parser.add_argument("--print-window-command", action="store_true", help=argparse.SUPPRESS)  # for testing --window
    parser.set_defaults(window=conf.get("window", False))
    args = parser.parse_args()

    for name in ("port", "app"):
        value = getattr(args, name)
        if value is not None and not 1 <= value <= 65535:
            parser.error(f"--{name} must be between 1 and 65535")
    if args.app is not None and args.folder != ".":
        parser.error("give either a folder or --app, not both")

    if args.app is None:
        return serve_folder(args, args.host)
    try:
        return asyncio.run(serve_app(args, args.host))
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
