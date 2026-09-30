"""Desktop popups on this machine: a Windows toast, Notification Center on macOS, notify-send on Linux.

    from kitlib.popup import popup
    popup("Time's up", "tea is ready", sound=True)
    popup("kit send", "photos.zip from laptop", link="http://192.168.1.20:8770/d/...")

A popup is its own short-lived process, so it doesn't hold the caller up and still appears if the
caller exits straight away. Title and message go to it as environment variables or arguments, never
pasted into a script, so no text can break it. Where there's no desktop (an SSH session, a server)
nothing is shown and popup() returns False - print the message yourself if it matters.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from kitlib.browser import no_display

# A toast: WinRT through PowerShell, no modules. The text is added as XML text nodes.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:KIT_POPUP_TITLE)) > $null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:KIT_POPUP_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
if ($env:KIT_POPUP_DRYRUN) { 'toast ready'; exit 0 }
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

# A tray balloon, for a popup with a link: clicking it opens the link. The process has to keep a
# message loop running for the click to land, and ends when the balloon is clicked or closes (the
# timeout is only a safety net) - so it's always started in the background.
BALLOON_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = $env:KIT_POPUP_TITLE
$n.BalloonTipText = $env:KIT_POPUP_BODY
if ($env:KIT_POPUP_DRYRUN) { 'balloon ready'; exit 0 }
$n.Visible = $true
$n.add_BalloonTipClicked({ Start-Process $env:KIT_POPUP_LINK; [System.Windows.Forms.Application]::ExitThread() })
$n.add_BalloonTipClosed({ [System.Windows.Forms.Application]::ExitThread() })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 20000
$timer.add_Tick({ [System.Windows.Forms.Application]::ExitThread() })
$timer.Start()
$n.ShowBalloonTip(8000)
[System.Windows.Forms.Application]::Run()
$n.Dispose()
"""

MACOS_SCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run"]


def _platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def popup_command(title: str, message: str, link: str | None = None, urgent: bool = False, app: str = "kit",
                  platform: str | None = None) -> tuple[list[str], dict[str, str]] | None:
    """The command that shows this popup, and the environment variables it reads - or None when the
    platform has nothing to show one with. Separate from popup() so it can be checked on any system."""
    platform = platform or _platform()
    if platform == "windows":
        script = BALLOON_SCRIPT if link else TOAST_SCRIPT
        env = {"KIT_POPUP_TITLE": title, "KIT_POPUP_BODY": message, "KIT_POPUP_LINK": link or ""}
        return [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], env
    if platform == "macos":
        return ["osascript", *MACOS_SCRIPT, title, message], {}
    if not shutil.which("notify-send"):
        return None
    command = ["notify-send", "-a", app]
    if urgent:
        command += ["-u", "critical"]
    return [*command, "--", title, message], {}


def beep() -> None:
    """The system's alert sound, where there is one to play (Windows). Never raises."""
    if _platform() == "windows":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def popup(title: str, message: str, *, link: str | None = None, sound: bool = False, urgent: bool = False,
          app: str = "kit") -> bool:
    """Show a desktop popup; True if one was started. Never raises, never waits.

    link: clicking the popup opens it (Windows; elsewhere it's just part of the text you add yourself).
    sound: play the alert sound too. urgent: ask for a popup that stays until dismissed (Linux).
    app: the name the popup is shown under, where the system shows one (Linux).
    """
    if no_display():
        return False
    found = popup_command(title, message, link=link, urgent=urgent, app=app)
    if found is None:
        return False
    command, extra_env = found
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                     "env": {**os.environ, **extra_env}}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen(command, **options)
    except OSError:
        return False
    if sound:
        beep()
    return True
