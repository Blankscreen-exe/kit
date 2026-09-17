"""View and edit the hosts file safely: list, add, block, disable and remove entries, with backups."""

from __future__ import annotations

import argparse
import codecs
import difflib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from kitlib import die, style, warn
from kitlib.settings import tool_settings

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
BLOCK_COMMENT = "kit: blocked"
BLOCK_IP = "0.0.0.0"
HOSTNAME_RE = re.compile(r"(?=.{1,253}$)[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*")
LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def system_hosts_path() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


# --- parsing ------------------------------------------------------------------------------

def valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def valid_name(text: str) -> bool:
    return bool(HOSTNAME_RE.fullmatch(text)) and not valid_ip(text)


def parse_entry(text: str) -> tuple[str, list[str], str] | None:
    """(ip, names, comment) when `text` is an 'IP name [name...] [# comment]' line."""
    body, _, comment = text.partition("#")
    fields = body.split()
    if len(fields) < 2 or not valid_ip(fields[0]) or not all(valid_name(n) for n in fields[1:]):
        return None
    return fields[0], fields[1:], comment.strip()


@dataclass
class Line:
    text: str  # without the line ending
    eol: str
    kind: str = "other"  # entry, disabled, comment, blank or other (binary junk, unparseable text)
    ip: str = ""
    names: list[str] = field(default_factory=list)
    comment: str = ""
    number: int = 0  # 1-based position in the file as read (0 for new lines)

    @property
    def is_entry(self) -> bool:
        return self.kind in ("entry", "disabled")

    def has_name(self, name: str) -> bool:
        return name.lower() in (n.lower() for n in self.names)

    def has_ip(self, ip: str) -> bool:
        return self.is_entry and same_ip(self.ip, ip)


def make_line(text: str, eol: str, number: int = 0) -> Line:
    line = Line(text, eol, number=number)
    stripped = text.strip()
    if CONTROL_RE.search(text):
        return line
    if not stripped:
        line.kind = "blank"
        return line
    if stripped.startswith("#"):
        parsed = parse_entry(stripped.lstrip("#"))
        line.kind = "disabled" if parsed else "comment"
    else:
        parsed = parse_entry(stripped)
        line.kind = "entry" if parsed else "other"
    if parsed:
        line.ip, line.names, line.comment = parsed
    return line


def same_ip(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a == b


def entry_text(ip: str, names: list[str], comment: str = "", disabled: bool = False) -> str:
    text = f"{'# ' if disabled else ''}{ip:<15} {' '.join(names)}"
    return f"{text}  # {comment}" if comment else text


def with_names(line: Line, names: list[str], disabled: bool | None = None) -> Line:
    """A copy of an entry line holding only `names` (rebuilt, so its spacing is normalised)."""
    off = line.kind == "disabled" if disabled is None else disabled
    return make_line(entry_text(line.ip, names, line.comment, off), line.eol, line.number)


def toggled(line: Line) -> Line:
    """Enable a disabled line or disable an enabled one, keeping its original spacing."""
    if line.kind == "disabled":
        text = re.sub(r"^(\s*)#+\s*", r"\1", line.text, count=1)
    else:
        text = "# " + line.text
    return make_line(text, line.eol, line.number)


@dataclass
class HostsFile:
    path: Path
    data: bytes
    lines: list[Line]
    encoding: str
    bom: bytes

    @property
    def eol(self) -> str:
        endings = [line.eol for line in self.lines if line.eol]
        if endings:
            return max(set(endings), key=endings.count)
        return "\r\n" if IS_WINDOWS else "\n"

    def encode(self, lines: list[Line]) -> bytes:
        text = "".join(line.text + line.eol for line in lines)
        errors = "surrogateescape" if self.encoding == "utf-8" else "strict"
        return self.bom + text.encode(self.encoding, errors)

    @property
    def nul_bytes(self) -> int:
        return self.data.count(b"\0") if self.encoding == "utf-8" else 0


def decode(data: bytes) -> tuple[str, str, bytes]:
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding, "replace" if encoding != "utf-8" else "surrogateescape"), encoding, bom
    return data.decode("utf-8", "surrogateescape"), "utf-8", b""


