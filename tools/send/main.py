"""Send files to someone's browser with a link: kit serves them over your network or the internet."""

from __future__ import annotations

import argparse
import hmac
import html
import io
import itertools
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from kitlib import die, style, theme
from kitlib.browser import no_display, open_app_window
from kitlib.qr import make_qr, qr_lines
from kitlib.settings import tool_settings
from kitlib.webserver import KitHandler

from tunnel import Tunnel, TunnelError, download_cloudflared, find_cloudflared, ssl_context

TOOL_DIR = Path(os.environ.get("KIT_TOOL_DIR") or Path(__file__).resolve().parent)
DEFAULT_PORT = 8770
PORT_ATTEMPTS = 20
STATIC = {"/sender.js": "text/javascript; charset=utf-8"}
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")
DOWNLOAD_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
PING = "/_kit/ping"   # what kit fetches through a new tunnel to know it really works
LINK_RE = re.compile(r"/d/([A-Za-z0-9_-]{16,64})(/file)?/?")
BLOCK = 1 << 20
MAX_BODY = 256_000     # a dialog can hand back a lot of long paths
BROWSE_LIMIT = 2000    # entries returned for one folder
DIALOG_TIMEOUT = 600   # seconds to wait for someone to finish choosing files
HISTORY_LIMIT = 100
MAX_UPLOAD = 1 << 40
PROGRESS_EVERY = 10    # seconds between progress lines when the terminal can't redraw one


class ApiError(Exception):
    """Something the window asked for that kit can't do, with the status to answer with."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class SetupError(Exception):
    """A mode that couldn't be started (no network, no tunnel...)."""


# --- terminal ------------------------------------------------------------------------

class Console:
    """Log lines, plus one progress line at the bottom that redraws in place on a terminal."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = ""
        self.tty = sys.stdout.isatty()

    def _clear(self) -> None:
        if self.tty and self.status:
            sys.stdout.write("\r" + " " * min(len(self.status), self._width()) + "\r")

    def _width(self) -> int:
        return max(20, shutil.get_terminal_size((100, 20)).columns - 1)

    def log(self, message: str) -> None:
        with self.lock:
            self._clear()
            print(f"{style(time.strftime('%H:%M:%S'), 'dim')}  {message}", flush=True)
            if self.tty and self.status:
                sys.stdout.write(self.status[: self._width()])
                sys.stdout.flush()

    def show(self, text: str) -> None:
        """Replace the progress line (a terminal only; elsewhere progress goes through log())."""
        if not self.tty:
            return
        with self.lock:
            self._clear()
            self.status = text
            sys.stdout.write(text[: self._width()])
            sys.stdout.flush()


console = Console()
log = console.log


# --- shares --------------------------------------------------------------------------

@dataclass
class Transfer:
    id: int
    who: str
    start: int                     # first byte of this response (non-zero when a download resumes)
    total: int                     # bytes this response will carry
    sent: int = 0
    began: float = field(default_factory=time.time)

    def state(self, size: int) -> dict:
        seconds = max(time.time() - self.began, 0.1)
        return {"id": self.id, "who": self.who, "done": self.start + self.sent, "size": size,
                "rate": self.sent / seconds}


@dataclass
class Share:
    token: str          # the secret in the link
    id: str             # a name for the history file, so the link's secret isn't written to disk
    name: str
    size: int
    path: Path
    expires_at: float
    max_downloads: int | None       # None: no limit
    copied: bool = False            # a file dropped on the window, copied into a temp folder
    created_at: float = field(default_factory=time.time)
    downloads: int = 0
    active: dict[int, Transfer] = field(default_factory=dict)
    dead: str = ""                  # why it stopped working ("" while live)
    logged_dead: bool = False

    @property
    def source(self) -> str:
        return "copy" if self.copied else "path"

    def state(self) -> dict:
        return {
            "token": self.token, "name": self.name, "size": self.size,
            "downloads": self.downloads, "max": self.max_downloads,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "dead": self.dead, "source": self.source,
            "folder": "" if self.copied else str(self.path.parent),
            # options can be changed until the first download, after which they are facts
            "locked": self.downloads > 0 or bool(self.dead),
            "active": [transfer.state(self.size) for transfer in list(self.active.values())],
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

def data_dir() -> Path:
    override = os.environ.get("KIT_SEND_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit"


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
            "folder": "" if share.copied else str(share.path.parent),
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

# A page can never learn where a chosen file lives, so kit opens the system's own dialog.
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
    """One folder's contents, folders first."""
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


