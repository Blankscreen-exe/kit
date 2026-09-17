"""Suspicion rules for kit top: plain functions over plain data, so they can be tested without psutil or a UI.

Every rule returns Flags (a reason plus a weight). A process's score is the sum of its weights, and the score
decides its level: high >= 60, medium >= 30, low >= 10, otherwise ok. Weak signs (a new file, an odd name) are
kept below 10 on purpose, so they only matter together with something else.
"""

from __future__ import annotations

import math
import ntpath
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

HIGH, MEDIUM, LOW = 60, 30, 10
LEVELS = ("high", "medium", "low", "ok")
RECENT_DAYS = 7


@dataclass
class Flag:
    reason: str
    weight: int

    def to_dict(self) -> dict:
        return {"reason": self.reason, "weight": self.weight}


def total_score(flags: list[Flag]) -> int:
    return sum(flag.weight for flag in flags)


def level_for(score: int) -> str:
    if score >= HIGH:
        return "high"
    if score >= MEDIUM:
        return "medium"
    if score >= LOW:
        return "low"
    return "ok"


def finish(flags: list[Flag]) -> list[Flag]:
    """Adds the 'several signs together' bonus and orders the flags, strongest first."""
    flags = sorted(flags, key=lambda f: -f.weight)
    if sum(1 for f in flags if f.weight >= 5) >= 3:
        flags.append(Flag("several warning signs together", 10))
    return flags


# --- places ------------------------------------------------------------------------

# Hidden folders in the home directory that developer tools install programs into.
DEV_DOT_DIRS = {
    ".local", ".cargo", ".rustup", ".nvm", ".bun", ".dotnet", ".vscode", ".vscode-server", ".vscode-insiders",
    ".cursor", ".cursor-server", ".pyenv", ".rbenv", ".deno", ".volta", ".sdkman", ".npm", ".yarn", ".pnpm",
    ".gradle", ".m2", ".conda", ".pixi", ".ghcup", ".fly", ".docker", ".rd", ".krew", ".jbang", ".jdks",
    ".platformio", ".espressif", ".windsurf", ".claude", ".codeium", ".asdf", ".mise", ".proto", ".rye",
    ".juliaup", ".elan", ".opam", ".nimble", ".foundry", ".sui", ".wasmtime", ".wasmer", ".cabal", ".stack",
    ".dotnet-tools", ".nuget", ".android", ".var", ".steam", ".wine", ".kube", ".minikube", ".pulumi",
}


