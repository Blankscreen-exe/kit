"""Making a port on this machine reachable from elsewhere, and the link that reaches it.

    from kitlib import reach
    server = MyServer((reach.bind_host("internet"), 0))     # where to listen, for that mode
    link = reach.open(server.server_address[1], "internet", say=print)
    print(link.url)        # https://quiet-river-sample.trycloudflare.com
    ...
    link.close()

| mode | who can open the link | how |
|---|---|---|
| lan | devices on this network | this machine's network address |
| public | anyone | this machine's own public address; the port has to be open to the internet |
| internet | anyone | a Cloudflare quick tunnel (cloudflared, fetched once if needed), no account or open port |
| tailnet | devices on your tailnet | `tailscale serve` (Tailscale installed and logged in) |

Every failure is a ReachError whose message says what to do about it.
"""

from __future__ import annotations

import os

# Avast and similar HTTPS scanners set SSLKEYLOGFILE to a path Python's OpenSSL can't open, which
# aborts the process as soon as the ssl module loads. Drop it before ssl/urllib.
os.environ.pop("SSLKEYLOGFILE", None)

import hashlib  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tarfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from collections import deque  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

MODES = ("lan", "public", "internet", "tailnet")
DOH = "https://cloudflare-dns.com/dns-query"
RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
TRY_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection", re.I)
TAILNET_URL_RE = re.compile(r"https://[^\s'\"<>]+")
START_TIMEOUT = 45    # seconds for cloudflared to hand out an address and connect
CHECK_TIMEOUT = 60    # seconds for that address to start answering (usually about 10)
TAILSCALE_TIMEOUT = 15
USER_AGENT = "kit"


class ReachError(Exception):
    """The link couldn't be made; the message says why, and what to do."""


@dataclass
class Link:
    """A way in from outside: `url` reaches the port it was opened for, until close()."""

    url: str
    mode: str
    _close: Callable[[], None] = field(default=lambda: None, repr=False)

    def close(self) -> None:
        self._close()


def bind_host(mode: str) -> str:
    """Where a server should listen for this mode: every network for lan and public, only this
    machine when a tunnel or Tailscale does the reaching (nothing else needs to get in)."""
    return "0.0.0.0" if mode in ("lan", "public") else "127.0.0.1"


def https_context() -> ssl.SSLContext:
    """Normal certificate checks, minus Python 3.13's strict X.509 flag, which HTTPS scanners' own
    root certificates (Avast's) fail. Chain and hostname verification stay on."""
    context = ssl.create_default_context()
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


def data_dir() -> Path:
    """kit's data folder, where a downloaded cloudflared is kept."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit"


# --- lan and public ------------------------------------------------------------------------

def lan_address() -> str | None:
    """This machine's address on its network, or None when it isn't on one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # sends nothing; picks the outgoing interface
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith(("127.", "0.")) else address


def public_address() -> str:
    """This machine's address as the internet sees it, asked of Cloudflare (IPv6 in brackets)."""
    try:
        request = urllib.request.Request("https://www.cloudflare.com/cdn-cgi/trace", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=10, context=https_context()) as response:
            text = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise ReachError(f"couldn't find this machine's public address: {exc} - give it yourself") from None
    match = re.search(r"^ip=(.+)$", text, re.M)
    if not match:
        raise ReachError("couldn't find this machine's public address - give it yourself")
    address = match.group(1).strip()
    return f"[{address}]" if ":" in address else address


# --- internet: a Cloudflare quick tunnel ---------------------------------------------------
#
# `cloudflared tunnel --url http://127.0.0.1:PORT` dials out to Cloudflare and prints a random
# https://<words>.trycloudflare.com address. Going out, it works behind any router or firewall that
# allows ordinary outgoing traffic. cloudflared is rarely installed, so kit downloads the official
# release on first use, checks it against the SHA-256 GitHub publishes, and keeps it in data_dir().

