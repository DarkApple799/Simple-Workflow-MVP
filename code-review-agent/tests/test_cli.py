"""CLI 层的交互防护测试。

场景来源：用户在 `chat` 的 `你 >` 提示符下粘贴 `python review.py ... --out r.md`，
被当成问题发给了模型——白烧 token 且答非所问。这里守住这条边界。
"""

from __future__ import annotations

import unittest

from cra.cli import looks_like_shell_command


class TestShellCommandGuard(unittest.TestCase):
    def test_detects_pasted_shell_commands(self) -> None:
        cases = [
            'python review.py "C:\\Users\\Lenovo\\Desktop\\session_service.c" --out "%USERPROFILE%\\Desktop\\c_report.md"',
            "python review.py examples",
            "python webui.py --port 9000",
            "python -m cra scan .",
            "py review.py scan examples",
            "review.cmd examples",
            "start-web.cmd",
            "./review.py examples",
            "cd /d G:\\DeepSeek工程\\code-review-agent",
            "git push",
            "git status",
            "pip install langchain",
            "taskkill /PID 5136 /F",
            "> python review.py .",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(looks_like_shell_command(text), text)

    def test_natural_questions_are_not_blocked(self) -> None:
        cases = [
            "python 的 GIL 是什么意思？",
            "buggy_service.py 有什么安全问题？",
            "帮我看看 read_file 这个工具是怎么实现的",
            "gitpython 和 git 命令有什么区别？",
            "这段代码里有没有内存泄漏",
            "review.py 是怎么组织的",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(looks_like_shell_command(text), text)

    def test_empty_input(self) -> None:
        self.assertFalse(looks_like_shell_command(""))
        self.assertFalse(looks_like_shell_command("   "))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
