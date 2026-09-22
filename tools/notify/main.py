"""Send a desktop notification to another PC on your LAN or tailnet."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from kitlib import die, style, warn
from kitlib.settings import tool_settings

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
MAX_BODY = 8_000
DISCOVER_MAGIC = "kit-notify-discover-v1"
DISCOVER_REPLY_MAGIC = "kit-notify-here-v1"
DISCOVER_WAIT = 1.5  # seconds to collect replies to a broadcast


# --- token: kept on disk rather than made fresh every start ------------------------------
#
# A fresh token on every 'serve' would mean re-copying the address every single time,
# defeating the point of saving one. Same reasoning content-machine's own token uses.

def _token_path() -> Path:
    override = os.environ.get("KIT_NOTIFY_DATA")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "notify-token"


def _load_or_create_token(rotate: bool = False) -> str:
    path = _token_path()
    if not rotate and path.is_file():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    value = secrets.token_urlsafe(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


# --- showing the notification, per platform ---------------------------------------------

def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def show_notification(title: str, message: str) -> None:
    try:
        if IS_WINDOWS:
            # No extra install needed: WinForms' tray-balloon API, part of every .NET-equipped
            # Windows box, rather than the newer toast APIs that need an extra module.
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$n.BalloonTipTitle = {_powershell_string(title)}; "
                f"$n.BalloonTipText = {_powershell_string(message)}; "
                "$n.Visible = $true; $n.ShowBalloonTip(8000); Start-Sleep -Seconds 1; $n.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        elif IS_MACOS:
            script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        else:
            subprocess.run(["notify-send", "--", title, message], capture_output=True, timeout=10)
    except FileNotFoundError:
        warn(f"no notification tool found on this system - here it is instead: {title}: {message}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"couldn't show the notification ({exc}) - here it is instead: {title}: {message}")


# --- server: kit notify serve -------------------------------------------------------------

def lan_ip() -> str:
    """Best-effort: the address this machine would be reached at on the LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # no packets are sent; this just picks the interface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# --- LAN discovery: no URL needed if you're fine reaching everyone announcing itself ------
#
# Tied to --lan, the same opt-in that already means "reachable on this network": a discoverable
# machine answers a broadcast query with its address AND token in the clear over UDP, so being
# discoverable is exactly as trusting of the LAN as being --lan-reachable already was, not a new,
# separate risk. A machine that never passed --lan never answers, same as it's never reachable.

def _discover_responder(discovery_port: int, http_port: int, token: str, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", discovery_port))
    except OSError as exc:
        warn(f"couldn't listen for discovery broadcasts on UDP {discovery_port}: {exc} - "
            "kit notify send (with no address) won't find this machine")
        return
    sock.settimeout(0.5)
    name = socket.gethostname()
    try:
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                query = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not (isinstance(query, dict) and query.get("magic") == DISCOVER_MAGIC):
                continue
            reply = json.dumps({"magic": DISCOVER_REPLY_MAGIC, "name": name, "port": http_port, "token": token}).encode()
            try:
                sock.sendto(reply, addr)
            except OSError:
                pass
    finally:
        sock.close()


def discover_on_lan(discovery_port: int) -> list[dict]:
    """Broadcasts a query and collects replies for DISCOVER_WAIT seconds. Each reply: name, ip, port, token."""
    query = json.dumps({"magic": DISCOVER_MAGIC}).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    found: dict[tuple[str, int], dict] = {}
    try:
        sock.sendto(query, ("255.255.255.255", discovery_port))
        deadline = time.monotonic() + DISCOVER_WAIT
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not (isinstance(reply, dict) and reply.get("magic") == DISCOVER_REPLY_MAGIC):
                continue
            port, token = reply.get("port"), reply.get("token")
            if not isinstance(port, int) or not isinstance(token, str) or not token:
                continue
            key = (addr[0], port)
            found[key] = {"name": reply.get("name") or addr[0], "ip": addr[0], "port": port, "token": token}
    finally:
        sock.close()
    return list(found.values())


class NotifyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str, port: int, token: str) -> None:
        super().__init__((host, port), Handler)
        self.token = token


class Handler(BaseHTTPRequestHandler):
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
        title = str(body.get("title") or "kit notify").strip()[:200] or "kit notify"
        show_notification(title, message)
        self._json(HTTPStatus.OK, {"ok": True})


