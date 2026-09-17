"""See and permanently change environment variables: the registry on Windows, shell startup files on Linux/macOS."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from kitlib import die, style, warn

IS_WINDOWS = os.name == "nt"
SEP = ";" if IS_WINDOWS else ":"
USER_ENV_KEY = "Environment"
MACHINE_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
ETC_ENVIRONMENT = "/etc/environment"
BEGIN_MARKER = "# >>> kit env >>>"
END_MARKER = "# <<< kit env <<<"
PATHFIX_MARKER = "# >>> kit pathfix >>>"
PATH_HELPER = "__kit_env_add"
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API")
POSIX_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PS_QUOTES = "'‘’‚‛"  # PowerShell treats all of these as single quotes


class Unsupported(Exception):
    pass


@dataclass
class Var:
    scope: str
    name: str
    value: str
    note: str = ""  # Windows: "expands" for REG_EXPAND_SZ; Linux: where a PATH entry goes


# --- shared helpers ----------------------------------------------------------------------

def is_secret(name: str) -> bool:
    upper = name.upper()
    return any(word in upper for word in SECRET_WORDS)


def masked(name: str, value: str | None, show_secrets: bool) -> str:
    if value is None:
        return ""
    return "****" if value and is_secret(name) and not show_secrets else value


def check_name(name: str) -> None:
    if IS_WINDOWS:
        if not name or "=" in name or "\0" in name or name != name.strip():
            die(f"invalid variable name {name!r}: it can't be empty, contain '=' or start/end with spaces")
    elif not POSIX_NAME.fullmatch(name):
        die(f"invalid variable name {name!r}: use letters, digits and _, and don't start with a digit")


def expand(text: str) -> str:
    """%VAR% (Windows) or $VAR expanded from this process's environment; unknown names stay as they are."""
    if IS_WINDOWS:
        return re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), text)
    return os.path.expandvars(text)


def normalise(entry: str) -> str:
    text = os.path.normpath(expand(os.path.expanduser(entry.strip().strip('"'))))
    if IS_WINDOWS:
        text = text.lower()
    return text.rstrip("\\/") or text


def clean_dir(raw: str) -> str:
    """The folder as it will be stored: absolute, without a trailing slash. %VARS% stay unexpanded on Windows."""
    raw = raw.strip().strip('"')
    if IS_WINDOWS and "%" in raw:
        stripped = raw.rstrip("\\/")
        return raw if stripped.endswith(":") else stripped
    return os.path.abspath(os.path.expanduser(expand(raw)))


def home_dir(args: argparse.Namespace) -> Path:
    return Path(args.home).expanduser() if args.home else Path.home()


def backup_dir(home: Path) -> Path:
    if os.environ.get("KIT_BACKUP_DIR"):
        return Path(os.environ["KIT_BACKUP_DIR"])
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return base / "kit" / "backups"


def save_backup(name: str, content: str, home: Path, suffix: str = ".bak") -> Path:
    stem = f"{name}.{time.strftime('%Y%m%d-%H%M%S')}"
    target, counter = backup_dir(home) / f"{stem}{suffix}", 1
    while target.exists():  # several changes within one second
        target, counter = backup_dir(home) / f"{stem}-{counter}{suffix}", counter + 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


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


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


# --- Windows: registry ---------------------------------------------------------------------

def broadcast_environment_change() -> None:
    """Tells Explorer and other programs that environment variables changed, so new terminals see it."""
    import ctypes
    from ctypes import wintypes

    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]
    result = wintypes.DWORD()
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    send(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))


