"""kit receive: the code, sending in pieces, resuming, limits and safe file names, over real HTTP."""
import http.client
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "receive"


def load_receive():
    spec = importlib.util.spec_from_file_location("receive_main", TOOL / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["receive_main"] = module
    spec.loader.exec_module(module)
    return module


receive = load_receive()


class Client:
    """What the upload page does, as plain requests."""

    def __init__(self, port: int, token: str) -> None:
        self.port, self.base, self.session = port, f"/u/{token}/", ""

    def request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers={"X-Kit-Session": self.session, **(headers or {})})
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        return response.status, data, response

    def post(self, route, payload):
        status, data, _ = self.request("POST", self.base + route, json.dumps(payload).encode(),
                                       {"Content-Type": "application/json"})
        return status, json.loads(data or b"{}")

    def sign_in(self, code):
        status, body = self.post("code", {"code": code})
        if status == 200:
            self.session = body["session"]
        return status, body

    def start(self, *files):
        return self.post("start", {"files": [{"name": n, "size": len(d), "modified": 1700000000000}
                                             for n, d in files]})

    def chunk(self, upload_id, offset, data):
        status, body, _ = self.request("PUT", f"{self.base}chunk?id={upload_id}&offset={offset}", data,
                                       {"Content-Type": "application/octet-stream"})
        return status, json.loads(body or b"{}")

    def send(self, name, data, piece=1000):
        status, body = self.start((name, data))
        assert status == 200, body
        entry = body["files"][0]
        offset = entry["received"]
        while offset < len(data):
            status, answer = self.chunk(entry["id"], offset, data[offset:offset + piece])
            assert status == 200, answer
            offset = answer["received"]
        return answer


@pytest.fixture
def make(tmp_path, monkeypatch):
    """A receiver serving on 127.0.0.1, and a client for it."""
    monkeypatch.setenv("KIT_TOOL_DIR", str(TOOL))
    servers = []

    def factory(code="123456", expire=3600, once=False, max_size=None):
        folder = tmp_path / "in"
        folder.mkdir(exist_ok=True)
        receiver = receive.Receiver(folder, code, expire, once, max_size)
        receiver.base = "http://127.0.0.1"
        server = receive.QuietServer(("127.0.0.1", 0), receiver)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return receiver, Client(server.server_address[1], receiver.token), folder

    yield factory
    for server in servers:
        server.shutdown()
        server.server_close()


def test_the_page_needs_the_whole_link(make):
    receiver, client, _ = make()
    status, data, _ = client.request("GET", client.base)
    assert status == 200 and b'data-code="1"' in data and b"upload.js" in data
    status, _, response = client.request("GET", client.base.rstrip("/"))
    assert status == 308 and response.getheader("Location") == client.base
    status, data, _ = client.request("GET", "/u/" + "x" * 22 + "/")
    assert status == 404 and b"This link doesn&#x27;t work" in data
    status, data, _ = client.request("GET", "/")
    assert status == 404
    status, data, response = client.request("GET", client.base + "upload.js")
    assert status == 200 and response.getheader("Content-Type").startswith("text/javascript")
    assert "script-src 'self'" in client.request("GET", client.base)[2].getheader("Content-Security-Policy")


def test_nothing_happens_without_the_code(make):
    receiver, client, folder = make()
    assert client.start(("a.txt", b"hi"))[0] == 401
    status, body = client.sign_in("000000")
    assert status == 403 and body["left"] == 4
    status, body = client.sign_in("123 456")      # typed with the space it's shown with
    assert status == 200 and body["session"]
    assert client.send("a.txt", b"hi")["done"]
    assert (folder / "a.txt").read_bytes() == b"hi"


def test_five_wrong_codes_close_the_link(make):
    receiver, client, _ = make()
    for left in (4, 3, 2, 1):
        assert client.sign_in("999999") == (403, {"error": "That's not the code.", "left": left})
    status, body = client.sign_in("999999")
    assert status == 410 and "last try" in body["error"]
    assert client.sign_in("123456")[0] == 410     # even the right code is too late now
    assert client.request("GET", client.base)[0] == 410
    receiver.tick()
    assert receiver.finished.is_set()


def test_an_empty_code_is_not_a_try(make):
    receiver, client, _ = make()
    assert client.sign_in("")[0] == 403
    assert receiver.wrong == 0


def test_no_code(make):
    receiver, client, folder = make(code="")
    assert b'data-code="0"' in client.request("GET", client.base)[1]
    assert client.sign_in("")[0] == 200
    client.send("b.bin", b"\x00" * 5000)
    assert (folder / "b.bin").stat().st_size == 5000


