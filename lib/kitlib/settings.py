"""Settings for kit and its tools: one TOML file outside the repo, so personal choices never get pushed.

Where it lives: $KIT_CONFIG if set, otherwise
  Windows      %APPDATA%\\kit\\config.toml
  Linux/macOS  $XDG_CONFIG_HOME/kit/config.toml   (default ~/.config/kit/config.toml)

[kit] holds kit's own settings; every other section is named after a tool. Tools declare the settings
they understand in tool.json under "settings":

  "settings": {
    "color": {"type": "choice", "choices": ["cyan", "magenta"], "default": "cyan", "help": "Label colour"},
    "art":   {"type": "string", "default": "", "env": "KIT_FETCH_ART", "help": "Art name or file"}
  }

Types: string, int, float, bool, choice. Optional fields: default, help, choices, min, max, env.

A tool reads its values with `tool_settings()`. Precedence, highest first:
command-line flag (the tool applies it) > the setting's environment variable > this file > default.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from kitlib.term import warn

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 outside kit's uv environment
    tomllib = None  # type: ignore[assignment]

TYPES = ("string", "int", "float", "bool", "choice")
SPEC_FIELDS = {"type", "default", "help", "choices", "min", "max", "env"}

# kit's own settings: the [kit] section.
KIT_SCHEMA: dict[str, dict[str, Any]] = {
    "update_check": {
        "type": "bool", "default": True,
        "help": "Check GitHub for kit updates at most once a day and print a one-line notice",
    },
    "hub_port": {
        "type": "int", "default": 9800, "min": 1, "max": 65535,
        "help": "Port kit hub listens on (the next free one is used if it's taken)",
    },
    "hub_open": {
        "type": "bool", "default": True,
        "help": "Open kit hub in your browser when it starts",
    },
    "browser": {
        "type": "string", "default": "", "env": "KIT_BROWSER",
        "help": "Edge/Chrome/Chromium executable for PDFs and app windows (empty: find one automatically)",
    },
}

HEADER = """\
# kit settings
# Edit by hand, or use:  kit config set <section>.<key> <value>
# [kit] is kit itself; every other section is a tool. See every setting with:  kit config list
"""

_warned: set[str] = set()


class SettingsError(Exception):
    pass


def config_path() -> Path:
    override = os.environ.get("KIT_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "kit" / "config.toml"


# --- reading -----------------------------------------------------------------------

def load() -> dict[str, dict[str, Any]]:
    """The whole file as {section: {key: value}}; {} when there's no file. Raises SettingsError if broken."""
    path = config_path()
    if not path.is_file():
        return {}
    if tomllib is None:
        raise SettingsError("reading settings needs Python 3.11 or newer - run kit through its launcher (uv)")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SettingsError(f"{path} is not valid TOML: {exc}") from exc
    return {section: values for section, values in data.items() if isinstance(values, dict)}


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        warn(message)


def _load_quietly() -> dict[str, dict[str, Any]]:
    try:
        return load()
    except SettingsError as exc:
        _warn_once(f"{exc} - using defaults")
        return {}


# --- validation ----------------------------------------------------------------------

def check_spec(key: str, spec: object) -> str | None:
    """Why a setting declaration (from tool.json) is invalid, or None if it's fine."""
    if not isinstance(spec, dict):
        return f"setting '{key}' must be an object"
    kind = spec.get("type")
    if kind not in TYPES:
        return f"setting '{key}' has unknown type {kind!r} (use one of: {', '.join(TYPES)})"
    unknown = sorted(set(spec) - SPEC_FIELDS)
    if unknown:
        return f"setting '{key}' has unknown field(s): {', '.join(unknown)}"
    if kind == "choice" and not (isinstance(spec.get("choices"), list) and spec["choices"]):
        return f"setting '{key}' is a choice but has no 'choices' list"
    if "default" in spec:
        problem = validate(spec, spec["default"])
        if problem:
            return f"setting '{key}' default: {problem}"
    return None


def validate(spec: dict[str, Any], value: Any) -> str | None:
    """Why value doesn't fit the setting, or None if it does."""
    kind = spec["type"]
    if kind == "bool" and not isinstance(value, bool):
        return "expected true or false"
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        return "expected a whole number"
    if kind == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return "expected a number"
    if kind == "string" and not isinstance(value, str):
        return "expected text"
    if kind == "choice" and value not in spec["choices"]:
        return f"expected one of: {', '.join(map(str, spec['choices']))}"
    if kind in ("int", "float"):
        if "min" in spec and value < spec["min"]:
            return f"must be at least {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return f"must be at most {spec['max']}"
    return None


