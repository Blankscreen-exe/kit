"""The kit machines on this network: find them, and send them a message.

Every machine running `kit notify serve` (or the notify window) listens for messages and pops them
up. This module is the other side, for any tool:

    from kitlib import lan
    devices = lan.discover()                           # who's out there
    for device, error in lan.send_to_all(devices, "backup finished"):
        ...                                            # error is None, or a DeliveryError

Discovery needs `notify.passphrase` set, the same on every machine: a query is signed with it, and
only a machine that knows it answers - a stranger gets silence, not an error. It's read from
notify's settings, so it's set once (`kit config set notify.passphrase ...`) for every tool.
Messages also carry the receiving machine's own token, which only discovery (or its printed address)
hands out.
"""

from __future__ import annotations

import os

# Avast and similar HTTPS scanners set SSLKEYLOGFILE to a path Python's OpenSSL can't open, which
# aborts the process as soon as the ssl module loads - even for plain http://. Drop it before urllib.
os.environ.pop("SSLKEYLOGFILE", None)

import hashlib  # noqa: E402
import hmac  # noqa: E402
import ipaddress  # noqa: E402
import json  # noqa: E402
import secrets  # noqa: E402
import select  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.error import HTTPError, URLError  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402
from urllib.request import Request, urlopen  # noqa: E402

from kitlib.term import warn  # noqa: E402

DEFAULT_PORT = 8899
DEFAULT_DISCOVERY_PORT = 8898
DEFAULT_TITLE = "kit notify"
# The wire format: unchanged, so machines running an older kit notify keep finding and answering these.
DISCOVER_MAGIC = "kit-notify-discover-v1"
DISCOVER_REPLY_MAGIC = "kit-notify-here-v1"
DISCOVER_WAIT = 1.5    # seconds to collect replies to a broadcast
DISCOVER_SENDS = 3     # copies of each query sent, since a broadcast lost on Wi-Fi is never resent
DISCOVER_RESEND = 0.5  # seconds between those copies
REPLAY_WINDOW = 30     # seconds a query or reply stays valid - limits replaying a captured one


@dataclass(frozen=True)
class Device:
    """A kit machine on this network, as discovery found it (or as its printed address names it)."""

    name: str
    ip: str
    port: int
    token: str

    @property
    def id(self) -> str:
        """Stable per machine, and safe to show - unlike the token, which lets anyone message it."""
        return hashlib.sha256(self.token.encode()).hexdigest()[:12]

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def url(self) -> str:
        """The address `kit notify serve` prints for this machine."""
        return f"http://{self.ip}:{self.port}/?t={self.token}"


class DeliveryError(Exception):
    """A message didn't arrive. `rejected`: the machine answered and said no (wrong token, say);
    otherwise nothing answered on its port - usually a firewall."""

    def __init__(self, reason: str, rejected: bool) -> None:
        super().__init__(reason)
        self.rejected = rejected


# --- settings and identity ---------------------------------------------------------------

def settings() -> dict:
    """notify's settings (passphrase, port, discovery_port), wherever they're read from."""
    values: dict = {}
    try:
        from kitlib import KIT_HOME
        from kitlib.settings import tool_schema, values_for

        values = values_for("notify", tool_schema(KIT_HOME / "tools" / "notify"))
    except Exception:
        pass  # no notify tool here, or no settings file: the defaults below
    return {"passphrase": values.get("passphrase") or "", "port": values.get("port") or DEFAULT_PORT,
            "discovery_port": values.get("discovery_port") or DEFAULT_DISCOVERY_PORT}


def token_path() -> Path:
    """Where this machine's token lives: the key messages to it must carry."""
    override = os.environ.get("KIT_NOTIFY_DATA")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "notify-token"


def own_token() -> str | None:
    """This machine's token, if it has one yet (the first serve makes it)."""
    path = token_path()
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def load_or_create_token(rotate: bool = False) -> str:
    """This machine's token - kept on disk, so its address stays the same across restarts."""
    if not rotate and (token := own_token()):
        return token
    value = secrets.token_urlsafe(16)
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


