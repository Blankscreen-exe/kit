"""Send a desktop notification to another PC on your LAN or tailnet."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kitlib import die, lan, style, theme, warn
from kitlib.browser import no_display, open_app_window
from kitlib.popup import popup
from kitlib.ui import add_ui_flags, want_ui, why_no_ui
from kitlib.webserver import KitHandler
from kitlib.settings import tool_settings

IS_WINDOWS = sys.platform.startswith("win")
TOOL_DIR = Path(os.environ.get("KIT_TOOL_DIR") or Path(__file__).resolve().parent)
DEFAULT_TITLE = lan.DEFAULT_TITLE
MAX_BODY = 8_000
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
SEARCH_EVERY = 30  # seconds between the window's own discovery rounds
DEVICE_TTL = 75  # a device the window hasn't heard from in this long drops off its list
MAX_ENTRIES = 500  # messages the window keeps; older ones scroll away for good
POLL_HOLD = 25  # seconds the window's update request waits for something new before answering anyway
BYE_GRACE = 4  # after the window says it's closing, seconds to wait for it to come back (a reload)
WINDOW_TIMEOUT = 600  # no word from the window at all in this long: it's gone, even without a goodbye
STATIC = {"/notify.js": "text/javascript; charset=utf-8"}
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")


# --- showing the notification ---------------------------------------------------------

_popup_warned = False


def show_notification(title: str, message: str) -> None:
    """A desktop popup, where there's a desktop - clicking it opens the first link in the message.
    Every message is printed in the terminal too (print_received), so a machine that can't show one
    - a server, an SSH session - loses nothing."""
    global _popup_warned
    if no_display():
        return
    link_match = URL_RE.search(message)
    link = link_match.group(0).rstrip(".,;)") if link_match else None
    if not popup(title, message, link=link, app="kit notify") and not _popup_warned:
        _popup_warned = True
        warn("no desktop notification tool found here (notify-send on Linux) - messages only print below")


def print_received(sender: str, title: str, message: str) -> None:
    """A received message as a line in the terminal - on a server, this is the list of what arrived."""
    head = style(time.strftime("%H:%M:%S"), "dim") + "  " + style(sender, "bold", "accent")
    if title and title != DEFAULT_TITLE:
        head += "  " + style(title, "bold")
    lines = message.splitlines() or [""]
    print(f"{head}  {lines[0]}" + "".join(f"\n{' ' * 10}{line}" for line in lines[1:]), flush=True)


# --- server: kit notify serve -------------------------------------------------------------
#
# Discovery and sending live in kitlib.lan, so other tools can message these machines too; what
# stays here is the receiving end - the server a message arrives at - and the CLI and window.

class NotifyServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use - and a second
    # serve or window has to be refused, not left silently sharing the port with the first.
    allow_reuse_address = not IS_WINDOWS

    def __init__(self, host: str, port: int, token: str, window: Window | None = None) -> None:
        super().__init__((host, port), Handler)
        self.token = token
        self.window = window  # set when the window is what's listening: it lists what arrives

    def handle_error(self, request, client_address) -> None:
        # a sender hanging up on a kept-alive connection isn't worth a traceback
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            ThreadingHTTPServer.handle_error(self, request, client_address)  # not super(): ControlServer shares this


class Handler(KitHandler, BaseHTTPRequestHandler):
    server: NotifyServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        body = (b"kit notify is listening.\n"
                b"Send it a message with: kit notify send <this address> \"your message\"\n")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/notify":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        token = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid token"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "message too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        message = str(body.get("message") or "").strip()[:2000]
        if not message:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "'message' is required"})
        title = str(body.get("title") or DEFAULT_TITLE).strip()[:200] or DEFAULT_TITLE
        # who sent it: its hostname, from any kit notify that includes one - older ones don't
        sender = str(body.get("from") or "").strip()[:100]
        if sender and title == DEFAULT_TITLE:
            title = f"{DEFAULT_TITLE} - from {sender}"
        own_title = str(body.get("title") or "").strip()[:200]
        if self.server.window:
            self.server.window.received(sender or self.client_address[0], self.client_address[0], own_title, message)
        print_received(sender or self.client_address[0], own_title, message)
        show_notification(title, message)  # starts the popup and returns: the sender isn't kept waiting
        self._json(HTTPStatus.OK, {"ok": True})


def listen(port: int, token: str, window: Window | None = None) -> NotifyServer:
    """The LAN-facing server - or a clear exit when this port is already taken, most likely by
    another serve or window on this machine."""
    try:
        return NotifyServer("0.0.0.0", port, token, window)
    except OSError as exc:
        die(f"can't listen on port {port} ({exc}) - is 'kit notify serve' or a kit notify window already "
            f"running here? Only one can run at a time. Otherwise see what's using it: kit port {port}")


def start_discoverable(server: NotifyServer, discovery_port: int, passphrase: str,
                       stop: threading.Event) -> None:
    if passphrase:
        threading.Thread(target=lan.respond,
                         args=(discovery_port, server.server_address[1], server.token, passphrase, stop),
                         daemon=True).start()


def passphrase_hint() -> str:
    return ("reachable on this network, but not discoverable - set the same passphrase on every machine "
            f"to turn that on: kit config set notify.passphrase {secrets.token_urlsafe(12)}")


def cmd_serve(port: int, rotate: bool, discovery_port: int, passphrase: str) -> int:
    token = lan.load_or_create_token(rotate)
    server = listen(port, token)
    url = lan.Device(socket.gethostname(), lan.lan_ip(), server.server_address[1], token).url
    print(f"kit notify: {url}")
    print(style(f"  send to this machine with: kit notify send {url} \"your message\"", "dim"))
    print(style("  the token stays the same across restarts, so this address keeps working - "
                "save it. 'kit notify serve --rotate' replaces it", "dim"))

    stop_discovery = threading.Event()
    if passphrase:
        print(style("  reachable on this network, and discoverable: "
                    "kit notify send (with no address) finds it automatically", "dim"))
    else:
        print(style(f"  {passphrase_hint()}", "dim"))
    start_discoverable(server, discovery_port, passphrase, stop_discovery)
    print(style("  Ctrl+C to stop", "dim"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping kit notify")
    finally:
        stop_discovery.set()
        server.server_close()
    return 0


# --- client: kit notify send --------------------------------------------------------------

def cmd_send(url: str, message: str, title: str) -> int:
    device = lan.parse_address(url)
    if device is None:
        die("that doesn't look like a kit notify address - it should look like what "
            "'kit notify serve' printed, ending in ?t=...")
    try:
        lan.send_message(device, message, title)
    except lan.DeliveryError as exc:
        hint = "" if exc.rejected else (" - if that machine is on Windows, its firewall may be blocking it (common "
                                        "the first time): see the Firewall section in 'kit help notify'")
        die(f"{device.address} {exc}{hint}")
    print(f"sent to {device.address}")
    return 0


def find_devices(discovery_port: int, passphrase: str) -> list[lan.Device]:
    """Every discoverable machine, or a clear exit when there's no passphrase or nobody answers."""
    if not passphrase:
        die("notify.passphrase isn't set, so there's nothing to discover with - set the same value "
            "here and on the other machine: kit config set notify.passphrase <same-value-on-both>")
    print(style("  looking for kit notify on this network...", "dim"), flush=True)
    devices = lan.discover(passphrase, discovery_port)
    if not devices:
        die("nothing answered. Check: another machine is running 'kit notify serve' or a kit notify window, with the "
            "exact same notify.passphrase - and that its firewall allows it (Windows often blocks "
            "this the first time: see the Firewall section in 'kit help notify' for the exact "
            "commands). Discovery doesn't cross into a tailnet - use the address directly for that.")
    return devices


