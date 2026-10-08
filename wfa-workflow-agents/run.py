"""wfa 便捷入口：`python run.py <子命令>`。

用法示例::

    python run.py list
    python run.py run --workflow code_review --offline --verbose
    python run.py run --workflow docs --task "写一份多 Agent 协作说明"
    python run.py graph --workflow code_review --format md --out docs/FLOW.md
    python run.py report --json docs/run_report.json

本文件只做三件事：把项目目录放进 `sys.path`、修正工作目录、调用 `wfa.cli.main`。
"""

from __future__ import annotations

import os
import sys

#: 让脚本在任意工作目录下都能 import wfa（例如在 C 盘用绝对路径调用）。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from wfa.cli import main  # noqa: E402  （必须在 sys.path 调整之后导入）

if __name__ == "__main__":
    raise SystemExit(main())
