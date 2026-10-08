"""`wfa/util.py` 单元测试：JSON 提取、路径安全、文本读写、重试策略。

覆盖点（对应 CONTRACT 第 2 节）：
    1. ``extract_json_object`` / ``extract_json_array`` 的脏数据场景：```json 代码块包裹、
       前后夹解释性文字、尾随逗号、字符串内部的括号、英文撇号（`Here's ...`）、嵌套结构、
       非目标类型与非法输入一律返回 None；
    2. ``RetryPolicy`` 只重试 ``LLMError.retryable=True`` 的异常（``LLMServerError`` 可重试、
       ``LLMResponseError`` 不可重试），``attempts`` / ``retries`` 计数与指数退避节奏正确，
       非 LLM 异常（程序 bug）立即抛出不做无谓等待；
    3. ``safe_join`` 防路径越界：``..`` 逃逸、同前缀兄弟目录、NUL、空路径 → ``PathSecurityError``；
    4. ``truncate`` / ``sha1_text`` / ``write_text`` / ``read_text`` 的边界行为
       （长度上限、UTF-8 无 BOM、GB18030 读取、max_bytes 截断、缺失文件抛 ToolError）。

全部离线、无真实等待：``RetryPolicy`` 的 ``sleep`` 注入为记录器。
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import temp_workspace
from wfa.errors import (
    LLMResponseError,
    LLMServerError,
    PathSecurityError,
    ToolError,
)
from wfa.util import (
    RetryPolicy,
    extract_json_array,
    extract_json_object,
    new_id,
    now_iso,
    read_text,
    safe_join,
    safe_relpath,
    sha1_text,
    truncate,
    write_text,
)


class ExtractJsonObjectTest(unittest.TestCase):
    """从自由文本中抠 JSON 对象。"""

    def test_plain_object(self):
        self.assertEqual(extract_json_object('{"a": 1, "b": [1, 2]}'), {"a": 1, "b": [1, 2]})

    def test_with_surrounding_prose(self):
        text = '好的，结果如下：\n{"files": {"a.py": "print(1)"}}\n以上。'
        self.assertEqual(extract_json_object(text), {"files": {"a.py": "print(1)"}})

    def test_fenced_json_block(self):
        text = '```json\n{"verdict": "pass", "counts": {"high": 0}}\n```'
        self.assertEqual(extract_json_object(text), {"verdict": "pass", "counts": {"high": 0}})

    def test_fenced_block_without_language(self):
        text = '```\n{"ok": true}\n```'
        self.assertEqual(extract_json_object(text), {"ok": True})

    def test_trailing_comma_is_repaired(self):
        self.assertEqual(extract_json_object('{"a": 1, "b": 2,}'), {"a": 1, "b": 2})
        self.assertEqual(extract_json_object('前言\n{"a": [1, 2,],}\n后记'), {"a": [1, 2]})

    def test_braces_inside_string_do_not_confuse_scanner(self):
        text = 'prefix {"a": "}"} suffix'
        self.assertEqual(extract_json_object(text), {"a": "}"})

    def test_escaped_quote_inside_string(self):
        text = r'{"msg": "he said \"hi\"", "n": 2}'
        self.assertEqual(extract_json_object(text), {"msg": 'he said "hi"', "n": 2})

    def test_apostrophe_does_not_enter_string_state(self):
        # Homework1 踩过的坑：撇号曾被当成字符串定界符，导致后面的花括号被整段忽略。
        text = 'Here\'s the result: {"a": 1}'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_nested_object_is_returned_as_a_whole(self):
        text = '{"outer": {"inner": {"deep": 1}}}'
        self.assertEqual(extract_json_object(text), {"outer": {"inner": {"deep": 1}}})

    def test_first_object_wins(self):
        text = '{"first": 1} 以及 {"second": 2}'
        self.assertEqual(extract_json_object(text), {"first": 1})

    def test_non_object_json_returns_none(self):
        self.assertIsNone(extract_json_object("[1, 2, 3]"))
        self.assertIsNone(extract_json_object('"just a string"'))
        self.assertIsNone(extract_json_object("42"))

    def test_invalid_or_empty_input_returns_none(self):
        self.assertIsNone(extract_json_object(""))
        self.assertIsNone(extract_json_object("   \n  "))
        self.assertIsNone(extract_json_object("完全不是 JSON 的一段话"))
        self.assertIsNone(extract_json_object("{不是合法 JSON"))
        self.assertIsNone(extract_json_object(None))


class ExtractJsonArrayTest(unittest.TestCase):
    """从自由文本中抠 JSON 数组。"""

    def test_plain_array(self):
        self.assertEqual(extract_json_array("[1, 2, 3]"), [1, 2, 3])

    def test_with_surrounding_prose(self):
        self.assertEqual(extract_json_array("结果如下：[{\"id\": 1}, {\"id\": 2}] 完毕"), [{"id": 1}, {"id": 2}])

    def test_fenced_block(self):
        self.assertEqual(extract_json_array('```json\n["a", "b"]\n```'), ["a", "b"])

    def test_trailing_comma_is_repaired(self):
        self.assertEqual(extract_json_array('["a", "b",]'), ["a", "b"])

    def test_brackets_inside_string(self):
        self.assertEqual(extract_json_array('["a[0]", "b]"]'), ["a[0]", "b]"])

    def test_nested_arrays(self):
        self.assertEqual(extract_json_array("[[1, 2], [3]]"), [[1, 2], [3]])

    def test_non_array_returns_none(self):
        self.assertIsNone(extract_json_array('{"a": 1}'))
        self.assertIsNone(extract_json_array(""))

    def test_invalid_input_returns_none(self):
        self.assertIsNone(extract_json_array("[1, 2"))
        self.assertIsNone(extract_json_array(None))


class TimeAndIdTest(unittest.TestCase):
    """时间与 ID 的基本形态。"""

    def test_now_iso_format(self):
        self.assertRegex(now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_new_id_prefix_and_uniqueness(self):
        first = new_id("msg")
        self.assertTrue(first.startswith("msg-"))
        self.assertEqual(len(first), len("msg-") + 8)
        self.assertEqual(len({new_id("msg") for _ in range(100)}), 100)

    def test_new_id_default_prefix(self):
        self.assertTrue(new_id().startswith("id-"))


class TruncateTest(unittest.TestCase):
    """文本截断：结果长度不超过上限。"""

    def test_short_text_unchanged(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_exact_length_unchanged(self):
        self.assertEqual(truncate("abcd", 4), "abcd")

    def test_long_text_truncated_with_suffix(self):
        result = truncate("a" * 100, 10)
        self.assertEqual(len(result), 10)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(result, "a" * 9 + "…")

    def test_custom_suffix(self):
        result = truncate("a" * 50, 8, suffix="...")
        self.assertEqual(len(result), 8)
        self.assertTrue(result.endswith("..."))

    def test_limit_one_keeps_only_suffix(self):
        self.assertEqual(truncate("abcdef", 1), "…")

    def test_none_becomes_empty_string(self):
        self.assertEqual(truncate(None), "")

    def test_non_string_is_stringified(self):
        self.assertEqual(truncate(12345, 10), "12345")


class Sha1TextTest(unittest.TestCase):
    """内容指纹：稳定、定长、对内容敏感。"""

    def test_length_and_determinism(self):
        digest = sha1_text("hello")
        self.assertEqual(len(digest), 10)
        self.assertEqual(digest, sha1_text("hello"))
        self.assertRegex(digest, r"^[0-9a-f]{10}$")

    def test_different_content_different_digest(self):
        self.assertNotEqual(sha1_text("hello"), sha1_text("hello!"))
        self.assertNotEqual(sha1_text(""), sha1_text(" "))

    def test_none_equals_empty(self):
        self.assertEqual(sha1_text(None), sha1_text(""))

    def test_unicode_is_stable(self):
        self.assertEqual(sha1_text("中文内容"), sha1_text("中文内容"))
        self.assertEqual(len(sha1_text("中文内容")), 10)


class SafeJoinTest(unittest.TestCase):
    """路径安全防线：工作区内的相对路径放行，越界一律拒绝。"""

    def test_normal_relative_path(self):
        with temp_workspace("util-join") as ws:
            target = safe_join(ws, os.path.join("pkg", "mod.py"))
            self.assertEqual(os.path.abspath(target), os.path.join(ws, "pkg", "mod.py"))

    def test_forward_slash_relative_path(self):
        with temp_workspace("util-join-fs") as ws:
            target = safe_join(ws, "sub/dir/file.txt")
            self.assertEqual(os.path.abspath(target), os.path.join(ws, "sub", "dir", "file.txt"))

    def test_absolute_path_inside_workspace_is_allowed(self):
        with temp_workspace("util-join-abs-in") as ws:
            inside = os.path.join(ws, "a.txt")
            self.assertEqual(os.path.abspath(safe_join(ws, inside)), inside)

    def test_quotes_are_stripped(self):
        with temp_workspace("util-join-quote") as ws:
            target = safe_join(ws, '"a.txt"')
            self.assertEqual(os.path.basename(target), "a.txt")

    def test_dotdot_escape_rejected(self):
        with temp_workspace("util-join-dotdot") as ws:
            for rel in ("../evil.txt", "sub/../../evil.txt", "..\\..\\evil.txt",
                        "sub\\..\\..\\out.txt"):
                with self.subTest(rel=rel):
                    with self.assertRaises(PathSecurityError):
                        safe_join(ws, rel)

    def test_absolute_path_outside_rejected(self):
        with temp_workspace("util-join-abs-out") as ws:
            outside = os.path.join(os.path.dirname(ws), "outside.txt")
            with self.assertRaises(PathSecurityError):
                safe_join(ws, outside)

    def test_sibling_with_shared_prefix_rejected(self):
        # C:\ws 不能把 C:\ws-evil 误判为子路径（不能用字符串 startswith 做前缀比较）。
        with temp_workspace("util-join-prefix") as ws:
            sibling = ws + "-evil"
            os.makedirs(sibling, exist_ok=True)
            try:
                with self.assertRaises(PathSecurityError):
                    safe_join(ws, "../" + os.path.basename(sibling) + "/file.txt")
            finally:
                try:
                    os.rmdir(sibling)
                except OSError:
                    pass

    def test_empty_path_rejected(self):
        with temp_workspace("util-join-empty") as ws:
            for rel in ("", "   ", None):
                with self.subTest(rel=rel):
                    with self.assertRaises(PathSecurityError):
                        safe_join(ws, rel)

    def test_nul_character_rejected(self):
        with temp_workspace("util-join-nul") as ws:
            with self.assertRaises(PathSecurityError):
                safe_join(ws, "a\x00b.txt")

    def test_safe_relpath_uses_forward_slashes(self):
        with temp_workspace("util-relpath") as ws:
            nested = os.path.join(ws, "a", "b.txt")
            self.assertEqual(safe_relpath(ws, nested), "a/b.txt")
            self.assertEqual(safe_relpath(ws, ""), "")


class TextIoTest(unittest.TestCase):
    """文件读写：UTF-8 无 BOM、自动建目录、多编码容忍、超限截断。"""

    def test_write_then_read_roundtrip(self):
        with temp_workspace("util-io") as ws:
            path = os.path.join(ws, "deep", "nested", "x.txt")
            write_text(path, "第一行\n第二行\n")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(read_text(path), "第一行\n第二行\n")

    def test_written_bytes_have_no_bom_and_lf_endings(self):
        with temp_workspace("util-io-bytes") as ws:
            path = os.path.join(ws, "x.txt")
            write_text(path, "a\nb")
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(raw, b"a\nb")

    def test_read_text_truncates_to_max_bytes(self):
        with temp_workspace("util-io-trunc") as ws:
            path = os.path.join(ws, "big.txt")
            write_text(path, "x" * 100)
            self.assertEqual(len(read_text(path, max_bytes=10)), 10)

    def test_read_text_handles_utf8_bom(self):
        with temp_workspace("util-io-bom") as ws:
            path = os.path.join(ws, "bom.txt")
            with open(path, "wb") as handle:
                handle.write("\ufeffhello".encode("utf-8"))
            self.assertEqual(read_text(path), "hello")

    def test_read_text_decodes_gb18030(self):
        with temp_workspace("util-io-gbk") as ws:
            path = os.path.join(ws, "gbk.txt")
            with open(path, "wb") as handle:
                handle.write("中文内容".encode("gb18030"))
            self.assertEqual(read_text(path), "中文内容")

    def test_read_missing_file_raises_tool_error(self):
        with temp_workspace("util-io-missing") as ws:
            with self.assertRaises(ToolError):
                read_text(os.path.join(ws, "not-exists.txt"))

    def test_write_non_string_payload(self):
        with temp_workspace("util-io-str") as ws:
            path = os.path.join(ws, "num.txt")
            write_text(path, 12345)
            self.assertEqual(read_text(path), "12345")


class RetryPolicyTest(unittest.TestCase):
    """重试策略：只重试可重试的 LLM 错误，计数与退避可观察。"""

    def _policy(self, **kwargs):
        slept: list = []
        policy = RetryPolicy(sleep=slept.append, **kwargs)
        return policy, slept

    def test_retryable_error_then_success(self):
        policy, slept = self._policy(max_retries=3, base_delay=0.5, max_delay=8.0)
        calls = {"n": 0}
        seen: list = []

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise LLMServerError("服务端 500", status=500)
            return "ok"

        result = policy.run(fn, on_retry=lambda attempt, exc: seen.append((attempt, type(exc).__name__)))
        self.assertEqual(result, "ok")
        self.assertEqual(policy.attempts, 3)
        self.assertEqual(policy.retries, 2)
        self.assertEqual(seen, [(1, "LLMServerError"), (2, "LLMServerError")])
        # 指数退避：0.5、1.0
        self.assertEqual(slept, [0.5, 1.0])

    def test_non_retryable_llm_error_is_not_retried(self):
        policy, slept = self._policy(max_retries=3)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise LLMResponseError("响应结构非法")

        with self.assertRaises(LLMResponseError):
            policy.run(fn)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(policy.attempts, 1)
        self.assertEqual(policy.retries, 0)
        self.assertEqual(slept, [])

    def test_programming_error_immediately_propagates(self):
        policy, slept = self._policy(max_retries=5)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ValueError("这是程序 bug，不该被当成网络抖动")

        with self.assertRaises(ValueError):
            policy.run(fn)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(policy.attempts, 1)
        self.assertEqual(policy.retries, 0)
        self.assertEqual(slept, [])

    def test_exhausted_retries_raise_last_error(self):
        policy, slept = self._policy(max_retries=2, base_delay=1.0)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise LLMServerError("一直 500", status=500)

        with self.assertRaises(LLMServerError):
            policy.run(fn)
        self.assertEqual(calls["n"], 3)          # 首次 + 2 次重试
        self.assertEqual(policy.attempts, 3)
        self.assertEqual(policy.retries, 2)
        self.assertEqual(slept, [1.0, 2.0])

    def test_zero_max_retries_means_single_attempt(self):
        policy, slept = self._policy(max_retries=0)

        def fn():
            raise LLMServerError("boom", status=503)

        with self.assertRaises(LLMServerError):
            policy.run(fn)
        self.assertEqual(policy.attempts, 1)
        self.assertEqual(policy.retries, 0)
        self.assertEqual(slept, [])

    def test_counters_reset_between_runs(self):
        policy, _ = self._policy(max_retries=1)
        state = {"fail": True}

        def fn():
            if state["fail"]:
                raise LLMServerError("第一次失败")
            return "done"

        with self.assertRaises(LLMServerError):
            policy.run(fn)
        self.assertEqual(policy.attempts, 2)
        self.assertEqual(policy.retries, 1)

        state["fail"] = False
        self.assertEqual(policy.run(fn), "done")
        self.assertEqual(policy.attempts, 1)
        self.assertEqual(policy.retries, 0)

    def test_delay_for_is_exponential_and_capped(self):
        policy = RetryPolicy(max_retries=10, base_delay=1.0, max_delay=5.0, sleep=lambda _: None)
        self.assertEqual(policy.delay_for(1), 1.0)
        self.assertEqual(policy.delay_for(2), 2.0)
        self.assertEqual(policy.delay_for(3), 4.0)
        self.assertEqual(policy.delay_for(4), 5.0)   # 8 → 被 max_delay 截断
        self.assertEqual(policy.delay_for(0), 0.0)

    def test_no_sleep_when_delays_are_zero(self):
        policy, slept = self._policy(max_retries=2, base_delay=0.0)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMServerError("瞬时错误")
            return 1

        self.assertEqual(policy.run(fn), 1)
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
