"""System information next to ASCII art from a text file, in the style of neofetch."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kitlib import KIT_HOME, color_enabled, die
from kitlib.figlet import FigletError, render_figlet

try:
    import psutil
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

PLATFORM = "windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin" else "linux"
ART_DIR = Path(__file__).resolve().parent / "art"
GIB = 1024 ** 3

COLORS = {
    "black": "30", "red": "91", "green": "92", "yellow": "93", "blue": "94",
    "magenta": "95", "cyan": "96", "white": "97", "gray": "90",
}
TOKEN_RE = re.compile(r"\{(" + "|".join([*COLORS, "accent", "bold", "reset"]) + r")\}")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

SHELLS = {
    "pwsh": "PowerShell", "powershell": "Windows PowerShell", "cmd": "cmd", "bash": "bash", "zsh": "zsh",
    "fish": "fish", "nu": "nushell", "sh": "sh", "dash": "dash", "ksh": "ksh", "tcsh": "tcsh",
    "xonsh": "xonsh", "elvish": "elvish",
}
TERMINALS = {
    "windowsterminal": "Windows Terminal", "openconsole": "Windows Terminal", "conhost": "Console Host",
    "code": "VS Code", "cursor": "Cursor", "wezterm-gui": "WezTerm", "alacritty": "Alacritty", "kitty": "kitty",
    "ghostty": "Ghostty", "mintty": "mintty", "tabby": "Tabby", "hyper": "Hyper", "warp": "Warp",
    "gnome-terminal-server": "GNOME Terminal", "konsole": "Konsole", "xfce4-terminal": "Xfce Terminal",
    "tilix": "Tilix", "terminator": "Terminator", "xterm": "xterm", "iterm2": "iTerm2", "sshd": "SSH session",
}
TERM_PROGRAMS = {"vscode": "VS Code", "iTerm.app": "iTerm2", "Apple_Terminal": "Terminal", "WezTerm": "WezTerm", "ghostty": "Ghostty"}
JUNK_HOST_VALUES = {"", "to be filled by o.e.m.", "system product name", "system manufacturer", "default string", "o.e.m.", "none"}


# --- colour ----------------------------------------------------------------------

class Painter:
    def __init__(self, enabled: bool, accent: str) -> None:
        self.enabled = enabled
        self.accent = accent

    def code(self, name: str) -> str:
        if not self.enabled:
            return ""
        if name == "reset":
            return "\x1b[0m"
        if name == "bold":
            return "\x1b[1m"
        return f"\x1b[{COLORS[self.accent if name == 'accent' else name]}m"

    def paint(self, text: str, *names: str) -> str:
        if not self.enabled or not names:
            return text
        return "".join(self.code(n) for n in names) + text + "\x1b[0m"


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def can_encode(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# --- art ---------------------------------------------------------------------------

def art_names() -> list[str]:
    return sorted(p.stem for p in ART_DIR.glob("*.txt"))


def resolve_art(choice: str | None) -> Path | None:
    choice = choice or os.environ.get("KIT_FETCH_ART")
    if choice:
        path = Path(choice).expanduser()
        if path.is_file():
            return path
        named = ART_DIR / f"{choice}.txt"
        if named.is_file():
            return named
        die(f"art not found: '{choice}' - give a text file path or one of: {', '.join(art_names())}")

    candidates = [PLATFORM, "default"]
    if PLATFORM == "linux":
        distro = os_release().get("ID")
        if distro:
            candidates.insert(0, distro)
    for name in candidates:
        path = ART_DIR / f"{name}.txt"
        if path.is_file():
            return path
    return None


def load_art(path: Path, painter: Painter) -> list[str]:
    """Art lines with colour tokens turned into ANSI codes (or removed when colour is off)."""
    text = path.read_text(encoding="utf-8-sig").expandtabs()
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not TOKEN_RE.search(text):
        return [painter.paint(line, "accent", "bold") for line in lines]

    rendered = []
    active = ""  # codes in effect, re-applied at the start of each line
    for line in lines:
        out, pos = active, 0
        for match in TOKEN_RE.finditer(line):
            out += line[pos:match.start()]
            code = painter.code(match[1])
            out += code
            if match[1] == "reset":
                active = ""
            elif match[1] == "bold":
                active += code
            else:
                active = code
            pos = match.end()
        rendered.append(out + line[pos:] + painter.code("reset"))
    return rendered


# --- system information ------------------------------------------------------------

@dataclass
class Field:
    label: str
    value: str
    percent: float | None = None


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace").strip()


def run(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip()


def os_release() -> dict[str, str]:
    values = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def registry_value(path: str, name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip()
    except OSError:
        return None


def os_name() -> str:
    arch = {"amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine())
    if PLATFORM == "windows":
        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        product = registry_value(key, "ProductName") or "Windows"
        build = registry_value(key, "CurrentBuild") or ""
        if build.isdigit() and int(build) >= 22000:
            product = product.replace("Windows 10", "Windows 11")  # the registry still says 10 on 11
        version = registry_value(key, "DisplayVersion") or ""
        name = f"{product} {version}".strip()
    elif PLATFORM == "macos":
        name = f"macOS {platform.mac_ver()[0]}"
    else:
        name = os_release().get("PRETTY_NAME") or "Linux"
    return f"{name} {arch}".strip()


def host_model() -> str | None:
    if PLATFORM == "windows":
        key = r"HARDWARE\DESCRIPTION\System\BIOS"
        parts = [registry_value(key, "SystemManufacturer"), registry_value(key, "SystemProductName")]
    elif PLATFORM == "macos":
        parts = [run("sysctl", "-n", "hw.model")]
    else:
        parts = []
        for name in ("sys_vendor", "product_name"):
            try:
                parts.append(read_text(f"/sys/devices/virtual/dmi/id/{name}"))
            except OSError:
                pass
    parts = [p for p in parts if p and p.lower() not in JUNK_HOST_VALUES]
    return " ".join(parts) or None


def kernel() -> str:
    if PLATFORM == "windows":
        return f"NT {platform.version()}"
    return f"{platform.system()} {platform.release()}"


def uptime() -> str:
    minutes = int((time.time() - psutil.boot_time()) // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}d"] if days else []
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def shell_and_terminal() -> tuple[str | None, str | None]:
    """Walks up the process tree: the first known shell, then the first known terminal above it."""
    shell = terminal = None
    try:
        process = psutil.Process().parent()
        while process is not None:
            name = process.name().lower().removesuffix(".exe")
            if shell is None and name in SHELLS:
                shell = SHELLS[name]
            elif shell is not None and name in TERMINALS:
                terminal = TERMINALS[name]
                break
            process = process.parent()
    except psutil.Error:
        pass

    if shell is None and os.environ.get("SHELL"):
        shell = Path(os.environ["SHELL"]).name
    program = os.environ.get("TERM_PROGRAM")
    if program:
        terminal = TERM_PROGRAMS.get(program, program)
    elif terminal is None and os.environ.get("WT_SESSION"):
        terminal = "Windows Terminal"
    return shell, terminal


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_uint32),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_uint16),
        ("dmDriverVersion", ctypes.c_uint16),
        ("dmSize", ctypes.c_uint16),
        ("dmDriverExtra", ctypes.c_uint16),
        ("dmFields", ctypes.c_uint32),
        ("dmPositionX", ctypes.c_int32),
        ("dmPositionY", ctypes.c_int32),
        ("dmDisplayOrientation", ctypes.c_uint32),
        ("dmDisplayFixedOutput", ctypes.c_uint32),
        ("dmColor", ctypes.c_int16),
        ("dmDuplex", ctypes.c_int16),
        ("dmYResolution", ctypes.c_int16),
        ("dmTTOption", ctypes.c_int16),
        ("dmCollate", ctypes.c_int16),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_uint16),
        ("dmBitsPerPel", ctypes.c_uint32),
        ("dmPelsWidth", ctypes.c_uint32),
        ("dmPelsHeight", ctypes.c_uint32),
        ("dmDisplayFlags", ctypes.c_uint32),
        ("dmDisplayFrequency", ctypes.c_uint32),
        ("dmICMMethod", ctypes.c_uint32),
        ("dmICMIntent", ctypes.c_uint32),
        ("dmMediaType", ctypes.c_uint32),
        ("dmDitherType", ctypes.c_uint32),
        ("dmReserved1", ctypes.c_uint32),
        ("dmReserved2", ctypes.c_uint32),
        ("dmPanningWidth", ctypes.c_uint32),
        ("dmPanningHeight", ctypes.c_uint32),
    ]


def windows_displays() -> tuple[list[str], list[str]]:
    """(resolutions, GPU names) for the displays attached to the desktop, straight from the Win32 API."""
    user32 = ctypes.windll.user32
    resolutions: list[str] = []
    gpus: list[str] = []
    device = DISPLAY_DEVICEW()
    device.cb = ctypes.sizeof(device)
    index = 0
    while user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
        index += 1
        if not device.StateFlags & 0x1:  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
            continue
        mode = DEVMODEW()
        mode.dmSize = ctypes.sizeof(mode)
        if user32.EnumDisplaySettingsW(device.DeviceName, -1, ctypes.byref(mode)):  # ENUM_CURRENT_SETTINGS
            resolutions.append(f"{mode.dmPelsWidth}x{mode.dmPelsHeight} @ {mode.dmDisplayFrequency}Hz")
        if device.DeviceString and device.DeviceString not in gpus:
            gpus.append(device.DeviceString)
    return resolutions, gpus


def linux_resolutions() -> list[str]:
    modes = []
    for status in sorted(Path("/sys/class/drm").glob("card*-*/status")):
        if read_text(str(status)) == "connected":
            first = read_text(str(status.parent / "modes")).split("\n", 1)[0].strip()
            if first:
                modes.append(first)
    return modes


def linux_gpus() -> list[str]:
    if not shutil.which("lspci"):
        return []
    names = []
    for line in run("lspci", "-mm").splitlines():
        parts = shlex.split(line)
        if len(parts) >= 4 and any(kind in parts[1] for kind in ("VGA", "3D", "Display")):
            vendor = re.sub(r",? (Corporation|Inc\.)|\[AMD/ATI\]", "", parts[2]).strip()
            names.append(f"{vendor} {parts[3]}")
    return names


def cpu() -> str | None:
    if PLATFORM == "windows":
        name = registry_value(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
    elif PLATFORM == "macos":
        name = run("sysctl", "-n", "machdep.cpu.brand_string")
    else:
        name = next((line.split(":", 1)[1] for line in read_text("/proc/cpuinfo").splitlines()
                     if line.startswith("model name")), None)
    if not name:
        return None
    name = re.sub(r"\((R|TM)\)|\bCPU\b|\bProcessor\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", re.sub(r"\s*@\s*", " @ ", name)).strip()
    physical, logical = psutil.cpu_count(logical=False), psutil.cpu_count()
    return f"{name} ({physical}C/{logical}T)" if physical and logical else name


def memory() -> Field:
    vm = psutil.virtual_memory()
    used = vm.total - vm.available
    return Field("Memory", f"{used / GIB:.1f} / {vm.total / GIB:.1f} GiB", vm.percent)


def disks() -> list[Field]:
    fields, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        mount = part.mountpoint
        if not part.fstype or "cdrom" in part.opts or part.device in seen:
            continue
        if PLATFORM != "windows" and (part.fstype in ("squashfs", "overlay") or mount.startswith(("/snap", "/boot", "/run", "/var/lib"))):
            continue
        try:
            usage = psutil.disk_usage(mount)
        except OSError:
            continue
        seen.add(part.device)
        label = mount.rstrip("\\") if PLATFORM == "windows" else mount
        fields.append(Field(f"Disk ({label})", f"{usage.used / GIB:.0f} / {usage.total / GIB:.0f} GiB", usage.percent))
    return fields


def local_ip() -> str:
    # Connecting a UDP socket sends nothing; it just makes the OS choose the outgoing interface.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("10.254.254.254", 1))
        return sock.getsockname()[0]


def battery() -> str | None:
    info = psutil.sensors_battery()
    if info is None:
        return None
    return f"{info.percent:.0f}%" + (" (charging)" if info.power_plugged else "")


def collect_fields() -> list[Field]:
    fields: list[Field] = []

    def add(label: str, getter: Callable[[], object]) -> None:
        try:
            result = getter()
        except Exception:
            return  # a value we can't read is simply left out
        if isinstance(result, Field):
            fields.append(result)
        elif isinstance(result, list):
            fields.extend(r if isinstance(r, Field) else Field(label, str(r)) for r in result)
        elif result:
            fields.append(Field(label, str(result)))

    try:
        shell, terminal = shell_and_terminal()
    except Exception:
        shell = terminal = None
    try:
        if PLATFORM == "windows":
            resolutions, gpus = windows_displays()
        elif PLATFORM == "linux":
            resolutions, gpus = linux_resolutions(), linux_gpus()
        else:
            resolutions, gpus = [], []
    except Exception:
        resolutions, gpus = [], []

    add("OS", os_name)
    add("Host", host_model)
    add("Kernel", kernel)
    add("Uptime", uptime)
    add("Shell", lambda: shell)
    add("Terminal", lambda: terminal)
    add("Resolution", lambda: ", ".join(resolutions))
    add("CPU", cpu)
    add("GPU", lambda: gpus)
    add("Memory", memory)
    add("Disk", disks)
    add("Local IP", local_ip)
    add("Battery", battery)
    return fields


# --- layout ------------------------------------------------------------------------

def usage_bar(percent: float, painter: Painter, width: int = 12) -> str:
    full, empty = ("█", "░") if can_encode("█░") else ("#", "-")
    filled = max(0, min(width, round(percent / 100 * width)))
    colour = "green" if percent < 60 else "yellow" if percent < 85 else "red"
    return painter.paint(full * filled, colour) + painter.paint(empty * (width - filled), "gray") + f" {percent:.0f}%"


def info_lines(fields: list[Field], painter: Painter, show_palette: bool) -> list[str]:
    user, host = getpass.getuser(), socket.gethostname()
    lines = [
        painter.paint(user, "accent", "bold") + "@" + painter.paint(host, "accent", "bold"),
        "-" * (len(user) + 1 + len(host)),
    ]
    label_width = max((len(f.label) + 1 for f in fields), default=0)
    value_width = max((len(f.value) for f in fields if f.percent is not None), default=0)
    for field in fields:
        label = painter.paint(f"{field.label}:".ljust(label_width), "accent", "bold")
        value = field.value
        if field.percent is not None:
            value = f"{value.ljust(value_width)}  {usage_bar(field.percent, painter)}"
        lines.append(f"{label}  {value}")
    if show_palette and painter.enabled:
        lines.append("")
        lines.append("".join(f"\x1b[4{i}m   " for i in range(8)) + "\x1b[0m")
        lines.append("".join(f"\x1b[10{i}m   " for i in range(8)) + "\x1b[0m")
    return lines


def layout(art: list[str], info: list[str], stack: bool, gap: int = 3) -> list[str]:
    if not art:
        return info
    art_width = max(visible_len(line) for line in art)
    info_width = max(visible_len(line) for line in info)
    columns = shutil.get_terminal_size((120, 30)).columns
    if stack or art_width + gap + info_width > columns:
        return art + [""] + info

    rows = []
    for i in range(max(len(art), len(info))):
        left = art[i] if i < len(art) else ""
        right = info[i] if i < len(info) else ""
        padding = " " * (art_width - visible_len(left) + gap) if right else ""
        rows.append(left + padding + right)
    return rows


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(prog="kit fetch", description="System information next to ASCII art, like neofetch.")
    parser.add_argument("--art", metavar="NAME|FILE", help="art name from the art/ folder, or a path to a text file")
    parser.add_argument("--figlet", metavar="TEXT", help="use a figlet banner of TEXT as the art instead")
    parser.add_argument("-f", "--font", default="standard", help="figlet font for --figlet (default: standard)")
    parser.add_argument("--color", default="cyan", choices=sorted(COLORS), help="colour of labels and plain art (default: cyan)")
    parser.add_argument("--stack", action="store_true", help="always put the art above the info")
    parser.add_argument("--no-palette", action="store_true", help="hide the colour swatches")
    parser.add_argument("--list-art", action="store_true", help="list the bundled art and exit")
    args = parser.parse_args()

    if args.list_art:
        default = resolve_art(None)
        for name in art_names():
            marker = "  (default here)" if default is not None and default.stem == name else ""
            print(f"{name}{marker}")
        print(f"\nfolder: {ART_DIR}")
        return 0

    painter = Painter(color_enabled(), args.color)
    if args.figlet:
        try:
            art = [painter.paint(line, "accent", "bold") for line in render_figlet(args.figlet, args.font)]
        except FigletError as exc:
            die(str(exc))
    else:
        path = resolve_art(args.art)
        art = load_art(path, painter) if path else []

    info = info_lines(collect_fields(), painter, show_palette=not args.no_palette)
    print()
    print("\n".join(layout(art, info, args.stack)))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
