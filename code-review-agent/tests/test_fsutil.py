"""文件系统层的边界情况测试。

这一层的 bug 最容易"静默"：读错编码、把二进制当文本、路径越界。
全部用临时目录验证，不依赖仓库里的任何文件。
"""

from __future__ import annotations

import os
import unittest

from cra.config import Config
from cra.errors import PathSecurityError
from cra.fsutil import (
    classify_file,
    decode_bytes,
    human_size,
    looks_binary,
    read_text_file,
    relpath,
    safe_path,
    walk_files,
)
from tests.support import TempDirTestCase


class TestDecoding(unittest.TestCase):
    def test_plain_utf8_is_not_reported_as_bom(self) -> None:
        text, encoding, lossy = decode_bytes(b"hello world")
        self.assertEqual(text, "hello world")
        self.assertEqual(encoding, "utf-8")
        self.assertFalse(lossy)

    def test_utf8_bom_detected(self) -> None:
        text, encoding, _ = decode_bytes("中文内容".encode("utf-8-sig"))
        self.assertEqual(text, "中文内容")
        self.assertIn("BOM", encoding)

    def test_gbk_fallback(self) -> None:
        text, encoding, lossy = decode_bytes("中文注释：旧系统".encode("gbk"))
        self.assertEqual(text, "中文注释：旧系统")
        self.assertIn(encoding, ("gb18030", "gbk"))
        self.assertFalse(lossy)

    def test_undecodable_bytes_do_not_crash(self) -> None:
        text, encoding, lossy = decode_bytes(b"\xff\xfe\x81\x8d\x00\x41")
        self.assertIsInstance(text, str)
        self.assertIsInstance(encoding, str)
        self.assertFalse(text == "" and encoding == "")

    def test_binary_detection(self) -> None:
        self.assertTrue(looks_binary(b"\x00\x01\x02"))
        self.assertFalse(looks_binary("正常文本".encode("utf-8")))


class TestPathSafety(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.root, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")

    def test_relative_path_inside_root(self) -> None:
        path = safe_path(self.root, "a.py")
        self.assertTrue(path.endswith("a.py"))

    def test_parent_traversal_is_blocked(self) -> None:
        with self.assertRaises(PathSecurityError):
            safe_path(self.root, "../secret.txt")

    def test_absolute_path_outside_root_is_blocked(self) -> None:
        outside = os.path.abspath(os.path.join(self.root, "..", "outside.txt"))
        with self.assertRaises(PathSecurityError):
            safe_path(self.root, outside)

    def test_missing_file_reports_clean_error(self) -> None:
        with self.assertRaises(PathSecurityError):
            safe_path(self.root, "missing.py")


class TestClassification(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = Config()

    def _write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_empty_file(self) -> None:
        info = classify_file(self._write("empty.py", b""), self.cfg)
        self.assertEqual(info.kind, "empty")

    def test_binary_file(self) -> None:
        info = classify_file(self._write("blob.bin", b"\x89PNG\r\n\x1a\n\x00\x01\x02"), self.cfg)
        self.assertEqual(info.kind, "binary")

    def test_oversized_file_is_flagged_not_analyzed(self) -> None:
        cfg = Config(max_file_bytes=10)
        info = classify_file(self._write("big.py", b"x = 1\n" * 100), cfg)
        self.assertEqual(info.kind, "too_large")

    def test_python_and_text_detection(self) -> None:
        self.assertEqual(classify_file(self._write("m.py", b"x = 1\n"), self.cfg).kind, "python")
        self.assertEqual(classify_file(self._write("n.md", b"# hi\n"), self.cfg).kind, "text")
        self.assertEqual(classify_file(self._write("s.js", b"var a = 1;\n"), self.cfg).kind, "code")

    def test_truncated_read_is_reported(self) -> None:
        path = self._write("long.txt", b"a" * 5000)
        text, _, _, truncated = read_text_file(path, max_bytes=100)
        self.assertEqual(len(text), 100)
        self.assertTrue(truncated)


class TestWalk(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = Config()
        for name in ("a.py", "b.py"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
        for ignored in ("__pycache__", "node_modules", ".git"):
            directory = os.path.join(self.root, ignored)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "junk.py"), "w", encoding="utf-8") as handle:
                handle.write("y = 2\n")

    def test_ignored_directories_are_skipped(self) -> None:
        files, skipped, _, _ = walk_files(self.root, self.cfg)
        rels = [relpath(self.root, path) for path in files]
        self.assertEqual(sorted(rels), ["a.py", "b.py"])
        reasons = {item["path"] for item in skipped}
        self.assertIn("__pycache__/", reasons)

    def test_max_files_truncates(self) -> None:
        cfg = Config(max_files=1)
        files, _, _, truncated = walk_files(self.root, cfg)
        self.assertEqual(len(files), 1)
        self.assertTrue(truncated)

    def test_include_filter(self) -> None:
        cfg = Config(include=("*.py",))
        files, _, _, _ = walk_files(self.root, cfg)
        self.assertEqual(len(files), 2)

    def test_exclude_filter(self) -> None:
        cfg = Config(exclude=("b.py",))
        files, _, _, _ = walk_files(self.root, cfg)
        self.assertEqual([relpath(self.root, path) for path in files], ["a.py"])

    def test_human_size(self) -> None:
        self.assertEqual(human_size(512), "512B")
        self.assertEqual(human_size(2048), "2.0KB")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