# --- addresses -----------------------------------------------------------------------

def lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # sends nothing; picks the outgoing interface
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith(("127.", "0.")) else address


def public_address() -> str:
    """This machine's address as the internet sees it, asked of Cloudflare."""
    try:
        request = urllib.request.Request("https://www.cloudflare.com/cdn-cgi/trace", headers={"User-Agent": "kit-send"})
        with urllib.request.urlopen(request, timeout=10, context=ssl_context()) as response:
            text = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise SetupError(f"couldn't find this machine's public address: {exc} - give it with --address") from None
    match = re.search(r"^ip=(.+)$", text, re.M)
    if not match:
        raise SetupError("couldn't find this machine's public address - give it with --address")
    address = match.group(1).strip()
    return f"[{address}]" if ":" in address else address


# --- the app: shares, and how the outside world reaches them -------------------------

class App:
    def __init__(self, history: History, defaults: dict, auto_exit: bool, port: int, explicit_port: bool,
                 internet_kind: str, address: str, cloudflared: str) -> None:
        self.shares: dict[str, Share] = {}
        self.history = history
        self.defaults = defaults          # what new shares get unless the window says otherwise
        self.auto_exit = auto_exit        # started with files: stop once they are all done
        self.port = port                  # the port links use on the LAN or a public address
        self.explicit_port = explicit_port
        self.internet_kind = internet_kind  # "tunnel" or "public"
        self.address = address            # --address: the public name to put in links
        self.cloudflared = cloudflared
        self.mode = ""                    # "lan" or "internet"
        self.status = ""                  # "" when links work; "starting" or a problem otherwise
        self.base = ""                    # what every link starts with
        self.share_server: ShareServer | None = None
        self.tunnel: Tunnel | None = None
        self.finished = threading.Event()  # set when every share is done, so main() can stop
        self.lock = threading.Lock()
        self.mode_lock = threading.Lock()
        self.dialog_lock = threading.Lock()
        self.transfer_ids = itertools.count(1)
        self.upload_dir: Path | None = None

    # -- shares

    def new_share(self, name: str, size: int, path: Path, expire: float, limit: int | None,
                  copied: bool = False) -> Share:
        share = Share(token=secrets.token_urlsafe(16), id=secrets.token_hex(6), name=name, size=size,
                      path=path, expires_at=time.time() + expire, max_downloads=limit, copied=copied)
        with self.lock:
            self.shares[share.token] = share
        self.history.record(share)
        return share

    def share_for(self, token: str) -> Share | None:
        for share in list(self.shares.values()):
            if hmac.compare_digest(share.token.encode(), token.encode()):
                return share
        return None

    def link(self, share: Share) -> str:
        return f"{self.base}/d/{share.token}" if self.base and not self.status else ""

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
        if self.auto_exit and self.shares and all(share.check() and not share.active
                                                  for share in self.shares.values()):
            self.finished.set()

    # -- modes

    def set_mode(self, mode: str) -> None:
        """Point the links at the LAN or the internet. Raises SetupError if that can't be done."""
        with self.mode_lock:
            self.mode, self.status = mode, "starting"
            self._close_outside()
            try:
                if mode == "lan":
                    self._start_lan()
                elif self.internet_kind == "public":
                    self._start_public()
                else:
                    self._start_tunnel()
            except SetupError as exc:
                self._close_outside()
                self.status = str(exc)
                raise
            self.status = ""

    def _listen(self, host: str, port: int, explicit: bool) -> ShareServer:
        candidates = [port] if explicit or port == 0 else range(port, port + PORT_ATTEMPTS)
        last: OSError | None = None
        for candidate in candidates:
            try:
                server = ShareServer((host, candidate), self)
                break
            except OSError as exc:
                last = exc
        else:
            if explicit:
                raise SetupError(f"can't listen on port {port}: {last}")
            raise SetupError(f"no free port between {port} and {port + PORT_ATTEMPTS - 1}")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.share_server = server
        return server

    def _start_lan(self) -> None:
        address = lan_address()
        if not address:
            raise SetupError("this computer isn't on a network - connect to one, or use --internet")
        server = self._listen("0.0.0.0", self.port, self.explicit_port)
        self.base = f"http://{address}:{server.server_address[1]}"

    def _start_public(self) -> None:
        address = self.address or public_address()
        server = self._listen("0.0.0.0", self.port, self.explicit_port)
        port = server.server_address[1]
        if port != self.port:
            log(style(f"port {self.port} was taken, so links use {port} - that's the one to open", "accent"))
        self.base = f"http://{address}:{port}"

    def _start_tunnel(self) -> None:
        server = self._listen("127.0.0.1", 0, False)   # only cloudflared talks to it
        server.behind_tunnel = True
        program = find_cloudflared(self.cloudflared, data_dir())
        try:
            if program is None:
                if self.cloudflared:
                    raise SetupError(f"send.cloudflared points at {self.cloudflared}, which isn't there")
                program = download_cloudflared(data_dir(), log)
            log(style("opening a tunnel through Cloudflare...", "dim"))
            tunnel = Tunnel(program, server.server_address[1])
            self.tunnel = tunnel
            self.base = tunnel.start()
            tunnel.check(PING)
        except TunnelError as exc:
            raise SetupError(str(exc)) from None

    def _close_outside(self) -> None:
        # Stops new downloads only: ones already running have their own connections and finish.
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
        if self.share_server:
            self.share_server.shutdown()
            self.share_server.server_close()
            self.share_server = None
        self.base = ""

    def close(self) -> None:
        with self.mode_lock:
            self._close_outside()
        if self.upload_dir:
            shutil.rmtree(self.upload_dir, ignore_errors=True)

    def describe_mode(self) -> str:
        if self.mode == "lan":
            return "devices on this network"
        if self.internet_kind == "public":
            return "anyone on the internet, through this machine's public address"
        return "anyone on the internet, through a Cloudflare tunnel"

    # -- progress

    def progress_line(self) -> str:
        parts = []
        for share in list(self.shares.values()):
            for transfer in list(share.active.values()):
                state = transfer.state(share.size)
                percent = 100 * state["done"] / share.size if share.size else 100
                parts.append(f"{share.name} -> {transfer.who}  {percent:.0f}%  {human_bytes(state['rate'])}/s")
        return ("  sending: " + "  |  ".join(parts)) if parts else ""


