"""kit's command line: built-in commands, and dispatch of everything else to tools."""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
from pathlib import Path

from kitlib import error, style, warn

from core import registry, runner, scaffold
from core.registry import KIT_HOME, Registry, Tool, current_platform

# name -> (argument hint, description)
BUILTINS = {
    "list": ("[filter]", "list tools, optionally filtered by text"),
    "help": ("[tool]", "show a tool's README and details"),
    "new": ("<name> [--lang py|ps1|sh]", "create a new tool folder from a template"),
    "doctor": ("", "check the install and every tool folder"),
    "path": ("[tool]", "print kit's folder, or a tool's folder"),
}
RESERVED = frozenset(BUILTINS) | {"_complete"}


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if not argv or argv[0] in ("-h", "--help"):
        return cmd_list([])
    command, args = argv[0], argv[1:]
    if command in COMMANDS:
        return COMMANDS[command](args)
    return run_tool(command, args)


def run_tool(name: str, args: list[str]) -> int:
    tool = _find(registry.discover(RESERVED), name)
    if tool is None:
        return 2
    if not tool.supported:
        error(f"'{tool.name}' is not available on {current_platform()} (available on: {', '.join(tool.available_on)})")
        return 2
    try:
        return runner.run(tool, args)
    except runner.RunError as exc:
        error(f"{tool.name}: {exc}")
        return 1


def _find(reg: Registry, name: str) -> Tool | None:
    tool = reg.resolve(name)
    if tool is None:
        error(f"unknown tool '{name}'")
        close = difflib.get_close_matches(name, [*reg.tools, *reg.aliases, *BUILTINS], n=3, cutoff=0.5)
        hint = f"did you mean: {', '.join(close)}?  " if close else ""
        print(f"{hint}run 'kit list' to see what's available", file=sys.stderr)
    return tool


def _section(title: str) -> None:
    print()
    print(style(title, "bold"))


def _print_markdown(text: str) -> None:
    """Prints a README readably: headings bold, code blocks indented and coloured."""
    lines = text.strip("\n").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]  # the tool name is already printed as the title
    in_code = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            print("    " + style(line, "cyan"))
        elif line.startswith("#"):
            print(style(line.lstrip("#").strip().upper(), "bold"))
        else:
            print(f"  {line}" if line.strip() else "")


def cmd_list(args: list[str]) -> int:
    reg = registry.discover(RESERVED)
    needle = " ".join(args).lower()
    tools = [
        t for t in reg.tools.values()
        if not needle or needle in " ".join([t.name, *t.aliases, t.summary, t.category]).lower()
    ]

    print(f"{style('kit', 'bold', 'cyan')} {style('- personal toolbox', 'dim')}   {style(KIT_HOME, 'dim')}")
    print()
    if not tools:
        print("  no tools match" if needle else "  no tools yet - create one with: kit new <name>")
        print()
    else:
        name_width = max(len(t.name) for t in tools)
        alias_width = max(len(", ".join(t.aliases)) for t in tools)
        by_category: dict[str, list[Tool]] = {}
        for tool in sorted(tools, key=lambda t: (t.category, t.name)):
            by_category.setdefault(tool.category, []).append(tool)
        for category, items in by_category.items():
            print(f"  {style(category.upper(), 'bold')}")
            for tool in items:
                if tool.supported:
                    columns = [style(tool.name.ljust(name_width), "green")]
                    summary = tool.summary or style("(no summary - add a README.md)", "dim")
                else:
                    columns = [style(tool.name.ljust(name_width), "dim")]
                    summary = style(f"{tool.summary} (only on {', '.join(tool.available_on)})".strip(), "dim")
                if alias_width:
                    columns.append(style(", ".join(tool.aliases).ljust(alias_width), "dim"))
                columns.append(summary)
                print("    " + "  ".join(columns))
            print()

    print(f"{style('run:', 'bold')}       kit <tool> [args]      {style('docs:', 'bold')} kit help <tool>")
    print(f"{style('built-ins:', 'bold')} {', '.join(BUILTINS)}")
    if reg.problems:
        warn(f"{len(reg.problems)} problem(s) found - run 'kit doctor'")
    return 0


def cmd_help(args: list[str]) -> int:
    if not args:
        print(f"{style('usage:', 'bold')} kit <tool> [args...]")
        _section("BUILT-IN COMMANDS")
        signatures = {name: f"{name} {hint}".strip() for name, (hint, _) in BUILTINS.items()}
        width = max(map(len, signatures.values()))
        for name, (_, text) in BUILTINS.items():
            print(f"  kit {signatures[name].ljust(width)}  {style(text, 'dim')}")
        print()
        print("Run 'kit' on its own to list tools, 'kit help <tool>' for a tool's docs.")
        return 0

    tool = _find(registry.discover(RESERVED), args[0])
    if tool is None:
        return 2

    title = style(tool.name, "bold", "green")
    if tool.aliases:
        title += style(f"  (alias: {', '.join(tool.aliases)})", "dim")
    print(title)
    if tool.readme:
        _print_markdown(tool.readme.read_text(encoding="utf-8"))
    else:
        if tool.summary:
            print(f"  {tool.summary}")
        print(style(f"  no README.md yet - add one in {tool.dir} to document this tool", "dim"))

    _section("DETAILS")
    rows = [
        ("folder", tool.dir),
        ("entry", tool.entry_path() or f"none for {current_platform()}"),
        ("platforms", ", ".join(tool.available_on)),
        ("category", tool.category),
    ]
    for label, value in rows:
        print(f"  {label:<10} {value}")
    print()
    print(style(f"Most tools also explain their own flags:  kit {tool.name} --help", "dim"))
    return 0


