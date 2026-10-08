"""`wfa/report.py` 单元测试：运行报告与流程文档渲染。

覆盖点（对应 CONTRACT 第 8 节 RunReport 与第 13 节文档要求）：
    1. ``render_run_report`` 章节完整：运行概览（含指标 / 问题分级）、最终问题清单、
       修复明细、Agent 协作轨迹（节点时间线 + 消息总线）、产物清单、备注；
    2. 边界与容错：空 payload、字符串形式的 issue/fix/message、非 dict 产物、
       Markdown 表格里的 ``|`` 转义与超长单元格截断、状态与严重程度的中文标签；
    3. ``render_flow_document`` 含 Mermaid 代码块（``flowchart``）、节点清单、边与循环章节、
       DOT 附录，并标注回边；
    4. **密钥绝不出现**在报告文本里（用带 api_key 的 Config 构造 payload）；
    5. ``write_report`` 落盘并自动建父目录。

离线、确定性；工作区文件走 ``tests.helpers.temp_workspace``。
"""

from __future__ import annotations

import json
import os
import unittest

from tests.helpers import temp_workspace
from wfa.config import Config
from wfa.graph import AgentResult, WorkflowBuilder
from wfa.report import render_flow_document, render_run_report, write_report


def gate_pass(node, ctx):
    """测试用门禁：永远通过。"""
    return AgentResult(kind="result", summary="通过", decision="pass")


def sample_payload() -> dict:
    """构造一份"有内容"的运行报告载荷（字段名与 RunReport.to_dict 对齐）。"""
    return {
        "run_id": "run-abc123",
        "workflow": "code_review",
        "task": "把示例模块补全测试",
        "workspace": "C:/ws/sample",
        "status": "partial",
        "turns": 6,
        "duration": 1.25,
        "message_count": 2,
        "model": "deepseek-chat",
        "metrics": {"nodes": 5, "iterations": {"impl": 2}, "total_latency": 0.5},
        "counts": {"critical": 0, "high": 2, "medium": 1, "low": 0, "info": 3},
        "issues": [
            {
                "id": "SEC001-app/service.py:8",
                "title": "疑似硬编码密钥/口令",
                "file": "app/service.py",
                "line": 8,
                "severity": "high",
                "category": "security",
                "detail": "疑似把 API_KEY 明文写在代码里。",
                "suggestion": "改为从环境变量读取。",
                "source": "rule",
                "rule_id": "SEC001",
            },
            {
                "id": "BUG002-app/service.py:15",
                "title": "异常被静默吞掉（except + pass）",
                "file": "app/service.py",
                "line": 15,
                "severity": "high",
                "category": "bug",
                "detail": "异常信息被完全丢弃。",
                "suggestion": "至少记录日志。",
                "source": "rule",
                "rule_id": "BUG002",
            },
        ],
        "fixes": [
            {"file": "app/service.py", "issue_id": "SEC001",
             "summary": "硬编码密钥改为从环境变量读取"},
            {"file": "app/service.py", "issue_id": "BUG002",
             "summary": "异常不再静默吞掉"},
        ],
        "node_runs": [
            {"turn": 1, "node_id": "code", "agent": "coder", "kind": "result",
             "status": "ok", "duration": 0.12, "summary": "生成第一版实现"},
            {"turn": 2, "node_id": "review", "agent": "reviewer", "kind": "review",
             "status": "ok", "duration": 0.08, "summary": "发现 2 个阻断问题"},
        ],
        "messages": [
            {"turn": 1, "sender": "workflow", "recipient": "*", "kind": "system",
             "payload": {"event": "turn_start"}},
            {"turn": 2, "sender": "coder", "recipient": "reviewer", "kind": "task",
             "payload": {"files": ["app/service.py"]}},
        ],
        "artifacts": {
            "run_report.md": {"name": "run_report.md", "kind": "report",
                              "producer": "documenter", "content": "# 报告"},
        },
        "notes": ["达到 max_iterations=3，已产出部分结果"],
    }