def parse_bytes(path: Path, data: bytes) -> HostsFile:
    text, encoding, bom = decode(data)
    lines = []
    for number, match in enumerate(LINE_RE.finditer(text), 1):
        chunk = match.group(0)
        body = chunk.rstrip("\r\n")
        lines.append(make_line(body, chunk[len(body):], number))
    return HostsFile(path, data, lines, encoding, bom)


def load(path: Path) -> HostsFile:
    try:
        return parse_bytes(path, path.read_bytes())
    except FileNotFoundError:
        die(f"hosts file not found: {path}")
    except PermissionError:
        die(f"no permission to read {path}")


def display(text: str) -> str:
    """Text safe to print: undecodable bytes replaced, control characters shown as dots."""
    clean = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return CONTROL_RE.sub("·", clean)


def ip_kind(ip: str, names: list[str] | None = None) -> str:
    """block (0.0.0.0, ::), local (loopback or this computer's own name), system (IPv6 multicast and
    reserved addresses from the default Linux file) or redirect (anything else)."""
    try:
        address = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return "redirect"
    if address.is_unspecified:
        return "block"
    if address.is_loopback or (names and own_hostname() in (n.lower() for n in names)):
        return "local"
    if address.version == 6 and (address.is_multicast or address.is_reserved):
        return "system"
    return "redirect"


def own_hostname() -> str:
    return socket.gethostname().lower()


# --- listing ------------------------------------------------------------------------------

KIND_STYLES = {"block": ("green",), "local": ("cyan",), "system": ("dim",), "redirect": ("yellow", "bold")}


def cmd_list(hosts: HostsFile, args: argparse.Namespace) -> int:
    needle = (args.filter or "").lower()

    def matches(line: Line) -> bool:
        if not needle:
            return True
        haystack = " ".join([line.ip, *line.names, line.comment, line.text if not line.is_entry else ""]).lower()
        return needle in haystack

    shown = [line for line in hosts.lines if (line.is_entry or args.all) and matches(line)]
    print(f"{style('HOSTS', 'bold')}  {style(str(hosts.path), 'dim')}")
    if not shown:
        print(style("  no entries" + (f" matching '{args.filter}'" if needle else ""), "dim"))
    ip_width = min(max([len(line.ip) for line in shown if line.is_entry] + [7]), 39)
    for line in shown:
        number = style(f"{line.number:>4}", "dim")
        if not line.is_entry:
            if line.kind == "blank":
                print(f"{number}")
            elif line.kind == "other" and "\0" in line.text:
                print(f"{number}  {style(f'(binary data: {line.text.count(chr(0))} NUL bytes)', 'red')}")
            else:
                text = display(line.text)
                print(f"{number}  {style(text[:100] + ('...' if len(text) > 100 else ''), 'dim')}")
            continue
        kind = ip_kind(line.ip, line.names)
        state = style("on ", "green") if line.kind == "entry" else style("off", "dim")
        ip = line.ip.ljust(ip_width)
        names = " ".join(line.names)
        comment = f"  {style('# ' + line.comment, 'dim')}" if line.comment else ""
        if line.kind == "entry":
            ip, names, label = style(ip, *KIND_STYLES[kind]), style(names, "bold"), style(kind.ljust(8), *KIND_STYLES[kind])
        else:
            ip, names, label = style(ip, "dim"), style(names, "dim"), style(kind.ljust(8), "dim")
        print(f"{number}  {state}  {label}  {ip}  {names}{comment}")

    entries = [line for line in hosts.lines if line.is_entry]
    active = [line for line in entries if line.kind == "entry"]
    redirects = [line for line in active if ip_kind(line.ip, line.names) == "redirect"]
    print()
    summary = f"{len(active)} on, {len(entries) - len(active)} off"
    if redirects:
        summary += f", {style(f'{len(redirects)} redirect(s) to other addresses', 'yellow')}"
    print(summary)
    sys.stdout.flush()  # keep the warnings below the table when output is piped
    if hosts.nul_bytes:
        warn(f"the file contains {hosts.nul_bytes} NUL bytes (usually left behind by another program); "
             "kit keeps them as they are")
    other = [line for line in hosts.lines if line.kind == "other" and "\0" not in line.text]
    if other:
        warn(f"{len(other)} line(s) aren't valid entries (see them with --all): "
             + ", ".join(str(line.number) for line in other[:10]))
    return 0


