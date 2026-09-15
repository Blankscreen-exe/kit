"""Local web dashboard for Docker: containers, images, volumes and networks, with common actions."""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kitlib import die, style, warn

PAGE = Path(__file__).resolve().parent / "page.html"
DEFAULT_PORT = 9900
PORT_ATTEMPTS = 50
MAX_LOG_LINES = 5000
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")

# action -> docker sub-command; the container name is appended. Remove never forces.
ACTIONS = {
    "start": ["start"],
    "stop": ["stop"],
    "restart": ["restart"],
    "pause": ["pause"],
    "unpause": ["unpause"],
    "remove": ["rm"],
}

UNAUTHORIZED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>docker-view</title></head>
<body style="font:15px system-ui,sans-serif;max-width:560px;margin:80px auto;padding:0 20px">
<h1 style="font-size:20px">docker-view needs its access token</h1>
<p>Open the full address printed in the terminal where you ran <code>kit docker-view</code>
(it ends in <code>?token=...</code>).</p></body></html>"""


# --- docker CLI -------------------------------------------------------------------

class DockerError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.CONFLICT) -> None:
        super().__init__(message)
        self.status = status


def explain(message: str) -> tuple[str, int]:
    """Turn common docker CLI failures into advice. Returns (message, HTTP status)."""
    low = message.lower()
    if "permission denied" in low and ("docker.sock" in low or "docker daemon socket" in low):
        return ("Permission denied on the Docker socket. Add your user to the docker group "
                "(sudo usermod -aG docker $USER, then log out and back in), or run kit with sudo.",
                HTTPStatus.SERVICE_UNAVAILABLE)
    if ("cannot connect to the docker daemon" in low or "is the docker daemon running" in low
            or "error during connect" in low or ("pipe" in low and "cannot find the file" in low)):
        return ("The Docker daemon isn't running. Start Docker Desktop (Windows/macOS) "
                "or run: sudo systemctl start docker (Linux).", HTTPStatus.SERVICE_UNAVAILABLE)
    return message, HTTPStatus.CONFLICT


def docker(*args: str, timeout: float = 30, merge_stderr: bool = False) -> str:
    """Run the docker CLI with an argument list (never through a shell) and return stdout."""
    kwargs: dict = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["docker", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, **kwargs,
        )
    except FileNotFoundError:
        raise DockerError("The docker command wasn't found. Install Docker and make sure 'docker' is on PATH.",
                          HTTPStatus.SERVICE_UNAVAILABLE) from None
    except subprocess.TimeoutExpired:
        raise DockerError(f"'docker {args[0]}' timed out after {timeout:.0f}s", HTTPStatus.GATEWAY_TIMEOUT) from None
    if result.returncode != 0:
        output = (result.stdout if merge_stderr else result.stderr) or ""
        message, status = explain(output.strip() or f"docker {args[0]} failed with exit code {result.returncode}")
        raise DockerError(message, status)
    return result.stdout


def json_lines(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def health_of(status: str) -> str:
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(healthy)" in status:
        return "healthy"
    if "health: starting" in status:
        return "starting"
    return ""


def list_containers() -> list[dict]:
    # Labels come back as one "k=v,k=v" string whose values can contain commas, so ask for the two we need.
    template = '{{json .}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}'
    containers = []
    for line in docker("ps", "-a", "--no-trunc", "--format", template).splitlines():
        if not line.strip():
            continue
        raw, project, service = line.rsplit("\t", 2)
        item = json.loads(raw)
        status = item.get("Status", "")
        containers.append({
            "id": item.get("ID", "")[:12],
            "name": item.get("Names", "").split(",")[0],
            "image": item.get("Image", ""),
            "state": item.get("State", ""),
            "status": status,
            "health": health_of(status),
            "ports": item.get("Ports", ""),
            "created": item.get("RunningFor", ""),
            "project": project.strip(),
            "service": service.strip(),
        })
    return containers


def container_stats() -> dict:
    stats = {}
    for item in json_lines(docker("stats", "--no-stream", "--format", "{{json .}}", timeout=60)):
        stats[item.get("Name", "")] = {
            "cpu": item.get("CPUPerc", ""),
            "mem": item.get("MemUsage", ""),
            "memPerc": item.get("MemPerc", ""),
            "net": item.get("NetIO", ""),
            "pids": item.get("PIDs", ""),
        }
    return stats


def list_images() -> list[dict]:
    return [{
        "repository": i.get("Repository", ""),
        "tag": i.get("Tag", ""),
        "id": i.get("ID", "").removeprefix("sha256:")[:12],
        "size": i.get("Size", ""),
        "created": i.get("CreatedSince", ""),
    } for i in json_lines(docker("images", "--format", "{{json .}}"))]


def list_volumes() -> list[dict]:
    return [{
        "name": v.get("Name", ""),
        "driver": v.get("Driver", ""),
        "mountpoint": v.get("Mountpoint", ""),
    } for v in json_lines(docker("volume", "ls", "--format", "{{json .}}"))]


def list_networks() -> list[dict]:
    return [{
        "name": n.get("Name", ""),
        "id": n.get("ID", ""),
        "driver": n.get("Driver", ""),
        "scope": n.get("Scope", ""),
        "internal": n.get("Internal", "") == "true",
    } for n in json_lines(docker("network", "ls", "--format", "{{json .}}"))]


def docker_info() -> dict:
    data = json.loads(docker("version", "--format", "{{json .}}"))
    server = data.get("Server") or {}
    return {
        "client": (data.get("Client") or {}).get("Version", ""),
        "server": server.get("Version", ""),
        "os": server.get("Os", ""),
        "arch": server.get("Arch", ""),
    }


def resolve_container(target: object) -> str:
    """Accept only an existing container's exact name or ID; returns its name."""
    if not isinstance(target, str) or not NAME_RE.fullmatch(target):
        raise DockerError("invalid container name or id", HTTPStatus.BAD_REQUEST)
    for container in list_containers():
        if target == container["name"] or (len(target) >= 12 and target[:12] == container["id"]):
            return container["name"]
    raise DockerError(f"no container named '{target}'", HTTPStatus.NOT_FOUND)