def pick_devices(devices: list[lan.Device], names: list[str]) -> list[lan.Device]:
    """The devices named (by hostname, any case, or by address) - every one of them has to be there."""
    picked, missing = lan.pick(devices, names)
    if missing:
        found = ", ".join(sorted(d.name for d in devices))
        die(f"not found on this network: {', '.join(missing)} - found: {found}")
    return picked


def cmd_devices(discovery_port: int, passphrase: str) -> int:
    devices = find_devices(discovery_port, passphrase)
    own = lan.own_token()
    width = max(len(d.name) for d in devices)
    for d in sorted(devices, key=lambda d: d.name.lower()):
        note = style("  (this machine)", "dim") if d.token == own else ""
        print(f"  {style(d.name.ljust(width), 'bold', 'accent')}  {d.address}{note}")
    return 0


def cmd_send_lan(message: str, title: str, discovery_port: int, passphrase: str, to: list[str] | None = None) -> int:
    devices = find_devices(discovery_port, passphrase)
    if to:
        devices = pick_devices(devices, to)
    failed = 0
    for device, error in lan.send_to_all(devices, message, title):
        if error is None:
            print(f"sent to {device.name} ({device.ip})")
            continue
        failed += 1
        if error.rejected:
            warn(f"{device.name} ({device.ip}) {error}")
        else:
            # found it (discovery got through), but the notify port didn't answer - a narrower signal
            # than "nothing answered": likely that port specifically, not discovery, is blocked
            warn(f"{device.name} ({device.ip}) answered discovery but port {device.port} {error} - "
                 "see the Firewall section in 'kit help notify'")
    if failed == len(devices):
        die("couldn't reach any of them")
    return 0


