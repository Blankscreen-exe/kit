"""Merge images into one PDF after arranging them on a local web page with thumbnails."""

from __future__ import annotations

import argparse
import glob
import hmac
import io
import itertools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, unquote, urlparse

from kitlib import KIT_HOME, die, style, warn
from kitlib.browser import open_app_window
from kitlib.settings import tool_settings

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

PAGE = Path(__file__).resolve().parent / "page.html"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "BMP", "DIB", "GIF", "TIFF"}
THUMB_SIZE = 400
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
ID_RE = re.compile(r"p\d{1,7}")
INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EXIF_ORIENTATION = 0x0112
EXIF_IFD = 0x8769
EXIF_DATE_TAKEN = 0x9003
EXIF_DATE_TIME = 0x0132

# Page geometry in inches, margins in inches, and per-quality render settings.
PAGE_SIZES = {"a4": (8.27, 11.69), "letter": (8.5, 11.0)}
MARGINS = {"none": 0.0, "small": 0.4, "medium": 0.8}
QUALITY = {
    "high": {"dpi": 200, "jpeg": 92, "max_side": None},
    "small": {"dpi": 120, "jpeg": 72, "max_side": 1800},
}
FIT_DPI = 150  # "fit" pages are exactly the image; this only sets their physical size
CHOICES = {
    "page": ("fit", "a4", "letter"),
    "orientation": ("auto", "portrait", "landscape"),
    "margin": tuple(MARGINS),
    "quality": tuple(QUALITY),
}

Image.MAX_IMAGE_PIXELS = 400_000_000  # big scans are fine; Pillow's default warns at ~90 MP

UNAUTHORIZED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>merge-pdf</title></head>
<body style="font:15px system-ui,sans-serif;max-width:560px;margin:80px auto;padding:0 20px">
<h1 style="font-size:20px">merge-pdf needs its access token</h1>
<p>Open the full address printed in the terminal where you ran <code>kit merge-pdf</code>
(it ends in <code>?token=...</code>).</p></body></html>"""


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


# --- images ------------------------------------------------------------------------

@dataclass
class Item:
    id: str
    path: Path
    frame: int
    name: str
    width: int  # after EXIF orientation, before the user's rotation
    height: int
    date: float
    size: int
    uploaded: bool = False
    rotation: int = 0  # clockwise degrees chosen on the page


@dataclass
class Settings:
    page: str = "fit"
    orientation: str = "auto"
    margin: str = "small"
    quality: str = "high"
    output: str = "merged.pdf"


def natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def clean_output_name(raw: object) -> str:
    """A bare file name ending in .pdf: folders, reserved characters and leading/trailing dots are dropped."""
    name = Path(str(raw).replace("\\", "/")).name
    name = INVALID_NAME_CHARS.sub("", name).strip().strip(".").strip()
    if not name:
        name = "merged"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[-150:]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in itertools.count(2):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


def taken_date(exif: Image.Exif, path: Path) -> float:
    raw = exif.get_ifd(EXIF_IFD).get(EXIF_DATE_TAKEN) or exif.get(EXIF_DATE_TIME)
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw.strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def load_image(item: Item, rotate: bool = True, draft: tuple[int, int] | None = None) -> Image.Image:
    """The image (or TIFF page) turned the right way up by EXIF, plus the user's rotation."""
    with Image.open(item.path) as image:
        if item.frame:
            image.seek(item.frame)
        if draft and image.format == "JPEG":
            image.draft("RGB", draft)  # decode JPEGs at reduced size for thumbnails
        image.load()
        oriented = ImageOps.exif_transpose(image)
    if rotate and item.rotation:
        oriented = oriented.rotate(-item.rotation, expand=True)
    return oriented


def flatten(image: Image.Image) -> Image.Image:
    """RGB on white: handles transparency, palettes, CMYK, greyscale and 16-bit images."""
    if image.mode == "P":
        image = image.convert("RGBA")
    if image.mode.startswith("I;16"):
        image = image.convert("I")
    if image.mode == "I":
        image = image.point(lambda value: value / 256).convert("L")
    if image.mode == "F":
        image = image.convert("L")
    if image.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image if image.mode == "RGB" else image.convert("RGB")