def run_action(action: object, target: object) -> dict:
    if not isinstance(action, str) or action not in ACTIONS:
        raise DockerError(f"unknown action '{action}'", HTTPStatus.BAD_REQUEST)
    name = resolve_container(target)
    args = [*ACTIONS[action], name]
    docker(*args, timeout=120)
    return {"ok": True, "action": action, "container": name, "command": "docker " + " ".join(args)}


def container_logs(target: object, tail: str) -> dict:
    name = resolve_container(target)
    try:
        lines = max(1, min(int(tail), MAX_LOG_LINES))
    except ValueError:
        raise DockerError("tail must be a number", HTTPStatus.BAD_REQUEST) from None
    args = ["logs", "--tail", str(lines), "--timestamps", name]
    return {"container": name, "logs": docker(*args, merge_stderr=True), "command": "docker " + " ".join(args)}


GET_ROUTES = {
    "/api/info": docker_info,
    "/api/containers": lambda: {"containers": list_containers()},
    "/api/stats": lambda: {"stats": container_stats()},
    "/api/images": lambda: {"images": list_images()},
    "/api/volumes": lambda: {"volumes": list_volumes()},
    "/api/networks": lambda: {"networks": list_networks()},
}


# --- web server -------------------------------------------------------------------

def log(message: str) -> None:
    print(f"{style(time.strftime('%H:%M:%S'), 'dim')}  {message}", flush=True)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, port: int, token: str) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.cookie_name = f"kit_docker_view_{port}"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "kit-docker-view"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep the terminal quiet; actions are logged explicitly

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
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def send_page(self, status: int, html: str) -> None:
        self.send_body(status, html.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        })

    # -- checks

    def host_ok(self) -> bool:
        # Blocks DNS-rebinding: pages on other domains that resolve to 127.0.0.1 send their own Host.
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

        params = parse_qs(url.query)
        try:
            if url.path == "/api/logs":
                payload = container_logs(params.get("container", [""])[0], params.get("tail", ["300"])[0])
            elif url.path in GET_ROUTES:
                payload = GET_ROUTES[url.path]()
            else:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except DockerError as exc:
            return self.send_error_json(exc.status, str(exc))
        self.send_json(HTTPStatus.OK, payload)

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
        if urlparse(self.path).path != "/api/action":
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length > 10_000:
            return self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON")
        if not isinstance(body, dict):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "expected a JSON object")

        try:
            result = run_action(body.get("action"), body.get("container"))
        except DockerError as exc:
            log(f"{style('failed', 'red')}  {body.get('action')} {body.get('container')}: {exc}")
            return self.send_error_json(exc.status, str(exc))
        log(f"{style('ran', 'green')}     {result['command']}")
        self.send_json(HTTPStatus.OK, result)


def start_server(port: int, explicit: bool, token: str) -> DashboardServer:
    candidates = [port] if explicit else range(port, port + PORT_ATTEMPTS)
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return DashboardServer(candidate, token)
        except OSError as exc:
            last_error = exc
    if explicit:
        die(f"can't listen on port {port}: {last_error}")
    die(f"no free port between {port} and {port + PORT_ATTEMPTS - 1}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit docker-view", description="Local web dashboard for Docker containers, images, volumes and networks.")
    parser.add_argument("--port", type=int, help=f"port to listen on (default: {DEFAULT_PORT}, or the next free one)")
    parser.add_argument("--no-open", action="store_true", help="don't open the dashboard in your browser")
    args = parser.parse_args()

    if not shutil.which("docker"):
        die("the docker command wasn't found - install Docker and make sure 'docker' is on PATH")
    try:
        info = docker_info()
        engine = f"Docker {info['server']} ({info['os']}/{info['arch']})"
    except DockerError as exc:
        warn(f"{exc} The dashboard will keep retrying.")
        engine = "Docker not reachable yet"

    token = secrets.token_urlsafe(24)
    server = start_server(args.port or DEFAULT_PORT, args.port is not None, token)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    print(f"{style('docker-view', 'bold', 'cyan')}  {engine}")
    print(f"  open  {style(url, 'bold')}")
    print(style("  only reachable from this computer - press Ctrl+C to stop", "dim"), flush=True)
    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
