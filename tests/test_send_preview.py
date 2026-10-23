"""kit send's download page showing a picture of an image, and the picture enlarging."""
import importlib.util
import io
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "send"


def load_send():
    spec = importlib.util.spec_from_file_location("send_main", TOOL / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["send_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def send(monkeypatch, tmp_path):
    """send's app and the outside server; add(path) shares a file and gives back (share, its link's path)."""
    monkeypatch.setenv("KIT_TOOL_DIR", str(TOOL))
    module = load_send()
    app = module.App(module.History(tmp_path / "history.json"), {"expire": "2h", "once": False, "max": None},
                     auto_exit=False, port=8770, explicit_port=False, internet_kind="tunnel", address="", cloudflared="")
    server = module.ShareServer(("127.0.0.1", 0), app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def add(path, limit=None):
        share = app.new_share(path.name, path.stat().st_size, path, 7200, limit)
        return share, f"/d/{share.token}"

    def get(path, page=False):
        headers = {"Accept": "text/html"} if page else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=10) as reply:
                return reply.status, dict(reply.headers), reply.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    yield module, add, get
    server.shutdown()
    server.server_close()


def photo(path, size=(4000, 3000), mode="RGB", **save):
    Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)).save(path, **save)
    return path


def test_an_image_page_shows_a_preview_that_enlarges(send, tmp_path):
    module, add, get = send
    share, link = add(photo(tmp_path / "beach.jpg"))
    status, headers, body = get(link, page=True)
    page = body.decode()
    assert status == 200
    assert f'<a class="preview" href="#view"' in page and f'src="{link}/thumb"' in page
    assert 'id="view"' in page and f'src="{link}/large"' in page
    # the page's one script is allowed by its hash, and images only from kit itself
    csp = headers["Content-Security-Policy"]
    assert "img-src 'self'" in csp and module.VIEWER_HASH in csp
    assert f"<script>{module.VIEWER_SCRIPT}</script>" in page


def test_previews_are_small_and_dont_count_as_downloads(send, tmp_path):
    _, add, get = send
    share, link = add(photo(tmp_path / "beach.jpg"), limit=1)
    for kind, side in (("thumb", 960), ("large", 1600)):
        status, headers, body = get(f"{link}/{kind}")
        assert status == 200 and headers["Content-Type"] == "image/jpeg"
        assert max(Image.open(io.BytesIO(body)).size) == side
    assert share.downloads == 0 and not share.check()
    assert get(link, page=True)[0] == 200   # a --once link still works after its previews


def test_a_preview_is_the_right_way_up_and_carries_no_metadata(send, tmp_path):
    _, add, get = send
    exif = Image.Exif()
    exif[0x0112] = 6            # orientation: the camera was turned; show it rotated
    exif[0x010F] = "PhoneCam"   # a make, standing in for the GPS block photos carry
    _, link = add(photo(tmp_path / "turned.jpg", size=(1200, 800), exif=exif))
    preview = Image.open(io.BytesIO(get(f"{link}/thumb")[2]))
    assert preview.size == (640, 960)
    assert not preview.getexif()


def test_a_transparent_image_previews_as_png(send, tmp_path):
    _, add, get = send
    _, link = add(photo(tmp_path / "logo.png", size=(300, 200), mode="RGBA"))
    status, headers, body = get(f"{link}/thumb")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert Image.open(io.BytesIO(body)).size == (300, 200)   # never made bigger


@pytest.mark.parametrize("name, content", [("notes.zip", b"PK\x03\x04" + b"x" * 500),
                                           ("fake.jpg", b"not really a picture"),
                                           ("drawing.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')])
def test_files_kit_cant_picture_get_the_plain_page(send, tmp_path, name, content):
    _, add, get = send
    path = tmp_path / name
    path.write_bytes(content)
    _, link = add(path)
    page = get(link, page=True)[2].decode()
    assert "Download" in page and "thumb" not in page and "<script>" not in page
    assert get(f"{link}/thumb")[0] == 404


def test_a_closed_link_shows_no_preview(send, tmp_path):
    _, add, get = send
    share, link = add(photo(tmp_path / "beach.jpg"))
    share.dead = "the link expired"
    assert get(f"{link}/thumb")[0] == 410
    assert get(f"{link}/large")[0] == 410
    assert re.search("This link has closed", get(link, page=True)[2].decode())
