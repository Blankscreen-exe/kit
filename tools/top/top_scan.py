"""Collects what kit top shows: processes, their connections, signatures, hashes and autostart entries."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import psutil

from top_rules import (AutoFacts, Flag, Places, ProcFacts, assess_autostart, assess_process, command_target,
                       level_for, total_score)

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
DENIED = object()
CPU_WINDOW = 30.0  # seconds of CPU history behind "sustained" use
CPU_MIN_COVERAGE = 20.0


# --- trust list -----------------------------------------------------------------------

def data_path() -> Path:
    override = os.environ.get("KIT_TOP_DATA")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "top-trusted.json"


def path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


class TrustStore:
    """Programs the user marked as OK: a path plus the SHA-256 it had then. A changed file is no longer trusted."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self.version = 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data.get("trusted", []):
                if isinstance(entry, dict) and entry.get("path") and entry.get("sha256"):
                    self.entries[path_key(entry["path"])] = entry
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            try:
                path.replace(path.with_suffix(".json.bak"))
            except OSError:
                pass

    def get(self, path: str) -> dict | None:
        return self.entries.get(path_key(path))

    def add(self, path: str, sha256: str) -> None:
        self.entries[path_key(path)] = {"path": path, "sha256": sha256,
                                        "added": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.save()

    def remove(self, path: str) -> bool:
        removed = self.entries.pop(path_key(path), None) is not None
        if removed:
            self.save()
        return removed

    def save(self) -> None:
        self.version += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        body = {"trusted": sorted(self.entries.values(), key=lambda e: e["path"].lower())}
        temp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, self.path)


# --- files: stat, hash, signature ---------------------------------------------------------

@dataclass
class FileInfo:
    exists: bool
    size: int = 0
    modified: float | None = None
    created: float | None = None  # Windows/macOS birth time; ctime ("changed") on Linux
    hidden: bool = False

    @property
    def key(self) -> tuple:
        return (self.size, self.modified)


def file_info(path: str) -> FileInfo:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return FileInfo(False)
    created = getattr(st, "st_birthtime", None)
    if created is None:
        created = st.st_ctime if IS_WINDOWS else st.st_mtime
    hidden = bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN) if IS_WINDOWS else False
    return FileInfo(True, st.st_size, st.st_mtime, created, hidden)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Signature:
    status: str  # trusted | unsigned | invalid | untrusted | error
    signer: str = ""
    catalog: bool = False

    @property
    def label(self) -> str:
        text = {"trusted": "valid signature", "unsigned": "not signed", "invalid": "BROKEN signature",
                "untrusted": "signed, but not trusted", "error": "couldn't check"}.get(self.status, self.status)
        if self.signer:
            text += f" - {self.signer}"
        if self.catalog:
            text += " (Windows catalog)"
        return text


_wintrust = None


