"""测试辅助：临时工作区管理。

**为什么不直接用 `tempfile`**：部分受限环境（含本工程的演示环境）里，
系统临时目录对 Python 进程只有"能建目录、不能在其下建子目录"的权限，
`tempfile.TemporaryDirectory()` + `os.makedirs()` 会直接抛 `PermissionError`。
因此测试统一把临时工作区放在**工程根目录**下的 `.tmp/` 里，既稳定又能被清理。

用法::

    from tests.helpers import temp_workspace, workspace_root

    with temp_workspace("engine") as ws:
        ...  # ws 是一个全新的空目录
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 工程目录加入 sys.path：保证 `python -m unittest discover -s tests -t .` 之外的调用方式也能 import wfa。
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TMP_ROOT = os.path.join(PROJECT_ROOT, ".tmp")


def workspace_root() -> str:
    """返回工程根目录的绝对路径。"""
    return PROJECT_ROOT


@contextlib.contextmanager
def temp_workspace(prefix: str = "case"):
    """产出一个全新的空目录，退出时尽力清理。"""
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, f"{prefix}-{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        # Windows 上文件可能仍被占用，重试几次再放弃。
        for attempt in range(3):
            try:
                shutil.rmtree(path, ignore_errors=False)
                break
            except OSError:
                time.sleep(0.05 * (attempt + 1))
                shutil.rmtree(path, ignore_errors=True)


def write(path: str, content: str) -> str:
    """写一个小文件（自动建父目录），返回绝对路径。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def cleanup_all() -> None:
    """清空整个 `.tmp/`（测试收尾或脚本结束时调用）。"""
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


__all__ = ["PROJECT_ROOT", "TMP_ROOT", "temp_workspace", "workspace_root", "write", "cleanup_all"]
