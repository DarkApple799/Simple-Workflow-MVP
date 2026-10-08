"""`wfa/analysis.py` 确定性规则引擎单元测试（CONTRACT 第 7.2 节，22 条规则）。

覆盖点：
    1. 22 条规则（SYN001 / BUG001/002/003/004/006 / SEC001..006 /
       MAINT001..009 / STYLE001）**每条都能被"故意写坏"的源码触发**，逐条断言 rule_id；
    2. 干净、规范的代码零误报；
    3. 语法错误产出 SYN001 而不是抛异常；单条规则自身抛错也不影响整次扫描；
    4. ``max_hits`` 上限生效，且按严重程度优先保留；
    5. ``RuleHit.to_issue()`` 字段符合 Issue 契约；
    6. ``scan_workspace`` 的文件分类、跳过原因、统计与 JSON 序列化；
    7. ``analyze_python_file`` 对不存在/语法错误文件的容错。

全部离线、确定性，不调用任何 LLM。
"""

from __future__ import annotations

import json
import os
import unittest

from tests.helpers import temp_workspace
from wfa import SEVERITY_ORDER
from wfa.analysis import (
    RULES,
    RULES_BY_ID,
    RuleHit,
    analyze_python_file,
    analyze_source,
    rule_catalog,
    scan_workspace,
)

#: 契约要求的 22 条规则 id。
EXPECTED_RULES = {
    "SYN001",
    "BUG001", "BUG002", "BUG003", "BUG004", "BUG006",
    "SEC001", "SEC002", "SEC003", "SEC004", "SEC005", "SEC006",
    "MAINT001", "MAINT002", "MAINT003", "MAINT004", "MAINT005",
    "MAINT006", "MAINT007", "MAINT008", "MAINT009",
    "STYLE001",
}

#: 一段"干净"的源码：不应触发任何规则。
CLEAN_SOURCE = '''"""一个规范的示例模块，用于验证规则引擎零误报。"""

import json

LIMIT = 100


def parse(payload):
    """把 JSON 文本解析成字典。"""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload 不是合法 JSON：%s" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("payload 必须是 JSON 对象")
    return data


class Parser:
    """一个很薄的解析器封装。"""

    def __init__(self, limit=LIMIT):
        self.limit = limit

    def run(self, payload):
        """执行解析。"""
        return parse(payload)
'''


def rule_ids(source: str, rel: str = "sample.py", **kwargs) -> set:
    """分析源码并返回命中的规则 id 集合。"""
    return {hit.rule_id for hit in analyze_source(source, rel, **kwargs)}


class RuleCatalogTest(unittest.TestCase):
    """规则目录完整性。"""

    def test_expected_rule_count(self):
        self.assertEqual(len(RULES), 22)
        self.assertEqual({rule["id"] for rule in RULES}, EXPECTED_RULES)

    def test_rule_metadata_is_complete(self):
        for rule in RULES:
            with self.subTest(rule=rule["id"]):
                self.assertTrue(rule["title"])
                self.assertIn(rule["severity"], SEVERITY_ORDER)
                self.assertTrue(rule["category"])
                self.assertTrue(callable(rule["detector"]))

    def test_rules_by_id_and_catalog(self):
        self.assertEqual(set(RULES_BY_ID), EXPECTED_RULES)
        catalog = rule_catalog()
        self.assertEqual(len(catalog), 22)
        self.assertEqual(set(catalog[0]), {"id", "title", "severity", "category"})


class SyntaxErrorTest(unittest.TestCase):
    """SYN001：语法错误不抛异常。"""

    def test_syntax_error_produces_syn001(self):
        hits = analyze_source("def broken(:\n    pass\n", "broken.py")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].rule_id, "SYN001")
        self.assertEqual(hits[0].severity, "high")
        self.assertIsNotNone(hits[0].line)

    def test_syntax_error_detector_is_independent(self):
        # 解析成功时，SYN001 的 detector 直接调用应返回空（规则可单独回归）。
        import ast

        detector = RULES_BY_ID["SYN001"]["detector"]
        tree = ast.parse("value = 1\n")
        self.assertEqual(detector(tree, "value = 1\n", "ok.py"), [])

    def test_indentation_error_also_produces_syn001(self):
        hits = analyze_source("def f():\npass\n", "bad_indent.py")
        self.assertEqual([hit.rule_id for hit in hits], ["SYN001"])

    def test_empty_source_has_no_hits(self):
        self.assertEqual(analyze_source("", "empty.py"), [])


