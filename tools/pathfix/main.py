"""Find and clean up duplicate, missing and redundant folders in PATH."""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from kitlib import die, style, warn

IS_WINDOWS = os.name == "nt"
USER_ENV_KEY = "Environment"
MACHINE_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
BEGIN_MARKER = "# >>> kit pathfix >>>"
END_MARKER = "# <<< kit pathfix <<<"

STATUS_STYLES = {
    "ok": "green",
    "empty": "yellow",
    "duplicate": "yellow",
    "redundant": "yellow",
    "missing": "red",
}


@dataclass
class Row:
    scope: str  # "machine", "user" (Windows) or "path" (Linux/macOS)
    index: int  # 1-based position within its scope
    raw: str
    status: str  # "ok", "empty", "missing", "duplicate" or "redundant"
    detail: str = ""


# --- analysis ------------------------------------------------------------------------

def expand(entry: str) -> str:
    return os.path.expandvars(os.path.expanduser(entry.strip().strip('"')))


def normalise(entry: str, fold_case: bool) -> str:
    text = os.path.normpath(expand(entry))
    if fold_case:
        text = text.lower()
    return text.rstrip("\\/") or text


def analyse(scopes: list[tuple[str, list[str]]], fold_case: bool = IS_WINDOWS) -> list[Row]:
    """Classifies every entry. Scopes are given in the order the OS searches them."""
    seen: dict[str, tuple[str, int]] = {}
    rows: list[Row] = []
    for scope, entries in scopes:
        for index, raw in enumerate(entries, 1):
            if not raw.strip():
                rows.append(Row(scope, index, raw, "empty", "empty entry"))
                continue
            key = normalise(raw, fold_case)
            if key in seen:
                first_scope, first_index = seen[key]
                if first_scope == scope:
                    rows.append(Row(scope, index, raw, "duplicate", f"same as #{first_index}"))
                else:
                    rows.append(Row(scope, index, raw, "redundant", f"already in {first_scope} PATH #{first_index}"))
                continue
            seen[key] = (scope, index)
            expanded = expand(raw)
            if not os.path.isdir(expanded):
                rows.append(Row(scope, index, raw, "missing", "folder not found"))
            else:
                rows.append(Row(scope, index, raw, "ok", expanded if expanded != raw.strip() else ""))
    return rows


def cleaned(entries: list[str], rows: list[Row], scope: str, keep_missing: bool) -> list[str]:
    drop = {
        r.index for r in rows
        if r.scope == scope and r.status != "ok" and not (keep_missing and r.status == "missing")
    }
    return [entry for index, entry in enumerate(entries, 1) if index not in drop]


def print_scope(title: str, subtitle: str, rows: list[Row], issues_only: bool) -> None:
    print(f"{style(title, 'bold')}  {style(subtitle, 'dim')}")
    shown = [r for r in rows if not issues_only or r.status != "ok"]
    if not rows:
        print(style("  (empty)", "dim"))
    elif not shown:
        print(style("  no problems", "green"))
    else:
        width = min(max(len(r.raw) for r in shown), 70)
        for row in shown:
            text = row.raw if row.raw.strip() else "(empty)"
            if len(text) > width:
                text = "..." + text[-(width - 3):]
            detail = f"  {style(row.detail, 'dim')}" if row.detail else ""
            label = style(row.status.ljust(9) if detail else row.status, STATUS_STYLES[row.status])
            print(f"  {row.index:>3}  {text.ljust(width)}  {label}{detail}")
    print()


