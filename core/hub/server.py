"""kit hub's web server: tools, runs and launches, settings, updates and doctor, behind a one-time token."""

from __future__ import annotations

import codecs
import hmac
import html
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kitlib import die
from kitlib import settings as settings_api

from core import registry, runner
from core.registry import KIT_HOME, TOOLS_DIR, Tool, current_platform

PAGE = Path(__file__).resolve().parent / "page.html"
IS_WINDOWS = sys.platform.startswith("win")
PORT_ATTEMPTS = 50
MAX_BODY = 64_000
MAX_ARGS = 4_000
MAX_JOB_OUTPUT = 2_000_000
MAX_RUNNING_JOBS = 12
MAX_FINISHED_JOBS = 40
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
SAFE_ARG_RE = re.compile(r"[\w@%+=:,./\\-]+")
DRY_RUN_ENV = "KIT_HUB_DRY_RUN"  # tests: report the terminal / open-file command instead of running it

UNAUTHORIZED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>kit hub</title></head>
<body style="font:15px system-ui,sans-serif;max-width:560px;margin:80px auto;padding:0 20px">
<h1 style="font-size:20px">kit hub needs its access token</h1>
<p>Open the full address printed in the terminal where you ran <code>kit hub</code>
(it ends in <code>?token=...</code>).</p></body></html>"""


class RequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def dry_run() -> bool:
    return bool(os.environ.get(DRY_RUN_ENV))


def discover() -> registry.Registry:
    from core.cli import RESERVED  # imported here: core.cli is the source of the built-in names

    return registry.discover(RESERVED)


def find_tool(name: object) -> Tool:
    if not isinstance(name, str) or not name:
        raise RequestError(HTTPStatus.BAD_REQUEST, "missing tool name")
    tool = discover().resolve(name)
    if tool is None:
        raise RequestError(HTTPStatus.NOT_FOUND, f"unknown tool '{name}'")
    return tool


def split_args(text: object) -> list[str]:
    """Shell-like splitting without a shell: quotes group words; backslashes stay literal on Windows."""
    if text is None:
        return []
    if not isinstance(text, str):
        raise RequestError(HTTPStatus.BAD_REQUEST, "args must be a string")
    if len(text) > MAX_ARGS:
        raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "the arguments are too long")
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if IS_WINDOWS:
        lexer.escape = ""  # keep C:\paths intact
    try:
        return list(lexer)
    except ValueError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"can't read the arguments: {exc}") from None


def display_command(name: str, args: list[str]) -> str:
    def quote(arg: str) -> str:
        return arg if SAFE_ARG_RE.fullmatch(arg) else '"' + arg.replace('"', '\\"') + '"'

    return " ".join(["kit", name, *map(quote, args)])


# --- jobs: running tools, launches, doctor and updates ---------------------------------

class Job:
    def __init__(self, kind: str, tool: str | None, display: str) -> None:
        self.id = secrets.token_hex(6)
        self.kind = kind  # run | launch | doctor | update
        self.tool = tool
        self.display = display
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.started = time.time()
        self.ended: float | None = None
        self.exit_code: int | None = None
        self.url: str | None = None
        self.process: subprocess.Popen | None = None
        self.stopped = False
        self._size = 0
        self._scan = ""

    @property
    def done(self) -> bool:
        return self.ended is not None

    def emit(self, event: dict) -> None:
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def output(self, text: str) -> None:
        if not text:
            return
        if self._size >= MAX_JOB_OUTPUT:
            return
        self._size += len(text)
        if self._size >= MAX_JOB_OUTPUT:
            text += "\n[output truncated]\n"
        if self.kind == "launch" and self.url is None and len(self._scan) < 8000:
            self._scan += ANSI_RE.sub("", text)
            match = URL_RE.search(self._scan)
            if match:
                self.url = match.group(0).rstrip(".,;)")
                self.emit({"type": "url", "url": self.url})
        self.emit({"type": "output", "text": text})

    def finish(self, code: int, error: str | None = None) -> None:
        with self.cond:
            if self.ended is not None:
                return
            self.ended = time.time()
            self.exit_code = code
            event = {"type": "exit", "code": code, "duration": round(self.ended - self.started, 2), "stopped": self.stopped}
            if error:
                event["error"] = error
            self.events.append(event)
            self.cond.notify_all()

    def summary(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "tool": self.tool, "display": self.display,
            "running": not self.done, "exitCode": self.exit_code, "url": self.url, "stopped": self.stopped,
            "started": self.started, "duration": round((self.ended or time.time()) - self.started, 2),
        }


def popen_options() -> dict:
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True  # its own process group, so Stop can end the whole tree
    return options


def output_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env.pop("NO_COLOR", None)
    env.update(FORCE_COLOR="1", COLUMNS="110", LINES="40", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", KIT_HUB="1")
    return env


def start_process(job: Job, command: list[str], env: dict[str, str], cwd: str) -> None:
    try:
        process = subprocess.Popen(command, env=env, cwd=cwd, **popen_options())
    except OSError as exc:
        job.output(f"couldn't start {command[0]}: {exc}\n")
        job.finish(127)
        return
    job.process = process

    def pump() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream = process.stdout
        assert stream is not None
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            job.output(decoder.decode(chunk))
        job.output(decoder.decode(b"", final=True))
        job.finish(process.wait())

    threading.Thread(target=pump, name=f"job-{job.id}", daemon=True).start()


def kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            parent = psutil.Process(process.pid)
            victims = parent.children(recursive=True) + [parent]
        except psutil.NoSuchProcess:
            return
        for victim in victims:
            try:
                victim.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(victims, timeout=5)
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


# --- terminals and opening files ----------------------------------------------------

def terminal_launch(argv: list[str], title: str) -> tuple[list[str] | None, dict]:
    """The command that shows argv in a new terminal window, plus Popen options. (None, {}) if there's no terminal."""
    if IS_WINDOWS:
        wt = shutil.which("wt")
        if wt:
            # Windows Terminal treats ';' as a command separator unless escaped.
            return [wt, "new-tab", "--title", title, "--", *(a.replace(";", r"\;") for a in argv)], {}
        return argv, {"creationflags": subprocess.CREATE_NEW_CONSOLE}
    if sys.platform == "darwin":
        script = shlex.join(argv).replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                "-e", 'tell application "Terminal" to activate'], {"start_new_session": True}
    for name, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"),
                       ("xfce4-terminal", "-x"), ("xterm", "-e")):
        path = shutil.which(name)
        if path:
            return [path, flag, *argv], {"start_new_session": True}
    return None, {}


