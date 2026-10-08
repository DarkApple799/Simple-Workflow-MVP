"""编排执行器（CONTRACT 第 8 节）。

``WorkflowEngine`` 按流程拓扑逐"回合（turn）"驱动节点执行：

    before_turn → when 判定 → loop_of 计数 → 执行（Agent 或 execute）→
    结果写黑板 → 出边选择（decision）→ 记录 + 消息 + 事件

支持的流程能力：顺序、条件分支（``when`` / ``decision``）、循环（``loop_of`` + ``max_iterations``）、
并行（``ThreadPoolExecutor``）、失败重试与降级（``retry`` / ``on_error``）、检查点与恢复
（``checkpoint`` / ``resume``）、事件回调（Web UI 的实时日志）。

依赖方向：只依赖 config/errors/util/bus/state/graph，**不** import ``agents`` / ``cli``：
Agent 通过注入的注册表 ``agents.get(node.agent)`` 获取。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bus import BROADCAST, PERF_ID, MessageBus
from .errors import LLMError, NodeError, WorkflowAborted, WorkflowError
from .graph import (AgentResult, Edge, Node, NodeContext, NodeOutcome, Workflow,
                    jsonable)
from .state import Blackboard, NodeRecord, RunState
from .util import RetryPolicy, new_id, now_iso, read_text, write_text

logger = logging.getLogger("wfa.engine")

#: 事件名（``on_event(name, payload)``）—— 只允许这 8 种，Web UI / 测试依赖它。
EVENTS: Tuple[str, ...] = (
    "turn_start", "turn_end", "node_start", "node_end", "node_failed",
    "stop", "parallel_start", "parallel_end",
)


# --------------------------------------------------------------------------- #
# 配置与报告
# --------------------------------------------------------------------------- #
@dataclass
class RunConfig:
    """一次运行的编排参数（与 ``wfa.config.Config`` 的编排字段对齐）。"""

    task: str = ""
    workspace: str = ""
    mode: str = "offline"            # offline | online
    model: str = ""
    max_turns: int = 50
    max_iterations: int = 3
    review_max_steps: int = 4
    stop_on_blocking: bool = True
    verbose: bool = False
    parallel_workers: int = 3
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化（绝不含密钥；``extra`` 中的运行期对象只保留可序列化部分）。"""
        data = {
            "task": self.task,
            "workspace": self.workspace,
            "mode": self.mode,
            "model": self.model,
            "max_turns": self.max_turns,
            "max_iterations": self.max_iterations,
            "review_max_steps": self.review_max_steps,
            "stop_on_blocking": self.stop_on_blocking,
            "verbose": self.verbose,
            "parallel_workers": self.parallel_workers,
        }
        extra: Dict[str, Any] = {}
        for key, value in dict(self.extra or {}).items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("key", "token", "secret", "password", "auth")):
                continue
            if lowered in ("model", "registry", "bus", "state", "engine", "agents"):
                continue
            extra[str(key)] = jsonable(value)
        data["extra"] = extra
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RunConfig":
        """从 ``to_dict()`` 的结构还原（忽略未知字段）。"""
        known = {item.name for item in fields(cls)}
        kwargs = {key: value for key, value in dict(data or {}).items() if key in known}
        if not isinstance(kwargs.get("extra"), dict):
            kwargs["extra"] = {}
        return cls(**kwargs)


