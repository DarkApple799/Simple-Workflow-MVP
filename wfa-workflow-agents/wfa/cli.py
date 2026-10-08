"""命令行入口：把"声明好的工作流"跑起来。

子命令：
    run      执行一个工作流（默认 code_review）
    list     列出可用的 Agent 与 Workflow
    graph    导出流程拓扑（Mermaid / DOT）
    report   把运行结果的 JSON 重新渲染成 Markdown

退出码约定（便于接 CI）：
    0 成功　1 参数/配置错误　2 鉴权失败　3 存在阻断级问题　4 运行失败或中断
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import __version__
from .config import load_config, PROJECT_ROOT
from .errors import ConfigError, LLMAuthError, WorkflowError
from .model import build_model
from .report import render_flow_document, render_run_report, write_report
from .util import write_text

#: 退出码
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_AUTH = 2
EXIT_BLOCKING = 3
EXIT_FAILED = 4

logger = logging.getLogger("wfa.cli")


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def setup_console() -> None:
    """Windows 控制台切 UTF-8，避免中文乱码。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 某些重定向场景不支持
                pass
    if os.name == "nt":  # pragma: no cover - 仅在 Windows 生效
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _llm_health(payload: dict) -> tuple:
    """检查在线模式下模型是否真的被用上。

    Returns:
        ``(ok, message)``：``ok=False`` 表示"配了 Key 但模型调用全部失败、已降级为规则报告"。
        这种情况下流程本身仍然产出报告（这是有意的容错设计），但用户必须被告知，
        否则会误以为"在线模式跑通了"。
    """
    metrics = payload.get("metrics") or {}
    llm = metrics.get("llm") if isinstance(metrics, dict) else None
    if not isinstance(llm, dict):
        return True, ""
    calls = llm.get("calls")
    failures = llm.get("failures")
    calls = calls if isinstance(calls, int) else 0
    failures = failures if isinstance(failures, int) else 0
    if failures and (calls == 0 or failures >= calls):
        return False, (
            f"在线模型调用全部失败（{failures} 次），本次结论已降级为确定性规则报告。"
            "请检查 API Key / 网络 / 模型名，或改用 --offline。"
        )
    if failures:
        return True, f"在线模型有 {failures} 次调用失败（已重试），报告结论可能不完整。"
    if calls == 0:
        return True, (
            "在线模式已启用，但本次没有任何模型调用（例如流程只触发了确定性规则阶段）。"
        )
    return True, ""


def _blocking_count(payload: dict) -> int:
    """取阻断级问题数量，兼容三种报告形态（顶层列表 / 顶层计数 / metrics 计数）。

    `RunReport.to_dict()` 目前把阻断计数放在 `metrics["blocking"]`（int），
    若将来 engine 直接在顶层输出 `blocking` 列表，这里也能兼容。
    """
    value = payload.get("blocking")
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int):
        return value
    metrics = payload.get("metrics") or {}
    if isinstance(metrics, dict):
        count = metrics.get("blocking")
        if isinstance(count, int):
            return count
        if isinstance(count, list):
            return len(count)
    counts = payload.get("counts") or {}
    if isinstance(counts, dict):
        return int(counts.get("critical", 0) or 0) + int(counts.get("high", 0) or 0)
    return 0


def _message_count(payload: dict) -> int:
    """兼容两种报告形态：`message_count`（int）或 `messages`（列表）。"""
    value = payload.get("message_count")
    if isinstance(value, int):
        return value
    messages = payload.get("messages")
    if isinstance(messages, list):
        return len(messages)
    if isinstance(messages, int):
        return messages
    return 0


def setup_logging(verbose: bool = False) -> None:
    """统一日志格式；默认只显示 WARNING 以上，`--verbose` 打开 INFO。"""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# 事件打印（把执行器的进度事件变成一行行日志）
