"""工具层基础设施：工具声明、参数校验、注册表、执行结果。

工具是 Agent 与真实世界之间的唯一接口。这里做三件关键的事：
    1. 把工具描述成 JSON Schema，交给模型做 function calling；
    2. 在执行前校验/纠正参数（模型经常把数字写成字符串）；
    3. 把异常收敛成"失败观察结果"，让 Agent 能自我纠正而不是崩溃。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..errors import PathSecurityError, ToolArgumentError, ToolError
from ..models import ScanResult, ToolTrace

logger = logging.getLogger("cra.tools")


# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    meta: dict = field(default_factory=dict)

    @classmethod
    def success(cls, output: str, **meta) -> "ToolResult":
        return cls(ok=True, output=output, meta=meta)

    @classmethod
    def failure(cls, error: str, **meta) -> "ToolResult":
        return cls(ok=False, error=error, meta=meta)

    def as_observation(self) -> str:
        """转成回灌给模型的文本（失败也要给出可操作的提示）。"""
        if self.ok:
            return self.output or "（无输出）"
        return f"[工具执行失败] {self.error}"


@dataclass
class ToolContext:
    """一次审查会话中工具可访问的共享状态。"""

    root: str
    cfg: Config
    scan: ScanResult | None = None
    submissions: list[dict] = field(default_factory=list)
    scan_calls: int = 0
    #: 单文件审查模式下的目标文件（相对 root）。设置后所有工具只能访问这一个文件，
    #: 避免"审一个文件"时 Agent 顺手把同目录（甚至整个桌面）的其他文件也读了。
    scope_file: str | None = None
    logger: logging.Logger = logger

    def analyses(self) -> dict[str, dict]:
        return dict(self.scan.analyses) if self.scan else {}


def ensure_in_scope(ctx: "ToolContext", absolute_path: str) -> None:
    """单文件模式下校验目标路径，越界直接拒绝。

    Args:
        ctx: 工具上下文（含 scope_file）。
        absolute_path: 已经过 safe_path 校验的绝对路径。

    Raises:
        PathSecurityError: 目标不是本次被指定的那个文件。
    """
    if not ctx.scope_file:
        return
    expected = os.path.normcase(os.path.abspath(os.path.join(ctx.root, ctx.scope_file)))
    actual = os.path.normcase(os.path.abspath(absolute_path))
    if actual != expected:
        raise PathSecurityError(
            f"本次只审查单个文件 {ctx.scope_file}，不允许访问其他路径（{os.path.basename(absolute_path)}）。"
            "如果要审查整个目录，请把目录作为参数传入。"
        )


HandlerType = Callable[..., "str | ToolResult"]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: HandlerType
    terminal: bool = False  # 终止型工具：调用即代表 Agent 给出最终结论

    def to_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------- #
def validate_arguments(schema: dict, arguments: dict) -> tuple[dict, list[str]]:
    """按 JSON Schema（只用得到的最小子集）校验并轻度纠正参数。

    返回 (清洗后的参数, 错误列表)。错误会作为观察结果回灌给模型，促使其修正调用。
    """
    if not isinstance(arguments, dict):
        return {}, [f"参数必须是 JSON 对象，收到 {type(arguments).__name__}"]

    properties: dict = schema.get("properties") or {}
    required: list = schema.get("required") or []
    cleaned: dict[str, Any] = {}
    errors: list[str] = []

    for key in required:
        if key not in arguments or arguments[key] in (None, ""):
            errors.append(f"缺少必填参数 {key!r}")

    for key, value in arguments.items():
        if key not in properties:
            errors.append(f"未知参数 {key!r}（可用参数：{', '.join(properties) or '无'}）")
            continue
        expected = properties[key].get("type")
        cleaned[key] = _coerce(key, value, expected, errors)

    return cleaned, errors


def _coerce(key: str, value: Any, expected: str | None, errors: list[str]) -> Any:
    try:
        if value is None:
            return None
        if expected == "integer":
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if expected == "number":
            return float(value)
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "y", "on")
        if expected == "string":
            return value if isinstance(value, str) else str(value)
        if expected == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                # 模型常把"数组里的一个对象"直接当成数组传进来，这里明确报错让它自我修正
                raise ValueError("期望数组，却收到单个对象；请用 [ ... ] 包裹")
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    return json.loads(stripped)
                return [item.strip() for item in stripped.split(",") if item.strip()]
            return [value]
        if expected == "object":
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                return json.loads(value)
            raise ValueError("无法转换为对象")
        return value
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"参数 {key!r} 期望 {expected}，实际无法转换（{exc}）")
        return value


# --------------------------------------------------------------------------- #
class ToolRegistry:
    """工具注册表：统一暴露 schema 与执行入口。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.traces: list[ToolTrace] = []

    # ------------------------------------------------------------------ #
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: HandlerType,
        *,
        terminal: bool = False,
    ) -> Tool:
        return self.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                terminal=terminal,
            )
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        return [self._tools[name].to_schema() for name in self.names()]

    def describe(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" for t in self._tools.values())

    # ------------------------------------------------------------------ #
    def execute(self, name: str, arguments: dict, ctx: ToolContext, *, step: int = 0) -> ToolResult:
        """执行工具，绝不向调用方抛异常（除 KeyboardInterrupt）。"""
        started = time.time()
        tool = self._tools.get(name)
        if tool is None:
            result = ToolResult.failure(
                f"不存在名为 {name!r} 的工具。可用工具：{', '.join(self.names())}"
            )
            self._trace(step, name, arguments, result, time.time() - started)
            return result

        cleaned, errors = validate_arguments(tool.parameters, arguments or {})
        if errors:
            result = ToolResult.failure("参数有误：" + "；".join(errors))
            self._trace(step, name, arguments, result, time.time() - started)
            return result

        try:
            raw = tool.handler(ctx, **cleaned)
            result = raw if isinstance(raw, ToolResult) else ToolResult.success(str(raw))
        except ToolArgumentError as exc:
            result = ToolResult.failure(f"参数错误：{exc}")
        except ToolError as exc:
            result = ToolResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001 - 工具内部异常统一转成观察结果
            logger.exception("工具 %s 执行异常", name)
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")

        self._trace(step, name, cleaned, result, time.time() - started)
        return result

    def _trace(self, step: int, name: str, arguments: dict, result: ToolResult, duration: float) -> None:
        preview = (result.error if not result.ok else result.output) or ""
        self.traces.append(
            ToolTrace(
                step=step,
                tool=name,
                arguments=_truncate_arguments(arguments),
                ok=result.ok,
                error=result.error,
                output_preview=preview[:400],
                duration=round(duration, 3),
            )
        )


def _truncate_arguments(arguments: dict) -> dict:
    """轨迹里保存参数摘要，避免把超长文件内容写进报告。"""
    safe: dict = {}
    for key, value in (arguments or {}).items():
        text = value if isinstance(value, str) else repr(value)
        safe[key] = text if len(text) <= 120 else text[:117] + "..."
    return safe
