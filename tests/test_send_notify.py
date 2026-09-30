"""kit send telling other machines about its links (kit notify's network, through kitlib.lan)."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kitlib import lan

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "send"


def load_send():
    spec = importlib.util.spec_from_file_location("send_main", TOOL / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["send_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inbox():
    """Stand-in machines running kit notify: token 'good' is accepted, anything else refused."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ok = self.path.endswith("t=good")
            if ok:
                received.append(body)
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], received
    server.shutdown()
    server.server_close()


@pytest.fixture
def send(monkeypatch, tmp_path, inbox):
    """send's app and window, one shared file, and three machines 'on the network'."""
    monkeypatch.setenv("KIT_TOOL_DIR", str(TOOL))
    monkeypatch.setenv("KIT_NOTIFY_DATA", str(tmp_path / "token"))
    module = load_send()
    port, _ = inbox
    own = lan.load_or_create_token()
    machines = [lan.Device("laptop", "127.0.0.1", port, "good"), lan.Device("desk", "127.0.0.1", port, "bad"),
                lan.Device(socket.gethostname(), "127.0.0.1", port, own)]   # this machine answers too
    monkeypatch.setattr(lan, "settings", lambda: {"passphrase": "p", "port": 8899, "discovery_port": 8898})
    monkeypatch.setattr(lan, "discover", lambda *a, **k: list(machines))

    shared = tmp_path / "photos.zip"
    shared.write_bytes(b"x" * 2048)
    app = module.App(module.History(tmp_path / "history.json"), {"expire": "2h", "once": False, "max": None},
                     auto_exit=False, port=8770, explicit_port=False, internet_kind="tunnel", address="", cloudflared="")
    app.mode, app.base = "lan", "http://192.168.1.13:8770"
    share = app.new_share(shared.name, 2048, shared, 7200, None)
    control = module.ControlServer(app, "window-token")
    threading.Thread(target=control.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{control.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.open(f"{base}/?token=window-token", timeout=10).close()

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json",
                                                                          "Origin": base})
        try:
            with opener.open(request, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield {"module": module, "app": app, "share": share, "call": call}
    control.shutdown()
    control.server_close()


def test_the_message_carries_the_link_on_its_own_line(send):
    text = send["module"].notify_text(send["share"], "http://192.168.1.13:8770/d/abc")
    assert text.startswith(f"photos.zip (2.0 KB) from {socket.gethostname()}:")
    assert text.splitlines()[-1] == "http://192.168.1.13:8770/d/abc"


def test_window_lists_the_other_machines_not_this_one(send):
    status, data = send["call"]("/api/devices")
    assert status == 200 and data["passphrase"]
    assert [d["name"] for d in data["devices"]] == ["desk", "laptop"]
    assert all("token" not in d for d in data["devices"])


def test_window_sends_the_link_and_reports_each_machine(send, inbox):
    _, received = inbox
    devices = send["call"]("/api/devices")[1]["devices"]
    status, reply = send["call"]("/api/notify", {"token": send["share"].token, "to": "all"})
    assert status == 200
    outcome = {r["name"]: (r["ok"], r["error"]) for r in reply["results"]}
    assert outcome["laptop"] == (True, "") and outcome["desk"][0] is False and "401" in outcome["desk"][1]
    assert len(received) == 1 and received[0]["title"] == "kit send"
    assert received[0]["message"].endswith(send["app"].link(send["share"]))

    laptop = next(d["id"] for d in devices if d["name"] == "laptop")
    status, reply = send["call"]("/api/notify", {"token": send["share"].token, "to": [laptop]})
    assert status == 200 and [r["name"] for r in reply["results"]] == ["laptop"]


def test_window_needs_a_machine_and_a_live_link(send):
    send["call"]("/api/devices")
    assert send["call"]("/api/notify", {"token": send["share"].token, "to": []})[0] == 400
    assert send["call"]("/api/notify", {"token": "no-such-link", "to": "all"})[0] == 404


def test_window_without_a_passphrase_says_so(send, monkeypatch):
    monkeypatch.setattr(lan, "settings", lambda: {"passphrase": "", "port": 8899, "discovery_port": 8898})
    assert send["call"]("/api/devices") == (200, {"passphrase": False, "devices": []})


def test_cli_notify_picks_by_name(send, inbox, capsys):
    _, received = inbox
    send["module"].cli_notify(send["app"], "laptop,phone")
    out = capsys.readouterr()
    assert "notified" in out.out and "laptop" in out.out
    assert "not found on this network: phone" in out.err
    assert len(received) == 1


def test_cli_notify_file_first_and_no_passphrase(tmp_path):
    """`kit send --notify file`: --notify grabs the file name; send still shares the file, and without a
    passphrase it says why nobody was notified instead of failing."""
    shared = tmp_path / "report.txt"
    shared.write_text("hello")
    config = tmp_path / "config.toml"
    config.write_text("")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "lib"), KIT_TOOL="send", KIT_TOOL_DIR=str(TOOL), KIT_HOME=str(ROOT),
               KIT_CONFIG=str(config), KIT_SEND_DATA=str(tmp_path), SSH_CONNECTION="10.0.0.2 1 10.0.0.1 22",
               NO_COLOR="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(TOOL / "main.py"), "--notify", str(shared), "--expire", "3s",
                             "--no-qr"], cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
                            timeout=60)
    assert "report.txt" in result.stdout and "/d/" in result.stdout
    assert "notify.passphrase isn't set" in result.stderr
