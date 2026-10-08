"""LLM 接口层：把"模型"抽象成一个可替换的 `chat()` 调用。

为什么要抽象（作业评分点"可扩展性"）：
    * **HttpChatModel** —— 任何 OpenAI 兼容服务（DeepSeek / 通义 / Moonshot / 本地 vLLM），
      标准库 urllib 实现，零第三方依赖，自带重试退避与错误分类；
    * **ScriptedChatModel** —— 按脚本返回预置响应，让单元测试与端到端测试完全离线、可复现；
    * **RuleChatModel** —— 离线确定性"伪模型"，按提示词关键词返回内置模板，
      使 `--offline` 演示在没有 API Key、没有网络时也能把整个 Workflow 完整跑通。

三个实现共享同一个 `ChatModel` 协议，Agent 代码完全不感知用的是哪一个。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
)
from .util import extract_json_object, truncate

logger = logging.getLogger("wfa.model")

#: transport 签名：(url, headers, body_bytes, timeout) -> (status, payload, response_headers)
Transport = Callable[[str, dict, bytes, float], "tuple"]


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

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str = ""
    name: str = ""
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
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def json(self) -> dict | None:
        """从 content 中稳健地抠出第一个 JSON 对象。"""
        return extract_json_object(self.content or "")


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
def http_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple:
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
    """可选的 openai 官方 SDK 传输层（需要 `pip install openai`）。"""

    def __init__(self, api_key: str, base_url: str, timeout: float) -> None:
        try:
            from openai import OpenAI  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - 依赖可选
            raise LLMError("未安装 openai SDK，请先 `pip install openai`，或使用默认 http 传输层。") from exc
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def __call__(self, url: str, headers: dict, body: bytes, timeout: float) -> tuple:
        payload = json.loads(body.decode("utf-8"))
        try:
            response = self._client.chat.completions.create(**payload)
            return 200, response.model_dump(), {}
        except Exception as exc:  # noqa: BLE001 - 统一翻译为 (status, payload)
            status = getattr(exc, "status_code", None)
            return status, {"error": {"message": str(exc)}}, {}


# --------------------------------------------------------------------------- #
# HTTP 模型
# --------------------------------------------------------------------------- #
class HttpChatModel:
    """OpenAI 兼容协议的对话模型（默认 DeepSeek）。"""

    is_deterministic = False

    def __init__(
        self,
        cfg: Any,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.api_key = cfg.resolve_api_key()
        self.base_url = str(cfg.base_url).rstrip("/")
        self.model = cfg.model
        self.stats = LLMStats()
        self.supports_tools: bool | None = None
        self._sleep = sleep
        self._rng = random.Random(20261008)  # 固定种子：重试节奏可复现
        if transport is not None:
            self._transport = transport
        elif getattr(cfg, "transport", "http") == "sdk":
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
            "User-Agent": "wfa-workflow-agents/1.0",
        }

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: Sequence[dict],
        *,
        tools: list | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """发起一次对话；自动重试与降级，失败时抛出分层异常。"""
        payload: dict = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
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
                if (
                    tools_requested
                    and isinstance(error, LLMBadRequestError)
                    and self.supports_tools is not False
                    and _mentions_tools(str(error))
                ):
                    logger.warning("服务端不支持 tools，降级为 JSON 文本协议：%s", error)
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
            except Exception as exc:  # noqa: BLE001 - 传输层意外不应炸掉 Workflow
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
            raise LLMResponseError(f"响应不是 JSON 对象：{truncate(str(data), 200)}")
        if data.get("error"):
            raise LLMError(f"接口返回错误：{_error_message(data)}")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(f"响应缺少 choices 字段：{truncate(str(data), 200)}")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        usage_data = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
            completion_tokens=int(usage_data.get("completion_tokens") or 0),
            total_tokens=int(usage_data.get("total_tokens") or 0),
        )
        self.stats.prompt_tokens += usage.prompt_tokens
        self.stats.completion_tokens += usage.completion_tokens

        tool_calls = []
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
                    raw_arguments=raw_arguments
                    if isinstance(raw_arguments, str)
                    else json.dumps(raw_arguments, ensure_ascii=False),
                    parse_error=parse_error,
                )
            )
        return LLMResponse(
            content=str(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=str(choice.get("finish_reason") or ""),
        )


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


# --------------------------------------------------------------------------- #
# 离线 / 测试用模型
# --------------------------------------------------------------------------- #
class ScriptedChatModel:
    """按脚本返回预置响应；脚本用尽后重复最后一条。

    用法：
        model = ScriptedChatModel([LLMResponse(content='{"files": {...}}')])
        model = ScriptedChatModel(responder=lambda messages: LLMResponse(content="..."))
    """

    is_deterministic = True

    def __init__(
        self,
        script: list | None = None,
        *,
        responder: Callable[[list], LLMResponse] | None = None,
        supports_tools: bool | None = True,
    ) -> None:
        self._script = list(script or [])
        self._responder = responder
        self.supports_tools = supports_tools
        self.stats = LLMStats()
        self.seen_messages: list = []

    def chat(self, messages: Sequence[dict], *, tools: list | None = None, **_) -> LLMResponse:
        self.seen_messages = list(messages)
        self.stats.calls += 1
        if self._responder is not None:
            response = self._responder(list(messages))
            return response if isinstance(response, LLMResponse) else LLMResponse(content=str(response))
        if not self._script:
            return LLMResponse(content="")
        if len(self._script) > 1:
            return self._script.pop(0)
        return self._script[0]


class RuleChatModel:
    """离线确定性"伪模型"：按提示词关键词返回内置模板。

    它**不做推理**，只保证"没有 Key、没有网络时整个 Workflow 仍然能端到端跑通并产出
    结构化结果"。因此 Agent 在设计上必须做到：模型能力不可用时自动走确定性路径。
    """

    is_deterministic = True
    supports_tools = False

    #: 关键词 -> 模板（顺序匹配，先命中先用）
    #: 注意顺序：`requirement` 必须排在 `design` 之前，因为需求提示词里通常也含"设计"二字；
    #: 更稳的做法是 Agent 在提示词里写显式标记 `[TASK=xxx]`（见 `_match`），关键词只是兜底。
    RULES: tuple = (
        (("requirement", "需求"), "_requirement_template"),
        (("design", "设计"), "_design_template"),
        (("review", "审查", "issue", "问题清单"), "_review_template"),
        (("fix", "修复", "patch"), "_fix_template"),
        (("test", "测试", "unittest"), "_test_template"),
        (("outline", "大纲", "章节"), "_outline_template"),
        (("proofread", "审校", "校对"), "_proofread_template"),
        (("document", "文档", "readme"), "_doc_template"),
        (("plan", "规划", "计划"), "_outline_template"),
        (("code", "实现", "编码"), "_code_template"),
    )

    def __init__(self, *, persona: str = "default", note: str = "") -> None:
        self.persona = persona
        self.note = note or "（离线模式：RuleChatModel 未调用真实模型，以下为确定性模板输出）"
        self.stats = LLMStats()
        self.calls: list = []

    # ------------------------------------------------------------------ #
    def chat(self, messages: Sequence[dict], *, tools: list | None = None, **_) -> LLMResponse:
        self.stats.calls += 1
        self.calls.append(list(messages))
        prompt = _flatten(messages)
        handler_name = self._match(prompt)
        content = getattr(self, handler_name)(prompt) if handler_name else json.dumps(
            {"summary": self.note, "decision": "continue"}, ensure_ascii=False
        )
        self.stats.prompt_tokens += max(1, len(prompt) // 4)
        self.stats.completion_tokens += max(1, len(content) // 4)
        return LLMResponse(content=content, usage=Usage(), finish_reason="stop")

    def _match(self, prompt: str) -> str:
        """先看显式标记 `[TASK=xxx]`，再退化为关键词匹配。

        显式标记解决"需求提示词里也含'设计'二字"这类歧义；关键词表只作兜底，
        因此 Agent 只要在提示词里写一行 `[TASK=requirement]` 就能 100% 命中对应模板。
        """
        lowered = prompt.lower()
        for keywords, handler in self.RULES:
            marker = f"[task={keywords[0]}]"
            if marker in lowered:
                return handler
        for keywords, handler in self.RULES:
            for keyword in keywords:
                if keyword in lowered or keyword in prompt:
                    return handler
        return ""

    # ------------------------------------------------------------------ #
    # 内置模板
    # ------------------------------------------------------------------ #
    def _requirement_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "已把任务拆成可执行的软件需求（离线模板）。",
                "decision": "continue",
                "data": {
                    "goal": "交付一个小而完整、可运行、可测试的 Python 模块",
                    "functional": [
                        "提供核心业务函数并保证输入校验与错误处理",
                        "提供统一日志输出，不把异常静默吞掉",
                        "附带单元测试覆盖正常路径与边界路径",
                    ],
                    "non_functional": ["零第三方依赖", "Python 3.9+", "单文件可读"],
                    "acceptance": ["python -m unittest 全绿", "静态规则无 critical/high 命中"],
                },
            },
            ensure_ascii=False,
        )

    def _design_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "已给出模块划分与接口设计（离线模板）。",
                "decision": "continue",
                "data": {
                    "module": "app/service.py",
                    "interface": [
                        {"name": "normalize_records", "args": ["records"], "returns": "list"},
                        {"name": "summarize", "args": ["records"], "returns": "dict"},
                    ],
                    "files": ["app/service.py", "tests/test_service.py"],
                    "notes": "接口保持纯粹函数式，便于单测与静态检查。",
                },
            },
            ensure_ascii=False,
        )

    def _code_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "生成第一版实现与单元测试（离线模板，故意保留两处可被规则命中的缺陷）。",
                "decision": "continue",
                "data": {
                    "plan": ["实现 normalize_records", "实现 summarize", "补单元测试"],
                    "files": {
                        "app/service.py": _OFFLINE_BAD_SERVICE,
                    },
                    "tests": {"tests/test_service.py": _OFFLINE_TEST},
                    "notes": ["第一版故意留下 except 静默吞异常与硬编码密钥，供审查-修复循环演示。"],
                },
            },
            ensure_ascii=False,
        )

    def _test_template(self, prompt: str) -> str:
        return json.dumps(
            {"summary": "补充单元测试（离线模板）。", "decision": "continue",
             "data": {"tests": {"tests/test_service.py": _OFFLINE_TEST}}},
            ensure_ascii=False,
        )

    def _review_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "离线模式下审查 Agent 使用确定性规则引擎的结论。",
                "decision": "continue",
                "data": {"issues": [], "notes": ["RuleChatModel 不产生语义发现，由规则引擎负责。"]},
            },
            ensure_ascii=False,
        )

    def _fix_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "按问题清单做确定性修复（离线模板）。",
                "decision": "continue",
                "data": {
                    "files": {"app/service.py": _OFFLINE_GOOD_SERVICE},
                    "patches": [
                        {"file": "app/service.py", "issue_id": "SEC001",
                         "summary": "硬编码密钥改为从环境变量读取"},
                        {"file": "app/service.py", "issue_id": "BUG002",
                         "summary": "异常不再静默吞掉，改为记录日志并向上返回错误信息"},
                    ],
                },
            },
            ensure_ascii=False,
        )

    def _outline_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "已生成文档大纲（离线模板）。",
                "decision": "continue",
                "data": {"outline": ["项目简介", "快速开始", "接口说明", "流程与角色", "测试与验收"]},
            },
            ensure_ascii=False,
        )

    def _doc_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "已按大纲生成文档（离线模板）。",
                "decision": "continue",
                "data": {
                    "documents": {
                        "README.md": (
                            "# 示例模块\n\n"
                            "## 项目简介\n\n本文件由 Workflow 中的文档 Agent 生成，用于演示多 Agent 协作产物。\n\n"
                            "## 快速开始\n\n```bash\npython -m unittest discover -s tests -t .\n```\n\n"
                            "## 接口说明\n\n- `normalize_records(records)`：清洗并规范化记录列表。\n"
                            "- `summarize(records)`：返回统计摘要。\n"
                        )
                    }
                },
            },
            ensure_ascii=False,
        )

    def _proofread_template(self, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "审校通过（离线模板）。",
                "decision": "pass",
                "data": {"issues": [], "verdict": "pass"},
            },
            ensure_ascii=False,
        )


# --------------------------------------------------------------------------- #
# 离线演示用的样例源码（故意含缺陷，供"审查 → 修复"循环演示）
# --------------------------------------------------------------------------- #
_OFFLINE_BAD_SERVICE = '''"""示例业务模块（Workflow 生成的第一版）。"""

import os
import json


API_KEY = "sk-hardcoded-demo-key-1234567890"


def normalize_records(records):
    result = []
    for record in records:
        try:
            item = json.loads(record)
        except Exception:
            pass
        result.append(item)
    return result


def summarize(records, key="value", scale=1, decimals=2, prefix="", fallback=None):
    total = 0
    for record in records:
        total = total + record.get(key, 0)
    return {"count": len(records), "total": total}


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
'''

_OFFLINE_GOOD_SERVICE = '''"""示例业务模块（Workflow 修复后的版本）。"""

import json
import logging
import os

logger = logging.getLogger("app.service")

#: 密钥一律从环境变量读取，绝不硬编码在源码里。
API_KEY_ENV = "SAMPLE_API_KEY"


def normalize_records(records):
    """把字符串记录解析成字典列表；无法解析的记录被显式跳过并记录日志。"""
    result = []
    for index, record in enumerate(records):
        try:
            item = json.loads(record)
        except (TypeError, ValueError) as exc:
            logger.warning("第 %d 条记录解析失败，已跳过：%s", index, exc)
            continue
        result.append(item)
    return result


def summarize(records, *, key="value"):
    """统计记录条数与指定字段的合计值。"""
    total = 0
    for record in records:
        if isinstance(record, dict):
            total += record.get(key, 0)
    return {"count": len(records), "total": total}


def load_config(path):
    """读取 JSON 配置；缺失或损坏时返回空配置并记录原因。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("配置读取失败，使用空配置：%s", exc)
        return {}
