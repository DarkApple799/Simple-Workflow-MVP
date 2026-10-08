"""`wfa/web/server.py` 单元测试：真实起 HTTP 服务并走完整 HTTP 往返。

覆盖点（对应 CONTRACT 第 12 节）：
    1. 用 ``wfa.web.server.serve(host="127.0.0.1", port=0)`` 起真实服务（端口 0 = 随机端口，
       从 ``httpd.server_address[1]`` 取真实端口），在守护线程里 ``serve_forever``，
       测试结束 ``shutdown()`` + ``server_close()``；
    2. ``GET /api/health``、``/api/workflows``（含 nodes/edges/mermaid）、``/api/agents``、
       ``GET /`` 与 ``/static/index.html`` 单页 UI、未知路径 404；
    3. ``POST /api/run``（离线 docs 工作流）→ ``run_id`` → 轮询 ``GET /api/runs/<id>`` 到结束，
       校验事件增量（``?since=``）、终态、``report`` 载荷与工作区产物；
    4. ``GET /api/runs/<id>/report`` 返回 ``text/markdown`` 且内容为运行报告；
    5. 错误路径：未知工作流 → 运行记录 ``failed`` 且带错误原因；未知 run_id → 404。

全部离线：服务以 ``offline=True`` 启动，模型是 ``RuleChatModel``（不需要 API Key、不触网）。
HTTP 客户端用 ``urllib`` 并显式禁用代理，避免本机代理干扰本地回环请求。
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import temp_workspace
from wfa.web.server import RunRegistry, RunRecord, WorkflowService, serve


class WebServerTestCase(unittest.TestCase):
    """起一个真实服务供本类所有用例复用。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = serve(host="127.0.0.1", port=0, offline=True)
        cls.host, cls.port = cls.httpd.server_address[:2]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    # ---- HTTP 小工具 ----
    def request(self, method: str, path: str, payload: dict | None = None, timeout: float = 60.0):
        """发一次请求，返回 ``(status, headers, body_bytes)``（4xx/5xx 不抛异常）。"""
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def get_json(self, path: str, timeout: float = 60.0):
        status, headers, body = self.request("GET", path, timeout=timeout)
        return status, headers, json.loads(body.decode("utf-8"))

    def post_json(self, path: str, payload: dict):
        status, headers, body = self.request("POST", path, payload)
        return status, headers, json.loads(body.decode("utf-8"))

    def poll_run(self, run_id: str, timeout: float = 120.0) -> dict:
        """轮询运行状态直到不再是 running。"""
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            status, _headers, data = self.get_json("/api/runs/" + run_id)
            self.assertEqual(status, 200)
            last = data
            if data.get("status") != "running":
                return data
            time.sleep(0.1)
        self.fail("运行 %s 未在 %.0fs 内结束（最后状态 %s）"
                  % (run_id, timeout, last.get("status")))


