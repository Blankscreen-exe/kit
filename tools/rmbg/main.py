"""Remove the background from an image with a local AI model, and fix mistakes by painting over them on a local page."""

from __future__ import annotations

import os

# Avast and similar HTTPS scanners set SSLKEYLOGFILE to a path Python's OpenSSL can't open, which
# aborts the whole process as soon as an SSL context is created. Drop it before touching ssl.
os.environ.pop("SSLKEYLOGFILE", None)

import argparse  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import io  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import ssl  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402
from http import HTTPStatus  # noqa: E402
from http.cookies import CookieError, SimpleCookie  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

from kitlib import KIT_HOME, die, style, theme  # noqa: E402
from kitlib.browser import open_app_window  # noqa: E402
from kitlib.ui import want_ui, why_no_ui  # noqa: E402
from kitlib.webserver import KitHandler  # noqa: E402

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

PAGE = Path(__file__).resolve().parent / "page.html"

# Kept out of pyproject.toml: `kit update` runs a plain `uv sync`, which would uninstall an optional extra,
# and every other tool would carry them if they were required. They live in this tool's data folder instead.
PACKAGES = ["numpy>=2,<3", "onnxruntime>=1.20,<2", "opencv-python-headless>=4.10,<5"]
INSTALL_TIMEOUT = 900

MODEL_NAME = "isnet-general-use.onnx"
MODEL_URL = f"https://github.com/danielgatis/rembg/releases/download/v0.0.0/{MODEL_NAME}"
MODEL_SIZE = 178_648_008
MODEL_SHA256 = "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a"
MODEL_INPUT = 1024

WORK_SIDE = 1024        # the page and GrabCut work at this size at most; saving applies the result to the full image
GRABCUT_ROUNDS = 2      # with WORK_SIDE, keeps a stroke to about 3 seconds on a CPU; more didn't improve the result
STROKE_COLOURS = 3      # how many main colours a stroke is reduced to
MAX_COLOUR_SAMPLES = 5000
COLOUR_TOLERANCE = 14.0  # how far from them a colour can be and still spread (CIELAB distance)
MAX_STROKES_BYTES = 32 * 1024 * 1024
OUTPUT_SUFFIXES = {".png", ".webp"}  # the common formats that keep transparency

Image.MAX_IMAGE_PIXELS = 400_000_000  # big scans are fine; Pillow's default warns at ~90 MP