def test_pieces_in_order_only(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    data = os.urandom(3000)
    upload_id = client.start(("c.bin", data))[1]["files"][0]["id"]
    assert client.chunk(upload_id, 0, data[:1000])[0] == 200
    status, body = client.chunk(upload_id, 0, data[:1000])          # sent twice: kit says where it is
    assert status == 409 and body["received"] == 1000
    status, body = client.chunk(upload_id, 2000, data[2000:])       # skipped ahead
    assert status == 409 and body["received"] == 1000
    assert client.chunk(upload_id, 1000, data[1000:])[1]["done"]
    assert (folder / "c.bin").read_bytes() == data


def test_a_piece_past_the_end_is_refused(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    upload_id = client.start(("d.bin", b"12345"))[1]["files"][0]["id"]
    status, body = client.chunk(upload_id, 0, b"123456")
    assert status == 400 and "bad piece" in body["error"]
    assert client.chunk(upload_id, 0, b"12345")[1]["done"]


def test_resume_after_a_cut_and_in_a_later_run(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    data = os.urandom(10_000)
    upload_id = client.start(("movie.mp4", data))[1]["files"][0]["id"]
    client.chunk(upload_id, 0, data[:4000])
    parts = list(folder.glob("*.kit-part"))
    assert len(parts) == 1 and parts[0].stat().st_size == 4000 and parts[0].name.startswith("movie.mp4.")

    # kit receive stopped and started again on the same folder: a new link, the same file carries on
    receiver2, client2, _ = make()
    client2.sign_in("123456")
    entry = client2.start(("movie.mp4", data))[1]["files"][0]
    assert entry == {"id": upload_id, "received": 4000, "done": False}
    client2.send("movie.mp4", data)
    assert (folder / "movie.mp4").read_bytes() == data
    assert not list(folder.glob("*.kit-part"))


def test_never_overwrites(make):
    receiver, client, folder = make()
    (folder / "report.pdf").write_bytes(b"mine")
    client.sign_in("123456")
    client.send("report.pdf", b"theirs")
    client.send("report.pdf", b"theirs, again")     # a different file with the same name
    assert (folder / "report.pdf").read_bytes() == b"mine"
    assert (folder / "report (2).pdf").read_bytes() == b"theirs"
    assert (folder / "report (3).pdf").read_bytes() == b"theirs, again"


def test_the_same_file_twice_is_done_once(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    client.send("e.txt", b"hello")
    status, body = client.start(("e.txt", b"hello"), ("e.txt", b"hello"))
    assert status == 200 and [f["done"] for f in body["files"]] == [True, True]
    assert body["files"][0]["id"] == body["files"][1]["id"]
    assert sorted(p.name for p in folder.iterdir()) == ["e.txt"]


@pytest.mark.parametrize("raw, expected", [
    ("../../evil.txt", "evil.txt"),
    ("..\\..\\Windows\\evil.dll", "evil.dll"),
    ("C:\\Users\\x\\a.txt", "a.txt"),
    ("/etc/passwd", "passwd"),
    ("..", "file"),
    ("", "file"),
    ("a<b>c:d\"e|f?g*h.txt", "a_b_c_d_e_f_g_h.txt"),
    ("bad\x00name\n.txt", "bad_name_.txt"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("trailing. . ", "trailing"),
    ("photo.JPG", "photo.JPG"),
    ("ünïcode – файл.pdf", "ünïcode – файл.pdf"),
])
def test_clean_name(raw, expected):
    assert receive.clean_name(raw) == expected


def test_clean_name_keeps_long_names_short_but_their_extension():
    name = receive.clean_name("é" * 300 + ".mkv")
    assert name.endswith(".mkv") and len(name.encode()) <= 200


def test_a_hostile_name_lands_in_the_folder(make, tmp_path):
    receiver, client, folder = make()
    client.sign_in("123456")
    client.send("../../outside.txt", b"x")
    assert (folder / "outside.txt").exists()
    assert not (tmp_path / "outside.txt").exists()


def test_size_limit(make):
    receiver, client, folder = make(max_size=100)
    client.sign_in("123456")
    status, body = client.start(("big.iso", b"x" * 101))
    assert status == 413 and "up to 100 B" in body["error"]
    assert client.start(("ok.txt", b"x" * 100))[0] == 200


def test_no_room(make, monkeypatch):
    receiver, client, folder = make()
    client.sign_in("123456")
    usage = type("usage", (), {"total": 100 << 30, "free": (1 << 30) + 500})
    monkeypatch.setattr(receive.shutil, "disk_usage", lambda path: usage)
    status, body = client.start(("big.bin", b"x" * 1000))
    assert status == 507 and "isn't room" in body["error"]
    assert client.start(("small.bin", b"x" * 400))[0] == 200
    # what's still to come of the first one counts against the next
    assert client.start(("next.bin", b"x" * 200))[0] == 507


def test_empty_file(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    status, body = client.start(("empty.txt", b""))
    assert body["files"][0]["done"] and (folder / "empty.txt").read_bytes() == b""


def test_once_takes_one_batch_then_closes(make):
    receiver, client, folder = make(once=True)
    client.sign_in("123456")
    first, second = os.urandom(3000), os.urandom(2000)
    body = client.start(("1.bin", first), ("2.bin", second))[1]
    client.chunk(body["files"][0]["id"], 0, first)
    # someone else, or a second batch, can't add files...
    status, answer = client.start(("3.bin", b"more"))
    assert status == 410 and "one upload" in answer["error"]
    # ...but the batch can carry on, even from a new page
    other = Client(client.port, receiver.token)
    other.sign_in("123456")
    assert other.start(("2.bin", second))[1]["files"][0]["received"] == 0
    receiver.tick()
    assert not receiver.finished.is_set()
    other.chunk(body["files"][1]["id"], 0, second)
    receiver.tick()
    assert receiver.finished.is_set() and "everything arrived" in receiver.why_finished
    assert client.request("GET", client.base)[0] == 410


def test_expiry_stops_new_files_but_lets_running_ones_finish(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    data = os.urandom(4000)
    upload_id = client.start(("late.bin", data))[1]["files"][0]["id"]
    client.chunk(upload_id, 0, data[:1000])
    receiver.expires_at = time.time() - 1
    assert client.start(("new.bin", b"x"))[0] == 410
    assert client.request("GET", client.base)[0] == 200      # its sender can still reopen the page
    receiver.tick()
    assert not receiver.finished.is_set()
    assert client.chunk(upload_id, 1000, data[1000:])[1]["done"]
    receiver.tick()
    assert receiver.finished.is_set() and "expired" in receiver.why_finished


def test_expired_and_quiet_means_over(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    client.start(("stuck.bin", b"x" * 100))
    receiver.uploads[next(iter(receiver.uploads))].last_seen -= receive.STALL + 1
    receiver.expires_at = time.time() - 1
    receiver.tick()
    assert receiver.finished.is_set()


def test_other_sites_cant_post(make):
    receiver, client, _ = make()
    status, data, _ = client.request("POST", client.base + "code", b'{"code": "123456"}',
                                     {"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert status == 403
    assert receiver.wrong == 0


def test_an_unknown_upload_or_route(make):
    receiver, client, _ = make()
    client.sign_in("123456")
    assert client.chunk("nope", 0, b"x")[0] == 404
    assert client.post("nothing", {})[0] == 404


def test_the_connection_survives_a_refused_small_piece(make):
    """A refused piece is read and dropped, so the next request on the same connection isn't garbage."""
    receiver, client, folder = make()
    client.sign_in("123456")
    upload_id = client.start(("f.bin", b"abcdef"))[1]["files"][0]["id"]
    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    headers = {"X-Kit-Session": client.session}
    connection.request("PUT", f"{client.base}chunk?id={upload_id}&offset=3", b"def", headers)
    response = connection.getresponse()
    assert response.status == 409 and json.loads(response.read())["received"] == 0
    connection.request("PUT", f"{client.base}chunk?id={upload_id}&offset=0", b"abcdef", headers)
    response = connection.getresponse()
    assert response.status == 200 and json.loads(response.read())["done"]
    connection.close()


def test_ping_answers_for_the_tunnel_check(make):
    receiver, client, _ = make()
    assert client.request("GET", "/_kit/ping") [0] == 200


@pytest.mark.skipif(os.name != "nt", reason="Windows' mark of the web")
def test_received_files_are_marked_as_from_the_internet(make):
    receiver, client, folder = make()
    client.sign_in("123456")
    client.send("setup.exe", b"MZ")
    with open(f"{folder / 'setup.exe'}:Zone.Identifier", encoding="ascii") as stream:
        assert "ZoneId=3" in stream.read()


@pytest.mark.parametrize("text, expected", [
    ("2GB", 2 << 30), ("500 MB", 500 << 20), ("1.5g", int(1.5 * (1 << 30))), ("700k", 700 << 10),
    ("1234", 1234), ("10MiB", 10 << 20), ("", None), ("0", None),
])
def test_parse_size(text, expected):
    assert receive.parse_size(text) == expected


def test_parse_size_rejects_nonsense():
    with pytest.raises(ValueError):
        receive.parse_size("lots")