class BugRuleTest(unittest.TestCase):
    """BUG 系列规则逐条触发。"""

    def test_bug001_bare_except(self):
        source = '''"""裸 except 示例。"""


def risky(value):
    """执行可能失败的操作。"""
    try:
        return int(value)
    except:
        return 0
'''
        self.assertIn("BUG001", rule_ids(source))

    def test_bug002_silent_exception(self):
        source = '''"""静默吞异常示例。"""


def risky(value):
    """执行可能失败的操作。"""
    try:
        return int(value)
    except Exception:
        pass
'''
        self.assertIn("BUG002", rule_ids(source))

    def test_bug003_mutable_default_argument(self):
        source = '''"""可变默认参数示例。"""


def collect(item, bucket=[]):
    """收集元素。"""
    bucket.append(item)
    return bucket


def merge(options={}):
    """合并选项。"""
    return options
'''
        hits = [hit for hit in analyze_source(source, "sample.py") if hit.rule_id == "BUG003"]
        self.assertEqual(len(hits), 2)

    def test_bug004_compare_with_none(self):
        source = '''"""与 None 比较示例。"""


def check(token):
    """判断 token 是否缺失。"""
    if token == None:
        return False
    return True
'''
        self.assertIn("BUG004", rule_ids(source))

    def test_bug006_mutating_while_iterating(self):
        source = '''"""遍历时修改列表示例。"""


def drop(items, target):
    """删除所有等于 target 的元素。"""
    for item in items:
        if item == target:
            items.remove(item)
    return items
'''
        self.assertIn("BUG006", rule_ids(source))


class SecurityRuleTest(unittest.TestCase):
    """SEC 系列规则逐条触发。"""

    def test_sec001_hardcoded_secret(self):
        source = '''"""硬编码密钥示例。"""

API_KEY = "sk-hardcoded-demo-key-1234567890"
'''
        self.assertIn("SEC001", rule_ids(source))

    def test_sec001_ignores_placeholders(self):
        source = '''"""占位符不应误报。"""

API_KEY = "<YOUR_API_KEY>"
PASSWORD = "changeme"
'''
        self.assertNotIn("SEC001", rule_ids(source))

    def test_sec002_eval_and_exec(self):
        source = '''"""动态执行示例。"""


def run(text, mode):
    """执行用户输入。"""
    if mode:
        return eval(text)
    exec(text)
    return None
'''
        hits = [hit for hit in analyze_source(source, "sample.py") if hit.rule_id == "SEC002"]
        self.assertEqual(len(hits), 2)

    def test_sec003_subprocess_shell_true(self):
        source = '''"""命令注入示例。"""

import subprocess


def run_command(command):
    """执行外部命令。"""
    return subprocess.run(command, shell=True)
'''
        self.assertIn("SEC003", rule_ids(source))

    def test_sec004_os_system(self):
        source = '''"""os.system 示例。"""

import os


def run_command(name):
    """执行外部命令。"""
    os.system("ls " + name)
'''
        self.assertIn("SEC004", rule_ids(source))

    def test_sec005_unsafe_deserialization(self):
        source = '''"""不安全反序列化示例。"""

import pickle
import yaml


def load_pickle(raw):
    """反序列化不可信数据。"""
    return pickle.loads(raw)


def load_yaml(text):
    """解析不可信 YAML。"""
    return yaml.load(text)
'''
        hits = [hit for hit in analyze_source(source, "sample.py") if hit.rule_id == "SEC005"]
        self.assertEqual(len(hits), 2)

    def test_sec005_safe_yaml_loader_is_ignored(self):
        source = '''"""安全 YAML 示例。"""

import yaml


def load_yaml(text):
    """用 SafeLoader 解析。"""
    return yaml.load(text, Loader=yaml.SafeLoader)
'''
        self.assertNotIn("SEC005", rule_ids(source))

    def test_sec006_assert_for_validation(self):
        source = '''"""用 assert 做校验示例。"""


def check(value):
    """校验输入。"""
    assert isinstance(value, int)
    return value
'''
        self.assertIn("SEC006", rule_ids(source))


