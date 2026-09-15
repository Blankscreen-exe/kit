"""List, add, remove and connect to the SSH hosts in ~/.ssh/config."""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from kitlib import die, error, style, warn

SUBCOMMANDS = ("list", "add", "remove", "show", "keys")
MARKER = "# added by kit ssh"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
OPTION_RE = re.compile(r"^(\S+?)(?:\s*=\s*|\s+)(.*)$")
BLOCK_START_RE = re.compile(r"^\s*(host|match)(\s|=)", re.IGNORECASE)
SHOW_KEYS = ("hostname", "user", "port", "identityfile", "proxyjump", "forwardagent", "localforward", "remoteforward")

HELP = """\
usage: kit ssh [--config PATH] [COMMAND]

Manage and connect to the hosts in your SSH config (~/.ssh/config).

commands:
  list                              list hosts (the default)
  <name> [ssh args...]              connect to a host
  add <name> <user@host[:port]> [-p PORT] [-i KEY]
                                    add a host
  remove <name>                     remove a host
  show <name> [--all]               settings ssh would use for a host (doesn't connect)
  keys                              list your public keys and which hosts use them

options:
  --config PATH                     use another config file (or set KIT_SSH_CONFIG)
"""


@dataclass
class HostEntry:
    names: list[str]
    source: Path
    line: int  # 1-based line number of the Host keyword
    options: dict[str, str] = field(default_factory=dict)

    @property
    def concrete_names(self) -> list[str]:
        """Names that can actually be connected to (wildcard patterns and negations excluded)."""
        return [n for n in self.names if not any(c in n for c in "*?!")]


# --- files -----------------------------------------------------------------------

def ssh_dir() -> Path:
    return Path.home() / ".ssh"


def backup_dir() -> Path:
    if os.environ.get("KIT_BACKUP_DIR"):
        return Path(os.environ["KIT_BACKUP_DIR"])
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "kit" / "backups"


def save_backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stem = f"ssh-{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    target, counter = backup_dir() / f"{stem}.bak", 1
    while target.exists():  # several changes within one second
        target, counter = backup_dir() / f"{stem}-{counter}.bak", counter + 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def read_text(path: Path) -> tuple[str, str]:
    """(content with \\n line endings, the file's own line ending)."""
    raw = path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else ""
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def write_text(path: Path, content: str, newline: str) -> None:
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created and os.name != "nt":
        path.parent.chmod(0o700)
    path.write_bytes(content.replace("\n", newline).encode("utf-8"))
    if created and os.name != "nt":
        path.chmod(0o600)  # ssh refuses configs that others can write to


# --- parsing ---------------------------------------------------------------------

def split_option(line: str) -> tuple[str, str] | None:
    match = OPTION_RE.match(line)
    if not match:
        return None
    value = match[2].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return match[1].lower(), value


def parse_config(path: Path, notes: list[str], seen: set[Path] | None = None) -> list[HostEntry]:
    """Host blocks from a config file, following Include directives."""
    seen = set() if seen is None else seen
    if not path.is_file():
        return []
    resolved = path.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)
    try:
        text, _ = read_text(path)
    except OSError as exc:
        notes.append(f"can't read {path}: {exc}")
        return []

    entries: list[HostEntry] = []
    current: HostEntry | None = None
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = split_option(line)
        if parsed is None:
            continue
        key, value = parsed
        if key == "host":
            current = HostEntry(value.split(), path, number)
            entries.append(current)
        elif key == "match":
            current = None
        elif key == "include":
            for pattern in re.findall(r'"[^"]*"|\S+', value):
                pattern = os.path.expanduser(pattern.strip('"'))
                if not os.path.isabs(pattern):
                    pattern = str(path.parent / pattern)  # relative includes are resolved against ~/.ssh
                for match in sorted(glob.glob(pattern)):
                    entries.extend(parse_config(Path(match), notes, seen))
        elif current is not None:
            current.options.setdefault(key, value)  # like ssh, the first value wins
    return entries


def find_entries(entries: list[HostEntry], name: str) -> list[HostEntry]:
    return [e for e in entries if name in e.names]


# --- ssh itself --------------------------------------------------------------------

def ssh_command(config: Path, custom: bool) -> list[str]:
    exe = shutil.which("ssh")
    if not exe:
        hint = "Settings > System > Optional features > OpenSSH Client" if os.name == "nt" else "e.g. sudo apt install openssh-client"
        die(f"ssh client not found - install OpenSSH ({hint})")
    return [exe, "-F", str(config)] if custom else [exe]


def run(command: list[str]) -> int:
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130


# --- commands ----------------------------------------------------------------------

