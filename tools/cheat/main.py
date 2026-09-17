"""Open command cheatsheets (SS64 or tldr pages) for PowerShell, bash, cmd and macOS in the browser."""

from __future__ import annotations

import os

# Avast and similar HTTPS scanners set SSLKEYLOGFILE to a path Python's OpenSSL can't open, which
# aborts the whole process as soon as an SSL context is created. Drop it before touching ssl.
os.environ.pop("SSLKEYLOGFILE", None)

import argparse  # noqa: E402
import ssl  # noqa: E402
import sys  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from kitlib import die, style, warn  # noqa: E402
from kitlib.browser import open_app_window  # noqa: E402
from kitlib.settings import tool_settings  # noqa: E402

TIMEOUT = 3.0
SS64 = "https://ss64.com"
TLDR = "https://tldr.inbrowser.app"
TLDR_RAW = "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages"


@dataclass(frozen=True)
class Topic:
    key: str
    title: str
    names: tuple[str, ...]      # what you can type for it
    ss64: str                   # SS64 section folder
    tldr: tuple[str, ...]       # tldr-pages platform folders, most specific first
    search: str                 # extra word for the web-search fallback


TOPICS = [
    Topic("ps", "PowerShell", ("ps", "powershell", "pwsh", "posh"), "ps", ("windows", "common"), "powershell"),
    Topic("bash", "bash / Linux", ("bash", "linux", "sh", "shell"), "bash", ("linux", "common"), "bash"),
    Topic("cmd", "Windows cmd", ("cmd", "bat", "batch", "nt"), "nt", ("windows", "common"), "cmd"),
    Topic("mac", "macOS", ("mac", "macos", "osx", "zsh"), "mac", ("osx", "common"), "macos"),
]
TOPIC_NAMES = {name: topic for topic in TOPICS for name in topic.names}

# Built-in PowerShell aliases, so offline links and tldr lookups land on the cmdlet page.
# (SS64 itself also redirects many alias pages, e.g. /ps/gci.html -> /ps/get-childitem.html.)
PS_ALIASES = {
    "%": "foreach-object", "?": "where-object", "ac": "add-content", "cat": "get-content",
    "cd": "set-location", "chdir": "set-location", "clc": "clear-content", "clear": "clear-host",
    "cls": "clear-host", "copy": "copy-item", "cp": "copy-item", "cpi": "copy-item", "curl": "invoke-webrequest",
    "del": "remove-item", "diff": "compare-object", "dir": "get-childitem", "echo": "write-output",
    "erase": "remove-item", "fl": "format-list", "foreach": "foreach-object", "ft": "format-table",
    "gc": "get-content", "gci": "get-childitem", "gcm": "get-command", "gi": "get-item", "gl": "get-location",
    "gm": "get-member", "gp": "get-itemproperty", "gps": "get-process", "group": "group-object",
    "gsv": "get-service", "gv": "get-variable", "h": "get-history", "history": "get-history",
    "iex": "invoke-expression", "irm": "invoke-restmethod", "iwr": "invoke-webrequest", "kill": "stop-process",
    "ls": "get-childitem", "man": "get-help", "md": "mkdir", "measure": "measure-object", "mi": "move-item",
    "move": "move-item", "mv": "move-item", "ni": "new-item", "popd": "pop-location", "ps": "get-process",
    "pushd": "push-location", "pwd": "get-location", "r": "invoke-history", "ren": "rename-item",
    "rm": "remove-item", "rmdir": "remove-item", "ri": "remove-item", "rni": "rename-item", "sa": "start-process",
    "saps": "start-process", "sc": "set-content", "select": "select-object", "set": "set-variable",
    "sl": "set-location", "sleep": "start-sleep", "sls": "select-string", "sort": "sort-object",
    "sp": "set-itemproperty", "spps": "stop-process", "start": "start-process", "sv": "set-variable",
    "tee": "tee-object", "type": "get-content", "wget": "invoke-webrequest", "where": "where-object",
    "write": "write-output",
}


# --- lookups -------------------------------------------------------------------

def ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Python 3.13+ rejects CA certificates that don't mark Basic Constraints as critical - including the
    # roots antivirus HTTPS scanners (e.g. Avast) install. Chain and host name are still fully verified.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


