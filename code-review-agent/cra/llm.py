"""LLM 客户端：原生 HTTP 调用 OpenAI 兼容接口（默认 DeepSeek）。

设计要点（对应作业里的"错误处理与重试机制"）：
    * 分层重试：网络抖动 / 429 / 5xx 指数退避重试，401、400 直接失败不浪费时间；
    * 尊重 Retry-After 响应头；
    * 模型能力自适应：若服务端或模型不支持 tools，则自动关闭工具并降级为 JSON 协议；
    * 传输层可注入（transport 参数），因此单元测试完全不需要联网；
    * 统计调用次数、token 用量与重试次数，供报告展示。
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Config
from .errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
)

logger = logging.getLogger("cra.llm")

#: transport 签名：(url, headers, body_bytes, timeout) -> (status, payload, response_headers)
Transport = Callable[[str, dict, bytes, float], "tuple[int | None, Any, dict]"]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""

    def to_message_part(self) -> dict:
        """还原成回传给模型的 tool_calls 结构。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or "{}"},
        }


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class LLMStats:
    calls: int = 0
    retries: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency: float = 0.0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_latency": round(self.total_latency, 2),
        }


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #
def http_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int | None, Any, dict]:
    """标准库 urllib 传输层：零第三方依赖，任何 OpenAI 兼容服务都可用。"""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, _loads(raw), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _loads(raw), dict(exc.headers or {})
    except urllib.error.URLError as exc:
        raise LLMNetworkError(f"网络不可达：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMNetworkError(f"请求超时（{timeout}s）") from exc
    except OSError as exc:
        raise LLMNetworkError(f"连接失败：{exc}") from exc


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}