@dataclass
class Places:
    """The folders the rules compare paths against, for one platform. Paths are stored normalised."""

    windows: bool
    home: str
    temp: list[str] = field(default_factory=list)
    user_dirs: list[tuple[str, str]] = field(default_factory=list)  # (folder, what it is)
    roaming: str = ""
    programdata: str = ""
    windir: str = ""
    system_dirs: list[str] = field(default_factory=list)
    program_dirs: list[str] = field(default_factory=list)
    cache: str = ""

    @classmethod
    def detect(cls, windows: bool | None = None, env: dict[str, str] | None = None, home: str | None = None) -> Places:
        windows = os.name == "nt" if windows is None else windows
        env = dict(os.environ) if env is None else env
        home = home or str(Path.home())
        if windows:
            def get(name: str, default: str = "") -> str:
                return env.get(name) or env.get(name.upper()) or default

            profile = get("USERPROFILE", home)
            local = get("LOCALAPPDATA", ntpath.join(profile, "AppData", "Local"))
            windir = get("SystemRoot", get("WINDIR", r"C:\Windows"))
            system_drive = get("SystemDrive", "C:") + "\\"
            programdata = get("ProgramData", ntpath.join(system_drive, "ProgramData"))
            places = cls(
                windows=True,
                home=profile,
                temp=[get("TEMP"), get("TMP"), ntpath.join(local, "Temp"), ntpath.join(windir, "Temp")],
                user_dirs=[
                    (ntpath.join(profile, "Downloads"), "your Downloads folder"),
                    (ntpath.join(profile, "Desktop"), "the Desktop"),
                    (ntpath.join(profile, "OneDrive", "Desktop"), "the Desktop"),
                    (ntpath.join(get("PUBLIC", ntpath.join(system_drive, "Users", "Public"))), "the Public user folder"),
                    (ntpath.join(system_drive, "$Recycle.Bin"), "the Recycle Bin"),
                    (ntpath.join(system_drive, "PerfLogs"), "C:\\PerfLogs"),
                    (ntpath.join(windir, "Tasks"), "C:\\Windows\\Tasks"),
                    (ntpath.join(windir, "debug"), "C:\\Windows\\debug"),
                ],
                roaming=get("APPDATA", ntpath.join(profile, "AppData", "Roaming")),
                programdata=programdata,
                windir=windir,
                system_dirs=[windir],
                program_dirs=[
                    get("ProgramFiles", ntpath.join(system_drive, "Program Files")),
                    get("ProgramFiles(x86)", ntpath.join(system_drive, "Program Files (x86)")),
                    get("ProgramW6432", ntpath.join(system_drive, "Program Files")),
                    ntpath.join(local, "Programs"),
                    ntpath.join(local, "Microsoft"),
                    ntpath.join(programdata, "Microsoft"),
                ],
            )
        else:
            places = cls(
                windows=False,
                home=home,
                temp=["/tmp", "/var/tmp", "/dev/shm", env.get("TMPDIR", "/tmp")],
                user_dirs=[(posixpath.join(home, "Downloads"), "your Downloads folder"),
                           (posixpath.join(home, "Desktop"), "the Desktop")],
                system_dirs=["/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/lib", "/lib", "/usr/libexec"],
                program_dirs=["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/opt", "/snap",
                              "/nix", "/gnu", "/app", "/var/lib/flatpak", "/var/lib/snapd",
                              posixpath.join(home, ".local", "share", "flatpak"), "/Applications", "/System", "/Library"],
                cache=env.get("XDG_CACHE_HOME") or posixpath.join(home, ".cache"),
            )
        places.normalise()
        return places

    def norm(self, path: str) -> str:
        if self.windows:
            return ntpath.normcase(ntpath.normpath(path)) if path else ""
        return posixpath.normpath(path) if path else ""

    def normalise(self) -> None:
        self.home = self.norm(self.home)
        self.temp = list(dict.fromkeys(self.norm(p) for p in self.temp if p))
        self.user_dirs = [(self.norm(p), what) for p, what in self.user_dirs if p]
        self.roaming = self.norm(self.roaming)
        self.programdata = self.norm(self.programdata)
        self.windir = self.norm(self.windir)
        self.system_dirs = [self.norm(p) for p in self.system_dirs if p]
        self.program_dirs = list(dict.fromkeys(self.norm(p) for p in self.program_dirs if p))
        self.cache = self.norm(self.cache)

    def under(self, path: str, folder: str) -> bool:
        """`path` (already normalised) is inside `folder` (already normalised)."""
        if not folder:
            return False
        sep = "\\" if self.windows else "/"
        return path.startswith(folder.rstrip(sep) + sep)

    def dirname(self, path: str) -> str:
        return ntpath.dirname(path) if self.windows else posixpath.dirname(path)

    def basename(self, path: str) -> str:
        return ntpath.basename(path) if self.windows else posixpath.basename(path)

    def standard(self, path: str) -> bool:
        """Installed where programs normally live (needs admin rights to write there, mostly)."""
        p = self.norm(path)
        return any(self.under(p, d) for d in self.system_dirs + self.program_dirs)

    def user_writable(self, path: str) -> str:
        """A description of the odd, user-writable place `path` is in, or ''."""
        p = self.norm(path)
        if any(self.under(p, t) for t in self.temp):
            return "a temp folder"
        for folder, what in self.user_dirs:
            if self.under(p, folder):
                return what
        if self.windows:
            parent = self.dirname(p)
            if self.programdata and parent == self.programdata:
                return "the top of C:\\ProgramData"
            if self.roaming and parent == self.roaming:
                return "the top of AppData\\Roaming"
            if parent == self.home:
                return "the top of your user folder"
        else:
            if self.cache and self.under(p, self.cache):
                return "~/.cache"
        return ""

    def hidden_dir(self, path: str) -> str:
        """The name of a hidden (dot) folder in the path that isn't a known developer-tool folder, or ''."""
        p = self.norm(path)
        parts = p.replace("\\", "/").split("/")[:-1]
        for index, part in enumerate(parts):
            if part.startswith(".") and part not in (".", ".."):
                folder = "/".join(parts[: index + 1])
                is_home_child = folder.replace("/", "\\" if self.windows else "/") == self.home + ("\\" if self.windows else "/") + part
                if part.lower() in DEV_DOT_DIRS and is_home_child:
                    return ""
                if part.lower() in {".venv", ".tox", ".nox", ".git"}:
                    return ""
                return part
        return ""


