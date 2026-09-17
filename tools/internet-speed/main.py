"""Internet speed test (latency, jitter, download, upload) against Cloudflare, plus TCP ping."""

from __future__ import annotations

import os

# Avast and similar HTTPS scanners set SSLKEYLOGFILE to a path Python's OpenSSL can't open, which
# aborts the whole process as soon as an SSL context is created. Drop it before touching ssl.
os.environ.pop("SSLKEYLOGFILE", None)

import argparse  # noqa: E402
import http.client  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from kitlib import die, error, style  # noqa: E402
from kitlib.settings import tool_settings  # noqa: E402

HOST = "speed.cloudflare.com"
TIMEOUT = 20.0
CHUNK = 64 * 1024
UPLOAD_BLOCK = os.urandom(CHUNK)  # random bytes, so nothing in the path can compress them
# /meta answers 403 without a Referer from the speed test page; the transfer endpoints don't mind.
HEADERS = {"User-Agent": "kit-internet-speed", "Referer": f"https://{HOST}/", "Accept-Encoding": "identity"}

# (bytes per transfer, how many) - grows until the phase's time budget runs out
DOWNLOAD_PLAN = [(100_000, 1), (1_000_000, 3), (10_000_000, 2), (25_000_000, 2)]
UPLOAD_PLAN = [(100_000, 1), (1_000_000, 3), (5_000_000, 2), (10_000_000, 2)]
QUICK_DOWNLOAD_PLAN = [(100_000, 1), (1_000_000, 2), (5_000_000, 1)]
QUICK_UPLOAD_PLAN = [(100_000, 1), (1_000_000, 2), (2_000_000, 1)]
BUDGET = 12.0
QUICK_BUDGET = 5.0
LATENCY_SAMPLES = 10


class SpeedTestError(Exception):
    pass


def explain(exc: BaseException, host: str = HOST) -> str:
    """A readable reason for a network failure."""
    if isinstance(exc, SpeedTestError):
        return str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return (f"couldn't verify {host}'s TLS certificate ({exc.verify_message}). Antivirus HTTPS scanning "
                "(such as Avast's Web Shield) or a proxy may be intercepting the connection.")
    if isinstance(exc, ssl.SSLError):
        return (f"TLS error talking to {host}: {exc.reason or exc}. Antivirus HTTPS scanning or a proxy "
                "may be interfering with the connection.")
    if isinstance(exc, socket.gaierror):
        return f"couldn't look up {host} - check that you're online and that DNS works"
    if isinstance(exc, TimeoutError):  # socket.timeout is TimeoutError since Python 3.10
        return f"{host} didn't respond in time - the connection may be down or very slow"
    if isinstance(exc, (ConnectionError, http.client.RemoteDisconnected)):
        return f"the connection to {host} was refused or dropped ({exc.__class__.__name__})"
    if isinstance(exc, http.client.HTTPException):
        return f"unexpected HTTP response from {host}: {exc!r}"
    if isinstance(exc, OSError):
        return f"network error: {exc.strerror or exc}"
    return f"{exc.__class__.__name__}: {exc}"


# --- HTTP ---------------------------------------------------------------------------

@dataclass
class Transfer:
    size: int
    seconds: float  # body transfer time (after the response headers, for downloads)
    ttfb: float = 0.0  # request sent -> response headers received
    server_ms: float = 0.0  # Cloudflare's own processing time, from Server-Timing
    body: bytes = b""


SERVER_TIMING_RE = re.compile(r"\b(cfSpeedEdge|cfSpeedWorker|cfRequestDuration);dur=([\d.]+)")


def server_timing(response: http.client.HTTPResponse) -> float:
    """Cloudflare's processing time in ms: 'cfSpeedEdge;dur=4, cfSpeedWorker;dur=22' -> 26 (or cfRequestDuration)."""
    durations = dict(SERVER_TIMING_RE.findall(response.getheader("Server-Timing") or ""))
    try:
        if "cfSpeedEdge" in durations or "cfSpeedWorker" in durations:
            return float(durations.get("cfSpeedEdge", 0)) + float(durations.get("cfSpeedWorker", 0))
        return float(durations.get("cfRequestDuration", 0))
    except ValueError:
        return 0.0