class SdkTransport:
    """可选的 openai 官方 SDK 传输层（需要 pip install openai）。

    因为请求体本来就是 OpenAI 协议格式，这里只做一层薄封装，
    既能复用同一套重试/降级逻辑，也方便展示"传输层可替换"的架构。
    """

    def __init__(self, api_key: str, base_url: str, timeout: float) -> None:
        try:
            from openai import OpenAI  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - 依赖可选
            raise LLMError("未安装 openai SDK，请先 pip install openai，或使用默认 http 传输层。") from exc
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def __call__(self, url: str, headers: dict, body: bytes, timeout: float) -> tuple[int | None, Any, dict]:
        payload = json.loads(body.decode("utf-8"))
        try:
            response = self._client.chat.completions.create(**payload)
            return 200, response.model_dump(), {}
        except Exception as exc:  # noqa: BLE001 - 统一翻译为 (status, payload)
            status = getattr(exc, "status_code", None)
            return status, {"error": {"message": str(exc)}}, {}


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class LLMClient:
    """OpenAI 兼容协议的对话客户端。"""

    def __init__(
        self,
        cfg: Config,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.api_key = cfg.resolve_api_key()
        self.base_url = cfg.base_url.rstrip("/")
        self.model = cfg.model
        self.stats = LLMStats()
        self.supports_tools: bool | None = None
        self._sleep = sleep
        self._rng = random.Random(20261007)  # 固定种子：重试节奏可复现
        if transport is not None:
            self._transport = transport
        elif cfg.transport == "sdk":
            self._transport = SdkTransport(self.api_key, self.base_url, cfg.timeout)
        else:
            self._transport = http_transport

    # ------------------------------------------------------------------ #
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "code-review-agent/1.0",
        }

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: Sequence[dict],
        *,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """发起一次对话；自动重试与降级，失败时抛出分层异常。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format
        if tools and self.supports_tools is not False:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        return self._request(payload, tools_requested=bool(tools and self.supports_tools is not False))

    # ------------------------------------------------------------------ #
    def _request(self, payload: dict, *, tools_requested: bool) -> LLMResponse:
        attempt = 0
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        while True:
            started = time.time()
            try:
                status, data, headers = self._transport(self.endpoint, self._headers(), body, self.cfg.timeout)
                self.stats.total_latency += time.time() - started
                if status == 200:
                    response = self._parse(data)
                    self.stats.calls += 1
                    return response
                error = self._error_for(status, data, headers)
                # 模型/服务端不支持 tools → 关闭工具并立即重试一次（不算失败）
                if (
                    tools_requested
                    and isinstance(error, LLMBadRequestError)
                    and self.supports_tools is not False
                    and _mentions_tools(str(error))
                ):
                    logger.warning("服务端不支持 tools，降级为 JSON 协议：%s", error)
                    self.supports_tools = False
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    continue
                raise error
            except LLMError as exc:
                if not exc.retryable or attempt >= self.cfg.max_retries:
                    self.stats.failures += 1
                    raise
                attempt += 1
                self.stats.retries += 1
                delay = self._backoff(attempt)
                logger.warning("第 %d 次重试（%.1fs 后）：%s", attempt, delay, exc)
                self._sleep(delay)
            except Exception as exc:  # noqa: BLE001 - 传输层任何意外都不应炸掉 Agent
                if attempt >= self.cfg.max_retries:
                    self.stats.failures += 1
                    raise LLMError(f"调用失败：{type(exc).__name__}: {exc}") from exc
                attempt += 1
                self.stats.retries += 1
                self._sleep(self._backoff(attempt))

    def _backoff(self, attempt: int) -> float:
        base = self.cfg.retry_base_delay * (2 ** (attempt - 1))
        jitter = self._rng.uniform(0, 0.4) * base
        return min(base + jitter, 20.0)

    # ------------------------------------------------------------------ #
    def _error_for(self, status: int | None, data: Any, headers: dict) -> LLMError:
        message = _error_message(data) or f"HTTP {status}"
        if status in (401, 403):
            return LLMAuthError(f"鉴权失败（HTTP {status}）：{message}", status=status)
        if status == 429:
            retry_after = _retry_after(headers)
            suffix = f"，服务端建议 {retry_after:.0f}s 后重试" if retry_after else ""
            return LLMRateLimitError(f"触发限流（HTTP 429）：{message}{suffix}", status=status)
        if status is not None and status >= 500:
            return LLMServerError(f"服务端错误（HTTP {status}）：{message}", status=status)
        if status is not None and 400 <= status < 500:
            return LLMBadRequestError(f"请求被拒绝（HTTP {status}）：{message}", status=status)
        return LLMError(f"未知错误（status={status}）：{message}", status=status)

    def _parse(self, data: Any) -> LLMResponse:
        if not isinstance(data, dict):
            raise LLMResponseError(f"响应不是 JSON 对象：{str(data)[:200]}")
        if "error" in data and data["error"]:
            raise LLMError(f"接口返回错误：{_error_message(data)}")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(f"响应缺少 choices 字段：{str(data)[:200]}")
        choice = choices[0]
        message = choice.get("message") or {}
        usage_data = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
            completion_tokens=int(usage_data.get("completion_tokens") or 0),
            total_tokens=int(usage_data.get("total_tokens") or 0),
        )
        self.stats.prompt_tokens += usage.prompt_tokens
        self.stats.completion_tokens += usage.completion_tokens

        tool_calls: list[ToolCall] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            arguments: dict = {}
            parse_error = ""
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                try:
                    parsed = json.loads(raw_arguments or "{}")
                    arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
                except (json.JSONDecodeError, ValueError) as exc:
                    parse_error = f"工具参数不是合法 JSON：{exc}"
            tool_calls.append(
                ToolCall(
                    id=raw_call.get("id") or f"call_{index}",
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    raw_arguments=raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments, ensure_ascii=False),
                    parse_error=parse_error,
                )
            )
            if tool_calls[-1].parse_error:
                logger.warning("工具参数解析失败：%s", tool_calls[-1].parse_error)

        return LLMResponse(
            content=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=str(choice.get("finish_reason") or ""),
        )


# --------------------------------------------------------------------------- #
def _error_message(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        if "_raw" in data:
            return str(data["_raw"])[:200]
    return str(data)[:200] if data else ""


def _mentions_tools(text: str) -> bool:
    lowered = text.lower()
    return "tool" in lowered or "function" in lowered


def _retry_after(headers: dict) -> float:
    for key, value in (headers or {}).items():
        if str(key).lower() == "retry-after":
            try:
                return float(str(value).strip())
            except ValueError:
                return 0.0
    return 0.0