def asset_name(machine: str | None = None, system: str | None = None) -> str:
    """The cloudflared release file for this machine."""
    machine = (machine or platform.machine()).lower()
    system = system or ("windows" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux")
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "i386": "386", "i686": "386", "x86": "386"}.get(machine)
    if arch is None and machine.startswith("arm"):
        arch = "arm"
    if arch is None:
        raise ReachError(f"no cloudflared build for this processor ({machine}) - install it yourself")
    if system == "windows":
        return f"cloudflared-windows-{arch}.exe"
    if system == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def installed_path(folder: Path | None = None) -> Path:
    return (folder or data_dir()) / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")


def find_cloudflared(configured: str = "", folder: Path | None = None) -> str | None:
    """The configured program, else cloudflared on PATH, else the copy kit downloaded earlier."""
    if configured:
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    if (found := shutil.which("cloudflared")):
        return found
    mine = installed_path(folder)
    return str(mine) if mine.is_file() else None


def download_cloudflared(say: Callable[[str], None], folder: Path | None = None) -> str:
    """Fetch the official release for this machine, check its SHA-256, and return its path."""
    name = asset_name()
    context = https_context()
    request = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            release = json.load(response)
    except OSError as exc:
        raise ReachError(f"couldn't look up the latest cloudflared release: {exc}") from None
    asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
    if asset is None:
        raise ReachError(f"the cloudflared release has no {name}")
    expected = str(asset.get("digest") or "").removeprefix("sha256:").lower()

    target = installed_path(folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    size = int(asset.get("size") or 0)
    say(f"downloading cloudflared {release.get('tag_name', '')} ({size / 1e6:.0f} MB) - only needed once")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60, context=context) as response, partial.open("wb") as out:
            while (block := response.read(1 << 20)):
                digest.update(block)
                out.write(block)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ReachError(f"downloading cloudflared failed: {exc}") from None
    if expected and digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise ReachError("the cloudflared download didn't match its published checksum - not using it")

    if name.endswith(".tgz"):
        with tarfile.open(partial) as archive:
            member = next((m for m in archive.getmembers() if Path(m.name).name == "cloudflared"), None)
            if member is None:
                raise ReachError("the cloudflared archive has no cloudflared in it")
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
        partial.unlink(missing_ok=True)
    else:
        os.replace(partial, target)
    if os.name != "nt":
        target.chmod(0o755)
    return str(target)