@dataclass
class RunReport:
    """一次运行的终态报告（人读 + JSON 双输出）。"""

    run_id: str
    workflow: str
    status: str = "pending"          # success | partial | failed | aborted
    turns: int = 0
    duration: float = 0.0
    node_runs: List[NodeRecord] = field(default_factory=list)
    issues: List[dict] = field(default_factory=list)          # 最终 issue 列表
    counts: dict = field(default_factory=dict)
    artifacts: Dict[str, dict] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    messages: int = 0
    error: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "status": self.status,
            "turns": self.turns,
            "duration": round(self.duration, 4),
            "node_runs": [_as_dict(record) for record in self.node_runs],
            "issues": jsonable(self.issues),
            "counts": jsonable(self.counts),
            "artifacts": jsonable(self.artifacts),
            "metrics": jsonable(self.metrics),
            "messages": self.messages,
            "error": self.error,
            "notes": list(self.notes or []),
        }

    def to_markdown(self) -> str:
        """人读的运行报告（``docs/run_report.md`` 使用）。"""
        lines: List[str] = []
        lines.append("# 运行报告：%s" % self.workflow)
        lines.append("")
        lines.append("- 运行 ID：`%s`" % self.run_id)
        lines.append("- 终态：**%s**" % self.status)
        lines.append("- 回合数：%s" % self.turns)
        lines.append("- 耗时：%.3fs" % self.duration)
        lines.append("- 消息数：%s" % self.messages)
        lines.append("- 停止原因：`%s`" % self.metrics.get("stop_reason", ""))
        if self.metrics.get("iterations"):
            lines.append("- 循环迭代：`%s`" % json.dumps(self.metrics["iterations"], ensure_ascii=False))
        lines.append("")

        lines.append("## 节点执行")
        lines.append("")
        if self.node_runs:
            lines.append("| 回合 | 节点 | Agent | 状态 | 决策 | 尝试 | 耗时(s) | 摘要 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for record in self.node_runs:
                lines.append("| %s | %s | %s | %s | %s | %s | %.3f | %s |" % (
                    getattr(record, "turn", 0), getattr(record, "node_id", ""),
                    getattr(record, "agent", ""), getattr(record, "status", ""),
                    getattr(record, "decision", "") or "-", getattr(record, "attempt", 1),
                    float(getattr(record, "duration", 0.0) or 0.0),
                    _md_cell(getattr(record, "summary", "") or getattr(record, "error", ""))))
        else:
            lines.append("_没有执行任何节点。_")
        lines.append("")

        lines.append("## 问题（issues：%s）" % len(self.issues))
        lines.append("")
        if self.counts:
            lines.append("严重度统计：" + "，".join("%s=%s" % (k, v) for k, v in sorted(self.counts.items())))
            lines.append("")
        if self.issues:
            lines.append("| 严重度 | 标题 | 文件:行 | 说明 |")
            lines.append("| --- | --- | --- | --- |")
            for issue in self.issues[:50]:
                location = "%s:%s" % (issue.get("file", ""), issue.get("line", ""))
                lines.append("| %s | %s | %s | %s |" % (
                    issue.get("severity", ""), _md_cell(issue.get("title", "")),
                    _md_cell(location), _md_cell(issue.get("detail", ""))))
            if len(self.issues) > 50:
                lines.append("")
                lines.append("_（仅展示前 50 条）_")
        else:
            lines.append("_未发现问题。_")
        lines.append("")

        lines.append("## 产物（artifacts：%s）" % len(self.artifacts))
        lines.append("")
        if self.artifacts:
            for name, item in self.artifacts.items():
                lines.append("- `%s`（%s，生产者 %s）" % (
                    name, item.get("kind", "doc"), item.get("producer", "")))
        else:
            lines.append("_无产物。_")
        lines.append("")

        lines.append("## 指标")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(jsonable(self.metrics), ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

        if self.notes:
            lines.append("## 备注")
            lines.append("")
            for note in self.notes:
                lines.append("- %s" % note)
            lines.append("")
        if self.error:
            lines.append("## 错误")
            lines.append("")
            lines.append("```")
            lines.append(self.error)
            lines.append("```")
            lines.append("")
        return "\n".join(lines)


class StopReason:
    """停止原因常量（写入 ``RunReport.metrics["stop_reason"]``）。"""

    COMPLETED = "completed"          # 正常跑完（无出边 / 到达 end）
    MAX_TURNS = "max_turns"          # 达到 config.max_turns
    MAX_ITERATIONS = "max_iterations"  # 循环达到 max_iterations
    ABORTED = "aborted"              # abort() / stop_check() / WorkflowAborted
    ERROR = "error"                  # 节点失败且无法降级 / 未预期异常
    STOP = "stop"                    # 流程自身决策停止（如 stop_on_blocking）

    ALL: Tuple[str, ...] = (COMPLETED, MAX_TURNS, MAX_ITERATIONS, ABORTED, ERROR, STOP)

    LABELS: Dict[str, str] = {
        COMPLETED: "正常完成",
        MAX_TURNS: "达到最大回合数",
        MAX_ITERATIONS: "达到最大迭代次数",
        ABORTED: "被中断",
        ERROR: "执行错误",
        STOP: "按策略停止",
    }


# --------------------------------------------------------------------------- #
# 内部控制流
# --------------------------------------------------------------------------- #
class _StopRun(Exception):
    """内部信号：终止主循环并携带终态。"""

    def __init__(self, status: str, reason: str, error: str = "", note: str = "") -> None:
        super().__init__(error or note or reason)
        self.status = status
        self.reason = reason
        self.error = error
        self.note = note


class _RetryableNodeFailure(LLMError):
    """把任意节点异常包装为"可重试错误"，以复用 ``util.RetryPolicy`` 的指数退避。

    ``RetryPolicy`` 按契约只重试 ``LLMError.retryable=True``；而 ``Node.retry`` 是**节点级**
    策略，需要覆盖任意异常（程序 bug、断言失败、文件错误……）。因此引擎在这里做一次内部包装，
    重试耗尽后再用 ``original`` 还原真实原因。本类型只在引擎内部流转，不会写进报告。
    """

    retryable = True

    def __init__(self, message: str, original: Optional[BaseException] = None) -> None:
        super().__init__(message, status=getattr(original, "status", None))
        self.original = original if original is not None else self


class _DecisionFailure(WorkflowError):
    """节点决策为 ``fail`` 且没有 ``when="fail"`` 出边时，按失败处理。"""


class _NodeOutcomeFailure(WorkflowError):
    """``NodeOutcome(status="failed")`` 归一化成的异常。"""

    def __init__(self, message: str, outcome: Optional[NodeOutcome] = None) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass
class _ExecOutcome:
    """一次成功执行的归一化结果。"""

    result: AgentResult
    next_node: Optional[str] = None
    extra_payload: Dict[str, Any] = field(default_factory=dict)


def _as_dict(obj: Any) -> dict:
    """把 ``NodeRecord`` 等对象安全转成 dict。"""
    if obj is None:
        return {}
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(obj, dict):
        return dict(obj)
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {"value": str(obj)}


def _error_text(exc: BaseException) -> str:
    """异常 → 一行错误文本（不丢类型信息）。"""
    name = type(exc).__name__
    text = str(exc).strip()
    return text if text.startswith(name) else "%s: %s" % (name, text) if text else name


def _md_cell(text: Any) -> str:
    """Markdown 表格单元转义。"""
    return str(text).replace("|", "\\|").replace("\n", " ").strip() or "-"


def _append_unique(items: List[str], value: str) -> None:
    if value not in items:
        items.append(value)


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class WorkflowEngine:
    """按流程定义驱动节点执行；``run()`` 返回 ``RunReport``。"""

    def __init__(self, workflow: Workflow, agents: Any, *,
                 config: Optional[RunConfig] = None, bus: Optional[MessageBus] = None,
                 state: Optional[Blackboard] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 stop_check: Optional[Callable[[], bool]] = None) -> None:
        if workflow is None:
            raise NodeError("WorkflowEngine 需要 workflow")
        self._workflow = workflow
        self._agents = agents
        self._config = config or RunConfig()
        self._on_event = on_event
        self._stop_check = stop_check
        self._aborted = False
        self._sleep = time.sleep
        self._notes: List[str] = []
        self._notes_lock = threading.Lock()
        self._events: List[dict] = []
        self._results: Dict[str, AgentResult] = {}
        self._last_node: Optional[str] = None
        entry = workflow.entry_node()
        self._cursor: Optional[str] = entry.id if entry is not None else None
        self._stopped: Optional[str] = None

        # 注意：MessageBus 定义了 __len__，"bus or ..." 在**空总线**时会误判为未提供，
        # 因此必须用显式 None 判定（否则调用方拿到的总线永远收不到 system 消息）。
        if bus is not None:
            self._bus = bus
        else:
            self._bus = (getattr(state, "bus", None) if state is not None else None) or MessageBus()
        if state is None:
            run_state = RunState(run_id=new_id("run"), workflow=workflow.name, status="pending")
            state = Blackboard(workspace=self._config.workspace, bus=self._bus, state=run_state)
        self._state = state
        if not getattr(self._state.state, "workflow", ""):
            self._state.state.workflow = workflow.name

    # ---- 只读属性 ----
    @property
    def workflow(self) -> Workflow:
        return self._workflow

    @property
    def config(self) -> RunConfig:
        return self._config

    @property
    def agents(self) -> Any:
        return self._agents

    @property
    def bus(self) -> MessageBus:
        return self._bus

    @property
    def state(self) -> Blackboard:
        return self._state

    @property
    def events(self) -> List[dict]:
        """本次运行产生的事件流（``[{"seq","event","at","data"}]``）。"""
        return list(self._events)

    @property
    def cursor(self) -> Optional[str]:
        """下一个待执行节点（checkpoint 用）。"""
        return self._cursor

    @property
    def last_node(self) -> Optional[str]:
        """最近一个已执行节点 id。"""
        return self._last_node

    @property
    def stop_reason(self) -> Optional[str]:
        """最近一次运行的停止原因。"""
        return self._stopped

    def abort(self) -> None:
        """请求中断（下一个回合开始处生效），终态为 ``aborted``。"""
        self._aborted = True
        logger.info("收到中断请求：%s", self._workflow.name)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self, *, resume_from: Optional[str] = None) -> RunReport:
        """执行流程；``resume_from`` 指定从哪个节点开始（检查点恢复用）。"""
        started_wall = time.time()
        self._aborted = False
        self._notes = []
        self._events = []
        self._stopped = None
        state = self._state.state
        if not state.started_at:
            state.started_at = now_iso()
        state.status = "running"
        state.workflow = self._workflow.name
        if resume_from:
            self._note("从节点 %s 继续执行" % resume_from)
        self._publish_system("开始执行流程 %s" % self._workflow.name, turn=state.turn,
                             extra={"event": "run_start", "task": self._config.task})
        try:
            current = self._resolve_start(resume_from)
            if current is None:
                status, reason, error = "success", StopReason.COMPLETED, ""
                self._note("流程没有可执行节点")
            else:
                status, reason, error = self._drive(current)
        except WorkflowAborted as exc:
            status, reason, error = "aborted", StopReason.ABORTED, _error_text(exc)
            self._note("流程被中断：%s" % error)
        except Exception as exc:  # noqa: BLE001 - 引擎兜底：任何异常都要产出报告
            status, reason, error = "failed", StopReason.ERROR, _error_text(exc)
            logger.exception("流程 %s 执行失败", self._workflow.name)
        report = RunReport(run_id=state.run_id, workflow=self._workflow.name)
        self._finalize(report, status, reason, error, started_wall)
        return report

    def _resolve_start(self, resume_from: Optional[str]) -> Optional[Node]:
        """确定起始节点。"""
        if resume_from:
            node = self._workflow.node(resume_from)
            if node is None:
                raise NodeError("resume_from 指定的节点不存在：%s" % resume_from)
            return node
        node = self._workflow.entry_node()
        if node is None:
            return None
        return node

    def _drive(self, current: Node) -> Tuple[str, str, str]:
        """主循环：逐回合执行节点。返回 ``(status, stop_reason, error)``。"""
        try:
            while current is not None:
                node = current
                # ---------- 语义 1：before_turn ----------
                turn = self._state.state.turn + 1
                if self._config.max_turns and turn > int(self._config.max_turns):
                    raise _StopRun("partial", StopReason.MAX_TURNS,
                                   note="达到最大回合数 max_turns=%s" % self._config.max_turns)
                if self._stop_requested():
                    raise _StopRun("aborted", StopReason.ABORTED,
                                   note="收到停止请求（abort/stop_check）")
                self._state.state.turn = turn
                self._emit("turn_start", {"run_id": self._state.state.run_id,
                                          "workflow": self._workflow.name,
                                          "turn": turn, "node": node.id, "type": node.type})
                started_at = now_iso()
                started = time.time()
                ctx = self._build_context(node, turn)

                # ---------- 语义 2：when=False → skipped ----------
                if node.when is not None:
                    try:
                        allowed = bool(node.when(ctx))
                    except Exception as exc:  # noqa: BLE001 - 条件异常按节点失败处理
                        logger.warning("节点 %s 的 when 求值失败：%s", node.id, exc)
                        current = self._fail_turn(node, ctx, started, started_at, exc, attempt=1)
                        continue
                    if not allowed:
                        current = self._skip_turn(node, ctx, started, started_at,
                                                  reason="when 条件为 False")
                        continue

                # ---------- 语义 7：loop_of 迭代计数与上限 ----------
                if node.loop_of:
                    iteration = self._state.bump_iteration(node.loop_of)
                    limit = int(node.max_iterations or self._config.max_iterations or 0)
                    if limit and iteration > limit:
                        raise _StopRun("partial", StopReason.MAX_ITERATIONS,
                                       note="循环 %s 达到最大迭代 %s" % (node.loop_of, limit))
                    ctx.iteration = iteration

                # ---------- 语义 3/4/9：执行（含重试） ----------
                self._emit("node_start", {"node": node.id, "agent": node.agent, "type": node.type,
                                          "turn": turn, "attempt": 1})
                raw, failure, attempts = self._execute_with_retry(node, ctx)
                if failure is not None:
                    current = self._fail_turn(node, ctx, started, started_at, failure,
                                              attempt=attempts)
                    continue
                if isinstance(raw, NodeOutcome) and raw.status == "skipped":
                    current = self._skip_turn(node, ctx, started, started_at,
                                              reason=raw.error or "节点自报 skipped")
                    continue
                outcome = self._normalize(node, raw)

                # ---------- 语义 6：AgentResult → 黑板 ----------
                self._store_result(node, outcome)

                decision = (outcome.result.decision or "").strip().lower()
                # ---------- 语义 8：decision=fail 且无 when=fail 出边 → on_error 语义 ----------
                if decision == "fail" and self._select_fail_edge(node.id) is None:
                    logger.info("节点 %s 决策为 fail 且无 when=fail 出边，按 on_error 处理", node.id)
                    current = self._fail_turn(node, ctx, started, started_at,
                                              _DecisionFailure("节点 %s 决策为 fail" % node.id),
                                              attempt=attempts)
                    continue
                next_id = self._next_node_id(node, outcome)
                # ---------- stop_on_blocking：阻断级问题 + 流程在非 end 节点断流 → 提前停止 ----------
                blocking_stop = (next_id is None and node.type != "end"
                                 and self._config.stop_on_blocking
                                 and bool(self._state.get("blocking")))

                # ---------- 语义 10：记录 + 消息 + 事件 ----------
                self._complete_turn(node, ctx, status="ok", started=started, started_at=started_at,
                                    result=outcome.result, decision=outcome.result.decision,
                                    summary=outcome.result.summary, next_id=next_id,
                                    attempt=attempts)
                if blocking_stop:
                    raise _StopRun("partial", StopReason.STOP,
                                   note="存在阻断级问题且流程已无后续节点，按 stop_on_blocking 停止")
                current = self._workflow.node(next_id) if next_id else None
        except _StopRun as stop:
            if stop.note:
                self._note(stop.note)
            return stop.status, stop.reason, stop.error
        return self._terminal_status()

    def _terminal_status(self) -> Tuple[str, str, str]:
        """正常走到终点时的终态判定。

        ``stop_on_blocking`` 的口径：**阻断级问题 + 已无后续节点可补救**时提前停止（``partial``）；
        流程本身跑完（走到 end / 无出边）则终态为 ``success``——阻断级问题通过
        ``RunReport.issues`` / ``counts`` / ``metrics["blocking"]`` 表达，交给 CLI 映射退出码 3，
        避免"流程完整跑完却报 partial"的歧义。
        """
        blocking = self._state.get("blocking") or []
        if blocking:
            self._note("流程结束，仍有 %s 个阻断级问题（stop_on_blocking=%s）"
                       % (len(blocking), self._config.stop_on_blocking))
        return "success", StopReason.COMPLETED, ""

    def _stop_requested(self) -> bool:
        """``abort()`` 或用户 ``stop_check`` 是否要求停止。"""
        if self._aborted:
            return True
        if self._stop_check is None:
            return False
        try:
            return bool(self._stop_check())
        except Exception:  # noqa: BLE001 - stop_check 异常视为请求停止，避免死循环
            logger.exception("stop_check 回调异常，按停止处理")
            return True

    # ------------------------------------------------------------------ #
    # 单个回合的各个阶段
    # ------------------------------------------------------------------ #
    def _build_context(self, node: Node, turn: int) -> NodeContext:
        """构造节点上下文（inputs = 直接前驱最近结果）。"""
        inputs: Dict[str, AgentResult] = {}
        for pred_id in self._predecessors(node.id):
            result = self._results.get(pred_id)
            if result is not None:
                inputs[pred_id] = result
        iteration = self._state.iteration(node.loop_of) if node.loop_of else 0
        return NodeContext(node=node, state=self._state, bus=self._bus, inputs=inputs,
                           iteration=iteration, turn=turn, workspace=self._config.workspace,
                           config=self._config, extras={})

    def _predecessors(self, node_id: str) -> List[str]:
        """直接前驱节点 id（按边/next 声明顺序，去重）。"""
        order: List[str] = []
        for edge in self._workflow.edges():
            if edge.dst == node_id and edge.src != node_id:
                _append_unique(order, edge.src)
        for other in self._workflow.nodes():
            if other.id == node_id:
                continue
            if node_id in (other.next or []):
                _append_unique(order, other.id)
        return [item for item in order if self._workflow.node(item) is not None]

    def _invoke_once(self, node: Node, ctx: NodeContext) -> Any:
        """执行一次节点：parallel > execute > Agent。"""
        if node.type == "parallel" and node.children:
            return self._run_parallel(node, ctx)
        if node.execute is not None:
            return node.execute(node, ctx)
        if node.type in ("start", "end") and not node.agent:
            return NodeOutcome(status="ok", payload={})   # 起止节点默认是空操作
        if not node.agent:
            raise NodeError("节点 %s 既没有 execute 也没有 agent" % node.id)
        try:
            agent = self._agents.get(node.agent)
        except Exception as exc:  # noqa: BLE001 - 注册表缺失按失败处理（on_error 语义）
            raise NodeError("节点 %s 找不到 Agent：%s（%s）" % (node.id, node.agent, exc)) from exc
        if agent is None:
            raise NodeError("节点 %s 找不到 Agent：%s" % (node.id, node.agent))
        run = getattr(agent, "run", None)
        if not callable(run):
            raise NodeError("节点 %s 的 Agent %s 没有 run(ctx) 方法" % (node.id, node.agent))
        return run(ctx)

    def _execute_with_retry(self, node: Node, ctx: NodeContext) -> Tuple[Any, Optional[BaseException], int]:
        """按 ``node.retry`` 重试执行；返回 ``(raw, failure, attempts)``。"""
        retries = max(0, int(node.retry or 0))
        delay = float(node.retry_delay if node.retry_delay is not None else 0.2)
        policy = RetryPolicy(max_retries=retries, base_delay=max(0.0, delay),
                             max_delay=max(delay * 8.0, 1.0), sleep=self._sleep)
        counter = {"attempts": 0}

        def attempt() -> Any:
            counter["attempts"] += 1
            try:
                raw = self._invoke_once(node, ctx)
            except WorkflowAborted:
                raise
            except Exception as exc:  # noqa: BLE001 - 节点失败：可重试则包装后交给 RetryPolicy
                if retries <= 0 or getattr(exc, "retryable", False):
                    raise
                raise _RetryableNodeFailure(_error_text(exc), exc) from exc
            if isinstance(raw, NodeOutcome) and raw.status == "failed":
                message = raw.error or "节点 %s 返回 status=failed" % node.id
                if retries <= 0:
                    raise _NodeOutcomeFailure(message, raw)
                raise _RetryableNodeFailure(message, _NodeOutcomeFailure(message, raw))
            return raw

        def on_retry(index: int, exc: BaseException) -> None:
            message = "节点 %s 第 %s 次重试（退避 %.2fs）：%s" % (node.id, index, delay, exc)
            logger.warning("%s", message)
            self._note(message)

        try:
            raw = policy.run(attempt, on_retry=on_retry)
            return raw, None, counter["attempts"]
        except Exception as exc:  # noqa: BLE001 - 重试耗尽：取出原始失败原因
            original = getattr(exc, "original", None)
            return None, (original if original is not None else exc), counter["attempts"]

    def _normalize(self, node: Node, raw: Any) -> _ExecOutcome:
        """把 ``execute`` / Agent 的返回值归一化为 ``_ExecOutcome``。"""
        if raw is None:
            return _ExecOutcome(AgentResult(summary="%s 完成（无返回）" % node.id))
        if isinstance(raw, NodeOutcome):
            payload = dict(raw.payload or {})
            decision = str(payload.get("decision", "") or "")
            result = AgentResult(kind=str(payload.get("kind") or "result"),
                                 summary=raw.error or payload.get("summary") or "%s 完成" % node.id,
                                 decision=decision, data=payload)
            extra = {key: value for key, value in payload.items()
                     if key not in ("decision", "kind", "summary")}
            return _ExecOutcome(result=result, next_node=raw.next_node, extra_payload=extra)
        if isinstance(raw, dict):
            raw = AgentResult.from_dict(raw)
        if not isinstance(raw, AgentResult):
            raise NodeError("节点 %s 的返回值类型不支持：%s" % (node.id, type(raw).__name__))
        # 并行节点可能把 execute 返回的 NodeOutcome.next_node 暂存在 result 上
        return _ExecOutcome(result=raw, next_node=getattr(raw, "next_node", None))

    def _store_result(self, node: Node, outcome: _ExecOutcome) -> None:
        """语义 6：把 AgentResult 写入黑板（含 review / fix 的约定键名与产物）。"""
        result = outcome.result
        data = result.to_dict()
        self._state.set(node.id, data)
        self._state.ns_merge("results", {node.id: data})
        if outcome.extra_payload:
            self._state.update(outcome.extra_payload)

        payload = result.data or {}
        if result.kind == "review":
            if "issues" in payload:
                self._state.set("issues", payload.get("issues") or [])
            counts = payload.get("counts", payload.get("issue_counts"))
            if counts:
                self._state.set("issue_counts", counts)
            if payload.get("verdict"):
                self._state.set("verdict", payload["verdict"])
            if "blocking" in payload:
                self._state.set("blocking", payload.get("blocking") or [])
        elif result.kind == "fix":
            self._state.set("fixes", payload.get("patches", []))

        for name, content in dict(result.artifacts or {}).items():
            try:
                self._state.put_artifact(str(name), content, kind="doc", producer=node.id)
            except Exception:  # noqa: BLE001 - 产物登记失败不应中断流程
                logger.warning("节点 %s 的产物 %s 登记失败", node.id, name, exc_info=True)
        if result.metrics:
            self._state.ns_merge("metrics", {node.id: jsonable(result.metrics)})
        self._results[node.id] = result

    def _out_edges(self, node_id: str) -> List[Edge]:
        """节点的出边（声明顺序）；``node.next`` 无对应 Edge 时合成伪边。"""
        edges = [edge for edge in self._workflow.edges() if edge.src == node_id]
        if edges:
            return edges
        node = self._workflow.node(node_id)
        if node is None:
            return []
        return [Edge(src=node_id, dst=dst) for dst in (node.next or [])]

    def _select_fail_edge(self, node_id: str) -> Optional[Edge]:
        """第一个 ``when="fail"`` 的出边。"""
        for edge in self._out_edges(node_id):
            if (edge.when or "").strip().lower() == "fail":
                return edge
        return None

    def _default_next(self, node_id: str) -> Optional[str]:
        """默认出边：第一个 ``when`` 为空的边，否则第一条边。"""
        edges = self._out_edges(node_id)
        if not edges:
            return None
        for edge in edges:
            if not (edge.when or "").strip():
                return edge.dst
        return edges[0].dst

    def _next_node_id(self, node: Node, outcome: _ExecOutcome) -> Optional[str]:
        """语义 8：由 ``decision`` 选择下一个节点。"""
        if outcome.next_node:
            if self._workflow.node(outcome.next_node) is None:
                raise _StopRun("failed", StopReason.ERROR,
                               error="节点 %s 指定的 next_node 不存在：%s" % (node.id, outcome.next_node),
                               note="节点 %s 的 next_node 非法" % node.id)
            return outcome.next_node
        edges = self._out_edges(node.id)
        if not edges:
            return None
        decision = (outcome.result.decision or "").strip().lower()
        if decision in ("pass", "done"):
            for edge in edges:
                target = self._workflow.node(edge.dst)
                if target is not None and target.type == "end":
                    return edge.dst
            return edges[-1].dst
        if decision in ("retry", "continue"):
            for edge in edges:
                if ((edge.when or "").strip().lower() == "retry"
                        or (edge.label or "").strip().lower() == "retry"):
                    return edge.dst
            return edges[0].dst
        if decision == "fail":
            edge = self._select_fail_edge(node.id)
            return edge.dst if edge is not None else None
        if decision:
            logger.debug("节点 %s 的决策 %r 未定义，按默认出边处理", node.id, decision)
        return self._default_next(node.id)

    # ------------------------------------------------------------------ #
    # 回合收尾
    # ------------------------------------------------------------------ #
    def _complete_turn(self, node: Node, ctx: NodeContext, *, status: str, started: float,
                       started_at: str, result: Optional[AgentResult] = None,
                       decision: str = "", summary: str = "", error: str = "",
                       next_id: Optional[str] = None, attempt: int = 1) -> NodeRecord:
        """语义 10：发事件、写记录、发系统消息。"""
        duration = max(0.0, time.time() - started)
        turn = self._state.state.turn
        if status == "failed":
            self._emit("node_failed", {"node": node.id, "error": error, "turn": turn,
                                       "attempt": attempt, "duration": round(duration, 4)})
        else:
            self._emit("node_end", {"node": node.id, "status": status, "decision": decision,
                                    "summary": summary, "turn": turn, "attempt": attempt,
                                    "duration": round(duration, 4)})
        record = NodeRecord(node_id=node.id, agent=self._agent_label(node), turn=turn,
                            status=status, started_at=started_at, duration=duration,
                            summary=summary or "", kind=(result.kind if result is not None else ""),
                            decision=decision or "", result=(result.to_dict() if result is not None else {}),
                            messages=[], error=error or "", attempt=attempt)
        message = self._publish_system(
            self._system_text(node, status, summary, error), turn=turn, sender=node.id,
            extra={"event": "node", "node": node.id, "status": status,
                   "decision": decision or "", "next": next_id or ""})
        if message is not None and getattr(message, "id", ""):
            record.messages.append(message.id)
        self._state.record(record)
        self._last_node = node.id
        self._cursor = next_id
        self._emit("turn_end", {"turn": turn, "node": node.id, "status": status,
                                "decision": decision or "", "next": next_id,
                                "duration": round(duration, 4)})
        return record

    def _skip_turn(self, node: Node, ctx: NodeContext, started: float, started_at: str,
                   reason: str = "") -> Optional[Node]:
        """语义 2：``when=False`` → 记 skipped、不执行、跳到下一个后继。"""
        summary = reason or "跳过"
        next_id = self._default_next(node.id)
        self._complete_turn(node, ctx, status="skipped", started=started, started_at=started_at,
                            summary=summary, next_id=next_id)
        self._note("节点 %s 被跳过：%s" % (node.id, summary))
        return self._workflow.node(next_id) if next_id else None

    def _fail_turn(self, node: Node, ctx: NodeContext, started: float, started_at: str,
                   failure: BaseException, attempt: int = 1) -> Optional[Node]:
        """语义 5：失败处理 —— 显式 fallback > ``when="fail"`` 出边 > ``on_error`` 策略。

        * ``fallback`` / ``on_error="fallback:<node_id>"``：跳转到降级节点；
        * 声明了 ``when="fail"`` 的出边：走该失败分支（等价于流程作者显式声明的降级路径）；
        * ``on_error="continue"``：记 failed，沿默认出边继续；
        * 其余（``on_error="fail"`` 且无降级路径）：终止，终态 ``failed`` / ``error``。
        """
        message = _error_text(failure)
        self._state.ns_merge("errors", {node.id: message})
        self._results[node.id] = AgentResult(kind="error", summary=message, decision="fail")

        mode = (node.on_error or "fail").strip()
        fallback = (node.fallback or "").strip()
        if mode.startswith("fallback:"):
            fallback = fallback or mode.split(":", 1)[1].strip()
            mode = "fallback"
        elif mode not in ("fail", "continue"):
            logger.warning("节点 %s 的 on_error=%r 非法，按 fail 处理", node.id, node.on_error)
            mode = "fail"

        if fallback:
            target = self._workflow.node(fallback)
            if target is None:
                self._complete_turn(node, ctx, status="failed", started=started,
                                    started_at=started_at, error=message, decision="fail",
                                    attempt=attempt, next_id=None)
                raise _StopRun("failed", StopReason.ERROR, error=message,
                               note="节点 %s 的 fallback 节点不存在：%s" % (node.id, fallback))
            self._complete_turn(node, ctx, status="failed", started=started, started_at=started_at,
                                error=message, decision="fail", attempt=attempt, next_id=fallback)
            self._note("节点 %s 失败，转入 fallback 节点 %s" % (node.id, fallback))
            return target

        if mode == "fail":
            fail_edge = self._select_fail_edge(node.id)
            if fail_edge is not None:
                self._complete_turn(node, ctx, status="failed", started=started,
                                    started_at=started_at, error=message, decision="fail",
                                    attempt=attempt, next_id=fail_edge.dst)
                self._note("节点 %s 失败，沿 when=fail 出边转入 %s" % (node.id, fail_edge.dst))
                return self._workflow.node(fail_edge.dst)

        if mode == "continue":
            next_id = self._default_next(node.id)
            self._complete_turn(node, ctx, status="failed", started=started, started_at=started_at,
                                error=message, decision="fail", attempt=attempt, next_id=next_id)
            self._note("节点 %s 失败，按 on_error=continue 继续" % node.id)
            return self._workflow.node(next_id) if next_id else None

        self._complete_turn(node, ctx, status="failed", started=started, started_at=started_at,
                            error=message, decision="fail", attempt=attempt, next_id=None)
        raise _StopRun("failed", StopReason.ERROR, error=message,
                       note="节点 %s 失败且无 fallback，流程终止" % node.id)

    def _agent_label(self, node: Node) -> str:
        """记录用的 Agent 标签。"""
        if node.agent:
            return node.agent
        if node.execute is not None:
            return "<execute>"
        return node.type

    def _system_text(self, node: Node, status: str, summary: str, error: str) -> str:
        """系统消息文本。"""
        detail = summary or error or ""
        return "节点 %s [%s] %s" % (node.id, status, detail)

    def _publish_system(self, text: str, *, turn: int = 0, sender: str = PERF_ID,
                        extra: Optional[dict] = None) -> Any:
        """向总线广播一条 ``system`` 消息（失败不影响流程）。"""
        try:
            payload: Dict[str, Any] = {"text": text, "run_id": self._state.state.run_id}
            if extra:
                payload.update(extra)
            return self._bus.send("system", sender, BROADCAST, payload, turn=turn)
        except Exception:  # noqa: BLE001 - 总线异常不影响编排
            logger.debug("系统消息发送失败：%s", text, exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # 语义 9：并行节点
    # ------------------------------------------------------------------ #
    def _run_parallel(self, node: Node, ctx: NodeContext) -> AgentResult:
        """用线程池真并发执行 ``node.children``，合并到 ``data["children"]``。"""
        children = list(node.children or [])
        workers = max(1, int(self._config.parallel_workers or 1))
        self._emit("parallel_start", {"node": node.id, "turn": ctx.turn, "workers": workers,
                                      "children": [child.id for child in children]})
        started = time.time()
        results: Dict[str, AgentResult] = {}
        errors: Dict[str, str] = {}
        timings: Dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(children)))) as pool:
            futures = {}
            for child in children:
                child_ctx = replace(ctx, node=child, inputs=dict(ctx.inputs),
                                    extras=dict(ctx.extras))
                futures[pool.submit(self._run_child, node, child, child_ctx)] = child
            for future in as_completed(futures):
                child = futures[future]
                try:
                    payload = future.result()
                except Exception as exc:  # noqa: BLE001 - 单个子节点失败不影响其它子节点
                    errors[child.id] = _error_text(exc)
                    logger.warning("并行子节点 %s/%s 失败：%s", node.id, child.id, errors[child.id])
                    continue
                results[child.id] = payload["result"]
                timings[child.id] = {"start": round(payload["start"], 4),
                                     "end": round(payload["end"], 4),
                                     "duration": round(payload["duration"], 4)}
        duration = max(0.0, time.time() - started)

        ordered = [child for child in children if child.id in results]
        merged_children = {child.id: results[child.id].to_dict() for child in ordered}
        self._state.ns_merge(node.id, merged_children)          # 子结果写入父节点命名空间
        for child in ordered:                                   # 同时登记子节点自身键，便于下游取用
            self._state.set(child.id, merged_children[child.id])
            self._results[child.id] = results[child.id]

        if errors and not results:
            decision = "fail"
        elif errors:
            decision = "continue"
        else:
            decision = "pass"
        merged = AgentResult(
            kind="result",
            summary="并行节点 %s：%s/%s 个子节点完成" % (node.id, len(merged_children), len(children)),
            decision=decision,
            data={"children": merged_children, "errors": errors},
            metrics={"children": len(children), "succeeded": len(merged_children),
                     "failed": len(errors), "duration": round(duration, 4), "workers": workers},
            notes=["并发执行 %s 个子节点，耗时 %.3fs" % (len(children), duration)],
        )

        final = merged
        if node.execute is not None:
            exec_ctx = replace(ctx, extras=dict(ctx.extras, children=merged_children,
                                                parallel=merged.to_dict()))
            raw = node.execute(node, exec_ctx)
            outcome = self._normalize(node, raw if raw is not None else merged)
            final = outcome.result
            data = dict(merged.data)
            data.update(final.data or {})
            data["children"] = merged_children
            data.setdefault("errors", errors)
            final.data = data
            final.next_node = outcome.next_node  # 供 _normalize 读取的跳转覆盖
            metrics = dict(merged.metrics)
            metrics.update(final.metrics or {})
            final.metrics = metrics
        self._emit("parallel_end", {"node": node.id, "turn": ctx.turn, "duration": round(duration, 4),
                                    "succeeded": len(merged_children), "failed": len(errors),
                                    "children": [child.id for child in children], "timings": timings})
        return final

    def _run_child(self, parent: Node, child: Node, ctx: NodeContext) -> dict:
        """在线程中执行一个并行子节点（复用节点的重试语义）。"""
        started = time.time()
        self._emit("node_start", {"node": child.id, "agent": child.agent, "type": child.type,
                                  "turn": ctx.turn, "attempt": 1, "parent": parent.id})
        raw, failure, attempts = self._execute_with_retry(child, ctx)
        if failure is not None:
            self._emit("node_failed", {"node": child.id, "error": _error_text(failure),
                                       "turn": ctx.turn, "attempt": attempts,
                                       "parent": parent.id})
            raise failure
        if isinstance(raw, NodeOutcome) and raw.status == "skipped":
            self._emit("node_end", {"node": child.id, "status": "skipped", "turn": ctx.turn,
                                    "parent": parent.id})
            result = AgentResult(kind="result", summary=raw.error or "跳过")
        else:
            result = self._normalize(child, raw).result
            self._emit("node_end", {"node": child.id, "status": "ok",
                                    "decision": result.decision, "summary": result.summary,
                                    "turn": ctx.turn, "attempt": attempts, "parent": parent.id})
        end = time.time()
        return {"result": result, "start": started, "end": end, "duration": max(0.0, end - started)}

    # ------------------------------------------------------------------ #
    # 终态汇总
    # ------------------------------------------------------------------ #
    def _finalize(self, report: RunReport, status: str, reason: str, error: str,
                  started_wall: float) -> None:
        """语义 11：汇总终态并写回 ``state.state.final``。"""
        state = self._state.state
        duration = max(0.0, time.time() - started_wall)
        state.status = status
        state.finished_at = now_iso()
        state.duration = duration
        state.error = error
        self._stopped = reason

        report.status = status
        report.turns = state.turn
        report.duration = duration
        report.node_runs = list(state.records)
        report.issues = list(self._state.get("issues") or [])
        report.counts = dict(self._state.get("issue_counts") or {})
        report.artifacts = self._state.artifacts()
        report.messages = len(self._bus.messages)
        report.error = error
        report.notes = list(self._notes)

        metrics: Dict[str, Any] = dict(self._state.summary())
        metrics["stop_reason"] = reason
        metrics["stop_label"] = StopReason.LABELS.get(reason, reason)
        metrics["turns"] = state.turn
        metrics["iterations"] = dict(state.iterations or {})
        metrics["nodes"] = len(report.node_runs)
        metrics["duration"] = round(duration, 4)
        metrics["blocking"] = len(self._state.get("blocking") or [])
        metrics["stop_on_blocking"] = bool(self._config.stop_on_blocking)
        model_stats = self._collect_model_stats()
        if model_stats:
            metrics["llm"] = model_stats
        report.metrics = metrics
        state.final = report.to_dict()

        self._emit("stop", {"reason": reason, "status": status, "turn": state.turn,
                            "duration": round(duration, 4), "error": error})
        self._publish_system("流程结束：%s（%s）" % (status, reason), turn=state.turn,
                             extra={"event": "run_stop", "status": status, "reason": reason})

    def _collect_model_stats(self) -> dict:
        """汇总模型统计（``config.extra["model"]`` 与各 Agent 的 ``.model``）。"""
        models: List[Any] = []
        if isinstance(self._config.extra, dict):
            candidate = self._config.extra.get("model")
            if candidate is not None:
                models.append(candidate)
        try:
            names = list(self._agents.names())
        except Exception:  # noqa: BLE001 - 注册表可能没有 names()
            names = []
        for name in names:
            try:
                agent = self._agents.get(name)
            except Exception:  # noqa: BLE001
                continue
            model = getattr(agent, "model", None)
            if model is not None and all(model is not item for item in models):
                models.append(model)
        merged: Dict[str, Any] = {}
        for model in models:
            stats = getattr(model, "stats", None)
            as_dict = getattr(stats, "as_dict", None)
            if not callable(as_dict):
                continue
            try:
                data = as_dict() or {}
            except Exception:  # noqa: BLE001
                continue
            for key, value in data.items():
                if isinstance(value, bool):
                    merged[key] = value
                elif isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                    merged[key] = merged[key] + value
                else:
                    merged[key] = value
        return merged

    def _note(self, text: str) -> None:
        """记录一条运行备注（去重，线程安全）。"""
        if not text:
            return
        with self._notes_lock:
            if text not in self._notes:
                self._notes.append(text)
        logger.info("%s", text)

    def _emit(self, event: str, payload: Optional[dict] = None) -> None:
        """触发 ``on_event`` 回调并缓存事件（回调异常不影响流程）。"""
        data = jsonable(payload or {})
        self._events.append({"seq": len(self._events) + 1, "event": event,
                             "at": now_iso(), "data": data})
        logger.debug("event=%s %s", event, data)
        if self._on_event is None:
            return
        try:
            self._on_event(event, data)
        except Exception:  # noqa: BLE001 - UI 回调不应打断编排
            logger.warning("on_event 回调异常：%s", event, exc_info=True)

    # ------------------------------------------------------------------ #
    # 语义 12：检查点与恢复
    # ------------------------------------------------------------------ #
    def checkpoint(self, path: str) -> str:
        """把 ``{workflow, config, blackboard, bus}`` 写入 JSON，返回绝对路径。"""
        blackboard = self._state.to_dict()
        payload: Dict[str, Any] = {
            "version": 1,
            "created_at": now_iso(),
            "workflow": self._workflow.to_dict(),
            "config": self._config.to_dict(),
            "blackboard": blackboard,
            "bus": self._bus.to_dict(),
            "cursor": {"last_node": self._last_node, "next_node": self._cursor,
                       "turn": self._state.state.turn, "status": self._state.state.status},
        }
        # 顶层同时展开黑板结构，使 Blackboard.load(path) 可直接读取本文件
        for key, value in blackboard.items():
            payload.setdefault(key, value)
        write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        logger.info("检查点已写入：%s", path)
        return os.path.abspath(path)

    @classmethod
    def resume(cls, path: str, agents: Any, **kwargs: Any) -> Tuple["WorkflowEngine", RunReport]:
        """从检查点恢复并继续执行。

        可选的 ``kwargs``：

        * ``workflow``：直接传入工作流对象（推荐；可保留 gate/route 的 ``execute``）；
        * ``executors`` / ``conditions``：``{node_id: callable}``，给反序列化后的节点重新挂载
          ``execute`` / ``when``（这两类函数无法序列化）；
        * ``config`` / ``bus`` / ``state`` / ``on_event`` / ``stop_check`` / ``resume_from``。
        """
        data = json.loads(read_text(path) or "{}")
        workflow = kwargs.get("workflow") or _workflow_from_dict(data.get("workflow") or {})
        config = kwargs.get("config") or RunConfig.from_dict(data.get("config") or {})
        bus = kwargs.get("bus") or _bus_from_dict(data.get("bus") or {})
        state = kwargs.get("state") or _blackboard_from_checkpoint(path, data, config, bus)

        executors = dict(kwargs.get("executors") or {})
        conditions = dict(kwargs.get("conditions") or {})
        missing: List[str] = []
        for node in workflow.nodes():
            if node.id in executors:
                node.execute = executors[node.id]
            elif node.execute is None:
                declared = _declared_flags(data).get(node.id, {})
                if declared.get("has_execute") and not node.agent:
                    missing.append(node.id)
            if node.id in conditions:
                node.when = conditions[node.id]

        engine = cls(workflow, agents, config=config, bus=bus, state=state,
                     on_event=kwargs.get("on_event"), stop_check=kwargs.get("stop_check"))
        cursor = data.get("cursor") or {}
        resume_from = (kwargs.get("resume_from") or cursor.get("next_node")
                       or _last_record_next(data))
        engine._last_node = cursor.get("last_node")
        engine._cursor = resume_from
        # 回合预算是"单次运行"的：检查点里的 state.turn 是上一次运行已用掉的回合数，
        # 若直接沿用，恢复后往往立刻又命中 max_turns→partial。这里重置预算，历史记录保留。
        previous_turns = getattr(engine._state.state, "turn", 0)
        engine._state.state.turn = 0
        report = engine.run(resume_from=resume_from)
        report.notes.insert(0, "已从检查点恢复")
        if previous_turns:
            report.notes.append(
                "已重置本回合预算（检查点中累计回合 %s 次保留在记录里）" % previous_turns
            )
        for node_id in missing:
            note = "节点 %s 的 execute 无法从检查点恢复，请通过 executors= 重新挂载" % node_id
            if note not in report.notes:
                report.notes.append(note)
        return engine, report