class RegistryStore:
    """String values of one registry key, edited in memory and written by save()."""

    def __init__(self, scope: str, subkey: str, machine_hive: bool, real: bool) -> None:
        self.scope, self.subkey, self.machine_hive, self.real = scope, subkey, machine_hive, real
        self.label = ("HKLM\\" if machine_hive else "HKCU\\") + subkey
        self.data = self._read()  # lower-case name -> (name, value, registry type)
        self.original = dict(self.data)

    def _hive(self):
        import winreg

        return winreg.HKEY_LOCAL_MACHINE if self.machine_hive else winreg.HKEY_CURRENT_USER

    def _read(self) -> dict[str, tuple[str, str, int]]:
        import winreg

        found: dict[str, tuple[str, str, int]] = {}
        try:
            with winreg.OpenKey(self._hive(), self.subkey) as key:
                index = 0
                while True:
                    try:
                        name, value, kind = winreg.EnumValue(key, index)  # REG_EXPAND_SZ comes back unexpanded
                    except OSError:
                        break
                    index += 1
                    if name and kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        found[name.lower()] = (name, str(value), kind)
        except FileNotFoundError:
            pass
        return found

    def check_writable(self) -> None:
        if self.machine_hive and not is_admin():
            die("changing machine variables needs admin rights: run this from a terminal opened with "
                "'Run as administrator' (or put sudo in front if sudo for Windows is turned on)")

    def variables(self) -> list[Var]:
        import winreg

        return [Var(self.scope, name, value, "expands" if kind == winreg.REG_EXPAND_SZ else "")
                for name, value, kind in sorted(self.data.values(), key=lambda entry: entry[0].lower())]

    def flat(self) -> dict[str, str]:
        return {name: value for name, value, _ in self.data.values()}

    def get(self, name: str) -> str | None:
        entry = self.data.get(name.lower())
        return entry[1] if entry else None

    def expands(self, name: str) -> bool:
        import winreg

        entry = self.data.get(name.lower())
        return bool(entry and entry[2] == winreg.REG_EXPAND_SZ)

    def set(self, name: str, value: str) -> None:
        import winreg

        existing = self.data.get(name.lower())
        if existing:
            name = existing[0]  # keep the spelling Windows already has
        expand = bool(re.search(r"%[^%]+%", value)) or bool(existing and existing[2] == winreg.REG_EXPAND_SZ)
        self.data[name.lower()] = (name, value, winreg.REG_EXPAND_SZ if expand else winreg.REG_SZ)

    def unset(self, name: str) -> bool:
        return self.data.pop(name.lower(), None) is not None

    def load_values(self, values: dict[str, list]) -> None:
        self.data = {name.lower(): (name, str(value), int(kind)) for name, (value, kind) in values.items()}

    def _path_list(self) -> list[str]:
        raw = self.get("Path") or ""
        entries = raw.split(";") if raw else []
        while entries and not entries[-1].strip():
            entries.pop()
        return entries

    def _set_path(self, entries: list[str]) -> None:
        import winreg

        if not entries:
            self.unset("Path")
            return
        existing = self.data.get("path")
        name, kind = (existing[0], existing[2]) if existing else ("Path", winreg.REG_EXPAND_SZ)
        self.data["path"] = (name, ";".join(entries), kind)

    def path_entries(self) -> list[str]:
        return [entry for entry in self._path_list() if entry.strip()]

    def path_add(self, directory: str, front: bool) -> bool:
        entries = self._path_list()
        key = normalise(directory)
        if any(entry.strip() and normalise(entry) == key for entry in entries):
            return False
        self._set_path([directory, *entries] if front else [*entries, directory])
        return True

    def path_remove(self, directory: str) -> int:
        entries = self._path_list()
        key = normalise(directory)
        kept = [entry for entry in entries if not (entry.strip() and normalise(entry) == key)]
        if len(kept) != len(entries):
            self._set_path(kept)
        return len(entries) - len(kept)

    def changed(self) -> bool:
        return self.data != self.original

    def save(self, home: Path) -> list[Path]:
        import winreg

        snapshot = {
            "tool": "kit env", "scope": self.scope, "key": self.label,
            "values": {name: [value, kind] for name, value, kind in self.original.values()},
        }
        backup = save_backup(f"env-{self.scope}", json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", home, ".json")
        with winreg.CreateKeyEx(self._hive(), self.subkey, 0, winreg.KEY_SET_VALUE) as key:
            for lower, (name, value, kind) in self.data.items():
                if self.original.get(lower) != (name, value, kind):
                    winreg.SetValueEx(key, name, 0, kind, value)
            for lower, (name, _, _) in self.original.items():
                if lower not in self.data:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
        if self.real:
            broadcast_environment_change()
        self.original = dict(self.data)
        return [backup]


# --- Linux / macOS: the kit env block in ~/.bashrc and ~/.zshrc --------------------------------

def rc_files(home: Path) -> list[Path]:
    files = [home / ".bashrc"]
    if (home / ".zshrc").is_file() or sys.platform == "darwin":
        files.append(home / ".zshrc")
    return files


def find_block(lines: list[str]) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if line.strip() == BEGIN_MARKER), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == END_MARKER), None)
    return (start, end) if end is not None else None