def cmd_list(config: Path, entries: list[HostEntry], notes: list[str]) -> int:
    rows = []
    for entry in entries:
        names = entry.concrete_names
        if not names:
            continue
        options = entry.options
        user = options.get("user")
        port = options.get("port")
        target = options.get("hostname", names[0])
        if user:
            target = f"{user}@{target}"
        if port and port != "22":
            target = f"{target}:{port}"
        rows.append((", ".join(names), target, options.get("identityfile", "")))

    print(f"{style('SSH hosts', 'bold')}  {style(config, 'dim')}")
    print()
    if not rows:
        print("  no hosts yet - add one with: kit ssh add <name> user@host")
    else:
        name_width = max(4, *(len(r[0]) for r in rows))
        target_width = max(6, *(len(r[1]) for r in rows))
        print(f"  {style('NAME'.ljust(name_width), 'bold')}  {style('TARGET'.ljust(target_width), 'bold')}  {style('KEY', 'bold')}")
        for name, target, key in rows:
            print(f"  {style(name.ljust(name_width), 'green')}  {target.ljust(target_width)}  {style(key, 'dim')}".rstrip())
    print()
    print(style("connect: kit ssh <name>    add: kit ssh add <name> user@host    details: kit ssh show <name>", "dim"))
    for note in notes:
        warn(note)
    return 0


def cmd_connect(config: Path, custom: bool, entries: list[HostEntry], name: str, extra: list[str]) -> int:
    if not find_entries(entries, name):
        warn(f"'{name}' is not a Host in {config} - passing it to ssh as a hostname")
    return run([*ssh_command(config, custom), name, *extra])


def format_identity(value: str) -> str:
    keys = ssh_dir()
    path = Path(value).expanduser()
    if not path.is_file() and (keys / value).is_file():
        path = keys / value
    if path.suffix == ".pub" and path.with_suffix("").is_file():
        warn(f"IdentityFile should be the private key - using {path.with_suffix('').name} instead of {path.name}")
        path = path.with_suffix("")
    if not path.is_file():
        warn(f"key file not found: {value} (adding it anyway)")
        result = value
    else:
        try:
            result = "~/.ssh/" + path.resolve().relative_to(keys.resolve()).as_posix()
        except ValueError:
            result = str(path.resolve())
    return f'"{result}"' if " " in result else result