# --- the outside: download links only -----------------------------------------------

class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not sys.platform.startswith("win")

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a client (or cloudflared) went away mid-request; not worth a traceback
        super().handle_error(request, client_address)


class ShareServer(QuietServer):

    def __init__(self, address: tuple[str, int], app: App) -> None:
        super().__init__(address, ShareHandler)
        self.app = app
        self.behind_tunnel = False


class ShareHandler(KitHandler, BaseHTTPRequestHandler):
    """What the people you send to reach: a download page and the file behind each link, nothing else."""

    server: ShareServer
    server_version = "kit-send"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        pass  # the terminal shows downloads, not requests

    def who(self) -> str:
        if self.server.behind_tunnel:
            # cloudflared is the only thing that can reach a tunnel listener, so its header is trustworthy
            return self.headers.get("CF-Connecting-IP", "") or "someone"
        return self.client_address[0]

    def send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8",
                  extra: dict | None = None) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_page(self, status: int, **values: str) -> None:
        page = theme.inject((TOOL_DIR / "download.html").read_text(encoding="utf-8"))
        values.setdefault("button", "")
        values.setdefault("facts", "")
        for key, value in values.items():
            page = page.replace("{{" + key + "}}", value)
        self.send_text(status, page, "text/html; charset=utf-8",
                       {"Content-Security-Policy": DOWNLOAD_CSP, "X-Frame-Options": "DENY"})

    def refuse(self, status: int, wants_page: bool, title: str, detail: str) -> None:
        if wants_page:
            return self.send_page(status, title=html.escape(title), detail=html.escape(detail))
        return self.send_text(status, f"{title}: {detail}\n")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == PING:
            return self.send_text(HTTPStatus.OK, "ok\n")
        match = LINK_RE.fullmatch(path)
        # A browser opening the link gets a page with a Download button; curl and wget get the file.
        wants_page = "text/html" in self.headers.get("Accept", "") and not (match and match[2])
        if not match:
            return self.refuse(HTTPStatus.NOT_FOUND, wants_page, "Nothing here",
                               "Ask whoever sent you the link for the whole thing.")
        share = self.server.app.share_for(match[1])
        if share is None:
            return self.refuse(HTTPStatus.NOT_FOUND, wants_page, "This link doesn't work",
                               "It's wrong, or the sender has stopped sharing. Ask them for a new one.")
        if (reason := share.check()):
            return self.refuse(HTTPStatus.GONE, wants_page, "This link has closed",
                               reason[0].upper() + reason[1:] + ". Ask the sender for a new one.")
        if wants_page:
            return self.download_page(share)
        return self.send_file(share)

    def download_page(self, share: Share) -> None:
        facts = [f"Link works for {human_time(share.expires_at - time.time())} more"]
        if share.max_downloads is not None:
            left = share.max_downloads - share.downloads
            facts.append("one download only" if share.max_downloads == 1
                         else f"{left} download{'s' if left != 1 else ''} left")
        href = f"/d/{share.token}/file"
        self.send_page(HTTPStatus.OK, title=html.escape(share.name), detail=html.escape(human_bytes(share.size)),
                       button=f'<a class="button" href="{href}" download>Download</a>',
                       facts=html.escape(" · ".join(facts)))

    def send_file(self, share: Share) -> None:
        app = self.server.app
        try:
            size = share.path.stat().st_size
        except OSError as exc:
            return self.send_text(HTTPStatus.GONE, f"The file can't be read any more: {exc}\n")

        start, end = 0, size - 1
        status = HTTPStatus.OK
        header = self.headers.get("Range")
        if header and size:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not match or not (match[1] or match[2]):
                return self.send_text(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "bad Range\n")
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
        length = max(0, end - start + 1)
        fresh = start == 0

        transfer = None
        if self.command == "GET":
            with app.lock:
                # A limited link can't be started by more people than it has downloads left, or two
                # recipients of a --once link could both finish.
                if fresh and share.max_downloads is not None:
                    starting = sum(1 for t in share.active.values() if t.start == 0)
                    if share.downloads + starting >= share.max_downloads:
                        return self.send_text(HTTPStatus.CONFLICT,
                                              "Someone is downloading this file right now, and the link "
                                              "only allows that many downloads. Try again in a moment.\n")
                transfer = Transfer(next(app.transfer_ids), self.who(), start, length)
                share.active[transfer.id] = transfer

        ascii_name = share.name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(share.name)[0] or "application/octet-stream")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(share.name, safe='')}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if transfer is None:
            return

        who = style(transfer.who, "dim")
        name = style(share.name, "bold")
        log(f"{style('sending', 'accent')}   {name} to {who}" if fresh
            else f"{style('resuming', 'accent')}  {name} to {who} from {100 * start / max(size, 1):.0f}%")
        try:
            with share.path.open("rb") as handle:
                handle.seek(start)
                while transfer.sent < length:
                    block = handle.read(min(BLOCK, length - transfer.sent))
                    if not block:
                        break
                    self.wfile.write(block)
                    transfer.sent += len(block)
            self.wfile.flush()
        except OSError:
            pass  # the recipient went away; said below
        finally:
            with app.lock:
                share.active.pop(transfer.id, None)

        seconds = max(time.time() - transfer.began, 0.001)
        if transfer.sent == length and end == size - 1:
            share.downloads += 1
            log(f"{style('sent', 'bold', 'good')}      {name} to {who}  {human_bytes(transfer.sent)} "
                f"in {human_time(seconds)} ({human_bytes(transfer.sent / seconds)}/s)")
            app.history.record(share)
        elif transfer.sent < length:
            log(f"{style('stopped', 'accent')}   {name} to {who} at "
                f"{100 * (start + transfer.sent) / max(size, 1):.0f}% - they can resume it")
        app.check_finished()