# --- the window: kit notify with no command ---------------------------------------------
#
# The same LAN server as 'serve', plus a page on a second, localhost-only server that lists
# what's been sent and received and sends to the devices discovery finds. Nothing is kept on
# disk: the list lives in this process and goes when the window closes.

class Window:
    """Everything the page shows, and the long poll it waits on for changes."""

    def __init__(self, token: str, port: int, discovery_port: int, passphrase: str) -> None:
        self.token = token
        self.discovery_port = discovery_port
        self.passphrase = passphrase
        self.me = {"name": socket.gethostname(), "address": f"{lan.lan_ip()}:{port}", "discoverable": bool(passphrase)}
        self.cond = threading.Condition()
        self.version = 0
        self.entries: list[dict] = []
        self.devices: dict[str, dict] = {}  # token -> device, with when it last answered
        self.searching = False
        self.last_id = 0
        self.stop = threading.Event()
        self.search_now = threading.Event()
        # whether the page is still open: an update request waiting, or one recently
        self.polls = 0
        self.last_seen = time.monotonic()
        self.bye_at: float | None = None

    def _changed(self) -> None:
        """Call with self.cond held."""
        self.version += 1
        self.cond.notify_all()

    def _add(self, entry: dict) -> dict:
        with self.cond:
            self.last_id += 1
            entry.update(id=self.last_id, time=time.time())
            self.entries.append(entry)
            del self.entries[:-MAX_ENTRIES]
            self._changed()
        return entry

    def received(self, sender: str, ip: str, title: str, message: str) -> None:
        self._add({"dir": "in", "from": sender, "ip": ip,
                   "title": "" if title == DEFAULT_TITLE else title, "message": message})

    # -- what the page sees

    def snapshot(self) -> dict:
        with self.cond:
            devices = sorted(self.devices.values(), key=lambda d: (d["name"].lower(), d["ip"]))
            return {
                "version": self.version, "me": self.me, "searching": self.searching,
                "devices": [{"id": d["id"], "name": d["name"], "ip": d["ip"]} for d in devices],
                "entries": [{**e, "to": [dict(r) for r in e["to"]]} if "to" in e else dict(e) for e in self.entries],
            }

    def wait_for_change(self, since: int) -> dict:
        with self.cond:
            self.polls += 1
            self.last_seen = time.monotonic()
            self.bye_at = None
            try:
                self.cond.wait_for(lambda: self.version != since or self.stop.is_set(), POLL_HOLD)
            finally:
                self.polls -= 1
                self.last_seen = time.monotonic()
        return self.snapshot()

    def left(self) -> None:
        """The page is unloading - closed, or reloading. Answer its waiting poll so it lets go."""
        with self.cond:
            self.bye_at = time.monotonic()
            self._changed()

    def gone(self) -> bool:
        with self.cond:
            if self.polls:
                return False
            now = time.monotonic()
            if self.bye_at is not None and now - self.bye_at > BYE_GRACE:
                return True  # a reloaded page would have polled again by now, clearing bye_at
            return now - self.last_seen > WINDOW_TIMEOUT

    # -- finding devices

    def search_loop(self) -> None:
        while not self.stop.is_set():
            with self.cond:
                self.searching = True
                self._changed()
            found = lan.discover(self.passphrase, self.discovery_port)
            now = time.monotonic()
            with self.cond:
                for device in found:
                    if device.token != self.token:  # this machine answers its own query too
                        self.devices[device.token] = {"device": device, "name": device.name, "ip": device.ip,
                                                      "id": device.id, "seen": now}
                for token in [t for t, d in self.devices.items() if now - d["seen"] > DEVICE_TTL]:
                    del self.devices[token]
                self.searching = False
                self._changed()
            self.search_now.wait(SEARCH_EVERY)
            self.search_now.clear()

    # -- sending

    def send(self, message: str, to: object) -> None:
        with self.cond:
            devices = list(self.devices.values())
        if to != "all":
            wanted = set(to) if isinstance(to, list) else set()
            devices = [d for d in devices if d["id"] in wanted]
        if not devices:
            raise ValueError("none of those devices are around any more - pick again" if to != "all"
                             else "no devices found to send to yet")
        entry = self._add({"dir": "out", "message": message,
                           "to": [{"id": d["id"], "name": d["name"], "ip": d["ip"], "status": "sending", "error": ""}
                                  for d in devices]})
        # each on its own thread, same as the CLI's broadcast: one slow device holds up nobody else
        for device in devices:
            threading.Thread(target=self._deliver, args=(entry, device, message), daemon=True).start()

    def _deliver(self, entry: dict, device: dict, message: str) -> None:
        error = ""
        try:
            lan.send_message(device["device"], message)
        except lan.DeliveryError as exc:
            error = str(exc) if exc.rejected else f"{exc} - its firewall may be blocking port {device['device'].port}"
        with self.cond:
            for recipient in entry["to"]:
                if recipient["id"] == device["id"]:
                    recipient.update(status="failed" if error else "sent", error=error)
            self._changed()


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    handle_error = NotifyServer.handle_error

    def __init__(self, window: Window) -> None:
        super().__init__(("127.0.0.1", 0), ControlHandler)
        port = self.server_address[1]
        self.window = window
        self.token = secrets.token_urlsafe(24)
        self.cookie_name = f"kit_notify_{port}"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