UNAUTHORIZED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>rmbg</title>
<!--kit-theme-->
<style>body { min-height: 100vh; display: grid; place-items: center; padding: 16px; } .slab { max-width: 460px; padding: 22px 24px; }</style>
</head><body><div class="slab">
<h1><span class="mark">■</span>kit rmbg</h1>
<p>This page needs its access token. Open the full address printed in the terminal where you ran
<code>kit rmbg</code> (it ends in <code>?token=...</code>).</p></div></body></html>"""


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


# --- the packages and the model --------------------------------------------------------

def data_dir() -> Path:
    override = os.environ.get("KIT_RMBG_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":  # Local, not Roaming: these are hundreds of MB and shouldn't follow a profile around
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "rmbg"


def packages_dir() -> Path:
    # Compiled packages only load in the Python they were built for, so a new Python gets a fresh folder.
    return data_dir() / f"packages-py{sys.version_info.major}.{sys.version_info.minor}"


def model_path() -> Path:
    return data_dir() / MODEL_NAME


def activate_packages() -> bool:
    """Make the tool's own packages importable. False when they aren't installed yet."""
    folder = packages_dir()
    if not folder.is_dir():
        return False
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    return True


def install_packages() -> None:
    uv = shutil.which("uv")
    if not uv:
        die("needs uv on PATH to install its packages - see https://docs.astral.sh/uv/")
    target = packages_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    print("installing packages (numpy, onnxruntime, opencv)...", flush=True)
    temporary = Path(tempfile.mkdtemp(prefix=".packages-", dir=target.parent))
    try:
        problem = ""
        # Antivirus that inspects HTTPS breaks uv's built-in certificates; the system store usually works.
        for extra in ([], ["--system-certs"]):
            done = subprocess.run(
                [uv, "pip", "install", "--quiet", "--python", sys.executable, "--target", str(temporary), *extra, *PACKAGES],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=INSTALL_TIMEOUT,
            )
            if done.returncode == 0:
                os.replace(temporary, target)
                return
            problem = done.stderr.strip() or f"exit code {done.returncode}"
        die(f"couldn't install the packages: {problem}")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def https_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Python 3.13+ rejects CA certificates that don't mark Basic Constraints as critical - including the
    # roots antivirus HTTPS scanners (e.g. Avast) install. Chain and host name are still fully verified.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


def download_model() -> None:
    destination = model_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    total_mb = MODEL_SIZE // 1_000_000
    shown = -1
    print(f"downloading the model ({total_mb} MB)...", flush=True)
    try:
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "kit-rmbg"})
        with urllib.request.urlopen(request, timeout=60, context=https_context()) as reply, open(partial, "wb") as out:
            received = 0
            while chunk := reply.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                percent = received * 100 // MODEL_SIZE
                if sys.stdout.isatty() and percent != shown:
                    print(f"\r  {percent}%", end="", flush=True)
                    shown = percent
        if sys.stdout.isatty():
            print()
        if digest.hexdigest() != MODEL_SHA256:
            die("the downloaded model doesn't match its checksum, so it wasn't kept - try again")
        os.replace(partial, destination)
    except (OSError, urllib.error.URLError) as exc:
        die(f"couldn't download the model: {exc}")
    finally:
        partial.unlink(missing_ok=True)


def ready() -> bool:
    return packages_dir().is_dir() and model_path().is_file()


def set_up() -> None:
    if not packages_dir().is_dir():
        install_packages()
    if not model_path().is_file():
        download_model()
    print(f"{style('ready', 'good')}  files are in {data_dir()}")


def ensure_ready() -> None:
    """Install what's missing on first use, after asking: it's a few hundred MB."""
    if not ready():
        question = f"rmbg needs its packages and a {MODEL_SIZE // 1_000_000} MB model the first time. Download them now? [y/N] "
        if not sys.stdin.isatty():
            die("rmbg isn't set up yet - run: kit rmbg --setup")
        if input(question).strip().lower() not in ("y", "yes"):
            die("nothing downloaded - run kit rmbg --setup when you're ready")
        set_up()
    activate_packages()


# --- the image -------------------------------------------------------------------------

def load_image(path: Path) -> tuple[Image.Image, Image.Image, bytes | None]:
    """The image the right way up as RGB, its own transparency, and its colour profile."""
    with Image.open(path) as image:
        image.load()
        profile = image.info.get("icc_profile")
        oriented = ImageOps.exif_transpose(image)
    if oriented.mode.startswith("I;16"):
        oriented = oriented.convert("I")
    if oriented.mode == "I":
        oriented = oriented.point(lambda value: value / 256).convert("L")
    if oriented.mode == "F":
        oriented = oriented.convert("L")
    rgba = oriented.convert("RGBA")
    return rgba.convert("RGB"), rgba.getchannel("A"), profile


def find_subject(image: Image.Image):
    """The AI model's answer: a greyscale array the size of the image, 255 where the subject is."""
    import numpy as np
    import onnxruntime

    session = onnxruntime.InferenceSession(str(model_path()), providers=["CPUExecutionProvider"])
    small = np.asarray(image.resize((MODEL_INPUT, MODEL_INPUT), Image.Resampling.LANCZOS), dtype=np.float32)
    tensor = (small / max(float(small.max()), 1e-6) - 0.5).transpose(2, 0, 1)[np.newaxis]
    prediction = session.run(None, {session.get_inputs()[0].name: tensor})[0][0, 0]
    low, high = float(prediction.min()), float(prediction.max())
    scaled = (prediction - low) / max(high - low, 1e-6)
    mask = Image.fromarray((scaled * 255).astype("uint8")).resize(image.size, Image.Resampling.LANCZOS)
    return np.asarray(mask)


def touching(region, seed):
    """The parts of a yes/no region that are connected to the seed pixels."""
    import cv2
    import numpy as np

    _count, labels = cv2.connectedComponents(region.astype(np.uint8), connectivity=8)
    hit = np.unique(labels[seed])
    return np.isin(labels, hit[hit > 0])