# --------------------------------------------------------------------------- #
# 检查点反序列化辅助
# --------------------------------------------------------------------------- #
def _declared_flags(data: dict) -> Dict[str, dict]:
    """检查点里各节点"是否有 execute/when"的声明（用于恢复告警）。"""
    result: Dict[str, dict] = {}
    for node in ((data.get("workflow") or {}).get("nodes") or []):
        if isinstance(node, dict) and node.get("id"):
            result[str(node["id"])] = {"has_execute": bool(node.get("has_execute")),
                                       "has_when": bool(node.get("has_when"))}
    return result


def _node_from_dict(data: dict) -> Node:
    """检查点 → ``Node``（``execute`` / ``when`` 无法序列化，留空待重挂）。"""
    return Node(
        id=str(data.get("id") or ""),
        agent=str(data.get("agent") or ""),
        type=str(data.get("type") or "agent"),
        next=list(data.get("next") or []),
        retry=int(data.get("retry") or 0),
        retry_delay=float(data.get("retry_delay") or 0.2),
        on_error=str(data.get("on_error") or "fail"),
        fallback=data.get("fallback"),
        loop_of=str(data.get("loop_of") or ""),
        max_iterations=int(data.get("max_iterations") or 0),
        children=[_node_from_dict(child) for child in (data.get("children") or [])],
        options=dict(data.get("options") or {}),
        description=str(data.get("description") or ""),
    )