def lan_ip() -> str:
    """Best effort: the address this machine is reached at on the LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # no packets are sent; this just picks the interface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# --- discovery -------------------------------------------------------------------------------

def _sign(passphrase: str, message: str) -> str:
    return hmac.new(passphrase.encode(), message.encode(), "sha256").hexdigest()


def _fresh(ts: object) -> bool:
    return isinstance(ts, (int, float)) and abs(time.time() - ts) <= REPLAY_WINDOW


def query_packet(passphrase: str, ts: float | None = None) -> bytes:
    ts = time.time() if ts is None else ts
    return json.dumps({"magic": DISCOVER_MAGIC, "ts": ts, "mac": _sign(passphrase, f"{DISCOVER_MAGIC}:{ts}")}).encode()


def reply_packet(passphrase: str, name: str, http_port: int, token: str, ts: float | None = None) -> bytes:
    ts = time.time() if ts is None else ts
    reply = {"magic": DISCOVER_REPLY_MAGIC, "name": name, "port": http_port, "token": token, "ts": ts,
             "mac": _sign(passphrase, f"{DISCOVER_REPLY_MAGIC}:{ts}:{token}")}
    return json.dumps(reply).encode()


def valid_query(data: bytes, passphrase: str) -> bool:
    try:
        query = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(query, dict) or query.get("magic") != DISCOVER_MAGIC or not _fresh(query.get("ts")):
        return False
    return hmac.compare_digest(str(query.get("mac", "")), _sign(passphrase, f"{DISCOVER_MAGIC}:{query['ts']}"))


def parse_reply(data: bytes, passphrase: str, sender_ip: str) -> Device | None:
    """The machine a reply describes, if it's genuine: fresh, and signed with the same passphrase."""
    try:
        reply = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(reply, dict) or reply.get("magic") != DISCOVER_REPLY_MAGIC:
        return None
    port, token, ts = reply.get("port"), reply.get("token"), reply.get("ts")
    if not isinstance(port, int) or not isinstance(token, str) or not token or not _fresh(ts):
        return None
    if not hmac.compare_digest(str(reply.get("mac", "")), _sign(passphrase, f"{DISCOVER_REPLY_MAGIC}:{ts}:{token}")):
        return None  # answered, but doesn't know the same passphrase - don't trust it
    return Device(str(reply.get("name") or sender_ip), sender_ip, port, token)


