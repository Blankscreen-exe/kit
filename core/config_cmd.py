"""kit config: view and change the settings of kit and its tools (one TOML file, see kitlib.settings)."""

from __future__ import annotations

import argparse
import difflib
import os
import shlex
import shutil
import subprocess
import sys
from typing import Any

from kitlib import error, settings, style, warn

from core import registry

# Test hook: print the editor command instead of opening an editor.
EDIT_DRY_RUN_ENV = "KIT_CONFIG_EDIT_DRYRUN"


# --- schemas and checks (also used by kit doctor) ------------------------------------

def all_schemas(reg: registry.Registry | None = None) -> dict[str, dict[str, dict]]:
    """section -> schema: [kit] first, then every tool that declares settings, by name."""
    if reg is None:
        from core.cli import RESERVED
        reg = registry.discover(RESERVED)
    schemas: dict[str, dict[str, dict]] = {"kit": settings.KIT_SCHEMA}
    for name in sorted(reg.tools):
        if reg.tools[name].settings:
            schemas[name] = reg.tools[name].settings
    return schemas


def _hint(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=1, cutoff=0.6)
    return f" - did you mean '{close[0]}'?" if close else ""


def audit(schemas: dict[str, dict[str, dict]], data: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Problems in the stored settings: unknown sections/keys and invalid values, as (level, message)."""
    problems: list[tuple[str, str]] = []
    for section, values in data.items():
        schema = schemas.get(section)
        if schema is None:
            problems.append(("warn", f"unknown section [{section}] in the settings file{_hint(section, schemas)}"))
            continue
        for key, value in values.items():
            spec = schema.get(key)
            if spec is None:
                problems.append(("warn", f"unknown setting [{section}] {key}{_hint(key, schema)}"))
                continue
            problem = settings.validate(spec, value)
            if problem:
                problems.append(("warn", f"[{section}] {key} = {fmt(value)}: {problem} (the default is used instead)"))
    return problems


def _valid_only(schemas: dict[str, dict[str, dict]], data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Stored values that fit their schema, so resolving them doesn't print warnings audit() already shows."""
    return {
        section: {
            key: value for key, value in values.items()
            if key in schemas.get(section, {}) and settings.validate(schemas[section][key], value) is None
        }
        for section, values in data.items()
    }


# --- formatting ----------------------------------------------------------------------

def fmt(value: Any) -> str:
    """A value the way it looks in the TOML file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)


def raw(value: Any) -> str:
    """A value for scripts: no quotes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _source_text(item: dict) -> tuple[str, str]:
    """(where a value comes from, colour)."""
    if item["source"] == "file":
        return "file", "green"
    if item["source"] == "env":
        return f"env ${item['spec'].get('env')}", "yellow"
    return "default", "dim"


def _source_label(item: dict, width: int = 0) -> str:
    text, colour = _source_text(item)
    return style(text.ljust(width), colour)


def _help_text(spec: dict) -> str:
    text = spec.get("help", "")
    if spec["type"] == "choice":
        text = f"{text} ({' | '.join(map(str, spec['choices']))})".strip()
    elif "min" in spec or "max" in spec:
        text = f"{text} ({spec.get('min', '')}..{spec.get('max', '')})".strip()
    return text


# --- helpers -------------------------------------------------------------------------

def _load_or_exit() -> dict[str, dict[str, Any]] | None:
    try:
        return settings.load()
    except settings.SettingsError as exc:
        error(str(exc))
        return None


def _lookup(target: str, schemas: dict[str, dict[str, dict]]) -> tuple[str, str, dict] | None:
    section, dot, key = target.partition(".")
    if not dot or not section or not key:
        error(f"expected <section>.<key>, e.g. fetch.color - got '{target}'")
        return None
    if section not in schemas:
        error(f"unknown section '{section}'{_hint(section, schemas)}  (see: kit config list)")
        return None
    if key not in schemas[section]:
        error(f"[{section}] has no setting '{key}'{_hint(key, schemas[section])}  "
              f"(settings: {', '.join(schemas[section])})")
        return None
    return section, key, schemas[section][key]


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        error("can't ask for confirmation without a terminal - pass --yes")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --- commands --------------------------------------------------------------------------

def cmd_list(only: str | None) -> int:
    schemas = all_schemas()
    data = _load_or_exit()
    if data is None:
        return 1
    if only is not None and only not in schemas:
        error(f"unknown section '{only}'{_hint(only, schemas)}  (sections: {', '.join(schemas)})")
        return 1

    path = settings.config_path()
    state = "" if path.is_file() else style("  (not created yet - every value is a default)", "dim")
    print(f"{style('settings file', 'bold')}  {path}{state}")

    valid = _valid_only(schemas, data)
    for section, schema in schemas.items():
        if only is not None and section != only:
            continue
        resolved = settings.resolve(section, schema, valid)
        key_width = max(len(key) for key in schema)
        value_width = min(max(len(fmt(item["value"])) for item in resolved.values()), 32)
        source_width = max(len(_source_text(item)[0]) for item in resolved.values())
        print()
        print(style(f"[{section}]", "bold", "cyan"))
        for key, item in resolved.items():
            value = fmt(item["value"])
            print(f"  {key.ljust(key_width)}  {style(value.ljust(value_width), 'bold')}  "
                  f"{_source_label(item, source_width)}  {style(_help_text(item['spec']), 'dim')}".rstrip())

    problems = audit(schemas, data)
    if only is not None:
        problems = [p for p in problems if f"[{only}]" in p[1]]
    if problems:
        print()
        for _, message in problems:
            warn(message)
    print()
    print(style("change one with:  kit config set <section>.<key> <value>     back to default:  kit config unset <section>.<key>", "dim"))
    return 0


def cmd_get(target: str) -> int:
    schemas = all_schemas()
    found = _lookup(target, schemas)
    if found is None:
        return 1
    section, key, spec = found
    data = _load_or_exit()
    if data is None:
        return 1
    item = settings.resolve(section, {key: spec}, _valid_only(schemas, data))[key]
    print(raw(item["value"]))
    return 0


def cmd_set(target: str, text: str) -> int:
    schemas = all_schemas()
    found = _lookup(target, schemas)
    if found is None:
        return 1
    section, key, spec = found
    data = _load_or_exit()
    if data is None:
        return 1
    try:
        value = settings.parse_value(spec, text)
    except settings.SettingsError as exc:
        error(f"[{section}] {key}: {exc}")
        return 1
    old = settings.resolve(section, {key: spec}, _valid_only(schemas, data))[key]["value"]
    try:
        settings.set_value(section, key, value)
    except settings.SettingsError as exc:
        error(str(exc))
        return 1
    print(f"[{section}] {key}: {fmt(old)} -> {style(fmt(value), 'bold', 'green')}")
    env_name = spec.get("env")
    if env_name and os.environ.get(env_name):
        warn(f"${env_name} is set in this shell, so it still overrides this setting")
    return 0


def cmd_unset(target: str) -> int:
    schemas = all_schemas()
    found = _lookup(target, schemas)
    if found is None:
        return 1
    section, key, spec = found
    try:
        removed = settings.unset_value(section, key)
    except settings.SettingsError as exc:
        error(str(exc))
        return 1
    default = fmt(spec.get("default"))
    if removed:
        print(f"[{section}] {key}: back to the default {style(default, 'bold')}")
    else:
        print(f"[{section}] {key} wasn't set - it already uses the default {default}")
    return 0


def cmd_reset(section: str, yes: bool) -> int:
    schemas = all_schemas()
    data = _load_or_exit()
    if data is None:
        return 1
    if section not in data:
        if section not in schemas:
            error(f"unknown section '{section}'{_hint(section, schemas)}")
            return 1
        print(f"[{section}] has nothing set - it already uses its defaults")
        return 0
    keys = list(data[section])
    if not yes and not _confirm(f"Remove {len(keys)} setting(s) from [{section}] ({', '.join(keys)})?"):
        print("nothing changed")
        return 1
    try:
        for key in keys:
            settings.unset_value(section, key)
    except settings.SettingsError as exc:
        error(str(exc))
        return 1
    print(f"[{section}] reset to defaults")
    return 0


def editor_command(path) -> list[str] | None:
    for variable in ("VISUAL", "EDITOR"):
        value = os.environ.get(variable, "").strip()
        if value:
            return [*shlex.split(value, posix=os.name != "nt"), str(path)]
    if os.name == "nt":
        return ["notepad", str(path)]
    if sys.platform == "darwin":
        return ["open", "-t", str(path)]
    for name in ("nano", "vi", "vim"):
        if shutil.which(name):
            return [name, str(path)]
    return None


def cmd_edit() -> int:
    path = settings.config_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(settings.HEADER, encoding="utf-8", newline="\n")
        print(f"created {path}")
    command = editor_command(path)
    if command is None:
        error(f"no editor found - set $EDITOR, or open {path} yourself")
        return 1
    if os.environ.get(EDIT_DRY_RUN_ENV):
        print(" ".join(command))
        return 0
    try:
        return subprocess.call(command)
    except OSError as exc:
        error(f"couldn't start {command[0]}: {exc} - set $EDITOR, or open {path} yourself")
        return 1


def main(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit config", description="View and change settings for kit and its tools.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    p = commands.add_parser("list", help="show every setting with its value and where it comes from (default)")
    p.add_argument("section", nargs="?", help="only this section, e.g. fetch")
    p = commands.add_parser("get", help="print one setting's value")
    p.add_argument("target", metavar="SECTION.KEY")
    p = commands.add_parser("set", help="change a setting")
    p.add_argument("target", metavar="SECTION.KEY")
    p.add_argument("value")
    p = commands.add_parser("unset", help="put a setting back to its default")
    p.add_argument("target", metavar="SECTION.KEY")
    p = commands.add_parser("reset", help="put every setting in a section back to its default")
    p.add_argument("section")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    commands.add_parser("edit", help="open the settings file in your editor")
    commands.add_parser("path", help="print where the settings file is")
    ns = parser.parse_args(args)

    if ns.command in (None, "list"):
        return cmd_list(getattr(ns, "section", None))
    if ns.command == "get":
        return cmd_get(ns.target)
    if ns.command == "set":
        return cmd_set(ns.target, ns.value)
    if ns.command == "unset":
        return cmd_unset(ns.target)
    if ns.command == "reset":
        return cmd_reset(ns.section, ns.yes)
    if ns.command == "edit":
        return cmd_edit()
    print(settings.config_path())
    return 0