def resolve(host: str, context: ssl.SSLContext) -> list[str]:
    """IPv4 addresses for host, from Cloudflare's DNS over HTTPS (never the system resolver)."""
    request = urllib.request.Request(f"{DOH}?name={host}&type=A",
                                     headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        answer = json.load(response)
    return [record["data"] for record in answer.get("Answer", []) if record.get("type") == 1]


def fetch_status(address: str, host: str, path: str, context: ssl.SSLContext) -> int:
    """The status of GET https://host/path, connecting to address instead of looking host up."""
    with socket.create_connection((address, 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n"
                        "Connection: close\r\n\r\n".encode())
            first = tls.recv(64).split(b"\r\n", 1)[0].split()
    return int(first[1]) if len(first) > 1 and first[1].isdigit() else 0


class Tunnel:
    """One cloudflared process pointing a trycloudflare.com address at a local port."""

    def __init__(self, program: str, port: int) -> None:
        self.program = program
        self.port = port
        self.url = ""
        self.process: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=40)   # the end of its output, for error messages
        self.ready = threading.Event()

    def start(self, timeout: float = START_TIMEOUT) -> str:
        command = [self.program, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]
        options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                         "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = subprocess.Popen(command, **options)
        except OSError as exc:
            raise ReachError(f"cloudflared didn't start: {exc}") from None
        threading.Thread(target=self._read, daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline and not self.ready.is_set():
            if self.process.poll() is not None:
                break
            self.ready.wait(0.2)
        if not (self.url and self.ready.is_set()):
            self.stop()
            tail = " / ".join(line for line in list(self.lines)[-4:] if line)
            raise ReachError("cloudflared couldn't open a tunnel" + (f": {tail}" if tail else "")
                             + " - it needs outgoing access to Cloudflare (ports 443 and 7844)")
        return self.url

    def _read(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            self.lines.append(line)
            if not self.url and (match := TRY_URL_RE.search(line)):
                self.url = match.group(0)
            if READY_RE.search(line) and self.url:
                self.ready.set()

    def check(self, path: str) -> None:
        """Wait until the public address really reaches this machine, so the link works when it's shown.

        A new trycloudflare.com name takes several seconds to exist. Asking the system's resolver too
        early gets a "no such name" that it then caches for minutes (Windows does), so the link would
        fail on this machine long after it works everywhere else. So kit asks Cloudflare's DNS over
        HTTPS instead, and connects to the address it gives.
        """
        context = https_context()
        host = urlparse(self.url).hostname or ""
        deadline = time.time() + CHECK_TIMEOUT
        last = "its name doesn't exist yet"
        while time.time() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise ReachError("cloudflared stopped")
            try:
                addresses = resolve(host, context)
                if addresses and fetch_status(addresses[0], host, path, context) == 200:
                    return
                if addresses:
                    last = "it doesn't reach this machine yet"
            except OSError as exc:
                last = str(exc)
            time.sleep(1)
        raise ReachError(f"the tunnel address didn't start working in time ({last})")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()


# --- tailnet: tailscale serve --------------------------------------------------------------

def _text(value: str | bytes | None) -> str:
    """TimeoutExpired's .stdout/.stderr are bytes even with text=True: whatever was read so far."""
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def tailscale_serve(port: int) -> str:
    """Point `tailscale serve` at the port and return the tailnet address it gives.

    It stays configured in the tailscale daemon after this program ends (that's what --bg does);
    `tailscale serve reset` removes it.
    """
    if not shutil.which("tailscale"):
        raise ReachError("tailscale isn't installed or not on PATH - see https://tailscale.com/download")
    try:
        result = subprocess.run(["tailscale", "serve", "--bg", str(port)],
                                capture_output=True, text=True, timeout=TAILSCALE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        # tailscale prints its own explanation (often a one-time setup link) before it blocks
        # waiting on something - Serve needing to be turned on for the tailnet, say
        partial = (_text(exc.stdout) + _text(exc.stderr)).strip()
        raise ReachError(f"tailscale serve is waiting on something before it can continue:\n{partial}" if partial
                         else f"tailscale serve didn't finish within {TAILSCALE_TIMEOUT}s and printed nothing - "
                              f"check it yourself: tailscale serve --bg {port}") from None
    except OSError as exc:
        raise ReachError(f"couldn't run tailscale: {exc}") from None
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise ReachError(f"tailscale serve failed:\n{output}")
    match = TAILNET_URL_RE.search(output)
    if not match:
        raise ReachError("tailscale serve ran, but no address was found in what it printed - "
                         "check: tailscale serve status")
    return match.group(0).rstrip(".,;)/")


# --- one way in, whichever the mode ------------------------------------------------------------

def open(port: int, mode: str, *, address: str = "", cloudflared: str = "", folder: Path | None = None,
         check_path: str | None = None, say: Callable[[str], None] = lambda text: None) -> Link:
    """A link that reaches `port` on this machine in the given mode - see the table at the top.

    address: the name or address to put in public links (default: looked up).
    cloudflared, folder: which cloudflared to run, and where a downloaded one lives.
    check_path: for internet, a path that answers 200 through the tunnel - the link is only handed
        back once it really works from outside.
    say: where progress goes ("downloading cloudflared...", "opening a tunnel...").
    """
    if mode == "lan":
        found = lan_address()
        if not found:
            raise ReachError("this computer isn't on a network - connect to one first")
        return Link(f"http://{found}:{port}", mode)
    if mode == "public":
        return Link(f"http://{address or public_address()}:{port}", mode)
    if mode == "tailnet":
        return Link(tailscale_serve(port), mode)
    if mode != "internet":
        raise ReachError(f"unknown mode '{mode}' - use one of: {', '.join(MODES)}")

    program = find_cloudflared(cloudflared, folder)
    if program is None:
        if cloudflared:
            raise ReachError(f"the cloudflared setting points at {cloudflared}, which isn't there")
        program = download_cloudflared(say, folder)
    say("opening a tunnel through Cloudflare...")
    tunnel = Tunnel(program, port)
    url = tunnel.start()
    if check_path is not None:
        try:
            tunnel.check(check_path)
        except ReachError:
            tunnel.stop()
            raise
    return Link(url, mode, tunnel.stop)
