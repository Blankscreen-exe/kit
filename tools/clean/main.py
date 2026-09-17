"""Find and safely free disk space: temp files, the trash, package-manager caches and Docker leftovers."""

from __future__ import annotations

import argparse
import ctypes
import heapq
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from kitlib import die, error, style, warn
from kitlib.settings import tool_settings

SAFE = "safe"
REDOWNLOAD = "re-downloads"
CAREFUL = "careful"
REPORT = "report only"
SAFETY_STYLE = {SAFE: ("green",), REDOWNLOAD: ("cyan",), CAREFUL: ("yellow", "bold"), REPORT: ("dim",)}

ALL_PLATFORMS = ("windows", "linux", "macos")
IS_WINDOWS = sys.platform.startswith("win")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def current_platform() -> str:
    if IS_WINDOWS:
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def can_encode(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# --- files ---------------------------------------------------------------------

def is_link(entry: os.DirEntry) -> bool:
    """Symlinks, and on Windows junctions and other reparse points; never followed, so nothing outside is touched."""
    if entry.is_symlink():
        return True
    if not IS_WINDOWS:
        return False
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def walk(root: str | Path, deadline: float | None = None) -> Iterator[tuple[str, os.stat_result | None]]:
    """Yields (path, stat) for regular files and (path, None) for folders under root. Unreadable folders are skipped."""
    stack = [str(root)]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError
        folder = stack.pop()
        try:
            with os.scandir(folder) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        for entry in entries:
            try:
                if is_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    yield entry.path, None
                else:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        yield entry.path, info
            except OSError:
                continue


def walk_files(root: str | Path, deadline: float | None = None) -> Iterator[tuple[str, os.stat_result]]:
    for path, info in walk(root, deadline):
        if info is not None:
            yield path, info


def tree_usage(paths: list[Path]) -> tuple[int, int]:
    size = count = 0
    for root in paths:
        for _, info in walk_files(root):
            size += info.st_size
            count += 1
    return size, count


def remove_file(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except PermissionError:
        try:  # read-only files need their write bit first (common on Windows)
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
            return True
        except OSError:
            return False
    except OSError:
        return False


def inside(path: str, root: str) -> bool:
    """True when path is strictly below root."""
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return path.startswith(root.rstrip(os.sep) + os.sep)


def prune_empty_parents(root: Path, folders: set[str]) -> None:
    """Remove folders a clean left empty, walking up towards (never including) root."""
    for folder in sorted(folders, key=len, reverse=True):
        current = folder
        while inside(current, str(root)):
            try:
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)


def delete_files(files: list[tuple[str, int]], root: Path) -> tuple[int, int]:
    """Returns (bytes freed, files that couldn't be deleted, e.g. because they're in use)."""
    freed = failed = 0
    parents: set[str] = set()
    for path, size in files:
        if not inside(path, str(root)):
            failed += 1  # never delete outside the category's folder
        elif remove_file(path):
            freed += size
            parents.add(os.path.dirname(path))
        else:
            failed += 1
    prune_empty_parents(root, parents)
    return freed, failed


def empty_folder(root: Path) -> tuple[int, int]:
    """Delete everything inside root (but not root itself). Returns (bytes freed, files skipped)."""
    files, folders = [], []
    for path, info in walk(root):
        if info is None:
            folders.append(path)
        else:
            files.append((path, info.st_size))
    freed, failed = delete_files(files, root)
    for folder in sorted(folders, key=len, reverse=True):
        try:
            os.rmdir(folder)
        except OSError:
            pass
    return freed, failed


# --- commands ------------------------------------------------------------------

def query(*command: str, timeout: float = 60) -> str | None:
    """Output of a read-only command, or None when the tool is missing or fails."""
    exe = shutil.which(command[0])
    if not exe:
        return None
    try:
        result = subprocess.run([exe, *command[1:]], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, cwd=Path.home(), **NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def run_command(command: list[str], timeout: float = 1800) -> tuple[bool, str]:
    exe = shutil.which(command[0])
    if not exe:
        return False, f"{command[0]} not found"
    try:
        result = subprocess.run([exe, *command[1:]], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, cwd=Path.home(), **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, str(exc)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return result.returncode == 0, output


# --- categories ------------------------------------------------------------------

@dataclass
class Context:
    platform: str
    older_than: float  # days
    test_root: Path | None = None  # hidden --test-root: every category uses <root>/<key>, commands only simulated
    _cache: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cached(self, key: str, compute: Callable[[], object]) -> object:
        with self._lock:
            if key not in self._cache:
                self._cache[key] = compute()
            return self._cache[key]

    def forget(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._cache.pop(key, None)


@dataclass
class Scan:
    size: int = 0
    items: int = 0
    location: str = ""
    note: str = ""
    available: bool = True  # False: the tool or folder doesn't exist here, so the category is hidden
    failed: bool = False  # couldn't be measured (e.g. Docker not running); not cleanable this time
    paths: list[Path] = field(default_factory=list)
    files: list[tuple[str, int, Path]] = field(default_factory=list)  # (path, size, root) for file-by-file cleans
    command: list[str] | None = None


@dataclass
class Result:
    freed: int = 0
    note: str = ""
    error: str = ""


@dataclass
class Category:
    key: str
    label: str
    safety: str
    scan: Callable[[Context, "Category"], Scan]
    clean: Callable[[Context, "Category", Scan], Result] | None = None
    platforms: tuple[str, ...] = ALL_PLATFORMS

    @property
    def default(self) -> bool:
        """Included in --all-safe."""
        return self.safety in (SAFE, REDOWNLOAD) and self.clean is not None


def locate(ctx: Context, key: str, finder: Callable[[], list[Path]]) -> list[Path]:
    if ctx.test_root is not None:
        fake = ctx.test_root / key
        return [fake] if fake.is_dir() else []
    try:
        found = finder()
    except Exception:
        return []
    unique: dict[str, Path] = {}
    for path in found:
        if path and path.is_dir():
            unique.setdefault(os.path.normcase(os.path.abspath(path)), path)
    return list(unique.values())


def scan_folders(ctx: Context, cat: Category, finder: Callable[[], list[Path]], note: str = "") -> Scan:
    paths = locate(ctx, cat.key, finder)
    if not paths:
        return Scan(available=False)
    size, items = tree_usage(paths)
    return Scan(size, items, location=", ".join(map(str, paths)), note=note, paths=paths)


def clean_folders(ctx: Context, cat: Category, scan: Scan) -> Result:
    freed = skipped = 0
    for path in scan.paths:
        done, failed = empty_folder(path)
        freed += done
        skipped += failed
    return Result(freed, f"{skipped} file(s) in use were skipped" if skipped else "")


def folder_category(key: str, label: str, safety: str, finder: Callable[[], list[Path]],
                    platforms: tuple[str, ...] = ALL_PLATFORMS, note: str = "") -> Category:
    return Category(key, label, safety, lambda ctx, cat: scan_folders(ctx, cat, finder, note), clean_folders, platforms)


def simulate(command: list[str]) -> None:
    print(style(f"    [test] would run: {' '.join(command)}", "dim"))


def tool_category(key: str, label: str, safety: str, tool: str, finder: Callable[[], list[Path]],
                  command: list[str], note: str = "") -> Category:
    """A cache measured by folder size and cleared with the tool's own command."""

    def scan(ctx: Context, cat: Category) -> Scan:
        if ctx.test_root is None and not shutil.which(tool):
            return Scan(available=False)
        result = scan_folders(ctx, cat, finder, note)
        result.command = command
        return result

    def clean(ctx: Context, cat: Category, before: Scan) -> Result:
        if ctx.test_root is not None:
            simulate(command)
            clean_folders(ctx, cat, before)  # simulate the command's effect on the fake folder
        else:
            ok, output = run_command(command)
            if not ok:
                return Result(error=(output.splitlines() or ["failed"])[-1])
        after = scan_folders(ctx, cat, finder)
        return Result(max(0, before.size - after.size))

    return Category(key, label, safety, scan, clean)


def home() -> Path:
    return Path.home()


def env_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def xdg_cache() -> Path:
    return env_path("XDG_CACHE_HOME", home() / ".cache")


def local_appdata() -> Path:
    return env_path("LOCALAPPDATA", home() / "AppData" / "Local")


def path_from(*command: str) -> list[Path]:
    output = query(*command)
    return [Path(output.splitlines()[-1].strip())] if output else []


# temp -----------------------------------------------------------------------------

def temp_roots(ctx: Context) -> list[Path]:
    if ctx.platform == "windows":
        return [Path(tempfile.gettempdir())]
    return [Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")]


def scan_temp(ctx: Context, cat: Category) -> Scan:
    roots = locate(ctx, cat.key, lambda: temp_roots(ctx))
    if not roots:
        return Scan(available=False)
    cutoff = time.time() - ctx.older_than * 86400
    uid = os.getuid() if hasattr(os, "getuid") and ctx.test_root is None else None
    files = []
    for root in roots:
        for path, info in walk_files(root):
            if info.st_mtime >= cutoff:
                continue
            if uid is not None and info.st_uid != uid:
                continue  # shared /tmp: only your own files
            files.append((path, info.st_size, root))
    return Scan(sum(size for _, size, _ in files), len(files), ", ".join(map(str, roots)),
                note=f"files not changed in {ctx.older_than:g} days", paths=roots, files=files)


def clean_temp(ctx: Context, cat: Category, scan: Scan) -> Result:
    freed = skipped = 0
    for root in scan.paths:
        done, failed = delete_files([(path, size) for path, size, owner in scan.files if owner == root], root)
        freed += done
        skipped += failed
    return Result(freed, f"{skipped} file(s) in use were skipped" if skipped else "")


# trash -------------------------------------------------------------------------------

TRASH_NOTE = "emptied items can't be restored afterwards"


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


def recycle_bin() -> tuple[int, int]:
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    result = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) & 0xFFFFFFFF
    if result != 0:
        raise OSError(f"SHQueryRecycleBinW failed ({result:#x})")
    return info.i64Size, info.i64NumItems


def unix_trash() -> list[Path]:
    if current_platform() == "macos":
        return [home() / ".Trash"]
    base = env_path("XDG_DATA_HOME", home() / ".local" / "share") / "Trash"
    return [base / "files", base / "info", base / "expunged"]


def scan_trash(ctx: Context, cat: Category) -> Scan:
    if ctx.test_root is None and ctx.platform == "windows":
        size, items = recycle_bin()
        return Scan(size, items, "Recycle Bin (all drives)", note=TRASH_NOTE)
    return scan_folders(ctx, cat, unix_trash, TRASH_NOTE)


def clean_trash(ctx: Context, cat: Category, scan: Scan) -> Result:
    if ctx.test_root is not None or ctx.platform != "windows":
        return clean_folders(ctx, cat, scan)
    flags = 0x1 | 0x2 | 0x4  # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags) & 0xFFFFFFFF
    if result not in (0, 0x8000FFFF):  # E_UNEXPECTED just means it was already empty
        return Result(error=f"SHEmptyRecycleBinW failed ({result:#x})")
    after, _ = recycle_bin()
    return Result(max(0, scan.size - after))


# docker ------------------------------------------------------------------------------

DOCKER_SIZE_RE = re.compile(r"([\d.]+)\s*([kKMGTP]?)B")
DOCKER_UNITS = {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}
DOCKER_CACHE_KEYS = ("docker-df", "docker-dangling", "docker-unused")


def docker_size(text: str) -> int:
    match = DOCKER_SIZE_RE.search(text or "")
    return int(float(match[1]) * DOCKER_UNITS[match[2]]) if match else 0


def docker_json_lines(*args: str) -> list[dict] | None:
    output = query("docker", *args, timeout=90)
    if output is None:
        return None
    rows = []
    for line in output.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def docker_df(ctx: Context) -> dict[str, dict] | None:
    def compute() -> dict[str, dict] | None:
        rows = docker_json_lines("system", "df", "--format", "{{json .}}")
        return None if rows is None else {row.get("Type", ""): row for row in rows}

    return ctx.cached("docker-df", compute)


def docker_category(key: str, label: str, safety: str, measure: Callable[[Context], tuple[int, int, str]],
                    command: list[str] | None, note: str) -> Category:
    """measure(ctx) -> (size, items, location); a category without a command is report only."""

    def scan(ctx: Context, cat: Category) -> Scan:
        if ctx.test_root is not None:
            result = scan_folders(ctx, cat, list, note)
            result.command = command
            return result
        if not shutil.which("docker"):
            return Scan(available=False)
        if docker_df(ctx) is None:
            return Scan(location="Docker", note="Docker isn't running - start it to measure this", failed=True)
        size, items, location = measure(ctx)
        return Scan(size, items, location, note=note, command=command)

    def clean(ctx: Context, cat: Category, before: Scan) -> Result:
        if ctx.test_root is not None:
            simulate(command)
            return clean_folders(ctx, cat, before)
        ok, output = run_command(command)
        if not ok:
            return Result(error=(output.splitlines() or ["failed"])[-1])
        ctx.forget(*DOCKER_CACHE_KEYS)
        size = measure(ctx)[0] if docker_df(ctx) is not None else before.size
        reported = re.search(r"Total reclaimed space:\s*(.+)", output)
        return Result(max(0, before.size - size), f"docker reports {reported[1].strip()} reclaimed" if reported else "")

    return Category(key, label, safety, scan, clean if command else None)


def df_row(ctx: Context, row_type: str) -> dict:
    return (docker_df(ctx) or {}).get(row_type, {})


def unique_images(images: list[dict]) -> list[dict]:
    """One entry per image ID (an image with several tags is listed once per tag)."""
    seen: dict[str, dict] = {}
    for image in images:
        seen.setdefault(image.get("ID", ""), image)
    return list(seen.values())


def measure_build_cache(ctx: Context) -> tuple[int, int, str]:
    row = df_row(ctx, "Build Cache")
    return docker_size(row.get("Reclaimable", "")), int(row.get("TotalCount") or 0), "Docker build cache"


def measure_dangling(ctx: Context) -> tuple[int, int, str]:
    def compute() -> list[dict]:
        return unique_images(docker_json_lines("images", "--no-trunc", "--filter", "dangling=true", "--format", "{{json .}}") or [])

    images = ctx.cached("docker-dangling", compute)
    return sum(docker_size(i.get("Size", "")) for i in images), len(images), "untagged <none> images"


def measure_unused_images(ctx: Context) -> tuple[int, int, str]:
    # `docker system df` counts shared layers oddly, so compare images with what containers actually use.
    def compute() -> list[dict]:
        images = unique_images(docker_json_lines("images", "--no-trunc", "--format", "{{json .}}") or [])
        container_ids = (query("docker", "ps", "-aq", timeout=60) or "").split()
        used = set((query("docker", "inspect", "--format", "{{.Image}}", *container_ids, timeout=90) or "").split()) if container_ids else set()
        return [image for image in images if image.get("ID") not in used]

    images = ctx.cached("docker-unused", compute)
    names = ", ".join(f"{i.get('Repository')}:{i.get('Tag')}" for i in images[:3]) + (" ..." if len(images) > 3 else "")
    location = f"images no container uses: {names}" if images else "images no container uses"
    return sum(docker_size(i.get("Size", "")) for i in images), len(images), location


def measure_volumes(ctx: Context) -> tuple[int, int, str]:
    row = df_row(ctx, "Local Volumes")
    return docker_size(row.get("Size", "")), int(row.get("TotalCount") or 0), f"Docker volumes ({row.get('Reclaimable', '?')} not attached)"


# report-only system locations ----------------------------------------------------------

def scan_journal(ctx: Context, cat: Category) -> Scan:
    note = "shrink it with: sudo journalctl --vacuum-time=2weeks"
    if ctx.test_root is not None:
        return scan_folders(ctx, cat, list, note)
    output = query("journalctl", "--disk-usage")
    if output is None:
        return Scan(available=False)
    match = re.search(r"([\d.]+)\s*([BKMGT])", output)
    units = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    size = int(float(match[1]) * units[match[2]]) if match else 0
    return Scan(size, 0, "systemd journal (/var/log/journal)", note=note)


def scan_windows_temp(ctx: Context, cat: Category) -> Scan:
    root = env_path("SystemRoot", Path("C:/Windows")) / "Temp"
    note = "clear it from an admin terminal or with Disk Cleanup"
    if ctx.test_root is None:
        try:
            with os.scandir(root):
                pass
        except PermissionError:
            return Scan(location=str(root), note=f"needs admin to measure - {note}", failed=True)
        except OSError:
            return Scan(available=False)
    return scan_folders(ctx, cat, lambda: [root], note)


def bun_cache() -> list[Path]:
    # `bun pm cache` only works inside a project, so use bun's documented cache location.
    configured = os.environ.get("BUN_INSTALL_CACHE_DIR")
    if configured:
        return [Path(configured)]
    return [env_path("BUN_INSTALL", home() / ".bun") / "install" / "cache"]


def nuget_folders() -> list[Path]:
    output = query("dotnet", "nuget", "locals", "all", "--list") or ""
    return [Path(line.partition(": ")[2].strip()) for line in output.splitlines() if ": " in line]


CATEGORIES = [
    Category("temp", "Temporary files", SAFE, scan_temp, clean_temp),
    Category("trash", "Recycle Bin / Trash", SAFE, scan_trash, clean_trash),
    folder_category("crash-dumps", "Crash dumps from apps that crashed", SAFE,
                    lambda: [local_appdata() / "CrashDumps"], ("windows",)),
    Category("windows-temp", "Windows system temp folder", REPORT, scan_windows_temp, None, ("windows",)),
    folder_category("thumbnails", "Thumbnail cache", SAFE, lambda: [xdg_cache() / "thumbnails"], ("linux",)),
    Category("journal", "systemd journal logs", REPORT, scan_journal, None, ("linux",)),
    tool_category("npm", "npm package cache", REDOWNLOAD, "npm",
                  lambda: [p / "_cacache" for p in path_from("npm", "config", "get", "cache")],
                  ["npm", "cache", "clean", "--force"]),
    tool_category("pip", "pip download cache", REDOWNLOAD, "pip", lambda: path_from("pip", "cache", "dir"),
                  ["pip", "cache", "purge"]),
    tool_category("uv", "uv package cache", REDOWNLOAD, "uv", lambda: path_from("uv", "cache", "dir"),
                  ["uv", "cache", "clean", "--force"],
                  note="environments keep their own copies; packages download again when needed"),
    tool_category("yarn", "Yarn cache", REDOWNLOAD, "yarn", lambda: path_from("yarn", "cache", "dir"),
                  ["yarn", "cache", "clean"]),
    tool_category("pnpm", "pnpm store (unused packages)", REDOWNLOAD, "pnpm", lambda: path_from("pnpm", "store", "path"),
                  ["pnpm", "store", "prune"], note="prune only removes packages no project uses, so it frees less than the store size"),
    folder_category("bun", "Bun install cache", REDOWNLOAD, bun_cache),
    tool_category("go-build", "Go build cache", REDOWNLOAD, "go", lambda: path_from("go", "env", "GOCACHE"),
                  ["go", "clean", "-cache"]),
    tool_category("nuget", "NuGet / .NET package caches", REDOWNLOAD, "dotnet", nuget_folders,
                  ["dotnet", "nuget", "locals", "all", "--clear"]),
    docker_category("docker-build-cache", "Docker build cache", REDOWNLOAD, measure_build_cache,
                    ["docker", "builder", "prune", "-a", "-f"], "the next builds are slower while the cache refills"),
    docker_category("docker-dangling", "Untagged Docker images", SAFE, measure_dangling,
                    ["docker", "image", "prune", "-f"], "leftovers from rebuilt images"),
    docker_category("docker-unused-images", "Docker images no container uses", CAREFUL, measure_unused_images,
                    ["docker", "image", "prune", "-a", "-f"],
                    "pulled or rebuilt again when needed; sizes include shared layers, so this is an upper bound; never in --all-safe"),
    docker_category("docker-volumes", "Docker volumes", REPORT, measure_volumes, None,
                    "volumes hold databases and uploads - kit never deletes them"),
]


def categories_for(platform: str) -> list[Category]:
    return [c for c in CATEGORIES if platform in c.platforms]


def scan_all(ctx: Context, cats: list[Category]) -> list[tuple[Category, Scan]]:
    def one(cat: Category) -> tuple[Category, Scan]:
        try:
            return cat, cat.scan(ctx, cat)
        except Exception as exc:  # one broken category shouldn't hide the rest
            return cat, Scan(note=f"couldn't scan: {exc}", failed=True)

    spinner = sys.stderr.isatty()
    if spinner:
        print(style("scanning (large caches take a while)...", "dim"), end="\r", file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, cats))
    if spinner:
        print(" " * 45, end="\r", file=sys.stderr, flush=True)
    return [(cat, scan) for cat, scan in results if scan.available]


# --- output --------------------------------------------------------------------

def print_rows(rows: list[tuple[Category, Scan]], plan: bool = False) -> None:
    key_width = max(len(cat.key) for cat, _ in rows)
    print(style(f"  {'CATEGORY'.ljust(key_width)}  {'SIZE':>9}  {'ITEMS':>8}  {'SAFETY':<12}  WHAT", "dim"))
    for cat, scan in rows:
        size = "?" if scan.failed else human(scan.size)
        items = f"{scan.items:,}" if scan.items else "-"
        safety = style(cat.safety.ljust(12), *SAFETY_STYLE[cat.safety])
        print(f"  {style(cat.key.ljust(key_width), 'bold')}  {size:>9}  {items:>8}  {safety}  {cat.label}")
        indent = " " * (key_width + 36)
        details = []
        if plan and scan.command:
            details.append(f"runs: {' '.join(scan.command)}")
        elif plan and scan.files:
            details.append(f"deletes {len(scan.files):,} file(s) in {scan.location}")
        elif plan:
            details.append(f"empties {scan.location}")
        elif scan.location:
            details.append(scan.location)
        if scan.note:
            details.append(scan.note)
        for detail in details:
            print(style(f"  {indent}{detail}", "dim"))


def disk() -> tuple[str, shutil._ntuple_diskusage]:
    anchor = Path.home().anchor or "/"
    return anchor, shutil.disk_usage(anchor)


def confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


# --- commands ------------------------------------------------------------------

def cmd_scan(ctx: Context) -> int:
    rows = sorted(scan_all(ctx, categories_for(ctx.platform)), key=lambda r: (r[1].failed, -r[1].size))
    print(f"{style('kit clean', 'bold', 'cyan')}  {style('reclaimable space - nothing is deleted', 'dim')}")
    print()
    if not rows:
        print("  nothing found to clean")
        return 0
    print_rows(rows)

    measurable = [(c, s) for c, s in rows if not s.failed]
    safe_total = sum(s.size for c, s in measurable if c.default)
    careful_total = sum(s.size for c, s in measurable if c.safety == CAREFUL)
    report_total = sum(s.size for c, s in measurable if c.safety == REPORT)
    anchor, usage = disk()
    print()
    print(f"{style('Disk ' + anchor, 'bold')}  {human(usage.free)} free of {human(usage.total)} "
          f"({usage.used / usage.total * 100:.0f}% used)")
    print(f"  {style('kit clean run --all-safe', 'green')}  frees about {style(human(safe_total), 'bold')}"
          f"  ->  about {human(usage.free + safe_total)} free afterwards")
    if careful_total:
        print(f"  careful categories        up to {human(careful_total)} more, only when you name them")
    if report_total:
        print(f"  report only               {human(report_total)} kit won't delete (see the notes above)")
    print()
    print(style("clean:  kit clean run <category> ...   or   kit clean run --all-safe   (shows the plan and asks first)", "dim"))
    return 0


def cmd_run(ctx: Context, args: argparse.Namespace) -> int:
    cats = categories_for(ctx.platform)
    by_key = {cat.key: cat for cat in cats}
    unknown = [key for key in args.keys if key not in by_key]
    if unknown:
        die(f"unknown categor{'y' if len(unknown) == 1 else 'ies'}: {', '.join(unknown)} - choose from: {', '.join(by_key)}")
    if not args.keys and not args.all_safe:
        die("name the categories to clean, or use --all-safe (run 'kit clean' to see them)")

    chosen = [cat for cat in cats if cat.key in args.keys or (args.all_safe and cat.default)]
    for cat in [c for c in chosen if c.clean is None]:
        note = cat.scan(ctx, cat).note
        warn(f"{cat.key} is report only - kit doesn't delete it" + (f" ({note})" if note else ""))
    chosen = [c for c in chosen if c.clean is not None]

    results = {cat.key: scan for cat, scan in scan_all(ctx, chosen)}
    plan = []
    for cat in chosen:
        scan = results.get(cat.key)
        if scan is None:
            if cat.key in args.keys:
                warn(f"{cat.key}: not found on this computer, skipped")
        elif scan.failed:
            warn(f"{cat.key}: {scan.note}, skipped")
        elif scan.size == 0 and scan.items == 0:
            print(style(f"  {cat.key}: already empty", "dim"))
        else:
            plan.append((cat, scan))
    if not plan:
        print("nothing to clean")
        return 0

    total = sum(scan.size for _, scan in plan)
    print(f"{style('Plan', 'bold')}  frees about {style(human(total), 'bold')}")
    print_rows(plan, plan=True)
    for cat, _ in plan:
        if cat.safety == CAREFUL:
            warn(f"{cat.key} is marked careful: {cat.label.lower()} will be removed")
    print()
    if args.dry_run:
        print("dry run - nothing was deleted")
        return 0
    if not args.yes and not confirm(f"Clean {len(plan)} categor{'y' if len(plan) == 1 else 'ies'}?"):
        print("cancelled - nothing was deleted" + ("" if sys.stdin.isatty() else " (pass --yes to run without asking)"))
        return 1

    anchor, before = disk()
    freed_total = 0
    failures = 0
    for cat, scan in plan:
        try:
            result = cat.clean(ctx, cat, scan)
        except Exception as exc:
            result = Result(error=str(exc))
        if result.error:
            failures += 1
            print(f"  {style(cat.key, 'bold')}: {style('failed', 'red')} - {result.error}")
            continue
        freed_total += result.freed
        extra = style(f"  ({result.note})", "dim") if result.note else ""
        print(f"  {style(cat.key, 'bold')}: {style('freed ' + human(result.freed), 'green')}{extra}")
    _, after = disk()
    print()
    print(f"{style('Freed', 'bold')} {human(freed_total)}.  Disk {anchor}: {human(before.free)} free before, "
          f"{style(human(after.free), 'bold')} free now.")
    if failures:
        error(f"{failures} categor{'y' if failures == 1 else 'ies'} failed")
    return 1 if failures else 0


def cmd_big(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser() if args.path else Path.home()
    if not root.is_dir():
        die(f"not a folder: {root}")
    if args.top < 1:
        die("--top must be at least 1")
    deadline = time.monotonic() + args.limit
    started = time.monotonic()
    children: dict[str, int] = {}
    largest: list[tuple[int, str]] = []  # min-heap of the biggest files
    scanned = 0
    timed_out = False
    progress = sys.stderr.isatty()
    last_progress = 0.0

    def note_file(path: str, size: int) -> None:
        nonlocal scanned, last_progress
        scanned += 1
        if len(largest) < args.top:
            heapq.heappush(largest, (size, path))
        elif size > largest[0][0]:
            heapq.heapreplace(largest, (size, path))
        if progress and time.monotonic() - last_progress > 0.3:
            last_progress = time.monotonic()
            print(style(f"  scanned {scanned:,} files...", "dim"), end="\r", file=sys.stderr, flush=True)

    try:
        with os.scandir(root) as iterator:
            entries = list(iterator)
    except OSError as exc:
        die(f"can't read {root}: {exc}")

    for entry in entries:
        try:
            if is_link(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                total = 0
                try:
                    for path, info in walk_files(entry.path, deadline):
                        total += info.st_size
                        note_file(path, info.st_size)
                except TimeoutError:
                    timed_out = True
                children[entry.name + os.sep] = total
                if timed_out:
                    break
            else:
                info = entry.stat(follow_symlinks=False)
                children[entry.name] = info.st_size
                note_file(entry.path, info.st_size)
        except OSError:
            continue
    if progress:
        print(" " * 40, end="\r", file=sys.stderr, flush=True)

    full, empty = ("█", "░") if can_encode("█░") else ("#", ".")
    top_children = sorted(children.items(), key=lambda item: -item[1])[:args.top]
    biggest = top_children[0][1] if top_children else 0
    elapsed = time.monotonic() - started
    print(f"{style('Largest in', 'bold')} {root}  {style(f'({scanned:,} files in {elapsed:.1f}s)', 'dim')}")
    if timed_out:
        warn(f"stopped after {args.limit:g}s, so the results are partial - use --limit to scan longer")
    print()
    print(style("  FOLDERS AND FILES", "bold"))
    for name, size in top_children:
        filled = round(size / biggest * 20) if biggest else 0
        bar = style(full * filled, "cyan") + style(empty * (20 - filled), "dim")
        print(f"  {human(size):>9}  {bar}  {name}")
    print()
    print(style("  LARGEST FILES", "bold"))
    for size, path in sorted(largest, reverse=True):
        try:
            shown = os.path.relpath(path, root)
        except ValueError:
            shown = path
        print(f"  {human(size):>9}  {shown}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--older-than", type=float, metavar="DAYS", default=argparse.SUPPRESS,
                        help="temp files must be at least this old (default: 2)")
    common.add_argument("--test-root", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--platform", choices=ALL_PLATFORMS, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="kit clean", parents=[common],
                                     description="Find and safely free disk space. Without a command it only scans.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("scan", parents=[common], help="report reclaimable space (the default; deletes nothing)")
    run = commands.add_parser("run", parents=[common], help="clean the named categories after showing the plan")
    run.add_argument("keys", nargs="*", metavar="CATEGORY", help="categories from 'kit clean', e.g. temp npm")
    run.add_argument("--all-safe", action="store_true", help="every category marked safe or re-downloads")
    run.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    run.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    big = commands.add_parser("big", help="largest folders and files under a path")
    big.add_argument("path", nargs="?", help="folder to look in (default: your home folder)")
    big.add_argument("--top", type=int, default=15, help="how many to show (default: 15)")
    big.add_argument("--limit", type=float, default=60, metavar="SECONDS", help="stop scanning after this long (default: 60)")
    args = parser.parse_args()

    if args.command == "big":
        return cmd_big(args)

    older_than = getattr(args, "older_than", tool_settings().get("older_than", 2.0))
    if older_than < 0:
        die("--older-than can't be negative")
    test_root = getattr(args, "test_root", None)
    if test_root is not None and not test_root.is_dir():
        die(f"--test-root folder not found: {test_root}")
    ctx = Context(platform=getattr(args, "platform", None) or current_platform(), older_than=older_than,
                  test_root=test_root.resolve() if test_root else None)
    if args.command == "run":
        return cmd_run(ctx, args)
    return cmd_scan(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
