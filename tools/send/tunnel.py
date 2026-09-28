"""A Cloudflare quick tunnel: a public https address for a port on this machine, no account needed.

kit runs `cloudflared tunnel --url http://127.0.0.1:PORT`, which dials out to Cloudflare and prints a
random https://<words>.trycloudflare.com address. Because the connection goes out, it works behind
any router or firewall that allows ordinary outgoing traffic.

cloudflared is rarely installed, so kit downloads the official release binary on first use, checks
it against the SHA-256 GitHub publishes for it, and keeps it in kit's data folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

DOH = "https://cloudflare-dns.com/dns-query"
RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection", re.I)
START_TIMEOUT = 45    # seconds for cloudflared to hand out an address and connect
CHECK_TIMEOUT = 60    # seconds for that address to start answering (usually about 10)


class TunnelError(Exception):
    pass


def ssl_context() -> ssl.SSLContext:
    # Avast and similar HTTPS scanners set SSLKEYLOGFILE to a path Python's OpenSSL can't open, and
    # their root certificates fail Python 3.13's strict X.509 check. Chain and hostname checks stay on.
    os.environ.pop("SSLKEYLOGFILE", None)
    context = ssl.create_default_context()
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


# --- finding or fetching cloudflared --------------------------------------------------

def asset_name() -> str:
    """The release file for this machine."""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "i386": "386", "i686": "386", "x86": "386"}.get(machine)
    if arch is None and machine.startswith("arm"):
        arch = "arm"
    if arch is None:
        raise TunnelError(f"no cloudflared build for this processor ({machine}) - install it yourself "
                          "and set send.cloudflared")
    if os.name == "nt":
        return f"cloudflared-windows-{arch}.exe"
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def installed_path(data_dir: Path) -> Path:
    return data_dir / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")


def find_cloudflared(configured: str, data_dir: Path) -> str | None:
    """The setting, else cloudflared on PATH, else the copy kit downloaded earlier."""
    if configured:
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    if (found := shutil.which("cloudflared")):
        return found
    mine = installed_path(data_dir)
    return str(mine) if mine.is_file() else None


def download_cloudflared(data_dir: Path, say: Callable[[str], None]) -> str:
    """Fetch the official release for this machine into data_dir/bin and return its path."""
    name = asset_name()
    context = ssl_context()
    request = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "kit-send"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            release = json.load(response)
    except OSError as exc:
        raise TunnelError(f"couldn't look up the latest cloudflared release: {exc}") from None
    asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
    if asset is None:
        raise TunnelError(f"the cloudflared release has no {name}")
    expected = str(asset.get("digest") or "").removeprefix("sha256:").lower()

    target = installed_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    size = int(asset.get("size") or 0)
    say(f"downloading cloudflared {release.get('tag_name', '')} ({size / 1e6:.0f} MB) - only needed once")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "kit-send"})
        with urllib.request.urlopen(request, timeout=60, context=context) as response, partial.open("wb") as out:
            while (block := response.read(1 << 20)):
                digest.update(block)
                out.write(block)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise TunnelError(f"downloading cloudflared failed: {exc}") from None
    if expected and digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise TunnelError("the cloudflared download didn't match its published checksum - not using it")

    if name.endswith(".tgz"):
        with tarfile.open(partial) as archive:
            member = next((m for m in archive.getmembers() if Path(m.name).name == "cloudflared"), None)
            if member is None:
                raise TunnelError("the cloudflared archive has no cloudflared in it")
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
        partial.unlink(missing_ok=True)
    else:
        os.replace(partial, target)
    if os.name != "nt":
        target.chmod(0o755)
    return str(target)


# --- running it ----------------------------------------------------------------------

def resolve(host: str, context: ssl.SSLContext) -> list[str]:
    """IPv4 addresses for host, from Cloudflare's DNS over HTTPS (never the system resolver)."""
    request = urllib.request.Request(f"{DOH}?name={host}&type=A",
                                     headers={"Accept": "application/dns-json", "User-Agent": "kit-send"})
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        answer = json.load(response)
    return [record["data"] for record in answer.get("Answer", []) if record.get("type") == 1]


def fetch_status(address: str, host: str, path: str, context: ssl.SSLContext) -> int:
    """The status of GET https://host/path, connecting to address instead of looking host up."""
    with socket.create_connection((address, 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: kit-send\r\n"
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

    def start(self) -> str:
        command = [self.program, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]
        options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                         "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = subprocess.Popen(command, **options)
        except OSError as exc:
            raise TunnelError(f"cloudflared didn't start: {exc}") from None
        threading.Thread(target=self._read, daemon=True).start()

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline and not self.ready.is_set():
            if self.process.poll() is not None:
                break
            self.ready.wait(0.2)
        if not (self.url and self.ready.is_set()):
            self.stop()
            tail = " / ".join(line for line in list(self.lines)[-4:] if line)
            raise TunnelError("cloudflared couldn't open a tunnel" + (f": {tail}" if tail else "")
                              + " - it needs outgoing access to Cloudflare (ports 443 and 7844)")
        return self.url

    def _read(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            self.lines.append(line)
            if not self.url and (match := URL_RE.search(line)):
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
        context = ssl_context()
        host = urlparse(self.url).hostname or ""
        deadline = time.time() + CHECK_TIMEOUT
        last = "its name doesn't exist yet"
        while time.time() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise TunnelError("cloudflared stopped")
            try:
                addresses = resolve(host, context)
                if addresses and fetch_status(addresses[0], host, path, context) == 200:
                    return
                if addresses:
                    last = "it doesn't reach kit yet"
            except OSError as exc:
                last = str(exc)
            time.sleep(1)
        raise TunnelError(f"the tunnel address didn't start working in time ({last})")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
