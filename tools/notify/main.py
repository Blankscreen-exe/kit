"""Send a desktop notification to another PC on your LAN or tailnet."""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import socket
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from kitlib import die, style, warn
from kitlib.settings import tool_settings

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
MAX_BODY = 8_000


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


def cmd_serve(lan: bool, port: int) -> int:
    host = "0.0.0.0" if lan else "127.0.0.1"
    token = secrets.token_urlsafe(16)
    try:
        server = NotifyServer(host, port, token)
    except OSError as exc:
        die(f"can't listen on port {port}: {exc}")
    shown = lan_ip() if lan else "127.0.0.1"
    url = f"http://{shown}:{server.server_address[1]}/?t={token}"
    print(f"kit notify: {url}")
    print(style(f"  send to this machine with: kit notify send {url} \"your message\"", "dim"))
    if lan:
        print(style("  reachable on this network too", "dim"))
    print(style("  Ctrl+C to stop", "dim"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping kit notify")
    finally:
        server.server_close()
    return 0


# --- client: kit notify send --------------------------------------------------------------

def cmd_send(url: str, message: str, title: str) -> int:
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("t", [""])[0]
    if not parsed.netloc or not token:
        die("that doesn't look like a kit notify address - it should look like what "
            "'kit notify serve' printed, ending in ?t=...")
    target = f"{parsed.scheme}://{parsed.netloc}/notify?t={token}"
    payload = json.dumps({"title": title, "message": message}).encode("utf-8")
    request = Request(target, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=10):
            pass
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        die(f"{parsed.netloc} rejected it ({exc.code}): {detail}")
    except URLError as exc:
        die(f"couldn't reach {parsed.netloc}: {exc.reason}")
    print(f"sent to {parsed.netloc}")
    return 0


# --- main -----------------------------------------------------------------------------

def main() -> int:
    settings = tool_settings()
    parser = argparse.ArgumentParser(prog="kit notify",
                                     description="Send a desktop notification to another PC on your LAN or tailnet.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("serve", help="listen for notifications - run this on the machine that should pop them up")
    p.add_argument("--port", type=int, default=settings["port"], help=f"port to listen on (default {settings['port']})")
    p.add_argument("--lan", action="store_true", help="also reachable from other devices on this network")

    p = sub.add_parser("send", help="send a notification to a machine running 'kit notify serve'")
    p.add_argument("url", help="the address 'kit notify serve' printed, e.g. http://host:port/?t=...")
    p.add_argument("message")
    p.add_argument("--title", default="kit notify", help="notification title (default: 'kit notify')")

    args = parser.parse_args()
    if args.command == "serve":
        return cmd_serve(args.lan, args.port)
    return cmd_send(args.url, args.message, args.title)


if __name__ == "__main__":
    raise SystemExit(main())