def render_page(item: Item, settings: Settings) -> Image.Image:
    image = flatten(load_image(item))
    quality = QUALITY[settings.quality]
    if settings.page == "fit":
        if quality["max_side"] and max(image.size) > quality["max_side"]:
            image.thumbnail((quality["max_side"], quality["max_side"]), Image.Resampling.LANCZOS)
        return image

    width_in, height_in = PAGE_SIZES[settings.page]
    landscape = settings.orientation == "landscape" or (settings.orientation == "auto" and image.width > image.height)
    if landscape:
        width_in, height_in = height_in, width_in
    dpi = quality["dpi"]
    page_w, page_h = round(width_in * dpi), round(height_in * dpi)
    margin = round(MARGINS[settings.margin] * dpi)
    scale = min((page_w - 2 * margin) / image.width, (page_h - 2 * margin) / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    image = image.resize(size, Image.Resampling.LANCZOS)
    page = Image.new("RGB", (page_w, page_h), "white")
    page.paste(image, ((page_w - size[0]) // 2, (page_h - size[1]) // 2))
    return page


def build_pdf(items: list[Item], settings: Settings, output_dir: Path) -> dict:
    if not items:
        raise ApiError("add at least one image first", HTTPStatus.CONFLICT)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = unique_path(output_dir / settings.output)
    quality = QUALITY[settings.quality]
    pages = [render_page(item, settings) for item in items]
    partial = output.with_name(output.name + ".part")
    try:
        pages[0].save(
            partial, "PDF", save_all=True, append_images=pages[1:],
            resolution=FIT_DPI if settings.page == "fit" else quality["dpi"], quality=quality["jpeg"],
            title=Path(settings.output).stem, creator="kit merge-pdf",
        )
        os.replace(partial, output)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ApiError(f"couldn't write {output}: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from None
    return {"path": str(output), "pages": len(pages), "sizeBytes": output.stat().st_size}


def reveal_command(path: Path, folder: bool) -> list[str] | None:
    """How to open a file (or show it in its folder); None means os.startfile on Windows."""
    if os.name == "nt":
        return ["explorer", f"/select,{path}"] if folder else None
    if sys.platform == "darwin":
        return ["open", "-R", str(path)] if folder else ["open", str(path)]
    return ["xdg-open", str(path.parent if folder else path)]


def reveal(path: Path, folder: bool) -> str:
    command = reveal_command(path, folder)
    described = " ".join(command) if command else f'start "" "{path}"'
    if os.environ.get("KIT_MERGE_PDF_NO_REVEAL"):  # tests: report what would run without opening windows
        return described
    if command is None:
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name != "nt":
            options["start_new_session"] = True
        subprocess.Popen(command, **options)
    return described


def collect_paths(inputs: list[str], recursive: bool) -> list[Path]:
    """Image files from paths, folders and globs, de-duplicated, in the order given."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            seen.add(key)
            found.append(path)

    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            children = [c for c in (path.rglob("*") if recursive else path.iterdir()) if c.is_file()]
            skipped = [c for c in children if c.suffix.lower() not in IMAGE_EXTENSIONS]
            if skipped:
                warn(f"skipping {len(skipped)} non-image file(s) in {path}")
            for child in sorted(children, key=lambda c: natural_key(str(c.relative_to(path)))):
                if child.suffix.lower() in IMAGE_EXTENSIONS:
                    add(child)
            continue
        matches = [path] if path.is_file() else [Path(m) for m in sorted(glob.glob(os.path.expanduser(raw), recursive=recursive), key=natural_key)]
        if not matches:
            warn(f"no such file or folder: {raw}")
        for match in matches:
            if not match.is_file():
                continue
            if match.suffix.lower() in IMAGE_EXTENSIONS:
                add(match)
            else:
                warn(f"skipping {match.name}: not a supported image")
    return found


# --- session state -------------------------------------------------------------------

class Session:
    """Everything the page can change. Pages only ever refer to images by id, never by path."""

    def __init__(self, output_dir: Path, settings: Settings) -> None:
        self.lock = threading.RLock()
        self.items: dict[str, Item] = {}
        self.order: list[str] = []
        self.removed: list[tuple[str, int]] = []
        self.settings = settings
        self.output_dir = output_dir
        self.last_output: dict | None = None
        self.thumbs: dict[str, bytes] = {}
        self.ids = itertools.count(1)
        self.upload_counter = itertools.count(1)
        self._temp_dir: Path | None = None

    @property
    def temp_dir(self) -> Path:
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="kit-merge-pdf-"))
        return self._temp_dir

    def cleanup(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- adding images

    def add_file(self, path: Path, name: str, uploaded: bool = False) -> list[Item]:
        with Image.open(path) as image:
            if image.format not in IMAGE_FORMATS:
                raise ApiError(f"{name}: {image.format} images aren't supported", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            frames = getattr(image, "n_frames", 1) if image.format == "TIFF" else 1
            size = path.stat().st_size
            new = []
            for frame in range(frames):
                if frame:
                    image.seek(frame)
                exif = image.getexif()
                width, height = image.size
                if exif.get(EXIF_ORIENTATION, 1) in (5, 6, 7, 8):
                    width, height = height, width
                label = name if frames == 1 else f"{name} · page {frame + 1}"
                new.append(Item(f"p{next(self.ids)}", path, frame, label, width, height, taken_date(exif, path), size, uploaded))
        with self.lock:
            for item in new:
                self.items[item.id] = item
                self.order.append(item.id)
        return new

    def add_upload(self, raw_name: str, stream: BinaryIO, length: int) -> dict:
        name = INVALID_NAME_CHARS.sub("_", Path(raw_name.replace("\\", "/")).name).strip() or "image"
        destination = self.temp_dir / f"{next(self.upload_counter):04d}-{name[-120:]}"
        remaining = length
        with open(destination, "wb") as out:
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining:
            destination.unlink(missing_ok=True)
            raise ApiError("the upload was cut short", HTTPStatus.BAD_REQUEST)
        try:
            self.add_file(destination, name, uploaded=True)
        except ApiError:
            destination.unlink(missing_ok=True)
            raise
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            destination.unlink(missing_ok=True)
            raise ApiError(f"{name} isn't a supported image", HTTPStatus.UNSUPPORTED_MEDIA_TYPE) from None
        return self.state()

    # -- reading

    def item(self, item_id: object) -> Item:
        if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id) or item_id not in self.items:
            raise ApiError(f"no image with id '{item_id}'", HTTPStatus.NOT_FOUND)
        return self.items[item_id]

    def state(self) -> dict:
        with self.lock:
            return {
                "items": [{
                    "id": item.id, "name": item.name, "width": item.width, "height": item.height,
                    "rotation": item.rotation, "date": datetime.fromtimestamp(item.date).isoformat(timespec="seconds"),
                    "sizeBytes": item.size, "uploaded": item.uploaded,
                } for item in (self.items[i] for i in self.order)],
                "canUndo": bool(self.removed),
                "settings": asdict(self.settings),
                "outputDir": str(self.output_dir),
                "lastOutput": self.last_output,
            }

    def thumbnail(self, item_id: object) -> bytes:
        item = self.item(item_id)
        cached = self.thumbs.get(item.id)
        if cached is None:
            image = flatten(load_image(item, rotate=False, draft=(THUMB_SIZE * 2, THUMB_SIZE * 2)))
            image.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=82)
            cached = self.thumbs[item.id] = buffer.getvalue()
        return cached

    # -- changes

    def set_order(self, order: object) -> dict:
        with self.lock:
            if (not isinstance(order, list) or len(order) != len(self.order)
                    or not all(isinstance(i, str) for i in order) or set(order) != set(self.order)):
                raise ApiError("the new order must list every current page exactly once")
            self.order = list(order)
            return self.state()

    def rotate(self, item_id: object, delta: object) -> dict:
        with self.lock:
            item = self.item(item_id)
            if item.id not in self.order:
                raise ApiError(f"image '{item_id}' was removed", HTTPStatus.CONFLICT)
            if delta not in (90, -90, 180):
                raise ApiError("rotate by 90, -90 or 180 degrees")
            item.rotation = (item.rotation + delta) % 360
            return self.state()

    def remove(self, item_id: object) -> dict:
        with self.lock:
            item = self.item(item_id)
            if item.id not in self.order:
                raise ApiError(f"image '{item_id}' was already removed", HTTPStatus.CONFLICT)
            index = self.order.index(item.id)
            self.order.pop(index)
            self.removed.append((item.id, index))
            return self.state()

    def undo(self) -> dict:
        with self.lock:
            if not self.removed:
                raise ApiError("nothing to undo", HTTPStatus.CONFLICT)
            item_id, index = self.removed.pop()
            self.order.insert(min(index, len(self.order)), item_id)
            return self.state()

    def sort(self, by: object) -> dict:
        with self.lock:
            if by == "name":
                self.order.sort(key=lambda i: natural_key(self.items[i].name))
            elif by == "date":
                self.order.sort(key=lambda i: (self.items[i].date, natural_key(self.items[i].name)))
            elif by == "reverse":
                self.order.reverse()
            else:
                raise ApiError("sort by name, date or reverse")
            return self.state()

    def update_settings(self, changes: dict) -> dict:
        with self.lock:
            unknown = set(changes) - set(CHOICES) - {"output"}
            if unknown:
                raise ApiError(f"unknown setting(s): {', '.join(sorted(unknown))}")
            for key, value in changes.items():
                if key == "output":
                    if not isinstance(value, str) or len(value) > 300:
                        raise ApiError("the file name must be text")
                    self.settings.output = clean_output_name(value)
                elif value not in CHOICES[key]:
                    raise ApiError(f"{key} must be one of: {', '.join(CHOICES[key])}")
                else:
                    setattr(self.settings, key, value)
            return self.state()

    def create(self) -> dict:
        with self.lock:
            items = [self.items[i] for i in self.order]
            self.last_output = build_pdf(items, self.settings, self.output_dir)
            return {**self.state(), "output": self.last_output}

    def open_output(self, what: object) -> dict:
        with self.lock:
            if not self.last_output:
                raise ApiError("create the PDF first", HTTPStatus.CONFLICT)
            if what not in ("pdf", "folder"):
                raise ApiError("open 'pdf' or 'folder'")
            path = Path(self.last_output["path"])
            if not path.exists():
                raise ApiError(f"{path} no longer exists", HTTPStatus.GONE)
        return {**self.state(), "command": reveal(path, folder=what == "folder")}


POST_ROUTES = {
    "/api/order": lambda session, body: session.set_order(body.get("order")),
    "/api/rotate": lambda session, body: session.rotate(body.get("id"), body.get("delta")),
    "/api/remove": lambda session, body: session.remove(body.get("id")),
    "/api/undo": lambda session, body: session.undo(),
    "/api/sort": lambda session, body: session.sort(body.get("by")),
    "/api/settings": lambda session, body: session.update_settings(body),
    "/api/create": lambda session, body: session.create(),
    "/api/open": lambda session, body: session.open_output(body.get("what")),
}


# --- web server ----------------------------------------------------------------------

def log(message: str) -> None:
    print(f"{style(time.strftime('%H:%M:%S'), 'dim')}  {message}", flush=True)


class MergeServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, session: Session, token: str) -> None:
        super().__init__(("127.0.0.1", 0), Handler)  # port 0: the OS picks a free port
        port = self.server_address[1]
        self.session = session
        self.token = token
        self.cookie_name = f"kit_merge_pdf_{port}"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}


class Handler(BaseHTTPRequestHandler):
    server: MergeServer
    server_version = "kit-merge-pdf"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep the terminal quiet

    # -- responses

    def send_body(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        headers = {"Cache-Control": "no-store", **(extra or {})}
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def send_page(self, status: int, html: str) -> None:
        self.send_body(status, html.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "connect-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        })

    # -- checks

    def host_ok(self) -> bool:
        # Blocks DNS rebinding: pages on other domains that resolve to 127.0.0.1 send their own Host.
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

    def read_json(self) -> dict:
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ApiError("send JSON", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        length = self.content_length()
        if length > MAX_JSON_BYTES:
            raise ApiError("request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("invalid JSON") from None
        if not isinstance(body, dict):
            raise ApiError("expected a JSON object")
        return body

    def content_length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            raise ApiError("bad Content-Length") from None

    # -- routes

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
                return self.send_page(HTTPStatus.UNAUTHORIZED, UNAUTHORIZED_PAGE)
            return self.send_page(HTTPStatus.OK, PAGE.read_text(encoding="utf-8"))

        if url.path == "/favicon.ico":
            return self.send_body(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        if not url.path.startswith("/api/"):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, "missing or invalid token - open the address printed in the terminal")

        session = self.server.session
        try:
            if url.path == "/api/state":
                return self.send_json(HTTPStatus.OK, session.state())
            if url.path == "/api/thumb":
                data = session.thumbnail(parse_qs(url.query).get("id", [""])[0])
                return self.send_body(HTTPStatus.OK, data, "image/jpeg", {"Cache-Control": "private, max-age=3600"})
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        except Exception as exc:  # a broken image file shouldn't take the server down
            log(f"{style('error', 'red')}  {url.path}: {exc}")
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        if not self.host_ok():
            return self.send_error_json(HTTPStatus.FORBIDDEN, "unexpected Host header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            return self.send_error_json(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
        if origin is None and not self.headers.get("X-Kit-Token"):
            return self.send_error_json(HTTPStatus.FORBIDDEN, "requests without an Origin header must send X-Kit-Token")
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, "missing or invalid token")

        session = self.server.session
        path = urlparse(self.path).path
        try:
            if path == "/api/done":
                self.send_json(HTTPStatus.OK, {"ok": True})
                log("done - stopping")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/upload":
                if not self.headers.get("Content-Type", "").startswith("application/octet-stream"):
                    raise ApiError("send the file as application/octet-stream", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                length = self.content_length()
                if length == 0:
                    raise ApiError("the file is empty")
                if length > MAX_UPLOAD_BYTES:
                    raise ApiError("files must be under 200 MB", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                name = unquote(self.headers.get("X-File-Name", ""))
                result = session.add_upload(name, self.rfile, length)
                log(f"added  {name}")
                return self.send_json(HTTPStatus.OK, result)
            route = POST_ROUTES.get(path)
            if route is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            result = route(session, self.read_json())
            if path == "/api/create":
                log(f"{style('wrote', 'green')}  {result['output']['path']}  ({result['output']['pages']} pages)")
            return self.send_json(HTTPStatus.OK, result)
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        except Exception as exc:
            log(f"{style('error', 'red')}  {path}: {exc}")
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")


# --- command line ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="kit merge-pdf", description="Merge images into one PDF after arranging them on a local page.")
    parser.add_argument("inputs", nargs="*", help="image files, folders or globs (e.g. *.jpg)")
    parser.add_argument("-o", "--output", help="PDF to write (default: merged.pdf in the current folder)")
    parser.add_argument("-r", "--recursive", action="store_true", help="include images in subfolders")
    conf = tool_settings()
    parser.add_argument("--no-open", action="store_true", help="don't open the page, only print its address (setting: merge-pdf.open)")
    parser.add_argument("--open", dest="no_open", action="store_false", help="open the page in your browser")
    parser.add_argument("--window", action="store_true", help="open the page in its own app window")
    parser.add_argument("--no-ui", action="store_true", help="build the PDF straight away without the page")
    parser.add_argument("--order", choices=["name", "date", "given"], default="name", help="starting page order (default: name)")
    parser.add_argument("--page", choices=CHOICES["page"], default=conf.get("page", "fit"),
                        help="page size (setting: merge-pdf.page, default: fit each image)")
    parser.add_argument("--orientation", choices=CHOICES["orientation"], default="auto", help="A4/Letter orientation (default: auto)")
    parser.add_argument("--margin", choices=CHOICES["margin"], default=conf.get("margin", "small"),
                        help="A4/Letter margin (setting: merge-pdf.margin, default: small)")
    parser.add_argument("--quality", choices=CHOICES["quality"], default=conf.get("quality", "high"),
                        help="image quality (setting: merge-pdf.quality, default: high)")
    parser.set_defaults(no_open=not conf.get("open", True))
    args = parser.parse_args()

    if args.output:
        requested = Path(args.output).expanduser()
        output_dir, output_name = requested.parent.resolve(), clean_output_name(requested.name)
    else:
        output_dir, output_name = Path.cwd(), "merged.pdf"
    settings = Settings(args.page, args.orientation, args.margin, args.quality, output_name)
    session = Session(output_dir, settings)

    for path in collect_paths(args.inputs, args.recursive):
        try:
            session.add_file(path, path.name)
        except ApiError as exc:
            warn(f"skipping {exc}")
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            warn(f"skipping {path.name}: {exc}")
    if args.order != "given":
        session.sort(args.order)

    if args.no_ui:
        if not session.order:
            die("no images to merge")
        try:
            output = session.create()["output"]
        except ApiError as exc:
            die(str(exc))
        print(f"{style('wrote', 'bold', 'green')} {output['path']}  ({output['pages']} pages)")
        return 0

    token = secrets.token_urlsafe(24)
    server = MergeServer(session, token)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    count = len(session.order)
    print(f"{style('merge-pdf', 'bold', 'cyan')}  {count} image{'' if count == 1 else 's'} to arrange")
    print(f"  open  {style(url, 'bold')}")
    print(style("  arrange the pages, click Create PDF, then Done - or press Ctrl+C to stop", "dim"), flush=True)
    if args.window:
        open_app_window(url)
    elif not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
        session.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
