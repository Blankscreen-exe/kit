"""Put text on the system clipboard: the Win32 API on Windows, pbcopy / wl-copy / xclip / xsel elsewhere."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time


def copy_windows(text: str) -> bool:
    """Put Unicode text on the Windows clipboard through the Win32 API (clip.exe mangles non-ASCII)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL

    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)

    for _ in range(20):  # another program may be holding the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
            kernel32.GlobalFree(handle)
            return False
        return True  # the clipboard owns the memory now
    finally:
        user32.CloseClipboard()


def copy_to_clipboard(text: str) -> bool:
    """True when the text was copied; False when no clipboard is available."""
    try:
        if os.name == "nt":
            return copy_windows(text)
        candidates = []
        if sys.platform == "darwin" and shutil.which("pbcopy"):
            candidates.append(["pbcopy"])
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            candidates.append(["wl-copy"])
        if shutil.which("xclip"):
            candidates.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            candidates.append(["xsel", "--clipboard", "--input"])
        for command in candidates:
            try:
                subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    except OSError:
        pass
    return False