# --------------------------------------------------------------------------- #
EVENT_ICON = {
    "turn_start": "▶",
    "turn_end": "◀",
    "node_start": "·",
    "node_end": "✓",
    "node_failed": "✗",
    "node_skipped": "⤼",
    "parallel_start": "⑃",
    "parallel_end": "⑃",
    "stop": "■",
    "checkpoint": "💾",
}


class EventPrinter:
    """把 engine 的事件流打印成可读进度（同时收集到列表，供总结使用）。"""

    def __init__(self, verbose: bool = False, stream=None, config=None) -> None:
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.config = config
        self.events: list = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, dict(payload or {})))
        if not self.verbose and event not in ("node_start", "node_end", "node_failed", "stop", "parallel_start"):
            return
        icon = EVENT_ICON.get(event, "·")
        detail = self._describe(event, payload or {})
        self._write(f"  {icon} {event:<16} {detail}")

    def _describe(self, event: str, payload: dict) -> str:
        if event == "turn_start":
            max_turns = payload.get("max_turns")
            if max_turns is None:
                # engine 的 turn_start 载荷可能不带 max_turns；从运行配置兜底，避免打印 "1/None"。
                max_turns = getattr(getattr(self, "config", None), "max_turns", None) or "-"
            return f"回合 {payload.get('turn')}/{max_turns}"
        if event == "node_start":
            return f"[{payload.get('node')}] agent={payload.get('agent') or '-'} 迭代={payload.get('iteration', 0)}"
        if event == "node_end":
            return (
                f"[{payload.get('node')}] status={payload.get('status')} "
                f"decision={payload.get('decision') or '-'} {payload.get('summary') or ''}"
            )
        if event == "node_failed":
            return f"[{payload.get('node')}] {payload.get('error')}"
        if event == "parallel_start":
            return f"[{payload.get('node')}] 子节点 {payload.get('children')}"
        if event == "stop":
            return f"reason={payload.get('reason')} status={payload.get('status')}"
        if event == "checkpoint":
            return str(payload.get("path") or "")
        return json.dumps(payload, ensure_ascii=False)[:160]

    def _write(self, text: str) -> None:
        try:
            print(text, file=self.stream, flush=True)
        except (UnicodeEncodeError, ValueError):  # pragma: no cover
            print(text.encode("ascii", "replace").decode("ascii"), file=self.stream, flush=True)


