"""工具层测试：参数校验、路径安全、边界文件、执行结果语义。"""

from __future__ import annotations

import json
import os
import unittest

from cra.config import Config
from cra.scanner import scan_workspace
from cra.tools import build_default_registry, validate_arguments
from cra.tools.base import ToolContext
from tests.support import TempDirTestCase


class TestArgumentValidation(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_lines": {"type": "integer"},
            "ignore_case": {"type": "boolean"},
            "patterns": {"type": "array"},
        },
        "required": ["path"],
    }

    def test_string_number_is_coerced(self) -> None:
        cleaned, errors = validate_arguments(self.SCHEMA, {"path": "a.py", "max_lines": "10"})
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["max_lines"], 10)

    def test_missing_required_is_reported(self) -> None:
        _, errors = validate_arguments(self.SCHEMA, {"max_lines": 1})
        self.assertTrue(any("path" in error for error in errors))

    def test_unknown_argument_is_reported(self) -> None:
        _, errors = validate_arguments(self.SCHEMA, {"path": "a.py", "typo": 1})
        self.assertTrue(any("typo" in error for error in errors))

    def test_array_from_string(self) -> None:
        cleaned, errors = validate_arguments(self.SCHEMA, {"path": "a.py", "patterns": "*.py,*.js"})
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["patterns"], ["*.py", "*.js"])

    def test_boolean_from_string(self) -> None:
        cleaned, _ = validate_arguments(self.SCHEMA, {"path": "a.py", "ignore_case": "true"})
        self.assertTrue(cleaned["ignore_case"])


