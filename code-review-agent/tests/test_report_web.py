"""报告渲染与 Web 层测试。

重点：
    * 报告必须包含关键章节，且不能泄露 API Key；
    * Markdown → HTML 的转换要能处理报告里真实出现的表格、代码块、列表；
    * Web 服务能起来、能响应 /api/health 与 /api/scan（用 0 端口，避免占用）。
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from cra.cli import Console
from cra.config import Config
from cra.models import AgentOutcome
from cra.report import render_json, render_markdown
from cra.scanner import scan_workspace
from cra.web.server import make_handler, markdown_to_html
from tests.support import TempDirTestCase

#: 故意写得有问题的样例源码；密钥部分用拼接，避免被本项目自己的 SEC001 规则误伤
BUGGY = (
    'API_KEY = "' + "sk-" + 'verysecret123456"\n\n\n'
    "def f(x):\n    try:\n        return x\n    except:\n        return None\n"
)


class ReportTestBase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.root, "bad.py"), "w", encoding="utf-8") as handle:
            handle.write(BUGGY)
        # 密钥用拼接构造：既验证"报告不泄露密钥"，又不会触发我们自己的密钥规则
        self.cfg = Config(api_key="sk-" + "should-never-appear-0001", model="test-model")
        self.scan = scan_workspace(self.root, self.cfg)
        self.outcome = AgentOutcome(
            summary="测试摘要：存在硬编码密钥。",
            verdict="request_changes",
            findings=self.scan.findings,
            mode="online",
            model="test-model",
            steps=2,
            tool_calls=3,
            duration=1.25,
        )


class TestMarkdown(ReportTestBase):
    def test_sections_present(self) -> None:
        markdown = render_markdown(self.scan, self.outcome, cfg=self.cfg)
        for heading in ("# 代码审查报告", "## 一、结论摘要", "## 二、问题清单", "## 三、分类统计", "## 四、扫描范围与限制"):
            self.assertIn(heading, markdown)
        self.assertIn("SEC001", markdown)
        self.assertIn("bad.py:1", markdown)
        self.assertIn("要求修改", markdown)

    def test_degraded_reason_is_visible(self) -> None:
        self.outcome.degraded = True
        self.outcome.degraded_reason = "LLM 调用失败：超时"
        markdown = render_markdown(self.scan, self.outcome, cfg=self.cfg)
        self.assertIn("降级说明", markdown)
        self.assertIn("超时", markdown)

    def test_empty_findings_message(self) -> None:
        outcome = AgentOutcome(summary="没有发现问题", mode="online")
        markdown = render_markdown(self.scan, outcome, cfg=self.cfg)
        self.assertIn("未发现需要报告的问题", markdown)


class TestJson(ReportTestBase):
    def test_json_is_serializable_and_counts(self) -> None:
        payload = render_json(self.scan, self.outcome, self.cfg)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("findings", payload)
        self.assertGreaterEqual(payload["counts"]["findings"], 1)

    def test_api_key_never_leaks(self) -> None:
        payload = render_json(self.scan, self.outcome, self.cfg)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sk-should-never-appear-0001", text)
        markdown = render_markdown(self.scan, self.outcome, cfg=self.cfg)
        self.assertNotIn("sk-should-never-appear-0001", markdown)
        self.assertNotIn("api_key", payload.get("config", {}))


class TestMarkdownToHtml(unittest.TestCase):
    def test_headings_tables_and_code(self) -> None:
        markup = markdown_to_html(
            "# 标题\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- 项目一\n\n```\ncode()\n```\n\n**粗体** 和 `行内`\n"
        )
        self.assertIn("<h1>标题</h1>", markup)
        self.assertIn("<table>", markup)
        self.assertIn("<li>项目一</li>", markup)
        self.assertIn("<pre><code>code()</code></pre>", markup)
        self.assertIn("<strong>粗体</strong>", markup)
        self.assertIn("<code>行内</code>", markup)

    def test_html_is_escaped(self) -> None:
        markup = markdown_to_html("- <script>alert(1)</script>")
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)


class TestWebServer(ReportTestBase):
    def setUp(self) -> None:
        super().setUp()
        handler = make_handler(self.cfg, Console(color=False, quiet=True))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_index_page(self) -> None:
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertIn("Code Review Agent", body)

    def test_health(self) -> None:
        data = self.get_json("/api/health")
        self.assertTrue(data["ok"])
        self.assertEqual(data["model"], "test-model")

    def test_scan_endpoint(self) -> None:
        status, data = self.post_json("/api/scan", {"path": self.root})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["scan"]["stats"]["files"], 1)

    def test_review_endpoint_offline_mode(self) -> None:
        status, data = self.post_json("/api/review", {"path": self.root, "offline": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("report_markdown", data)
        self.assertIn("<h1>", data["report_html"])

    def test_unknown_path_returns_404(self) -> None:
        status, data = self.post_json("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])


class TestCliPort(unittest.TestCase):
    """端口参数必须被限制在合法范围，避免把 Python 堆栈抛给用户。"""

    def test_valid_port(self) -> None:
        from cra.cli import _port

        self.assertEqual(_port("8765"), 8765)

    def test_invalid_ports_are_rejected(self) -> None:
        import argparse

        from cra.cli import _port

        for bad in ("0", "65536", "99999", "-1", "abc"):
            with self.subTest(port=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _port(bad)


class TestPortDiagnostics(unittest.TestCase):
    """端口冲突诊断：拿不到信息也必须安全降级，绝不能因为诊断本身抛异常。"""

    def test_never_raises(self) -> None:
        from cra.web.server import describe_port_owner

        self.assertIsInstance(describe_port_owner(8765), str)

    def test_busy_port_reports_pid_when_available(self) -> None:
        import os
        import socket

        from cra.web.server import describe_port_owner

        if os.name != "nt":
            self.skipTest("该诊断仅在 Windows 上提供")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            text = describe_port_owner(port)
        # 受限环境下可能拿不到信息（返回空串）；一旦拿到就必须是可用的提示
        if text:
            self.assertIn("PID", text)
            self.assertIn("taskkill", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