# --------------------------------------------------------------------------- #
# run 子命令
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    """执行工作流并写出报告。"""
    from .agents import default_registry
    from .bus import MessageBus
    from .engine import RunConfig, WorkflowEngine
    from .state import Blackboard
    from .workflows import build_workflow

    workspace = os.path.abspath(args.workspace or os.path.join(PROJECT_ROOT, "examples", "workflow_workspace"))
    cfg = load_config(
        root=workspace,
        env_file=args.env_file,
        mode="online" if args.online else "offline",
        model=args.model,
        max_turns=args.max_turns,
        max_iterations=args.max_iterations,
        review_max_steps=args.review_max_steps,
        parallel_workers=args.workers,
        stop_on_blocking=not args.ignore_blocking,
        write_workspace=not args.no_write_workspace,
        verbose=args.verbose,
    )

    workflow = build_workflow(
        args.workflow,
        parallel_review=args.parallel_review,
        max_iterations=args.max_iterations,
        max_turns=args.max_turns,
    )
    model = build_model(cfg)
    agents = default_registry(model, config=cfg)

    run_config = RunConfig(
        task=args.task or "按需求实现一个小而完整的 Python 模块，并完成审查与修复",
        workspace=workspace,
        mode=cfg.mode,
        model=cfg.model if cfg.mode == "online" else "offline-rule-model",
        max_turns=args.max_turns,
        max_iterations=args.max_iterations,
        review_max_steps=args.review_max_steps,
        stop_on_blocking=not args.ignore_blocking,
        verbose=args.verbose,
        parallel_workers=args.workers,
        extra={"workflow": args.workflow},
    )

    bus = MessageBus()
    state = Blackboard(workspace=workspace, bus=bus)
    state.set("workspace", workspace)
    state.set("task", run_config.task)

    # 默认只打印关键节点事件；加 --verbose 才输出全量事件（含 turn_end 的 JSON 明细）。
    printer = EventPrinter(verbose=args.verbose, config=run_config)
    engine = WorkflowEngine(workflow, agents, config=run_config, bus=bus, state=state, on_event=printer)

    print(f"wfa v{__version__} | 工作流 {args.workflow} | {cfg.describe()}")
    print(f"工作区：{workspace}")
    started = time.time()
    try:
        report = engine.run(resume_from=args.resume_from)
    except LLMAuthError as exc:
        print(f"[鉴权失败] {exc}", file=sys.stderr)
        return EXIT_AUTH
    except WorkflowError as exc:
        print(f"[运行失败] {exc}", file=sys.stderr)
        return EXIT_FAILED

    payload = report.to_dict()
    payload["task"] = run_config.task
    payload["workspace"] = workspace
    payload["model"] = run_config.model

    print("")
    reason = (payload.get("metrics") or {}).get("stop_reason") if isinstance(payload.get("metrics"), dict) else ""
    suffix = f" | 停止原因：{reason}" if reason else ""
    print(f"状态：{payload.get('status')} | 回合：{payload.get('turns')} | 耗时：{payload.get('duration')}s{suffix}")
    print(f"问题：{payload.get('counts')} | 消息：{_message_count(payload)} | 产物：{len(payload.get('artifacts') or {})}")

    out = args.out or os.path.join(PROJECT_ROOT, "docs", "run_report.md")
    write_report(payload, out)
    print(f"报告：{os.path.abspath(out)}")
    if args.json_out:
        write_text(args.json_out, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"JSON：{os.path.abspath(args.json_out)}")
    if args.checkpoint:
        engine.checkpoint(args.checkpoint)
        print(f"检查点：{os.path.abspath(args.checkpoint)}")

    blocking = _blocking_count(payload)
    llm_ok, llm_message = _llm_health(payload)
    if llm_message:
        print(("[在线模式告警] " if not llm_ok else "[在线模式提示] ") + llm_message, file=sys.stderr)
    if blocking:
        print(f"存在阻断级问题 {blocking} 条（详见报告的问题清单）", file=sys.stderr)
        return EXIT_BLOCKING
    if not llm_ok:
        # 配了 Key 却全部调用失败：报告是降级产物，按"鉴权/在线失败"返回，避免误判为在线跑通。
        return EXIT_AUTH
    return EXIT_OK if payload.get("status") in ("success", "partial") else EXIT_FAILED


# --------------------------------------------------------------------------- #
# list / graph / report 子命令
# --------------------------------------------------------------------------- #
def cmd_list(args) -> int:
    from .agents import default_registry
    from .model import RuleChatModel
    from .workflows import build_workflow, list_workflows

    registry = default_registry(RuleChatModel())
    print(f"wfa v{__version__} —— 可用的 Agent 与 Workflow\n")
    print("Agent：")
    for item in registry.describe():
        print(f"  - {item['name']:<22} {item['role']}")
        if args.verbose:
            print(f"      {item['describe']}")
    print("\nWorkflow：")
    for item in list_workflows():
        workflow = build_workflow(item["name"])
        print(f"  - {item['name']:<14} {item['description']}")
        print(f"      节点 {len(workflow.nodes())} 个：{', '.join(node.id for node in workflow.nodes())}")
    return EXIT_OK


def cmd_graph(args) -> int:
    from .workflows import build_workflow

    workflow = build_workflow(args.workflow, parallel_review=args.parallel_review)
    text = render_flow_document(workflow) if args.format == "md" else (
        workflow.to_dot() if args.format == "dot" else workflow.to_mermaid()
    )
    if args.out:
        write_text(args.out, text)
        print(f"已写入 {os.path.abspath(args.out)}")
    else:
        print(text)
    return EXIT_OK


