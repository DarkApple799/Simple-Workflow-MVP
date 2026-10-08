#!/usr/bin/env python3
"""便捷入口：`python webui.py --port 8765` 等价于 `python -m cra web`。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cra.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["web", *sys.argv[1:]]))