def cmd_serve(lan: bool, port: int, rotate: bool, discovery_port: int) -> int:
    host = "0.0.0.0" if lan else "127.0.0.1"
    token = _load_or_create_token(rotate)
    try:
        server = NotifyServer(host, port, token)
    except OSError as exc:
        die(f"can't listen on port {port}: {exc}")
    shown = lan_ip() if lan else "127.0.0.1"
    url = f"http://{shown}:{server.server_address[1]}/?t={token}"
    print(f"kit notify: {url}")
    print(style(f"  send to this machine with: kit notify send {url} \"your message\"", "dim"))
    print(style("  the token stays the same across restarts, so this address keeps working - "
                "save it. 'kit notify serve --rotate' replaces it", "dim"))

    stop_discovery = threading.Event()
    if lan:
        print(style(f"  reachable on this network too, and discoverable: "
                    f"kit notify send (with no address) finds it automatically", "dim"))
        threading.Thread(target=_discover_responder, args=(discovery_port, server.server_address[1], token, stop_discovery),
                         daemon=True).start()
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

def _post_notify(host: str, port: int, token: str, title: str, message: str) -> None:
    """Raises HTTPError/URLError on failure - callers decide how to report it."""
    target = f"http://{host}:{port}/notify?t={token}"
    payload = json.dumps({"title": title, "message": message}).encode("utf-8")
    request = Request(target, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10):
        pass


def cmd_send(url: str, message: str, title: str) -> int:
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("t", [""])[0]
    if not parsed.hostname or not parsed.port or not token:
        die("that doesn't look like a kit notify address - it should look like what "
            "'kit notify serve' printed, ending in ?t=...")
    try:
        _post_notify(parsed.hostname, parsed.port, token, title, message)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        die(f"{parsed.netloc} rejected it ({exc.code}): {detail}")
    except URLError as exc:
        die(f"couldn't reach {parsed.netloc}: {exc.reason}")
    print(f"sent to {parsed.netloc}")
    return 0


def cmd_send_lan(message: str, title: str, discovery_port: int) -> int:
    print(style("  looking for kit notify on this network...", "dim"))
    devices = discover_on_lan(discovery_port)
    if not devices:
        die("nothing answered - is a machine running 'kit notify serve --lan' on this network? "
            "(discovery doesn't cross into a tailnet - use its address directly for that)")
    failed = 0
    for device in devices:
        try:
            _post_notify(device["ip"], device["port"], device["token"], title, message)
        except (HTTPError, URLError) as exc:
            failed += 1
            warn(f"{device['name']} ({device['ip']}): {exc}")
        else:
            print(f"sent to {device['name']} ({device['ip']})")
    if failed == len(devices):
        die("couldn't reach any of them")
    return 0


# --- main -----------------------------------------------------------------------------

def main() -> int:
    settings = tool_settings()
    parser = argparse.ArgumentParser(prog="kit notify",
                                     description="Send a desktop notification to another PC on your LAN or tailnet.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("serve", help="listen for notifications - run this on the machine that should pop them up")
    p.add_argument("--port", type=int, default=settings["port"], help=f"port to listen on (default {settings['port']})")
    p.add_argument("--lan", action="store_true",
                   help="also reachable from other devices on this network, and discoverable by 'kit notify send'")
    p.add_argument("--rotate", action="store_true", help="replace the saved token - old addresses stop working")
    p.add_argument("--discovery-port", type=int, default=settings["discovery_port"],
                   help=f"UDP port for LAN discovery (default {settings['discovery_port']})")

    p = sub.add_parser("send", help="send a notification to a machine running 'kit notify serve'")
    p.add_argument("url", nargs="?",
                   help="the address 'kit notify serve' printed, e.g. http://host:port/?t=... "
                        "omit it to broadcast to every --lan machine on this network instead")
    p.add_argument("message")
    p.add_argument("--title", default="kit notify", help="notification title (default: 'kit notify')")
    p.add_argument("--discovery-port", type=int, default=settings["discovery_port"],
                   help=f"UDP port for LAN discovery, with no address (default {settings['discovery_port']})")

    args = parser.parse_args()
    if args.command == "serve":
        return cmd_serve(args.lan, args.port, args.rotate, args.discovery_port)
    if args.url:
        return cmd_send(args.url, args.message, args.title)
    return cmd_send_lan(args.message, args.title, args.discovery_port)


if __name__ == "__main__":
    raise SystemExit(main())