def cmd_report(args) -> int:
    with open(args.json, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    text = render_run_report(payload)
    if args.out:
        write_text(args.out, text)
        print(f"已写入 {os.path.abspath(args.out)}")
    else:
        print(text)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="wfa —— 多 Agent 协作的 Workflow 编排框架（Homework2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py list\n"
            "  python run.py run --workflow code_review --offline --verbose\n"
            "  python run.py run --workflow code_review --parallel-review --max-iterations 3\n"
            "  python run.py run --workflow docs --task \"写一份多 Agent 协作说明\"\n"
            "  python run.py graph --workflow code_review --format md --out docs/FLOW.md\n"
            "  python run.py report --json docs/run_report.json --out report.md\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"wfa {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="执行一个工作流")
    run.add_argument("--workflow", "-w", default="code_review", help="工作流名（默认 code_review）")
    run.add_argument("--task", "-t", default="", help="给流程的任务描述")
    run.add_argument("--workspace", default="", help="产物写入的工作区目录（默认 examples/workflow_workspace）")
    run.add_argument("--online", action="store_true", help="调用真实 LLM（需要 DEEPSEEK_API_KEY）")
    run.add_argument("--offline", action="store_true", help="强制离线（确定性规则 + 模板模型）")
    run.add_argument("--model", default=None, help="覆盖模型名")
    run.add_argument("--max-turns", type=int, default=50, help="最大回合数")
    run.add_argument("--max-iterations", type=int, default=3, help="每个循环最大迭代次数")
    run.add_argument("--review-max-steps", type=int, default=4, help="审查 Agent 最多几步工具调用")
    run.add_argument("--parallel-review", action="store_true", help="启用并行审查（安全线 + 可维护性线）")
    run.add_argument("--workers", type=int, default=3, help="并行节点线程数")
    run.add_argument("--ignore-blocking", action="store_true",
                     help="把 stop_on_blocking 设为 False（存在阻断问题时流程也不降级为 partial）；退出码仍按阻断问题返回 3")
    run.add_argument("--no-write-workspace", action="store_true", help="只做分析，不往工作区写文件")
    run.add_argument("--out", default="", help="Markdown 报告输出路径")
    run.add_argument("--json-out", default="", help="JSON 结果输出路径")
    run.add_argument("--checkpoint", default="", help="把运行检查点写到该路径")
    run.add_argument("--resume-from", default="", help="从检查点恢复（传检查点文件路径）")
    run.add_argument("--env-file", default=None, help="显式指定 .env")
    run.add_argument("--verbose", "-v", action="store_true", help="输出详细日志")
    run.set_defaults(func=cmd_run)

    list_cmd = sub.add_parser("list", help="列出 Agent 与 Workflow")
    list_cmd.add_argument("--verbose", "-v", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    graph = sub.add_parser("graph", help="导出流程拓扑")
    graph.add_argument("--workflow", "-w", default="code_review")
    graph.add_argument("--format", choices=["mermaid", "dot", "md"], default="mermaid")
    graph.add_argument("--parallel-review", action="store_true")
    graph.add_argument("--out", default="")
    graph.set_defaults(func=cmd_graph)

    report = sub.add_parser("report", help="把 JSON 结果渲染成 Markdown")
    report.add_argument("--json", required=True)
    report.add_argument("--out", default="")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv=None) -> int:
    """CLI 主入口。"""
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    setup_logging(getattr(args, "verbose", False))
    try:
        return int(args.func(args) or EXIT_OK)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover
        print("\n[已中断]", file=sys.stderr)
        return EXIT_FAILED
    except WorkflowError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_FAILED


__all__ = ["main", "build_parser", "setup_console", "EventPrinter"]