def open_file_command(path: Path) -> list[str] | None:
    """How to open a file in its default app; None means os.startfile (Windows)."""
    if IS_WINDOWS:
        return None
    return ["open" if sys.platform == "darwin" else "xdg-open", str(path)]


# --- payloads -----------------------------------------------------------------------

_markdown = None


def render_readme(path: Path) -> str:
    global _markdown
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        text = "\n".join(lines[1:])  # the page already shows the tool name
    if _markdown is None:
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            return f"<pre>{html.escape(text)}</pre>"
        # html=False: any HTML in a README is shown as text, never run.
        _markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    return _markdown.render(text)


def tool_summary(tool: Tool) -> dict:
    return {
        "name": tool.name, "summary": tool.summary, "category": tool.category, "aliases": tool.aliases,
        "web": tool.web, "interactive": tool.interactive, "supported": tool.supported,
        "availableOn": tool.available_on, "hasSettings": bool(tool.settings), "hasReadme": tool.readme is not None,
        "external": tool.dir.parent != TOOLS_DIR,
    }


def tools_payload() -> dict:
    reg = discover()
    tools = sorted(reg.tools.values(), key=lambda t: (t.category, t.name))
    return {
        "tools": [tool_summary(t) for t in tools],
        "problems": [{"level": level, "message": message} for level, message in reg.problems],
    }


def tool_detail(tool: Tool) -> dict:
    detail = tool_summary(tool)
    detail["folder"] = str(tool.dir)
    entry = tool.entry_path()
    detail["entry"] = str(entry) if entry else None
    detail["readmeHtml"] = render_readme(tool.readme) if tool.readme else ""
    return detail


