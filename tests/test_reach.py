import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kitlib import reach


def test_bind_host_per_mode():
    assert reach.bind_host("lan") == reach.bind_host("public") == "0.0.0.0"
    assert reach.bind_host("internet") == reach.bind_host("tailnet") == "127.0.0.1"


@pytest.mark.parametrize("machine, system, expected", [
    ("AMD64", "windows", "cloudflared-windows-amd64.exe"),
    ("x86_64", "linux", "cloudflared-linux-amd64"),
    ("aarch64", "linux", "cloudflared-linux-arm64"),
    ("armv7l", "linux", "cloudflared-linux-arm"),
    ("arm64", "darwin", "cloudflared-darwin-arm64.tgz"),
])
def test_asset_name(machine, system, expected):
    assert reach.asset_name(machine, system) == expected


def test_asset_name_for_an_unknown_processor():
    with pytest.raises(reach.ReachError, match="no cloudflared build"):
        reach.asset_name("sparc64", "linux")


def test_find_cloudflared_prefers_the_setting_then_path_then_the_download(tmp_path, monkeypatch):
    downloaded = reach.installed_path(tmp_path)
    downloaded.parent.mkdir(parents=True)
    downloaded.write_text("")
    monkeypatch.setattr(reach.shutil, "which", lambda name: "/usr/bin/cloudflared" if name == "cloudflared" else None)
    configured = tmp_path / "mine"
    configured.write_text("")
    assert reach.find_cloudflared(str(configured), tmp_path) == str(configured)
    assert reach.find_cloudflared("", tmp_path) == "/usr/bin/cloudflared"
    monkeypatch.setattr(reach.shutil, "which", lambda name: None)
    assert reach.find_cloudflared("", tmp_path) == str(downloaded)
    assert reach.find_cloudflared(str(tmp_path / "missing"), tmp_path) is None


def test_lan_link(monkeypatch):
    monkeypatch.setattr(reach, "lan_address", lambda: "192.168.1.13")
    assert reach.open(8770, "lan").url == "http://192.168.1.13:8770"


def test_lan_without_a_network(monkeypatch):
    monkeypatch.setattr(reach, "lan_address", lambda: None)
    with pytest.raises(reach.ReachError, match="isn't on a network"):
        reach.open(8770, "lan")


def test_public_link_with_a_given_address():
    assert reach.open(8770, "public", address="example.com").url == "http://example.com:8770"


def test_unknown_mode():
    with pytest.raises(reach.ReachError, match="unknown mode"):
        reach.open(1, "carrier-pigeon")


# --- tailnet -----------------------------------------------------------------------

@pytest.fixture
def tailscale(monkeypatch):
    """Pretend tailscale is installed; set .result to what `tailscale serve` does."""
    class Fake:
        result = None

    def run(command, **kwargs):
        if isinstance(Fake.result, Exception):
            raise Fake.result
        return Fake.result

    monkeypatch.setattr(reach.shutil, "which", lambda name: "/usr/bin/tailscale" if name == "tailscale" else None)
    monkeypatch.setattr(reach.subprocess, "run", run)
    return Fake


def test_tailnet_link(tailscale):
    tailscale.result = subprocess.CompletedProcess([], 0, "Available within your tailnet:\n\n"
                                                         "https://pc.tail1234.ts.net/\n|-- proxy http://127.0.0.1:8000\n", "")
    assert reach.open(8000, "tailnet").url == "https://pc.tail1234.ts.net"


def test_tailnet_failure_says_what_tailscale_said(tailscale):
    tailscale.result = subprocess.CompletedProcess([], 1, "", "Serve is not enabled on your tailnet")
    with pytest.raises(reach.ReachError, match="Serve is not enabled"):
        reach.open(8000, "tailnet")


def test_tailnet_waiting_shows_the_setup_link(tailscale):
    tailscale.result = subprocess.TimeoutExpired([], 15, output=b"To enable, visit: https://login.tailscale.com/f/serve")
    with pytest.raises(reach.ReachError, match="login.tailscale.com"):
        reach.open(8000, "tailnet")


def test_tailnet_without_tailscale(monkeypatch):
    monkeypatch.setattr(reach.shutil, "which", lambda name: None)
    with pytest.raises(reach.ReachError, match="isn't installed"):
        reach.open(8000, "tailnet")


# --- internet, with a stand-in cloudflared ------------------------------------------

def fake_cloudflared(tmp_path: Path, lines: list[str], stay: bool = True) -> str:
    """A program that prints what cloudflared prints, then waits to be stopped (or exits)."""
    script = tmp_path / "fake_cloudflared.py"
    script.write_text("import sys, time\n"
                      f"for line in {lines!r}:\n    print(line, flush=True)\n"
                      + ("time.sleep(60)\n" if stay else ""), encoding="utf-8")
    if os.name == "nt":
        program = tmp_path / "cloudflared.cmd"
        program.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        program = tmp_path / "cloudflared"
        program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        program.chmod(0o755)
    return str(program)


GOOD = ["INF Requesting new quick Tunnel on trycloudflare.com...",
        "INF |  https://quiet-river-sample-tone.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0"]


def test_tunnel_link_comes_from_cloudflareds_output(tmp_path):
    said = []
    link = reach.open(9, "internet", cloudflared=fake_cloudflared(tmp_path, GOOD), say=said.append)
    try:
        assert link.url == "https://quiet-river-sample-tone.trycloudflare.com"
        assert said == ["opening a tunnel through Cloudflare..."]
    finally:
        link.close()


def test_closing_the_link_stops_cloudflared(tmp_path):
    tunnel = reach.Tunnel(fake_cloudflared(tmp_path, GOOD), 9)
    tunnel.start()
    assert tunnel.alive()
    tunnel.stop()
    assert not tunnel.alive()


def test_tunnel_that_never_connects_reports_its_last_words(tmp_path):
    program = fake_cloudflared(tmp_path, ["ERR failed to request quick Tunnel: 429 Too Many Requests"], stay=False)
    started = time.monotonic()
    with pytest.raises(reach.ReachError, match="429 Too Many Requests"):
        reach.Tunnel(program, 9).start(timeout=10)
    assert time.monotonic() - started < 10  # it noticed cloudflared exiting rather than waiting it out


def test_a_missing_configured_cloudflared_is_named(tmp_path):
    with pytest.raises(reach.ReachError, match="points at"):
        reach.open(9, "internet", cloudflared=str(tmp_path / "nope"))
