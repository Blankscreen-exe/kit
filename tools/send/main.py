"""Send a file straight to someone's browser: kit holds it, a local page does the transferring."""

from __future__ import annotations

import argparse
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from kitlib import die, style
from kitlib.browser import open_app_window
from kitlib.qr import make_qr, qr_lines
from kitlib.settings import tool_settings
from kitlib.webserver import KitHandler

TOOL_DIR = Path(os.environ.get("KIT_TOOL_DIR") or Path(__file__).resolve().parent)
DEFAULT_PORT = 8770
PORT_ATTEMPTS = 20
STATIC = {
    "/sender.js": "text/javascript; charset=utf-8",
    "/receiver.js": "text/javascript; charset=utf-8",
    "/peerjs.min.js": "text/javascript; charset=utf-8",
    "/sw.js": "text/javascript; charset=utf-8",
}
# The pages talk to the PeerJS cloud (the free matchmaker) and to this server, nothing else.
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' https://0.peerjs.com wss://0.peerjs.com; "
            # the receiver saves through a service worker, reached by framing a same-origin URL
            "worker-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'")
SLICE = 8 << 20        # bytes the sender page reads at a time
MAX_BODY = 256_000     # a dialog can hand back a lot of long paths
BROWSE_LIMIT = 2000    # entries returned for one folder
DIALOG_TIMEOUT = 600   # seconds to wait for someone to finish choosing files
HISTORY_LIMIT = 100
MAX_WINDOW_FILE = 1 << 40


class ApiError(Exception):
    """Something the window asked for that kit can't do, with the status to answer with."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


# --- shares --------------------------------------------------------------------------

@dataclass
class Share:
    token: str          # the secret in the link
    id: str             # a name for the history file, so the link's secret isn't written to disk
    name: str
    size: int
    expires_at: float
    max_downloads: int | None       # None: no limit
    path: Path | None = None        # None when the window holds the file (dropped in)
    created_at: float = field(default_factory=time.time)
    downloads: int = 0
    active: dict[str, int] = field(default_factory=dict)  # peer -> bytes sent so far
    dead: str = ""                  # why it stopped working ("" while live)
    logged_dead: bool = False

    @property
    def source(self) -> str:
        return "path" if self.path else "window"

    def state(self) -> dict:
        return {
            "token": self.token, "name": self.name, "size": self.size,
            "downloads": self.downloads, "max": self.max_downloads,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "dead": self.dead, "source": self.source,
            "folder": str(self.path.parent) if self.path else "",
            # options can be changed until the first download, after which they are facts
            "locked": self.downloads > 0 or bool(self.dead),
        }

    def check(self) -> str:
        """"" while the share still works, else why it doesn't."""
        if self.dead:
            return self.dead
        if time.time() >= self.expires_at:
            self.dead = "the link expired"
        elif self.max_downloads is not None and self.downloads >= self.max_downloads:
            self.dead = "the link reached its download limit"
        return self.dead


DURATION_RE = re.compile(r"(?:(?P<h>\d+(?:\.\d+)?)\s*h)?\s*(?:(?P<m>\d+(?:\.\d+)?)\s*m)?\s*(?:(?P<s>\d+(?:\.\d+)?)\s*s)?$", re.I)


def parse_expire(text: str) -> float:
    """'2h', '90m', '30s', '1h30m' or a bare number of minutes -> seconds."""
    value = str(text).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        seconds = float(value) * 60
    elif (match := DURATION_RE.fullmatch(value)) and any(match.groupdict().values()):
        seconds = float(match["h"] or 0) * 3600 + float(match["m"] or 0) * 60 + float(match["s"] or 0)
    else:
        raise ValueError(f"not a duration: '{text}' (try 2h, 90m, 30s or 1h30m)")
    if seconds <= 0:
        raise ValueError("the expiry must be more than zero")
    return seconds