# --- names ---------------------------------------------------------------------------

WINDOWS_SYSTEM_NAMES = {
    # name -> folders (relative to the Windows folder) it normally runs from
    "svchost.exe": ("system32", "syswow64"),
    "lsass.exe": ("system32",),
    "lsaiso.exe": ("system32",),
    "csrss.exe": ("system32",),
    "smss.exe": ("system32",),
    "wininit.exe": ("system32",),
    "winlogon.exe": ("system32",),
    "services.exe": ("system32",),
    "spoolsv.exe": ("system32",),
    "taskhostw.exe": ("system32",),
    "sihost.exe": ("system32",),
    "dwm.exe": ("system32",),
    "ctfmon.exe": ("system32", "syswow64"),
    "fontdrvhost.exe": ("system32",),
    "conhost.exe": ("system32",),
    "dllhost.exe": ("system32", "syswow64"),
    "rundll32.exe": ("system32", "syswow64"),
    "regsvr32.exe": ("system32", "syswow64"),
    "userinit.exe": ("system32",),
    "taskmgr.exe": ("system32", "syswow64"),
    "wmiprvse.exe": ("system32\\wbem", "syswow64\\wbem"),
    "searchindexer.exe": ("system32",),
    "audiodg.exe": ("system32",),
    "cmd.exe": ("system32", "syswow64"),
    "powershell.exe": ("system32\\windowspowershell\\v1.0", "syswow64\\windowspowershell\\v1.0"),
    "wscript.exe": ("system32", "syswow64"),
    "cscript.exe": ("system32", "syswow64"),
    "mshta.exe": ("system32", "syswow64"),
    "explorer.exe": ("", "syswow64"),
}
# Names malware most likes to imitate; look-alikes of these get flagged.
LOOKALIKE_TARGETS = ["svchost", "lsass", "csrss", "winlogon", "services", "smss", "wininit", "spoolsv",
                     "taskhostw", "rundll32", "explorer", "conhost", "dllhost", "sihost", "taskmgr"]
LOOKALIKE_ALLOWED = {"iexplore", "taskhost", "lsaiso", "service", "explore", "svchost", "hostx"}
LINUX_SYSTEM_NAMES = {"sshd", "systemd", "init", "cron", "crond", "dbus-daemon", "rsyslogd", "systemd-journald",
                      "systemd-logind", "systemd-resolved", "systemd-udevd", "polkitd", "agetty", "login",
                      "atd", "udevd", "networkmanager", "snapd", "containerd", "dockerd", "journald"}
LINUX_SYSTEM_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin", "/usr/lib", "/lib", "/usr/libexec", "/usr/local/sbin",
                     "/usr/local/bin", "/snap")
KERNEL_THREAD_RE = re.compile(r"^(kworker|kthreadd|ksoftirqd|kswapd|migration|rcu_\w+|watchdog|kdevtmpfs|jbd2|"
                              r"khugepaged|kcompactd|kblockd|irq/|cpuhp)", re.I)
LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "4": "a", "7": "t", "!": "i", "|": "l"})
DOUBLE_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|txt|rtf|jpe?g|png|gif|bmp|mp[34]|mov|avi|zip|rar|7z|html?)"
                           r"\.(exe|scr|com|bat|cmd|pif|js|jse|vbs|vbe|wsf|hta|lnk)$", re.I)


