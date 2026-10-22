"""Receive files from someone's browser: send them a link and a code, and what they upload lands in a folder here."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kitlib import die, reach, style, theme, warn
from kitlib.qr import qr_lines
from kitlib.settings import tool_settings
from kitlib.webserver import KitHandler


TOOL_DIR = Path(os.environ.get("KIT_TOOL_DIR") or Path(__file__).resolve().parent)
DEFAULT_PORT = 8771
PORT_ATTEMPTS = 20
PING = "/_kit/ping"   # what kit fetches through a new tunnel to know it really works
LINK_RE = re.compile(r"/u/([A-Za-z0-9_-]{16,64})(/[a-z.]*)?")
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
NOTICE_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
BLOCK = 1 << 20
CHUNK = 8 << 20          # what the page sends at first; it adapts to the connection from there
CHUNK_LIMIT = 64 << 20   # largest piece kit takes in one request (Cloudflare's free tunnels stop at 100 MB)
MAX_JSON = 256_000
MAX_FILES = 500          # in one go
CODE_TRIES = 5           # wrong codes, from anyone, before the link closes
STALL = 120              # seconds an unfinished upload may go quiet after the link closes, before kit gives up
QUIET = 30               # seconds without data before an upload counts as paused
PROGRESS_EVERY = 10      # seconds between progress lines when the terminal can't redraw one
# Programs, scripts and containers: harmless until opened, and the files most worth a second look.
RISKY = {".exe", ".msi", ".msix", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".dll", ".sys", ".ps1",
         ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".url", ".reg", ".jar",
         ".sh", ".run", ".bin", ".appimage", ".deb", ".rpm", ".apk", ".app", ".pkg", ".dmg", ".iso", ".img",
         ".vhd", ".vhdx", ".docm", ".xlsm", ".pptm", ".one"}
WINDOWS_RESERVED = re.compile(r"(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?", re.I)


class ApiError(Exception):
    """A request kit refuses, with the status to answer with and anything the page needs to know."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


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


# --- sizes, times and names ----------------------------------------------------------

DURATION_RE = re.compile(r"(?:(?P<h>\d+(?:\.\d+)?)\s*h)?\s*(?:(?P<m>\d+(?:\.\d+)?)\s*m)?\s*(?:(?P<s>\d+(?:\.\d+)?)\s*s)?$", re.I)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?", re.I)


def parse_expire(text: str) -> float:
    """'1h', '90m', '30s', '1h30m' or a bare number of minutes -> seconds."""
    value = str(text).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        seconds = float(value) * 60
    elif (match := DURATION_RE.fullmatch(value)) and any(match.groupdict().values()):
        seconds = float(match["h"] or 0) * 3600 + float(match["m"] or 0) * 60 + float(match["s"] or 0)
    else:
        raise ValueError(f"not a duration: '{text}' (try 1h, 30m or 2h30m)")
    if seconds <= 0:
        raise ValueError("the expiry must be more than zero")
    return seconds


def parse_size(text: str) -> int | None:
    """'2GB', '500 MB', '1.5g', '700k' or a bare number of bytes -> bytes; '' or '0' -> None (no limit)."""
    value = str(text or "").strip()
    if value in ("", "0"):
        return None
    match = SIZE_RE.fullmatch(value)
    if not match:
        raise ValueError(f"not a size: '{text}' (try 2GB, 500MB or 700KB)")
    size = int(float(match[1]) * 1024 ** "_kmgt".index((match[2] or "_").lower()))
    if size <= 0:
        raise ValueError("the size limit must be more than zero")
    return size


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


def clean_name(raw: object) -> str:
    """A file name that's safe to create in the folder on Windows and Linux: no folders, no characters
    either system refuses, no device names, not too long. Never empty."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if ch in '<>:"|?*' or ord(ch) < 32 or ord(ch) == 127 else ch for ch in name)
    name = name.strip().rstrip(". ")    # Windows drops trailing dots and spaces; "." and ".." end up empty
    if WINDOWS_RESERVED.fullmatch(name):
        name = "_" + name
    stem, ext = os.path.splitext(name)
    if len(ext) > 16:
        stem, ext = name, ""
    while len((stem + ext).encode("utf-8")) > 200:
        stem = stem[:-1]
    return (stem.rstrip(". ") + ext) or "file"


def free_name(folder: Path, name: str) -> Path:
    """name in folder, or 'name (2).ext', 'name (3).ext'... if that's taken."""
    stem, ext = os.path.splitext(name)
    candidate, number = folder / name, 1
    while candidate.exists():
        number += 1
        candidate = folder / f"{stem} ({number}){ext}"
    return candidate


