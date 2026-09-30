import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kitlib import lan

PASS = "test-passphrase"
LOCAL = [("", "127.0.0.1")]  # discovery goes to localhost only: no broadcast, same on every machine


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def responder():
    """A machine answering discovery on a free port; yields (port, token)."""
    port, token, stop = free_udp_port(), "machine-token", threading.Event()
    thread = threading.Thread(target=lan.respond, args=(port, 18899, token, PASS, stop), daemon=True)
    thread.start()
    time.sleep(0.2)
    yield port, token
    stop.set()
    thread.join(2)


# --- the protocol ------------------------------------------------------------------

def test_query_needs_the_same_passphrase():
    packet = lan.query_packet(PASS)
    assert lan.valid_query(packet, PASS)
    assert not lan.valid_query(packet, "another passphrase")
    assert not lan.valid_query(b"not json", PASS)


def test_stale_query_is_refused():
    assert not lan.valid_query(lan.query_packet(PASS, ts=time.time() - lan.REPLAY_WINDOW - 5), PASS)


def test_reply_is_trusted_only_with_the_passphrase():
    packet = lan.reply_packet(PASS, "laptop", 8899, "tok")
    assert lan.parse_reply(packet, PASS, "10.0.0.5") == lan.Device("laptop", "10.0.0.5", 8899, "tok")
    assert lan.parse_reply(packet, "wrong", "10.0.0.5") is None


def test_wire_format_is_unchanged():
    """Older kit notify installs must keep understanding these packets."""
    query = json.loads(lan.query_packet(PASS, ts=1.0))
    assert query["magic"] == "kit-notify-discover-v1" and set(query) == {"magic", "ts", "mac"}
    reply = json.loads(lan.reply_packet(PASS, "n", 1, "t", ts=1.0))
    assert reply["magic"] == "kit-notify-here-v1" and set(reply) == {"magic", "name", "port", "token", "ts", "mac"}


# --- discovery ---------------------------------------------------------------------

def test_discover_finds_a_responder(responder):
    port, token = responder
    devices = lan.discover(PASS, port, wait=1.2, targets=LOCAL)
    assert [(d.token, d.port, d.ip) for d in devices] == [(token, 18899, "127.0.0.1")]
    assert devices[0].name == socket.gethostname()


def test_discover_with_the_wrong_passphrase_finds_nothing(responder):
    port, _ = responder
    assert lan.discover("wrong", port, wait=1.0, targets=LOCAL) == []


def test_discover_without_a_passphrase_finds_nothing():
    assert lan.discover("", 9, wait=0.1, targets=LOCAL) == []


def test_pick_by_name_or_address():
    devices = [lan.Device("Laptop", "10.0.0.2", 1, "a"), lan.Device("desk", "10.0.0.3", 1, "b")]
    picked, missing = lan.pick(devices, ["laptop", "10.0.0.3", "phone"])
    assert [d.name for d in picked] == ["Laptop", "desk"] and missing == ["phone"]


def test_device_id_hides_the_token():
    device = lan.Device("n", "1.2.3.4", 1, "secret-token")
    assert "secret" not in device.id and len(device.id) == 12
    assert device.url == "http://1.2.3.4:1/?t=secret-token"


def test_parse_address():
    assert lan.parse_address("http://192.168.1.28:8899/?t=AbCd") == lan.Device("192.168.1.28", "192.168.1.28", 8899, "AbCd")
    assert lan.parse_address("http://host:8899/") is None
    assert lan.parse_address("not a url") is None


# --- sending -----------------------------------------------------------------------

@pytest.fixture
def inbox():
    """A stand-in for notify's server: accepts token 'good', records what arrives."""
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


def test_send_message_delivers_with_the_senders_name(inbox):
    port, received = inbox
    lan.send_message(lan.Device("x", "127.0.0.1", port, "good"), "hello", title="Build")
    assert received == [{"title": "Build", "message": "hello", "from": socket.gethostname()}]


def test_wrong_token_is_rejected(inbox):
    port, _ = inbox
    with pytest.raises(lan.DeliveryError) as caught:
        lan.send_message(lan.Device("x", "127.0.0.1", port, "bad"), "hello")
    assert caught.value.rejected and "401" in str(caught.value)


def test_nothing_listening_is_not_a_rejection():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed again at once: nothing listens there
    with pytest.raises(lan.DeliveryError) as caught:
        lan.send_message(lan.Device("x", "127.0.0.1", port, "good"), "hello", timeout=3)
    assert not caught.value.rejected


def test_send_to_all_reports_each_device_in_order(inbox):
    port, received = inbox
    devices = [lan.Device("a", "127.0.0.1", port, "good"), lan.Device("b", "127.0.0.1", port, "bad")]
    results = lan.send_to_all(devices, "hi")
    assert [d.name for d, _ in results] == ["a", "b"]
    assert results[0][1] is None and results[1][1].rejected
    assert len(received) == 1


def test_token_is_kept_between_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("KIT_NOTIFY_DATA", str(tmp_path / "token"))
    assert lan.own_token() is None
    first = lan.load_or_create_token()
    assert lan.load_or_create_token() == first == lan.own_token()
    assert lan.load_or_create_token(rotate=True) != first