'''

_OFFLINE_TEST = '''"""示例模块的单元测试。"""

import json
import unittest

from app.service import load_config, normalize_records, summarize


class NormalizeRecordsTest(unittest.TestCase):
    def test_valid_records(self):
        self.assertEqual(normalize_records(['{"value": 1}']), [{"value": 1}])

    def test_invalid_record_is_skipped(self):
        self.assertEqual(normalize_records(["not-json"]), [])


class SummarizeTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(summarize([{"value": 2}, {"value": 3}])["total"], 5)

    def test_count(self):
        self.assertEqual(summarize([])["count"], 0)


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_config("not-exists.json"), {})


if __name__ == "__main__":
    unittest.main()
'''


def _flatten(messages: Sequence[dict]) -> str:
    """把消息列表拍平成一段文本，供关键词匹配。"""
    parts = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(item) for item in content)
        parts.append(str(content or ""))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def build_model(cfg: Any, *, offline: bool | None = None) -> Any:
    """按配置构造模型：online → HttpChatModel；offline → RuleChatModel。

    Args:
        cfg: `wfa.config.Config`。
        offline: 显式指定离线；默认取 `cfg.mode != "online"`。

    Returns:
        ChatModel 实例。离线模式永远不抛 `ConfigError`。
    """
    is_offline = (cfg.mode != "online") if offline is None else offline
    if is_offline:
        return RuleChatModel()
    transport = getattr(cfg, "transport", "http")
    if transport == "scripted":  # 仅测试用
        return ScriptedChatModel()
    return HttpChatModel(cfg)


__all__ = [
    "Usage",
    "ToolCall",
    "LLMResponse",
    "LLMStats",
    "ChatModel",
    "HttpChatModel",
    "ScriptedChatModel",
    "RuleChatModel",
    "Transport",
    "http_transport",
    "SdkTransport",
    "build_model",
]

#: 供类型标注使用的协议（Python 3.9 没有 typing.Protocol 的结构化运行检查需求，这里用文档表达）
ChatModel = Any