class Client:
    """One keep-alive HTTPS connection to Cloudflare, reopened if the server closes it."""

    def __init__(self) -> None:
        self.context = ssl.create_default_context()
        # Python 3.13+ turns on strict X.509 checks, which reject root certificates that don't mark
        # Basic Constraints as critical - including the roots antivirus HTTPS scanners (e.g. Avast)
        # install. The certificate chain and host name are still fully verified without it.
        self.context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        self.conn: http.client.HTTPSConnection | None = None

    def connection(self) -> http.client.HTTPSConnection:
        if self.conn is None:
            self.conn = http.client.HTTPSConnection(HOST, timeout=TIMEOUT, context=self.context)
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get(self, path: str, on_progress=None, keep_body: bool = False) -> Transfer:
        for attempt in (1, 2):
            conn = self.connection()
            try:
                started = time.perf_counter()
                conn.request("GET", path, headers=HEADERS)
                response = conn.getresponse()
                headers_at = time.perf_counter()
                received, chunks = 0, []
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if keep_body:
                        chunks.append(chunk)
                    if on_progress:
                        on_progress(received, time.perf_counter() - headers_at)
                finished = time.perf_counter()
                if response.status != 200:
                    raise SpeedTestError(f"{HOST} answered HTTP {response.status} for {path}")
                return Transfer(received, finished - headers_at, headers_at - started, server_timing(response), b"".join(chunks))
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, http.client.CannotSendRequest):
                self.close()  # a stale keep-alive connection; try once more on a fresh one
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def post(self, path: str, size: int, on_progress=None) -> Transfer:
        for attempt in (1, 2):
            conn = self.connection()
            try:
                conn.putrequest("POST", path, skip_accept_encoding=True)
                for key, value in HEADERS.items():
                    conn.putheader(key, value)
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(size))
                conn.endheaders()
                started, sent = time.perf_counter(), 0
                while sent < size:
                    count = min(CHUNK, size - sent)
                    conn.send(UPLOAD_BLOCK[:count])
                    sent += count
                    if on_progress:
                        on_progress(sent, time.perf_counter() - started)
                response = conn.getresponse()
                finished = time.perf_counter()
                response.read()
                if response.status != 200:
                    raise SpeedTestError(f"{HOST} answered HTTP {response.status} for {path}")
                return Transfer(size, finished - started, server_ms=server_timing(response))
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, http.client.CannotSendRequest):
                self.close()
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")


# --- progress display ------------------------------------------------------------------