class ToolTestBase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = Config(api_key="", max_read_lines=50)
        self.registry = build_default_registry()
        self.ctx = ToolContext(root=self.root, cfg=self.cfg)
        with open(os.path.join(self.root, "sample.py"), "w", encoding="utf-8") as handle:
            handle.write("import os\n\n\ndef f(x):\n    return x == None\n")
        with open(os.path.join(self.root, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello\nworld\n")
        with open(os.path.join(self.root, "blob.bin"), "wb") as handle:
            handle.write(b"\x00\x01\x02binary")
        with open(os.path.join(self.root, "empty.py"), "w", encoding="utf-8"):
            pass
        self.ctx.scan = scan_workspace(self.root, self.cfg)

    def call(self, name: str, **arguments):
        return self.registry.execute(name, arguments, self.ctx, step=1)


class TestFileTools(ToolTestBase):
    def test_list_files(self) -> None:
        result = self.call("list_files", path=".", pattern="*.py")
        self.assertTrue(result.ok)
        self.assertIn("sample.py", result.output)
        self.assertNotIn("blob.bin", result.output)

    def test_read_file_has_line_numbers(self) -> None:
        result = self.call("read_file", path="sample.py")
        self.assertTrue(result.ok)
        self.assertIn("    1 | import os", result.output)

    def test_read_file_window(self) -> None:
        result = self.call("read_file", path="sample.py", start_line=4, max_lines=2)
        self.assertIn("    4 |", result.output)
        self.assertNotIn("    1 |", result.output)

    def test_read_binary_gives_actionable_error(self) -> None:
        result = self.call("read_file", path="blob.bin")
        self.assertFalse(result.ok)
        self.assertIn("二进制", result.error)

    def test_read_empty_file(self) -> None:
        result = self.call("read_file", path="empty.py")
        self.assertFalse(result.ok)
        self.assertIn("空文件", result.error)

    def test_read_directory_is_rejected(self) -> None:
        result = self.call("read_file", path=".")
        self.assertFalse(result.ok)

    def test_path_traversal_is_denied(self) -> None:
        result = self.call("read_file", path="../../etc/passwd")
        self.assertFalse(result.ok)
        self.assertIn("越界", result.error)

    def test_search_code(self) -> None:
        result = self.call("search_code", pattern="== None")
        self.assertTrue(result.ok)
        self.assertIn("sample.py:5", result.output)

    def test_search_code_invalid_regex(self) -> None:
        result = self.call("search_code", pattern="[*")
        self.assertFalse(result.ok)
        self.assertIn("正则", result.error)

    def test_search_code_no_match_explains_scope(self) -> None:
        result = self.call("search_code", pattern="zzz_not_found")
        self.assertTrue(result.ok)
        self.assertIn("没有匹配", result.output)

    def test_file_stats(self) -> None:
        result = self.call("file_stats", path=".")
        self.assertTrue(result.ok)
        self.assertIn("文件数", result.output)


class TestAnalysisTools(ToolTestBase):
    def test_analyze_python_returns_rules(self) -> None:
        result = self.call("analyze_python", path="sample.py")
        self.assertTrue(result.ok)
        payload = json.loads(result.output)
        self.assertIn("BUG004", {item["rule_id"] for item in payload["rule_findings"]})
        self.assertEqual(payload["file"], "sample.py")

    def test_analyze_python_on_syntax_error_still_returns(self) -> None:
        with open(os.path.join(self.root, "broken.py"), "w", encoding="utf-8") as handle:
            handle.write("def f(:\n")
        result = self.call("analyze_python", path="broken.py")
        self.assertTrue(result.ok)
        payload = json.loads(result.output)
        self.assertIn("SYN001", {item["rule_id"] for item in payload["rule_findings"]})

    def test_scan_directory(self) -> None:
        result = self.call("scan_directory", path=".")
        payload = json.loads(result.output)
        self.assertGreaterEqual(payload["findings_total"], 1)
        self.assertIn("hotspot_files", payload)

    def test_scan_directory_call_limit(self) -> None:
        for _ in range(4):
            result = self.call("scan_directory", path=".")
        self.assertIn("不再扫描", result.output)


class TestReviewTools(ToolTestBase):
    def test_submit_review_stores_submission(self) -> None:
        result = self.call(
            "submit_review",
            summary="整体可用，但异常处理有问题。",
            verdict="request_changes",
            findings=[
                {
                    "title": "裸 except",
                    "file": "sample.py",
                    "line": 3,
                    "severity": "medium",
                    "category": "bug",
                    "detail": "会吞掉 KeyboardInterrupt",
                    "suggestion": "改为 except Exception",
                }
            ],
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.ctx.submissions), 1)
        self.assertEqual(self.ctx.submissions[0]["verdict"], "request_changes")
        self.assertEqual(result.meta["submitted"], 1)

    def test_submit_review_requires_summary(self) -> None:
        result = self.call("submit_review", summary="", findings=[])
        self.assertFalse(result.ok)

    def test_submit_review_requires_list(self) -> None:
        result = self.call("submit_review", summary="ok", findings={"title": "x"})
        self.assertFalse(result.ok)

    def test_submit_review_accepts_empty_findings(self) -> None:
        result = self.call("submit_review", summary="没有发现问题", findings=[])
        self.assertTrue(result.ok)

    def test_submit_review_is_registered_as_terminal(self) -> None:
        self.assertTrue(self.registry.get("submit_review").terminal)


class TestRegistry(unittest.TestCase):
    def test_expected_tools_registered(self) -> None:
        registry = build_default_registry()
        self.assertEqual(
            registry.names(),
            [
                "analyze_python",
                "file_stats",
                "list_files",
                "read_file",
                "scan_directory",
                "search_code",
                "submit_review",
            ],
        )

    def test_unknown_tool_returns_failure_not_exception(self) -> None:
        registry = build_default_registry()
        ctx = ToolContext(root=".", cfg=Config())
        result = registry.execute("nope", {}, ctx)
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.error)

    def test_schemas_are_openai_compatible(self) -> None:
        for schema in build_default_registry().schemas():
            self.assertEqual(schema["type"], "function")
            self.assertIn("parameters", schema["function"])

    def test_trace_records_every_call(self) -> None:
        registry = build_default_registry()
        ctx = ToolContext(root=".", cfg=Config())
        registry.execute("nope", {}, ctx, step=2)
        self.assertEqual(len(registry.traces), 1)
        self.assertEqual(registry.traces[0].step, 2)