def _workflow_from_dict(data: dict) -> Workflow:
    """检查点 → ``Workflow``。"""
    workflow = Workflow(
        str(data.get("name") or "workflow"),
        description=str(data.get("description") or ""),
        entry=str(data.get("entry") or ""),
        max_turns=int(data.get("max_turns") or 50),
        max_iterations=int(data.get("max_iterations") or 3),
    )
    for node in (data.get("nodes") or []):
        workflow.add_node(_node_from_dict(node))
    for edge in (data.get("edges") or []):
        workflow.add_edge(str(edge.get("src") or ""), str(edge.get("dst") or ""),
                          when=str(edge.get("when") or ""), label=str(edge.get("label") or ""))
    return workflow


def _bus_from_dict(data: dict) -> MessageBus:
    """检查点 → ``MessageBus``（失败则退化为空总线）。"""
    try:
        return MessageBus.from_dict(data or {})
    except Exception:  # noqa: BLE001
        logger.warning("消息总线恢复失败，使用空总线", exc_info=True)
        return MessageBus()


def _blackboard_empty(blackboard: Blackboard) -> bool:
    """判断黑板是否为空（用于决定是否回退到嵌套结构）。"""
    try:
        return not blackboard.keys() and not blackboard.artifacts()
    except Exception:  # noqa: BLE001
        return True


