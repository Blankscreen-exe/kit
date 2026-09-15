"""'kit new': creates a tool folder with a starter entry point and README."""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.registry import MANIFEST, README, TOOLS_DIR

NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class ScaffoldError(Exception):
    pass


_PY = '''\
"""kit tool: __NAME__. Documented in README.md next to this file (see: kit help __NAME__)."""

import argparse

from kitlib import style


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit __NAME__", description=__SUMMARY_LITERAL__)
    parser.add_argument("name", nargs="?", default="world", help="who to greet")
    args = parser.parse_args()

    print(f"Hello, {style(args.name, 'bold')}!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_PS1 = """\
<#
.SYNOPSIS
    __SUMMARY__
#>
param(
    [string]$Name = 'world'
)

$ErrorActionPreference = 'Stop'

Write-Output "Hello, $Name!"
"""

_SH = """\
#!/usr/bin/env bash
# __SUMMARY__
set -euo pipefail

name="${1:-world}"
echo "Hello, ${name}!"
"""

_README = """\
# __NAME__

__SUMMARY__

## Usage

```
kit __NAME__ [name]
```

## Examples

```
kit __NAME__ there    # prints: Hello, there!
```
"""

# lang -> (entry file name, template)
TEMPLATES = {"py": ("main.py", _PY), "ps1": ("main.ps1", _PS1), "sh": ("main.sh", _SH)}


def create_tool(name: str, lang: str, summary: str, category: str, aliases: list[str], taken: set[str]) -> Path:
    if not NAME_RE.fullmatch(name):
        raise ScaffoldError(f"invalid tool name '{name}': use lowercase letters, digits and dashes (e.g. git-clean)")
    for candidate in [name, *aliases]:
        if candidate in taken:
            raise ScaffoldError(f"'{candidate}' is already a built-in command, tool or alias")
    directory = TOOLS_DIR / name
    if directory.exists():
        raise ScaffoldError(f"{directory} already exists")

    def fill(template: str) -> str:
        return (
            template.replace("__NAME__", name)
            .replace("__SUMMARY_LITERAL__", json.dumps(summary))
            .replace("__SUMMARY__", summary)
        )

    filename, template = TEMPLATES[lang]
    directory.mkdir(parents=True)
    entry = directory / filename
    if lang == "ps1":
        # BOM + CRLF so Windows PowerShell 5.1 reads non-ASCII text correctly
        entry.write_text(fill(template), encoding="utf-8-sig", newline="\r\n")
    else:
        entry.write_text(fill(template), encoding="utf-8", newline="\n")
    if lang == "sh":
        entry.chmod(0o755)

    (directory / README).write_text(fill(_README), encoding="utf-8", newline="\n")

    extras: dict[str, object] = {}
    if category != "misc":
        extras["category"] = category
    if aliases:
        extras["aliases"] = aliases
    if extras:
        (directory / MANIFEST).write_text(json.dumps(extras, indent=2) + "\n", encoding="utf-8", newline="\n")
    return directory
