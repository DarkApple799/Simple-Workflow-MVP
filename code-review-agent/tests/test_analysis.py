"""确定性静态分析引擎的规则测试。

每个规则都用一个最小片段验证"该报的报、不该报的不报"，
这是保证报告可信度的基础：宁可漏报，也不要在好代码上误报。
"""

from __future__ import annotations

import unittest

from cra.analysis import RULES, analyze_source


def rule_ids(source: str) -> set[str]:
    findings, _ = analyze_source(source, "sample.py", max_findings=500)
    return {finding.rule_id for finding in findings}


class TestRulesFire(unittest.TestCase):
    """每条规则都要能被触发。"""

    def test_security_rules(self) -> None:
        # 注意：这里的字面量是"被测规则本身的夹具"，必须长得像真密钥才能触发 SEC001
        cases = {
            "SEC001": 'API_KEY = "sk-abcdef123456"\n',
            "SEC002": "def f(x):\n    return eval(x)\n",
            "SEC003": "import subprocess\n\ndef f(cmd):\n    subprocess.run(cmd, shell=True)\n",
            "SEC004": "import os\n\ndef f(host):\n    os.system('ping ' + host)\n",
            "SEC005": "import pickle\n\ndef f(blob):\n    return pickle.loads(blob)\n",
            "SEC006": "def f(user_id):\n    assert user_id\n    return user_id\n",
        }
        for rule_id, source in cases.items():
            with self.subTest(rule=rule_id):
                self.assertIn(rule_id, rule_ids(source))

    def test_bug_rules(self) -> None:
        cases = {
            "BUG001": "def f():\n    try:\n        pass\n    except:\n        pass\n",
            "BUG002": "def f():\n    try:\n        pass\n    except Exception:\n        pass\n",
            "BUG003": "def f(items=[]):\n    return items\n",
            "BUG004": "def f(x):\n    return x == None\n",
            "BUG005": "def f(x):\n    return type(x) == dict\n",
            "BUG006": "def f(items):\n    for item in items:\n        items.remove(item)\n    return items\n",
        }
        for rule_id, source in cases.items():
            with self.subTest(rule=rule_id):
                self.assertIn(rule_id, rule_ids(source))

    def test_maintainability_rules(self) -> None:
        long_function = "def big(x):\n" + "".join(f"    x = x + {i}\n" for i in range(45)) + "    return x\n"
        complex_function = "def branchy(x):\n" + "".join(
            f"    if x == {i}:\n        x += 1\n" for i in range(12)
        ) + "    return x\n"
        many_args = "def f(" + ", ".join(f"a{i}" for i in range(7)) + "):\n    return a0\n"
        cases = {
            "MAINT001": long_function,
            "MAINT002": complex_function,
            "MAINT003": many_args,
            "MAINT004": "def public():\n    return 1\n",
            "MAINT005": "# TODO: 稍后处理\ndef f():\n    return 1\n",
            "MAINT006": "import os\n\ndef f():\n    return 1\n",
            "MAINT007": "from os import *\n",
            "MAINT008": "def f(list):\n    return list\n",
            "MAINT009": "def f():\n    print('debug')\n",
        }
        for rule_id, source in cases.items():
            with self.subTest(rule=rule_id):
                self.assertIn(rule_id, rule_ids(source))

    def test_style_rules(self) -> None:
        self.assertIn("STYLE001", rule_ids("x = '" + "a" * 200 + "'\n"))
        self.assertIn("STYLE002", rule_ids("x = 1   \n"))
        big = "".join(f"x{i} = {i}\n" for i in range(520))
        self.assertIn("STYLE003", rule_ids(big))

    def test_syntax_error_is_reported_not_raised(self) -> None:
        findings, analysis = analyze_source("def broken(:\n    pass\n", "bad.py")
        self.assertIn("SYN001", {f.rule_id for f in findings})
        self.assertFalse(analysis.parse_ok)
        self.assertEqual(findings[0].severity, "high")

    def test_all_declared_rules_are_reachable(self) -> None:
        """防止 RULES 里登记了却从未实现的"幽灵规则"。"""
        covered = {
            "SYN001", "BUG001", "BUG002", "BUG003", "BUG004", "BUG005", "BUG006",
            "SEC001", "SEC002", "SEC003", "SEC004", "SEC005", "SEC006",
            "MAINT001", "MAINT002", "MAINT003", "MAINT004", "MAINT005",
            "MAINT006", "MAINT007", "MAINT008", "MAINT009",
            "STYLE001", "STYLE002", "STYLE003",
        }
        self.assertEqual(set(RULES) - covered, set(), "RULES 中存在未实现或未测试的规则")


class TestNoFalsePositives(unittest.TestCase):
    """规范代码不应被刷出一堆问题。"""

    CLEAN = '''"""模块文档。"""

from __future__ import annotations

import json
from pathlib import Path


def load(path: str | Path) -> dict:
    """读取 JSON 配置。"""
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class Loader:
    """加载器。"""

    def __init__(self, base: str) -> None:
        self.base = base
'''

    def test_clean_source(self) -> None:
        self.assertEqual(rule_ids(self.CLEAN), set())


class TestFindingCap(unittest.TestCase):
    """单文件问题上限：必须优先保留高危问题。"""

    def test_cap_keeps_high_severity(self) -> None:
        source = "import json\n" + "".join(f"def f{i}():\n    return {i}\n" for i in range(40))
        source += "def g(items=[]):\n    return items\n"
        findings, _ = analyze_source(source, "noisy.py", max_findings=5)
        rules = [finding.rule_id for finding in findings]
        self.assertIn("BUG003", rules, "高危规则不应被上限挤掉")
        self.assertIn("LIMIT001", rules)
        self.assertEqual(len(findings), 6)  # 5 条 + 1 条折叠说明