def _load_wintrust():
    """ctypes bindings for WinVerifyTrust and the catalog API, loaded once."""
    global _wintrust
    if _wintrust is not None:
        return _wintrust
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p)]

    class WINTRUST_CATALOG_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("dwCatalogVersion", wintypes.DWORD),
                    ("pcwszCatalogFilePath", wintypes.LPCWSTR), ("pcwszMemberTag", wintypes.LPCWSTR),
                    ("pcwszMemberFilePath", wintypes.LPCWSTR), ("hMemberFile", wintypes.HANDLE),
                    ("pbCalculatedFileHash", ctypes.POINTER(ctypes.c_ubyte)),
                    ("cbCalculatedFileHash", wintypes.DWORD), ("pcCatalogContext", ctypes.c_void_p),
                    ("hCatAdmin", wintypes.HANDLE)]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pInfo", ctypes.c_void_p), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    class CATALOG_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("wszCatalogFile", wintypes.WCHAR * 260)]

    class CRYPT_PROVIDER_CERT(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.c_void_p)]

    class CRYPT_PROVIDER_SGNR(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("sftVerifyAsOf", wintypes.FILETIME),
                    ("csCertChain", wintypes.DWORD), ("pasCertChain", ctypes.POINTER(CRYPT_PROVIDER_CERT))]

    wintrust = ctypes.WinDLL("wintrust")
    crypt32 = ctypes.WinDLL("crypt32")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(WINTRUST_DATA)]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(CRYPT_PROVIDER_SGNR)
    wintrust.CryptCATAdminAcquireContext2.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p,
                                                      wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD]
    wintrust.CryptCATAdminAcquireContext2.restype = wintypes.BOOL
    wintrust.CryptCATAdminCalcHashFromFileHandle2.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD]
    wintrust.CryptCATAdminCalcHashFromFileHandle2.restype = wintypes.BOOL
    wintrust.CryptCATAdminEnumCatalogFromHash.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                          wintypes.DWORD, ctypes.c_void_p]
    wintrust.CryptCATAdminEnumCatalogFromHash.restype = wintypes.HANDLE
    wintrust.CryptCATCatalogInfoFromContext.argtypes = [wintypes.HANDLE, ctypes.POINTER(CATALOG_INFO), wintypes.DWORD]
    wintrust.CryptCATCatalogInfoFromContext.restype = wintypes.BOOL
    wintrust.CryptCATAdminReleaseCatalogContext.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD]
    wintrust.CryptCATAdminReleaseContext.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    crypt32.CertGetNameStringW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                           wintypes.LPWSTR, wintypes.DWORD]
    crypt32.CertGetNameStringW.restype = wintypes.DWORD
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2 {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    _wintrust = {
        "ctypes": ctypes, "wintypes": wintypes, "wintrust": wintrust, "crypt32": crypt32, "kernel32": kernel32,
        "action": action, "WINTRUST_FILE_INFO": WINTRUST_FILE_INFO, "WINTRUST_CATALOG_INFO": WINTRUST_CATALOG_INFO,
        "WINTRUST_DATA": WINTRUST_DATA, "CATALOG_INFO": CATALOG_INFO,
    }
    return _wintrust


# HRESULTs from WinVerifyTrust
_UNSIGNED = {0x800B0100, 0x800B0003, 0x800B0001, 0x800B0002}  # no signature / unknown form / provider
_INVALID = {0x80096010, 0x800B0111, 0x80096019, 0x8009200E}   # bad digest / explicitly distrusted / bad signature
_OK_ISH = {0x800B0101}                                        # certificate expired (still a valid signature)


def _verify(api, choice: int, info) -> tuple[int, str]:
    """Runs WinVerifyTrust; returns (result, signer name)."""
    ctypes = api["ctypes"]
    data = api["WINTRUST_DATA"]()
    data.cbStruct = ctypes.sizeof(data)
    data.dwUIChoice = 2            # WTD_UI_NONE
    data.fdwRevocationChecks = 0   # WTD_REVOKE_NONE
    data.dwUnionChoice = choice    # 1 = file, 2 = catalog
    data.pInfo = ctypes.cast(ctypes.pointer(info), ctypes.c_void_p)
    data.dwStateAction = 1         # WTD_STATEACTION_VERIFY
    data.dwProvFlags = 0x1000 | 0x80  # cache-only URL retrieval, no revocation check over the network
    result = api["wintrust"].WinVerifyTrust(None, ctypes.byref(api["action"]), ctypes.byref(data)) & 0xFFFFFFFF
    signer = ""
    try:
        if data.hWVTStateData:
            provider = api["wintrust"].WTHelperProvDataFromStateData(data.hWVTStateData)
            if provider:
                sgnr = api["wintrust"].WTHelperGetProvSignerFromChain(provider, 0, False, 0)
                if sgnr and sgnr.contents.csCertChain and sgnr.contents.pasCertChain:
                    cert = sgnr.contents.pasCertChain[0].pCert
                    if cert:
                        buffer = ctypes.create_unicode_buffer(512)
                        api["crypt32"].CertGetNameStringW(cert, 4, 0, None, buffer, 512)  # SIMPLE_DISPLAY
                        signer = buffer.value
    finally:
        data.dwStateAction = 2  # WTD_STATEACTION_CLOSE
        api["wintrust"].WinVerifyTrust(None, ctypes.byref(api["action"]), ctypes.byref(data))
    return result, signer


def _classify(result: int) -> str:
    if result == 0 or result in _OK_ISH:
        return "trusted"
    if result in _UNSIGNED:
        return "unsigned"
    if result in _INVALID:
        return "invalid"
    return "untrusted"


def _catalog_check(api, path: str) -> Signature | None:
    """Most Windows system files are signed through a catalog instead of inside the file."""
    ctypes, wintypes, wintrust, kernel32 = api["ctypes"], api["wintypes"], api["wintrust"], api["kernel32"]
    handle = kernel32.CreateFileW(path, 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0, None)  # GENERIC_READ, OPEN_EXISTING
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        for algorithm in ("SHA256", None):
            admin = wintypes.HANDLE()
            if not wintrust.CryptCATAdminAcquireContext2(ctypes.byref(admin), None, algorithm, None, 0):
                continue
            try:
                size = wintypes.DWORD(0)
                wintrust.CryptCATAdminCalcHashFromFileHandle2(admin, handle, ctypes.byref(size), None, 0)
                if not size.value:
                    continue
                digest = (ctypes.c_ubyte * size.value)()
                if not wintrust.CryptCATAdminCalcHashFromFileHandle2(admin, handle, ctypes.byref(size), digest, 0):
                    continue
                catalog = wintrust.CryptCATAdminEnumCatalogFromHash(admin, digest, size.value, 0, None)
                if not catalog:
                    continue
                try:
                    info = api["CATALOG_INFO"]()
                    info.cbStruct = ctypes.sizeof(info)
                    if not wintrust.CryptCATCatalogInfoFromContext(catalog, ctypes.byref(info), 0):
                        continue
                    member = api["WINTRUST_CATALOG_INFO"]()
                    member.cbStruct = ctypes.sizeof(member)
                    member.pcwszCatalogFilePath = info.wszCatalogFile
                    member.pcwszMemberTag = "".join(f"{b:02X}" for b in digest)
                    member.pcwszMemberFilePath = path
                    member.hMemberFile = handle
                    member.pbCalculatedFileHash = ctypes.cast(digest, ctypes.POINTER(ctypes.c_ubyte))
                    member.cbCalculatedFileHash = size.value
                    member.hCatAdmin = admin
                    result, signer = _verify(api, 2, member)
                    return Signature(_classify(result), signer, catalog=True)
                finally:
                    wintrust.CryptCATAdminReleaseCatalogContext(admin, catalog, 0)
            finally:
                wintrust.CryptCATAdminReleaseContext(admin, 0)
    finally:
        kernel32.CloseHandle(handle)
    return None


def check_signature(path: str) -> Signature:
    """Authenticode status of a file (Windows). Never touches the network."""
    if not IS_WINDOWS:
        return Signature("error")
    try:
        api = _load_wintrust()
        info = api["WINTRUST_FILE_INFO"]()
        info.cbStruct = api["ctypes"].sizeof(info)
        info.pcwszFilePath = path
        result, signer = _verify(api, 1, info)
        status = _classify(result)
        if status == "unsigned":
            catalog = _catalog_check(api, path)
            if catalog is not None:
                return catalog
        return Signature(status, signer)
    except Exception:
        return Signature("error")


class FileCache:
    """Stat, SHA-256 and signature results, keyed by (path, size, mtime) so a changed file is checked again.

    Signatures are slow (tens of ms each), so `signature(path, wait=False)` queues the check on a background
    thread and returns None until it's done.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hashes: dict[tuple, str] = {}
        self._sigs: dict[tuple, Signature] = {}
        self._pending: set[tuple] = set()
        self._queue: deque[tuple[tuple, str]] = deque()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def key(path: str, info: FileInfo) -> tuple:
        return (path_key(path), info.size, info.modified)

    def sha256(self, path: str, info: FileInfo | None = None) -> str | None:
        info = info or file_info(path)
        if not info.exists:
            return None
        key = self.key(path, info)
        with self._lock:
            if key in self._hashes:
                return self._hashes[key]
        try:
            value = sha256_file(path)
        except OSError:
            return None
        with self._lock:
            self._hashes[key] = value
        return value

    def cached_sha256(self, path: str, info: FileInfo | None = None) -> str | None:
        info = info or file_info(path)
        with self._lock:
            return self._hashes.get(self.key(path, info)) if info.exists else None

    def signature(self, path: str, info: FileInfo | None = None, wait: bool = False) -> Signature | None:
        if not IS_WINDOWS or not path:
            return None
        info = info or file_info(path)
        if not info.exists:
            return None
        key = self.key(path, info)
        with self._lock:
            if key in self._sigs:
                return self._sigs[key]
            if not wait:
                if key not in self._pending:
                    self._pending.add(key)
                    self._queue.append((key, path))
                    self._wake.set()
                    self._start()
                return None
        result = check_signature(path)
        with self._lock:
            self._sigs[key] = result
        return result

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def _start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kit-top-signatures", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if not self._queue:
                    self._wake.clear()
                    continue
                key, path = self._queue.popleft()
            result = check_signature(path)
            with self._lock:
                self._sigs[key] = result
                self._pending.discard(key)


# --- processes -----------------------------------------------------------------------------

@dataclass
class Conn:
    kind: str  # tcp | udp
    local: str
    remote: str
    status: str
    remote_port: int | None = None
    remote_ip: str = ""


@dataclass
class ProcRow:
    pid: int
    name: str
    exe: str | None
    cmdline: list[str]
    username: str | None
    ppid: int | None
    started: float | None
    cpu: float
    memory: int
    conns: list[Conn]
    flags: list[Flag]
    score: int
    level: str
    signature: Signature | None
    file: FileInfo | None
    trusted: bool
    suspended: bool = False
    denied: bool = False

    @property
    def user_short(self) -> str:
        name = self.username or ""
        return name.split("\\")[-1]

    @property
    def why(self) -> str:
        return "; ".join(f.reason for f in self.flags)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid, "name": self.name, "exe": self.exe, "cmdline": self.cmdline, "user": self.username,
            "ppid": self.ppid,
            "started": datetime.fromtimestamp(self.started).astimezone().isoformat(timespec="seconds") if self.started else None,
            "cpu_percent": round(self.cpu, 1), "memory_bytes": self.memory, "level": self.level, "score": self.score,
            "trusted": self.trusted, "signature": self.signature.status if self.signature else None,
            "signer": self.signature.signer if self.signature else None,
            "connections": [{"kind": c.kind, "local": c.local, "remote": c.remote, "status": c.status} for c in self.conns],
            "flags": [f.to_dict() for f in self.flags],
        }


@dataclass
class Snapshot:
    rows: list[ProcRow]
    denied: int
    elevated: bool
    taken: float = field(default_factory=time.time)
    signatures_pending: int = 0

    def counts(self) -> dict[str, int]:
        counts = {"high": 0, "medium": 0, "low": 0, "ok": 0}
        for row in self.rows:
            counts[row.level] += 1
        return counts


def is_elevated() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def elevation_hint() -> str:
    if IS_WINDOWS:
        return "run from an administrator terminal to see them"
    return "run with sudo to see them"


def _addr(address) -> str:
    if not address:
        return ""
    ip, port = address.ip, address.port
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _system_account(username: str | None) -> bool:
    if not username:
        return False
    lower = username.lower()
    if IS_WINDOWS:
        return lower.endswith(("\\system", "\\local service", "\\network service")) or lower == "system"
    return lower == "root"


def _deleted_exe(pid: int) -> bool:
    try:
        return os.readlink(f"/proc/{pid}/exe").endswith(" (deleted)")
    except OSError:
        return False


class Engine:
    """Takes snapshots and assesses them. Keeps per-process caches between snapshots."""

    def __init__(self, trust: TrustStore, wait_for_signatures: bool = False) -> None:
        self.trust = trust
        self.files = FileCache()
        self.places = Places.detect()
        self.wait = wait_for_signatures
        self.elevated = is_elevated()
        self.cpu_count = psutil.cpu_count() or 1
        self._static: dict[tuple[int, float], dict] = {}
        self._cpu: dict[tuple[int, float], deque] = {}
        self._procs: dict[tuple[int, float], psutil.Process] = {}
        self.suspended: set[tuple[int, float]] = set()
        self._lock = threading.Lock()

    def _static_info(self, proc: psutil.Process, created: float) -> dict:
        key = (proc.pid, created)
        info = self._static.get(key)
        if info is not None:
            return info
        info = {}
        for attr in ("exe", "cmdline", "username"):
            try:
                info[attr] = getattr(proc, attr)()
            except psutil.AccessDenied:
                info[attr] = DENIED
            except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                info[attr] = None
        self._static[key] = info
        return info

    def _connections(self) -> dict[int, list[Conn]]:
        by_pid: dict[int, list[Conn]] = {}
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, OSError):
            return by_pid
        for c in conns:
            if not c.pid:
                continue
            kind = "tcp" if c.type == 1 else "udp"
            status = c.status if c.status and c.status != "NONE" else ""
            conn = Conn(kind, _addr(c.laddr), _addr(c.raddr), status,
                        c.raddr.port if c.raddr else None, c.raddr.ip if c.raddr else "")
            by_pid.setdefault(c.pid, []).append(conn)
        return by_pid

    def prime(self) -> None:
        """Start CPU measurement (the first cpu_percent reading of a process is always 0)."""
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(None)
            except (psutil.Error, OSError):
                pass

    def snapshot(self, signatures: bool = True) -> Snapshot:
        """`signatures=False` skips signature checks (a quick first pass before checking them in parallel)."""
        with self._lock:
            return self._snapshot(signatures)

    def _snapshot(self, signatures: bool) -> Snapshot:
        now = time.time()
        conns = self._connections()
        procs: list[tuple[psutil.Process, dict]] = []
        seen: set[tuple[int, float]] = set()
        for proc in psutil.process_iter(["pid", "name", "ppid", "create_time"]):
            info = proc.info
            created = info.get("create_time") or 0.0
            key = (proc.pid, created)
            seen.add(key)
            self._procs[key] = proc
            procs.append((proc, info))
        names = {proc.pid: (info.get("name") or "", info.get("create_time") or 0.0) for proc, info in procs}

        rows: list[ProcRow] = []
        denied = 0
        for proc, info in procs:
            pid = proc.pid
            created = info.get("create_time") or 0.0
            key = (pid, created)
            static = self._static_info(proc, created)
            exe_raw, cmd_raw, user_raw = static.get("exe"), static.get("cmdline"), static.get("username")
            is_denied = exe_raw is DENIED
            exe = exe_raw if isinstance(exe_raw, str) and exe_raw else None
            cmdline = cmd_raw if isinstance(cmd_raw, list) else []
            username = user_raw if isinstance(user_raw, str) else None
            if is_denied and pid not in (0, 4):
                denied += 1

            with proc.oneshot():  # one slow system call for both on Windows
                try:
                    cpu = proc.cpu_percent(None) / self.cpu_count
                except (psutil.Error, OSError):
                    cpu = 0.0
                try:
                    memory = proc.memory_info().rss
                except (psutil.Error, OSError):
                    memory = 0
            history = self._cpu.setdefault(key, deque())
            history.append((now, cpu))
            while history and now - history[0][0] > CPU_WINDOW:
                history.popleft()
            sustained = None
            if len(history) >= 3 and history[-1][0] - history[0][0] >= CPU_MIN_COVERAGE:
                sustained = sum(v for _, v in list(history)[1:]) / (len(history) - 1)

            fileinfo = file_info(exe) if exe else None
            exe_state = "ok"
            if exe and not IS_WINDOWS and _deleted_exe(pid):
                exe_state = "deleted"
            elif exe and os.path.isabs(exe) and fileinfo and not fileinfo.exists:  # "Registry" etc. are pseudo-processes
                exe_state = "missing" if IS_WINDOWS or IS_MAC else "deleted"
            signature = None
            if signatures and exe and fileinfo and fileinfo.exists:
                signature = self.files.signature(exe, fileinfo, wait=self.wait)

            parent_name = None
            ppid = info.get("ppid")
            if ppid in names and ppid != pid:
                pname, pcreated = names[ppid]
                if not created or not pcreated or pcreated <= created:
                    parent_name = pname

            my_conns = conns.get(pid, [])
            remotes = [(c.remote_ip, c.remote_port) for c in my_conns if c.remote_port]
            listening = any(c.status == "LISTEN" for c in my_conns)

            facts = ProcFacts(
                pid=pid, name=info.get("name") or "", exe=exe, cmdline=cmdline, username=username,
                parent_name=parent_name, exe_state=exe_state,
                hidden=bool(fileinfo and fileinfo.hidden),
                file_created=fileinfo.created if fileinfo and fileinfo.exists else None,
                signature=signature.status if signature and signature.status != "error" else None,
                signer=signature.signer if signature else "",
                remotes=remotes, listening=listening, cpu_sustained=sustained,
                elevated=_system_account(username),
                idle=pid == 0,
            )
            flags = assess_process(facts, self.places, now)
            score = total_score(flags)
            trusted = False
            if exe and flags and fileinfo and fileinfo.exists:
                entry = self.trust.get(exe)
                if entry:
                    trusted = self.files.sha256(exe, fileinfo) == entry["sha256"]
            level = "ok" if trusted else level_for(score)
            rows.append(ProcRow(
                pid=pid, name=facts.name, exe=exe, cmdline=cmdline, username=username, ppid=ppid,
                started=created or None, cpu=cpu, memory=memory, conns=my_conns, flags=flags,
                score=0 if trusted else score, level=level, signature=signature, file=fileinfo,
                trusted=trusted, suspended=key in self.suspended, denied=is_denied,
            ))

        for cache in (self._static, self._cpu, self._procs):
            for key in [k for k in cache if k not in seen]:
                del cache[key]
        self.suspended &= seen
        return Snapshot(rows, denied, self.elevated, now, self.files.pending())

    def process(self, row: ProcRow) -> psutil.Process:
        """The live process behind a row; raises NoSuchProcess if the PID now belongs to something else."""
        proc = self._procs.get((row.pid, row.started or 0.0)) or psutil.Process(row.pid)
        if row.started and abs(proc.create_time() - row.started) > 1:
            raise psutil.NoSuchProcess(row.pid)
        return proc

    def set_suspended(self, row: ProcRow, value: bool) -> None:
        key = (row.pid, row.started or 0.0)
        if value:
            self.suspended.add(key)
        else:
            self.suspended.discard(key)

    def parent_chain(self, row: ProcRow, rows: list[ProcRow]) -> list[tuple[int, str]]:
        by_pid = {r.pid: r for r in rows}
        chain: list[tuple[int, str]] = []
        current = row
        visited = {row.pid}
        while current.ppid and current.ppid in by_pid and current.ppid not in visited:
            parent = by_pid[current.ppid]
            if parent.started and current.started and parent.started > current.started:
                break
            chain.append((parent.pid, parent.name))
            visited.add(parent.pid)
            current = parent
        if current.ppid and current.ppid not in by_pid and current.ppid not in visited:
            chain.append((current.ppid, "(exited)"))
        return chain

    # --- autostart ---

    def autostart(self, entries: list[AutoFacts] | None = None) -> list[AutoRow]:
        entries = collect_autostart(self.places) if entries is None else entries
        rows: list[AutoRow] = []
        for entry in entries:
            if entry.target and not entry.target_state == "unknown":
                info = file_info(entry.target)
                if not info.exists:
                    entry.target_state = "missing"
                else:
                    entry.hidden = info.hidden
                    sig = self.files.signature(entry.target, info, wait=True) if IS_WINDOWS else None
                    if sig and sig.status != "error":
                        entry.signature, entry.signer = sig.status, sig.signer
            flags = assess_autostart(entry, self.places)
            score = total_score(flags)
            trusted = False
            if entry.target and flags:
                trust = self.trust.get(entry.target)
                if trust:
                    trusted = self.files.sha256(entry.target) == trust["sha256"]
            rows.append(AutoRow(entry, flags, 0 if trusted else score, "ok" if trusted else level_for(score), trusted))
        return rows

    def autostart_parallel(self) -> list[AutoRow]:
        """Like autostart(), but checks signatures on several threads first (for --scan)."""
        entries = collect_autostart(self.places)
        if IS_WINDOWS:
            self.presign({e.target for e in entries if e.target and os.path.isabs(e.target)})
        return self.autostart(entries)

    def presign(self, paths: set[str]) -> None:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda p: self.files.signature(p, wait=True), paths))


@dataclass
class AutoRow:
    entry: AutoFacts
    flags: list[Flag]
    score: int
    level: str
    trusted: bool

    @property
    def why(self) -> str:
        return "; ".join(f.reason for f in self.flags)

    def to_dict(self) -> dict:
        e = self.entry
        return {"kind": e.kind, "name": e.name, "command": e.command, "location": e.location, "target": e.target,
                "target_state": e.target_state, "signature": e.signature, "signer": e.signer or None,
                "disabled": e.disabled, "level": self.level, "score": self.score, "trusted": self.trusted,
                "flags": [f.to_dict() for f in self.flags]}


# --- autostart: Windows ---------------------------------------------------------------------------

RUN_KEYS = [
    r"Software\Microsoft\Windows\CurrentVersion\Run",
    r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce",
    r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run",
]


def _reg_values(root, subkey: str) -> list[tuple[str, object]]:
    import winreg

    values = []
    try:
        with winreg.OpenKey(root, subkey) as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                values.append((name, value))
                index += 1
    except OSError:
        pass
    return values


def _reg_value(root, subkey: str, name: str):
    import winreg

    try:
        with winreg.OpenKey(root, subkey) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little") if offset + 4 <= len(data) else 0


def _cstr(data: bytes, offset: int, wide: bool) -> str:
    if wide:
        end = offset
        while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
            end += 2
        return data[offset:end].decode("utf-16-le", "replace")
    end = data.find(b"\0", offset)
    return data[offset:end if end != -1 else len(data)].decode("mbcs" if IS_WINDOWS else "latin-1", "replace")


def read_lnk(path: Path) -> tuple[str, str]:
    """(target, arguments) of a Windows shortcut, parsed from the file itself ('' when it can't tell)."""
    try:
        data = path.read_bytes()
    except OSError:
        return "", ""
    if len(data) < 76 or _u32(data, 0) != 0x4C:
        return "", ""
    flags = _u32(data, 20)
    pos = 76
    if flags & 0x01:  # HasLinkTargetIDList
        pos += 2 + int.from_bytes(data[pos:pos + 2], "little")
    target = ""
    if flags & 0x02:  # HasLinkInfo
        info = data[pos:]
        size, header, info_flags = _u32(info, 0), _u32(info, 4), _u32(info, 8)
        if info_flags & 0x01:
            if header >= 0x24 and _u32(info, 28):
                target = _cstr(info, _u32(info, 28), True)
                suffix = _cstr(info, _u32(info, 32), True) if _u32(info, 32) else ""
            else:
                target = _cstr(info, _u32(info, 16), False)
                suffix = _cstr(info, _u32(info, 24), False) if _u32(info, 24) else ""
            target += suffix
        pos += size
    wide = bool(flags & 0x80)
    strings: dict[int, str] = {}
    for bit in (0x04, 0x08, 0x10, 0x20, 0x40):
        if flags & bit:
            count = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
            length = count * (2 if wide else 1)
            strings[bit] = data[pos:pos + length].decode("utf-16-le" if wide else "latin-1", "replace")
            pos += length
    if not target:
        marker = data.find(b"\x01\x00\x00\xa0", pos)  # EnvironmentVariableDataBlock
        if marker >= 4:
            block = data[marker + 4:marker + 4 + 260 + 520]
            target = _cstr(block, 260, True) or _cstr(block, 0, False)
    if not target and 0x08 in strings:
        target = os.path.normpath(os.path.join(str(path.parent), strings[0x08]))
    if IS_WINDOWS:
        target = os.path.expandvars(target)
    return target, strings.get(0x20, "")


def _schtasks() -> list[tuple[str, str, str, str]]:
    """(name, command, author, state) for every scheduled task this user can see."""
    try:
        result = subprocess.run(["schtasks", "/query", "/fo", "csv", "/v"], capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    text = result.stdout.decode("oem", "replace") if IS_WINDOWS else result.stdout.decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = rows[0]

    def column(name: str, fallback: int) -> int:
        return header.index(name) if name in header else fallback

    i_name, i_cmd = column("TaskName", 1), column("Task To Run", 8)
    i_author, i_state = column("Author", 7), column("Scheduled Task State", 11)
    tasks: dict[str, tuple[str, str, str, str]] = {}
    for row in rows[1:]:
        if not row or row == header or len(row) <= max(i_name, i_cmd):
            continue
        name = row[i_name]
        if name in tasks:
            continue
        get = lambda i: row[i] if i < len(row) else ""  # noqa: E731
        tasks[name] = (name, get(i_cmd), get(i_author), get(i_state))
    return list(tasks.values())


def _resolve_windows_program(target: str) -> str:
    if not target or os.path.isabs(target):
        return target
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    for folder in (os.path.join(windir, "System32"), windir):
        for candidate in (target, target + ".exe"):
            path = os.path.join(folder, candidate)
            if os.path.isfile(path):
                return path
    return target


def windows_autostart(places: Places) -> list[AutoFacts]:
    import winreg

    entries: list[AutoFacts] = []

    def add(kind: str, name: str, command: str, location: str, base: list[Flag] | None = None,
            disabled: bool = False, target: str | None = None) -> None:
        tgt = command_target(command, True) if target is None else target
        tgt = _resolve_windows_program(tgt)
        entries.append(AutoFacts(kind, name, command, location, tgt, base_flags=base or [], disabled=disabled,
                                 target_state="ok" if tgt and os.path.isabs(tgt) else "unknown"))

    for root, root_name in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for subkey in RUN_KEYS:
            for name, value in _reg_values(root, subkey):
                if isinstance(value, str) and value.strip():
                    kind = "RunOnce key" if subkey.endswith("RunOnce") else "Run key"
                    add(kind, name or "(default)", value, f"{root_name}\\{subkey}")

    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("ProgramData", r"C:\ProgramData")
    for folder in (Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup",
                   Path(programdata) / "Microsoft/Windows/Start Menu/Programs/StartUp"):
        if not folder.is_dir():
            continue
        for item in sorted(folder.iterdir()):
            if item.name.lower() == "desktop.ini" or item.is_dir():
                continue
            if item.suffix.lower() == ".lnk":
                target, args = read_lnk(item)
                command = f'"{target}" {args}'.strip() if target else str(item)
                add("Startup folder", item.name, command, str(folder), target=target or "")
            else:
                add("Startup folder", item.name, f'"{item}"', str(folder), target=str(item))

    for name, command, author, state in _schtasks():
        if not command or command.strip().lower() in ("com handler", "multiple actions", "n/a"):
            continue
        disabled = state.strip().lower() == "disabled"
        add("Scheduled task", name, command, f"Task Scheduler{' (disabled)' if disabled else ''}", disabled=disabled)

    windir = places.windir
    try:
        for service in psutil.win_service_iter():
            try:
                info = service.as_dict()
            except (psutil.Error, OSError):
                continue
            if info.get("start_type") != "automatic":
                continue
            binpath = info.get("binpath") or ""
            target = _resolve_windows_program(command_target(binpath, True))
            if not target or places.under(places.norm(target), windir):
                continue
            entries.append(AutoFacts("Service", f"{info.get('display_name') or info.get('name')} ({info.get('name')})",
                                     binpath, "Services (automatic start)", target,
                                     target_state="ok" if os.path.isabs(target) else "unknown"))
    except (psutil.Error, OSError):
        pass

    winlogon = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    shell = _reg_value(winreg.HKEY_LOCAL_MACHINE, winlogon, "Shell")
    if isinstance(shell, str) and shell.strip().lower() not in ("explorer.exe", ""):
        add("Winlogon", "Shell", shell, f"HKLM\\{winlogon}", [Flag("replaces the Windows shell (normally explorer.exe)", 50)])
    user_shell = _reg_value(winreg.HKEY_CURRENT_USER, winlogon, "Shell")
    if isinstance(user_shell, str) and user_shell.strip():
        add("Winlogon", "Shell (user)", user_shell, f"HKCU\\{winlogon}", [Flag("replaces the Windows shell for this user", 50)])
    userinit = _reg_value(winreg.HKEY_LOCAL_MACHINE, winlogon, "Userinit")
    if isinstance(userinit, str):
        parts = [p.strip().lower() for p in userinit.split(",") if p.strip()]
        if not all(p.endswith("userinit.exe") for p in parts):
            add("Winlogon", "Userinit", userinit, f"HKLM\\{winlogon}", [Flag("extra program added to Userinit (runs at every logon)", 50)])

    appinit_key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows"
    appinit = _reg_value(winreg.HKEY_LOCAL_MACHINE, appinit_key, "AppInit_DLLs")
    if isinstance(appinit, str) and appinit.strip():
        add("AppInit DLL", "AppInit_DLLs", appinit, f"HKLM\\{appinit_key}", [Flag("a DLL loaded into every program (AppInit_DLLs)", 50)])

    ifeo = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ifeo) as key:
            index = 0
            while True:
                try:
                    program = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                debugger = _reg_value(winreg.HKEY_LOCAL_MACHINE, f"{ifeo}\\{program}", "Debugger")
                if isinstance(debugger, str) and debugger.strip():
                    add("IFEO debugger", program, debugger, f"HKLM\\{ifeo}\\{program}",
                        [Flag(f"starting {program} runs this program instead", 35)])
    except OSError:
        pass
    return entries


# --- autostart: Linux / macOS -------------------------------------------------------------------

SHELL_RC_RE = re.compile(r"(curl|wget)\b.*\|\s*(ba|z|da)?sh|base64\s+(-d|--decode)|\b(nc|ncat|netcat)\b.*\s-[ec]\s|"
                         r"/dev/tcp/|LD_PRELOAD=|nohup\s+/(tmp|dev/shm|var/tmp)/|python[\d.]*\s+-c\s+.*socket", re.I)
CRON_TIME_RE = re.compile(r"^(@\w+)\s+(.*)$|^((?:\S+\s+){5})(.*)$")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _cron_lines(text: str, system: bool) -> list[str]:
    commands = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", line):
            continue
        match = CRON_TIME_RE.match(line)
        if not match:
            continue
        rest = match.group(2) if match.group(1) else match.group(4)
        if system and rest:
            parts = rest.split(None, 1)
            rest = parts[1] if len(parts) > 1 else ""
        if rest:
            commands.append(rest)
    return commands


def _shell_target(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for word in words:
        if "=" in word and not word.startswith("/"):
            continue
        if word in ("sudo", "nice", "ionice", "nohup", "exec", "env", "-u", "root"):
            continue
        return word
    return ""


def unix_autostart(places: Places) -> list[AutoFacts]:
    entries: list[AutoFacts] = []
    home = Path.home()

    def add(kind: str, name: str, command: str, location: str, base: list[Flag] | None = None,
            disabled: bool = False) -> None:
        target = _shell_target(command)
        state = "ok" if target.startswith("/") else "unknown"
        if not target.startswith("/") and target:
            import shutil
            found = shutil.which(target)
            if found:
                target, state = found, "ok"
        entries.append(AutoFacts(kind, name, command, location, target, target_state=state,
                                 base_flags=base or [], disabled=disabled))

    # cron
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        for command in _cron_lines(result.stdout if result.returncode == 0 else "", system=False):
            add("cron (user)", command[:40], command, "crontab -l")
    except (OSError, subprocess.SubprocessError):
        pass
    for path in [Path("/etc/crontab"), *sorted(Path("/etc/cron.d").glob("*"))]:
        if path.is_file():
            for command in _cron_lines(_read_text(path), system=True):
                add("cron (system)", command[:40], command, str(path))
    for path in sorted(Path("/var/spool/cron/crontabs").glob("*")) + sorted(Path("/var/spool/cron").glob("*")):
        if path.is_file() and os.access(path, os.R_OK):
            for command in _cron_lines(_read_text(path), system=False):
                add("cron (user)", command[:40], command, str(path))
    for period in ("hourly", "daily", "weekly", "monthly"):
        for path in sorted(Path(f"/etc/cron.{period}").glob("*")):
            if path.is_file() and not path.name.startswith("."):
                add(f"cron.{period}", path.name, str(path), f"/etc/cron.{period}")

    # systemd: admin-installed and enabled units, plus user units
    unit_files: dict[str, tuple[Path, str]] = {}
    for folder, scope in ((Path("/etc/systemd/system"), "system"), (Path("/usr/local/lib/systemd/system"), "system"),
                          (Path("/etc/systemd/user"), "user (all)"), (home / ".config/systemd/user", "user")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.suffix != ".service":
                continue
            try:
                real = path.resolve()
            except OSError:
                continue
            if real.is_file() and str(real) not in unit_files and str(real) != "/dev/null":
                unit_files[str(real)] = (path, scope)
    for real, (path, scope) in unit_files.items():
        for line in _read_text(Path(real)).splitlines():
            line = line.strip()
            if line.startswith(("ExecStart=", "ExecStartPre=", "ExecStartPost=")):
                command = line.split("=", 1)[1].lstrip("-@:+!")
                if command:
                    add(f"systemd ({scope})", path.name, command, real)

    # desktop autostart
    for folder in (home / ".config/autostart", Path("/etc/xdg/autostart")):
        for path in sorted(folder.glob("*.desktop")):
            text = _read_text(path)
            exec_line = re.search(r"^Exec\s*=\s*(.+)$", text, re.M)
            hidden = re.search(r"^(Hidden|X-GNOME-Autostart-enabled)\s*=\s*(true|false)", text, re.M | re.I)
            disabled = bool(hidden and ((hidden.group(1) == "Hidden") == (hidden.group(2).lower() == "true")))
            if exec_line:
                command = re.sub(r"\s%[fFuUdDnNickvm]", "", exec_line.group(1)).strip()
                add("Desktop autostart", path.name, command, str(folder), disabled=disabled)

    # shell startup files: only suspicious lines
    rc_files = [home / n for n in (".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile",
                                   ".zshenv", ".bash_logout")]
    rc_files += [Path("/etc/profile"), Path("/etc/bash.bashrc"), Path("/etc/zsh/zshrc"), Path("/etc/environment")]
    rc_files += sorted(Path("/etc/profile.d").glob("*.sh"))
    for path in rc_files:
        for number, line in enumerate(_read_text(path).splitlines(), 1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and SHELL_RC_RE.search(stripped):
                base = [Flag("suspicious command in a shell startup file", 20)]
                if "LD_PRELOAD" in stripped:
                    base.append(Flag("sets LD_PRELOAD (injects a library into programs)", 40))
                add("Shell startup", f"{path.name}:{number}", stripped, str(path), base)

    preload = Path("/etc/ld.so.preload")
    for line in _read_text(preload).splitlines():
        if line.strip() and not line.strip().startswith("#"):
            entries.append(AutoFacts("ld.so.preload", Path(line.strip()).name, line.strip(), str(preload),
                                     line.strip(), base_flags=[Flag("library injected into every program "
                                                                    "(/etc/ld.so.preload is a rootkit favourite)", 60)]))
    rc_local = Path("/etc/rc.local")
    for line in _read_text(rc_local).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped not in ("exit 0", "exit"):
            add("rc.local", stripped[:40], stripped, str(rc_local))

    if IS_MAC:
        import plistlib
        for folder in (home / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")):
            for path in sorted(folder.glob("*.plist")):
                try:
                    data = plistlib.loads(path.read_bytes())
                except Exception:
                    continue
                args = data.get("ProgramArguments") or ([data["Program"]] if data.get("Program") else [])
                if args:
                    add("launchd", data.get("Label", path.stem), " ".join(shlex.quote(str(a)) for a in args), str(folder),
                        disabled=bool(data.get("Disabled")))
    return entries


def collect_autostart(places: Places) -> list[AutoFacts]:
    try:
        return windows_autostart(places) if IS_WINDOWS else unix_autostart(places)
    except Exception as exc:  # one broken source shouldn't hide the rest of the tool
        return [AutoFacts("error", "autostart scan failed", str(exc), "")]


# --- helpers for actions ---------------------------------------------------------------------------

def open_folder(path: str) -> None:
    """Show the file in the system file manager."""
    options: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WINDOWS:
        subprocess.Popen(["explorer", f"/select,{path}"], **options)
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path], **options)
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) or "/"], start_new_session=True, **options)


def virustotal_url(sha256: str) -> str:
    return f"https://www.virustotal.com/gui/file/{sha256}"