def cmd_new(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit new", description="Create a new tool folder under tools/<name>.")
    parser.add_argument("name", help="lowercase letters, digits and dashes, e.g. git-clean")
    parser.add_argument("--lang", choices=sorted(scaffold.TEMPLATES), default="py", help="entry point language (default: py)")
    parser.add_argument("--summary", default="TODO: describe what this tool does in one line.", help="first line of the README, shown in 'kit list'")
    parser.add_argument("--category", default="misc", help="group shown in 'kit list' (default: misc)")
    parser.add_argument("--alias", action="append", default=[], help="short name; repeat for more (e.g. --alias gc)")
    ns = parser.parse_args(args)

    reg = registry.discover(RESERVED)
    taken = set(RESERVED) | set(reg.tools) | set(reg.aliases)
    try:
        directory = scaffold.create_tool(ns.name, ns.lang, ns.summary, ns.category, ns.alias, taken)
    except scaffold.ScaffoldError as exc:
        error(str(exc))
        return 2

    print(f"created {style(directory, 'bold')}")
    for path in sorted(directory.iterdir()):
        print(f"  {path.name}")
    print()
    print(f"next: write the tool in its main file, document it in README.md, then run:  kit {ns.name}")
    return 0


def _same_path(a: str | Path, b: str | Path) -> bool:
    def norm(p: str | Path) -> str:
        return os.path.normcase(os.path.normpath(os.path.expandvars(str(p))))
    return norm(a) == norm(b)


def cmd_doctor(args: list[str]) -> int:
    failures = 0

    def report(level: str, label: str) -> None:
        nonlocal failures
        marks = {"ok": style(" ok ", "green"), "warn": style("warn", "yellow"), "fail": style("FAIL", "bold", "red")}
        failures += level == "fail"
        print(f"  [{marks[level]}] {label}")

    print(style("ENVIRONMENT", "bold"))
    report("ok" if sys.version_info >= (3, 10) else "fail", f"python {sys.version.split()[0]}  ({sys.executable})")
    report("ok", f"kit home  {KIT_HOME}")

    venv = KIT_HOME / ".venv"
    if not shutil.which("uv"):
        report("warn", "uv not found - tools that need packages from pyproject.toml will fail (https://docs.astral.sh/uv/)")
    elif _same_path(Path(sys.prefix).resolve(), venv.resolve()):
        report("ok", f"uv environment  {venv}")
    else:
        report("warn", f"not running in the repo's uv environment ({venv}) - is KIT_PYTHON set? try 'uv sync' in {KIT_HOME}")

    found = shutil.which("kit")
    if found and _same_path(Path(found).resolve().parent, (KIT_HOME / "bin").resolve()):
        report("ok", f"'kit' is on PATH  ({found})")
    elif found:
        report("warn", f"'kit' on PATH is a different program: {found}")
    else:
        report("warn", "'kit' is not on PATH yet - run install.ps1 (Windows) or install.sh (Linux/macOS)")

    kit_home_env = os.environ.get("KIT_HOME")
    if kit_home_env and not _same_path(kit_home_env, KIT_HOME):
        report("warn", f"KIT_HOME is set to {kit_home_env}, but kit lives in {KIT_HOME}")

    _section("TOOLS")
    reg = registry.discover(RESERVED)
    for tool in sorted(reg.tools.values(), key=lambda t: t.name):
        if not tool.supported:
            only = ", ".join(tool.available_on)
            report("ok", f"{tool.name}  {style(f'skipped here, only for {only}', 'dim')}")
            continue
        try:
            runner.build_command(tool, [])
        except runner.RunError as exc:
            report("fail", f"{tool.name}: {exc}")
            continue
        report("ok", f"{tool.name}  {style(tool.entry_path(), 'dim')}")
        if not tool.readme:
            report("warn", f"{tool.name}: no README.md, so 'kit help {tool.name}' has nothing to show")
    for level, message in reg.problems:
        report(level, message)
    if not reg.tools and not reg.problems:
        report("warn", "no tools found")

    print()
    print(style("all good", "green") if not failures else style(f"{failures} problem(s) need fixing", "red"))
    return 1 if failures else 0


def cmd_path(args: list[str]) -> int:
    if not args:
        print(KIT_HOME)
        return 0
    tool = _find(registry.discover(RESERVED), args[0])
    if tool is None:
        return 2
    print(tool.dir)
    return 0


def cmd_complete(args: list[str]) -> int:
    """Hidden: prints candidates for shell tab completion ('tools' = tool names only)."""
    names = sorted(registry.discover(RESERVED).tools)
    if args[:1] != ["tools"]:
        names = sorted([*names, *BUILTINS])
    print("\n".join(names))
    return 0


COMMANDS = {
    "list": cmd_list,
    "help": cmd_help,
    "new": cmd_new,
    "doctor": cmd_doctor,
    "path": cmd_path,
    "_complete": cmd_complete,
}