class MaintainabilityRuleTest(unittest.TestCase):
    """MAINT 系列规则逐条触发。"""

    def test_maint001_long_function(self):
        body = "\n".join("    step_%d = %d" % (index, index) for index in range(45))
        source = '"""过长函数示例。"""\n\n\ndef long_one():\n    """很长的函数。"""\n' + body + "\n"
        self.assertIn("MAINT001", rule_ids(source))

    def test_maint002_high_complexity(self):
        body = "\n".join(
            "    if value > %d:\n        value -= 1" % index for index in range(10)
        )
        source = ('"""高复杂度示例。"""\n\n\ndef branched(value):\n'
                  '    """分支很多。"""\n' + body + "\n    return value\n")
        self.assertIn("MAINT002", rule_ids(source))

    def test_maint003_too_many_arguments(self):
        source = '''"""参数过多示例。"""


def many(first, second, third, fourth, fifth, sixth):
    """参数很多。"""
    return first + second + third + fourth + fifth + sixth
'''
        self.assertIn("MAINT003", rule_ids(source))

    def test_maint004_missing_docstring(self):
        source = '''def public_one(value):
    return value
'''
        self.assertIn("MAINT004", rule_ids(source))

    def test_maint004_relaxed_for_test_files(self):
        source = '''def public_one(value):
    return value
'''
        self.assertNotIn("MAINT004", rule_ids(source, "tests/test_sample.py"))

    def test_maint005_todo_marker(self):
        source = '''"""遗留标记示例。"""


def pending(value):
    """待完成。"""
    # TODO: 补充边界测试
    return value
'''
        self.assertIn("MAINT005", rule_ids(source))

    def test_maint006_unused_import(self):
        source = '''"""未使用导入示例。"""

import json


def value():
    """返回常量。"""
    return 1
'''
        self.assertIn("MAINT006", rule_ids(source))

    def test_maint007_wildcard_import(self):
        source = '''"""通配符导入示例。"""

from os import *


def value():
    """返回常量。"""
    return 1
'''
        self.assertIn("MAINT007", rule_ids(source))

    def test_maint008_shadowing_builtin(self):
        source = '''"""遮蔽内置名示例。"""


def shadow(list):
    """参数名遮蔽内置 list。"""
    return list
'''
        self.assertIn("MAINT008", rule_ids(source))

    def test_maint008_shadowing_by_assignment(self):
        source = '''"""赋值遮蔽内置名示例。"""

list = [1, 2, 3]
'''
        self.assertIn("MAINT008", rule_ids(source))

    def test_maint009_print_statement(self):
        source = '''"""调试 print 示例。"""


def debug(value):
    """调试输出。"""
    print(value)
'''
        self.assertIn("MAINT009", rule_ids(source))


class StyleAndFalsePositiveTest(unittest.TestCase):
    """STYLE001 与零误报底线。"""

    def test_style001_long_line(self):
        source = '"""行过长示例。"""\n\nLONG_VALUE = "' + "x" * 140 + '"\n'
        self.assertIn("STYLE001", rule_ids(source))

    def test_clean_source_has_no_hits(self):
        self.assertEqual(analyze_source(CLEAN_SOURCE, "clean_sample.py"), [])

    def test_clean_source_produces_no_hits_when_scanned_from_file(self):
        with temp_workspace("analysis-clean") as ws:
            path = os.path.join(ws, "clean_sample.py")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(CLEAN_SOURCE)
            self.assertEqual(analyze_python_file(path, rel="clean_sample.py"), [])