class ControlHandler(KitHandler, BaseHTTPRequestHandler):
    server: ControlServer
    server_version = "kit-notify"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def send_body(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def send_page(self, status: int, name: str) -> None:
        page = theme.inject((TOOL_DIR / name).read_text(encoding="utf-8")).encode("utf-8")
        self.send_body(status, page, "text/html; charset=utf-8",
                       {"Content-Security-Policy": PAGE_CSP, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})

    def host_ok(self) -> bool:
        # Blocks DNS-rebinding: pages on other domains that resolve here send their own Host.
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def authorized(self) -> bool:
        token = self.headers.get("X-Kit-Token", "")
        if not token:
            try:
                morsel = SimpleCookie(self.headers.get("Cookie", "")).get(self.server.cookie_name)
            except CookieError:
                morsel = None
            token = morsel.value if morsel else ""
        return hmac.compare_digest(token.encode(), self.server.token.encode())

    def do_GET(self) -> None:
        if not self.host_ok():
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
        url = urlparse(self.path)
        if url.path == "/":
            query_token = parse_qs(url.query).get("token", [""])[0]
            if query_token and hmac.compare_digest(query_token.encode(), self.server.token.encode()):
                # Swap the token in the address for a cookie, so it doesn't linger in the address bar.
                cookie = f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
                return self.send_body(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
            if not self.authorized():
                return self.send_page(HTTPStatus.UNAUTHORIZED, "denied.html")
            return self.send_page(HTTPStatus.OK, "notify.html")
        if url.path == "/favicon.ico":
            return self.send_body(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        if not self.authorized():
            return self.send_json(HTTPStatus.UNAUTHORIZED,
                                  {"error": "missing or invalid token - open the address printed in the terminal"})
        if url.path in STATIC:
            return self.send_body(HTTPStatus.OK, (TOOL_DIR / url.path.lstrip("/")).read_bytes(), STATIC[url.path])
        if url.path == "/api/events":
            try:
                since = int(parse_qs(url.query).get("since", ["-1"])[0])
            except ValueError:
                since = -1
            return self.send_json(HTTPStatus.OK, self.server.window.wait_for_change(since))
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.host_ok():
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
        if not self.authorized():
            return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid token"})
        path = urlparse(self.path).path
        window = self.server.window
        if path == "/api/bye":  # sendBeacon: can't add headers, and needs no body
            window.left()
            return self.send_json(HTTPStatus.OK, {"ok": True})
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in self.server.allowed_hosts}:
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request rejected"})
        if origin is None and not self.headers.get("X-Kit-Token"):
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "requests without an Origin header must send X-Kit-Token"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "message too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "expected a JSON object"})

        if path == "/api/refresh":
            window.search_now.set()
            return self.send_json(HTTPStatus.OK, {"ok": True})
        if path == "/api/send":
            message = str(body.get("message") or "").strip()[:2000]
            if not message:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "write a message first"})
            try:
                window.send(message, body.get("to"))
            except ValueError as exc:
                return self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return self.send_json(HTTPStatus.OK, {"ok": True})
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def cmd_window(port: int, discovery_port: int, passphrase: str) -> int:
    token = lan.load_or_create_token()
    window = Window(token, port, discovery_port, passphrase)
    server = listen(port, token, window)
    control = ControlServer(window)
    ui = f"http://127.0.0.1:{control.server_address[1]}/?token={control.token}"

    # The window's own address first: kit's runner and hub take the first URL printed as the
    # tool's, and this one - loopback - is never announced to anyone.
    print(f"{style('kit notify', 'bold', 'accent')}  window: {ui}")
    print(style(f"  this machine: http://{window.me['address']}/?t={token}", "dim"))
    print(style("  reachable on this network, and discoverable" if passphrase else f"  {passphrase_hint()}", "dim"))
    print(style("  closing the window stops it, and so does Ctrl+C here", "dim"), flush=True)

    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=control.serve_forever, daemon=True).start()
    start_discoverable(server, discovery_port, passphrase, window.stop)
    if passphrase:
        threading.Thread(target=window.search_loop, daemon=True).start()
    open_app_window(ui)
    try:
        while not window.stop.wait(1):
            if window.gone():
                print("the window was closed - stopping kit notify")
                break
    except KeyboardInterrupt:
        print("\nstopping kit notify")
    finally:
        window.stop.set()
        window.search_now.set()
        with window.cond:
            window.cond.notify_all()
        server.shutdown()
        server.server_close()
        control.shutdown()
        control.server_close()
    return 0


