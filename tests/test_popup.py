import os
import shutil
import subprocess
import sys

import pytest

from kitlib import popup as popup_mod
from kitlib.popup import popup, popup_command

TRICKY = "it's \"quoted\"; $(rm -rf ~) `x` & <b>"


def test_windows_toast_carries_text_in_environment_only():
    command, env = popup_command("Time's up", TRICKY, platform="windows")
    assert command[-1] == popup_mod.TOAST_SCRIPT
    assert TRICKY not in " ".join(command)          # never pasted into the script
    assert env["KIT_POPUP_TITLE"] == "Time's up" and env["KIT_POPUP_BODY"] == TRICKY


def test_windows_link_uses_the_clickable_balloon():
    command, env = popup_command("kit send", "photos.zip", link="http://host:8770/d/abc", platform="windows")
    assert command[-1] == popup_mod.BALLOON_SCRIPT
    assert env["KIT_POPUP_LINK"] == "http://host:8770/d/abc"


def test_macos_passes_text_as_arguments():
    command, env = popup_command("Title", TRICKY, platform="macos")
    assert command[0] == "osascript" and command[-2:] == ["Title", TRICKY] and env == {}


def test_linux_uses_notify_send(monkeypatch):
    monkeypatch.setattr(popup_mod.shutil, "which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    command, _ = popup_command("Title", "-looks like a flag", urgent=True, app="kit countdown", platform="linux")
    assert command == ["notify-send", "-a", "kit countdown", "-u", "critical", "--", "Title", "-looks like a flag"]


def test_linux_without_notify_send_has_nothing(monkeypatch):
    monkeypatch.setattr(popup_mod.shutil, "which", lambda name: None)
    assert popup_command("Title", "body", platform="linux") is None


def test_no_display_shows_nothing(monkeypatch):
    started = []
    monkeypatch.setattr(popup_mod, "no_display", lambda: True)
    monkeypatch.setattr(popup_mod.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert popup("Title", "body") is False
    assert started == []


def test_popup_starts_a_background_process(monkeypatch):
    started = []
    monkeypatch.setattr(popup_mod, "no_display", lambda: False)
    monkeypatch.setattr(popup_mod, "popup_command", lambda *a, **k: (["show-it"], {"KIT_POPUP_TITLE": "T"}))
    monkeypatch.setattr(popup_mod.subprocess, "Popen", lambda command, **options: started.append((command, options)))
    assert popup("T", "body") is True
    command, options = started[0]
    assert command == ["show-it"] and options["env"]["KIT_POPUP_TITLE"] == "T"


def test_popup_never_raises_when_the_tool_is_missing(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("no such program")
    monkeypatch.setattr(popup_mod, "no_display", lambda: False)
    monkeypatch.setattr(popup_mod, "popup_command", lambda *a, **k: (["show-it"], {}))
    monkeypatch.setattr(popup_mod.subprocess, "Popen", missing)
    assert popup("T", "body") is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="PowerShell scripts run on Windows")
@pytest.mark.parametrize("link", [None, "https://example.com/x?a=1&b=2"])
def test_windows_scripts_really_build_the_popup(link):
    """PowerShell parses the script and builds the toast or balloon, stopping just before showing it."""
    command, env = popup_command("kit test", TRICKY, link=link, platform="windows")
    result = subprocess.run(command, env={**os.environ, **env, "KIT_POPUP_DRYRUN": "1"},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert ("balloon ready" if link else "toast ready") in result.stdout