class RuleHitContractTest(unittest.TestCase):
    """RuleHit → Issue 的字段契约。"""

    def test_to_issue_fields(self):
        hit = RuleHit(rule_id="BUG001", title="裸 except", file="a.py", line=3,
                      severity="medium", category="bug", detail="d", suggestion="s")
        issue = hit.to_issue()
        for key in ("id", "title", "file", "line", "severity", "category",
                    "detail", "suggestion", "source", "rule_id"):
            self.assertIn(key, issue)
        self.assertEqual(issue["severity"], "medium")
        self.assertEqual(issue["source"], "rule")
        self.assertEqual(issue["id"], "BUG001-a.py:3")
        json.dumps(issue, ensure_ascii=False)

    def test_invalid_severity_and_line_are_normalized(self):
        hit = RuleHit(rule_id="X", title="t", file="a.py", line=-5, severity="catastrophic")
        self.assertEqual(hit.severity, "medium")
        self.assertIsNone(hit.line)
        self.assertEqual(hit.issue_id(), "X-a.py")

    def test_severity_rank_order(self):
        high = RuleHit(rule_id="A", title="t", file="a.py", severity="high")
        info = RuleHit(rule_id="B", title="t", file="a.py", severity="info")
        self.assertGreater(high.severity_rank, info.severity_rank)


class MaxHitsTest(unittest.TestCase):
    """max_hits 上限与优先级。"""

    def _noisy_source(self) -> str:
        lines = ['"""噪声源码。"""', "",
                 'API_KEY = "sk-hardcoded-demo-key-1234567890"', "",
                 "def noisy(value):", '    """很多低级问题。"""']
        for index in range(20):
            lines.append("    print(value + %d)" % index)
        lines.append("    # TODO: 清理这些 print")
        lines.append("    return value")
        return "\n".join(lines) + "\n"

    def test_limit_is_respected(self):
        source = self._noisy_source()
        limited = analyze_source(source, "noisy.py", max_hits=5)
        self.assertEqual(len(limited), 5)
        self.assertLess(len(limited), len(analyze_source(source, "noisy.py", max_hits=0)))

    def test_high_severity_kept_first(self):
        limited = analyze_source(self._noisy_source(), "noisy.py", max_hits=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0].rule_id, "SEC001")     # high 优先于 low/info

    def test_zero_means_unlimited(self):
        hits = analyze_source(self._noisy_source(), "noisy.py", max_hits=0)
        self.assertGreater(len(hits), 20)

    def test_hits_are_sorted_by_severity_when_truncated(self):
        hits = analyze_source(self._noisy_source(), "noisy.py", max_hits=10)
        ranks = [hit.severity_rank for hit in hits]
        self.assertEqual(ranks, sorted(ranks, reverse=True))


class DetectorIsolationTest(unittest.TestCase):
    """单条规则抛异常不影响其它规则。"""

    def test_failing_detector_does_not_break_scan(self):
        def broken_detector(tree, source, file):
            raise RuntimeError("故意让这条规则失败")

        RULES.append({"id": "ZZZ999", "title": "会崩的规则", "severity": "low",
                      "category": "other", "detector": broken_detector})
        try:
            source = '"""有 print 的源码。"""\n\nprint("hi")\n'
            with self.assertLogs("wfa.analysis", level="WARNING") as captured:
                ids = rule_ids(source)
            self.assertTrue(any("ZZZ999" in line for line in captured.output))
            self.assertIn("MAINT009", ids)
            self.assertNotIn("ZZZ999", ids)
        finally:
            RULES.pop()


class AnalyzeFileTest(unittest.TestCase):
    """analyze_python_file 的容错。"""

    def test_missing_file_returns_empty(self):
        with temp_workspace("analysis-missing") as ws:
            self.assertEqual(analyze_python_file(os.path.join(ws, "nope.py")), [])
            self.assertEqual(analyze_python_file(""), [])

    def test_syntax_error_file(self):
        with temp_workspace("analysis-syntax") as ws:
            path = os.path.join(ws, "broken.py")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("def broken(:\n    pass\n")
            hits = analyze_python_file(path)
            self.assertEqual([hit.rule_id for hit in hits], ["SYN001"])
            self.assertEqual(hits[0].file, "broken.py")

    def test_non_python_readable_file_still_parsed_by_name(self):
        with temp_workspace("analysis-rel") as ws:
            path = os.path.join(ws, "code.txt")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("print(1)\n")
            hits = analyze_python_file(path, rel="code.txt")
            self.assertEqual([hit.rule_id for hit in hits], ["MAINT009"])