def parse_block(text: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(variables, PATH additions as (front|end, folder)) from the kit env block in a file's text."""
    lines = text.split("\n")
    span = find_block(lines)
    variables: dict[str, str] = {}
    paths: list[tuple[str, str]] = []
    if span is None:
        return variables, paths
    for line in lines[span[0] + 1:span[1]]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        if len(tokens) == 2 and tokens[0] == "export" and "=" in tokens[1]:
            name, value = tokens[1].split("=", 1)
            if name != "PATH":
                variables[name] = value
        elif len(tokens) == 3 and tokens[0] == PATH_HELPER and tokens[1] in ("front", "end"):
            paths.append((tokens[1], tokens[2]))
    return variables, paths


def block_text(variables: dict[str, str], paths: list[tuple[str, str]]) -> str:
    if not variables and not paths:
        return ""
    lines = [
        BEGIN_MARKER,
        "# Managed by 'kit env': change it with kit env set / unset / path rather than by hand.",
    ]
    lines += [f"export {name}={shlex.quote(value)}" for name, value in variables.items()]
    if paths:
        lines.append(f'{PATH_HELPER}() {{ case ":$PATH:" in *":$2:"*) ;; *) if [ "$1" = front ]; '
                     f'then PATH="$2${{PATH:+:$PATH}}"; else PATH="${{PATH:+$PATH:}}$2"; fi ;; esac; }}')
        lines += [f"{PATH_HELPER} {where} {shlex.quote(folder)}" for where, folder in paths]
        lines += ["export PATH", f"unset -f {PATH_HELPER}"]
    lines.append(END_MARKER)
    return "\n".join(lines)


def replace_block(text: str, block: str) -> str:
    """The file's text with the kit env block replaced, added (before any kit pathfix block) or removed."""
    lines = text.split("\n")
    new_lines = block.split("\n") if block else []
    span = find_block(lines)
    if span is not None:
        before, after = lines[:span[0]], lines[span[1] + 1:]
        if not new_lines:
            while before and not before[-1].strip() and after and not after[0].strip():
                before.pop()  # don't leave a double blank line behind
        lines = before + new_lines + after
    elif new_lines:
        pathfix = next((i for i, line in enumerate(lines) if line.strip() == PATHFIX_MARKER), None)
        if pathfix is not None:
            lines = lines[:pathfix] + new_lines + [""] + lines[pathfix:]
        else:
            body = "\n".join(lines).rstrip("\n")
            return (body + "\n\n" if body else "") + block + "\n"
    result = "\n".join(lines).rstrip("\n")
    return result + "\n" if result else ""


class RcStore:
    scope = "user"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.files = rc_files(home)
        self.label = " and ".join(f"~/{path.name}" for path in self.files)
        self.variables_, self.paths = {}, []
        for path in self.files:
            if path.is_file():
                variables, paths = parse_block(path.read_text(encoding="utf-8"))
                if variables or paths:
                    self.variables_, self.paths = variables, paths
                    break
        self.original = (dict(self.variables_), list(self.paths))

    def check_writable(self) -> None:
        pass

    def variables(self) -> list[Var]:
        rows = [Var(self.scope, name, value) for name, value in self.variables_.items()]
        rows += [Var(self.scope, "PATH", folder, f"added at the {where}") for where, folder in self.paths]
        return rows

    def flat(self) -> dict[str, str]:
        values = dict(self.variables_)
        if self.paths:
            values["PATH (kit additions)"] = "  ".join(f"{folder} ({where})" for where, folder in self.paths)
        return values

    def get(self, name: str) -> str | None:
        return self.variables_.get(name)

    def expands(self, name: str) -> bool:
        return False

    def set(self, name: str, value: str) -> None:
        self.variables_[name] = value

    def unset(self, name: str) -> bool:
        return self.variables_.pop(name, None) is not None

    def load_text(self, text: str) -> None:
        self.variables_, self.paths = parse_block(text)

    def path_entries(self) -> list[str]:
        return [folder for _, folder in self.paths]

    def path_add(self, directory: str, front: bool) -> bool:
        where = "front" if front else "end"
        key = normalise(directory)
        existing = [(w, f) for w, f in self.paths if normalise(f) == key]
        if existing and existing[0][0] == where:
            return False
        self.paths = [(w, f) for w, f in self.paths if normalise(f) != key] + [(where, directory)]
        return True

    def path_remove(self, directory: str) -> int:
        key = normalise(directory)
        kept = [(w, f) for w, f in self.paths if normalise(f) != key]
        removed = len(self.paths) - len(kept)
        self.paths = kept
        return removed

    def changed(self) -> bool:
        return (self.variables_, self.paths) != self.original

    def save(self, home: Path) -> list[Path]:
        block = block_text(self.variables_, self.paths)
        backups = []
        for path in self.files:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            new = replace_block(text, block)
            if new == text:
                continue
            if path.is_file():
                backups.append(save_backup(f"env-{path.name.lstrip('.')}", text, home))
            path.write_text(new, encoding="utf-8", newline="\n")
        self.original = (dict(self.variables_), list(self.paths))
        return backups


# --- Linux: /etc/environment ------------------------------------------------------------------

ETC_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class EtcStore:
    """/etc/environment: NAME="value" lines read at login by pam_env, without any expansion."""

    scope = "machine"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.label = str(path)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        self.original_text = text
        self.lines = text.splitlines()
        self.original_lines = list(self.lines)

    @staticmethod
    def parse(line: str) -> tuple[str, str] | None:
        if line.lstrip().startswith("#"):
            return None
        match = ETC_LINE.match(line)
        if not match:
            return None
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return match.group(1), value

    def _writable_directly(self) -> bool:
        return os.access(self.path, os.W_OK) if self.path.exists() else os.access(self.path.parent, os.W_OK)

    def check_writable(self) -> None:
        if not self._writable_directly() and not shutil.which("sudo"):
            die(f"{self.path} needs root to change and sudo isn't installed - run this as root")

    def _pairs(self) -> list[tuple[int, str, str]]:
        return [(i, *parsed) for i, line in enumerate(self.lines) if (parsed := self.parse(line))]

    def variables(self) -> list[Var]:
        latest = {name: value for _, name, value in self._pairs()}
        return [Var(self.scope, name, value) for name, value in latest.items()]

    def flat(self) -> dict[str, str]:
        return {name: value for _, name, value in self._pairs()}

    def get(self, name: str) -> str | None:
        return self.flat().get(name)

    def expands(self, name: str) -> bool:
        return False

    def set(self, name: str, value: str) -> None:
        if '"' in value or "\n" in value:
            die(f"{self.path} can't hold values with double quotes or line breaks")
        line = f'{name}="{value}"'
        matches = [i for i, found, _ in self._pairs() if found == name]
        if matches:
            self.lines[matches[-1]] = line
            for index in reversed(matches[:-1]):
                del self.lines[index]
        else:
            self.lines.append(line)

    def unset(self, name: str) -> bool:
        matches = [i for i, found, _ in self._pairs() if found == name]
        for index in reversed(matches):
            del self.lines[index]
        return bool(matches)

    def load_text(self, text: str) -> None:
        self.lines = text.splitlines()

    def path_entries(self) -> list[str]:
        return [entry for entry in (self.get("PATH") or "").split(":") if entry]

    def _require_path(self) -> list[str]:
        value = self.get("PATH")
        if value is None:
            die(f"{self.path} has no PATH line, so there's nothing to add to - set one first with: "
                "kit env set PATH <folders> --machine")
        return value.split(":") if value else []

    def path_add(self, directory: str, front: bool) -> bool:
        entries = self._require_path()
        key = normalise(directory)
        if any(entry and normalise(entry) == key for entry in entries):
            return False
        self.set("PATH", ":".join([directory, *entries] if front else [*entries, directory]))
        return True

    def path_remove(self, directory: str) -> int:
        entries = self._require_path()
        key = normalise(directory)
        kept = [entry for entry in entries if not (entry and normalise(entry) == key)]
        if len(kept) != len(entries):
            self.set("PATH", ":".join(kept))
        return len(entries) - len(kept)

    def changed(self) -> bool:
        return self.lines != self.original_lines

    def save(self, home: Path) -> list[Path]:
        backups = [save_backup("env-etc-environment", self.original_text, home)] if self.path.exists() else []
        text = "\n".join(self.lines) + "\n" if self.lines else ""
        if self._writable_directly():
            self.path.write_text(text, encoding="utf-8", newline="\n")
        else:
            print(style(f"writing {self.path} through sudo", "dim"))
            result = subprocess.run(["sudo", "tee", str(self.path)], input=text.encode("utf-8"),
                                    stdout=subprocess.DEVNULL)
            if result.returncode != 0:
                die(f"couldn't write {self.path} (sudo tee exited with {result.returncode}) - nothing changed")
        self.original_text, self.original_lines = text, list(self.lines)
        return backups


# --- choosing a store --------------------------------------------------------------------------

Store = RegistryStore | RcStore | EtcStore


def open_store(args: argparse.Namespace, machine: bool) -> Store:
    if IS_WINDOWS:
        if machine:
            if args.machine_registry_key:  # testing: a throwaway HKCU key stands in for the machine key
                return RegistryStore("machine", args.machine_registry_key, machine_hive=False, real=False)
            return RegistryStore("machine", MACHINE_ENV_KEY, machine_hive=True, real=True)
        return RegistryStore("user", args.registry_key, machine_hive=False, real=args.registry_key == USER_ENV_KEY)
    if machine:
        if not args.etc_environment and not Path(ETC_ENVIRONMENT).exists() and sys.platform == "darwin":
            raise Unsupported("macOS has no /etc/environment, so there are no machine variables to manage here")
        return EtcStore(Path(args.etc_environment or ETC_ENVIRONMENT))
    return RcStore(home_dir(args))


def other_store(args: argparse.Namespace, store: Store) -> Store | None:
    try:
        return open_store(args, store.scope != "machine")
    except Unsupported:
        return None


def scope_text(store: Store) -> str:
    return f"{store.scope} environment ({store.label})"


def preview(before: dict[str, str], after: dict[str, str], show_secrets: bool = True) -> list[str]:
    lines = []
    for name in sorted(set(before) | set(after), key=str.lower):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None:
            lines.append(f"  {style('+', 'green')} {name} = {masked(name, new, show_secrets)}")
        elif new is None:
            lines.append(f"  {style('-', 'red')} {name}  {style('(was ' + masked(name, old, show_secrets) + ')', 'dim')}")
        else:
            lines.append(f"  {style('~', 'yellow')} {name}")
            lines.append(f"      old: {masked(name, old, show_secrets)}")
            lines.append(f"      new: {masked(name, new, show_secrets)}")
    return lines


def save_store(store: Store, home: Path) -> None:
    backups = store.save(home)
    print(f"{style('done', 'bold', 'green')} - saved to the {scope_text(store)}")
    if backups:
        print(style(f"backup: {backups[0]}   (undo with: kit env restore)", "dim"))


# --- the current terminal ----------------------------------------------------------------------

def ps_quote(text: str) -> str:
    return "'" + "".join(ch * 2 if ch in PS_QUOTES else ch for ch in text) + "'"


def apply_line(shell: str, name: str, value: str | None) -> str:
    """A command that makes the change in a running shell; sourced by the kit wrapper in shell/."""
    if shell == "powershell":
        target = "$null" if value is None else ps_quote(value)
        return f"[Environment]::SetEnvironmentVariable({ps_quote(name)}, {target}, 'Process')"
    return f"unset {name}" if value is None else f"export {name}={shlex.quote(value)}"


def hint_line(shell: str, name: str, value: str | None) -> str:
    if shell == "powershell" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return f"Remove-Item Env:{name}" if value is None else f"$env:{name} = {ps_quote(value)}"
    return apply_line(shell, name, value)


def current_shell() -> str:
    shell = os.environ.get("KIT_ENV_SHELL", "")
    if shell in ("powershell", "bash"):
        return shell
    return "powershell" if IS_WINDOWS else "bash"


def finish_session(apply_lines: list[str], hints: list[str]) -> None:
    """Runs the commands in the calling shell through the kit wrapper in shell/, or prints them to run by hand."""
    apply = os.environ.get("KIT_ENV_APPLY")
    if apply and Path(apply).is_file():
        try:
            Path(apply).write_text("\n".join(apply_lines) + "\n", encoding="utf-8")
            print(style("this terminal has the change too", "dim"))
            return
        except OSError:
            pass
    print("New terminals pick it up. To use it in this one, run:")
    for line in hints:
        print(f"  {style(line, 'cyan')}")


def variable_session(name: str, value: str | None) -> None:
    shell = current_shell()
    finish_session([apply_line(shell, name, value)], [hint_line(shell, name, value)])


def session_value(store: Store, other: Store | None, name: str) -> tuple[bool, str | None]:
    """(should apply, value) for a variable the store just changed, taking the other scope into account."""
    if IS_WINDOWS and name.lower() == "path":
        print(style("PATH is the machine and user values joined together: open a new terminal to use the new one", "dim"))
        return False, None
    if store.scope == "machine" and other is not None and other.get(name) is not None:
        print(style(f"your user variable {name} takes priority, so terminals keep using that value", "dim"))
        return False, None
    value = store.get(name)
    if value is None and other is not None and store.scope == "user":
        value = other.get(name)  # the machine value shows through again
        store, other = other, store
    if value is not None and IS_WINDOWS and store.expands(name):
        value = expand(value)
    return True, value


def path_session(directory: str, front: bool, remove: bool) -> None:
    """Adds or removes the folder in the calling shell's own PATH.

    The shell edits its PATH itself, because this process's PATH isn't the shell's (uv run adds to it).
    """
    shell = current_shell()
    folder = expand(directory) if IS_WINDOWS else directory
    in_path = any(normalise(entry) == normalise(folder) for entry in os.environ.get("PATH", "").split(SEP) if entry)
    if not remove and in_path and not os.environ.get("KIT_ENV_APPLY"):
        return
    if shell == "powershell":
        quoted = ps_quote(folder)
        if remove:
            line = f"$env:Path = (($env:Path -split ';') | Where-Object {{ $_ -and $_.TrimEnd('\\') -ne {quoted} }}) -join ';'"
            finish_session([line], [line])
            return
        if front:
            add = f"$env:Path = {ps_quote(folder + ';')} + $env:Path"
        else:
            add = f"$env:Path = $env:Path.TrimEnd(';') + {ps_quote(';' + folder)}"
        line = f"if (-not (($env:Path -split ';') | Where-Object {{ $_.TrimEnd('\\') -eq {quoted} }})) {{ {add} }}"
        finish_session([line], [add])
        return
    quoted = shlex.quote(folder)
    if remove:
        line = (f'__kit_p=":$PATH:"; __kit_d={quoted}; '
                'while case $__kit_p in *":$__kit_d:"*) true ;; *) false ;; esac; do __kit_p=${__kit_p/":$__kit_d:"/:}; done; '
                '__kit_p=${__kit_p#:}; export PATH="${__kit_p%:}"; unset __kit_p __kit_d')
        finish_session([line], [line])
        return
    add = f'export PATH={quoted}"${{PATH:+:$PATH}}"' if front else f'export PATH="${{PATH:+$PATH:}}"{quoted}'
    finish_session([f'case ":$PATH:" in *:{quoted}:*) ;; *) {add} ;; esac'], [add])


# --- commands ------------------------------------------------------------------------------------

def print_table(title: str, subtitle: str, rows: list[Var], args: argparse.Namespace) -> None:
    print(f"{style(title, 'bold')}  {style(subtitle, 'dim')}")
    if not rows:
        print(style("  (none)", "dim"))
        print()
        return
    columns = shutil.get_terminal_size((100, 20)).columns
    width = max(len(r.name) for r in rows) if args.full else min(max(len(r.name) for r in rows), 30)
    for row in rows:
        name = row.name if len(row.name) <= width else row.name[:width - 3] + "..."
        value = masked(row.name, row.value, args.show_secrets).replace("\r", "").replace("\n", "\\n")
        room = max(columns - width - 5 - (len(row.note) + 2 if row.note else 0), 20)
        if not args.full and len(value) > room:
            value = value[:room - 3] + "..."
        if value == "****":
            value = style(value, "dim")
        note = f"  {style(row.note, 'dim')}" if row.note else ""
        print(f"  {style(name.ljust(width), 'cyan')}  {value}{note}")
    print()


def cmd_list(args: argparse.Namespace) -> int:
    needle = (args.filter or "").lower()
    if args.current:
        rows = [Var("process", k, v) for k, v in sorted(os.environ.items(), key=lambda kv: kv[0].lower())
                if needle in k.lower()]
        print_table("THIS TERMINAL", f"{len(rows)} variable{'' if len(rows) == 1 else 's'}", rows, args)
        return 0
    for machine in (False, True):
        try:
            store = open_store(args, machine)
        except Unsupported:
            continue
        rows = [row for row in store.variables() if needle in row.name.lower()]
        if isinstance(store, RcStore):
            subtitle = f"the kit env block in {store.label}"
        elif machine and not IS_WINDOWS:
            subtitle = f"{store.label} - read at login"
        else:
            read_only = isinstance(store, RegistryStore) and store.machine_hive and not is_admin()
            subtitle = store.label + (" - changing it needs admin rights" if read_only else "")
        print_table(f"{store.scope.upper()}", subtitle, rows, args)
    if not IS_WINDOWS and not needle:
        print(style("Only variables set through kit env and /etc/environment are listed; "
                    "see everything this terminal has with: kit env --current", "dim"))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    scopes = [True] if args.machine else [False] if args.user else [False, True]
    for machine in scopes:
        try:
            value = open_store(args, machine).get(args.name)
        except Unsupported:
            continue
        if value is not None:
            print(value)
            return 0
    return 1


def cmd_set(args: argparse.Namespace) -> int:
    check_name(args.name)
    if not IS_WINDOWS and not args.machine and args.name == "PATH":
        die("setting PATH here would replace it entirely - add a folder with: kit env path add <folder>")
    store = open_store(args, args.machine)
    old = store.get(args.name)
    if old == args.value:
        print(f"{args.name} already has that value in the {scope_text(store)}")
        return 0
    store.check_writable()
    before = store.flat()
    store.set(args.name, args.value)
    print(f"{style('Set ' + args.name, 'bold')} in the {scope_text(store)}:")
    print("\n".join(preview(before, store.flat())))
    if store.expands(args.name):
        print(style("      (%VARIABLES% in it are expanded when it's used)", "dim"))
    if not confirm("Apply?", args.yes):
        print("nothing changed")
        return 1
    save_store(store, home_dir(args))
    apply, value = session_value(store, other_store(args, store), args.name)
    if apply:
        variable_session(args.name, value)
    return 0


def cmd_unset(args: argparse.Namespace) -> int:
    check_name(args.name)
    store = open_store(args, args.machine)
    if store.get(args.name) is None:
        print(f"{args.name} isn't set in the {scope_text(store)}")
        other = other_store(args, store)
        if other is not None and other.get(args.name) is not None:
            flag = "--machine" if other.scope == "machine" else "without --machine"
            print(style(f"it is set in the {other.scope} environment - run the command {flag}", "dim"))
        return 1
    store.check_writable()
    before = store.flat()
    store.unset(args.name)
    print(f"{style('Remove ' + args.name, 'bold')} from the {scope_text(store)}:")
    print("\n".join(preview(before, store.flat())))
    if not confirm("Apply?", args.yes):
        print("nothing changed")
        return 1
    save_store(store, home_dir(args))
    apply, value = session_value(store, other_store(args, store), args.name)
    if apply:
        variable_session(args.name, value)
    return 0


def find_path_sources(folder: str, home: Path) -> list[str]:
    """Startup files that mention the folder, to explain where a PATH entry comes from."""
    candidates = [home / name for name in (".profile", ".bash_profile", ".bash_login", ".bashrc", ".zshenv", ".zprofile", ".zshrc")]
    candidates += [Path("/etc/environment"), Path("/etc/profile"), Path("/etc/bash.bashrc"), Path("/etc/zsh/zshenv")]
    profile_d = Path("/etc/profile.d")
    if profile_d.is_dir():
        candidates += sorted(profile_d.glob("*.sh"))
    variants = {folder, folder.replace(str(home), "$HOME"), folder.replace(str(home), "${HOME}"), folder.replace(str(home), "~")}
    # the whole path only: /usr/bin shouldn't match /usr/bin/lesspipe
    pattern = re.compile("|".join(rf"(?<![\w./~-]){re.escape(v)}/?(?![\w./-])" for v in variants))
    found = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            found.append(str(path))
    return found


def list_path(args: argparse.Namespace) -> int:
    if not IS_WINDOWS and not args.machine:
        store = RcStore(home_dir(args))
        ours = {normalise(folder) for folder in store.path_entries()}
        entries = [entry for entry in os.environ.get("PATH", "").split(":") if entry]
        print(f"{style('PATH', 'bold')}  {style(f'this terminal - {len(entries)} entries', 'dim')}")
        shown = set()
        for index, entry in enumerate(entries, 1):
            key = normalise(entry)
            notes = []
            if key in ours:
                notes.append(style("kit env", "green"))
            if not os.path.isdir(entry):
                notes.append(style("missing", "red"))
            shown.add(key)
            print(f"  {index:>3}  {entry}" + (f"  {'  '.join(notes)}" if notes else ""))
        pending = [f for f in store.path_entries() if normalise(f) not in shown]
        if pending:
            print(style("\nadded by kit env, active in new terminals:", "dim"))
            for folder in pending:
                print(f"       {folder}")
        print(style("\nadd or remove folders with: kit env path add|remove <folder>", "dim"))
        return 0
    store = open_store(args, args.machine)
    entries = store.path_entries()
    print(f"{style(store.scope.upper() + ' PATH', 'bold')}  {style(f'{store.label} - {len(entries)} entries', 'dim')}")
    if not entries:
        print(style("  (empty)", "dim"))
    for index, entry in enumerate(entries, 1):
        expanded = expand(entry)
        notes = []
        if expanded != entry:
            notes.append(style(expanded, "dim"))
        if not os.path.isdir(expanded):
            notes.append(style("missing", "red"))
        print(f"  {index:>3}  {entry}" + (f"  {'  '.join(notes)}" if notes else ""))
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    if args.action is None:
        if args.dir:
            die("to change PATH say what to do: kit env path add|remove <folder>")
        return list_path(args)
    if not args.dir:
        die(f"which folder? kit env path {args.action} <folder>")
    directory = clean_dir(args.dir)
    store = open_store(args, args.machine)
    other = other_store(args, store)
    target = f"{store.scope} PATH ({store.label})"

    if args.action == "add":
        if not os.path.isdir(expand(directory)):
            warn(f"{directory} doesn't exist (yet)")
        before = store.path_entries()
        if not store.path_add(directory, args.front):
            print(f"{directory} is already in the {target}")
            return 0
        store.check_writable()
        where = "front" if args.front else "end"
        print(f"{style('Add', 'bold')} {directory} to the {where} of the {target}")
        if isinstance(store, RcStore) and any(normalise(f) == normalise(directory) for f in before):
            print(style("  (it was already there, at the other end)", "dim"))
        if not confirm("Apply?", args.yes):
            print("nothing changed")
            return 1
        save_store(store, home_dir(args))
        path_session(directory, args.front, remove=False)
        return 0

    count = store.path_remove(directory)
    if not count:
        print(f"{directory} isn't in the {target}")
        if isinstance(store, RcStore):
            key = normalise(directory)
            if any(normalise(e) == key for e in os.environ.get("PATH", "").split(":") if e):
                sources = find_path_sources(directory, home_dir(args))
                print("It is in this terminal's PATH, but kit env didn't add it.")
                if sources:
                    print("It's mentioned in: " + ", ".join(sources))
                else:
                    print("It comes from a system startup file or the program that started this shell.")
        if other is not None and any(normalise(e) == normalise(directory) for e in other.path_entries()):
            flag = "--machine" if other.scope == "machine" else "without --machine"
            print(style(f"it is in the {other.scope} PATH - run the command {flag}", "dim"))
        return 1
    store.check_writable()
    print(f"{style('Remove', 'bold')} {directory} from the {target}" + (f" ({count} entries)" if count > 1 else ""))
    if not confirm("Apply?", args.yes):
        print("nothing changed")
        return 1
    save_store(store, home_dir(args))
    still_set = other is not None and any(normalise(e) == normalise(directory) for e in other.path_entries())
    if isinstance(store, EtcStore) or (not IS_WINDOWS and store.scope == "user"):
        still_set = still_set or bool(find_path_sources(directory, home_dir(args)))
    if not still_set:
        path_session(directory, False, remove=True)
    return 0


def backup_files(home: Path) -> list[Path]:
    folder = backup_dir(home)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("env-")]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def cmd_backups(args: argparse.Namespace) -> int:
    home = home_dir(args)
    files = backup_files(home)
    print(f"{style('BACKUPS', 'bold')}  {style(str(backup_dir(home)), 'dim')}")
    if not files:
        print(style("  none yet - kit env makes one before every change", "dim"))
        return 0
    for index, path in enumerate(files, 1):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
        print(f"  {index:>3}  {when}  {path.name}")
    print(style("\nrestore one with: kit env restore [name]   (newest by default)", "dim"))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    home = home_dir(args)
    if args.backup:
        path = Path(args.backup).expanduser()
        if not path.is_file():
            path = backup_dir(home) / args.backup
        if not path.is_file():
            die(f"backup not found: {args.backup} (see: kit env backups)")
    else:
        files = backup_files(home)
        if not files:
            die("there are no kit env backups yet")
        path = files[0]
    text = path.read_text(encoding="utf-8")
    name = path.name

    if name.endswith(".json"):
        if not IS_WINDOWS:
            die(f"{name} is a Windows registry backup")
        try:
            data = json.loads(text)
            scope, values = data["scope"], data["values"]
        except (json.JSONDecodeError, KeyError, TypeError):
            die(f"{path} isn't a kit env backup")
        store = open_store(args, scope == "machine")
        before = store.flat()
        store.load_values(values)
    elif name.startswith("env-etc-environment"):
        if IS_WINDOWS:
            die(f"{name} is a Linux backup")
        store = open_store(args, True)
        before = store.flat()
        store.load_text(text)
    elif name.startswith(("env-bashrc", "env-zshrc")):
        if IS_WINDOWS:
            die(f"{name} is a Linux backup")
        store = RcStore(home)
        before = store.flat()
        store.load_text(text)
    else:
        die(f"{path} isn't a kit env backup")

    changes = preview(before, store.flat(), args.show_secrets)
    if not store.changed() or not changes:
        print(f"the {scope_text(store)} already matches {name}")
        return 0
    store.check_writable()
    print(f"{style('Restore', 'bold')} the {scope_text(store)} from {name}:")
    print("\n".join(changes))
    if not confirm("Apply?", args.yes):
        print("nothing changed")
        return 1
    save_store(store, home)
    print("open a new terminal to use the restored values")
    return 0


