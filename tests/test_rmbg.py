"""kit rmbg: strokes changing a mask, saving without overwriting, and the edit page's access checks.

The model isn't needed: tests hand the session a mask. The cv2/numpy tests are skipped until `kit rmbg --setup` has run.
"""
import http.client
import importlib.util
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "rmbg"


def load_rmbg():
    spec = importlib.util.spec_from_file_location("rmbg_main", TOOL / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rmbg_main"] = module
    spec.loader.exec_module(module)
    return module


rmbg = load_rmbg()
rmbg.activate_packages()


def needs_packages():
    pytest.importorskip("cv2")
    return pytest.importorskip("numpy")


def scene(np):
    """A red square on a blue background, with a green block in the far corner that's the same 'kind' as the square."""
    bgr = np.zeros((200, 200, 3), dtype=np.uint8)
    bgr[:] = (200, 60, 20)
    bgr[60:140, 60:140] = (20, 20, 220)
    bgr[5:35, 165:195] = (20, 200, 20)
    alpha = np.zeros((200, 200), dtype=np.uint8)
    alpha[60:140, 60:140] = 255
    alpha[5:35, 165:195] = 255  # the model wrongly kept the corner block
    return bgr, alpha


def test_a_remove_stroke_clears_the_whole_area_it_touches(tmp_path):
    np = needs_packages()
    bgr, alpha = scene(np)
    remove, keep = np.zeros((200, 200), dtype=bool), np.zeros((200, 200), dtype=bool)
    remove[15:25, 175:185] = True
    clear, restore = rmbg.stroke_changes(bgr, alpha, remove, keep)
    assert clear[5:35, 165:195].mean() > 0.95
    assert not clear[60:140, 60:140].any()
    assert not restore.any()


def test_a_small_stroke_spreads_over_an_object_whose_colour_appears_elsewhere_in_the_subject():
    """The helmet problem: GrabCut alone keeps a colour the rest of the subject also has."""
    np = needs_packages()
    bgr = np.full((200, 200, 3), 230, dtype=np.uint8)
    bgr[20:90, 20:180] = (20, 20, 220)   # the subject: red...
    bgr[30:60, 30:60] = (10, 10, 10)     # ...with something black in it
    bgr[110:190, 60:140] = (10, 10, 10)  # the black object the model wrongly kept
    alpha = np.zeros((200, 200), dtype=np.uint8)
    alpha[20:90, 20:180] = 255
    alpha[110:190, 60:140] = 255
    remove, keep = np.zeros((200, 200), dtype=bool), np.zeros((200, 200), dtype=bool)
    remove[148:152, 98:102] = True
    clear, restore = rmbg.stroke_changes(bgr, alpha, remove, keep)
    assert clear[110:190, 60:140].mean() > 0.95
    assert not clear[20:90, 20:180].any()
    assert not restore.any()


def test_a_keep_stroke_stops_a_remove_stroke_spreading():
    np = needs_packages()
    bgr = np.full((100, 100, 3), 200, dtype=np.uint8)
    bgr[:, :] = (10, 10, 10)
    alpha = np.full((100, 100), 255, dtype=np.uint8)
    remove, keep = np.zeros((100, 100), dtype=bool), np.zeros((100, 100), dtype=bool)
    remove[45:55, 5:15] = True
    keep[:, 50:54] = True  # a wall of green right across
    clear, _restore = rmbg.stroke_changes(bgr, alpha, remove, keep)
    assert clear[45:55, 5:15].all()
    assert not clear[:, 50:].any()


def test_a_keep_stroke_brings_back_a_missing_area(tmp_path):
    np = needs_packages()
    bgr, alpha = scene(np)
    alpha[60:140, 60:140] = 0  # the model missed the square
    remove, keep = np.zeros((200, 200), dtype=bool), np.zeros((200, 200), dtype=bool)
    keep[95:105, 95:105] = True
    _clear, restore = rmbg.stroke_changes(bgr, alpha, remove, keep)
    assert restore[60:140, 60:140].mean() > 0.95


def test_changes_not_connected_to_a_stroke_are_dropped():
    np = needs_packages()
    region = np.zeros((50, 50), dtype=bool)
    region[0:10, 0:10] = True
    region[40:50, 40:50] = True
    seed = np.zeros((50, 50), dtype=bool)
    seed[5, 5] = True
    result = rmbg.touching(region, seed)
    assert result[0:10, 0:10].all()
    assert not result[40:50, 40:50].any()


def test_changes_are_applied_at_full_size_and_leave_other_alpha_alone():
    np = needs_packages()
    alpha = np.full((400, 400), 200, dtype=np.uint8)
    clear = np.zeros((100, 100), dtype=bool)
    restore = np.zeros((100, 100), dtype=bool)
    clear[0:20, 0:20] = True
    restore[80:100, 80:100] = True
    result = rmbg.apply_changes(alpha, clear, restore)
    assert result.shape == (400, 400)
    assert result[20, 20] == 0
    assert result[380, 380] == 255
    assert result[200, 200] == 200


def test_saving_never_replaces_a_file(tmp_path):
    image = Image.new("RGBA", (4, 4), (1, 2, 3, 128))
    target = tmp_path / "a-nobg.png"
    assert rmbg.save_image(image, target, None) == target
    second = rmbg.save_image(image, target, None)
    assert second.name == "a-nobg (2).png"
    with Image.open(second) as reread:
        assert reread.getpixel((0, 0)) == (1, 2, 3, 128)
    assert not list(tmp_path.glob("*.part"))


def test_the_original_transparency_stays_and_photos_are_turned_upright(tmp_path):
    np = needs_packages()
    path = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees to display
    Image.new("RGB", (30, 10), "red").save(path, exif=exif)
    image, source_alpha, _profile = rmbg.load_image(path)
    assert image.size == (10, 30)

    half = Image.new("L", (10, 30), 128)
    result = rmbg.cut_out(image, half, np.full((30, 10), 128, dtype=np.uint8))
    assert result.getpixel((0, 0))[3] == 64


def test_only_formats_with_transparency_are_accepted(tmp_path):
    assert rmbg.output_path(Path("a.jpg"), "b.webp").name == "b.webp"
    assert rmbg.output_path(tmp_path / "a.jpg", None).name == "a-nobg.png"
    with pytest.raises(SystemExit):
        rmbg.output_path(Path("a.jpg"), "b.jpg")


@pytest.fixture
def edit(tmp_path):
    np = needs_packages()
    source = tmp_path / "a.png"
    image = Image.new("RGB", (64, 48), "blue")
    image.paste("yellow", (0, 0, 24, 24))  # strokes spread across a colour, so the corner gets its own
    image.save(source)
    alpha = np.full((48, 64), 255, dtype=np.uint8)
    session = rmbg.Session(source, image, Image.new("L", image.size, 255), None, alpha, tmp_path / "a-nobg.png")
    server = rmbg.EditServer(session, "secret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, data=None, headers=None):
        request = urllib.request.Request(base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    yield session, base, call
    server.shutdown()
    server.server_close()


def strokes_png(size, box, color):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(color, box)
    buffer = io.BytesIO()
    layer.save(buffer, "PNG")
    return buffer.getvalue()


TOKEN = {"X-Kit-Token": "secret"}


def test_the_api_needs_the_token(edit):
    _session, _base, call = edit
    assert call("/api/state")[0] == 401
    assert call("/api/original")[0] == 401
    assert call("/api/save", b"{}", {"Content-Type": "application/json", "Origin": "http://127.0.0.1"})[0] == 403
    assert call("/api/save", b"{}", {"Content-Type": "application/json"})[0] == 403
    assert call("/api/state", headers=TOKEN)[0] == 200


def test_other_sites_are_turned_away(edit):
    session, _base, call = edit
    headers = {**TOKEN, "Content-Type": "application/json", "Origin": "https://evil.example"}
    assert call("/api/save", b"{}", headers)[0] == 403
    assert call("/api/state", headers={**TOKEN, "Host": "evil.example"})[0] == 403
    assert session.last_output is None


def test_painting_then_saving_writes_a_full_size_cut_out(edit):
    session, _base, call = edit
    assert json.loads(call("/api/state", headers=TOKEN)[1])["workWidth"] == 64
    headers = {**TOKEN, "Content-Type": "application/octet-stream"}
    status, preview = call("/api/strokes", strokes_png((64, 48), (0, 0, 20, 20), (255, 0, 0, 255)), headers)
    assert status == 200
    with Image.open(io.BytesIO(preview)) as image:
        assert image.getpixel((5, 5))[3] == 0
        assert image.getpixel((55, 40))[3] == 255

    status, body = call("/api/save", b"{}", {**TOKEN, "Content-Type": "application/json"})
    assert status == 200
    with Image.open(json.loads(body)["lastOutput"]["path"]) as saved:
        assert saved.size == (64, 48)
        assert saved.getpixel((5, 5))[3] == 0
        assert saved.getpixel((55, 40))[3] == 255


def test_clearing_the_strokes_goes_back_to_the_first_result(edit):
    _session, _base, call = edit
    headers = {**TOKEN, "Content-Type": "application/octet-stream"}
    call("/api/strokes", strokes_png((64, 48), (0, 0, 20, 20), (255, 0, 0, 255)), headers)
    status, preview = call("/api/strokes", strokes_png((64, 48), (0, 0, 0, 0), (0, 0, 0, 0)), headers)
    assert status == 200
    with Image.open(io.BytesIO(preview)) as image:
        assert image.getpixel((5, 5))[3] == 255


def test_a_kept_alive_connection_survives_every_kind_of_post(edit):
    """Browsers reuse connections: an unread request body would be read as the start of the next request."""
    _session, base, _call = edit
    connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=20)
    json_token = {**TOKEN, "Content-Type": "application/json"}
    for path, headers, expected in (
        ("/api/save", json_token, 200),  # answered without looking at the body
        ("/api/nothing", json_token, 404),
        ("/api/save", {"Content-Type": "application/json", "Origin": "https://evil.example"}, 403),  # turned away first
    ):
        connection.request("POST", path, b"{}", headers)
        reply = connection.getresponse()
        reply.read()
        assert reply.status == expected
        connection.request("GET", "/api/state", headers=TOKEN)
        reply = connection.getresponse()
        reply.read()
        assert reply.status == 200
    connection.close()


def test_done_stops_the_server(tmp_path):
    np = needs_packages()
    image = Image.new("RGB", (8, 8), "blue")
    alpha = np.full((8, 8), 255, dtype=np.uint8)
    session = rmbg.Session(tmp_path / "a.png", image, Image.new("L", image.size, 255), None, alpha, tmp_path / "out.png")
    server = rmbg.EditServer(session, "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/done", data=b"{}",
                                     headers={**TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as reply:
        assert reply.status == 200
    thread.join(10)
    assert not thread.is_alive()
    server.server_close()


def test_strokes_of_the_wrong_size_or_kind_are_refused(edit):
    _session, _base, call = edit
    headers = {**TOKEN, "Content-Type": "application/octet-stream"}
    assert call("/api/strokes", strokes_png((10, 10), (0, 0, 5, 5), (255, 0, 0, 255)), headers)[0] == 400
    assert call("/api/strokes", b"not an image", headers)[0] == 400
