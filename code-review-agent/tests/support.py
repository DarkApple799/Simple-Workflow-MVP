"""测试辅助：可写的临时目录。

不用 tempfile.mkdtemp：在受限环境（如本仓库作者使用的沙箱）里，
mkdtemp 创建目录时带 0o700 权限位，后续写入会被拒绝。
统一改为 os.makedirs + 唯一子目录名，并允许用环境变量 CRA_TEST_TMP 指定根目录。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid


def make_temp_dir(prefix: str = "case-") -> str:
    """创建一个可写的临时目录，返回其绝对路径。"""
    candidates = []
    if os.environ.get("CRA_TEST_TMP"):
        candidates.append(os.environ["CRA_TEST_TMP"])
    candidates.append(os.path.join(tempfile.gettempdir(), "cra-tests"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp_run"))

    last_error: Exception | None = None
    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, f"{prefix}{uuid.uuid4().hex[:8]}")
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".probe")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return path
        except OSError as exc:  # 换下一个候选目录
            last_error = exc
    raise RuntimeError(f"无法创建可写的临时目录：{last_error}")


def remove_temp_dir(path: str) -> None:
    """尽力清理；清理失败不影响测试结论（Windows 上偶发占用）。"""
    shutil.rmtree(path, ignore_errors=True)


class TempDirTestCase(unittest.TestCase):
    """为每个用例准备 self.root 临时目录。"""

    def setUp(self) -> None:
        self.root = make_temp_dir(self.__class__.__name__ + "-")

    def tearDown(self) -> None:
        remove_temp_dir(self.root)