def place(part: Path, target: Path) -> bool:
    """Give the finished file its name without ever replacing a file that has it. False if it's taken."""
    try:
        if os.name == "nt":
            os.rename(part, target)       # refuses an existing target on Windows
        else:
            os.link(part, target)         # refuses one everywhere; rename would replace it
            os.unlink(part)
        return True
    except FileExistsError:
        return False
    except OSError:
        # FAT and exFAT drives can't make links: check, then rename
        if target.exists():
            return False
        os.rename(part, target)
        return True


def mark_downloaded(path: Path, source: str) -> None:
    """Windows' "this came from the internet" mark, which a browser download gets too: Windows then asks
    before running a program, and Office opens documents in Protected View. NTFS only; elsewhere a no-op."""
    if os.name != "nt":
        return
    try:
        with open(f"{path}:Zone.Identifier", "wb") as stream:
            stream.write(f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={source}\r\n".encode("ascii", "replace"))
    except OSError:
        pass  # FAT/exFAT and network drives have no room for it


def default_folder() -> Path:
    return Path.home() / "Downloads" / "kit-received"


# --- uploads -------------------------------------------------------------------------

@dataclass
class Upload:
    key: str            # the same file (name, size, date) always gets the same key, so it can resume
    name: str
    size: int
    part: Path          # where it's written until the last byte arrives
    batch: int          # which "send" on the page brought it, for --once
    who: str
    received: int = 0
    saved: Path | None = None
    busy: bool = False                  # a piece of it is arriving right now
    last_seen: float = field(default_factory=time.time)
    flowing: bool = False               # logged as receiving, and not since stopped
    flow_began: float = 0.0
    flow_start: int = 0

    def rate(self) -> float:
        return (self.received - self.flow_start) / max(time.time() - self.flow_began, 0.1)


def part_path(folder: Path, name: str, key: str) -> Path:
    return folder / f"{name}.{key[:8]}.kit-part"


class Receiver:
    """One link: who may upload, what has arrived, and when it closes."""

    def __init__(self, folder: Path, code: str, expire: float, once: bool, max_size: int | None) -> None:
        self.folder = folder
        self.token = secrets.token_urlsafe(16)   # the secret in the link
        self.code = code                          # "" when --no-code
        self.expires_at = time.time() + expire
        self.once = once
        self.max_size = max_size
        self.base = ""
        self.behind_tunnel = False
        self.sessions: set[str] = set()
        self.wrong = 0
        self.dead = ""                  # set when the link closes early (too many wrong codes)
        self.uploads: dict[str, Upload] = {}
        self.batches = 0
        self.lock = threading.Lock()    # uploads, sessions and the counters above
        self.place_lock = threading.Lock()
        self.finished = threading.Event()
        self.why_finished = ""

    @property
    def link(self) -> str:
        return f"{self.base}/u/{self.token}/"

    def token_ok(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.token.encode())

    def over(self) -> str:
        """Why the link has stopped taking anything new, whatever its uploads are doing."""
        if self.dead:
            return self.dead
        if time.time() >= self.expires_at:
            return "the link has expired"
        return ""

    def closed(self) -> str:
        """Why new files can't be sent any more, or "" while they can."""
        if (reason := self.over()):
            return reason
        if self.once and self.batches:
            return "the link was for one upload, and it has been used"
        return ""

    def unfinished(self) -> list[Upload]:
        return [upload for upload in list(self.uploads.values()) if upload.saved is None]

    def recently_active(self) -> bool:
        return any(time.time() - upload.last_seen < STALL for upload in self.unfinished())

    def open_for_page(self) -> str:
        """"" if someone opening the link can still do something there: send, or finish sending."""
        if self.dead:
            return self.dead
        if self.once and self.batches and not self.unfinished():
            return "the link was for one upload, and it has been used"
        if self.over() and not self.recently_active():
            return self.over()
        return ""

    # -- the code

    def sign_in(self, code: str, who: str) -> str:
        """A session for the page, if the code is right."""
        if self.finished.is_set() or self.dead:
            raise ApiError(self.dead or "the link has closed", HTTPStatus.GONE)
        if (reason := self.open_for_page()):
            raise ApiError(reason, HTTPStatus.GONE)
        typed = re.sub(r"\D", "", str(code))
        with self.lock:
            if self.code and not hmac.compare_digest(typed.encode(), self.code.encode()):
                self.wrong += 1
                left = CODE_TRIES - self.wrong
                if left <= 0:
                    self.dead = "too many wrong codes"
                    log(style(f"closed    {CODE_TRIES} wrong codes - someone may be guessing. Nothing new can be "
                              "sent; run kit receive again for a new link and code", "bad"))
                    raise ApiError("That's not the code, and it was the last try: the link has closed. "
                                   "Ask for a new one.", HTTPStatus.GONE)
                log(f"{style('wrong code', 'bad')} from {style(who, 'dim')}  ({left} {'try' if left == 1 else 'tries'} left)")
                raise ApiError("That's not the code.", HTTPStatus.FORBIDDEN, left=left)
            session = secrets.token_urlsafe(24)
            self.sessions.add(session)
        if self.code:
            log(f"{style('code ok', 'good')}   from {style(who, 'dim')}")
        return session

    def session_ok(self, session: str) -> bool:
        return bool(session) and session in self.sessions

    # -- starting

    def start(self, files: object, who: str) -> list[dict]:
        """Register what the page is about to send; say how much of each has already arrived."""
        if not isinstance(files, list) or not files:
            raise ApiError("choose at least one file")
        if len(files) > MAX_FILES:
            raise ApiError(f"that's more than {MAX_FILES} files at once")
        wanted, order = [], []   # each file once; and which one every entry asked about
        for item in files:
            if not isinstance(item, dict):
                raise ApiError("bad file entry")
            name = clean_name(item.get("name"))
            try:
                size = int(item.get("size"))
                modified = int(float(item.get("modified") or 0))
            except (TypeError, ValueError):
                raise ApiError(f"bad size for {name}") from None
            if size < 0 or size > 1 << 44:
                raise ApiError(f"bad size for {name}")
            if self.max_size is not None and size > self.max_size:
                raise ApiError(f"{name} is {human_bytes(size)}, and this link takes files up to "
                               f"{human_bytes(self.max_size)}", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            key = hashlib.sha256(f"{name}\0{size}\0{modified}".encode()).hexdigest()[:32]
            if key not in order:   # the same file picked twice goes once
                wanted.append((key, name, size))
            order.append(key)

        with self.lock:
            fresh = [entry for entry in wanted if entry[0] not in self.uploads]
            if fresh and (reason := self.closed()):
                raise ApiError(reason[0].upper() + reason[1:] + ", so nothing new can be sent.", HTTPStatus.GONE)
            needed = sum(u.size - u.received for u in self.unfinished())
            for key, name, size in fresh:
                part = part_path(self.folder, name, key)
                needed += size - (part.stat().st_size if part.is_file() else 0)
            self.check_room(needed)
            if fresh:
                self.batches += 1
            for key, name, size in fresh:
                part = part_path(self.folder, name, key)
                upload = Upload(key, name, size, part, self.batches, who)
                if part.is_file():
                    upload.received = part.stat().st_size
                    if upload.received > size:   # not this file after all: start it again
                        part.unlink()
                        upload.received = 0
                self.uploads[key] = upload
            for key, _name, _size in wanted:
                self.uploads[key].last_seen = time.time()
                self.uploads[key].who = who
        for key, _name, _size in fresh:
            if self.uploads[key].size == 0:
                self.finish(self.uploads[key])   # nothing to wait for
        return [{"id": key, "received": self.uploads[key].received, "done": self.uploads[key].saved is not None}
                for key in order]

    def check_room(self, needed: int) -> None:
        usage = shutil.disk_usage(self.folder)
        keep = min(1 << 30, usage.total // 20)   # never fill the disk to the brim
        room = max(0, usage.free - keep)
        if needed > room:
            raise ApiError(f"There isn't room on the receiving computer: this needs {human_bytes(needed)}, "
                           f"and {human_bytes(room)} is free.", HTTPStatus.INSUFFICIENT_STORAGE)

    # -- the data

    def claim(self, upload: Upload, offset: int, length: int, who: str) -> None:
        """Check a piece can be taken, before any of it is read, and hold the file for it."""
        with self.lock:
            if upload.saved is not None:
                raise ApiError("already received", HTTPStatus.CONFLICT, received=upload.size, done=True)
            if upload.busy:
                raise ApiError("a piece of this file is still arriving", HTTPStatus.CONFLICT,
                               received=upload.received, busy=True)
            if offset != upload.received:
                raise ApiError("out of step", HTTPStatus.CONFLICT, received=upload.received)
            if length <= 0 or length > CHUNK_LIMIT or offset + length > upload.size:
                raise ApiError("bad piece size")
            upload.busy = True
            upload.who = who
            if not upload.flowing:
                upload.flowing, upload.flow_began, upload.flow_start = True, time.time(), upload.received
                name, who_ = style(upload.name, "bold"), style(who, "dim")
                log(f"{style('receiving', 'accent')} {name}  {style(human_bytes(upload.size), 'dim')} from {who_}"
                    if upload.received == 0 else
                    f"{style('resuming', 'accent')}  {name} from {who_} at {100 * upload.received / upload.size:.0f}%")

    def write(self, upload: Upload, length: int, stream) -> None:
        """Append a claimed piece, read from the request as it arrives."""
        cut = False
        try:
            with upload.part.open("ab") as out:
                remaining = length
                while remaining > 0:
                    block = stream.read(min(BLOCK, remaining))
                    if not block:
                        cut = True
                        break
                    out.write(block)
                    remaining -= len(block)
                    upload.received += len(block)
                    upload.last_seen = time.time()
        except (ConnectionError, TimeoutError):
            cut = True
        except OSError as exc:
            log(style(f"couldn't write {upload.name}: {exc}", "bad"))
            raise ApiError(f"The receiving computer couldn't save it: {exc.strerror or exc}",
                           HTTPStatus.INSUFFICIENT_STORAGE, received=upload.received) from None
        finally:
            upload.busy = False
        if cut:
            self.stopped(upload)
            raise ApiError("the piece didn't arrive whole", HTTPStatus.BAD_REQUEST, received=upload.received)
        if upload.received >= upload.size:
            self.finish(upload)

    def stopped(self, upload: Upload) -> None:
        if upload.flowing and upload.saved is None:
            upload.flowing = False
            log(f"{style('paused', 'accent')}    {upload.name} at {100 * upload.received / max(upload.size, 1):.0f}%"
                " - they can carry on from there")

    def finish(self, upload: Upload) -> None:
        if upload.size == 0:
            upload.part.touch()
        elif upload.part.stat().st_size != upload.size:
            raise ApiError("the file on disk isn't the size it should be - send it again", HTTPStatus.CONFLICT,
                           received=upload.part.stat().st_size)
        with self.place_lock:
            target = free_name(self.folder, upload.name)
            while not place(upload.part, target):
                target = free_name(self.folder, upload.name)
        mark_downloaded(target, self.base)
        upload.saved = target
        upload.flowing = False
        seconds = max(time.time() - (upload.flow_began or time.time()), 0.001)
        moved = upload.size - upload.flow_start
        speed = f" in {human_time(seconds)} ({human_bytes(moved / seconds)}/s)" if moved and upload.flow_began else ""
        log(f"{style('received', 'bold', 'good')}  {style(target.name, 'bold')}  {human_bytes(upload.size)} "
            f"from {style(upload.who, 'dim')}{speed}")
        if target.suffix.lower() in RISKY:
            log(style(f"          {target.name} is a program, script or disk image - only open it if you trust "
                      "whoever sent it", "accent"))

    # -- the end

    def tick(self) -> None:
        """Pause quiet uploads, and decide whether everything is over."""
        now = time.time()
        for upload in list(self.unfinished()):
            if upload.flowing and not upload.busy and now - upload.last_seen > QUIET:
                self.stopped(upload)
        if self.finished.is_set():
            return
        if self.once and self.batches and not self.unfinished():
            self.why_finished = "everything arrived, and the link was for one upload"
        elif self.over() and not self.recently_active():
            # uploads still going when the link expired were let finish; ones that went quiet aren't waited for
            self.why_finished = self.over()
        else:
            return
        self.finished.set()

    def progress_line(self) -> str:
        parts = [f"{u.name} <- {u.who}  {100 * u.received / max(u.size, 1):.0f}%  {human_bytes(u.rate())}/s"
                 for u in self.unfinished() if u.flowing]
        return ("  receiving: " + "  |  ".join(parts)) if parts else ""


# --- the web side --------------------------------------------------------------------

class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, address: tuple[str, int], receiver: Receiver) -> None:
        super().__init__(address, ReceiveHandler)
        self.receiver = receiver

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a browser (or cloudflared) went away mid-request; not worth a traceback
        super().handle_error(request, client_address)


class ReceiveHandler(KitHandler, BaseHTTPRequestHandler):
    """The upload page and what it talks to - nothing else on this machine is reachable through it."""

    server: QuietServer
    server_version = "kit-receive"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        pass  # the terminal shows uploads, not requests

    def who(self) -> str:
        if self.server.receiver.behind_tunnel:
            # cloudflared is the only thing that can reach a tunnel listener, so its header is trustworthy
            return self.headers.get("CF-Connecting-IP", "") or "someone"
        return self.client_address[0]

    # -- responses

    def send_body(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")   # the link's secret is in every address
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def send_template(self, status: int, name: str, csp: str, **values: str) -> None:
        page = theme.inject((TOOL_DIR / name).read_text(encoding="utf-8"))
        for key, value in values.items():
            page = page.replace("{{" + key + "}}", value)
        self.send_body(status, page.encode("utf-8"), "text/html; charset=utf-8",
                       {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})

    def notice(self, status: int, title: str, detail: str) -> None:
        self.send_template(status, "notice.html", NOTICE_CSP, title=html.escape(title), detail=html.escape(detail))

    def handle_one_request(self) -> None:
        self.body_read = False
        self.must_close = False
        super().handle_one_request()
        if self.must_close:
            self.close_connection = True   # after KitHandler, which would otherwise keep it open

    def fail(self, exc: ApiError) -> None:
        if not self.body_read:
            # Unread, the body would be taken for the next request. A small one is read and dropped, so
            # the answer arrives for sure; a big piece isn't worth the wait, so the connection is closed
            # (the page then asks where things stand before it goes on).
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if 0 < length <= 2 * BLOCK:
                self.rfile.read(length)
            elif length:
                self.must_close = True
        self.send_json(exc.status, {"error": str(exc), **exc.extra})

    # -- routes

    def route(self) -> str | None:
        """What's asked for under a valid link ("" for the page), or None after answering for a bad one."""
        path = urlparse(self.path).path
        if path == PING:
            self.send_body(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            return None
        match = LINK_RE.fullmatch(path)
        if not match or not self.server.receiver.token_ok(match[1]):
            if path == "/favicon.ico":
                self.send_body(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            elif self.command in ("GET", "HEAD"):
                self.notice(HTTPStatus.NOT_FOUND, "This link doesn't work",
                            "It's wrong, or it has been replaced by a new one. Ask whoever sent it.")
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "this link doesn't work"})
            return None
        return match[2] or ""

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        route = self.route()
        if route is None:
            return
        receiver = self.server.receiver
        if route == "":
            # the page's own requests are relative to it, so it has to be opened as a folder
            location = urlparse(self.path).path + "/"
            return self.send_body(HTTPStatus.PERMANENT_REDIRECT, b"", "text/plain", {"Location": location})
        if route == "/":
            if (reason := receiver.open_for_page()):
                return self.notice(HTTPStatus.GONE, "This link has closed",
                                   reason[0].upper() + reason[1:] + ". Ask whoever sent it for a new one.")
            return self.upload_page()
        if route == "/upload.js":
            return self.send_body(HTTPStatus.OK, (TOOL_DIR / "upload.js").read_bytes(), "text/javascript; charset=utf-8")
        return self.notice(HTTPStatus.NOT_FOUND, "Nothing here", "Open the link you were sent.")

    def upload_page(self) -> None:
        receiver = self.server.receiver
        facts = []
        if not receiver.closed():
            facts.append(f"open for {human_time(receiver.expires_at - time.time())} more")
        if receiver.max_size is not None:
            facts.append(f"files up to {human_bytes(receiver.max_size)}")
        if receiver.once:
            facts.append("one upload only")
        self.send_template(HTTPStatus.OK, "upload.html", PAGE_CSP,
                           code="1" if receiver.code else "0",
                           max=str(receiver.max_size or 0), chunk=str(CHUNK), limit=str(CHUNK_LIMIT),
                           facts=html.escape(" · ".join(facts)))

    def same_origin(self) -> bool:
        # A page elsewhere can't post here unseen: the session header needs a preflight kit never
        # answers, and this catches a plain form aimed at the code.
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host", "")

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("bad Content-Length") from None
        if length > MAX_JSON:
            raise ApiError("request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length)
        self.body_read = True
        try:
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("invalid JSON") from None
        if not isinstance(body, dict):
            raise ApiError("expected a JSON object")
        return body

    def signed_in(self) -> None:
        if not self.server.receiver.session_ok(self.headers.get("X-Kit-Session", "")):
            raise ApiError("enter the code again", HTTPStatus.UNAUTHORIZED)

    def do_POST(self) -> None:
        route = self.route()
        if route is None:
            return
        receiver = self.server.receiver
        try:
            if not self.same_origin():
                raise ApiError("cross-origin request refused", HTTPStatus.FORBIDDEN)
            if route == "/code":
                body = self.read_json()
                if receiver.code and not str(body.get("code", "")).strip():
                    raise ApiError("Type the code you were given.", HTTPStatus.FORBIDDEN,
                                   left=CODE_TRIES - receiver.wrong)
                return self.send_json(HTTPStatus.OK, {"session": receiver.sign_in(body.get("code", ""), self.who())})
            if route == "/start":
                self.signed_in()
                body = self.read_json()
                return self.send_json(HTTPStatus.OK, {"files": receiver.start(body.get("files"), self.who())})
        except ApiError as exc:
            if exc.status == HTTPStatus.FORBIDDEN and "left" in exc.extra:
                time.sleep(1)   # slows guessing down further; the try limit is what stops it
            return self.fail(exc)
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self) -> None:
        route = self.route()
        if route is None:
            return
        receiver = self.server.receiver
        try:
            if route != "/chunk":
                raise ApiError("not found", HTTPStatus.NOT_FOUND)
            if not self.same_origin():
                raise ApiError("cross-origin request refused", HTTPStatus.FORBIDDEN)
            self.signed_in()
            params = parse_qs(urlparse(self.path).query)
            upload = receiver.uploads.get(params.get("id", [""])[0])
            if upload is None:
                raise ApiError("unknown file - start sending it again", HTTPStatus.NOT_FOUND)
            try:
                offset = int(params.get("offset", [""])[0])
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError("bad offset or length") from None
            receiver.claim(upload, offset, length, self.who())
            self.body_read = True   # from here on it is read, all or until the browser goes
            receiver.write(upload, length, self.rfile)
            self.send_json(HTTPStatus.OK, {"received": upload.received, "done": upload.saved is not None})
        except ApiError as exc:
            try:
                self.fail(exc)
            except OSError:
                pass  # they've gone; the page will ask where things stand when it's back


# --- start ---------------------------------------------------------------------------

def listen(receiver: Receiver, mode: str, port: int, explicit: bool) -> QuietServer:
    host = reach.bind_host(mode)
    candidates = [0] if mode == "internet" else [port] if explicit else range(port, port + PORT_ATTEMPTS)
    last: OSError | None = None
    for candidate in candidates:
        try:
            server = QuietServer((host, candidate), receiver)
            break
        except OSError as exc:
            last = exc
    else:
        if explicit:
            die(f"can't listen on port {port}: {last}")
        die(f"no free port between {port} and {port + PORT_ATTEMPTS - 1}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def print_link(receiver: Receiver, mode: str, show_qr: bool, expire: float) -> None:
    link = receiver.link
    print(f"\n  {style(link, 'bold')}")
    if show_qr:
        print()
        for line in qr_lines(link):
            print("  " + line)
    if receiver.code:
        code = f"{receiver.code[:3]} {receiver.code[3:]}"
        print(f"\n  {style('Code:', 'bold')} {style(code, 'bold', 'accent')}   "
              + style("tell them yourself - a call, or another app than the one the link goes in", "dim"))
    else:
        print(style("\n  no code: anyone who has the link can send files (--no-code)", "accent"))
    facts = [f"takes new files for {human_time(expire)}"]
    if receiver.max_size is not None:
        facts.append(f"up to {human_bytes(receiver.max_size)} each")
    if receiver.once:
        facts.append("one upload, then it closes")
    print(style(f"  {', '.join(facts)}", "dim"))
    if mode == "lan" and os.name == "nt":
        print(style("  If Windows Firewall asks, allow access on Private networks so other devices can connect.", "dim"))
    print(style("\n  kit has to keep running until the files arrive - Ctrl+C here stops it", "dim"), flush=True)


def report_leftovers(receiver: Receiver) -> None:
    for upload in receiver.unfinished():
        if upload.part.is_file() and upload.received:
            print(style(f"  unfinished: {upload.part.name} ({100 * upload.received / max(upload.size, 1):.0f}%) - "
                        "run kit receive on this folder again and have them send the same file to carry on, "
                        "or delete it", "dim"))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kit receive",
        description="Receive files from someone's browser: send them a link and a code, and what they upload "
                    "lands in a folder here.",
    )
    parser.add_argument("folder", nargs="?", help="where received files go (setting: receive.folder; "
                                                  "default: kit-received in your Downloads folder)")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--lan", dest="mode", action="store_const", const="lan",
                       help="the link works for devices on this network (setting: receive.mode)")
    where.add_argument("--internet", dest="mode", action="store_const", const="internet",
                       help="the link works from anywhere, through a free Cloudflare tunnel")
    parser.add_argument("--once", action="store_true", default=None,
                        help="close the link once the first set of files has arrived (setting: receive.once)")
    parser.add_argument("--max-size", metavar="SIZE", help="largest file accepted: 2GB, 500MB (setting: receive.max_size)")
    parser.add_argument("--expire", metavar="TIME", help="how long the link takes new files: 1h, 30m (setting: receive.expire)")
    parser.add_argument("--no-code", dest="code", action="store_false", default=None,
                        help="don't ask for a code - anyone with the link can send files (setting: receive.code)")
    parser.add_argument("--port", type=int, help=f"port the link uses on the LAN (setting: receive.port, default {DEFAULT_PORT})")
    parser.add_argument("--no-qr", dest="qr", action="store_false", default=True, help="don't print a QR code of the link")
    args = parser.parse_args()
    conf = tool_settings()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")   # a file name the console can't show mustn't crash kit

    try:
        expire = parse_expire(args.expire or conf.get("expire", "1h"))
        max_size = parse_size(args.max_size if args.max_size is not None else conf.get("max_size", ""))
    except ValueError as exc:
        die(str(exc))
    folder = Path(args.folder or conf.get("folder") or default_folder()).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve()
        probe = folder / f".kit-receive-{secrets.token_hex(4)}"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        die(f"can't save files in {folder}: {exc}")

    mode = args.mode or conf.get("mode", "lan")
    use_code = conf.get("code", True) if args.code is None else args.code
    code = f"{secrets.randbelow(1_000_000):06d}" if use_code else ""
    once = conf.get("once", False) if args.once is None else args.once
    receiver = Receiver(folder, code, expire, once, max_size)
    server = listen(receiver, mode, args.port or conf.get("port", DEFAULT_PORT), args.port is not None)
    receiver.behind_tunnel = mode == "internet"

    print(f"{style('kit receive', 'bold', 'accent')}  files go to {folder}", flush=True)
    outside = None
    try:
        try:
            outside = reach.open(server.server_address[1], mode, cloudflared=conf.get("cloudflared", ""),
                                 check_path=PING, say=lambda text: log(style(text, "dim")))
        except reach.ReachError as exc:
            die(str(exc))
        receiver.base = outside.url
        print_link(receiver, mode, args.qr, expire)

        last_line = time.time()
        while not receiver.finished.wait(0.5):
            receiver.tick()
            line = receiver.progress_line()
            if console.tty:
                console.show(line)
            elif line and time.time() - last_line >= PROGRESS_EVERY:
                last_line = time.time()
                log(line.strip())
        console.show("")
        log(f"{style('closed', 'dim')}    {receiver.why_finished}")
        time.sleep(0.3)  # let the last answer finish going out
    except KeyboardInterrupt:
        console.show("")
        print("\nstopped receiving")
    finally:
        if outside:
            outside.close()
        server.shutdown()
        server.server_close()
    saved = [upload.saved for upload in receiver.uploads.values() if upload.saved]
    if saved:
        print(f"  {len(saved)} file{'s' if len(saved) != 1 else ''} in {folder}")
    report_leftovers(receiver)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