def cmd_add(config: Path, entries: list[HostEntry], argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit ssh add", description="Add a Host entry to your SSH config.")
    parser.add_argument("name", help="short name to connect with, e.g. myserver")
    parser.add_argument("target", metavar="user@host[:port]", help="who and where, e.g. deploy@203.0.113.10")
    parser.add_argument("-p", "--port", type=int, help="SSH port (default 22)")
    parser.add_argument("-i", "--identity", metavar="KEY", help="private key file, e.g. id_ed25519 or a full path")
    args = parser.parse_args(argv)

    name = args.name
    if not NAME_RE.fullmatch(name):
        die(f"invalid name '{name}': use letters, digits, dots, dashes and underscores")
    if name in SUBCOMMANDS:
        die(f"'{name}' is a kit ssh command - pick another name")
    existing = find_entries(entries, name)
    if existing:
        die(f"'{name}' already exists ({existing[0].source}, line {existing[0].line}) - remove it first or pick another name")

    user, at, host = args.target.rpartition("@")
    if not at:
        user, host = "", args.target
    port = args.port
    if host.count(":") == 1:
        host, port_text = host.split(":")
        if not port_text.isdigit():
            die(f"invalid port in '{args.target}'")
        port = port or int(port_text)
    if not host:
        die(f"missing host in '{args.target}' - use user@host")
    if port is not None and not 1 <= port <= 65535:
        die(f"invalid port: {port}")

    block = [f"{MARKER} on {time.strftime('%Y-%m-%d')}", f"Host {name}", f"    HostName {host}"]
    if user:
        block.append(f"    User {user}")
    if port and port != 22:
        block.append(f"    Port {port}")
    if args.identity:
        block.append(f"    IdentityFile {format_identity(args.identity)}")

    text, newline = read_text(config)
    body = text.rstrip("\n")
    backup = save_backup(config)
    write_text(config, (body + "\n\n" if body else "") + "\n".join(block) + "\n", newline)

    print(f"{style('added', 'bold', 'green')} {name} to {config}")
    for line in block[1:]:
        print(style(f"  {line}", "dim"))
    if backup:
        print(style(f"backup: {backup}", "dim"))
    print(f"connect with:  kit ssh {name}")
    return 0


def cmd_remove(config: Path, entries: list[HostEntry], argv: list[str]) -> int:
    if len(argv) != 1:
        die("usage: kit ssh remove <name>")
    name = argv[0]
    matches = find_entries(entries, name)
    if not matches:
        die(f"no Host '{name}' in {config}")
    if len(matches) > 1:
        places = ", ".join(f"{m.source.name} line {m.line}" for m in matches)
        die(f"'{name}' appears in several Host blocks ({places}) - edit the file by hand")
    entry = matches[0]
    if entry.names != [name]:
        die(f"'{name}' shares its Host line with {' '.join(n for n in entry.names if n != name)} "
            f"({entry.source}, line {entry.line}) - edit the file by hand")

    text, newline = read_text(entry.source)
    lines = text.split("\n")
    start = entry.line - 1
    end = start + 1
    while end < len(lines) and not BLOCK_START_RE.match(lines[end]):
        end += 1
    # blank lines and comments just before the next block belong to that block
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    if start > 0 and lines[start - 1].strip().startswith(MARKER):
        start -= 1
    if start > 0 and not lines[start - 1].strip():
        start -= 1

    removed = [line for line in lines[start:end] if line.strip()]
    backup = save_backup(entry.source)
    write_text(entry.source, "\n".join(lines[:start] + lines[end:]), newline)

    print(f"{style('removed', 'bold', 'yellow')} {name} from {entry.source}")
    for line in removed:
        print(style(f"  {line}", "dim"))
    if backup:
        print(style(f"backup: {backup}", "dim"))
    return 0


def cmd_show(config: Path, custom: bool, entries: list[HostEntry], argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit ssh show", description="Show the settings ssh would use for a host.")
    parser.add_argument("name")
    parser.add_argument("--all", action="store_true", help="print every setting, not just the main ones")
    args = parser.parse_args(argv)

    result = subprocess.run([*ssh_command(config, custom), "-G", args.name], capture_output=True, text=True)
    if result.returncode != 0:
        error(result.stderr.strip() or f"ssh -G exited with code {result.returncode}")
        return result.returncode

    settings: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key, []).append(value)

    matches = find_entries(entries, args.name)
    print(style(args.name, "bold", "green") + style(f"  {matches[0].source}, line {matches[0].line}" if matches else "", "dim"))
    if not matches:
        warn(f"'{args.name}' is not a Host in {config} - these are ssh's defaults for that hostname")
    shown = [k for k in SHOW_KEYS if k in settings]
    width = max(len(k) for k in shown) if shown else 0
    for key in shown:
        print(f"  {style(key.ljust(width), 'cyan')}  {', '.join(settings[key])}")
    if args.all:
        print()
        for key, values in settings.items():
            if key not in SHOW_KEYS:
                print(style(f"  {key} {', '.join(values)}", "dim"))
    else:
        print(style("\n  add --all to see every setting", "dim"))
    return 0


def cmd_keys(entries: list[HostEntry]) -> int:
    keys = ssh_dir()
    public_keys = sorted(keys.glob("*.pub"))
    if not public_keys:
        print(f"no public keys in {keys} - create one with: ssh-keygen -t ed25519")
        return 0

    used_by: dict[str, list[str]] = {}
    for entry in entries:
        identity = entry.options.get("identityfile")
        if identity and entry.concrete_names:
            key = os.path.normcase(str(Path(os.path.expanduser(identity)).resolve()))
            used_by.setdefault(key, []).extend(entry.concrete_names)

    keygen = shutil.which("ssh-keygen")
    rows = []
    for public in public_keys:
        parts = public.read_text(encoding="utf-8", errors="replace").split()
        kind = parts[0].removeprefix("ssh-") if parts else "?"
        fingerprint = ""
        if keygen:
            out = subprocess.run([keygen, "-lf", str(public)], capture_output=True, text=True, timeout=10).stdout.split()
            if len(out) >= 2:
                fingerprint = out[1]
        private = public.with_suffix("")
        hosts = used_by.get(os.path.normcase(str(private.resolve())), [])
        status = ", ".join(hosts) if hosts else "-"
        if not private.is_file():
            status = "private key missing"
        rows.append((private.name, kind, fingerprint, status, private.is_file()))

    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    print(f"{style('SSH keys', 'bold')}  {style(keys, 'dim')}")
    print()
    headers = ("KEY", "TYPE", "FINGERPRINT")
    print("  " + "  ".join(style(h.ljust(widths[i]), "bold") for i, h in enumerate(headers)) + "  " + style("USED BY", "bold"))
    for name, kind, fingerprint, status, has_private in rows:
        status_text = style(status, "red") if not has_private else style(status, "green" if status != "-" else "dim")
        print(f"  {style(name.ljust(widths[0]), 'cyan')}  {kind.ljust(widths[1])}  {style(fingerprint.ljust(widths[2]), 'dim')}  {status_text}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    config_arg = None
    if argv[:1] == ["--config"]:
        if len(argv) < 2:
            die("--config needs a file path")
        config_arg, argv = argv[1], argv[2:]
    elif argv and argv[0].startswith("--config="):
        config_arg, argv = argv[0].split("=", 1)[1], argv[1:]

    if argv[:1] in (["-h"], ["--help"]):
        print(HELP)
        return 0

    chosen = config_arg or os.environ.get("KIT_SSH_CONFIG")
    config = Path(chosen).expanduser() if chosen else ssh_dir() / "config"
    custom = bool(chosen)
    notes: list[str] = []
    entries = parse_config(config, notes)

    command, rest = (argv[0], argv[1:]) if argv else ("list", [])
    if command == "list":
        return cmd_list(config, entries, notes)
    if command == "add":
        return cmd_add(config, entries, rest)
    if command == "remove":
        return cmd_remove(config, entries, rest)
    if command == "show":
        return cmd_show(config, custom, entries, rest)
    if command == "keys":
        return cmd_keys(entries)
    if command.startswith("-"):
        die(f"unknown option '{command}' - see: kit ssh --help")
    return cmd_connect(config, custom, entries, command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
