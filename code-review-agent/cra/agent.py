"""Agent 主循环。

一次审查的完整链路：
    1. 确定性预扫描（scanner）—— 拿到事实与热点，不依赖 LLM；
    2. 组装记忆与提示词（memory + prompts）；
    3. 循环：模型推理 → 发起工具调用 → 执行工具 → 观察结果回灌 → 再推理；
       直到模型调用 submit_review（终止型工具）或达到步数上限；
    4. 合并"规则命中"与"模型发现"，输出去重后的最终问题列表；
    5. 任何一步失败都降级为规则报告，而不是抛给用户一个堆栈。

两种工具协议：
    * function calling（首选，结构可靠）；
    * JSON 文本协议（服务端/模型不支持 tools 时自动降级，见 prompts.JSON_PROTOCOL_PROMPT）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .errors import CodeReviewAgentError, ConfigError, LLMError
from .jsonutil import extract_json_array, extract_json_object
from .llm import LLMClient
from .memory import Memory
from .models import (
    AgentOutcome,
    Finding,
    ScanResult,
    SEVERITY_LABEL_ZH,
    merge_findings,
)
from .prompts import (
    JSON_PROTOCOL_PROMPT,
    OFFLINE_NOTE,
    SYSTEM_PROMPT,
    build_chat_system_prompt,
    build_scan_digest,
    build_step_nudge,
    build_user_task,
)
from .scanner import scan_workspace
from .tools import ToolContext, ToolRegistry, build_default_registry

logger = logging.getLogger("cra.agent")

#: 进度回调签名：(事件名, 负载) -> None
ProgressHook = Callable[[str, dict], None]


def _noop(event: str, payload: dict) -> None:  # pragma: no cover - 默认空实现
    return None


def _call_signature(name: str, arguments: dict) -> str:
    """工具调用的唯一指纹，用于识别"原地打转"的重复调用。"""
    try:
        payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(arguments)
    return f"{name}:{payload}"


@dataclass
class _LoopState:
    """Agent 循环的可变状态：集中在一个对象里，避免函数参数列表膨胀。"""

    protocol: str = "tools"
    steps: int = 0
    repeats: int = 0
    seen_calls: dict[str, int] = field(default_factory=dict)
    submission: dict | None = None
    final_text: str = ""


class CodeReviewAgent:
    """代码审查 Agent。"""

    def __init__(
        self,
        cfg: Config,
        *,
        client: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        progress: ProgressHook | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.registry = registry or build_default_registry()
        self.progress = progress or _noop

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _ensure_client(self) -> LLMClient:
        if self.client is None:
            self.client = LLMClient(self.cfg)
        return self.client

    def _emit(self, event: str, **payload) -> None:
        try:
            self.progress(event, payload)
        except Exception:  # noqa: BLE001 - 进度回调绝不能影响主流程
            logger.debug("进度回调异常", exc_info=True)

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    def review(
        self,
        path: str,
        *,
        focus: tuple[str, ...] = (),
        extra_instruction: str = "",
        only_file: str | None = None,
        scan: ScanResult | None = None,
    ) -> tuple[ScanResult, AgentOutcome]:
        """审查一个目录或文件，返回 (扫描结果, Agent 结果)。"""
        started = time.time()
        scan_result = scan or scan_workspace(path, self.cfg, only_file=only_file)
        self._emit("scan_done", files=len(scan_result.files), findings=len(scan_result.findings))

        # 离线 / 无 Key：直接给出规则报告
        if self.cfg.offline or not self.cfg.has_api_key():
            reason = "--offline 模式" if self.cfg.offline else "未配置 API Key"
            outcome = self._offline_outcome(scan_result, reason)
            outcome.duration = time.time() - started
            return scan_result, outcome

        context = ToolContext(
            root=scan_result.root,
            cfg=self.cfg,
            scan=scan_result,
            scope_file=scan_result.scope_file,
        )
        try:
            outcome = self._online_review(scan_result, context, focus, extra_instruction)
        except ConfigError as exc:
            outcome = self._offline_outcome(scan_result, str(exc))
        except LLMError as exc:
            logger.warning("LLM 调用失败，降级为规则报告：%s", exc)
            self._emit("degraded", reason=str(exc))
            outcome = self._offline_outcome(scan_result, f"LLM 调用失败：{exc}")
        except CodeReviewAgentError as exc:
            outcome = self._offline_outcome(scan_result, str(exc))
        except KeyboardInterrupt:
            raise

        outcome.duration = time.time() - started
        outcome.trace = list(self.registry.traces)
        return scan_result, outcome

    # ------------------------------------------------------------------ #
    # 在线流程
    # ------------------------------------------------------------------ #
    def _online_review(
        self,
        scan_result: ScanResult,
        context: ToolContext,
        focus: tuple[str, ...],
        extra_instruction: str,
    ) -> AgentOutcome:
        """在线流程：装配记忆 → 跑 Agent 循环 → 合并规则与模型结论。"""
        client = self._ensure_client()
        cfg = self.cfg
        outcome = AgentOutcome(mode="online", model=cfg.model)

        task = build_user_task(scan_result, cfg, focus=focus, extra_instruction=extra_instruction)
        state = _LoopState(protocol="tools" if client.supports_tools is not False else "json")
        memory = self._build_memory(state.protocol, task)

        self._run_agent_loop(client, memory, task, context, outcome, state)
        self._finish_online(outcome, scan_result, state, client, cfg)
        return outcome

    # ------------------------------------------------------------------ #
    # Agent 循环（拆成若干小步骤，便于单测与阅读）
    # ------------------------------------------------------------------ #
    def _build_memory(self, protocol: str, task: str) -> Memory:
        """按所用协议选择系统提示词，并写入首个任务消息。"""
        memory = Memory(
            system_prompt=SYSTEM_PROMPT if protocol == "tools" else JSON_PROTOCOL_PROMPT,
            max_chars=24000,
        )
        memory.add_user(task)
        return memory

    def _run_agent_loop(self, client, memory, task, context, outcome, state) -> None:
        """推理 → 工具调用 → 观察 → 再推理，直到提交结论、协议降级或步数用尽。"""
        cfg = self.cfg
        while state.steps < cfg.max_steps:
            state.steps += 1
            self._emit("step", step=state.steps, max_steps=cfg.max_steps, protocol=state.protocol)
            tools = self.registry.schemas() if state.protocol == "tools" else None
            response = client.chat(memory.to_messages(), tools=tools)

            if state.protocol == "tools" and client.supports_tools is False:
                # 服务端拒绝 tools：换提示词，改走 JSON 协议重来这一轮
                state.protocol = "json"
                memory = self._build_memory(
                    "json", task + "\n\n（注意：当前模型不支持工具调用，请按 JSON 协议回答。）"
                )
                self._emit("protocol_switch", reason="服务端不支持 tools")
                continue

            if state.protocol == "tools" and response.tool_calls:
                self._apply_tool_calls(response, memory, context, state)
            else:
                self._apply_text_turn(response, memory, context, state)

            if state.submission is not None or state.repeats >= 2:
                break

        if state.repeats >= 2:
            outcome.notes.append("检测到模型重复调用同一工具，已提前结束调查。")
            self._emit("degraded", reason="检测到重复工具调用")

    def _apply_tool_calls(self, response, memory: Memory, context: ToolContext, state: "_LoopState") -> None:
        """处理 function calling 协议下的一轮工具调用。"""
        memory.add_assistant(response.content, [call.to_message_part() for call in response.tool_calls])
        for call in response.tool_calls:
            self._emit("tool_call", tool=call.name, arguments=call.arguments, step=state.steps)
            if call.parse_error:
                memory.add_tool_result(
                    call.id,
                    call.name or "unknown",
                    f"[工具执行失败] {call.parse_error}；请重新以合法 JSON 传参。",
                )
                continue
            if self._register_call(call.name, call.arguments, state):
                memory.add_tool_result(
                    call.id,
                    call.name,
                    "[跳过重复调用] 你此前已用完全相同的参数调用过该工具，结果就在上文。"
                    "请不要重复调用；若信息已足够，请立即调用 submit_review 提交结论。",
                )
                self._emit("tool_result", tool=call.name, ok=False, preview="跳过重复调用")
                if state.repeats >= 2:
                    break
                continue
            result = self.registry.execute(call.name, call.arguments, context, step=state.steps)
            memory.add_tool_result(call.id, call.name, result.as_observation())
            self._emit(
                "tool_result",
                tool=call.name,
                ok=result.ok,
                preview=(result.output or result.error)[:200],
            )
            if call.name == "submit_review" and result.ok and context.submissions:
                state.submission = context.submissions[-1]

    def _register_call(self, name: str, arguments: dict, state: "_LoopState") -> bool:
        """登记工具调用；返回 True 表示这是完全重复的调用（不再执行）。"""
        signature = _call_signature(name, arguments)
        if signature not in state.seen_calls:
            state.seen_calls[signature] = 1
            return False
        state.seen_calls[signature] += 1
        state.repeats += 1
        return True

    def _apply_text_turn(self, response, memory: Memory, context: ToolContext, state: "_LoopState") -> None:
        """处理纯文本回复：可能是 JSON 协议的工具请求、最终结论，或跑题内容。"""
        state.final_text = response.content or ""
        payload = extract_json_object(state.final_text)
        if not payload:
            self._nudge(memory, state)
            return

        action = str(payload.get("action") or "").lower()
        if action == "tool" and state.protocol == "json":
            self._apply_json_tool_call(payload, memory, context, state)
            return
        if action == "final" or "findings" in payload or "summary" in payload:
            submission = _payload_to_submission(payload)
            if submission:
                context.submissions.append(submission)
                state.submission = submission
                return

        memory.add_assistant(state.final_text)
        memory.add_user("请调用 submit_review 工具提交最终结论（summary + findings）。")

    def _apply_json_tool_call(self, payload: dict, memory: Memory, context: ToolContext, state: "_LoopState") -> None:
        """JSON 降级协议下没有 tool 消息通道，改用 user 消息回灌观察结果。"""
        name = str(payload.get("tool") or "")
        arguments = payload.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        self._emit("tool_call", tool=name, arguments=arguments, step=state.steps)
        memory.add_assistant(state.final_text)
        if self._register_call(name, arguments, state):
            memory.add_user(
                "[跳过重复调用] 该工具已用相同参数调用过，结果在上文。请直接输出 action=final 的结论 JSON。"
            )
            return
        result = self.registry.execute(name, arguments, context, step=state.steps)
        memory.add_user(f"[工具 {name} 的执行结果]\n{result.as_observation()}")
        if name == "submit_review" and result.ok and context.submissions:
            state.submission = context.submissions[-1]

    def _nudge(self, memory: Memory, state: "_LoopState") -> None:
        """既没有工具调用也不是合法 JSON：催促模型收敛，而不是无限空转。"""
        if state.steps < self.cfg.max_steps:
            memory.add_assistant(state.final_text)
            memory.add_user(build_step_nudge(state.steps, self.cfg.max_steps))

    def _finish_online(self, outcome: AgentOutcome, scan_result: ScanResult, state: "_LoopState", client, cfg: Config) -> None:
        """把循环状态与统计数据落到 AgentOutcome 上，并合并两种来源的发现。"""
        outcome.steps = state.steps
        outcome.tool_calls = len(self.registry.traces)
        outcome.llm_calls = client.stats.calls
        outcome.llm_retries = client.stats.retries
        outcome.prompt_tokens = client.stats.prompt_tokens
        outcome.completion_tokens = client.stats.completion_tokens
        outcome.raw_answer = state.final_text

        if state.steps >= cfg.max_steps and state.submission is None:
            outcome.notes.append(
                f"达到最大步数限制（max_steps={cfg.max_steps}），报告基于已完成的部分调查与确定性规则生成。"
            )

        agent_findings = self._findings_from_submission(state.submission, state.final_text)
        outcome.findings = merge_findings(scan_result.findings, agent_findings)
        outcome.summary = (state.submission or {}).get("summary") or self._fallback_summary(
            scan_result, len(agent_findings)
        )
        outcome.verdict = (state.submission or {}).get("verdict") or ""
        outcome.degraded = state.submission is None
        if outcome.degraded:
            outcome.degraded_reason = "模型未按约定提交结构化结论，已回退为规则报告 + 文本结论"
            outcome.mode = "degraded"
            outcome.notes.append(outcome.degraded_reason)

    # ------------------------------------------------------------------ #
    # 结果组装
    # ------------------------------------------------------------------ #
    def _findings_from_submission(self, submission: dict | None, final_text: str) -> list[Finding]:
        """收集模型的发现：优先取 submit_review 的入参，其次从文本里抠 JSON。"""
        findings: list[Finding] = []
        for item in self._extract_raw_findings(submission, final_text)[:80]:
            if isinstance(item, dict) and item.get("title"):
                findings.append(Finding.from_llm_dict(item))
        return findings

    def _extract_raw_findings(self, submission: dict | None, final_text: str) -> list:
        if submission and isinstance(submission.get("findings"), list):
            return submission["findings"]
        if not final_text:
            return []
        payload = extract_json_object(final_text)
        if payload and isinstance(payload.get("findings"), list):
            return payload["findings"]
        array = extract_json_array(final_text)
        if array and all(isinstance(item, dict) for item in array):
            return array
        return []

    def _fallback_summary(self, scan_result: ScanResult, agent_findings: int) -> str:
        counts = scan_result.counts_by_severity()
        serious = counts.get("critical", 0) + counts.get("high", 0)
        if not scan_result.files:
            return "未找到可审查的文件，无法给出结论。"
        return (
            f"共扫描 {len(scan_result.files)} 个文件，确定性规则命中 {len(scan_result.findings)} 条问题"
            f"（其中高危 {serious} 条），模型补充发现 {agent_findings} 条。"
            "详细清单见下方按严重程度分组的问题列表。"
        )

    def _offline_outcome(self, scan_result: ScanResult, reason: str) -> AgentOutcome:
        outcome = AgentOutcome(
            mode="offline" if self.cfg.offline else "degraded",
            model="（未使用）" if self.cfg.offline else self.cfg.model,
            degraded=True,
            degraded_reason=reason,
            findings=list(scan_result.findings),
            summary=self._fallback_summary(scan_result, 0),
            notes=[OFFLINE_NOTE if self.cfg.offline else f"已降级为规则报告：{reason}"],
        )
        return outcome

    # ------------------------------------------------------------------ #
    # 交互式问答（chat 子命令）
    # ------------------------------------------------------------------ #
    def ask(
        self,
        question: str,
        memory: Memory,
        context: ToolContext,
        *,
        max_steps: int = 4,
    ) -> str:
        """带工具的问答：返回最终自然语言回答。"""
        client = self._ensure_client()
        memory.add_user(question)
        step = 0
        answer = ""
        while step < max_steps:
            step += 1
            tools = self.registry.schemas() if client.supports_tools is not False else None
            response = client.chat(memory.to_messages(), tools=tools)
            if client.supports_tools is False and response.content:
                memory.add_assistant(response.content)
                answer = response.content
                break
            if response.tool_calls:
                memory.add_assistant(response.content, [call.to_message_part() for call in response.tool_calls])
                for call in response.tool_calls:
                    if call.name == "submit_review":
                        memory.add_tool_result(call.id, call.name, "该工具仅用于整库审查，请直接回答开发者的问题。")
                        continue
                    if call.parse_error:
                        memory.add_tool_result(call.id, call.name, f"[工具执行失败] {call.parse_error}")
                        continue
                    result = self.registry.execute(call.name, call.arguments, context, step=step)
                    self._emit("tool_call", tool=call.name, arguments=call.arguments, step=step)
                    memory.add_tool_result(call.id, call.name, result.as_observation())
                continue
            answer = response.content
            memory.add_assistant(answer)
            break
        if not answer:
            answer = "（达到步数上限，未能给出结论；可以缩小问题范围后再问一次）"
        return answer


def _payload_to_submission(payload: dict) -> dict | None:
    """把模型的自由 JSON 规范化为一次 submission。"""
    summary = str(payload.get("summary") or "").strip()
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    if not summary and not findings:
        return None
    normalized = []
    for item in findings:
        if isinstance(item, dict):
            finding = Finding.from_llm_dict(item)
            if finding.file:
                normalized.append(finding.to_dict())
    return {
        "summary": summary,
        "verdict": str(payload.get("verdict") or ""),
        "findings": normalized,
    }


def build_chat_agent(cfg: Config, root: str, progress: ProgressHook | None = None) -> tuple[CodeReviewAgent, Memory, ToolContext]:
    """构造 chat 场景的三件套：Agent、记忆、工具上下文。"""
    scan_result = scan_workspace(root, cfg)
    agent = CodeReviewAgent(cfg, progress=progress)
    memory = Memory(system_prompt=build_chat_system_prompt(root), max_chars=20000)
    memory.add_user(
        "这是接下来要讨论的代码库概况，请先记住，不必逐条复述：\n" + build_scan_digest(scan_result, cfg, max_chars=2500)
    )
    context = ToolContext(root=scan_result.root, cfg=cfg, scan=scan_result)
    return agent, memory, context


__all__ = ["CodeReviewAgent", "build_chat_agent", "SEVERITY_LABEL_ZH"]