def accepts_no_open(tool: Tool) -> bool:
    entry = tool.entry_path()
    try:
        return entry is not None and "--no-open" in entry.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def section_payload(section: str, summary: str, schema: dict, data: dict) -> dict:
    stored = data.get(section, {}) if isinstance(data.get(section), dict) else {}
    resolved = settings_api.resolve(section, schema, data)
    items = []
    for key, item in resolved.items():
        spec = item["spec"]
        env_name = spec.get("env")
        invalid = settings_api.validate(spec, stored[key]) if key in stored else None
        items.append({
            "key": key, "type": spec["type"], "value": item["value"], "source": item["source"],
            "default": spec.get("default"), "help": spec.get("help", ""), "choices": spec.get("choices"),
            "min": spec.get("min"), "max": spec.get("max"), "env": env_name,
            "envActive": bool(env_name and os.environ.get(env_name)),
            "hasStored": key in stored, "invalid": invalid,
        })
    return {"section": section, "summary": summary, "settings": items,
            "unknownKeys": sorted(set(stored) - set(schema))}


def settings_payload() -> dict:
    path = settings_api.config_path()
    error = None
    try:
        data = settings_api.load()
    except settings_api.SettingsError as exc:
        data, error = {}, str(exc)
    sections = [section_payload("kit", "kit itself", settings_api.KIT_SCHEMA, data)]
    for tool in sorted(discover().tools.values(), key=lambda t: t.name):
        if tool.settings:
            sections.append(section_payload(tool.name, tool.summary, tool.settings, data))
    known = {s["section"] for s in sections}
    return {"path": str(path), "exists": path.is_file(), "error": error, "sections": sections,
            "unknownSections": sorted(set(data) - known)}


def schema_for(section: object) -> dict:
    if section == "kit":
        return settings_api.KIT_SCHEMA
    tool = find_tool(section)
    if tool.name != section or not tool.settings:
        raise RequestError(HTTPStatus.NOT_FOUND, f"'{section}' has no settings")
    return tool.settings


def save_setting(body: dict) -> dict:
    section, key = body.get("section"), body.get("key")
    schema = schema_for(section)
    if not isinstance(key, str) or key not in schema:
        raise RequestError(HTTPStatus.NOT_FOUND, f"[{section}] has no setting '{key}'")
    spec = schema[key]
    try:
        if body.get("reset"):
            settings_api.unset_value(section, key)
        else:
            value = body.get("value")
            if isinstance(value, str):
                value = settings_api.parse_value(spec, value)
            else:
                if spec["type"] == "float" and isinstance(value, int) and not isinstance(value, bool):
                    value = float(value)
                problem = settings_api.validate(spec, value)
                if problem:
                    raise settings_api.SettingsError(problem)
            settings_api.set_value(section, key, value)
    except settings_api.SettingsError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, str(exc)) from None
    return {"ok": True, "settings": settings_payload()}


def load_update():
    """(core.update module, None) or (None, reason it can't be used)."""
    try:
        from core import update
    except Exception as exc:
        return None, f"updates aren't available in this copy of kit ({exc.__class__.__name__}: {exc})"
    missing = [name for name in ("check", "apply") if not callable(getattr(update, name, None))]
    if missing:
        return None, f"updates aren't available in this copy of kit (core.update has no {', '.join(missing)})"
    return update, None