def respond(discovery_port: int, http_port: int, token: str, passphrase: str, stop: threading.Event) -> None:
    """Answer discovery queries until `stop` is set, so this machine can be found. Blocks: run it
    on a thread. A wrong or missing passphrase gets silence, never an error."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", discovery_port))
    except OSError as exc:
        warn(f"couldn't listen for discovery on UDP {discovery_port} ({exc}) - other machines won't find this one")
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
            if valid_query(data, passphrase):
                try:
                    sock.sendto(reply_packet(passphrase, name, http_port, token), addr)
                except OSError:
                    pass
    finally:
        sock.close()


def broadcast_targets() -> list[tuple[str, str]]:
    """(local address, broadcast address) for every network this machine is really on.

    A plain 255.255.255.255 only leaves by one interface - whichever the routing table picks - and on
    a machine with Docker, WSL or a VPN adapter that is often the wrong one: a Windows box here sent
    every query out the WSL adapter and so only ever found itself. Asking each interface separately
    is the fix. Falls back to the single broadcast when psutil isn't importable.
    """
    try:
        import psutil
    except ImportError:
        return [("", "255.255.255.255")]

    targets: list[tuple[str, str]] = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET or not address.netmask:
                continue
            if address.address.startswith(("127.", "169.254.")):  # loopback, or no DHCP answer
                continue
            try:
                network = ipaddress.IPv4Interface(f"{address.address}/{address.netmask}").network
            except ValueError:
                continue
            if network.prefixlen >= 31:  # point-to-point (Tailscale, VPNs): nothing to broadcast to
                continue
            targets.append((address.address, str(network.broadcast_address)))
    return targets or [("", "255.255.255.255")]


def discover(passphrase: str | None = None, discovery_port: int | None = None, wait: float = DISCOVER_WAIT,
             targets: list[tuple[str, str]] | None = None) -> list[Device]:
    """Every machine that answers within `wait` seconds - [] without a passphrase.

    The query goes out DISCOVER_SENDS times, not once: Wi-Fi never acknowledges or resends a broadcast,
    and one to a Linux laptop here went missing about one search in eight - more, and several in a row,
    while its Wi-Fi was power-saving. Replies are collected per machine, so answering each copy is harmless.
    """
    conf = settings() if passphrase is None or discovery_port is None else {}
    passphrase = conf.get("passphrase", "") if passphrase is None else passphrase
    discovery_port = conf.get("discovery_port", DEFAULT_DISCOVERY_PORT) if discovery_port is None else discovery_port
    if not passphrase:
        return []
    query = query_packet(passphrase)

    sockets: list[tuple[socket.socket, str]] = []
    for local, broadcast in targets or broadcast_targets():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        try:
            if local:
                sock.bind((local, 0))  # binding picks the interface the query goes out of
            sock.sendto(query, (broadcast, discovery_port))
        except OSError:
            sock.close()  # an interface that won't carry it (asleep, no route) isn't worth a message
            continue
        sockets.append((sock, broadcast))
    if not sockets:
        warn("couldn't send a discovery query on any network")
        return []

    found: dict[str, Device] = {}
    try:
        start = time.monotonic()
        sends = 1
        while time.monotonic() < start + wait:
            if sends < DISCOVER_SENDS and time.monotonic() >= start + sends * DISCOVER_RESEND:
                sends += 1
                for sock, broadcast in sockets:
                    try:
                        sock.sendto(query, (broadcast, discovery_port))
                    except OSError:
                        pass  # the first copy went out; a later one failing just means fewer chances
            ready, _, _ = select.select([sock for sock, _ in sockets], [], [], 0.1)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                device = parse_reply(data, passphrase, addr[0])
                # One machine answers once per network it shares with us, so key on its token
                # (stable per machine) rather than the address, or it gets messaged twice.
                if device:
                    found.setdefault(device.token, device)
    finally:
        for sock, _ in sockets:
            sock.close()
    return list(found.values())


def pick(devices: list[Device], names: list[str]) -> tuple[list[Device], list[str]]:
    """The devices named - by hostname in any case, or by address - and the names that matched none."""
    wanted = [name.strip() for name in names if name.strip()]
    picked = [d for d in devices if any(d.name.lower() == w.lower() or d.ip == w for w in wanted)]
    missing = [w for w in wanted if not any(d.name.lower() == w.lower() or d.ip == w for d in devices)]
    return picked, missing


# --- sending -----------------------------------------------------------------------------------

def parse_address(url: str) -> Device | None:
    """The machine behind an address `kit notify serve` printed (http://host:port/?t=...)."""
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("t", [""])[0]
    try:
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or not port or not token:
        return None
    return Device(parsed.hostname, parsed.hostname, port, token)


def send_message(device: Device, message: str, title: str = DEFAULT_TITLE, timeout: float = 10) -> None:
    """Deliver one message, or raise DeliveryError saying why not. The sender's hostname goes along,
    so the popup and the notify window can say who it's from."""
    payload = json.dumps({"title": title, "message": message, "from": socket.gethostname()}).encode("utf-8")
    request = Request(f"http://{device.ip}:{device.port}/notify?t={device.token}", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout):
            pass
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise DeliveryError(f"rejected it ({exc.code}){': ' + detail if detail else ''}", rejected=True) from exc
    except URLError as exc:
        raise DeliveryError(f"didn't answer ({exc.reason})", rejected=False) from exc
    except OSError as exc:  # a timeout while reading the answer
        raise DeliveryError(f"didn't answer ({exc})", rejected=False) from exc


def send_to_all(devices: list[Device], message: str, title: str = DEFAULT_TITLE,
                timeout: float = 10) -> list[tuple[Device, DeliveryError | None]]:
    """Message every device at once, not one after another - one slow or unreachable machine holds up
    nobody else, so the whole thing takes as long as the slowest one. Results come back in order."""
    def one(device: Device) -> tuple[Device, DeliveryError | None]:
        try:
            send_message(device, message, title, timeout)
        except DeliveryError as exc:
            return device, exc
        return device, None

    if not devices:
        return []
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        return list(pool.map(one, devices))