class RunReportSectionsTest(unittest.TestCase):
    """章节完整性。"""

    def setUp(self):
        self.text = render_run_report(sample_payload())

    def test_all_sections_present(self):
        for heading in ("# 运行报告 · code_review", "## 1. 运行概览", "### 1.1 指标",
                        "### 1.2 问题分级", "## 2. 最终问题清单", "## 3. 修复明细",
                        "## 4. Agent 协作轨迹", "### 4.1 节点时间线", "### 4.2 消息总线",
                        "## 5. 产物清单", "## 6. 备注与告警"):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.text)

    def test_overview_values(self):
        self.assertIn("`run-abc123`", self.text)
        self.assertIn("把示例模块补全测试", self.text)
        self.assertIn("`C:/ws/sample`", self.text)
        self.assertIn("🟡 部分完成", self.text)
        self.assertIn("deepseek-chat", self.text)

    def test_issue_rows_and_details(self):
        self.assertIn("`app/service.py`:8", self.text)
        self.assertIn("严重", self.text)          # severity 中文标签
        self.assertIn("疑似硬编码密钥/口令", self.text)
        self.assertIn("<details><summary>展开问题详情</summary>", self.text)
        self.assertIn("改为从环境变量读取。", self.text)

    def test_fix_rows(self):
        self.assertIn("硬编码密钥改为从环境变量读取", self.text)
        self.assertIn("SEC001", self.text)

    def test_node_timeline_and_kind_label(self):
        self.assertIn("| 1 | `code` | coder | 结果 | ok |", self.text)
        self.assertIn("| 2 | `review` | reviewer | 审查 | ok |", self.text)

    def test_message_rows(self):
        self.assertIn("workflow", self.text)
        self.assertIn("turn_start", self.text)
        self.assertIn("coder", self.text)

    def test_artifacts(self):
        self.assertIn("| `run_report.md` | report | documenter | 4 |", self.text)

    def test_notes(self):
        self.assertIn("达到 max_iterations=3，已产出部分结果", self.text)

    def test_ends_with_single_newline(self):
        self.assertTrue(self.text.endswith("\n"))
        self.assertFalse(self.text.endswith("\n\n"))


class RunReportEdgeCaseTest(unittest.TestCase):
    """空载荷与脏载荷不炸、不乱。"""

    def test_empty_payload(self):
        text = render_run_report({})
        self.assertIn("# 运行报告 · ?", text)
        self.assertIn("本次运行未发现问题（或问题已在修复循环中全部关闭）。", text)
        self.assertIn("本次运行没有产生修复动作。", text)
        self.assertIn("（无节点记录）", text)
        self.assertIn("（无消息）", text)
        self.assertIn("（无产物）", text)
        self.assertNotIn("## 6. 备注与告警", text)

    def test_none_payload(self):
        self.assertIn("运行报告", render_run_report(None))

    def test_string_entries_are_tolerated(self):
        text = render_run_report({
            "workflow": "w", "issues": ["裸字符串问题"], "fixes": ["裸字符串修复"],
            "messages": ["裸字符串消息"],
        })
        self.assertIn("裸字符串问题", text)
        self.assertIn("裸字符串修复", text)
        self.assertIn("裸字符串消息", text)

    def test_non_dict_artifact_is_tolerated(self):
        text = render_run_report({"workflow": "w", "artifacts": {"x": "just text"}})
        self.assertIn("| `x` | - | - | - |", text)

    def test_pipe_is_escaped_in_cells(self):
        text = render_run_report({
            "workflow": "w",
            "issues": [{"title": "a|b", "file": "f.py", "line": 1, "severity": "low",
                        "category": "bug", "suggestion": "c|d"}],
        })
        self.assertIn("a\\|b", text)
        self.assertIn("c\\|d", text)

    def test_long_cell_is_truncated(self):
        text = render_run_report({
            "workflow": "w",
            "issues": [{"title": "x" * 400, "file": "f.py", "line": 1, "severity": "low",
                        "category": "bug"}],
        })
        self.assertIn("…", text)
        self.assertNotIn("x" * 200, text)

    def test_multiline_detail_is_kept_in_details_block(self):
        text = render_run_report({
            "workflow": "w",
            "issues": [{"title": "t", "file": "f.py", "line": 1, "severity": "low",
                        "category": "bug", "detail": "第一行\n第二行"}],
        })
        self.assertIn("<details><summary>展开问题详情</summary>", text)
        self.assertIn("第一行\n第二行", text)

    def test_unknown_status_falls_back_to_raw_value(self):
        text = render_run_report({"workflow": "w", "status": "weird"})
        self.assertIn("| 状态 | weird |", text)

    def test_issue_count_uses_counts_when_available(self):
        text = render_run_report({"workflow": "w", "counts": {"high": 2, "low": 1}})
        self.assertIn("| 问题总数 | 3 |", text)

    def test_duration_and_turns_defaults(self):
        text = render_run_report({"workflow": "w"})
        self.assertIn("| 回合数 | 0 |", text)
        self.assertIn("| 耗时 | 0 s |", text)
        self.assertIn("离线（未使用）", text)