def jsonable(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


# --- server ---------------------------------------------------------------------------

class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use.
    allow_reuse_address = not IS_WINDOWS

    def __init__(self, port: int, token: str) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.cookie_name = f"kit_hub_{port}"
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}
        self.cwd = os.getcwd()
        self.jobs: dict[str, Job] = {}
        self.jobs_lock = threading.Lock()
        self.closing = False

    # -- jobs

    def add_job(self, job: Job) -> None:
        with self.jobs_lock:
            if sum(not j.done for j in self.jobs.values()) >= MAX_RUNNING_JOBS:
                raise RequestError(HTTPStatus.TOO_MANY_REQUESTS, "too many tools are running - stop some first")
            finished = sorted((j for j in self.jobs.values() if j.done), key=lambda j: j.started)
            for old in finished[:max(0, len(finished) - MAX_FINISHED_JOBS)]:
                self.jobs.pop(old.id, None)
            self.jobs[job.id] = job

    def get_job(self, job_id: str) -> Job:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise RequestError(HTTPStatus.NOT_FOUND, "no such job")
        return job

    def job_summaries(self) -> list[dict]:
        with self.jobs_lock:
            jobs = list(self.jobs.values())
        return [j.summary() for j in sorted(jobs, key=lambda j: j.started, reverse=True)]

    def stop_all_jobs(self) -> int:
        self.closing = True
        with self.jobs_lock:
            running = [j for j in self.jobs.values() if not j.done and j.process is not None]
        for job in running:
            job.stopped = True
            kill_tree(job.process)
        return len(running)

    def spawn(self, kind: str, tool: Tool, args: list[str]) -> dict:
        try:
            command = runner.build_command(tool, args)
        except runner.RunError as exc:
            raise RequestError(HTTPStatus.CONFLICT, f"{tool.name}: {exc}") from None
        job = Job(kind, tool.name, display_command(tool.name, args))
        self.add_job(job)
        start_process(job, command, output_env(runner.tool_env(tool)), self.cwd)
        return {"job": job.summary()}

    def start_run(self, body: dict) -> dict:
        tool = find_tool(body.get("tool"))
        if not tool.supported:
            raise RequestError(HTTPStatus.CONFLICT, f"'{tool.name}' isn't available on {current_platform()}")
        if tool.interactive:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"'{tool.name}' is a full-screen terminal app - open it in a terminal")
        if tool.web:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"'{tool.name}' starts a web page - use Launch")
        return self.spawn("run", tool, split_args(body.get("args")))

    def start_launch(self, body: dict) -> dict:
        tool = find_tool(body.get("tool"))
        if not tool.supported:
            raise RequestError(HTTPStatus.CONFLICT, f"'{tool.name}' isn't available on {current_platform()}")
        if not tool.web:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"'{tool.name}' isn't a web tool - use Run")
        args = split_args(body.get("args"))
        if "--no-open" not in args and accepts_no_open(tool):
            args.append("--no-open")  # the hub shows the link itself
        return self.spawn("launch", tool, args)

    def stop_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job.done:
            if job.process is None:
                raise RequestError(HTTPStatus.CONFLICT, "this job can't be stopped")
            job.stopped = True
            kill_tree(job.process)
        return {"job": job.summary()}

    def start_doctor(self) -> dict:
        job = Job("doctor", None, "kit doctor")
        self.add_job(job)
        start_process(job, [sys.executable, str(KIT_HOME / "kit.py"), "doctor"], output_env(os.environ), str(KIT_HOME))
        return {"job": job.summary()}

    def start_update(self) -> dict:
        update, reason = load_update()
        if update is None:
            raise RequestError(HTTPStatus.CONFLICT, reason)
        with self.jobs_lock:
            if any(j.kind == "update" and not j.done for j in self.jobs.values()):
                raise RequestError(HTTPStatus.CONFLICT, "an update is already running")
        job = Job("update", None, "kit update")
        self.add_job(job)

        def work() -> None:
            def log(line: object) -> None:
                text = str(line)
                job.output(text if text.endswith("\n") else text + "\n")

            try:
                result = jsonable(update.apply(log=log))
            except Exception as exc:
                job.output(f"update failed: {exc}\n")
                job.finish(1, str(exc))
                return
            job.emit({"type": "result", "result": result})
            job.finish(0 if isinstance(result, dict) and result.get("ok") else 1)

        threading.Thread(target=work, name=f"job-{job.id}", daemon=True).start()
        return {"job": job.summary()}

    # -- other actions

    def open_terminal(self, body: dict) -> dict:
        tool = find_tool(body.get("tool"))
        if not tool.supported:
            raise RequestError(HTTPStatus.CONFLICT, f"'{tool.name}' isn't available on {current_platform()}")
        args = split_args(body.get("args"))
        display = display_command(tool.name, args)
        argv = [sys.executable, str(KIT_HOME / "kit.py"), tool.name, *args]
        command, options = terminal_launch(argv, f"kit {tool.name}")
        if command is None:
            raise RequestError(HTTPStatus.CONFLICT, f"no terminal emulator found - run it yourself: {display}")
        if dry_run():
            return {"dryRun": True, "command": command, "display": display}
        try:
            subprocess.Popen(command, cwd=self.cwd, stdin=subprocess.DEVNULL, **options)
        except OSError as exc:
            raise RequestError(HTTPStatus.INTERNAL_SERVER_ERROR, f"couldn't open a terminal: {exc}") from None
        return {"dryRun": False, "command": command, "display": display}

    def open_settings_file(self) -> dict:
        path = settings_api.config_path()
        command = open_file_command(path)
        described = command or ["start", str(path)]
        if dry_run():
            return {"dryRun": True, "path": str(path), "command": described}
        created = False
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(settings_api.HEADER, encoding="utf-8", newline="\n")
            created = True
        try:
            if command is None:
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise RequestError(HTTPStatus.INTERNAL_SERVER_ERROR, f"couldn't open {path}: {exc}") from None
        return {"dryRun": False, "path": str(path), "created": created}

    def meta(self) -> dict:
        update, reason = load_update()
        return {
            "kitHome": str(KIT_HOME), "platform": current_platform(), "python": sys.version.split()[0],
            "cwd": self.cwd, "settingsPath": str(settings_api.config_path()), "dryRun": dry_run(),
            "updates": {"available": update is not None, "reason": reason},
        }