class HealthAndStaticTest(WebServerTestCase):
    """健康检查、单页 UI 与静态资源。"""

    def test_health(self):
        status, headers, data = self.get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["version"])
        self.assertRegex(data["time"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertIn("application/json", headers["Content-Type"])

    def test_index_page(self):
        status, headers, body = self.request("GET", "/")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("<title>", text)
        self.assertIn("Workflow", text)
        self.assertIn("/api/health", text)          # 页面确实会调用后端接口

    def test_static_index(self):
        status, headers, body = self.request("GET", "/static/index.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("多 Agent", body.decode("utf-8"))

    def test_unknown_path_404(self):
        status, _headers, data = self.get_json("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "not found")
        self.assertEqual(data["path"], "/api/nope")

    def test_unknown_static_404(self):
        status, _headers, data = self.get_json("/static/missing.js")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "static not found")

    def test_head_request(self):
        status, headers, body = self.request("HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("Content-Length", headers)


class WorkflowsAndAgentsTest(WebServerTestCase):
    """工作流与 Agent 清单接口。"""

    def test_workflows(self):
        status, _headers, data = self.get_json("/api/workflows")
        self.assertEqual(status, 200)
        names = [item["name"] for item in data["workflows"]]
        self.assertEqual(names, ["code_review", "docs"])
        for item in data["workflows"]:
            with self.subTest(name=item["name"]):
                self.assertTrue(item["description"])
                self.assertTrue(item["agents"])
                self.assertTrue(item["nodes"])
                self.assertTrue(item["edges"])
                self.assertIn("flowchart", item["mermaid"])
                self.assertNotIn("error", item)

    def test_code_review_nodes_include_gate(self):
        _status, _headers, data = self.get_json("/api/workflows")
        code_review = next(item for item in data["workflows"]
                           if item["name"] == "code_review")
        node_ids = [node["id"] for node in code_review["nodes"]]
        self.assertIn("code", node_ids)
        self.assertIn("gate_quality", node_ids)
        gate = next(node for node in code_review["nodes"] if node["id"] == "gate_quality")
        self.assertEqual(gate["type"], "gate")
        self.assertEqual(code_review["max_iterations"], 3)

    def test_agents(self):
        status, _headers, data = self.get_json("/api/agents")
        self.assertEqual(status, 200)
        names = [item["name"] for item in data["agents"]]
        self.assertEqual(len(names), 12)
        for name in ("coder", "reviewer", "fixer", "documenter", "legacy_reviewer"):
            self.assertIn(name, names)
        for item in data["agents"]:
            self.assertEqual(set(item), {"name", "role", "describe"})
            self.assertTrue(item["role"])


class RunLifecycleTest(WebServerTestCase):
    """POST /api/run + 轮询 /api/runs/<id> + 报告接口。"""

    def test_offline_docs_run_to_completion(self):
        with temp_workspace("web-run") as ws:
            status, _headers, started = self.post_json("/api/run", {
                "workflow": "docs",
                "task": "写一份多 Agent 协作说明",
                "workspace": ws,
                "offline": True,
                "max_iterations": 2,
                "max_turns": 30,
            })
            self.assertEqual(status, 200)
            self.assertTrue(started["run_id"].startswith("run-"))

            final = self.poll_run(started["run_id"])

            self.assertIn(final["status"], ("success", "partial", "success_with_issues"))
            self.assertEqual(final["workflow"], "docs")
            self.assertEqual(final["mode"], "offline")
            self.assertTrue(final["finished_at"])
            self.assertGreater(final["event_count"], 0)
            self.assertTrue(final["report"])

            events = [event["event"] for event in final["events"]]
            self.assertIn("turn_start", events)
            self.assertIn("node_end", events)
            self.assertIn("stop", events)

            report = final["report"]
            self.assertEqual(report["workflow"], "docs")
            self.assertIn("node_runs", report)
            self.assertTrue(report["node_runs"])
            self.assertIn("docs/outline.md", report["artifacts"])
            # 文档 Agent 确实把内容写进了工作区
            self.assertTrue(os.path.isfile(os.path.join(ws, "docs", "outline.md")))

    def test_run_report_endpoint(self):
        with temp_workspace("web-report") as ws:
            _status, _headers, started = self.post_json("/api/run", {
                "workflow": "docs", "task": "生成报告", "workspace": ws, "offline": True})
            final = self.poll_run(started["run_id"])
            self.assertNotEqual(final["status"], "running")

            status, headers, body = self.request(
                "GET", "/api/runs/%s/report" % started["run_id"])
            text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertIn("# 运行报告 · docs", text)
        self.assertIn("## 1. 运行概览", text)
        self.assertIn("## 5. 产物清单", text)

    def test_incremental_events_with_since(self):
        with temp_workspace("web-since") as ws:
            _status, _headers, started = self.post_json("/api/run", {
                "workflow": "docs", "workspace": ws, "offline": True})
            final = self.poll_run(started["run_id"])

            _s, _h, all_events = self.get_json("/api/runs/" + started["run_id"] + "?since=0")
            self.assertEqual(len(all_events["events"]), all_events["event_count"])

            tail = all_events["event_count"]
            _s2, _h2, remainder = self.get_json(
                "/api/runs/%s?since=%d" % (started["run_id"], tail))
            self.assertEqual(remainder["events"], [])
            self.assertEqual(remainder["event_count"], tail)

    def test_runs_listing(self):
        with temp_workspace("web-list") as ws:
            _status, _headers, started = self.post_json("/api/run", {
                "workflow": "docs", "workspace": ws, "offline": True})
            self.poll_run(started["run_id"])
            status, _headers, data = self.get_json("/api/runs")
        self.assertEqual(status, 200)
        self.assertIn(started["run_id"], [item["run_id"] for item in data["runs"]])

    def test_unknown_workflow_fails_with_reason(self):
        with temp_workspace("web-badwf") as ws:
            _status, _headers, started = self.post_json("/api/run", {
                "workflow": "no-such-workflow", "workspace": ws, "offline": True})
            final = self.poll_run(started["run_id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("no-such-workflow", final["error"])

    def test_unknown_run_id_404(self):
        status, _headers, data = self.get_json("/api/runs/run-not-exists")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "run not found")

    def test_unknown_run_report_404(self):
        status, _headers, data = self.get_json("/api/runs/run-not-exists/report")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "run not found")

    def test_post_unknown_path_404_with_body(self):
        """回归：POST 未知路径**带请求体**也必须稳定拿到 404 JSON。

        服务端若不按 Content-Length 排空请求体就关连接，Windows 会回 RST，
        客户端偶发 ConnectionAbortedError [WinError 10053]（已修：`_drain_body()`）。
        这里连续发 12 次带 body 的请求，必须每次都拿到 404 JSON。
        """
        for attempt in range(12):
            with self.subTest(attempt=attempt):
                status, _headers, data = self.post_json(
                    "/api/nope", {"probe": "x" * 128, "attempt": attempt})
                self.assertEqual(status, 404)
                self.assertEqual(data["error"], "not found")
                self.assertEqual(data["path"], "/api/nope")

    def test_post_unknown_path_404_without_body(self):
        status, _headers, body = self.request("POST", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "not found")

    def test_run_uses_default_workspace_when_absent(self):
        # 不传 workspace 时走默认目录；只校验服务端接受了请求并返回 run_id。
        status, _headers, started = self.post_json(
            "/api/run", {"workflow": "docs", "offline": True, "max_turns": 1})
        self.assertEqual(status, 200)
        self.assertTrue(started["run_id"])
        final = self.poll_run(started["run_id"])
        self.assertNotEqual(final["status"], "running")
        self.assertTrue(final["workspace"])


class ServiceUnitTest(unittest.TestCase):
    """服务层纯逻辑（不起 HTTP）。"""

    def test_workflows_service(self):
        service = WorkflowService(offline=True)
        names = [item["name"] for item in service.workflows()]
        self.assertEqual(names, ["code_review", "docs"])

    def test_agents_service(self):
        agents = WorkflowService(offline=True).agents()
        self.assertEqual(len(agents), 12)

    def test_run_registry_limit_and_get(self):
        registry = RunRegistry(limit=2)
        first = registry.create(workflow="docs", task="t", workspace=".", mode="offline")
        second = registry.create(workflow="docs", task="t", workspace=".", mode="offline")
        third = registry.create(workflow="docs", task="t", workspace=".", mode="offline")
        self.assertIsNone(registry.get(first.run_id))
        self.assertIs(registry.get(second.run_id), second)
        self.assertEqual([item.run_id for item in registry.all()],
                         [second.run_id, third.run_id])

    def test_run_record_to_dict_and_events_since(self):
        record = RunRecord(run_id="run-1", workflow="docs", task="t", workspace=".",
                           mode="offline")
        record.add_event("turn_start", {"turn": 1})
        record.add_event("stop", {"reason": "completed"})
        self.assertEqual(len(record.events_since(1)), 1)
        payload = record.to_dict(since=1)
        self.assertEqual(payload["event_count"], 2)
        self.assertEqual([item["event"] for item in payload["events"]], ["stop"])
        self.assertEqual(payload["status"], "running")
        json.dumps(record.to_dict(0), ensure_ascii=False)

    def test_two_servers_get_distinct_random_ports(self):
        first = serve(host="127.0.0.1", port=0, offline=True)
        second = serve(host="127.0.0.1", port=0, offline=True)
        try:
            self.assertNotEqual(first.server_address[1], second.server_address[1])
            self.assertTrue(first.daemon_threads)
            self.assertTrue(second.daemon_threads)
        finally:
            first.server_close()
            second.server_close()


if __name__ == "__main__":
    unittest.main()