# --- changes ------------------------------------------------------------------------------

def check_names(names: list[str]) -> list[str]:
    for name in names:
        if not valid_name(name):
            die(f"not a valid host name: {name}")
    return names


def ensure_trailing_eol(hosts: HostsFile, lines: list[Line]) -> list[Line]:
    if lines and not lines[-1].eol and lines[-1].text:
        last = lines[-1]
        lines = lines[:-1] + [Line(last.text, hosts.eol, last.kind, last.ip, last.names, last.comment, last.number)]
    return lines


def without_names(line: Line, names: set[str]) -> Line | None:
    """The line minus `names`; None when no names would be left."""
    kept = [n for n in line.names if n.lower() not in names]
    if len(kept) == len(line.names):
        return line
    return with_names(line, kept) if kept else None


def add_entries(hosts: HostsFile, ip: str, groups: list[list[str]], comment: str, replace: bool,
                notes: list[str]) -> list[Line]:
    """Map every group of names to `ip`, one line per group."""
    lines = list(hosts.lines)
    wanted = {n.lower() for group in groups for n in group}

    conflicts = [line for line in lines if line.kind == "entry" and not same_ip(line.ip, ip)
                 and any(line.has_name(n) for n in wanted)]
    if conflicts and not replace:
        for line in conflicts:
            clash = ", ".join(n for n in line.names if n.lower() in wanted)
            print(f"  line {line.number}: {clash} already -> {line.ip}")
        die("some names already point elsewhere - add --replace to move them")
    if conflicts:
        lines = [line for line in (without_names(line, wanted) if line in conflicts else line for line in lines) if line]
        notes.append(f"removed {', '.join(sorted(wanted))} from their old entries")

    new_lines: list[Line] = []
    for group in groups:
        todo = [n for n in group if not any(l.kind == "entry" and same_ip(l.ip, ip) and l.has_name(n) for l in lines)]
        if not todo:
            notes.append(f"{', '.join(group)} already -> {ip}")
            continue
        # A disabled line with exactly these names and IP is switched back on instead of added again.
        for index, line in enumerate(lines):
            if (line.kind == "disabled" and same_ip(line.ip, ip)
                    and sorted(n.lower() for n in line.names) == sorted(n.lower() for n in todo)):
                lines[index] = toggled(line)
                notes.append(f"re-enabled line {line.number}")
                break
        else:
            new_lines.append(make_line(entry_text(ip, todo, comment), hosts.eol))
    if new_lines:
        lines = ensure_trailing_eol(hosts, lines) + new_lines
    return lines


def remove_targets(hosts: HostsFile, targets: list[str], only_ips: set[str] | None, notes: list[str]) -> list[Line]:
    """Drop entries for IP targets, and names from any entry (on or off) for name targets."""
    ips = [t for t in targets if valid_ip(t)]
    names = {t.lower() for t in targets if not valid_ip(t)}
    lines, found = [], set()
    for line in hosts.lines:
        if not line.is_entry:
            lines.append(line)
            continue
        if only_ips is not None and ip_kind(line.ip) not in only_ips:
            lines.append(line)
            continue
        if any(line.has_ip(ip) for ip in ips):
            found.update(t for t in ips if line.has_ip(t))
            continue
        hit = {n.lower() for n in line.names} & names
        found.update(hit)
        result = without_names(line, names)
        if result is not None:
            lines.append(result)
    missing = [t for t in targets if t.lower() not in found and t not in found]
    for target in missing:
        notes.append(f"{target}: not found")
    return lines


