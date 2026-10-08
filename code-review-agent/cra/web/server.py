"""轻量 Web 界面（仅用标准库 http.server，零第三方依赖）。

为什么不用 Flask/FastAPI：作业只要求"命令行或简单 Web 界面"，而标准库实现
不会给评审老师增加任何安装负担——`python webui.py` 就能跑。
整体结构：/ 返回单页 UI，/api/review 与 /api/scan 提供 JSON 接口，
服务端把 Markdown 报告转成 HTML 片段返回，前端只负责展示。
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import __version__
from ..agent import CodeReviewAgent
from ..config import Config
from ..errors import CodeReviewAgentError, ConfigError
from ..fsutil import ensure_root
from ..report import render_markdown
from ..scanner import scan_workspace

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: 同一时刻只跑一个审查任务（演示用，避免并发烧 token）
_REVIEW_LOCK = threading.Lock()


class _WebServer(ThreadingHTTPServer):
    """定制版 HTTP 服务。

    关键点：Windows 上 SO_REUSEADDR 允许**第二个进程绑定同一端口**，
    于是"双击两次启动脚本"会静默出现两个服务、请求落到哪个全看运气。
    这里在 Windows 下关掉它，端口冲突时直接报错，行为可预期。
    """

    daemon_threads = True
    if os.name == "nt":  # pragma: no cover - 平台相关
        allow_reuse_address = False

_FENCE_RE = re.compile(r"^```")
_TABLE_SEP_RE = re.compile(r"^\|[\s\-:|]+\|$")


# --------------------------------------------------------------------------- #
# 极简 Markdown → HTML（只覆盖报告用到的语法，避免引入依赖）
# --------------------------------------------------------------------------- #
def inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def markdown_to_html(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    in_list = False
    in_code = False
    code_buffer: list[str] = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while index < len(lines):
        line = lines[index]

        if _FENCE_RE.match(line.strip()):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buffer)) + "</code></pre>")
                code_buffer = []
                in_code = False
            else:
                close_list()
                in_code = True
            index += 1
            continue
        if in_code:
            code_buffer.append(line)
            index += 1
            continue

        stripped = line.strip()

        # 表格
        if stripped.startswith("|") and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1].strip()):
            close_list()
            header = [cell.strip() for cell in stripped.strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
                index += 1
            out.append("</tbody></table>")
            continue

        if not stripped:
            close_list()
            index += 1
            continue

        if re.match(r"^#{1,6}\s", stripped):
            close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append(f"<h{level}>{inline(stripped[level:].strip())}</h{level}>")
            index += 1
            continue

        if stripped in ("---", "***", "___"):
            close_list()
            out.append("<hr/>")
            index += 1
            continue

        if stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
            index += 1
            continue

        close_list()
        out.append(f"<p>{inline(stripped)}</p>")
        index += 1

    if in_code and code_buffer:
        out.append("<pre><code>" + html.escape("\n".join(code_buffer)) + "</code></pre>")
    close_list()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 请求处理
# --------------------------------------------------------------------------- #
def make_handler(cfg: Config, console: Any) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"CodeReviewAgent/{__version__}"

        # ---------------- 基础设施 ---------------- #
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆盖基类
            if getattr(console, "quiet", False):
                return
            console.info(f"  [web] {self.address_string()} {fmt % args}")

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, markup: str, status: int = 200) -> None:
            body = markup.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", "replace")
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, ValueError):
                return {}

        # ---------------- 路由 ---------------- #
        def do_GET(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                index = os.path.join(STATIC_DIR, "index.html")
                if os.path.isfile(index):
                    with open(index, "r", encoding="utf-8") as handle:
                        self._send_html(handle.read())
                else:
                    self._send_html("<h1>缺少 static/index.html</h1>", status=500)
                return
            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "version": __version__,
                        "model": cfg.model,
                        "offline": cfg.offline,
                        "has_key": cfg.has_api_key(),
                        "cwd": os.getcwd(),
                    }
                )
                return
            self._send_json({"ok": False, "error": f"未知路径 {path}"}, status=404)

        def do_POST(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            payload = self._read_json()
            try:
                if path == "/api/review":
                    self._handle_review(payload)
                elif path == "/api/scan":
                    self._handle_scan(payload)
                else:
                    self._send_json({"ok": False, "error": f"未知路径 {path}"}, status=404)
            except ConfigError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except CodeReviewAgentError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:  # noqa: BLE001 - Web 层兜底，绝不能让服务挂掉
                console.error(f"Web 请求异常：{type(exc).__name__}: {exc}")
                self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, status=500)

        # ---------------- 业务 ---------------- #
        def _build_config(self, payload: dict) -> Config:
            focus = payload.get("focus") or []
            if isinstance(focus, str):
                focus = [item.strip() for item in focus.split(",") if item.strip()]
            max_steps = payload.get("max_steps")
            return dataclasses.replace(
                cfg,
                offline=bool(payload.get("offline", cfg.offline)),
                focus=tuple(str(item) for item in focus),
                max_steps=int(max_steps) if max_steps else cfg.max_steps,
            )

        def _handle_review(self, payload: dict) -> None:
            raw_path = str(payload.get("path") or ".").strip() or "."
            if not _REVIEW_LOCK.acquire(blocking=False):
                self._send_json({"ok": False, "error": "已有审查任务在运行，请等待它完成后再试。"}, status=429)
                return
            try:
                root = ensure_root(raw_path)
                run_cfg = self._build_config(payload)
                only_file = raw_path if os.path.isfile(raw_path) else None
                agent = CodeReviewAgent(run_cfg)
                started = time.time()
                scan_result, outcome = agent.review(
                    root,
                    focus=run_cfg.focus,
                    extra_instruction=str(payload.get("instruction") or ""),
                    only_file=only_file,
                )
                markdown = render_markdown(scan_result, outcome, cfg=run_cfg)
                self._send_json(
                    {
                        "ok": True,
                        "root": scan_result.root,
                        "mode": outcome.mode,
                        "degraded": outcome.degraded,
                        "degraded_reason": outcome.degraded_reason,
                        "summary": outcome.summary,
                        "verdict": outcome.verdict,
                        "counts": {
                            "files": len(scan_result.files),
                            "findings": len(outcome.findings),
                            "by_severity": scan_result.counts_by_severity() if not outcome.findings else _severity_counts(outcome.findings),
                        },
                        "findings": [finding.to_dict() for finding in outcome.findings],
                        "report_markdown": markdown,
                        "report_html": markdown_to_html(markdown),
                        "duration": round(time.time() - started, 2),
                        "usage": {
                            "steps": outcome.steps,
                            "tool_calls": outcome.tool_calls,
                            "llm_calls": outcome.llm_calls,
                            "prompt_tokens": outcome.prompt_tokens,
                            "completion_tokens": outcome.completion_tokens,
                        },
                    }
                )
            finally:
                _REVIEW_LOCK.release()

        def _handle_scan(self, payload: dict) -> None:
            raw_path = str(payload.get("path") or ".").strip() or "."
            root = ensure_root(raw_path)
            run_cfg = self._build_config(payload)
            only_file = raw_path if os.path.isfile(raw_path) else None
            result = scan_workspace(root, run_cfg, only_file=only_file)
            self._send_json({"ok": True, "scan": result.to_dict()})

    return Handler


def _severity_counts(findings: list) -> dict:
    counts = {key: 0 for key in ("critical", "high", "medium", "low", "info")}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
def self_check(url: str, console: Any) -> bool:
    """绑定成功后立刻请求一次自己的 /api/health。

    这样"服务已就绪"不是猜的：绑定端口成功但被防火墙拦截、或端口被别的程序占用时，
    用户会立刻看到提示，而不是在浏览器里对着 ERR_CONNECTION_REFUSED 发呆。
    """
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if console is not None:
            console.ok(f"自检通过：{url}/api/health 返回 ok={payload.get('ok')}")
        return True
    except Exception as exc:  # noqa: BLE001 - 自检失败只提示，不阻断
        if console is not None:
            console.warn(f"自检未通过（{type(exc).__name__}: {exc}），可能被防火墙/安全软件拦截。")
        return False


def describe_port_owner(port: int) -> str:
    """尽力找出"谁占用了这个端口"，返回给用户看的提示（失败就返回空串）。

    只在 Windows 上做（调用系统自带的 netstat / tasklist），且完全容错：
    拿不到信息就不显示，绝不影响主流程。
    """
    if os.name != "nt":  # pragma: no cover - 平台相关
        return ""
    try:
        listing = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:  # noqa: BLE001 - 诊断信息拿不到就算了
        return ""

    pids: list[str] = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}") and parts[4] not in pids:
                pids.append(parts[4])
    if not pids:
        return ""

    described = []
    for pid in pids:
        name = ""
        try:
            row = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if row:
                name = row.split(",")[0].strip('"')
        except Exception:  # noqa: BLE001
            pass
        described.append(f"PID {pid}" + (f"（{name}）" if name else ""))

    kill_cmds = "  ".join(f"taskkill /PID {pid} /F" for pid in pids)
    return (
        f"  占用端口的进程：{'、'.join(described)}\n"
        f"  结束它（确认那是你自己启动的旧服务）：{kill_cmds}\n"
        f"  或者换个端口：--port {port + 1}"
    )


def serve(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    console: Any = None,
    cwd: str | None = None,
) -> None:
    """启动 Web 服务（阻塞直到 Ctrl+C）。"""
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    handler = make_handler(cfg, console)
    try:
        httpd = _WebServer((host, port), handler)
    except (OSError, OverflowError, ValueError) as exc:
        # 端口被占用、端口越界、地址非法等，都给出可操作的提示而不是堆栈
        if console is not None:
            # 一次性输出，保证提示顺序稳定（拆成多次 print 时 stdout/stderr 会交错）
            console.error(
                f"无法监听 {host}:{port} —— {type(exc).__name__}: {exc}\n"
                + describe_port_owner(port)
                + "\n  端口范围必须是 1~65535。"
            )
        raise SystemExit(1) from exc

    url = f"http://{host}:{port}"
    if console is not None:
        console.ok(f"Web 界面已启动：{url}")
        console.info(f"  ▶ 浏览器地址栏请填**完整地址（含端口号）**：{url}")
        console.info(f"    只输入 {host} 会打开 80 端口，本服务不在那里，浏览器会报「拒绝连接」。")
        console.info(f"  审查根目录默认为启动目录：{os.getcwd()}")
        console.info(f"  模型：{cfg.model}；模式：{'离线规则' if cfg.offline or not cfg.has_api_key() else '在线 Agent'}")
        console.info("  按 Ctrl+C 停止服务")

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    self_check(url, console)

    if open_browser:
        try:
            webbrowser.open(url)
            if console is not None:
                console.info("  已尝试自动打开浏览器；若没弹出，请手动复制上面的地址。")
        except Exception:  # noqa: BLE001 - 无桌面环境时忽略
            pass
    try:
        while server_thread.is_alive():
            server_thread.join(0.5)
    except KeyboardInterrupt:
        if console is not None:
            console.info("\n已停止 Web 服务。")
    finally:
        httpd.shutdown()
        httpd.server_close()


__all__ = ["serve", "markdown_to_html", "inline", "self_check", "describe_port_owner"]