def summarise(rows: list[Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.status != "ok":
            counts[row.status] = counts.get(row.status, 0) + 1
    return counts


def describe(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {status}" for status, n in counts.items()) or "no problems"


# --- shared helpers ----------------------------------------------------------------------

def backup_dir(home: Path) -> Path:
    if os.environ.get("KIT_BACKUP_DIR"):
        return Path(os.environ["KIT_BACKUP_DIR"])
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return base / "kit" / "backups"


def save_backup(name: str, content: str, home: Path) -> Path:
    stem = f"{name}.{time.strftime('%Y%m%d-%H%M%S')}"
    target, counter = backup_dir(home) / f"{stem}.bak", 1
    while target.exists():  # several changes within one second
        target, counter = backup_dir(home) / f"{stem}-{counter}.bak", counter + 1
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


# --- Windows: registry PATH -------------------------------------------------------------

def read_registry_path(machine: bool, subkey: str) -> str:
    import winreg

    root = winreg.HKEY_LOCAL_MACHINE if machine else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, "Path")  # REG_EXPAND_SZ comes back unexpanded
            return str(value)
    except FileNotFoundError:
        return ""


def write_user_path(value: str, subkey: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)


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


def windows_main(args: argparse.Namespace) -> int:
    home = Path.home()
    subkey = args.registry_key

    if args.restore:
        backup = Path(args.restore)
        if not backup.is_file():
            die(f"backup not found: {backup}")
        value = backup.read_text(encoding="utf-8").strip()
        print(f"{style('restore', 'bold')} user PATH from {backup}:")
        for entry in value.split(";"):
            print(style(f"  {entry}", "dim"))
        if not confirm("Replace your user PATH with this?", args.yes):
            print("nothing changed")
            return 1
        current = read_registry_path(False, subkey)
        saved = save_backup("user-path", current, home)
        write_user_path(value, subkey)
        if subkey == USER_ENV_KEY:
            broadcast_environment_change()
        print(f"{style('done', 'bold', 'green')} - previous value saved to {saved}. Open a new terminal to use it.")
        return 0

    if args.path_string is not None:
        user_raw = args.path_string
        machine_raw = args.machine_string or ""
    else:
        user_raw = read_registry_path(False, subkey)
        machine_raw = args.machine_string if args.machine_string is not None else read_registry_path(True, MACHINE_ENV_KEY)

    user_entries = user_raw.split(";") if user_raw else []
    machine_entries = machine_raw.split(";") if machine_raw else []
    # Windows builds PATH as machine entries followed by user entries.
    rows = analyse([("machine", machine_entries), ("user", user_entries)])
    user_rows = [r for r in rows if r.scope == "user"]
    machine_rows = [r for r in rows if r.scope == "machine"]

    print_scope("USER PATH", f"HKCU\\{subkey} - {len(user_entries)} entries", user_rows, args.issues_only)
    print_scope("MACHINE PATH", f"HKLM - read-only here - {len(machine_entries)} entries", machine_rows, args.issues_only)

    user_counts, machine_counts = summarise(user_rows), summarise(machine_rows)
    print(f"{style('user PATH:', 'bold')}    {describe(user_counts)}")
    print(f"{style('machine PATH:', 'bold')} {describe(machine_counts)}")
    if machine_counts:
        print(style("  The machine PATH needs admin rights to change: open 'Edit the system environment variables' >\n"
                    "  Environment Variables > System variables > Path, and remove the entries marked above.", "dim"))

    if not args.apply:
        if user_counts:
            print(f"\nclean the user PATH with:  kit pathfix --apply")
        return 0

    new_entries = cleaned(user_entries, rows, "user", args.keep_missing)
    new_value = ";".join(new_entries)
    if new_value == user_raw:
        print("\nuser PATH is already clean - nothing to change")
        return 0
    if args.path_string is not None:
        die("--apply works on the registry, not on --path-string")

    removed = [r for r in user_rows if r.status != "ok" and not (args.keep_missing and r.status == "missing")]
    print(f"\n{style('Will remove from the user PATH:', 'bold')}")
    for row in removed:
        print(f"  {row.raw or '(empty)'}  {style(row.status, STATUS_STYLES[row.status])}")
    if not confirm("Apply?", args.yes):
        print("nothing changed")
        return 1

    saved = save_backup("user-path", user_raw, home)
    write_user_path(new_value, subkey)
    if subkey == USER_ENV_KEY:
        broadcast_environment_change()
    print(f"{style('done', 'bold', 'green')} - user PATH now has {len(new_entries)} entries. Open a new terminal to use it.")
    print(style(f"backup: {saved}   (undo with: kit pathfix --restore \"{saved}\")", "dim"))
    return 0


# --- Linux / macOS: shell rc block ------------------------------------------------------

def block_text(keep_missing: bool) -> str:
    check = '[ -n "$__kit_dir" ] || continue' if keep_missing else '[ -n "$__kit_dir" ] && [ -d "$__kit_dir" ] || continue'
    return "\n".join([
        BEGIN_MARKER,
        "# Added by 'kit pathfix --apply'. At every shell start this rebuilds PATH without duplicate",
        "# entries" + ("" if keep_missing else " or folders that don't exist") + ", keeping the original order.",
        "# Remove this block with:  kit pathfix --undo",
        '__kit_path=""',
        '__kit_rest="$PATH:"',
        'while [ -n "$__kit_rest" ]; do',
        '    __kit_dir=${__kit_rest%%:*}',
        '    __kit_rest=${__kit_rest#*:}',
        '    case $__kit_dir in ?*/) __kit_dir=${__kit_dir%/} ;; esac',
        f"    {check}",
        '    case ":$__kit_path:" in',
        '        *":$__kit_dir:"*) ;;',
        '        *) __kit_path="${__kit_path:+$__kit_path:}$__kit_dir" ;;',
        '    esac',
        'done',
        '[ -n "$__kit_path" ] && export PATH="$__kit_path"',
        'unset __kit_path __kit_rest __kit_dir',
        END_MARKER,
    ])


def strip_block(text: str) -> str:
    kept, skipping = [], False
    for line in text.split("\n"):
        if line.strip() == BEGIN_MARKER:
            skipping = True
        elif skipping and line.strip() == END_MARKER:
            skipping = False
        elif not skipping:
            kept.append(line)
    return "\n".join(kept)


def rc_files(home: Path) -> list[Path]:
    files = [home / ".bashrc"]
    if (home / ".zshrc").is_file():
        files.append(home / ".zshrc")
    return files


def write_block(rc: Path, keep_missing: bool, home: Path) -> tuple[bool, Path | None]:
    """(changed, backup path). Replaces any existing kit pathfix block."""
    text = rc.read_text(encoding="utf-8") if rc.is_file() else ""
    body = strip_block(text).rstrip("\n")
    new = (body + "\n\n" if body else "") + block_text(keep_missing) + "\n"
    if new == text:
        return False, None
    backup = save_backup(rc.name.lstrip("."), text, home) if rc.is_file() else None
    rc.write_text(new, encoding="utf-8", newline="\n")
    return True, backup


def remove_block(rc: Path, home: Path) -> tuple[bool, Path | None]:
    if not rc.is_file():
        return False, None
    text = rc.read_text(encoding="utf-8")
    if BEGIN_MARKER not in text:
        return False, None
    body = strip_block(text).rstrip("\n")
    backup = save_backup(rc.name.lstrip("."), text, home)
    rc.write_text(body + "\n" if body else "", encoding="utf-8", newline="\n")
    return True, backup


def posix_main(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser() if args.home else Path.home()

    if args.undo:
        changed_any = False
        for rc in rc_files(home):
            changed, backup = remove_block(rc, home)
            if changed:
                changed_any = True
                print(f"{style('removed', 'bold', 'green')} kit pathfix block from {rc}" + style(f"  (backup: {backup})", "dim"))
        if not changed_any:
            print("no kit pathfix block found - nothing to undo")
        else:
            print("open a new terminal for it to take effect")
        return 0

    raw = args.path_string if args.path_string is not None else os.environ.get("PATH", "")
    entries = raw.split(":") if raw else []
    rows = analyse([("path", entries)], fold_case=False)
    print_scope("PATH", f"$PATH - {len(entries)} entries", rows, args.issues_only)
    counts = summarise(rows)
    print(f"{style('PATH:', 'bold')} {describe(counts)}")
    active = [rc for rc in rc_files(home) if rc.is_file() and BEGIN_MARKER in rc.read_text(encoding="utf-8")]
    if active:
        print(style(f"the kit pathfix block is active in: {', '.join(str(rc) for rc in active)}", "dim"))

    if not args.apply:
        if counts:
            print("\nclean it at every shell start with:  kit pathfix --apply")
        return 0

    targets = rc_files(home)
    removable = [r for r in rows if r.status != "ok" and not (args.keep_missing and r.status == "missing")]
    print(f"\n{style('Will add a kit pathfix block to:', 'bold')} {', '.join(str(t) for t in targets)}")
    if removable:
        print("It removes these from PATH at every shell start:")
        for row in removable:
            print(f"  {row.raw or '(empty)'}  {style(row.status, STATUS_STYLES[row.status])}")
    else:
        print("PATH is clean right now; the block keeps it that way.")
    if not confirm("Apply?", args.yes):
        print("nothing changed")
        return 1

    for rc in targets:
        changed, backup = write_block(rc, args.keep_missing, home)
        if changed:
            note = style(f"  (backup: {backup})", "dim") if backup else ""
            print(f"{style('updated', 'bold', 'green')} {rc}{note}")
        else:
            print(f"{rc} already up to date")
    print("open a new terminal (or run: source ~/.bashrc) for it to take effect; undo with: kit pathfix --undo")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit pathfix", description="Find and clean up duplicate, missing and redundant folders in PATH.")
    parser.add_argument("--apply", action="store_true",
                        help="fix the problems (Windows: rewrites your user PATH; Linux/macOS: adds a block to ~/.bashrc/~/.zshrc)")
    parser.add_argument("--keep-missing", action="store_true", help="don't remove folders that don't exist (e.g. unplugged drives)")
    parser.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("-q", "--issues-only", action="store_true", help="only list entries with problems")
    parser.add_argument("--undo", action="store_true", help="Linux/macOS: remove the kit pathfix block again")
    parser.add_argument("--restore", metavar="BACKUP", help="Windows: put a saved user PATH backup back")
    # testing hooks: analyse given strings / use a throwaway registry key or fake home
    parser.add_argument("--path-string", help=argparse.SUPPRESS)
    parser.add_argument("--machine-string", help=argparse.SUPPRESS)
    parser.add_argument("--registry-key", default=USER_ENV_KEY, help=argparse.SUPPRESS)
    parser.add_argument("--home", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if IS_WINDOWS:
        if args.undo:
            die("--undo is for Linux/macOS - on Windows use: kit pathfix --restore <backup file>")
        return windows_main(args)
    if args.restore:
        die("--restore is for Windows - on Linux/macOS use: kit pathfix --undo")
    return posix_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