class Handler(BaseHTTPRequestHandler):
    server: HubServer
    server_version = "kit-hub"
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
        self.wfile.write(body)

    def send_json(self, status: int, payload: object) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def send_page(self, status: int, page: str) -> None:
        self.send_body(status, page.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'",
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

    # -- GET

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
            stream = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/stream", url.path)
            if stream:
                return self.stream_job(stream.group(1), params.get("from", ["0"])[0])
            if url.path == "/api/meta":
                payload: object = self.server.meta()
            elif url.path == "/api/tools":
                payload = tools_payload()
            elif url.path == "/api/tool":
                payload = tool_detail(find_tool(params.get("name", [""])[0]))
            elif url.path == "/api/jobs":
                payload = {"jobs": self.server.job_summaries()}
            elif url.path == "/api/settings":
                payload = settings_payload()
            else:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except RequestError as exc:
            return self.send_error_json(exc.status, str(exc))
        self.send_json(HTTPStatus.OK, payload)

    def stream_job(self, job_id: str, start: str) -> None:
        """Newline-delimited JSON events ({"type": output|url|result|exit|ping, ...}) until the job ends."""
        job = self.server.get_job(job_id)
        try:
            index = max(0, int(start))
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "from must be a number") from None
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        while True:
            with job.cond:
                if index >= len(job.events):
                    job.cond.wait(timeout=10)
                batch = job.events[index:]
                index += len(batch)
            lines: list[str] = []
            pending = ""
            for event in batch:  # merge neighbouring output chunks into one line
                if event["type"] == "output":
                    pending += event["text"]
                    continue
                if pending:
                    lines.append(json.dumps({"type": "output", "text": pending}))
                    pending = ""
                lines.append(json.dumps(event))
            if pending:
                lines.append(json.dumps({"type": "output", "text": pending}))
            try:
                self.wfile.write(("\n".join(lines or ['{"type": "ping"}']) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                return
            if any(event["type"] == "exit" for event in batch) or self.server.closing:
                return

    # -- POST

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
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
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
        server = self.server
        try:
            stop = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/stop", path)
            if stop:
                payload: object = server.stop_job(stop.group(1))
            elif path == "/api/run":
                payload = server.start_run(body)
            elif path == "/api/launch":
                payload = server.start_launch(body)
            elif path == "/api/terminal":
                payload = server.open_terminal(body)
            elif path == "/api/settings":
                payload = save_setting(body)
            elif path == "/api/settings/open":
                payload = server.open_settings_file()
            elif path == "/api/doctor":
                payload = server.start_doctor()
            elif path == "/api/updates/check":
                update, reason = load_update()
                if update is None:
                    raise RequestError(HTTPStatus.CONFLICT, reason)
                payload = {"result": jsonable(update.check(fetch=True))}
            elif path == "/api/updates/apply":
                payload = server.start_update()
            else:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except RequestError as exc:
            return self.send_error_json(exc.status, str(exc))
        self.send_json(HTTPStatus.OK, payload)


def start_server(port: int, explicit: bool, token: str) -> HubServer:
    candidates = [port] if explicit else range(port, min(port + PORT_ATTEMPTS, 65536))
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return HubServer(candidate, token)
        except OSError as exc:
            last_error = exc
    if explicit:
        die(f"can't listen on port {port}: {last_error}")
    die(f"no free port between {port} and {port + PORT_ATTEMPTS - 1}")