def can_encode(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Progress:
    """An animated single line while a phase runs (terminals only), and a result line per phase."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.live = enabled and sys.stdout.isatty()
        fancy = can_encode("⠋█░✓")
        self.frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if fancy else "|/-\\"
        self.full, self.empty = ("█", "░") if fancy else ("#", "-")
        self.check = "✓" if fancy else "+"
        self.frame = 0
        self.last_draw = 0.0

    def show(self, phase: str, fraction: float, detail: str = "") -> None:
        if not self.live:
            return
        now = time.perf_counter()
        if now - self.last_draw < 0.08 and fraction < 1:
            return
        self.last_draw = now
        self.frame = (self.frame + 1) % len(self.frames)
        width = 24
        filled = max(0, min(width, round(fraction * width)))
        bar = style(self.full * filled, "cyan") + style(self.empty * (width - filled), "dim")
        sys.stdout.write(f"\r  {style(self.frames[self.frame], 'cyan')} {phase:<9} {bar}  {detail}\x1b[K")
        sys.stdout.flush()

    def clear(self) -> None:
        if self.live:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

    def done(self, phase: str, text: str) -> None:
        if not self.enabled:
            return
        self.clear()
        print(f"  {style(self.check, 'green')} {phase:<9} {text}", flush=True)


def human_bytes(size: int) -> str:
    return f"{size / 1_000_000:g} MB" if size >= 1_000_000 else f"{size / 1000:g} kB"


# --- measurements ----------------------------------------------------------------------

def fetch_meta(client: Client) -> dict:
    """Where the test runs and who your provider is. Optional: an odd answer just means no details."""
    try:
        transfer = client.get("/meta", keep_body=True)
        data = json.loads(transfer.body)
    except (SpeedTestError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    colo = data.get("colo")
    code, colo_city = (colo.get("iata") or colo.get("code"), colo.get("city")) if isinstance(colo, dict) else (colo, None)
    return {
        "colo": code,
        "colo_city": colo_city,
        "city": data.get("city"),
        "country": data.get("country"),
        "isp": data.get("asOrganization"),
        "asn": data.get("asn"),
        "ip": data.get("clientIp"),
    }


def measure_latency(client: Client, progress: Progress) -> tuple[float, float, list[float]]:
    client.get("/__down?bytes=0")  # warm-up: DNS, TCP and TLS handshakes aren't latency
    samples = []
    for index in range(LATENCY_SAMPLES):
        transfer = client.get("/__down?bytes=0")
        rtt = max(transfer.ttfb * 1000 - transfer.server_ms, 0.1)
        samples.append(rtt)
        progress.show("Latency", (index + 1) / LATENCY_SAMPLES, f"{rtt:.0f} ms")
    latency = statistics.median(samples)
    jitter = statistics.mean(abs(a - b) for a, b in zip(samples, samples[1:])) if len(samples) > 1 else 0.0
    return latency, jitter, samples


def measure_speed(client: Client, direction: str, plan: list[tuple[int, int]], budget: float, progress: Progress) -> list[dict]:
    label = "Download" if direction == "down" else "Upload"
    results: list[dict] = []
    phase_started = time.perf_counter()
    for step, (size, count) in enumerate(plan):
        if step and results:
            # Skip a bigger step that would blow well past the time budget at the speed seen so far.
            last_mbps = results[-1]["mbps"] or 0.001
            expected = size * 8 / (last_mbps * 1_000_000) * count
            if time.perf_counter() - phase_started + expected > budget * 1.5:
                break
        for _ in range(count):
            def on_progress(done: int, elapsed: float, size: int = size) -> None:
                speed = f"{done * 8 / elapsed / 1_000_000:6.1f} Mbps" if elapsed > 0.05 else "   ... Mbps"
                progress.show(label, done / size, f"{speed}  ({human_bytes(size)})")

            if direction == "down":
                transfer = client.get(f"/__down?bytes={size}", on_progress)
            else:
                transfer = client.post("/__up", size, on_progress)
            seconds = max(transfer.seconds, 1e-6)
            results.append({"bytes": transfer.size, "seconds": round(seconds, 4), "mbps": round(transfer.size * 8 / seconds / 1_000_000, 2)})
        if time.perf_counter() - phase_started > budget:
            break
    return results


def summarize_speed(results: list[dict]) -> float | None:
    """90th-percentile speed of the transfers long enough to be meaningful."""
    usable = sorted(r["mbps"] for r in results if r["seconds"] >= 0.25) or sorted(r["mbps"] for r in results)
    if not usable:
        return None
    return usable[max(0, math.ceil(0.9 * len(usable)) - 1)]


def rating(download: float, latency: float) -> tuple[str, str, str]:
    """(label, what it's good for, colour)."""
    if download >= 200:
        result = ("Excellent", "4K streaming on several screens, huge downloads and gaming", "green")
    elif download >= 50:
        result = ("Good", "HD streaming, video calls and large downloads", "green")
    elif download >= 15:
        result = ("Fair", "streaming and video calls; big downloads take a while", "yellow")
    elif download >= 5:
        result = ("Slow", "browsing and SD video; video calls may stutter", "yellow")
    else:
        result = ("Very slow", "basic browsing only", "red")
    if latency > 100 and result[2] == "green":
        result = (result[0], result[1] + " (but high latency hurts calls and gaming)", "yellow")
    return result


def latency_colour(ms: float) -> str:
    return "green" if ms < 40 else "yellow" if ms < 100 else "red"


def run_speed_test(args: argparse.Namespace) -> int:
    progress = Progress(enabled=not args.json)
    client = Client()
    quick = args.quick
    report: dict = {}
    try:
        if not args.json:
            print(style("Internet speed test", "bold") + style("  ·  speed.cloudflare.com", "dim"), flush=True)
        progress.show("Server", 0.0, "connecting...")
        meta = fetch_meta(client)
        where = " · ".join(str(p) for p in (meta.get("colo"), meta.get("colo_city") or meta.get("city")) if p)
        isp = f"{meta.get('isp')} (AS{meta['asn']})" if meta.get("isp") and meta.get("asn") else meta.get("isp") or ""
        progress.done("Server", f"Cloudflare {where}" if where else "Cloudflare")
        report["server"] = {"colo": meta.get("colo"), "city": meta.get("colo_city") or meta.get("city"), "country": meta.get("country")}
        report["isp"] = meta.get("isp")
        report["asn"] = meta.get("asn")
        if args.show_ip:
            report["ip"] = meta.get("ip")

        latency, jitter, samples = measure_latency(client, progress)
        progress.done("Latency", f"{latency:.0f} ms  (jitter {jitter:.1f} ms)")
        report.update(latency_ms=round(latency, 1), jitter_ms=round(jitter, 1), latency_samples_ms=[round(s, 1) for s in samples])

        download = upload = None
        if not args.upload_only:
            results = measure_speed(client, "down", QUICK_DOWNLOAD_PLAN if quick else DOWNLOAD_PLAN, QUICK_BUDGET if quick else BUDGET, progress)
            download = summarize_speed(results)
            progress.done("Download", f"{download:.1f} Mbps" if download is not None else "no result")
            report.update(download_mbps=download, download_samples=results)
        if not args.download_only:
            results = measure_speed(client, "up", QUICK_UPLOAD_PLAN if quick else UPLOAD_PLAN, QUICK_BUDGET if quick else BUDGET, progress)
            upload = summarize_speed(results)
            progress.done("Upload", f"{upload:.1f} Mbps" if upload is not None else "no result")
            report.update(upload_mbps=upload, upload_samples=results)
    except KeyboardInterrupt:
        progress.clear()
        print("cancelled", file=sys.stderr)
        return 130
    except (OSError, http.client.HTTPException, SpeedTestError) as exc:
        progress.clear()
        message = explain(exc)
        if args.json:
            print(json.dumps({"error": message}))
        error(message)
        return 1
    finally:
        client.close()

    verdict = rating(download, latency) if download is not None else None
    if verdict:
        report["rating"] = verdict[0]
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    rows = [("Server", f"Cloudflare {where}" if where else "Cloudflare")]
    if isp:
        rows.append(("Provider", isp))
    if args.show_ip and meta.get("ip"):
        rows.append(("IP", meta["ip"]))
    rows.append(("Latency", style(f"{latency:.0f} ms", "bold", latency_colour(latency)) + style(f"   jitter {jitter:.1f} ms", "dim")))
    colour = verdict[2] if verdict else "cyan"
    if download is not None:
        rows.append(("Download", style(f"{download:.1f} Mbps", "bold", colour)))
    if upload is not None:
        rows.append(("Upload", style(f"{upload:.1f} Mbps", "bold", "cyan")))
    if verdict:
        rows.append(("Rating", style(verdict[0], "bold", verdict[2]) + f" - {verdict[1]}"))
    print()
    for label, value in rows:
        print(f"  {style(label.ljust(9), 'bold')} {value}")
    return 0


# --- TCP ping ----------------------------------------------------------------------------

def ping_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit internet-speed ping", description="Time TCP connections to a host (no admin rights needed).")
    parser.add_argument("host", help="host name or IP address")
    parser.add_argument("-p", "--port", type=int, default=tool_settings().get("ping_port", 443),
                        help="TCP port to connect to (setting: internet-speed.ping_port, default: 443)")
    parser.add_argument("-n", "--count", type=int, default=4, help="number of connections (default: 4)")
    # 3 s rather than 2: Windows retries a refused connection for about 2 s before reporting it.
    parser.add_argument("-t", "--timeout", type=float, default=3.0, help="seconds to wait for each connection (default: 3)")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="seconds between connections (default: 1)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.count < 1:
        parser.error("count must be at least 1")
    if args.timeout <= 0 or args.interval < 0:
        parser.error("timeout must be positive and interval can't be negative")

    try:
        infos = socket.getaddrinfo(args.host, args.port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        die(f"couldn't resolve '{args.host}' - check the name, your connection and DNS")
    family, socktype, proto, _, address = infos[0]
    print(f"TCP ping {style(args.host, 'bold')} ({address[0]}) port {args.port}")

    times: list[float] = []
    sent = 0
    try:
        for seq in range(1, args.count + 1):
            sent += 1
            started = time.perf_counter()
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(args.timeout)
            try:
                sock.connect(address)
                ms = (time.perf_counter() - started) * 1000
                times.append(ms)
                print(f"  seq={seq}  connected in {style(f'{ms:.1f} ms', latency_colour(ms))}")
            except TimeoutError:
                print(f"  seq={seq}  {style('timed out', 'red')} after {args.timeout:g}s")
            except ConnectionRefusedError:
                print(f"  seq={seq}  {style('refused', 'red')} - nothing is listening on port {args.port}")
            except OSError as exc:
                print(f"  seq={seq}  {style('failed', 'red')} - {exc.strerror or exc}")
            finally:
                sock.close()
            if seq < args.count:
                time.sleep(max(0.0, args.interval - (time.perf_counter() - started)))
    except KeyboardInterrupt:
        print()

    received = len(times)
    loss = 100 * (sent - received) / sent
    print()
    summary = f"  {sent} sent, {received} connected, {style(f'{loss:.0f}% loss', 'green' if loss == 0 else 'red')}"
    if times:
        summary += f"   min/avg/max {min(times):.1f} / {statistics.mean(times):.1f} / {max(times):.1f} ms"
    print(summary)
    return 0 if received else 1


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    argv = sys.argv[1:]
    if argv and argv[0] == "ping":
        return ping_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="kit internet-speed",
        description="Test latency, jitter, download and upload speed against Cloudflare.",
        epilog="Also: kit internet-speed ping <host> [-p PORT] [-n COUNT] - time TCP connections to any host.",
    )
    parser.add_argument("--quick", action="store_true", help="smaller transfers, done in a few seconds (setting: internet-speed.quick)")
    parser.add_argument("--full", dest="quick", action="store_false", help="full-size transfers, even when the quick setting is on")
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--download-only", action="store_true", help="skip the upload test")
    direction.add_argument("--upload-only", action="store_true", help="skip the download test")
    parser.add_argument("--json", action="store_true", help="print machine-readable results only")
    parser.add_argument("--show-ip", action="store_true", help="include your public IP address")
    parser.set_defaults(quick=tool_settings().get("quick", False))
    return run_speed_test(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