class ScanWorkspaceTest(unittest.TestCase):
    """工作区扫描：分类、跳过、统计与序列化。"""

    def _make_tree(self, ws: str) -> None:
        with open(os.path.join(ws, "clean.py"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(CLEAN_SOURCE)
        with open(os.path.join(ws, "noisy.py"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write('print("hi")\n')
        with open(os.path.join(ws, "readme.md"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# 说明\n")
        with open(os.path.join(ws, "data.bin"), "wb") as handle:
            handle.write(b"\x00\x01\x02binary")
        os.makedirs(os.path.join(ws, ".git"), exist_ok=True)
        with open(os.path.join(ws, ".git", "hook.py"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write('print("ignored")\n')

    def test_scan_classifies_and_skips(self):
        with temp_workspace("analysis-scan") as ws:
            self._make_tree(ws)
            report = scan_workspace(ws)
            paths = [item["path"] for item in report.files]
            self.assertIn("clean.py", paths)
            self.assertIn("noisy.py", paths)
            self.assertIn("readme.md", paths)
            self.assertIn("data.bin", paths)
            self.assertNotIn(".git/hook.py", paths)

            kinds = {item["path"]: item["kind"] for item in report.files}
            self.assertEqual(kinds["noisy.py"], "python")
            self.assertEqual(kinds["readme.md"], "text")
            self.assertEqual(kinds["data.bin"], "binary")
            self.assertEqual(kinds["noisy.py"], "python")

            self.assertEqual(sorted(report.python_files()), ["clean.py", "noisy.py"])
            self.assertIn("MAINT009", {hit.rule_id for hit in report.hits})
            self.assertEqual(report.hits[0].file, "noisy.py")
            self.assertFalse(any(item["path"] == "readme.md" for item in report.skipped))
            self.assertTrue(any(item["path"] == "data.bin" for item in report.skipped))
            self.assertTrue(report.notes)
            self.assertGreaterEqual(report.duration, 0.0)

    def test_counts_always_has_all_severities(self):
        with temp_workspace("analysis-counts") as ws:
            self._make_tree(ws)
            counts = scan_workspace(ws).counts()
            self.assertEqual(set(counts), set(SEVERITY_ORDER))
            self.assertGreaterEqual(counts["low"], 1)

    def test_to_dict_is_json_serializable(self):
        with temp_workspace("analysis-dict") as ws:
            self._make_tree(ws)
            payload = scan_workspace(ws).to_dict()
            text = json.dumps(payload, ensure_ascii=False)
            self.assertIn("clean.py", text)
            self.assertEqual(payload["stats"]["files"], len(payload["files"]))
            self.assertEqual(payload["stats"]["hits"], len(payload["hits"]))
            self.assertEqual(payload["stats"]["python_files"], 2)

    def test_missing_root_is_reported_not_raised(self):
        with temp_workspace("analysis-noroot") as ws:
            report = scan_workspace(os.path.join(ws, "does-not-exist"))
            self.assertEqual(report.hits, [])
            self.assertTrue(report.notes)

    def test_max_files_truncation_note(self):
        with temp_workspace("analysis-maxfiles") as ws:
            for index in range(5):
                with open(os.path.join(ws, "m%d.py" % index), "w",
                          encoding="utf-8", newline="\n") as handle:
                    handle.write("VALUE = %d\n" % index)
            report = scan_workspace(ws, max_files=2)
            self.assertEqual(len(report.files), 2)
            self.assertTrue(any("max_files" in note for note in report.notes))

    def test_oversized_file_is_skipped(self):
        with temp_workspace("analysis-bigfile") as ws:
            with open(os.path.join(ws, "big.py"), "w", encoding="utf-8", newline="\n") as handle:
                handle.write("VALUE = 1\n" + "# pad\n" * 200)
            report = scan_workspace(ws, max_file_bytes=50)
            self.assertTrue(any(item["path"] == "big.py" for item in report.skipped))
            self.assertEqual(report.hits, [])

    def test_empty_workspace_note(self):
        with temp_workspace("analysis-empty") as ws:
            os.makedirs(os.path.join(ws, "sub"), exist_ok=True)
            report = scan_workspace(ws)
            self.assertEqual(report.files, [])
            self.assertTrue(any("Python" in note for note in report.notes))


if __name__ == "__main__":
    unittest.main()