def probe(url: str) -> tuple[bool | None, str]:
    """(exists, final url after redirects). exists is None when the site couldn't be reached."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "kit-cheat"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=ssl_context()) as response:
            return True, response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return False, url
        return None, url
    except (urllib.error.URLError, OSError, ValueError):
        return None, url


def default_topic(setting: str) -> Topic:
    if setting != "auto":
        return TOPIC_NAMES[setting]
    if os.name == "nt":
        return TOPIC_NAMES["ps"]
    return TOPIC_NAMES["mac" if sys.platform == "darwin" else "bash"]


def slug(command: str, topic: Topic) -> str:
    text = "-".join(command.lower().split())
    if topic.key == "ps":
        text = PS_ALIASES.get(text, text)
    return text


def index_url(topic: Topic, site: str) -> str:
    return f"{SS64}/{topic.ss64}/" if site == "ss64" else f"{TLDR}/"


def search_url(topic: Topic, command: str, site: str) -> str:
    if site == "tldr":
        return f"{TLDR}/search?query={urllib.parse.quote(command)}"
    query = f"site:ss64.com {topic.search} {command}"
    return f"https://duckduckgo.com/?q={urllib.parse.quote_plus(query)}"


def command_url(topic: Topic, command: str, site: str, check: bool = True) -> tuple[str, str]:
    """(url, note). The note says when it fell back to a search or couldn't check the page."""
    name = slug(command, topic)
    if site == "ss64":
        url = f"{SS64}/{topic.ss64}/{urllib.parse.quote(name)}.html"
        if not check:
            return url, ""
        exists, final = probe(url)
        if exists:
            return final, ""
        if exists is None:
            return url, "couldn't reach ss64.com to check this page, opening it anyway"
        return search_url(topic, command, site), f"SS64 has no '{name}' page for {topic.title}, searching instead"

    candidates = [f"{TLDR_RAW}/{platform}/{urllib.parse.quote(name)}.md" for platform in topic.tldr]
    if check:
        for platform, raw in zip(topic.tldr, candidates):
            exists, _ = probe(raw)
            if exists is None:
                return search_url(topic, name, site), "couldn't reach tldr-pages to check, searching instead"
            if exists:
                return f"{TLDR}/pages/{platform}/{urllib.parse.quote(name)}", ""
        return search_url(topic, name, site), f"tldr has no '{name}' page, searching instead"
    return f"{TLDR}/pages/{topic.tldr[0]}/{urllib.parse.quote(name)}", ""


# --- opening -----------------------------------------------------------------------

def can_open_browser() -> bool:
    """False on headless Linux, where webbrowser would fall back to a text browser in this terminal."""
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_url(url: str, window: bool) -> bool:
    if not can_open_browser():
        return False
    try:
        if window:
            open_app_window(url)
            return True
        return webbrowser.open(url)
    except Exception:
        return False


def print_topics() -> int:
    print(style("TOPICS", "bold"))
    for topic in TOPICS:
        names = ", ".join(topic.names)
        print(f"  {style(topic.key.ljust(5), 'cyan')} {topic.title.ljust(14)} {style(names, 'dim')}")
        print(f"        ss64  {index_url(topic, 'ss64')}")
    print(f"\n  tldr pages (all topics): {TLDR}/")
    return 0


# --- main ----------------------------------------------------------------------

def main() -> int:
    settings = tool_settings()
    parser = argparse.ArgumentParser(
        prog="kit cheat",
        description="Open command cheatsheets (SS64 or tldr pages) for PowerShell, bash, cmd and macOS.",
    )
    parser.add_argument("words", nargs="*", metavar="[topic] [command]",
                        help="a topic (ps, bash, cmd, mac) and/or a command, e.g. 'ps gci' or 'grep'")
    site = parser.add_mutually_exclusive_group()
    site.add_argument("--tldr", dest="site", action="store_const", const="tldr", help="use tldr pages")
    site.add_argument("--ss64", dest="site", action="store_const", const="ss64", help="use SS64")
    parser.add_argument("--window", action=argparse.BooleanOptionalAction, default=None,
                        help=f"open in its own app window (default: the cheat.window setting, {settings['window']})")
    parser.add_argument("-p", "--print", "--url", dest="print_only", action="store_true",
                        help="only print the URL, don't open it")
    parser.add_argument("--no-check", action="store_true", help="don't check online that the page exists")
    parser.add_argument("-l", "--list", action="store_true", help="list the topics and their pages")
    args = parser.parse_args()

    if args.list:
        return print_topics()

    site_name = args.site or settings["site"]
    window = settings["window"] if args.window is None else args.window
    words = list(args.words)
    topic = default_topic(settings["topic"])
    if words and words[0].lower() in TOPIC_NAMES:
        topic = TOPIC_NAMES[words.pop(0).lower()]
    command = " ".join(words).strip()

    if command:
        if "/" in command or "\\" in command:
            die(f"'{command}' doesn't look like a command name")
        url, note = command_url(topic, command, site_name, check=not args.no_check)
    else:
        url, note = index_url(topic, site_name), ""

    if note:
        warn(note)
    if args.print_only:
        print(url)
        return 0
    if open_url(url, window):
        label = f"{topic.title} {command}".strip() if command else f"{topic.title} cheatsheet"
        print(f"{style('opened', 'bold', 'green')} {label}: {url}")
    else:
        print(f"no browser available here - open this link: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
