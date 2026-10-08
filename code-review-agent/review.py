#!/usr/bin/env python3
"""便捷入口：`python review.py <路径>` 等价于 `python -m cra review <路径>`。

也支持直接透传子命令：`python review.py chat examples`、`python review.py web`。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cra.cli import main  # noqa: E402  (需要先补 sys.path)

SUBCOMMANDS = {"review", "scan", "chat", "web"}
GLOBAL_FLAGS = {"-h", "--help", "--version"}


def run() -> int:
    """容错分发：子命令写在路径前面或后面都认，没写子命令就默认 review。

    支持这些写法：
        review.py examples                     -> review examples
        review.py examples scan --top 5        -> scan examples --top 5
        review.py scan examples                -> scan examples
        review.py --version                    -> 顶层 --version
    """
    argv = sys.argv[1:]
    if not argv:
        return main(["review"])

    # 只看第一个选项之前的"位置参数"，避免把 `--out scan` 这种取值误判成子命令
    head: list[str] = []
    for arg in argv:
        if arg.startswith("-"):
            break
        head.append(arg)

    for index, token in enumerate(head):
        if token in SUBCOMMANDS:
            if index > 0:  # 把子命令提到最前面，其余顺序保持不变
                argv = [token, *argv[:index], *argv[index + 1 :]]
            return main(argv)

    if argv[0] in GLOBAL_FLAGS:
        return main(argv)
    return main(["review", *argv])


if __name__ == "__main__":
    raise SystemExit(run())
