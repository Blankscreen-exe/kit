"""kit notify end to end: the window's server side, a second 'machine', discovery, sending both ways."""
import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from kitlib import lan

TOOL = Path(__file__).resolve().parents[1] / "tools" / "notify"
PASS = "test-pass-123"


def load_notify():
    spec = importlib.util.spec_from_file_location("notify_main", TOOL / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["notify_main"] = module
    spec.loader.exec_module(module)
    return module


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def world(monkeypatch, tmp_path):
    """This machine's window, plus another machine that only serves - all on localhost."""
    monkeypatch.setenv("KIT_TOOL_DIR", str(TOOL))
    monkeypatch.setenv("KIT_NOTIFY_DATA", str(tmp_path / "token"))
    monkeypatch.setattr(lan, "broadcast_targets", lambda: [("", "127.0.0.1")])
    notify = load_notify()
    popups = []
    monkeypatch.setattr(notify, "show_notification", lambda title, message: popups.append((title, message)))

    discovery = free_port(socket.SOCK_DGRAM)
    other = notify.NotifyServer("127.0.0.1", free_port(), "other-token")
    threading.Thread(target=other.serve_forever, daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=lan.respond, args=(discovery, other.server_address[1], "other-token", PASS, stop),
                     daemon=True).start()

    port = free_port()
    window = notify.Window("win-token", port, discovery, PASS)
    server = notify.NotifyServer("127.0.0.1", port, "win-token", window)
    control = notify.ControlServer(window)
    for s in (server, control):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    threading.Thread(target=window.search_loop, daemon=True).start()

    base = f"http://127.0.0.1:{control.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    with opener.open(f"{base}/?token={control.token}", timeout=10):
        pass  # swaps the token for the window's cookie

    def get(path):
        with opener.open(base + path, timeout=30) as r:
            return json.loads(r.read())

    def post(path, body, origin=base):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Origin": origin})
        try:
            with opener.open(request, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    deadline = time.monotonic() + 5
    while not get("/api/events?since=-1")["devices"] and time.monotonic() < deadline:
        time.sleep(0.2)
    yield {"notify": notify, "window": window, "server": server, "get": get, "post": post, "popups": popups,
           "base": base, "opener": opener, "port": port}
    window.stop.set()
    window.search_now.set()
    stop.set()
    for s in (other, server, control):
        s.shutdown()
        s.server_close()


def test_window_page_needs_its_token(world):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(world["base"] + "/api/events?since=-1", timeout=10)
    assert caught.value.code == 401


def test_window_finds_the_other_machine_not_itself(world):
    devices = world["get"]("/api/events?since=-1")["devices"]
    assert len(devices) == 1 and "token" not in devices[0]


def test_sending_from_the_window(world):
    device_id = world["get"]("/api/events?since=-1")["devices"][0]["id"]
    assert world["post"]("/api/send", {"message": "hello", "to": "all"})[0] == 200
    assert world["post"]("/api/send", {"message": "just you", "to": [device_id]})[0] == 200
    assert world["post"]("/api/send", {"message": "x", "to": ["nope"]})[0] == 409
    assert world["post"]("/api/send", {"message": "  ", "to": "all"})[0] == 400
    time.sleep(1)
    sent = [e for e in world["get"]("/api/events?since=-1")["entries"] if e["dir"] == "out"]
    assert len(sent) == 2 and all(r["status"] == "sent" for e in sent for r in e["to"])
    assert len(world["popups"]) == 2 and all(t.startswith("kit notify - from ") for t, _ in world["popups"])


def test_receiving_lists_and_prints(world, capsys):
    to_window = lan.Device("me", "127.0.0.1", world["port"], "win-token")
    lan.send_message(to_window, "hi window")
    lan.send_message(to_window, "custom title one", title="Build")
    time.sleep(0.3)
    received = [e for e in world["get"]("/api/events?since=-1")["entries"] if e["dir"] == "in"]
    assert [(e["title"], e["message"]) for e in received] == [("", "hi window"), ("Build", "custom title one")]
    assert received[0]["from"] == socket.gethostname()
    printed = capsys.readouterr().out
    assert "hi window" in printed and "Build" in printed


def test_long_poll_wakes_on_a_change(world):
    version = world["get"]("/api/events?since=-1")["version"]
    to_window = lan.Device("me", "127.0.0.1", world["port"], "win-token")
    threading.Timer(0.5, lambda: lan.send_message(to_window, "late")).start()
    started = time.monotonic()
    assert world["get"](f"/api/events?since={version}")["version"] != version
    assert time.monotonic() - started < 3


def test_cross_origin_post_is_refused(world):
    assert world["post"]("/api/send", {"message": "x", "to": "all"}, origin="http://evil.example")[0] == 403


def test_second_server_on_the_same_port_is_refused(world):
    with pytest.raises(OSError):
        world["notify"].NotifyServer("127.0.0.1", world["port"], "x")


def test_window_closing_stops_it(world):
    window = world["window"]
    assert not window.gone()
    request = urllib.request.Request(world["base"] + "/api/bye", data=b"", method="POST")
    world["opener"].open(request, timeout=5)
    time.sleep(world["notify"].BYE_GRACE + 0.5)
    assert window.gone()
