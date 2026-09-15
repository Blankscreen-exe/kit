"""Tool registry: every sub-folder of tools/ is a tool. Nothing needs registering.

A tool folder holds:
  main.py | main.ps1 | main.sh | ...   the entry point, found by name
  README.md                            its docs; the first paragraph line becomes the summary
  tool.json                            optional: summary, category, aliases, entry, platforms
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

KIT_HOME = Path(__file__).resolve().parent.parent
TOOLS_DIR = KIT_HOME / "tools"
MANIFEST = "tool.json"
README = "README.md"
PLATFORMS = ("windows", "linux", "macos")

# Entry points looked for, in order, when tool.json doesn't name one.
ENTRY_CANDIDATES = {
    "windows": ("main.py", "main.ps1", "main.cmd", "main.bat", "main.exe"),
    "linux": ("main.py", "main.sh", "main"),
    "macos": ("main.py", "main.sh", "main"),
}

# tool.json field -> accepted JSON type(s)
_FIELDS: dict[str, type | tuple[type, ...]] = {
    "summary": str,
    "category": str,
    "aliases": list,
    "entry": (str, dict),
    "platforms": list,
}


def current_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def tool_roots() -> list[Path]:
    """tools/ in this repo, then any extra folders listed in $KIT_PATH."""
    roots = [TOOLS_DIR]
    for raw in os.environ.get("KIT_PATH", "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    return roots


class ToolError(Exception):
    def __init__(self, message: str, level: str = "fail") -> None:
        super().__init__(message)
        self.level = level


@dataclass
class Tool:
    name: str
    dir: Path
    summary: str = ""
    category: str = "misc"
    aliases: list[str] = field(default_factory=list)
    entry: str | dict[str, str] | None = None
    platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))

    @property
    def readme(self) -> Path | None:
        path = self.dir / README
        return path if path.is_file() else None

    def entry_path(self, platform: str | None = None) -> Path | None:
        platform = platform or current_platform()
        if isinstance(self.entry, str):
            return self.dir / self.entry
        if isinstance(self.entry, dict):
            name = self.entry.get(platform) or self.entry.get("default")
            return self.dir / name if name else None
        for name in ENTRY_CANDIDATES[platform]:
            if (self.dir / name).is_file():
                return self.dir / name
        return None

    @property
    def available_on(self) -> list[str]:
        return [p for p in self.platforms if self.entry_path(p) is not None]

    @property
    def supported(self) -> bool:
        return current_platform() in self.available_on


def readme_summary(path: Path) -> str:
    """First line of prose in a README: skips headings, code blocks, images, tables and HTML."""
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith(("#", "!", "<", "|", "---")):
            continue
        return re.sub(r"\*\*|`", "", stripped)
    return ""


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ToolError(f"{path}: expected a JSON object")

    unknown = sorted(set(data) - set(_FIELDS))
    if unknown:
        raise ToolError(f"{path}: unknown field(s) {', '.join(unknown)} (allowed: {', '.join(_FIELDS)})")
    for key, value in data.items():
        if not isinstance(value, _FIELDS[key]):
            raise ToolError(f"{path}: field '{key}' has the wrong type")
    bad_platforms = sorted(set(data.get("platforms", [])) - set(PLATFORMS))
    if bad_platforms:
        raise ToolError(f"{path}: unknown platform(s) {', '.join(bad_platforms)}")
    if isinstance(data.get("entry"), dict) and not all(isinstance(v, str) for v in data["entry"].values()):
        raise ToolError(f"{path}: 'entry' map values must be file names")
    return data


def load_tool(directory: Path) -> Tool:
    manifest = directory / MANIFEST
    data = _read_manifest(manifest) if manifest.is_file() else {}
    tool = Tool(name=directory.name, dir=directory, **data)
    if not tool.summary and tool.readme:
        tool.summary = readme_summary(tool.readme)
    if not tool.available_on:
        expected = ", ".join(dict.fromkeys(n for names in ENTRY_CANDIDATES.values() for n in names))
        raise ToolError(f"{directory}: no entry point found (expected one of {expected}), skipped", level="warn")
    return tool


@dataclass
class Registry:
    tools: dict[str, Tool]
    aliases: dict[str, str]
    problems: list[tuple[str, str]]  # (level: "warn" | "fail", message)

    def resolve(self, name: str) -> Tool | None:
        return self.tools.get(name) or self.tools.get(self.aliases.get(name, ""))


def discover(reserved: frozenset[str] = frozenset()) -> Registry:
    tools: dict[str, Tool] = {}
    aliases: dict[str, str] = {}
    problems: list[tuple[str, str]] = []

    for root in tool_roots():
        if not root.is_dir():
            if root != TOOLS_DIR:
                problems.append(("warn", f"KIT_PATH folder not found: {root}"))
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name.startswith((".", "_")):
                continue
            try:
                tool = load_tool(directory)
            except ToolError as exc:
                problems.append((exc.level, str(exc)))
                continue
            if tool.name in reserved:
                problems.append(("fail", f"{directory}: '{tool.name}' is a built-in command name, rename the folder"))
            elif tool.name in tools:
                problems.append(("fail", f"{directory}: duplicate tool name (already loaded from {tools[tool.name].dir})"))
            else:
                tools[tool.name] = tool

    for tool in tools.values():
        for alias in tool.aliases:
            if alias in tools or alias in reserved or alias in aliases:
                problems.append(("warn", f"{tool.name}: alias '{alias}' clashes with another name, ignored"))
            else:
                aliases[alias] = tool.name

    return Registry(tools, aliases, problems)
