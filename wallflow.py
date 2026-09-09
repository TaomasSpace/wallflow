#!/usr/bin/env python3
"""Entry point — works from a git checkout or from ~/.local/share/wallflow."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wallflow.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