def similar_area(lab, stroke, blocked):
    """Pixels connected to the stroke whose colour is close to one of the stroke's main colours."""
    import cv2
    import numpy as np

    samples = lab[stroke]
    if not len(samples):
        return stroke
    samples = samples[:: max(1, len(samples) // MAX_COLOUR_SAMPLES)]
    clusters = min(STROKE_COLOURS, len(samples))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    _score, _labels, centres = cv2.kmeans(samples, clusters, None, criteria, 2, cv2.KMEANS_PP_CENTERS)
    distances = np.linalg.norm(lab[:, :, np.newaxis, :] - centres[np.newaxis, np.newaxis, :, :], axis=3).min(axis=2)
    return touching(((distances < COLOUR_TOLERANCE) & ~blocked) | stroke, stroke)


def stroke_changes(bgr, alpha, remove, keep):
    """What the strokes change: (areas to clear, areas to keep), as yes/no arrays.

    A stroke only says something about what it's changing, so a remove stroke samples the colours of the
    subject it crosses, and a keep stroke those of the removed area. Growing it through those colours gives
    GrabCut a starting guess that it then fits to the edges; without that, GrabCut still counts those colours
    as subject and changes little beyond the stroke. It can also flip areas far from anything painted,
    so only changes connected to a stroke of the same kind are used.
    """
    import cv2
    import numpy as np

    was_subject = alpha >= 128
    labels = np.where(was_subject, cv2.GC_PR_FGD, cv2.GC_PR_BGD).astype(np.uint8)
    lab = cv2.cvtColor((bgr / 255).astype(np.float32), cv2.COLOR_BGR2LAB)
    labels[similar_area(lab, remove & was_subject, keep) & was_subject] = cv2.GC_PR_BGD
    labels[similar_area(lab, keep & ~was_subject, remove) & ~was_subject] = cv2.GC_PR_FGD
    labels[remove] = cv2.GC_BGD
    labels[keep] = cv2.GC_FGD
    try:
        cv2.grabCut(bgr, labels, None, np.zeros((1, 65)), np.zeros((1, 65)), GRABCUT_ROUNDS, cv2.GC_INIT_WITH_MASK)
    except cv2.error:  # e.g. everything is painted one way, so there's nothing to compare it with
        labels[remove] = cv2.GC_BGD
        labels[keep] = cv2.GC_FGD
    now_subject = (labels == cv2.GC_FGD) | (labels == cv2.GC_PR_FGD)
    return touching((was_subject & ~now_subject) | remove, remove), touching((~was_subject & now_subject) | keep, keep)


def apply_changes(alpha, clear, restore):
    """The alpha array with the changes applied at its own size, their edges softened a little."""
    import cv2
    import numpy as np

    if not clear.any() and not restore.any():
        return alpha
    height, width = alpha.shape
    sigma = max(1.0, max(width, height) / 1000)

    def soft(region):
        scaled = cv2.resize(region.astype(np.float32), (width, height), interpolation=cv2.INTER_LINEAR)
        return cv2.GaussianBlur(scaled, (0, 0), sigma)

    result = np.maximum(alpha.astype(np.float32) * (1 - soft(clear)), soft(restore) * 255)
    return np.clip(result + 0.5, 0, 255).astype(np.uint8)


def cut_out(image: Image.Image, source_alpha: Image.Image, alpha) -> Image.Image:
    """The picture with the new transparency; anything already transparent in the original stays so."""
    import numpy as np

    combined = (np.asarray(source_alpha, dtype=np.uint16) * alpha.astype(np.uint16) // 255).astype(np.uint8)
    result = image.convert("RGBA")
    result.putalpha(Image.fromarray(combined))
    return result


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in itertools.count(2):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


def output_path(source: Path, requested: str | None) -> Path:
    if not requested:
        return source.resolve().with_name(f"{source.stem}-nobg.png")
    path = Path(requested).expanduser().resolve()
    if path.suffix.lower() not in OUTPUT_SUFFIXES:
        die(f"{path.name}: use a .png or .webp name, the formats here that keep transparency")
    return path


def save_image(image: Image.Image, wanted: Path, profile: bytes | None) -> Path:
    """Write the file without ever replacing an existing one, and return where it went."""
    wanted.parent.mkdir(parents=True, exist_ok=True)
    output = unique_path(wanted)
    partial = output.with_name(output.name + ".part")
    options = {"icc_profile": profile} if profile else {}
    if output.suffix.lower() == ".webp":
        options["lossless"] = True
    try:
        image.save(partial, output.suffix.lstrip(".").upper(), **options)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return output


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=1)
    return buffer.getvalue()


# --- session state ---------------------------------------------------------------------

class Session:
    """The image being edited. `alpha` is the AI model's answer, which strokes always apply on top of."""

    def __init__(self, source: Path, image: Image.Image, source_alpha: Image.Image, profile: bytes | None, alpha, output: Path) -> None:
        import cv2
        import numpy as np

        self.lock = threading.Lock()
        self.source = source
        self.image, self.source_alpha, self.profile, self.alpha, self.output = image, source_alpha, profile, alpha, output
        scale = min(1.0, WORK_SIDE / max(image.size))
        self.work_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        self.work_image = image.resize(self.work_size, Image.Resampling.LANCZOS) if scale < 1 else image
        self.work_source_alpha = source_alpha.resize(self.work_size, Image.Resampling.BILINEAR)
        self.work_bgr = np.asarray(self.work_image)[:, :, ::-1].copy()
        self.work_alpha = cv2.resize(alpha, self.work_size, interpolation=cv2.INTER_AREA)
        self.clear = np.zeros(self.work_alpha.shape, dtype=bool)
        self.restore = np.zeros(self.work_alpha.shape, dtype=bool)
        self.last_output: dict | None = None

    def state(self) -> dict:
        return {
            "name": self.source.name, "width": self.image.width, "height": self.image.height,
            "workWidth": self.work_size[0], "workHeight": self.work_size[1],
            "output": str(self.output), "lastOutput": self.last_output,
        }

    def original_jpeg(self) -> bytes:
        buffer = io.BytesIO()
        self.work_image.save(buffer, "JPEG", quality=88)
        return buffer.getvalue()

    def result_png(self) -> bytes:
        alpha = apply_changes(self.work_alpha, self.clear, self.restore)
        return png_bytes(cut_out(self.work_image, self.work_source_alpha, alpha))

    def set_strokes(self, data: bytes) -> bytes:
        """Take the painted layer (red = remove, green = keep) and return the new preview."""
        import numpy as np

        try:
            with Image.open(io.BytesIO(data)) as painted:
                if painted.format != "PNG" or painted.size != self.work_size:
                    raise ApiError(f"the strokes must be a {self.work_size[0]}x{self.work_size[1]} PNG")
                pixels = np.asarray(painted.convert("RGBA"))
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise ApiError("the strokes aren't a readable image") from None
        solid = pixels[:, :, 3] >= 128
        remove = solid & (pixels[:, :, 0] > pixels[:, :, 1])
        keep = solid & (pixels[:, :, 1] > pixels[:, :, 0])
        with self.lock:
            if remove.any() or keep.any():
                self.clear, self.restore = stroke_changes(self.work_bgr, self.work_alpha, remove, keep)
            else:
                self.clear, self.restore = remove, keep
            return self.result_png()

    def save(self) -> dict:
        with self.lock:
            alpha = apply_changes(self.alpha, self.clear, self.restore)
            try:
                path = save_image(cut_out(self.image, self.source_alpha, alpha), self.output, self.profile)
            except OSError as exc:
                raise ApiError(f"couldn't write {self.output}: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from None
            self.last_output = {"path": str(path), "sizeBytes": path.stat().st_size}
            return self.state()


# --- web server ------------------------------------------------------------------------

def log(message: str) -> None:
    print(f"{style(time.strftime('%H:%M:%S'), 'dim')}  {message}", flush=True)


class EditServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, session: Session, token: str) -> None:
        super().__init__(("127.0.0.1", 0), Handler)  # port 0: the OS picks a free port
        port = self.server_address[1]
        self.session = session
        self.token = token
        self.cookie_name = f"kit_rmbg_{port}"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}


class Handler(KitHandler, BaseHTTPRequestHandler):
    server: EditServer
    server_version = "kit-rmbg"
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
        self.send_body(status, theme.inject(html).encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; form-action 'none'",
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

    def read_body(self) -> bytes:
        """Always called, whatever the answer will be: on a kept-alive connection, a body left
        unread would be taken for the start of the next request."""
        try:
            length = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            self.close_connection = True
            raise ApiError("bad Content-Length") from None
        if length <= MAX_STROKES_BYTES:
            return self.rfile.read(length)
        while length > 0:
            chunk = self.rfile.read(min(1 << 20, length))
            if not chunk:
                break
            length -= len(chunk)
        raise ApiError("request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

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
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, "missing or invalid token - open the address printed in the terminal")

        session = self.server.session
        try:
            if url.path == "/api/state":
                return self.send_json(HTTPStatus.OK, session.state())
            if url.path == "/api/original":
                return self.send_body(HTTPStatus.OK, session.original_jpeg(), "image/jpeg")
            if url.path == "/api/result":
                with session.lock:
                    return self.send_body(HTTPStatus.OK, session.result_png(), "image/png")
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            log(f"{style('error', 'bad')}  {url.path}: {exc}")
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        try:
            body = self.read_body()
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
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
            if path == "/api/strokes":
                if not self.headers.get("Content-Type", "").startswith("application/octet-stream"):
                    raise ApiError("send the strokes as application/octet-stream", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return self.send_body(HTTPStatus.OK, session.set_strokes(body), "image/png")
            if path == "/api/save":
                result = session.save()
                log(f"{style('wrote', 'good')}  {result['lastOutput']['path']}")
                return self.send_json(HTTPStatus.OK, result)
            if path == "/api/done":
                self.send_json(HTTPStatus.OK, {"ok": True})
                log("done - stopping")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as exc:
            return self.send_error_json(exc.status, str(exc))
        except Exception as exc:
            log(f"{style('error', 'bad')}  {path}: {exc}")
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")


# --- command line ----------------------------------------------------------------------

def edit(session: Session, no_open: bool, window: bool) -> None:
    token = secrets.token_urlsafe(24)
    server = EditServer(session, token)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"
    print(f"  open  {style(url, 'bold')}")
    print(style("  paint red over what should go and green over what should stay, click Save, then Done - or press Ctrl+C to stop", "dim"))
    if window:
        open_app_window(url)
    elif not no_open:
        if want_ui(None):
            webbrowser.open(url)
        else:
            print(f"  {why_no_ui()}: from your own computer run  ssh -L {port}:127.0.0.1:{port} <this machine>  and open the address there")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit rmbg", description="Remove the background from an image with a local AI model.")
    parser.add_argument("image", nargs="?", help="the image to work on")
    parser.add_argument("-o", "--output", help="file to write, .png or .webp (default: <name>-nobg.png next to the image)")
    parser.add_argument("-e", "--edit", action="store_true", help="fix the result by painting on a local page")
    parser.add_argument("--no-open", action="store_true", help="with --edit: only print the page's address")
    parser.add_argument("--window", action="store_true", help="with --edit: open the page in its own app window")
    parser.add_argument("--setup", action="store_true", help="download the packages and model now, then stop if no image was given")
    args = parser.parse_args()

    if args.setup:
        set_up()
    if not args.image:
        if args.setup:
            return 0
        parser.error("give an image, e.g. kit rmbg photo.jpg")

    source = Path(args.image).expanduser()
    if not source.is_file():
        die(f"no such file: {args.image}")
    output = output_path(source, args.output)
    try:
        image, source_alpha, profile = load_image(source)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        die(f"{source.name} isn't a readable image: {exc}")

    ensure_ready()
    print(f"{style('rmbg', 'bold', 'accent')}  finding the subject in {source.name} ({image.width}x{image.height})...", flush=True)
    alpha = find_subject(image)

    if args.edit:
        edit(Session(source, image, source_alpha, profile, alpha, output), args.no_open, args.window)
        return 0
    try:
        written = save_image(cut_out(image, source_alpha, alpha), output, profile)
    except OSError as exc:
        die(f"couldn't write {output}: {exc}")
    print(f"{style('wrote', 'bold', 'good')} {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