def edit_distance(a: str, b: str, limit: int = 3) -> int:
    """Optimal string alignment distance (swaps of two neighbouring letters count as one edit)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                current[j] = min(current[j], previous2[j - 2] + 1)
        previous2, previous = previous, current
    return previous[-1]


def lookalike_of(name: str) -> str:
    """The system process `name` imitates (e.g. svch0st.exe -> svchost.exe), or ''."""
    stem = name.lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    if len(stem) < 4 or stem in LOOKALIKE_ALLOWED or stem in LOOKALIKE_TARGETS:
        return ""
    swapped = stem.translate(LEET)
    for target in LOOKALIKE_TARGETS:
        if swapped == target:
            return target + ".exe"
        limit = 1 if len(target) < 8 else 2
        distance = edit_distance(stem, target, limit)
        if distance <= limit and not (distance == 2 and len(stem) != len(target)):
            return target + ".exe"
    return ""


def entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = {ch: text.count(ch) for ch in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def random_looking(name: str) -> bool:
    stem = re.sub(r"\.[a-z0-9]{1,4}$", "", name.lower())
    if re.fullmatch(r"[0-9a-f]{12,}", stem) or re.fullmatch(r"[0-9a-f]{8}(-?[0-9a-f]{4}){3}-?[0-9a-f]{12}", stem):
        return True
    digits = sum(ch.isdigit() for ch in stem)
    letters = sum(ch.isalpha() for ch in stem)
    if len(stem) < 10 or digits < 3 or letters < 3 or not stem.isalnum():
        return False
    switches = sum(1 for a, b in zip(stem, stem[1:]) if a.isdigit() != b.isdigit())
    return entropy(stem) >= 3.2 and switches >= 4


def name_flags(name: str, exe: str, places: Places, signer: str = "") -> list[Flag]:
    flags: list[Flag] = []
    lower = name.lower()
    if places.windows:
        expected = WINDOWS_SYSTEM_NAMES.get(lower)
        if expected and exe:
            folder = places.dirname(places.norm(exe))
            allowed = {places.norm(ntpath.join(places.windir, sub)) if sub else places.windir for sub in expected}
            if folder not in allowed:
                weight = 15 if "microsoft" in signer.lower() else 60
                flags.append(Flag(f"named like the Windows program {lower}, but runs from {folder}", weight))
        target = lookalike_of(lower)
        if target:
            flags.append(Flag(f"name looks like the Windows program {target} but isn't it", 45))
        if lower.endswith((".scr", ".pif")):
            flags.append(Flag(f"a {lower[-4:]} file running as a program (a classic malware disguise)", 20))
    else:
        base = lower.split(":")[0]
        if base in LINUX_SYSTEM_NAMES and exe:
            folder = places.dirname(exe)
            if not any(folder == d or folder.startswith(d + "/") for d in LINUX_SYSTEM_DIRS):
                flags.append(Flag(f"named like the system service {base}, but runs from {folder}", 50))
    if DOUBLE_EXT_RE.search(lower):
        flags.append(Flag("double file extension, made to look like a document", 40))
    if random_looking(name):
        flags.append(Flag("random-looking name", 8))
    return flags


# --- locations -------------------------------------------------------------------------

def location_flags(path: str, places: Places, signed: bool = False, hidden: bool = False, prefix: str = "runs from") -> list[Flag]:
    if not path:
        return []
    flags: list[Flag] = []
    halve = 2 if signed else 1
    odd = places.user_writable(path)
    if odd:
        weight = 35 if (odd == "a temp folder" and not places.windows) else 30 if odd == "a temp folder" else 25
        flags.append(Flag(f"{prefix} {odd}", weight // halve))
    dot = places.hidden_dir(path)
    if dot and not odd:
        flags.append(Flag(f"{prefix} a hidden folder ({dot})", 15 // halve))
    if hidden:
        flags.append(Flag("the program file is marked hidden", 20))
    if not places.windows and not odd and not dot and not places.standard(path):
        where = "runs from" if prefix == "runs from" else "starts a program"
        flags.append(Flag(f"{where} outside the usual system folders", 5))
    return flags


def signature_flags(status: str | None, path: str, places: Places) -> list[Flag]:
    """Windows Authenticode result -> flags. None means not checked (yet)."""
    if not places.windows or not status or not path:
        return []
    if status == "invalid":
        return [Flag("its digital signature is broken: the file was changed after it was signed", 40)]
    if status == "untrusted":
        return [Flag("signed with a certificate Windows doesn't trust", 25)]
    if status == "unsigned":
        if "\\windowsapps\\" in places.norm(path):
            return []  # Store apps are signed as a package, not file by file
        weight = 3 if places.standard(path) else 6
        return [Flag("has no digital signature", weight)]
    return []


# --- command lines ------------------------------------------------------------------------

POWERSHELL = {"powershell.exe", "pwsh.exe", "powershell", "pwsh", "powershell_ise.exe"}
COMMAND_RULES: list[tuple[re.Pattern, str, int, set[str] | None]] = [
    # (pattern, reason, weight, only for these programs or None)
    (re.compile(r"(^|\s)[-/]e(n|nc|nco|ncod|ncode|ncoded|ncodedc\w*)?\s+[a-z0-9+/=]{16,}", re.I),
     "runs a hidden (base64-encoded) PowerShell command", 45, POWERSHELL),
    (re.compile(r"(^|\s)[-/]w(i|in|ind|indo|indow|indows|indowst\w*)?\s+hid", re.I),
     "starts PowerShell with a hidden window", 25, POWERSHELL),
    (re.compile(r"(^|\s)[-/](nop|noprofile)(\s|$)", re.I), "skips the PowerShell profile", 2, POWERSHELL),
    (re.compile(r"(^|[\s;|(])(iex|invoke-expression)\b", re.I), "runs code built at run time (Invoke-Expression)", 7, None),
    (re.compile(r"downloadstring|downloadfile|downloaddata|net\.webclient|start-bitstransfer", re.I),
     "downloads something from the command line", 30, None),
    (re.compile(r"(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|]*\|\s*(iex|invoke-expression)", re.I),
     "downloads a script and runs it straight away", 40, None),
    (re.compile(r"frombase64string", re.I), "decodes base64 data", 25, None),
    (re.compile(r"bitsadmin\b.*[/-]transfer", re.I), "downloads with bitsadmin", 35, None),
    (re.compile(r"certutil(\.exe)?\b.*[/-](urlcache|decode|decodehex)", re.I), "misuses certutil to download or decode files", 40, None),
    (re.compile(r"mshta(\.exe)?[\"']?\s+[\"']?(https?:|javascript:|vbscript:)", re.I), "mshta running a script from the web or inline", 45, None),
    (re.compile(r"regsvr32(\.exe)?\b.*([/-]i:\s*[\"']?https?:|scrobj\.dll)", re.I), "regsvr32 loading a remote script (squiblydoo)", 50, None),
    (re.compile(r"rundll32(\.exe)?\b.*(javascript:|vbscript:|mshtml,\s*runhtmlapplication)", re.I), "rundll32 running script code", 50, None),
    (re.compile(r"wmic(\.exe)?\b.*process\s+call\s+create", re.I), "starts programs through WMI", 35, None),
    (re.compile(r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", re.I), "downloads a script and pipes it into a shell", 35, None),
    (re.compile(r"base64\s+(-d|--decode|-D)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.I), "decodes base64 and runs it in a shell", 45, None),
    (re.compile(r"\b(nc|ncat|netcat)(\.exe)?\b.*\s-[ce]\s", re.I), "netcat handing a shell to the network", 50, None),
    (re.compile(r"/dev/(tcp|udp)/", re.I), "opens a raw network connection from the shell (reverse shell pattern)", 50, None),
    (re.compile(r"python[\d.]*\s+-c\s+.*\b(socket|pty\.spawn)", re.I), "inline Python using sockets or a pty (reverse shell pattern)", 40, None),
    (re.compile(r"xmrig|stratum\+(tcp|ssl|tls)|--donate-level|cryptonight|\brandomx\b|\bminerd\b|cpuminer|nicehash", re.I),
     "crypto-miner markers in the command line", 60, None),
    (re.compile(r"(^|\s)(chmod\s+\+x|chmod\s+7\d\d)\s+/(tmp|dev/shm|var/tmp)/", re.I), "makes a file in a temp folder executable", 25, None),
]


def command_flags(command: str, program: str = "") -> list[Flag]:
    if not command:
        return []
    program = program.lower()
    flags: list[Flag] = []
    seen: set[str] = set()
    for pattern, reason, weight, only in COMMAND_RULES:
        if only is not None and program not in only:
            continue
        if reason not in seen and pattern.search(command):
            seen.add(reason)
            flags.append(Flag(reason, weight))
    return flags


# --- parents ---------------------------------------------------------------------------------

DOCUMENT_APPS = {"winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe", "onenote.exe", "msaccess.exe",
                 "mspub.exe", "visio.exe", "acrord32.exe", "acrobat.exe", "foxitreader.exe", "foxitpdfreader.exe",
                 "hwp.exe", "wordpad.exe", "soffice.bin", "soffice", "libreoffice", "evince", "okular", "atril",
                 "xreader", "zathura", "thunderbird", "thunderbird.exe"}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "iexplore.exe", "vivaldi.exe"}
SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe",
          "regsvr32.exe", "certutil.exe", "bitsadmin.exe", "wmic.exe", "sh", "bash", "dash", "zsh", "python",
          "python3", "perl", "nc", "ncat", "curl", "wget"}
SCRIPT_HOSTS = {"wscript.exe", "cscript.exe", "mshta.exe"}


def parent_flags(name: str, parent: str | None, command: str) -> list[Flag]:
    child, parent = name.lower(), (parent or "").lower()
    flags: list[Flag] = []
    if parent in DOCUMENT_APPS and child in SHELLS:
        flags.append(Flag(f"started by {parent}: documents shouldn't launch {child}", 50))
    elif parent in BROWSERS and child in SHELLS and "chrome-extension://" not in command and "moz-extension" not in command:
        flags.append(Flag(f"started by the browser {parent}", 25))
    if child in SCRIPT_HOSTS:
        flags.append(Flag(f"{child} runs a script (legitimate uses are rare these days)", 10))
    return flags


# --- network ----------------------------------------------------------------------------------

BAD_PORTS = {
    4444: "Metasploit", 4445: "backdoors", 1337: "backdoors", 31337: "backdoors", 12345: "backdoors",
    54321: "backdoors", 6666: "IRC botnets", 6667: "IRC botnets", 6668: "IRC botnets", 6669: "IRC botnets",
    6697: "IRC botnets", 3333: "mining pools", 5555: "mining pools", 7777: "mining pools", 14444: "mining pools",
    14433: "mining pools", 45700: "mining pools", 45560: "mining pools", 9050: "Tor", 9150: "Tor",
}
BROWSER_NAMES = BROWSERS | {"firefox", "chrome", "chromium", "brave", "msedgewebview2.exe", "opera", "vivaldi",
                            "code.exe", "teams.exe", "ms-teams.exe", "discord.exe", "slack.exe", "steam.exe",
                            "onedrive.exe", "dropbox.exe", "svchost.exe"}


def network_flags(name: str, exe: str, places: Places, remotes: list[tuple[str, int]], listening: bool) -> list[Flag]:
    flags: list[Flag] = []
    reported: set[int] = set()
    for ip, port in remotes:
        if port in BAD_PORTS and port not in reported:
            reported.add(port)
            flags.append(Flag(f"connected to {ip}:{port} (port often used by {BAD_PORTS[port]})", 30))
    if listening and exe and (places.user_writable(exe) or places.hidden_dir(exe)):
        flags.append(Flag("accepts network connections while running from an unusual folder", 20))
    if len(remotes) >= 50 and name.lower() not in BROWSER_NAMES:
        flags.append(Flag(f"{len(remotes)} open network connections (scanning or spam?)", 10))
    return flags


# --- whole process -----------------------------------------------------------------------------

@dataclass
class ProcFacts:
    """What the collector learned about one process. None means unknown (usually: access denied)."""

    pid: int
    name: str
    exe: str | None = None
    cmdline: list[str] = field(default_factory=list)
    username: str | None = None
    parent_name: str | None = None
    exe_state: str = "ok"  # ok | missing | deleted
    hidden: bool = False
    file_created: float | None = None
    signature: str | None = None  # trusted | unsigned | invalid | untrusted | None (not checked)
    signer: str = ""
    remotes: list[tuple[str, int]] = field(default_factory=list)
    listening: bool = False
    cpu_sustained: float | None = None  # share of all CPUs (0-100), averaged over the last ~30 s
    elevated: bool = False
    idle: bool = False  # the System Idle Process: its "CPU use" is idle time


def assess_process(facts: ProcFacts, places: Places, now: float) -> list[Flag]:
    exe = facts.exe or ""
    command = " ".join(facts.cmdline)
    program = places.basename(exe) if exe else facts.name
    signed = facts.signature == "trusted"
    flags: list[Flag] = []

    if exe:
        loc = location_flags(exe, places, signed=signed, hidden=facts.hidden)
        flags += loc
        if facts.elevated and places.user_writable(exe):
            flags.append(Flag("runs with full system rights from a folder any user can write to", 40))
        flags += signature_flags(facts.signature, exe, places)
        if facts.file_created and now - facts.file_created < RECENT_DAYS * 86400:
            flags.append(Flag(f"program file is new (less than {RECENT_DAYS} days old)", 3))
    if facts.exe_state == "deleted":
        flags.append(Flag("its program file was deleted or replaced while it runs", 40))
    elif facts.exe_state == "missing":
        flags.append(Flag("its program file no longer exists", 25))

    flags += name_flags(facts.name, exe, places, facts.signer)
    if not places.windows and facts.cmdline:
        first = facts.cmdline[0]
        if re.fullmatch(r"\[.+\]", first.strip()):
            flags.append(Flag(f"pretends to be the kernel thread {first.strip()}", 60))
        elif exe and KERNEL_THREAD_RE.match(facts.name):
            flags.append(Flag(f"uses a kernel-thread name ({facts.name}) but is a normal program", 60))
        else:
            claimed = posixpath.basename(first.split(":")[0].split()[0]) if first.strip() else ""
            real = posixpath.basename(exe)
            if exe and claimed.lower() in LINUX_SYSTEM_NAMES and claimed != real and not real.startswith(claimed):
                flags.append(Flag(f"calls itself {claimed} but the program is {real}", 40))

    flags += command_flags(command, program)
    for arg in facts.cmdline[1:]:
        if places.windows and re.search(r"[a-z]:\\", arg, re.I) or (not places.windows and arg.startswith("/")):
            path = arg.split(",")[0].strip('"')
            if program.lower() in SHELLS and places.user_writable(path):
                flags.append(Flag(f"{program} runs a file from {places.user_writable(path)}", 20))
                break
    flags += parent_flags(program, facts.parent_name, command)
    flags += network_flags(program, exe, places, facts.remotes, facts.listening)

    if facts.cpu_sustained is not None and facts.cpu_sustained >= 50 and not facts.idle:
        flags.append(Flag(f"has used {facts.cpu_sustained:.0f}% of all CPU for a while", 10))
        if any(f.weight >= 10 for f in flags[:-1]):
            flags.append(Flag("high CPU use together with other warning signs (crypto-miner?)", 10))
    return finish(flags)


# --- autostart entries ----------------------------------------------------------------------------

EXEC_EXTS = (".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".scr",
             ".pif", ".dll", ".cpl", ".msi", ".lnk", ".jar", ".py", ".pyw", ".sh")


def command_target(command: str, windows: bool) -> str:
    """The program a command line starts, e.g. '"C:\\A B\\x.exe" --flag' -> 'C:\\A B\\x.exe'."""
    cmd = command.strip()
    if windows:
        cmd = ntpath.expandvars(cmd)
    if not cmd:
        return ""
    if cmd[0] in "\"'":
        end = cmd.find(cmd[0], 1)
        return cmd[1:end] if end > 0 else cmd[1:]
    if windows:
        lower = cmd.lower()
        best = None
        for ext in EXEC_EXTS:
            start = lower.find(ext)
            while start != -1:
                stop = start + len(ext)
                if stop == len(lower) or lower[stop] in ' ,"\t':
                    best = stop if best is None else min(best, stop)
                    break
                start = lower.find(ext, start + 1)
        if best:
            return cmd[:best]
        return cmd.split()[0]
    first = cmd.split()[0]
    return first


@dataclass
class AutoFacts:
    kind: str
    name: str
    command: str
    location: str
    target: str = ""
    target_state: str = "ok"  # ok | missing | unknown
    signature: str | None = None
    signer: str = ""
    hidden: bool = False
    disabled: bool = False
    base_flags: list[Flag] = field(default_factory=list)  # found by the collector (e.g. an IFEO debugger)


def assess_autostart(entry: AutoFacts, places: Places) -> list[Flag]:
    flags = list(entry.base_flags)
    signed = entry.signature == "trusted"
    absolute = ntpath.isabs(entry.target) if places.windows else entry.target.startswith("/")
    if entry.target and absolute:
        flags += location_flags(entry.target, places, signed=signed, hidden=entry.hidden, prefix="starts a program in")
        flags += signature_flags(entry.signature, entry.target, places)
        flags += name_flags(places.basename(entry.target), entry.target, places, entry.signer)
        if entry.target_state == "missing":
            flags.append(Flag("points to a file that doesn't exist (leftover or hijack chance)", 10))
    program = places.basename(entry.target) if entry.target else ""
    flags += command_flags(entry.command, program)
    for part in re.split(r"\s+", entry.command)[1:]:
        path = part.strip("\"',")
        if (places.windows and re.match(r"[a-z]:\\", path, re.I)) or (not places.windows and path.startswith("/")):
            where = places.user_writable(path)
            if where:
                flags.append(Flag(f"its arguments point into {where}", 20))
                break
    return finish(flags)
