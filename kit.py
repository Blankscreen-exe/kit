#!/usr/bin/env python3
"""Entry point for kit. The launchers in bin/ call this; running it directly works too."""

import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("kit needs Python 3.10+ (found %d.%d)" % sys.version_info[:2])

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from core.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