class TestSingleFileScope(TempDirTestCase):
    """单文件审查模式：所有工具都必须被限制在那一个文件上。

    背景（真实 bug）：早期版本只收窄了"扫描范围"，工具作用域仍是目标文件所在目录，
    于是 `review.py 某个文件` 会让 Agent 顺手把同目录（甚至整个桌面）的其他文件读进模型上下文。
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = Config(api_key="")
        self.registry = build_default_registry()
        self.target = os.path.join(self.root, "target.c")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("int main(void) { return 0; }\n")
        # 邻居文件：既不该被列出，也不该被读到
        with open(os.path.join(self.root, "secret.txt"), "w", encoding="utf-8") as handle:
            handle.write("API_KEY=super-secret-value\n")
        with open(os.path.join(self.root, "other.py"), "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")
        self.ctx = ToolContext(root=self.root, cfg=self.cfg, scope_file="target.c")
        self.ctx.scan = scan_workspace(self.root, self.cfg, only_file=self.target)

    def call(self, name: str, **arguments):
        return self.registry.execute(name, arguments, self.ctx, step=1)

    def test_scan_records_scope_file(self) -> None:
        self.assertEqual(self.ctx.scan.scope_file, "target.c")
        self.assertEqual(len(self.ctx.scan.files), 1)

    def test_named_tools_see_scope(self) -> None:
        self.assertTrue(hasattr(self.ctx, "scope_file"))

    def test_reading_target_is_allowed(self) -> None:
        result = self.call("read_file", path="target.c")
        self.assertTrue(result.ok)
        self.assertIn("int main", result.output)

    def test_reading_neighbour_is_rejected(self) -> None:
        result = self.call("read_file", path="secret.txt")
        self.assertFalse(result.ok)
        self.assertIn("单个文件", result.error)

    def test_path_traversal_still_rejected(self) -> None:
        result = self.call("read_file", path="../../etc/passwd")
        self.assertFalse(result.ok)

    def test_list_files_hides_other_files(self) -> None:
        result = self.call("list_files")
        self.assertTrue(result.ok)
        self.assertIn("target.c", result.output)
        self.assertNotIn("secret.txt", result.output)

    def test_search_code_stays_inside_scope(self) -> None:
        result = self.call("search_code", pattern="API_KEY")
        self.assertTrue(result.ok)
        self.assertNotIn("secret.txt", result.output)
        self.assertIn("没有匹配", result.output)

    def test_file_stats_is_scoped(self) -> None:
        result = self.call("file_stats")
        self.assertTrue(result.ok)
        self.assertIn("单文件审查模式", result.output)
        self.assertNotIn("secret.txt", result.output)

    def test_analyze_python_on_other_file_is_rejected(self) -> None:
        result = self.call("analyze_python", path="other.py")
        self.assertFalse(result.ok)
        self.assertIn("单个文件", result.error)

    def test_analyze_python_refuses_non_python_source(self) -> None:
        """C/JS 文件不该喂给 Python 解析器（否则只会吐一个'语法错误'假发现）。"""
        result = self.call("analyze_python", path="target.c")
        self.assertFalse(result.ok)
        self.assertIn("不是 Python 源码", result.error)

    def test_scan_directory_ignores_path_argument(self) -> None:
        result = self.call("scan_directory", path=".")
        self.assertTrue(result.ok)
        payload = json.loads(result.output)
        self.assertEqual(payload["files_scanned"], 1)

    def test_without_scope_everything_is_still_reachable(self) -> None:
        plain = ToolContext(root=self.root, cfg=self.cfg)
        self.assertTrue(self.registry.execute("read_file", {"path": "secret.txt"}, plain, step=1).ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