def set_enabled(hosts: HostsFile, targets: list[str], enable: bool, notes: list[str]) -> list[Line]:
    source_kind = "disabled" if enable else "entry"
    ips = [t for t in targets if valid_ip(t)]
    names = {t.lower() for t in targets if not valid_ip(t)}
    lines, found = [], set()
    for line in hosts.lines:
        if line.kind != source_kind:
            lines.append(line)
            continue
        if any(line.has_ip(ip) for ip in ips):
            found.update(t for t in ips if line.has_ip(t))
            lines.append(toggled(line))
            continue
        hit = [n for n in line.names if n.lower() in names]
        if not hit:
            lines.append(line)
            continue
        found.update(n.lower() for n in hit)
        rest = [n for n in line.names if n.lower() not in names]
        if not rest:
            lines.append(toggled(line))
            continue
        # Only some of the line's names are targeted: split it in two.
        kept = with_names(line, rest)
        kept.eol = line.eol or hosts.eol
        moved = with_names(line, hit, disabled=not enable)
        moved.number = 0
        lines += [kept, moved]
        notes.append(f"line {line.number} also holds {', '.join(rest)}: split it so only "
                     f"{', '.join(hit)} {'is' if len(hit) == 1 else 'are'} {'enabled' if enable else 'disabled'}")
    for target in targets:
        if target.lower() not in found and target not in found:
            notes.append(f"{target}: no {'disabled' if enable else 'enabled'} entry found")
    return lines


# --- writing ------------------------------------------------------------------------------

def backup_dir() -> Path:
    if os.environ.get("KIT_BACKUP_DIR"):
        return Path(os.environ["KIT_BACKUP_DIR"])
    home = Path.home()
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return base / "kit" / "backups"


def backup_stem(path: Path) -> str:
    if is_system_file(path):
        return "hosts"
    return "hosts-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name)


def is_system_file(path: Path) -> bool:
    try:
        return path.resolve() == system_hosts_path().resolve()
    except OSError:
        return False


def save_backup(path: Path, data: bytes) -> Path:
    stem = f"{backup_stem(path)}.{time.strftime('%Y%m%d-%H%M%S')}"
    target, counter = backup_dir() / f"{stem}.bak", 1
    while target.exists():
        target, counter = backup_dir() / f"{stem}-{counter}.bak", counter + 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def list_backups(path: Path) -> list[Path]:
    folder = backup_dir()
    if not folder.is_dir():
        return []
    pattern = re.compile(re.escape(backup_stem(path)) + r"\.\d{8}-\d{6}(?:-\d+)?\.bak")
    found = [p for p in folder.iterdir() if pattern.fullmatch(p.name)]
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        die("can't ask for confirmation in a non-interactive shell - add --yes")
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print()
        die("no answer (input was closed) - add --yes to skip the question")


def print_diff(old: list[str], new: list[str]) -> None:
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for index in range(i1, i2):
            print(style(f"  {index + 1:>4} - {display(old[index])}", "red"))
        for index in range(j1, j2):
            print(style(f"  {index + 1:>4} + {display(new[index])}", "green"))


def write_file(path: Path, data: bytes) -> None:
    """Write in place (keeps the file's owner and permissions), asking for admin rights when needed."""
    try:
        with open(path, "r+b") as handle:
            handle.write(data)
            handle.truncate()
        return
    except PermissionError:
        pass
    print(style("the hosts file needs admin rights to change - asking for them...", "dim"), flush=True)
    if IS_WINDOWS:
        write_elevated_windows(path, data)
    else:
        write_with_sudo(path, data)


def apply_change(hosts: HostsFile, new_lines: list[Line], notes: list[str], args: argparse.Namespace,
                 question: str = "Apply?") -> int:
    for note in notes:
        print(style(f"note: {note}", "dim"))
    new_data = hosts.encode(new_lines)
    if new_data == hosts.data:
        print("nothing to change")
        return 0
    print(f"{style('Changes to', 'bold')} {hosts.path}:")
    print_diff([line.text for line in hosts.lines], [line.text for line in new_lines])
    if args.dry_run:
        print(style("dry run - nothing written", "dim"))
        return 0
    if not confirm(question, args.yes):
        print("nothing changed")
        return 1
    saved = save_backup(hosts.path, hosts.data)
    try:
        write_file(hosts.path, new_data)
    except BaseException:
        saved.unlink(missing_ok=True)  # nothing was written, so the backup would only be clutter
        raise
    print(f"{style('done', 'bold', 'green')} - backup: {style(str(saved), 'dim')}")
    after_change(hosts.path, args)
    return 0


def after_change(path: Path, args: argparse.Namespace) -> None:
    if args.flush and is_system_file(path):
        flush_dns(quiet=True)


# --- privileged helpers ---------------------------------------------------------------------

