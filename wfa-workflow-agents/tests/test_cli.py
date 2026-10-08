"""CLI 与入口脚本的轻量冒烟测试。

只验证"命令能跑、参数解析正确、离线不需要 Key、退出码符合约定"，
不验证完整流程语义（那部分在 test_workflows.py / test_graph_engine.py 里）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from tests.helpers import PROJECT_ROOT, temp_workspace, workspace_root
from wfa.cli import build_parser, main


def run_cli(argv) -> tuple:
    """执行 CLI 并捕获 stdout/stderr 与退出码。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ParserTest(unittest.TestCase):
    """参数解析：默认值、开关与子命令。"""

    def test_run_defaults(self):
        args = build_parser().parse_args(["run"])
        self.assertEqual(args.workflow, "code_review")
        self.assertEqual(args.max_turns, 50)
        self.assertEqual(args.max_iterations, 3)
        self.assertFalse(args.online)
        self.assertFalse(args.parallel_review)
        self.assertEqual(args.func.__name__, "cmd_run")

    def test_run_flags(self):
        args = build_parser().parse_args(
            ["run", "--workflow", "docs", "--online", "--parallel-review",
             "--max-iterations", "2", "--task", "写文档", "--out", "x.md"]
        )
        self.assertEqual(args.workflow, "docs")
        self.assertTrue(args.online)
        self.assertTrue(args.parallel_review)
        self.assertEqual(args.max_iterations, 2)
        self.assertEqual(args.task, "写文档")

    def test_subcommands_exist(self):
        for name in ("run", "list", "graph", "report"):
            args = build_parser().parse_args([name] if name != "report" else ["report", "--json", "a.json"])
            self.assertIsNotNone(args.func, name)

    def test_no_command_prints_help(self):
        code, out, _ = run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("wfa", out)


class ListCommandTest(unittest.TestCase):
    """`list` 必须离线可用（不需要 API Key）。"""

    def test_list_outputs_agents_and_workflows(self):
        code, out, _ = run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn("Agent", out)
        self.assertIn("Workflow", out)
        self.assertIn("code_review", out)
        self.assertIn("docs", out)

    def test_list_verbose(self):
        code, out, _ = run_cli(["list", "--verbose"])
        self.assertEqual(code, 0)
        self.assertIn("coder", out)


class GraphCommandTest(unittest.TestCase):
    """`graph` 必须能在不运行流程的情况下导出拓扑。"""

    def test_graph_mermaid(self):
        code, out, _ = run_cli(["graph", "--workflow", "code_review"])
        self.assertEqual(code, 0)
        self.assertIn("flowchart", out.lower() if "flowchart" in out.lower() else out)
        self.assertIn("review", out)

    def test_graph_writes_file(self):
        with temp_workspace("cli-graph") as ws:
            target = os.path.join(ws, "FLOW.md")
            code, out, _ = run_cli(["graph", "--workflow", "docs", "--format", "md", "--out", target])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.isfile(target))
            with open(target, "r", encoding="utf-8") as handle:
                self.assertIn("流程图", handle.read())


class ReportCommandTest(unittest.TestCase):
    """`report` 把 JSON 渲染成 Markdown。"""

    def test_report_roundtrip(self):
        payload = {
            "run_id": "run-test", "workflow": "code_review", "status": "success",
            "turns": 4, "duration": 0.5, "issues": [], "counts": {},
            "artifacts": {}, "node_runs": [], "messages": [], "notes": [],
        }
        with temp_workspace("cli-report") as ws:
            src = os.path.join(ws, "report.json")
            dst = os.path.join(ws, "report.md")
            with open(src, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            code, out, _ = run_cli(["report", "--json", src, "--out", dst])
            self.assertEqual(code, 0)
            with open(dst, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("运行报告", text)
            self.assertIn("运行概览", text)


class EntryScriptTest(unittest.TestCase):
    """入口脚本存在且可直接以文件方式运行（不 import 网络相关逻辑）。"""

    def test_entry_files_exist(self):
        for name in ("run.py", "webui.py", "wfa.cmd", "start-web.cmd", ".env.example", "requirements.txt"):
            self.assertTrue(os.path.isfile(os.path.join(PROJECT_ROOT, name)), name)

    def test_run_py_help(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, os.path.join(workspace_root(), "run.py"), "--version"],
            cwd=workspace_root(), capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wfa", proc.stdout)


class SecretLeakTest(unittest.TestCase):
    """密钥绝不进日志/报告的底线（与 Homework1 同款约束）。"""

    def test_config_public_dict_hides_key(self):
        from wfa.config import Config

        cfg = Config(api_key="sk-super-secret-value")
        self.assertNotIn("sk-super-secret-value", json.dumps(cfg.public_dict(), ensure_ascii=False))
        self.assertIn("sk-super-secret-value", cfg.resolve_api_key())

    def test_runtime_does_not_raise_without_key_offline(self):
        cfg = type("C", (), {})()
        from wfa.config import Config
        from wfa.model import build_model

        offline_cfg = Config(api_key="", mode="offline")
        model = build_model(offline_cfg)
        self.assertTrue(getattr(model, "is_deterministic", False))


if __name__ == "__main__":
    unittest.main()