def parse_value(spec: dict[str, Any], text: str) -> Any:
    """Turn text (from the command line or an environment variable) into a valid value, or raise SettingsError."""
    kind, raw = spec["type"], text.strip()
    try:
        if kind == "bool":
            lowered = raw.lower()
            if lowered in ("true", "yes", "on", "1"):
                value: Any = True
            elif lowered in ("false", "no", "off", "0"):
                value = False
            else:
                raise ValueError
        elif kind == "int":
            value = int(raw)
        elif kind == "float":
            value = float(raw)
        elif kind == "choice":
            value = next((c for c in spec["choices"] if str(c).lower() == raw.lower()), raw)
        else:
            value = text
    except ValueError:
        raise SettingsError(f"'{text}' is not a valid {kind}") from None
    problem = validate(spec, value)
    if problem:
        raise SettingsError(problem)
    return value


# --- values for kit and tools --------------------------------------------------------

def resolve(section: str, schema: dict[str, dict[str, Any]], data: dict | None = None) -> dict[str, dict[str, Any]]:
    """Every setting's effective value and where it came from: {key: {"value", "source", "spec"}}.

    source is "env", "file" or "default". Invalid stored values are ignored with a warning.
    """
    stored = (data if data is not None else _load_quietly()).get(section, {})
    resolved = {}
    for key, spec in schema.items():
        value, source = spec.get("default"), "default"
        if key in stored:
            problem = validate(spec, stored[key])
            if problem:
                _warn_once(f"ignoring [{section}] {key} in {config_path()}: {problem}")
            else:
                value, source = stored[key], "file"
        env_name = spec.get("env")
        if env_name and os.environ.get(env_name):
            try:
                value, source = parse_value(spec, os.environ[env_name]), "env"
            except SettingsError as exc:
                _warn_once(f"ignoring ${env_name}: {exc}")
        resolved[key] = {"value": value, "source": source, "spec": spec}
    return resolved


def values_for(section: str, schema: dict[str, dict[str, Any]], data: dict | None = None) -> dict[str, Any]:
    return {key: item["value"] for key, item in resolve(section, schema, data).items()}


def tool_schema(tool_dir: Path) -> dict[str, dict[str, Any]]:
    """The "settings" declared in a tool folder's tool.json ({} if none)."""
    try:
        data = json.loads((Path(tool_dir) / "tool.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    settings = data.get("settings") if isinstance(data, dict) else None
    return settings if isinstance(settings, dict) else {}


def tool_settings() -> dict[str, Any]:
    """Settings for the running tool. kit sets KIT_TOOL and KIT_TOOL_DIR before starting a tool."""
    name, folder = os.environ.get("KIT_TOOL"), os.environ.get("KIT_TOOL_DIR")
    if not name or not folder:
        return {}
    return values_for(name, tool_schema(Path(folder)))


def kit_settings() -> dict[str, Any]:
    return values_for("kit", KIT_SCHEMA)


# --- writing (keeps comments and layout) ---------------------------------------------

def _tomlkit():
    try:
        import tomlkit
    except ModuleNotFoundError:
        raise SettingsError("changing settings needs the tomlkit package - run 'uv sync' in the kit folder") from None
    return tomlkit


def _read_document():
    tomlkit = _tomlkit()
    path = config_path()
    if not path.is_file():
        return tomlkit.parse(HEADER)
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # tomlkit raises several parse error types
        raise SettingsError(f"{path} is not valid TOML: {exc}") from exc


def _write_document(document) -> None:
    tomlkit = _tomlkit()
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def set_value(section: str, key: str, value: Any) -> None:
    """Store one setting. Validate it first (validate / parse_value); this only writes."""
    tomlkit = _tomlkit()
    document = _read_document()
    if section not in document:
        document[section] = tomlkit.table()
    document[section][key] = value
    _write_document(document)


def unset_value(section: str, key: str) -> bool:
    """Remove one setting so its default applies again. Returns False if it wasn't set."""
    document = _read_document()
    table = document.get(section)
    if table is None or key not in table:
        return False
    del table[key]
    if not table:
        del document[section]
    _write_document(document)
    return True