# --- the inside: the sharing window and its API ------------------------------------

class ControlServer(QuietServer):

    def __init__(self, app: App, token: str) -> None:
        super().__init__(("127.0.0.1", 0), ControlHandler)
        port = self.server_address[1]
        self.app = app
        self.token = token
        self.cookie_name = f"kit_send_{port}"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


class ControlHandler(KitHandler, BaseHTTPRequestHandler):
    server: ControlServer
    server_version = "kit-send"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        pass

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
        page = theme.inject((TOOL_DIR / name).read_text(encoding="utf-8"))
        headers = {"Content-Security-Policy": PAGE_CSP, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
        self.send_body(status, page.encode("utf-8"), "text/html; charset=utf-8", {**headers, **(extra or {})})

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

    def live_share(self, token: str) -> Share:
        share = self.server.app.share_for(token)
        if share is None:
            raise ApiError("unknown link", HTTPStatus.NOT_FOUND)
        if (reason := share.check()):
            raise ApiError(reason, HTTPStatus.GONE)
        return share

    def with_link(self, share: Share) -> dict:
        return {**share.state(), "link": self.server.app.link(share)}

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

        if url.path in STATIC:
            if not self.authorized():
                return self.send_error_json(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            body = (TOOL_DIR / url.path.lstrip("/")).read_bytes()
            return self.send_body(HTTPStatus.OK, body, STATIC[url.path])

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
            if url.path == "/api/browse":
                return self.send_json(HTTPStatus.OK, browse(params.get("path", [""])[0]))
            if url.path == "/api/qr":
                return self.send_qr(params.get("token", [""])[0])
            if url.path == "/api/history":
                return self.send_json(HTTPStatus.OK, {"entries": list(reversed(self.server.app.history.entries))})
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def state(self) -> dict:
        app = self.server.app
        app.sweep()
        return {
            "mode": app.mode, "kind": app.internet_kind, "status": app.status, "base": app.base,
            "reach": app.describe_mode(),
            "defaults": app.defaults,
            "dialog": dialog_command() is not None,
            "shares": [self.with_link(share) for share in list(app.shares.values())],
        }

    def send_qr(self, token: str) -> None:
        link = self.server.app.link(self.live_share(token))
        if not link:
            raise ApiError("the link isn't ready yet", HTTPStatus.CONFLICT)
        buffer = io.BytesIO()
        make_qr(link).save(buffer, kind="svg", scale=4, border=2, dark=theme.BG, light="#ffffff")
        self.send_body(HTTPStatus.OK, buffer.getvalue(), "image/svg+xml")

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

        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if url.path == "/api/upload":
            try:
                return self.send_json(HTTPStatus.OK, self.handle_upload(parse_qs(url.query), length))
            except ApiError as exc:
                self.close_connection = True   # the rest of the body may still be on its way
                return self.send_error_json(exc.status, str(exc))

        if length > MAX_BODY:
            return self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON")
        if not isinstance(body, dict):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "expected a JSON object")

        app = self.server.app
        try:
            if url.path == "/api/dialog":
                return self.send_json(HTTPStatus.OK, self.handle_dialog())
            if url.path == "/api/add":
                return self.send_json(HTTPStatus.OK, self.handle_add(body))
            if url.path == "/api/options":
                return self.send_json(HTTPStatus.OK, self.handle_options(body))
            if url.path == "/api/remove":
                return self.send_json(HTTPStatus.OK, self.handle_remove(body))
            if url.path == "/api/mode":
                return self.send_json(HTTPStatus.OK, self.handle_mode(body))
            if url.path == "/api/history/clear":
                app.history.clear()
                return self.send_json(HTTPStatus.OK, {"entries": []})
            if url.path == "/api/stop":
                for share in list(app.shares.values()):
                    share.dead = share.dead or "you stopped it"
                app.sweep()
                app.finished.set()
                return self.send_json(HTTPStatus.OK, {"ok": True})
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    # -- window actions

    def handle_dialog(self) -> dict:
        lock = self.server.app.dialog_lock
        if not lock.acquire(blocking=False):
            raise ApiError("a file dialog is already open", HTTPStatus.CONFLICT)
        try:
            return {"paths": run_dialog()}
        finally:
            lock.release()

    def options_from(self, body: dict) -> tuple[float, int | None]:
        try:
            expire = parse_expire(body.get("expire") or self.server.app.defaults["expire"])
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        return expire, parse_limit(body.get("once"), body.get("max"))

    def handle_add(self, body: dict) -> dict:
        app = self.server.app
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
            share = app.new_share(path.name, size, path, expire, limit)
            log(f"{style('added', 'good')}     {style(share.name, 'bold')}  {style(human_bytes(size), 'dim')}")
            added.append(self.with_link(share))
        if added:
            app.auto_exit = False  # someone is working in the window; don't pull it away
        return {"added": added, "errors": errors}

    def handle_upload(self, params: dict, length: int) -> dict:
        """A file dropped on the window. The page can't say where it lives, so it hands kit a copy."""
        app = self.server.app
        name = os.path.basename(params.get("name", [""])[0].replace("\\", "/")).strip() or "file"
        if length <= 0 or length > MAX_UPLOAD:
            raise ApiError("that file is empty or too big")
        body = {"expire": params.get("expire", [""])[0], "once": params.get("once", [""])[0] == "1",
                "max": params.get("max", [""])[0]}
        expire, limit = self.options_from(body)
        if app.upload_dir is None:
            app.upload_dir = Path(tempfile.mkdtemp(prefix="kit-send-"))
        folder = app.upload_dir / secrets.token_hex(6)
        folder.mkdir()
        target = folder / name
        remaining = length
        try:
            with target.open("wb") as out:
                while remaining > 0:
                    block = self.rfile.read(min(BLOCK, remaining))
                    if not block:
                        break
                    out.write(block)
                    remaining -= len(block)
        except OSError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise ApiError(f"couldn't copy {name}: {exc}", HTTPStatus.INSUFFICIENT_STORAGE) from None
        if remaining:
            shutil.rmtree(folder, ignore_errors=True)
            raise ApiError(f"{name} didn't arrive whole")
        share = app.new_share(name, length, target, expire, limit, copied=True)
        app.auto_exit = False
        log(f"{style('added', 'good')}     {style(share.name, 'bold')}  {style(human_bytes(length), 'dim')}"
            f"  {style('(dropped on the window)', 'dim')}")
        return self.with_link(share)

    def handle_options(self, body: dict) -> dict:
        share = self.live_share(str(body.get("token", "")))
        if share.downloads:
            raise ApiError("this link has already been used, so its limits are fixed", HTTPStatus.CONFLICT)
        expire, limit = self.options_from(body)
        share.expires_at = time.time() + expire
        share.max_downloads = limit
        self.server.app.history.record(share)
        return self.with_link(share)

    def handle_remove(self, body: dict) -> dict:
        app = self.server.app
        share = app.share_for(str(body.get("token", "")))
        if share is None:
            raise ApiError("unknown link", HTTPStatus.NOT_FOUND)
        share.dead = share.dead or "you removed it"
        app.sweep()
        app.check_finished()
        return {"ok": True}

    def handle_mode(self, body: dict) -> dict:
        app = self.server.app
        mode = str(body.get("mode", ""))
        if mode not in ("lan", "internet"):
            raise ApiError("mode has to be lan or internet")
        if mode != app.mode or app.status not in ("", "starting"):
            app.mode, app.status = mode, "starting"   # shown straight away; set_mode does the work

            def switch() -> None:
                try:
                    app.set_mode(mode)
                    log(f"{style('links', 'accent')}     now reach {app.describe_mode()}")
                except SetupError as exc:
                    log(style(f"couldn't switch: {exc}", "bad"))
            threading.Thread(target=switch, daemon=True).start()
        return self.state()


# --- start ---------------------------------------------------------------------------

def print_links(app: App, show_qr: bool) -> None:
    for share in list(app.shares.values()):
        link = app.link(share)
        print(f"\n  {style(share.name, 'bold')}  {style(human_bytes(share.size), 'dim')}")
        print(f"  {style(link, 'bold')}")
        if show_qr:
            for line in qr_lines(link):
                print("  " + line)
    if app.shares:
        print(style("\n  a browser gets a Download button; from a terminal: curl -OJ LINK  or  wget --content-disposition LINK",
                    "dim"))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kit send",
        description="Send files to someone's browser with a link, over your network or the internet.",
    )
    parser.add_argument("files", nargs="*", help="the file(s) to share; each one gets its own link. "
                                                 "Leave it out to add them in the window instead")
    parser.add_argument("--once", action="store_true", default=None,
                        help="stop the link after one completed download (setting: send.once)")
    parser.add_argument("--max", type=int, metavar="N", help="stop the link after N completed downloads")
    parser.add_argument("--expire", metavar="TIME", help="how long the link works: 2h, 90m, 30s (setting: send.expire)")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--lan", dest="mode", action="store_const", const="lan",
                       help="links work for devices on this network (setting: send.mode)")
    where.add_argument("--internet", dest="mode", action="store_const", const="internet",
                       help="links work from anywhere, through a free Cloudflare tunnel")
    where.add_argument("--public", action="store_true",
                       help="links work from anywhere, through this machine's own public address "
                            "(the port has to be open to the internet)")
    parser.add_argument("--address", metavar="HOST", default="",
                        help="with --public: the name or address to put in links (default: looked up)")
    parser.add_argument("--port", type=int, help=f"port the links use on the LAN or with --public "
                                                 f"(setting: send.port, default {DEFAULT_PORT})")
    parser.add_argument("--headless", action="store_true",
                        help="no window, just the terminal (automatic over SSH or without a display)")
    parser.add_argument("--no-open", dest="open", action="store_false", help="don't open the sharing window")
    parser.add_argument("--open", dest="open", action="store_true",
                        help="open the sharing window, even where kit would pick the terminal")
    parser.add_argument("--no-qr", dest="qr", action="store_false", help="don't print QR codes of the links")
    parser.add_argument("--window", dest="window", action="store_true", help="open in an app window (setting: send.window)")
    parser.add_argument("--no-window", dest="window", action="store_false", help="open in a normal browser tab")
    conf = tool_settings()
    parser.set_defaults(open=None, qr=True, mode=None,
                        window=conf.get("window", True),
                        once=conf.get("once", False))
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")   # a file name the console can't show mustn't crash kit

    if args.max is not None and args.max < 1:
        die("--max must be at least 1")
    if args.address and not args.public:
        die("--address goes with --public")
    expire_text = args.expire or conf.get("expire", "2h")
    try:
        expire_seconds = parse_expire(expire_text)
        limit = parse_limit(args.once, args.max)
    except (ValueError, ApiError) as exc:
        die(str(exc))

    headless = args.headless or (args.open is None and no_display())
    if headless and not args.files:
        die("there's no window here" + ("" if args.headless else " (this looks like an SSH session, or no display)")
            + ", so name the files to share: kit send FILE [FILE...]")

    mode = "internet" if args.public else (args.mode or conf.get("mode", "lan"))
    app = App(History(data_dir() / "send-history.json"),
              {"expire": expire_text, "once": bool(args.once), "max": args.max},
              auto_exit=bool(args.files), port=args.port or conf.get("port", DEFAULT_PORT),
              explicit_port=args.port is not None, internet_kind="public" if args.public else "tunnel",
              address=args.address, cloudflared=conf.get("cloudflared", ""))

    try:
        for name in args.files:
            path, size = readable_file(name)
            app.new_share(path.name, size, path, expire_seconds, limit)
    except ApiError as exc:
        die(str(exc))

    control = None
    if not headless:
        control = ControlServer(app, secrets.token_urlsafe(24))
        threading.Thread(target=control.serve_forever, daemon=True).start()
        ui = f"http://127.0.0.1:{control.server_address[1]}/?token={control.token}"
        # First URL printed: `kit share` takes it as the tool's address.
        print(f"{style('kit send', 'bold', 'accent')}  window: {ui}", flush=True)
        if args.open is not False:
            opened = True
            if args.window:
                open_app_window(ui)
            else:
                opened = webbrowser.open(ui)
            if not opened:
                print(style("  couldn't open a browser - open the address above yourself", "accent"))

    try:
        try:
            app.set_mode(mode)
        except SetupError as exc:
            if headless or args.files:
                die(str(exc))
            log(style(f"links can't reach {mode} yet: {exc} - try again from the window", "bad"))

        if headless:
            print(f"{style('kit send', 'bold', 'accent')}  links reach {app.describe_mode()}")
        if app.shares and app.base:
            many = len(app.shares) != 1
            print(f"{style('kit send', 'bold', 'accent')}  {len(app.shares)} file{'s' if many else ''}, "
                  f"{'links last' if many else 'link lasts'} {human_time(expire_seconds)}"
                  f"{', one download each' if limit == 1 else f', {limit} downloads each' if limit else ''}")
            print_links(app, args.qr)
        elif not app.shares:
            print("  no files yet - add them in the sharing window")
        if app.internet_kind == "public" and app.mode == "internet":
            port = app.base.rsplit(":", 1)[-1]
            print(style(f"\n  --public: port {port} has to be open in this machine's firewall "
                        "(and forwarded by the router, if there is one)", "accent"))
        print(style("\n  kit has to keep running while people download - Ctrl+C here stops sharing"
                    + ("" if headless else ", and so does Stop in the window"), "dim"), flush=True)

        last_line = time.time()
        while not app.finished.wait(0.5):
            app.check_finished()
            line = app.progress_line()
            if console.tty:
                console.show(line)
            elif line and time.time() - last_line >= PROGRESS_EVERY:
                last_line = time.time()
                log(line.strip())
        console.show("")
        time.sleep(0.3)  # let the last response finish writing
    except KeyboardInterrupt:
        console.show("")
        print("\nstopped sharing")
    finally:
        app.close()
        if control:
            control.shutdown()
            control.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