# --- main ------------------------------------------------------------------------------------------

def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    # testing hooks: a throwaway registry key, a fake home folder or a stand-in for /etc/environment
    common.add_argument("--registry-key", default=USER_ENV_KEY, help=argparse.SUPPRESS)
    common.add_argument("--machine-registry-key", help=argparse.SUPPRESS)
    common.add_argument("--home", help=argparse.SUPPRESS)
    common.add_argument("--etc-environment", help=argparse.SUPPRESS)
    changes = argparse.ArgumentParser(add_help=False)
    changes.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    machine_help = "the machine-wide variables (Windows: needs admin; Linux: /etc/environment through sudo)"
    parser = argparse.ArgumentParser(prog="kit env", description="See and permanently change environment variables.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("list", parents=[common], help="list saved variables (the default)")
    p.add_argument("filter", nargs="?", help="only names containing this text")
    p.add_argument("--current", action="store_true", help="list this terminal's variables instead")
    p.add_argument("--show-secrets", action="store_true", help="show values of names like *TOKEN* or *KEY*")
    p.add_argument("--full", action="store_true", help="don't shorten long values")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", parents=[common], help="print one variable's saved value")
    p.add_argument("name")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--machine", action="store_true", help="only look at the machine variables")
    scope.add_argument("--user", action="store_true", help="only look at your user variables")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", parents=[common, changes], help="set a variable permanently")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--machine", action="store_true", help=machine_help)
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("unset", parents=[common, changes], help="remove a variable permanently")
    p.add_argument("name")
    p.add_argument("--machine", action="store_true", help=machine_help)
    p.set_defaults(func=cmd_unset)

    p = sub.add_parser("path", parents=[common, changes], help="list PATH, or add/remove a folder")
    p.add_argument("action", nargs="?", choices=["add", "remove"])
    p.add_argument("dir", nargs="?", metavar="FOLDER")
    p.add_argument("--front", action="store_true", help="add it at the front, so it wins over other folders")
    p.add_argument("--machine", action="store_true", help=machine_help)
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("backups", parents=[common], help="list the backups taken before each change")
    p.set_defaults(func=cmd_backups)

    p = sub.add_parser("restore", parents=[common, changes], help="put a backup back (newest by default)")
    p.add_argument("backup", nargs="?", help="backup file name or path (see: kit env backups)")
    p.add_argument("--show-secrets", action="store_true", help="show secret-looking values in the preview")
    p.set_defaults(func=cmd_restore)

    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "list")
    args = parser.parse_args(argv)
    if IS_WINDOWS and args.etc_environment:
        die("--etc-environment is for Linux")
    try:
        return args.func(args)
    except Unsupported as exc:
        die(str(exc))
    except PermissionError as exc:
        die(f"permission denied: {exc.filename or exc} - this needs admin/root rights")


if __name__ == "__main__":
    raise SystemExit(main())