def _blackboard_from_checkpoint(path: str, data: dict, config: RunConfig,
                                bus: MessageBus) -> Blackboard:
    """从检查点恢复黑板：优先直接读同一文件，失败则用嵌套的 ``blackboard`` 结构。"""
    blackboard: Optional[Blackboard] = None
    try:
        blackboard = Blackboard.load(path, workspace=config.workspace, bus=bus)
    except Exception:  # noqa: BLE001
        logger.debug("Blackboard.load(%s) 失败，尝试嵌套结构", path, exc_info=True)
    if blackboard is not None and not _blackboard_empty(blackboard):
        return blackboard

    nested = data.get("blackboard") or {}
    if nested:
        temp_path = ""
        try:
            handle, temp_path = tempfile.mkstemp(prefix="wfa-blackboard-", suffix=".json")
            os.close(handle)
            write_text(temp_path, json.dumps(nested, ensure_ascii=False, default=str))
            restored = Blackboard.load(temp_path, workspace=config.workspace, bus=bus)
            if restored is not None:
                return restored
        except Exception:  # noqa: BLE001
            logger.warning("从嵌套结构恢复黑板失败", exc_info=True)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:  # pragma: no cover
                    pass
    if blackboard is not None:
        return blackboard
    return Blackboard(workspace=config.workspace, bus=bus)


def _last_record_next(data: dict) -> Optional[str]:
    """兜底：从最后一条运行记录里找 ``next_node``（若实现方写入了该约定键）。"""
    blackboard = data.get("blackboard") or data
    records = ((blackboard.get("state") or {}).get("records")) or []
    if not records:
        return None
    last = records[-1] if isinstance(records[-1], dict) else {}
    result = last.get("result") or {}
    for container in (last, result, result.get("data") or {}):
        if isinstance(container, dict) and container.get("next_node"):
            return str(container["next_node"])
    return None


__all__ = [
    "RunConfig",
    "RunReport",
    "StopReason",
    "WorkflowEngine",
    "EVENTS",
]