class FlowDocumentTest(unittest.TestCase):
    """流程文档：Mermaid + 节点/边/循环清单 + DOT。"""

    def _workflow(self):
        return (
            WorkflowBuilder("mini", description="迷你代码审查流程")
            .start("start")
            .agent("code", "coder", description="写第一版实现", loop_of="impl")
            .gate("gate_quality", execute=gate_pass, description="质量门禁")
            .end("end")
            .edge("start", "code")
            .edge("code", "gate_quality")
            .edge("gate_quality", "end", when="pass")
            .edge("gate_quality", "code", when="retry")
            .build(max_iterations=3)
        )

    def test_mermaid_block_present(self):
        text = render_flow_document(self._workflow())
        self.assertIn("```mermaid", text)
        self.assertIn("flowchart TD", text)
        self.assertIn("```dot", text)
        self.assertIn("digraph", text)

    def test_node_and_edge_tables(self):
        text = render_flow_document(self._workflow())
        self.assertIn("# 流程说明 · mini", text)
        self.assertIn("迷你代码审查流程", text)
        self.assertIn("## 2. 节点清单", text)
        self.assertIn("| `code` | agent | coder | 写第一版实现 |", text)
        self.assertIn("| `gate_quality` | gate | - | 质量门禁 |", text)
        self.assertIn("## 3. 边（转移条件）", text)
        self.assertIn("| `gate_quality` | `code` | retry |", text)

    def test_loop_section_and_back_edge(self):
        text = render_flow_document(self._workflow())
        self.assertIn("## 4. 循环与分支", text)
        self.assertIn("循环 `impl`", text)
        self.assertIn("回边", text)

    def test_workflow_without_loop(self):
        workflow = (
            WorkflowBuilder("linear", description="无循环流程")
            .start("start").agent("only", "coder").end("end")
            .edge("start", "only").edge("only", "end")
            .build()
        )
        text = render_flow_document(workflow)
        self.assertIn("- 无显式循环", text)


class SecretLeakTest(unittest.TestCase):
    """报告里绝不出现 API Key（与 README/日志同一条底线）。"""

    SECRET = "sk-super-secret-leak-value-1234567890"

    def _cfg(self) -> Config:
        return Config(api_key=self.SECRET, model="deepseek-chat", mode="online")

    def test_public_dict_has_no_key(self):
        self.assertNotIn(self.SECRET, json.dumps(self._cfg().public_dict(), ensure_ascii=False))

    def test_render_does_not_leak_key(self):
        cfg = self._cfg()
        payload = sample_payload()
        payload["config"] = cfg.public_dict()
        payload["metrics"]["config"] = cfg.public_dict()
        payload["messages"].append(
            {"turn": 1, "sender": "workflow", "recipient": "*", "kind": "system",
             "payload": cfg.public_dict()}
        )
        payload["notes"] = [cfg.describe()]
        text = render_run_report(payload)
        self.assertNotIn(self.SECRET, text)
        self.assertIn("deepseek-chat", text)

    def test_write_report_does_not_leak_key(self):
        cfg = self._cfg()
        payload = sample_payload()
        payload["config"] = cfg.public_dict()
        with temp_workspace("report-write") as ws:
            path = os.path.join(ws, "out", "report.md")
            write_report(payload, path)
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        self.assertNotIn(self.SECRET, text)
        self.assertIn("# 运行报告 · code_review", text)


class WriteReportTest(unittest.TestCase):
    """落盘行为：自动建父目录、内容可读。"""

    def test_writes_file_and_creates_dirs(self):
        payload = sample_payload()
        with temp_workspace("report-file") as ws:
            path = os.path.join(ws, "nested", "dir", "report.md")
            returned = write_report(payload, path)
            # 契约：write_report 返回写入路径（与 write_text 的 None 返回值不同）。
            self.assertEqual(returned, path)
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("# 运行报告 · code_review", text)
            self.assertIn("## 5. 产物清单", text)


if __name__ == "__main__":
    unittest.main()
