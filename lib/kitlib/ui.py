"""Window or terminal: one rule for every kit tool that has both.

A tool shows its window (or web page) only where there's a desktop to show it on, and otherwise
does the same job in the terminal - so it works the same over SSH or on a server with no display.
`--ui` / `--no-ui` override the guess either way:

    add_ui_flags(parser)
    args = parser.parse_args()
    if want_ui(args.ui):
        ...open the window...
"""

from __future__ import annotations

import argparse

from kitlib.browser import no_display


def add_ui_flags(parser: argparse.ArgumentParser, *, ui_help: str = "show the window, even where kit would use the terminal",
                 no_ui_help: str = "terminal only: no window", ui_aliases: tuple[str, ...] = (),
                 no_ui_aliases: tuple[str, ...] = ()) -> None:
    """Add --ui / --no-ui (as args.ui: True, False, or None for "decide by the display").

    Aliases are older flags that meant the same thing; they keep working but stay out of --help.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ui", dest="ui", action="store_const", const=True, help=ui_help)
    group.add_argument("--no-ui", dest="ui", action="store_const", const=False, help=no_ui_help)
    for alias in ui_aliases:
        group.add_argument(alias, dest="ui", action="store_const", const=True, help=argparse.SUPPRESS)
    for alias in no_ui_aliases:
        group.add_argument(alias, dest="ui", action="store_const", const=False, help=argparse.SUPPRESS)
    parser.set_defaults(ui=None)


def want_ui(choice: bool | None, default: bool = True) -> bool:
    """Whether to show the window: the flag if one was given, else `default` where there's a display.

    `default` is the tool's own preference when nothing was said (a setting, say); a display is
    still required for it - an explicit --ui is the only way past that check.
    """
    if choice is not None:
        return choice
    return default and not no_display()


def why_no_ui() -> str:
    """The reason in words, for a tool that has to say it's staying in the terminal."""
    return "no display here (an SSH session, or no desktop)"