# --- main -----------------------------------------------------------------------------

def main() -> int:
    settings = tool_settings()
    parser = argparse.ArgumentParser(prog="kit notify",
                                     description="Send a desktop notification to another PC on your LAN or tailnet.")
    parser.epilog = ("With no command, opens a window that lists what's sent and received, and sends to the "
                     "devices it finds - where there's a desktop to show it on, otherwise it prints this help.")
    add_ui_flags(parser, ui_help="with no command: open the window, even where kit would print help",
                 no_ui_help="with no command: print this help instead of opening the window")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("serve", help="listen for notifications - run this on the machine that should pop them up")
    p.add_argument("--port", type=int, default=settings["port"], help=f"port to listen on (default {settings['port']})")
    p.add_argument("--lan", action="store_true", help=argparse.SUPPRESS)  # always on now; kept so old commands still run
    p.add_argument("--rotate", action="store_true", help="replace the saved token - old addresses stop working")
    p.add_argument("--discovery-port", type=int, default=settings["discovery_port"],
                   help=f"UDP port for LAN discovery (default {settings['discovery_port']})")

    p = sub.add_parser("send", help="send a notification to a machine running 'kit notify serve'")
    p.add_argument("url", nargs="?",
                   help="the address 'kit notify serve' printed, e.g. http://host:port/?t=... "
                        "omit it to broadcast to every discoverable machine on this network instead")
    p.add_argument("message")
    p.add_argument("--title", default="kit notify", help="notification title (default: 'kit notify')")
    p.add_argument("--to", metavar="NAMES", type=lambda text: text.split(","),
                   help="with no address: only these machines, by name or address, comma-separated (default: all)")
    p.add_argument("--discovery-port", type=int, default=settings["discovery_port"],
                   help=f"UDP port for LAN discovery, with no address (default {settings['discovery_port']})")

    p = sub.add_parser("devices", help="list the machines discovery finds on this network")
    p.add_argument("--discovery-port", type=int, default=settings["discovery_port"],
                   help=f"UDP port for LAN discovery (default {settings['discovery_port']})")

    args = parser.parse_args()
    if args.command is None:
        if not want_ui(args.ui):
            parser.print_help()
            if args.ui is None:
                print(style(f"\n{why_no_ui()} - use serve, send and devices instead", "dim"))
            return 0
        return cmd_window(settings["port"], settings["discovery_port"], settings["passphrase"])
    if args.command == "serve":
        return cmd_serve(args.port, args.rotate, args.discovery_port, settings["passphrase"])
    if args.command == "devices":
        return cmd_devices(args.discovery_port, settings["passphrase"])
    if args.url:
        if args.to:
            die("--to picks machines found on this network - leave out the address to use it")
        return cmd_send(args.url, args.message, args.title)
    return cmd_send_lan(args.message, args.title, args.discovery_port, settings["passphrase"], args.to)


if __name__ == "__main__":
    raise SystemExit(main())