def parse_limit(once: object, maximum: object) -> int | None:
    """The download limit from the window's two controls (or the CLI's two flags)."""
    if once:
        return 1
    if maximum in (None, "", 0, "0"):
        return None
    try:
        limit = int(maximum)
    except (TypeError, ValueError):
        raise ApiError("the download limit has to be a number") from None
    if limit < 1:
        raise ApiError("the download limit has to be at least 1")
    return limit


def human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" or count >= 100 else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


# --- history -------------------------------------------------------------------------

def data_path() -> Path:
    override = os.environ.get("KIT_SEND_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "send-history.json"


class History:
    """What has been shared, in one small JSON file, so the window can show it after a restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                self.entries = [entry for entry in loaded if isinstance(entry, dict)][-HISTORY_LIMIT:]
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            try:
                path.replace(path.with_suffix(".json.bak"))  # keep it rather than quietly overwrite it
            except OSError:
                pass

    @staticmethod
    def fields(share: Share) -> dict:
        return {
            "name": share.name, "size": share.size, "downloads": share.downloads,
            "folder": str(share.path.parent) if share.path else "",
            "source": share.source,
            "started": datetime.fromtimestamp(share.created_at).astimezone().isoformat(timespec="seconds"),
            "ended": datetime.now().astimezone().isoformat(timespec="seconds") if share.dead else "",
            "note": share.dead,
        }

    def record(self, share: Share) -> None:
        with self.lock:
            for entry in reversed(self.entries):
                if entry.get("id") == share.id:
                    entry.update(self.fields(share))
                    break
            else:
                self.entries.append({"id": share.id, **self.fields(share)})
                del self.entries[:-HISTORY_LIMIT]
            self.save()

    def clear(self) -> None:
        with self.lock:
            self.entries = []
            self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self.entries, indent=1) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass  # history is a convenience; never let it break a transfer


# --- picking files -------------------------------------------------------------------

# A page can never learn where a dropped file lives, so kit opens the system's own dialog.
WINDOWS_DIALOG = """
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Multiselect = $true
$dialog.Title = 'kit send - choose files to share'
$front = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
if ($dialog.ShowDialog($front) -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.FileNames -join \"`n\" }
$front.Dispose()
"""
MAC_DIALOG = [
    "-e", 'set chosen to choose file with prompt "kit send - choose files to share" with multiple selections allowed',
    "-e", 'set out to ""',
    "-e", "repeat with item_ in chosen",
    "-e", "set out to out & POSIX path of item_ & linefeed",
    "-e", "end repeat",
    "-e", "return out",
]


_dialog_command: list[str] | None | str = "unknown"  # looked up once: it can't change mid-run


def dialog_command() -> list[str] | None:
    """How to ask this system for files, or None when it has no file dialog."""
    global _dialog_command
    if _dialog_command != "unknown":
        return _dialog_command  # type: ignore[return-value]
    _dialog_command = _find_dialog()
    return _dialog_command


def _find_dialog() -> list[str] | None:
    if sys.platform.startswith("win"):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            return None
        # WinForms dialogs need a single-threaded apartment, which Windows PowerShell only uses with -STA.
        sta = ["-STA"] if Path(shell).name.lower() == "powershell.exe" else []
        return [shell, "-NoProfile", *sta, "-WindowStyle", "Hidden", "-Command", WINDOWS_DIALOG]
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", *MAC_DIALOG]
    if (zenity := shutil.which("zenity")):
        return [zenity, "--file-selection", "--multiple", "--separator=\n",
                "--title=kit send - choose files to share"]
    if (kdialog := shutil.which("kdialog")):
        return [kdialog, "--title", "kit send - choose files to share",
                "--getopenfilename", str(Path.home()), "--multiple", "--separate-output"]
    return None


def run_dialog() -> list[str]:
    """The files someone picked, or [] if they cancelled."""
    command = dialog_command()
    if command is None:
        raise ApiError("this system has no file dialog kit can open - use Browse instead, "
                       "or install zenity", HTTPStatus.NOT_IMPLEMENTED)
    options: dict = {"capture_output": True, "text": True, "encoding": "utf-8",
                     "errors": "replace", "timeout": DIALOG_TIMEOUT}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW  # the dialog shows; a console doesn't
    try:
        result = subprocess.run(command, **options)
    except subprocess.TimeoutExpired:
        raise ApiError("the file dialog was left open too long") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApiError(f"the file dialog didn't start: {exc}") from None
    if result.returncode != 0:
        return []  # cancelled (zenity and kdialog both exit non-zero)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def places() -> list[dict]:
    """Starting points for the file browser: the usual folders, plus drives on Windows."""
    home = Path.home()
    found: list[dict] = []
    seen: set[str] = set()
    for label, path in (("Home", home), ("Desktop", home / "Desktop"), ("Downloads", home / "Downloads"),
                        ("Documents", home / "Documents"), ("This folder", Path.cwd())):
        try:
            if path.is_dir() and str(path) not in seen:
                seen.add(str(path))
                found.append({"label": label, "path": str(path)})
        except OSError:
            continue
    if os.name == "nt" and hasattr(os, "listdrives"):
        try:
            for drive in os.listdrives():
                if drive not in seen:
                    seen.add(drive)
                    found.append({"label": drive.rstrip("\\/"), "path": drive})
        except OSError:
            pass
    return found


def browse(raw: str) -> dict:
    """One folder's contents, folders first. Listing only - files are read through /api/bytes."""
    target = Path(raw).expanduser() if str(raw).strip() else Path.home()
    try:
        target = target.resolve()
        if target.is_file():
            target = target.parent  # paste a file's path and land in its folder
        if not target.is_dir():
            raise ApiError(f"no such folder: {target}", HTTPStatus.NOT_FOUND)
        entries = []
        with os.scandir(target) as scan:
            for entry in scan:
                try:
                    is_dir = entry.is_dir()
                    stat = entry.stat()
                except OSError:
                    continue  # a broken link or a file that vanished mid-listing
                entries.append({"name": entry.name, "path": entry.path, "dir": is_dir,
                                "size": 0 if is_dir else stat.st_size, "modified": stat.st_mtime})
                if len(entries) >= BROWSE_LIMIT:
                    break
    except PermissionError:
        raise ApiError(f"{target} isn't readable", HTTPStatus.FORBIDDEN) from None
    except OSError as exc:
        raise ApiError(f"can't open that folder: {exc}") from None
    entries.sort(key=lambda entry: (not entry["dir"], entry["name"].lower()))
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "entries": entries,
        "truncated": len(entries) >= BROWSE_LIMIT,
        "places": places(),
    }


def readable_file(raw: str) -> tuple[Path, int]:
    path = Path(str(raw)).expanduser()
    try:
        path = path.resolve()
        stat = path.stat()
    except OSError as exc:
        raise ApiError(f"can't read {raw}: {exc}") from None
    if path.is_dir():
        raise ApiError(f"{path.name} is a folder - kit send takes files")
    if not path.is_file():
        raise ApiError(f"not a file: {path}")
    if not os.access(path, os.R_OK):
        raise ApiError(f"can't read {path.name}")
    return path, stat.st_size


# --- server --------------------------------------------------------------------------

def log(message: str) -> None:
    print(f"{style(time.strftime('%H:%M:%S'), 'dim')}  {message}", flush=True)


class SendServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, host: str, port: int, token: str, peer_id: str, shares: dict[str, Share],
                 history: History, defaults: dict, auto_exit: bool) -> None:
        super().__init__((host, port), Handler)
        self.token = token
        self.peer_id = peer_id
        self.shares = shares
        self.history = history
        self.defaults = defaults          # what new shares get unless the window says otherwise
        self.auto_exit = auto_exit        # started with files: stop once they are all done
        self.page = ""                    # set once main() knows which receiver page the links point at
        self.cookie_name = f"kit_send_{port}"
        self.lan = host != "127.0.0.1"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.finished = threading.Event()  # set when every share is done, so main() can stop
        self.window_session = ""           # the window that holds the dropped files
        self.dialog_lock = threading.Lock()
        self.shares_lock = threading.Lock()

    def allow_host(self, host: str) -> None:
        self.allowed_hosts.add(host)

    def live_shares(self) -> list[Share]:
        return [share for share in self.shares.values() if not share.check()]

    def new_share(self, name: str, size: int, path: Path | None, expire: float, limit: int | None) -> Share:
        share = Share(token=secrets.token_urlsafe(16), id=secrets.token_hex(6), name=name, size=size,
                      path=path, expires_at=time.time() + expire, max_downloads=limit)
        with self.shares_lock:
            self.shares[share.token] = share
        self.history.record(share)
        return share

    def sweep(self) -> None:
        """Notice shares that have just expired or hit their limit: log them once, remember them."""
        for share in list(self.shares.values()):
            reason = share.check()
            if reason and not share.logged_dead:
                share.logged_dead = True
                log(f"{style('closed', 'dim')}    {share.name}: {reason}")
                self.history.record(share)

    def check_finished(self) -> None:
        self.sweep()
        if self.auto_exit and self.shares and all(share.check() for share in self.shares.values()):
            self.finished.set()


