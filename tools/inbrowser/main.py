"""Open inbrowser.app: browser-based tools that run offline, on your own machine."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser
from html import unescape
from pathlib import Path

from kitlib import die, style, warn
from kitlib.browser import open_app_window
from kitlib.settings import tool_settings

SITE = "https://inbrowser.app"
TOOLS_URL = f"{SITE}/tools/"
CACHE_DAYS = 7
TIMEOUT = 8

ANCHOR_RE = re.compile(r'href="/tools/([a-z0-9-]+)/"[^>]*>(.*?)</a>', re.S)
TAG_RE = re.compile(r"<[^>]+>")


# --- the tool list -------------------------------------------------------------------

def cache_path() -> Path:
    override = os.environ.get("KIT_INBROWSER_DATA")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "kit" / "inbrowser-tools.json"


def parse_tools(page: str) -> list[dict[str, str]]:
    """Every tool linked from the index page: slug, name and one-line description."""
    tools: dict[str, dict[str, str]] = {}
    for slug, inner in ANCHOR_RE.findall(page):
        if slug in tools:
            continue
        parts = [" ".join(unescape(part).split()) for part in TAG_RE.split(inner)]
        words = [part for part in parts if part]
        tools[slug] = {
            "slug": slug,
            "name": words[0] if words else slug.replace("-", " ").title(),
            "description": words[1] if len(words) > 1 else "",
        }
    return list(tools.values())


def fetch_tools() -> list[dict[str, str]]:
    # Avast's web shield breaks Python's TLS in two ways on this machine (see tools/internet-speed).
    os.environ.pop("SSLKEYLOGFILE", None)
    import ssl
    import urllib.error
    import urllib.request

    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    request = urllib.request.Request(TOOLS_URL, headers={"User-Agent": "kit inbrowser"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            page = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ConnectionError(str(exc)) from exc
    tools = parse_tools(page)
    if not tools:
        raise ConnectionError("the tools page didn't list any tools")
    return tools


def save_cache(tools: list[dict[str, str]]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"fetched": time.time(), "tools": tools}, indent=1), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass  # a missing cache only costs a fetch next time


def load_cache() -> tuple[list[dict[str, str]], float]:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        tools = [t for t in data.get("tools", []) if isinstance(t, dict) and t.get("slug")]
        return tools, float(data.get("fetched", 0))
    except (OSError, ValueError, TypeError):
        return [], 0.0


def tool_list(refresh: bool = False, quiet: bool = False) -> list[dict[str, str]]:
    """The cached list, refetched when it's missing, stale or --refresh was given."""
    tools, fetched = load_cache()
    if tools and not refresh and time.time() - fetched < CACHE_DAYS * 86400:
        return tools
    try:
        fresh = fetch_tools()
    except ConnectionError as exc:
        if tools:
            if not quiet:
                warn(f"couldn't update the tool list ({exc}) - using the copy from {time.strftime('%Y-%m-%d', time.localtime(fetched))}")
            return tools
        if not quiet:
            warn(f"couldn't fetch the tool list ({exc})")
        return []
    save_cache(fresh)
    return fresh


# --- matching ------------------------------------------------------------------------

def score(tool: dict[str, str], query: str) -> int:
    """How well a tool matches: slug and name count for more than the description."""
    slug, name = tool["slug"], tool["name"].lower()
    words = query.split()
    if query == slug or query == name:
        return 100
    points = 0
    if slug.startswith(query) or name.startswith(query):
        points += 40
    if query in slug or query in name:
        points += 25
    haystack = f"{slug} {name} {tool['description']}".lower()
    matched = [word for word in words if word in haystack]
    if len(matched) < len(words):
        return 0  # every word has to appear somewhere
    points += 10 * len(matched)
    if all(word in f"{slug} {name}" for word in words):
        points += 15
    return points


def search(tools: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    query = " ".join(query.lower().split())
    scored = [(score(tool, query), tool) for tool in tools]
    hits = sorted(((points, tool) for points, tool in scored if points > 0), key=lambda pair: (-pair[0], pair[1]["slug"]))
    return [tool for _, tool in hits]


def url_for(slug: str) -> str:
    return f"{SITE}/tools/{slug}/"


def print_tools(tools: list[dict[str, str]], limit: int | None = None) -> None:
    shown = tools[:limit] if limit else tools
    width = max((len(tool["slug"]) for tool in shown), default=0)
    for tool in shown:
        description = tool["description"]
        if description and len(description) > 64:
            description = description[:61] + "..."
        print(f"  {style(tool['slug'].ljust(width), 'bold')}  {tool['name']}")
        if description:
            print(f"  {' ' * width}  {style(description, 'dim')}")
    if limit and len(tools) > limit:
        print(style(f"  ... and {len(tools) - limit} more - narrow it down or use --list", "dim"))


# --- opening -------------------------------------------------------------------------

def open_url(url: str, window: bool, print_only: bool) -> int:
    if print_only:
        print(url)
        return 0
    print(f"opening {style(url, 'bold')}")
    try:
        if window:
            open_app_window(url)
        elif not webbrowser.open(url):
            raise webbrowser.Error("no browser found")
    except (webbrowser.Error, OSError) as exc:
        warn(f"couldn't open a browser ({exc}) - the link is above")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kit inbrowser",
        description="Open inbrowser.app, a big set of tools that run inside your browser, offline.",
    )
    parser.add_argument("query", nargs="*", help="open the tool that matches these words (default: the tool list page)")
    parser.add_argument("-l", "--list", action="store_true", help="list the tools instead of opening one")
    parser.add_argument("-p", "--print", dest="print_only", action="store_true", help="print the link instead of opening it")
    parser.add_argument("--window", action="store_true", default=None, help="open in its own app window (setting: inbrowser.window)")
    parser.add_argument("--no-window", dest="window", action="store_false", help="open in a normal browser tab")
    parser.add_argument("--refresh", action="store_true", help="fetch the list of tools again")
    args = parser.parse_args()

    # Windows pipes default to cp1252, and the tool names use arrows and dashes it can't encode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    settings = tool_settings()
    window = settings.get("window", False) if args.window is None else args.window
    query = " ".join(args.query).strip()

    if args.refresh and not query and not args.list:
        tools = tool_list(refresh=True)
        if not tools:
            die("couldn't fetch the tool list - check your connection")
        print(f"{len(tools)} tools, saved to {style(cache_path(), 'dim')}")
        return 0

    if args.list:
        tools = tool_list(refresh=args.refresh)
        if not tools:
            die("no tool list available - check your connection and try: kit inbrowser --refresh")
        matches = search(tools, query) if query else tools
        if not matches:
            print(f"nothing matches {style(query, 'bold')} - see them all with: kit inbrowser --list")
            return 1
        print(style(f"INBROWSER.APP  {len(matches)} tool{'s' if len(matches) != 1 else ''}", "bold"))
        print_tools(matches)
        return 0

    if not query:
        return open_url(TOOLS_URL, window, args.print_only)

    tools = tool_list(refresh=args.refresh, quiet=args.print_only)
    matches = search(tools, query) if tools else []
    if len(matches) == 1 or (matches and matches[0]["slug"] == query.lower().replace(" ", "-")):
        return open_url(url_for(matches[0]["slug"]), window, args.print_only)
    if not matches:
        if not tools:
            warn("opening the tool list instead - search on the page")
            return open_url(TOOLS_URL, window, args.print_only)
        print(f"nothing matches {style(query, 'bold')} - see them all with: kit inbrowser --list")
        return 1

    print(f"{len(matches)} tools match {style(query, 'bold')} - open one by name:")
    print_tools(matches, limit=10)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