# Run by an elevated copy of this Python: overwrite the target in place so its permissions stay.
_COPY_SCRIPT = "import sys;d=open(sys.argv[1],'rb').read();f=open(sys.argv[2],'r+b');f.write(d);f.truncate();f.close()"


def elevated_copy_command(source: Path, target: Path) -> tuple[str, str]:
    """(program, parameters) that copy `source` over `target` when run elevated."""
    return sys.executable, subprocess.list2cmdline(["-c", _COPY_SCRIPT, str(source), str(target)])


def run_elevated_windows(program: str, parameters: str, show: bool = False, wait: bool = True) -> int:
    """Start `program` through the UAC prompt; returns its exit code (0 when not waiting)."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS, SEE_MASK_NOASYNC, ERROR_CANCELLED, INFINITE = 0x40, 0x100, 1223, 0xFFFFFFFF
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb, info.lpFile, info.lpParameters = "runas", program, parameters
    info.nShow = 1 if show else 0  # SW_SHOWNORMAL / SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == ERROR_CANCELLED:
            die("cancelled at the admin (UAC) prompt - nothing changed")
        die(f"couldn't start {program} as admin (Windows error {code})")
    if not info.hProcess:
        return 0
    try:
        if not wait:
            return 0
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def write_elevated_windows(target: Path, data: bytes) -> None:
    handle, name = tempfile.mkstemp(prefix="kit-hosts-", suffix=".tmp")
    source = Path(name)
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(data)
        program, parameters = elevated_copy_command(source, target)
        code = run_elevated_windows(program, parameters)
        if code != 0 or target.read_bytes() != data:
            die(f"the admin copy into {target} failed (exit code {code}) - the file may be locked by antivirus")
    finally:
        source.unlink(missing_ok=True)


def write_with_sudo(target: Path, data: bytes) -> None:
    if not shutil.which("sudo"):
        die(f"no permission to write {target} and sudo isn't installed - run this as root")
    command = ["sudo", "tee", str(target)]
    if not sys.stdin.isatty():
        command.insert(1, "-n")  # never hang waiting for a password nobody can type
    result = subprocess.run(command, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        if "-n" in command and "password" in detail.lower():
            die("sudo needs a password - run this in a terminal (or as root)")
        die(f"sudo couldn't write {target}: {detail or f'exit code {result.returncode}'}")


def flush_dns(quiet: bool = False) -> int:
    def run(command: list[str]) -> bool:
        try:
            return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def sudo(command: list[str]) -> list[str]:
        if os.name != "nt" and os.geteuid() != 0 and shutil.which("sudo"):
            return ["sudo", *([] if sys.stdin.isatty() else ["-n"]), *command]
        return command

    done: list[str] = []
    if IS_WINDOWS:
        if run(["ipconfig", "/flushdns"]):
            done.append("Windows DNS cache")
    elif IS_MACOS:
        if run(["dscacheutil", "-flushcache"]):
            done.append("directory service cache")
        if run(sudo(["killall", "-HUP", "mDNSResponder"])):
            done.append("mDNSResponder")
    else:
        if shutil.which("resolvectl") and (run(["resolvectl", "flush-caches"]) or run(sudo(["resolvectl", "flush-caches"]))):
            done.append("systemd-resolved")
        elif shutil.which("systemd-resolve") and run(sudo(["systemd-resolve", "--flush-caches"])):
            done.append("systemd-resolved")
        if shutil.which("nscd") and run(sudo(["nscd", "-i", "hosts"])):
            done.append("nscd")
        if not done and not (shutil.which("resolvectl") or shutil.which("nscd")):
            if not quiet:
                print("no DNS cache service found - nothing to flush (lookups read the hosts file directly)")
            return 0
    if done:
        print(f"{style('flushed', 'green')} {', '.join(done)}")
        return 0
    message = "couldn't flush the DNS cache (it may need admin rights)"
    if quiet:
        print(style(message, "dim"))
        return 0
    warn(message)
    return 1


# --- commands -----------------------------------------------------------------------------

def cmd_add(hosts: HostsFile, args: argparse.Namespace) -> int:
    if not valid_ip(args.ip):
        die(f"not a valid IP address: {args.ip}")
    names = check_names(args.names)
    notes: list[str] = []
    lines = add_entries(hosts, args.ip, [names], args.comment or "", args.replace, notes)
    return apply_change(hosts, lines, notes, args)


def block_groups(domains: list[str], www: bool) -> list[list[str]]:
    groups = []
    for domain in check_names([d.lower().rstrip(".") for d in domains]):
        group = [domain]
        if www and not domain.startswith("www."):
            group.append(f"www.{domain}")
        groups.append(group)
    return groups


def cmd_block(hosts: HostsFile, args: argparse.Namespace) -> int:
    notes: list[str] = []
    lines = add_entries(hosts, BLOCK_IP, block_groups(args.domains, args.www), BLOCK_COMMENT, args.replace, notes)
    return apply_change(hosts, lines, notes, args)


def cmd_unblock(hosts: HostsFile, args: argparse.Namespace) -> int:
    targets = [n for group in block_groups(args.domains, args.www) for n in group]
    notes: list[str] = []
    lines = remove_targets(hosts, targets, {"block", "local"}, notes)
    return apply_change(hosts, lines, notes, args)


def cmd_remove(hosts: HostsFile, args: argparse.Namespace) -> int:
    targets = [t for t in args.targets if valid_ip(t)] + check_names([t for t in args.targets if not valid_ip(t)])
    notes: list[str] = []
    return apply_change(hosts, remove_targets(hosts, targets, None, notes), notes, args)


def cmd_toggle(hosts: HostsFile, args: argparse.Namespace) -> int:
    targets = [t for t in args.targets if valid_ip(t)] + check_names([t for t in args.targets if not valid_ip(t)])
    notes: list[str] = []
    lines = set_enabled(hosts, targets, args.command == "enable", notes)
    return apply_change(hosts, lines, notes, args)


def cmd_edit(path: Path, args: argparse.Namespace) -> int:
    before = load(path)
    try:
        with open(path, "r+b"):
            writable = True
    except PermissionError:
        writable = False

    if IS_WINDOWS:
        if writable:
            subprocess.call(["notepad.exe", str(path)])
        else:
            print(style("opening Notepad as admin - save and close it when you're done...", "dim"))
            run_elevated_windows("notepad.exe", subprocess.list2cmdline([str(path)]), show=True)
    else:
        if not sys.stdin.isatty():
            die("kit hosts edit needs a terminal")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or next(
            (e for e in ("nano", "vim", "vi") if shutil.which(e)), None)
        if writable:
            if not editor:
                die("no editor found - set $EDITOR")
            subprocess.call([*editor.split(), str(path)])
        elif shutil.which("sudoedit") or shutil.which("sudo"):
            env = dict(os.environ)
            if editor:
                env.setdefault("SUDO_EDITOR", editor)
            subprocess.call(["sudoedit", str(path)] if shutil.which("sudoedit") else ["sudo", "-e", str(path)], env=env)
        else:
            die(f"no permission to edit {path} and sudo isn't installed")

    after = load(path)
    if after.data == before.data:
        print("no changes")
        return 0
    saved = save_backup(path, before.data)
    print(f"{style('Changes to', 'bold')} {path}:")
    print_diff([line.text for line in before.lines], [line.text for line in after.lines])
    print(f"{style('saved', 'bold', 'green')} - the previous version is in {style(str(saved), 'dim')}")
    bad = [line for line in after.lines if line.kind == "other" and "\0" not in line.text]
    if bad:
        warn("these lines aren't valid entries: " + ", ".join(f"{l.number} ({display(l.text)[:40]})" for l in bad[:5]))
    after_change(path, args)
    return 0


def cmd_backups(path: Path) -> int:
    backups = list_backups(path)
    if not backups:
        print(f"no backups of {path} yet " + style(f"(they go to {backup_dir()})", "dim"))
        return 0
    print(f"{style('BACKUPS', 'bold')} of {path}  {style('newest first', 'dim')}")
    for backup in backups:
        info = backup.stat()
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime))
        print(f"  {when}  {info.st_size:>7} B  {backup}")
    print(style("\nput one back with: kit hosts restore [BACKUP]", "dim"))
    return 0


def cmd_restore(hosts: HostsFile, args: argparse.Namespace) -> int:
    if args.backup:
        backup = Path(args.backup)
        if not backup.is_file():
            backup = backup_dir() / args.backup
        if not backup.is_file():
            die(f"backup not found: {args.backup}")
    else:
        backups = list_backups(hosts.path)
        if not backups:
            die(f"no backups of {hosts.path} found in {backup_dir()}")
        backup = backups[0]
    print(style(f"restoring from {backup}", "dim"))
    restored = parse_bytes(hosts.path, backup.read_bytes())
    # Compare lines for the diff, but write the backup's exact bytes.
    hosts_copy = HostsFile(hosts.path, hosts.data, hosts.lines, restored.encoding, restored.bom)
    if restored.data == hosts.data:
        print("the hosts file already matches that backup - nothing to change")
        return 0
    return apply_change(hosts_copy, restored.lines, [], args, question="Replace the hosts file with this backup?")


# --- main -----------------------------------------------------------------------------------

def main() -> int:
    settings = tool_settings()

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", default=argparse.SUPPRESS, help="work on this file instead of the system hosts file")
    common.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="don't ask for confirmation")
    common.add_argument("-n", "--dry-run", action="store_true", default=argparse.SUPPRESS, help="show the changes without writing them")
    common.add_argument("--no-flush", dest="flush", action="store_false", default=argparse.SUPPRESS,
                        help="don't flush the DNS cache after a change")

    parser = argparse.ArgumentParser(prog="kit hosts", parents=[common],
                                     description="View and edit the hosts file safely, with backups.")
    # Not set_defaults() for the shared options: that would also change the sub-commands' copies.
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("list", parents=[common], help="show entries (the default)")
    p.add_argument("filter", nargs="?", help="only entries containing this text")
    p.add_argument("-a", "--all", action="store_true", help="also show comments and blank lines")

    p = sub.add_parser("add", parents=[common], help="point one or more names at an IP")
    p.add_argument("ip")
    p.add_argument("names", nargs="+", metavar="NAME")
    p.add_argument("-c", "--comment", help="comment to put after the entry")
    p.add_argument("--replace", action="store_true", help="move names that already point somewhere else")

    for name, text in (("block", "block domains (points them at 0.0.0.0)"), ("unblock", "remove blocks for domains")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("domains", nargs="+", metavar="DOMAIN")
        p.add_argument("--www", action="store_true", help="also the www. version of each domain")
        if name == "block":
            p.add_argument("--replace", action="store_true", help="also move names that already point somewhere else")

    p = sub.add_parser("remove", aliases=["rm"], parents=[common], help="remove names or every entry for an IP")
    p.add_argument("targets", nargs="+", metavar="NAME|IP")
    for name, text in (("disable", "comment entries out (keeps them for later)"), ("enable", "switch disabled entries back on")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("targets", nargs="+", metavar="NAME|IP")

    sub.add_parser("edit", parents=[common], help="open the file in an editor (as admin when needed)")
    sub.add_parser("flush", parents=[common], help="flush the DNS cache")
    sub.add_parser("path", parents=[common], help="print where the hosts file is")
    sub.add_parser("backups", parents=[common], help="list saved backups")
    p = sub.add_parser("restore", parents=[common], help="put a backup back (the newest by default)")
    p.add_argument("backup", nargs="?", help="backup file (path or name from 'kit hosts backups')")

    args = parser.parse_args()
    for name, default in (("file", None), ("yes", False), ("dry_run", False), ("flush", settings["flush"]),
                          ("filter", None), ("all", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    args.command = {None: "list", "rm": "remove"}.get(args.command, args.command)
    path = Path(args.file).expanduser() if args.file else system_hosts_path()

    if args.command == "path":
        print(path)
        return 0
    if args.command == "flush":
        return flush_dns()
    if args.command == "backups":
        return cmd_backups(path)
    if args.command == "edit":
        return cmd_edit(path, args)

    hosts = load(path)
    handlers = {
        "list": cmd_list, "add": cmd_add, "block": cmd_block, "unblock": cmd_unblock,
        "remove": cmd_remove, "disable": cmd_toggle, "enable": cmd_toggle, "restore": cmd_restore,
    }
    return handlers[args.command](hosts, args)


if __name__ == "__main__":
    raise SystemExit(main())
