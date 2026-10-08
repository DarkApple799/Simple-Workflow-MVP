"""零依赖 Web 界面：用标准库 `http.server` 展示并驱动 Workflow。

接口一览：

===========  ==================================  ==========================================
方法         路径                                 说明
===========  ==================================  ==========================================
GET          /                                   单页 UI（static/index.html）
GET          /api/workflows                      预置工作流清单（含 Mermaid 拓扑）
GET          /api/agents                         Agent 清单
POST         /api/run                            启动一次运行（后台线程），返回 run_id
GET          /api/runs/<id>                      运行状态 + 增量事件 + 报告
GET          /api/runs/<id>/report               运行报告（text/markdown）
GET          /api/health                         健康检查
===========  ==================================  ==========================================

为什么不用 Flask/FastAPI：本项目坚持零第三方依赖（与 Homework1 一致），
而 http.server + 单页 UI 足以展示"编排可视化 + 实时日志"这两个重点。
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__
from ..config import PROJECT_ROOT, load_config
from ..errors import ConfigError, WorkflowError
from ..model import build_model
from ..util import new_id, now_iso, truncate

logger = logging.getLogger("wfa.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_WORKSPACE = os.path.join(PROJECT_ROOT, "examples", "workflow_workspace")


# --------------------------------------------------------------------------- #
# 运行注册表：保存每次运行的线程、事件与结果
# --------------------------------------------------------------------------- #
@dataclass
class RunRecord:
    """一次 Web 触发的运行。"""

    run_id: str
    workflow: str
    task: str
    workspace: str
    mode: str
    status: str = "running"
    created_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    events: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -------------------------------------------------------------- #
    def add_event(self, event: str, payload: dict) -> None:
        with self._lock:
            self.events.append({"event": event, "payload": payload, "at": now_iso()})

    def events_since(self, index: int) -> list:
        with self._lock:
            return list(self.events[index:])

    def to_dict(self, since: int = 0) -> dict:
        with self._lock:
            events = list(self.events[since:])
            total = len(self.events)
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "task": self.task,
            "workspace": self.workspace,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "event_count": total,
            "events": events,
            "error": self.error,
            "report": self.report,
        }


class RunRegistry:
    """线程安全的运行记录表。"""

    def __init__(self, limit: int = 50) -> None:
        self._runs: dict = {}
        self._order: list = []
        self._lock = threading.Lock()
        self._limit = limit

    def create(self, **kwargs) -> RunRecord:
        record = RunRecord(run_id=new_id("run"), **kwargs)
        with self._lock:
            self._runs[record.run_id] = record
            self._order.append(record.run_id)
            while len(self._order) > self._limit:
                self._runs.pop(self._order.pop(0), None)
        return record

    def get(self, run_id: str):
        with self._lock:
            return self._runs.get(run_id)

    def all(self) -> list:
        with self._lock:
            return [self._runs[rid] for rid in self._order if rid in self._runs]


# --------------------------------------------------------------------------- #
# 业务逻辑
# --------------------------------------------------------------------------- #
class WorkflowService:
    """把 CLI 的能力包装成可被 HTTP 调用的服务。"""

    def __init__(self, *, offline: bool = True, model: str | None = None, write_workspace: bool = True) -> None:
        self.offline = offline
        self.model_name = model
        self.write_workspace = write_workspace
        self.runs = RunRegistry()
        self._lock = threading.Lock()

    # -------------------------------------------------------------- #
    def workflows(self) -> list:
        from ..workflows import build_workflow, list_workflows

        items = []
        for meta in list_workflows():
            item = dict(meta)
            try:
                workflow = build_workflow(meta["name"])
                item["nodes"] = [
                    {"id": node.id, "agent": node.agent, "type": node.type,
                     "description": node.description, "loop_of": node.loop_of}
                    for node in workflow.nodes()
                ]
                item["edges"] = [
                    {"src": edge.src, "dst": edge.dst, "when": edge.when, "label": edge.label}
                    for edge in workflow.edges()
                ]
                item["mermaid"] = workflow.to_mermaid()
                item["max_iterations"] = workflow.max_iterations
            except Exception as exc:  # noqa: BLE001 - 单个流程构造失败不应影响列表
                item["error"] = f"{type(exc).__name__}: {exc}"
            items.append(item)
        return items

    def agents(self) -> list:
        from ..agents import default_registry
        from ..model import RuleChatModel

        registry = default_registry(RuleChatModel())
        return registry.describe()

    # -------------------------------------------------------------- #
    def start_run(self, payload: dict) -> RunRecord:
        """启动一次后台运行，立即返回 RunRecord（事件通过轮询获取）。"""
        workflow_name = str(payload.get("workflow") or "code_review")
        task = str(payload.get("task") or "按需求实现一个小而完整的 Python 模块，并完成审查与修复")
        workspace = os.path.abspath(str(payload.get("workspace") or DEFAULT_WORKSPACE))
        parallel_review = bool(payload.get("parallel_review"))
        max_iterations = int(payload.get("max_iterations") or 3)
        max_turns = int(payload.get("max_turns") or 50)
        offline = bool(payload.get("offline", self.offline))

        record = self.runs.create(
            workflow=workflow_name,
            task=task,
            workspace=workspace,
            mode="offline" if offline else "online",
        )
        thread = threading.Thread(
            target=self._execute,
            args=(record, parallel_review, max_iterations, max_turns, offline),
            name=f"wfa-{record.run_id}",
            daemon=True,
        )
        thread.start()
        return record

    def _execute(self, record: RunRecord, parallel_review: bool, max_iterations: int, max_turns: int, offline: bool) -> None:
        """后台线程里的完整运行流程。"""
        from ..agents import default_registry
        from ..bus import MessageBus
        from ..engine import RunConfig, WorkflowEngine
        from ..state import Blackboard
        from ..workflows import build_workflow

        try:
            cfg = load_config(
                root=record.workspace,
                mode="offline" if offline else "online",
                model=self.model_name,
                max_turns=max_turns,
                max_iterations=max_iterations,
                parallel_workers=3,
                write_workspace=self.write_workspace,
            )
            workflow = build_workflow(
                record.workflow, parallel_review=parallel_review,
                max_iterations=max_iterations, max_turns=max_turns,
            )
            model = build_model(cfg)
            agents = default_registry(model, config=cfg)
            run_config = RunConfig(
                task=record.task,
                workspace=record.workspace,
                mode=cfg.mode,
                model=cfg.model if cfg.mode == "online" else "offline-rule-model",
                max_turns=max_turns,
                max_iterations=max_iterations,
                review_max_steps=cfg.review_max_steps,
                stop_on_blocking=cfg.stop_on_blocking,
                parallel_workers=cfg.parallel_workers,
                extra={"workflow": record.workflow},
            )
            bus = MessageBus()
            state = Blackboard(workspace=record.workspace, bus=bus)
            state.set("workspace", record.workspace)
            state.set("task", record.task)
            engine = WorkflowEngine(
                workflow, agents, config=run_config, bus=bus, state=state,
                on_event=record.add_event,
            )
            report = engine.run()
            payload = report.to_dict()
            payload.update({
                "task": record.task,
                "workspace": record.workspace,
                "model": run_config.model,
                "workflow": record.workflow,
            })
            record.report = payload
            blocking = payload.get("blocking") or []
            record.status = "success" if payload.get("status") == "success" else (
                "partial" if payload.get("status") == "partial" else "failed"
            )
            if blocking and record.status == "success":
                record.status = "success_with_issues"
        except (ConfigError, WorkflowError) as exc:
            record.status = "failed"
            record.error = str(exc)
            logger.warning("Web 运行失败：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 后台线程异常必须落到记录里
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            record.add_event("error", {"traceback": truncate(traceback.format_exc(), 2000)})
            logger.exception("Web 运行异常")
        finally:
            record.finished_at = now_iso()


# --------------------------------------------------------------------------- #
# HTTP 处理器
# --------------------------------------------------------------------------- #
class WorkflowHTTPHandler(BaseHTTPRequestHandler):
    """极简路由器。`service` 由 `serve()` 注入。"""

    service: WorkflowService
    server_version = f"wfa/{__version__}"

    # ---- 日志：静默，避免刷屏（真正的事件流走 /api/runs/<id>）----
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 工具 ----
    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        self._send(status, text.encode("utf-8"), content_type)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                return self._serve_static("index.html")
            if path == "/api/health":
                return self._send_json({"ok": True, "version": __version__, "time": now_iso()})
            if path == "/api/workflows":
                return self._send_json({"workflows": self.service.workflows()})
            if path == "/api/agents":
                return self._send_json({"agents": self.service.agents()})
            if path == "/api/runs":
                return self._send_json({"runs": [r.to_dict(0) for r in self.service.runs.all()]})
            if path.startswith("/api/runs/"):
                return self._handle_run(path, query)
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            return self._send_json({"error": "not found", "path": path}, status=404)
        except BrokenPipeError:  # pragma: no cover - 客户端提前断开
            return
        except Exception as exc:  # noqa: BLE001 - HTTP 层兜底
            logger.exception("GET %s 失败", path)
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _drain_body(self) -> None:
        """读掉并丢弃请求体。

        即使本次请求是 404，也必须把 `Content-Length` 指定的字节读干净再关连接，
        否则 Windows 上客户端可能先收到 RST（`ConnectionAbortedError [WinError 10053]`），
        拿不到本该返回的 404 JSON。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if length > 0:
            try:
                self.rfile.read(length)
            except (OSError, ValueError):  # pragma: no cover - 客户端提前断开
                pass

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/run":
                payload = self._read_json()
                record = self.service.start_run(payload)
                return self._send_json({"run_id": record.run_id, "status": record.status})
            self._drain_body()
            return self._send_json({"error": "not found", "path": path}, status=404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST %s 失败", path)
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    # ---- 具体处理 ----
    def _handle_run(self, path: str, query: dict) -> None:
        tail = path[len("/api/runs/"):]
        if tail.endswith("/report"):
            run_id = tail[: -len("/report")]
            record = self.service.runs.get(run_id)
            if record is None:
                return self._send_json({"error": "run not found", "run_id": run_id}, status=404)
            from ..report import render_run_report

            return self._send_text(render_run_report(record.report or {"workflow": record.workflow,
                                                                      "status": record.status,
                                                                      "notes": [record.error] if record.error else []}),
                                   content_type="text/markdown; charset=utf-8")
        record = self.service.runs.get(tail)
        if record is None:
            return self._send_json({"error": "run not found", "run_id": tail}, status=404)
        since = int((query.get("since") or ["0"])[0] or 0)
        return self._send_json(record.to_dict(since=since))

    def _serve_static(self, name: str) -> None:
        safe = os.path.basename(name) or "index.html"
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(path):
            return self._send_json({"error": "static not found", "name": safe}, status=404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as handle:
            self._send(200, handle.read(), ctype)


# --------------------------------------------------------------------------- #
# 服务启动
# --------------------------------------------------------------------------- #
def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    offline: bool = True,
    model: str | None = None,
    write_workspace: bool = True,
    open_browser: bool = False,
) -> ThreadingHTTPServer:
    """创建并返回 HTTP 服务（不阻塞；调用方负责 `serve_forever()`）。"""
    service = WorkflowService(offline=offline, model=model, write_workspace=write_workspace)

    class Handler(WorkflowHTTPHandler):
        pass

    Handler.service = service
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    logger.info("wfa Web 已启动：http://%s:%s", host, port)
    return httpd


def main(argv=None) -> int:
    """`python webui.py` 入口。"""
    from ..cli import setup_console

    setup_console()
    parser = argparse.ArgumentParser(prog="webui.py", description="wfa 零依赖 Web 界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--online", action="store_true", help="默认使用真实 LLM（需要 API Key）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-write-workspace", action="store_true", help="只分析不写工作区")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    httpd = serve(
        args.host,
        args.port,
        offline=not args.online,
        model=args.model,
        write_workspace=not args.no_write_workspace,
        open_browser=not args.no_open,
    )
    url = f"http://{args.host}:{args.port}"
    print(f"wfa v{__version__} Web 界面：{url}")
    print("提示：必须带端口号访问（不带端口走的是 80 端口）。按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\n正在停止服务…")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