class Handler(KitHandler, BaseHTTPRequestHandler):
    server: SendServer
    server_version = "kit-send"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        pass  # the terminal shows transfers, not requests

    # -- responses

    def send_body(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def send_page(self, status: int, name: str, extra: dict | None = None) -> None:
        html = (TOOL_DIR / name).read_text(encoding="utf-8")
        headers = {"Content-Security-Policy": PAGE_CSP, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
        self.send_body(status, html.encode("utf-8"), "text/html; charset=utf-8", {**headers, **(extra or {})})

    # -- checks

    def host_ok(self) -> bool:
        # Blocks DNS-rebinding: pages on other domains that resolve here send their own Host.
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def request_token(self) -> str:
        header = self.headers.get("X-Kit-Token")
        if header:
            return header
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(self.server.cookie_name)
        except CookieError:
            return ""
        return morsel.value if morsel else ""

    def authorized(self) -> bool:
        return hmac.compare_digest(self.request_token().encode(), self.server.token.encode())

    def share_for(self, token: str) -> Share | None:
        for share in self.server.shares.values():
            if hmac.compare_digest(share.token.encode(), token.encode()):
                return share
        return None

    def live_share(self, token: str) -> Share:
        share = self.share_for(token)
        if share is None:
            raise ApiError("unknown link", HTTPStatus.NOT_FOUND)
        if (reason := share.check()):
            raise ApiError(reason, HTTPStatus.GONE)
        return share

    # -- routes

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.host_ok():
            return self.send_error_json(HTTPStatus.FORBIDDEN, "unexpected Host header")
        url = urlparse(self.path)

        if url.path == "/":
            query_token = parse_qs(url.query).get("token", [""])[0]
            if query_token and hmac.compare_digest(query_token.encode(), self.server.token.encode()):
                # Swap the token in the address for a cookie, so it doesn't linger in the address bar.
                cookie = f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
                return self.send_body(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
            if not self.authorized():
                return self.send_page(HTTPStatus.UNAUTHORIZED, "denied.html")
            return self.send_page(HTTPStatus.OK, "sender.html")

        # The receiver page is deliberately open: its secrets live in the link's # fragment, which the
        # browser never sends here. It is the same static page that gets published for remote people.
        if url.path in ("/r", "/r/"):
            return self.send_page(HTTPStatus.OK, "receiver.html")

        if url.path in STATIC:
            if url.path in ("/sender.js",) and not self.authorized():
                return self.send_error_json(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            body = (TOOL_DIR / url.path.lstrip("/")).read_bytes()
            extra = {"Service-Worker-Allowed": "/"} if url.path == "/sw.js" else None
            return self.send_body(HTTPStatus.OK, body, STATIC[url.path], extra)

        if url.path == "/favicon.ico":
            return self.send_body(HTTPStatus.NO_CONTENT, b"", "image/x-icon")

        if not url.path.startswith("/api/"):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED,
                                        "missing or invalid token - open the address printed in the terminal")

        params = parse_qs(url.query)
        try:
            if url.path == "/api/state":
                return self.send_json(HTTPStatus.OK, self.state())
            if url.path == "/api/share":
                return self.send_json(HTTPStatus.OK, self.live_share(params.get("token", [""])[0]).state())
            if url.path == "/api/bytes":
                return self.send_bytes(params.get("token", [""])[0])
            if url.path == "/api/browse":
                return self.send_json(HTTPStatus.OK, browse(params.get("path", [""])[0]))
            if url.path == "/api/qr":
                return self.send_qr(params.get("token", [""])[0])
            if url.path == "/api/history":
                return self.send_json(HTTPStatus.OK, {"entries": list(reversed(self.server.history.entries))})
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def state(self) -> dict:
        self.server.sweep()
        return {
            "peer": self.server.peer_id,
            "slice": SLICE,
            "session": self.server.window_session,
            "defaults": self.server.defaults,
            "dialog": dialog_command() is not None,
            "page": self.server.page,
            "shares": [
                {**share.state(), "link": share_link(self.server.page, self.server.peer_id, share)}
                for share in self.server.shares.values()
            ],
        }

    def send_qr(self, token: str) -> None:
        share = self.live_share(token)
        link = share_link(self.server.page, self.server.peer_id, share)
        buffer = io.BytesIO()
        make_qr(link).save(buffer, kind="svg", scale=4, border=2, dark="#1a1918", light="#ffffff")
        self.send_body(HTTPStatus.OK, buffer.getvalue(), "image/svg+xml")

    def send_bytes(self, token: str) -> None:
        """The file itself, to the sender page only, one Range slice at a time."""
        share = self.share_for(token)
        if share is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown link")
        if (reason := share.check()):
            return self.send_error_json(HTTPStatus.GONE, reason)
        if share.path is None:
            return self.send_error_json(HTTPStatus.CONFLICT, "the window holds this file, kit doesn't")
        try:
            size = share.path.stat().st_size
        except OSError as exc:
            return self.send_error_json(HTTPStatus.GONE, f"can't read the file any more: {exc}")

        start, end = 0, size - 1
        status = HTTPStatus.OK
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not match or not (match[1] or match[2]):
                return self.send_error_json(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "bad Range")
            if match[1]:
                start = int(match[1])
                end = int(match[2]) if match[2] else size - 1
            else:  # a suffix range: the last N bytes
                start = max(0, size - int(match[2]))
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(share.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = length
        with share.path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                block = handle.read(min(1 << 20, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_POST(self) -> None:
        if not self.host_ok():
            return self.send_error_json(HTTPStatus.FORBIDDEN, "unexpected Host header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in self.server.allowed_hosts}:
            return self.send_error_json(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
        if origin is None and not self.headers.get("X-Kit-Token"):
            return self.send_error_json(HTTPStatus.FORBIDDEN, "requests without an Origin header must send X-Kit-Token")
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, "missing or invalid token")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length > MAX_BODY:
            return self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON")
        if not isinstance(body, dict):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "expected a JSON object")

        path = urlparse(self.path).path
        try:
            if path == "/api/event":
                return self.send_json(HTTPStatus.OK, self.handle_event(body))
            if path == "/api/window-hello":
                return self.send_json(HTTPStatus.OK, self.handle_hello(body))
            if path == "/api/dialog":
                return self.send_json(HTTPStatus.OK, self.handle_dialog())
            if path == "/api/add":
                return self.send_json(HTTPStatus.OK, self.handle_add(body))
            if path == "/api/add-window":
                return self.send_json(HTTPStatus.OK, self.handle_add_window(body))
            if path == "/api/options":
                return self.send_json(HTTPStatus.OK, self.handle_options(body))
            if path == "/api/remove":
                return self.send_json(HTTPStatus.OK, self.handle_remove(body))
            if path == "/api/history/clear":
                self.server.history.clear()
                return self.send_json(HTTPStatus.OK, {"entries": []})
            if path == "/api/stop":
                for share in self.server.shares.values():
                    share.dead = share.dead or "you stopped it"
                self.server.sweep()
                self.server.finished.set()
                return self.send_json(HTTPStatus.OK, {"ok": True})
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    # -- window actions

    def handle_hello(self, body: dict) -> dict:
        """The window announcing itself. A new one can't serve files an older one was holding."""
        session = str(body.get("session", ""))[:64] or secrets.token_hex(8)
        if self.server.window_session and session != self.server.window_session:
            for share in self.server.shares.values():
                if share.source == "window" and not share.dead:
                    share.dead = "the sharing window was reloaded"
            self.server.sweep()
        self.server.window_session = session
        return self.state()

    def handle_dialog(self) -> dict:
        if not self.server.dialog_lock.acquire(blocking=False):
            raise ApiError("a file dialog is already open", HTTPStatus.CONFLICT)
        try:
            return {"paths": run_dialog()}
        finally:
            self.server.dialog_lock.release()

    def options_from(self, body: dict) -> tuple[float, int | None]:
        try:
            expire = parse_expire(body.get("expire") or self.server.defaults["expire"])
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return expire, parse_limit(body.get("once"), body.get("max"))

    def handle_add(self, body: dict) -> dict:
        paths = body.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ApiError("give at least one file")
        expire, limit = self.options_from(body)
        added, errors = [], []
        for raw in paths[:200]:
            try:
                path, size = readable_file(str(raw))
            except ApiError as exc:
                errors.append(str(exc))
                continue
            share = self.server.new_share(path.name, size, path, expire, limit)
            log(f"{style('added', 'green')}     {style(share.name, 'bold')}  {style(human_bytes(size), 'dim')}")
            added.append({**share.state(), "link": share_link(self.server.page, self.server.peer_id, share)})
        if added:
            self.server.auto_exit = False  # someone is working in the window; don't pull it away
        return {"added": added, "errors": errors}

    def handle_add_window(self, body: dict) -> dict:
        """A file dropped onto the window: the page holds the bytes, kit only holds the link."""
        name = os.path.basename(str(body.get("name", "")).replace("\\", "/")).strip() or "file"
        try:
            size = int(body.get("size", 0))
        except (TypeError, ValueError):
            raise ApiError("the file size has to be a number") from None
        if size < 0 or size > MAX_WINDOW_FILE:
            raise ApiError("that file size doesn't look right")
        expire, limit = self.options_from(body)
        share = self.server.new_share(name, size, None, expire, limit)
        self.server.auto_exit = False
        log(f"{style('added', 'green')}     {style(share.name, 'bold')}  {style(human_bytes(size), 'dim')}"
            f"  {style('(held by the window)', 'dim')}")
        return {**share.state(), "link": share_link(self.server.page, self.server.peer_id, share)}

    def handle_options(self, body: dict) -> dict:
        share = self.live_share(str(body.get("token", "")))
        if share.downloads:
            raise ApiError("this link has already been used, so its limits are fixed", HTTPStatus.CONFLICT)
        expire, limit = self.options_from(body)
        share.expires_at = time.time() + expire
        share.max_downloads = limit
        self.server.history.record(share)
        return {**share.state(), "link": share_link(self.server.page, self.server.peer_id, share)}

    def handle_remove(self, body: dict) -> dict:
        share = self.share_for(str(body.get("token", "")))
        if share is None:
            raise ApiError("unknown link", HTTPStatus.NOT_FOUND)
        share.dead = share.dead or "you removed it"
        self.server.sweep()
        self.server.check_finished()
        return {"ok": True}

    def handle_event(self, body: dict) -> dict:
        """Progress reports from the sender page: what the terminal shows, and what enforces the limits."""
        share = self.share_for(str(body.get("token", "")))
        kind = str(body.get("kind", ""))
        peer = str(body.get("peer", ""))[:40]
        who = style(peer[:12] or "someone", "dim")
        if share is None:
            if kind in ("asked", "rejected"):
                log(f"{style('rejected', 'yellow')}  {who} tried a link kit doesn't know")
            return {"ok": False, "error": "unknown link"}
        name = style(share.name, "bold")

        if kind == "asked":
            reason = share.check()
            if reason:
                log(f"{style('refused', 'yellow')}   {who} asked for {name}: {reason}")
                return {"ok": False, "error": reason}
            log(f"{style('connected', 'cyan')} {who} wants {name}")
            share.active[peer] = 0
            return {"ok": True}
        if kind == "rejected":
            log(f"{style('rejected', 'yellow')}  {who} sent the wrong link token for {name}")
            return {"ok": True}
        if kind == "progress":
            share.active[peer] = int(body.get("bytes", 0))
            return {"ok": True}
        if kind == "done":
            sent = int(body.get("bytes", 0))
            seconds = float(body.get("seconds", 0)) or 0.001
            share.active.pop(peer, None)
            if body.get("verified") is False:
                log(f"{style('MISMATCH', 'bold', 'red')}  {name} arrived corrupted at {who} - nothing was saved")
            else:
                share.downloads += 1
                rate = human_bytes(sent / seconds)
                log(f"{style('sent', 'bold', 'green')}      {name} to {who}  "
                    f"{human_bytes(sent)} in {human_time(seconds)} ({rate}/s)")
            self.server.history.record(share)
            self.server.check_finished()
            return {"ok": True}
        if kind == "failed":
            share.active.pop(peer, None)
            log(f"{style('failed', 'red')}    {name} to {who}: {body.get('message', 'unknown error')}")
            return {"ok": True}
        return {"ok": False, "error": "unknown event"}


# --- start ---------------------------------------------------------------------------

def start_server(host: str, port: int, explicit: bool, token: str, peer_id: str, shares: dict[str, Share],
                 history: History, defaults: dict, auto_exit: bool) -> SendServer:
    candidates = [port] if explicit else range(port, port + PORT_ATTEMPTS)
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return SendServer(host, candidate, token, peer_id, shares, history, defaults, auto_exit)
        except OSError as exc:
            last_error = exc
    if explicit:
        die(f"can't listen on port {port}: {last_error}")
    die(f"no free port between {port} and {port + PORT_ATTEMPTS - 1}")


def lan_address() -> str | None:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # sends nothing; picks the outgoing interface
            return sock.getsockname()[0]
    except OSError:
        return None


def share_link(page: str, peer_id: str, share: Share) -> str:
    """Everything the recipient needs, after the # so it never reaches a web server."""
    return (f"{page}#i={peer_id}&t={share.token}"
            f"&n={quote(share.name, safe='')}&s={share.size}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kit send",
        description="Send a file straight to someone's browser, peer to peer.",
    )
    parser.add_argument("files", nargs="*", help="the file(s) to share; each one gets its own link. "
                                                 "Leave it out to add them in the window instead")
    parser.add_argument("--once", action="store_true", default=None,
                        help="stop the link after one completed download (setting: send.once)")
    parser.add_argument("--max", type=int, metavar="N", help="stop the link after N completed downloads")
    parser.add_argument("--expire", metavar="TIME", help="how long the link works: 2h, 90m, 30s (setting: send.expire)")
    parser.add_argument("--page", metavar="URL",
                        help="receiver page the link points at (setting: send.page; empty: this computer's own copy)")
    parser.add_argument("--lan", action="store_true", help="also let other devices on this network load the receiver page")
    parser.add_argument("--port", type=int, help=f"port to listen on (setting: send.port, default {DEFAULT_PORT})")
    parser.add_argument("--no-open", dest="open", action="store_false", help="don't open the sharing window")
    parser.add_argument("--open", dest="open", action="store_true", help="open the sharing window")
    parser.add_argument("--no-qr", dest="qr", action="store_false", help="don't print a QR code of the link")
    parser.add_argument("--window", dest="window", action="store_true", help="open in an app window (setting: send.window)")
    parser.add_argument("--no-window", dest="window", action="store_false", help="open in a normal browser tab")
    conf = tool_settings()
    parser.set_defaults(open=True, qr=True,
                        window=conf.get("window", True),
                        once=conf.get("once", False))
    args = parser.parse_args()

    if args.max is not None and args.max < 1:
        die("--max must be at least 1")
    expire_text = args.expire or conf.get("expire", "2h")
    try:
        expire_seconds = parse_expire(expire_text)
        limit = parse_limit(args.once, args.max)
    except (ValueError, ApiError) as exc:
        die(str(exc))

    shares: dict[str, Share] = {}
    history = History(data_path())
    defaults = {"expire": expire_text, "once": bool(args.once), "max": args.max}
    token = secrets.token_urlsafe(24)
    peer_id = "kit" + secrets.token_hex(12)  # the matchmaker's name for this window
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = start_server(host, args.port or conf.get("port", DEFAULT_PORT), args.port is not None,
                          token, peer_id, shares, history, defaults, auto_exit=bool(args.files))
    port = server.server_address[1]
    address = lan_address() if args.lan else None
    if address:
        server.allow_host(f"{address}:{port}")

    page = (args.page if args.page is not None else conf.get("page", "")).strip().rstrip("/")
    if not page:
        base = address or "127.0.0.1"
        page = f"http://{base}:{port}/r"
    local_only = urlparse(page).hostname in ("127.0.0.1", "localhost")
    server.page = page

    try:
        for name in args.files:
            path, size = readable_file(name)
            server.new_share(path.name, size, path, expire_seconds, limit)
    except ApiError as exc:
        die(str(exc))

    if shares:
        many = len(shares) != 1
        print(f"{style('kit send', 'bold', 'cyan')}  {len(shares)} file{'s' if many else ''}, "
              f"{'links last' if many else 'link lasts'} {human_time(expire_seconds)}"
              f"{', one download each' if limit == 1 else f', {limit} downloads each' if limit else ''}")
        for share in shares.values():
            link = share_link(page, peer_id, share)
            print(f"\n  {style(share.name, 'bold')}  {style(human_bytes(share.size), 'dim')}")
            print(f"  {style(link, 'bold')}")
            if args.qr and not local_only:
                for line in qr_lines(link):
                    print("  " + line)
    else:
        print(f"{style('kit send', 'bold', 'cyan')}  no files yet - add them in the sharing window")

    if local_only:
        print(style("\n  links only work on this computer: publish the receiver page and point at it with", "yellow"))
        print(style("  --page https://you.github.io/.../send/  (or: kit config set send.page ...)", "yellow"))
        if args.lan and address:
            print(style(f"  devices on this network can use http://{address}:{port}/r", "dim"))
    print(style("\n  the sharing window must stay open - Ctrl+C here stops sharing", "dim"))

    ui = f"http://127.0.0.1:{port}/?token={token}"
    print(style(f"  window: {ui}", "dim"), flush=True)
    if args.open:
        if args.window:
            open_app_window(ui)
        else:
            webbrowser.open(ui)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not server.finished.wait(0.5):
            server.sweep()
            if server.auto_exit and shares and all(share.check() for share in shares.values()):
                break
        time.sleep(0.3)  # let the window's last report arrive
    except KeyboardInterrupt:
        print("\nstopped sharing")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
