"""`wfa/model.py` 单元测试：HTTP 模型（假 transport 离线）、脚本模型与规则模型。

覆盖点（对应 CONTRACT 第 2 节 LLM 接口层）：
    1. ``HttpChatModel`` 用**假 transport** 完全离线测试：请求体/请求头/端点拼接、
       重试退避（注入 ``sleep`` 记录器并断言 ``stats.retries``）、401 不重试抛
       ``LLMAuthError``、429 重试后成功、5xx 重试耗尽、网络错误重试、
       服务端不支持 tools 时**自动降级**（``supports_tools`` 变 False、后续请求不再带 tools）、
       脏响应结构抛 ``LLMResponseError``、工具调用参数解析与错误标记、错误分类表；
    2. ``ScriptedChatModel``：按顺序返回、用尽后复用最后一条、responder 模式；
    3. ``RuleChatModel``：离线确定性模板（关键词 → 模板）与 ``supports_tools=False``；
    4. ``Usage`` / ``ToolCall`` / ``LLMStats`` / ``Response`` 数据结构与 ``build_model`` 工厂。

全部用例不触网、不需要 API Key；退避等待由注入的 ``sleep`` 记录（真实耗时为 0）。
"""

from __future__ import annotations

import json
import logging
import unittest

from wfa.config import Config
from wfa.errors import (
    ConfigError,
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
)
from wfa.model import (
    HttpChatModel,
    LLMResponse,
    LLMStats,
    RuleChatModel,
    ScriptedChatModel,
    ToolCall,
    Usage,
    build_model,
)

#: 本模块会大量触发 wfa.model 的"重试/降级"warning，测试期压低级别以免刷屏。
_LOGGER = logging.getLogger("wfa.model")


def setUpModule():
    """屏蔽 wfa.model 的 WARNING（用例本身已断言重试/降级行为）。"""
    _LOGGER.setLevel(logging.ERROR)


def tearDownModule():
    """恢复日志级别，避免影响其它测试模块。"""
    _LOGGER.setLevel(logging.NOTSET)


def ok_payload(content: str = "hello", usage: dict | None = None,
               tool_calls: list | None = None, finish_reason: str = "stop") -> dict:
    """构造一个标准的 OpenAI 兼容成功响应。"""
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


class FakeTransport:
    """记录请求并按脚本返回 (status, payload, headers) 或抛异常的假传输层。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({
            "url": url,
            "headers": headers,
            "body": json.loads(body.decode("utf-8")),
            "timeout": timeout,
        })
        if len(self.responses) > 1:
            item = self.responses.pop(0)
        elif self.responses:
            item = self.responses[0]
        else:  # pragma: no cover - 测试都会给足响应
            item = (200, ok_payload(), {})
        if isinstance(item, BaseException):
            raise item
        return item


def make_model(transport, **overrides):
    """构造 HttpChatModel + 记录 sleep 的列表。"""
    slept: list = []
    kwargs = {
        "api_key": "sk-test-key",
        "base_url": "https://example.invalid/v1",
        "model": "test-model",
        "timeout": 5.0,
        "max_retries": 3,
        "retry_base_delay": 0.01,
        "temperature": 0.2,
        "max_tokens": 128,
    }
    kwargs.update(overrides)
    cfg = Config(**kwargs)
    model = HttpChatModel(cfg, transport=transport, sleep=slept.append)
    return model, slept


class DataStructureTest(unittest.TestCase):
    """Usage / ToolCall / LLMResponse / LLMStats。"""

    def test_usage_add(self):
        total = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        total.add(Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30))
        self.assertEqual(total.to_dict(),
                         {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33})

    def test_tool_call_message_part(self):
        call = ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"},
                        raw_arguments='{"path": "a.py"}')
        part = call.to_message_part()
        self.assertEqual(part["id"], "call_1")
        self.assertEqual(part["type"], "function")
        self.assertEqual(part["function"]["name"], "read_file")
        self.assertEqual(json.loads(part["function"]["arguments"]), {"path": "a.py"})

    def test_tool_call_message_part_defaults_arguments(self):
        self.assertEqual(ToolCall(id="x", name="n").to_message_part()["function"]["arguments"], "{}")

    def test_response_wants_tools_and_json(self):
        response = LLMResponse(content='前言 {"a": 1} 后记',
                               tool_calls=[ToolCall(name="t")])
        self.assertTrue(response.wants_tools)
        self.assertEqual(response.json(), {"a": 1})
        self.assertFalse(LLMResponse().wants_tools)
        self.assertIsNone(LLMResponse(content="不是 JSON").json())

    def test_stats_as_dict(self):
        stats = LLMStats(calls=2, retries=1, failures=0, prompt_tokens=10,
                         completion_tokens=20, total_latency=1.23456)
        payload = stats.as_dict()
        self.assertEqual(payload["calls"], 2)
        self.assertEqual(payload["retries"], 1)
        self.assertEqual(payload["total_latency"], 1.23)


class HttpChatModelSuccessTest(unittest.TestCase):
    """一次成功调用的请求形态与统计。"""

    def test_content_usage_and_stats(self):
        transport = FakeTransport([(200, ok_payload("你好"), {})])
        model, slept = make_model(transport)

        response = model.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.content, "你好")
        self.assertEqual(response.usage.total_tokens, 7)
        self.assertEqual(response.finish_reason, "stop")
        self.assertEqual(model.stats.calls, 1)
        self.assertEqual(model.stats.retries, 0)
        self.assertEqual(model.stats.failures, 0)
        self.assertEqual(model.stats.prompt_tokens, 3)
        self.assertEqual(model.stats.completion_tokens, 4)
        self.assertGreaterEqual(model.stats.total_latency, 0.0)
        self.assertEqual(slept, [])

    def test_request_payload_and_headers(self):
        transport = FakeTransport([(200, ok_payload(), {})])
        model, _ = make_model(transport)

        model.chat([{"role": "user", "content": "hi"}])
        request = transport.requests[0]

        self.assertEqual(request["url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-test-key")
        self.assertEqual(request["timeout"], 5.0)
        self.assertEqual(request["body"]["model"], "test-model")
        self.assertEqual(request["body"]["temperature"], 0.2)
        self.assertEqual(request["body"]["max_tokens"], 128)
        self.assertFalse(request["body"]["stream"])
        self.assertNotIn("tools", request["body"])

    def test_explicit_overrides_win(self):
        transport = FakeTransport([(200, ok_payload(), {})])
        model, _ = make_model(transport)
        model.chat([{"role": "user", "content": "hi"}], temperature=0.9, max_tokens=64)
        body = transport.requests[0]["body"]
        self.assertEqual(body["temperature"], 0.9)
        self.assertEqual(body["max_tokens"], 64)

    def test_endpoint_strips_trailing_slash(self):
        model, _ = make_model(FakeTransport([(200, ok_payload(), {})]),
                              base_url="https://example.invalid/v1/")
        self.assertEqual(model.endpoint, "https://example.invalid/v1/chat/completions")

    def test_tools_are_sent_when_supported(self):
        transport = FakeTransport([(200, ok_payload(), {})])
        model, _ = make_model(transport)
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        model.chat([{"role": "user", "content": "hi"}], tools=tools)

        body = transport.requests[0]["body"]
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertIsNone(model.supports_tools)      # 未降级

    def test_missing_api_key_raises_config_error(self):
        with self.assertRaises(ConfigError):
            HttpChatModel(Config(api_key="", base_url="https://x.invalid"),
                          transport=FakeTransport([]), sleep=lambda _: None)


class HttpChatModelParsingTest(unittest.TestCase):
    """响应解析：工具调用、脏结构。"""

    def test_tool_calls_are_parsed(self):
        payload = ok_payload(content="", tool_calls=[
            {"id": "call_1", "function": {"name": "list_files", "arguments": '{"pattern": "*.py"}'}},
            {"id": "call_2", "function": {"name": "read_file", "arguments": "不是 JSON"}},
        ])
        model, _ = make_model(FakeTransport([(200, payload, {})]))

        response = model.chat([{"role": "user", "content": "检查"}])

        self.assertTrue(response.wants_tools)
        self.assertEqual(len(response.tool_calls), 2)
        first, second = response.tool_calls
        self.assertEqual(first.name, "list_files")
        self.assertEqual(first.arguments, {"pattern": "*.py"})
        self.assertEqual(first.parse_error, "")
        self.assertEqual(second.name, "read_file")
        self.assertEqual(second.arguments, {})
        self.assertTrue(second.parse_error)
        self.assertEqual(second.raw_arguments, "不是 JSON")

    def test_tool_call_arguments_as_dict(self):
        payload = ok_payload(content="", tool_calls=[
            {"id": "c1", "function": {"name": "n", "arguments": {"a": 1}}},
        ])
        model, _ = make_model(FakeTransport([(200, payload, {})]))
        call = model.chat([{"role": "user", "content": "x"}]).tool_calls[0]
        self.assertEqual(call.arguments, {"a": 1})
        self.assertEqual(call.raw_arguments, '{"a": 1}')

    def test_tool_call_missing_id_and_name(self):
        payload = ok_payload(content="", tool_calls=[{"function": {}}])
        model, _ = make_model(FakeTransport([(200, payload, {})]))
        call = model.chat([{"role": "user", "content": "x"}]).tool_calls[0]
        self.assertEqual(call.id, "call_0")
        self.assertEqual(call.name, "")

    def test_content_is_stripped(self):
        model, _ = make_model(FakeTransport([(200, ok_payload("  hi  "), {})]))
        self.assertEqual(model.chat([{"role": "user", "content": "x"}]).content, "hi")

    def test_non_dict_response_raises_response_error(self):
        model, slept = make_model(FakeTransport([(200, ["not", "a", "dict"], {})]))
        with self.assertRaises(LLMResponseError):
            model.chat([{"role": "user", "content": "x"}])
        self.assertEqual(model.stats.failures, 1)
        self.assertEqual(model.stats.retries, 0)
        self.assertEqual(slept, [])

    def test_missing_choices_raises_response_error(self):
        model, _ = make_model(FakeTransport([(200, {"id": "x"}, {})]))
        with self.assertRaises(LLMResponseError):
            model.chat([{"role": "user", "content": "x"}])

    def test_empty_choices_raises_response_error(self):
        model, _ = make_model(FakeTransport([(200, {"choices": []}, {})]))
        with self.assertRaises(LLMResponseError):
            model.chat([{"role": "user", "content": "x"}])

    def test_error_field_in_200_body_raises(self):
        model, slept = make_model(FakeTransport([(200, {"error": {"message": "额度不足"}}, {})]))
        with self.assertRaises(LLMError) as ctx:
            model.chat([{"role": "user", "content": "x"}])
        self.assertIn("额度不足", str(ctx.exception))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(model.stats.failures, 1)
        self.assertEqual(slept, [])


class HttpChatModelRetryTest(unittest.TestCase):
    """重试、退避与错误分类。"""

    def test_rate_limit_then_success(self):
        transport = FakeTransport([
            (429, {"error": {"message": "rate limited"}}, {"Retry-After": "2"}),
            (200, ok_payload("终于成功"), {}),
        ])
        model, slept = make_model(transport)

        response = model.chat([{"role": "user", "content": "x"}])

        self.assertEqual(response.content, "终于成功")
        self.assertEqual(model.stats.retries, 1)
        self.assertEqual(model.stats.calls, 1)
        self.assertEqual(model.stats.failures, 0)
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0.0)
        self.assertEqual(len(transport.requests), 2)

    def test_server_error_retries_until_exhausted(self):
        transport = FakeTransport([(500, {"error": {"message": "boom"}}, {})])
        model, slept = make_model(transport, max_retries=2)

        with self.assertRaises(LLMServerError):
            model.chat([{"role": "user", "content": "x"}])

        self.assertEqual(model.stats.retries, 2)
        self.assertEqual(model.stats.failures, 1)
        self.assertEqual(len(slept), 2)
        self.assertEqual(len(transport.requests), 3)

    def test_zero_max_retries_fails_immediately(self):
        transport = FakeTransport([(503, {"error": {"message": "unavailable"}}, {})])
        model, slept = make_model(transport, max_retries=0)
        with self.assertRaises(LLMServerError):
            model.chat([{"role": "user", "content": "x"}])
        self.assertEqual(model.stats.retries, 0)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(slept, [])

    def test_auth_error_is_not_retried(self):
        for status in (401, 403):
            with self.subTest(status=status):
                transport = FakeTransport([(status, {"error": {"message": "bad key"}}, {})])
                model, slept = make_model(transport)
                with self.assertRaises(LLMAuthError) as ctx:
                    model.chat([{"role": "user", "content": "x"}])
                self.assertEqual(ctx.exception.status, status)
                self.assertFalse(ctx.exception.retryable)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(model.stats.retries, 0)
                self.assertEqual(model.stats.failures, 1)
                self.assertEqual(slept, [])

    def test_bad_request_without_tools_is_not_retried(self):
        transport = FakeTransport([(400, {"error": {"message": "model not found"}}, {})])
        model, slept = make_model(transport)
        with self.assertRaises(LLMBadRequestError):
            model.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(slept, [])

    def test_network_error_is_retried(self):
        transport = FakeTransport([LLMNetworkError("连接被拒"), (200, ok_payload(), {})])
        model, slept = make_model(transport)
        response = model.chat([{"role": "user", "content": "x"}])
        self.assertEqual(response.content, "hello")
        self.assertEqual(model.stats.retries, 1)
        self.assertEqual(len(slept), 1)

    def test_unexpected_exception_is_retried_then_wrapped(self):
        transport = FakeTransport([ValueError("传输层写错了")])
        model, slept = make_model(transport, max_retries=1)
        with self.assertRaises(LLMError) as ctx:
            model.chat([{"role": "user", "content": "x"}])
        self.assertIn("ValueError", str(ctx.exception))
        self.assertEqual(model.stats.retries, 1)
        self.assertEqual(len(slept), 1)

    def test_unexpected_exception_then_success(self):
        transport = FakeTransport([ValueError("偶发"), (200, ok_payload("ok"), {})])
        model, _ = make_model(transport)
        self.assertEqual(model.chat([{"role": "user", "content": "x"}]).content, "ok")
        self.assertEqual(model.stats.retries, 1)

    def test_backoff_is_bounded(self):
        model, _ = make_model(FakeTransport([]), retry_base_delay=1.0)
        self.assertLessEqual(model._backoff(1), 20.0)
        self.assertLessEqual(model._backoff(10), 20.0)
        self.assertGreaterEqual(model._backoff(1), 1.0)


class ErrorClassificationTest(unittest.TestCase):
    """错误分类表（status → 异常类型）。"""

    def setUp(self):
        self.model, _ = make_model(FakeTransport([]))

    def test_status_mapping(self):
        cases = {
            401: LLMAuthError,
            403: LLMAuthError,
            429: LLMRateLimitError,
            500: LLMServerError,
            503: LLMServerError,
            400: LLMBadRequestError,
            404: LLMBadRequestError,
            422: LLMBadRequestError,
            302: LLMError,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                error = self.model._error_for(status, {"error": {"message": "x"}}, {})
                self.assertIsInstance(error, expected)
                self.assertEqual(error.status, status)

    def test_retryable_flags(self):
        self.assertTrue(self.model._error_for(429, {}, {}).retryable)
        self.assertTrue(self.model._error_for(500, {}, {}).retryable)
        self.assertFalse(self.model._error_for(401, {}, {}).retryable)
        self.assertFalse(self.model._error_for(400, {}, {}).retryable)

    def test_retry_after_header_is_reported(self):
        error = self.model._error_for(429, {"error": {"message": "slow down"}},
                                      {"Retry-After": "3"})
        self.assertIn("3s", str(error))

    def test_error_message_fallback(self):
        error = self.model._error_for(500, {"_raw": "gateway timeout"}, {})
        self.assertIn("gateway timeout", str(error))

    def test_string_error_body(self):
        error = self.model._error_for(500, {"error": "server exploded"}, {})
        self.assertIn("server exploded", str(error))


class ToolsDowngradeTest(unittest.TestCase):
    """服务端不支持 tools 时自动降级为 JSON 文本协议。"""

    TOOLS = [{"type": "function", "function": {"name": "read_file"}}]

    def test_downgrade_on_bad_request_mentioning_tools(self):
        transport = FakeTransport([
            (400, {"error": {"message": "This model does not support tools"}}, {}),
            (200, ok_payload("降级成功"), {}),
        ])
        model, slept = make_model(transport)

        response = model.chat([{"role": "user", "content": "x"}], tools=self.TOOLS)

        self.assertEqual(response.content, "降级成功")
        self.assertFalse(model.supports_tools)
        self.assertEqual(len(transport.requests), 2)
        self.assertIn("tools", transport.requests[0]["body"])
        self.assertNotIn("tools", transport.requests[1]["body"])
        self.assertNotIn("tool_choice", transport.requests[1]["body"])
        self.assertEqual(model.stats.retries, 0)      # 降级不记为重试
        self.assertEqual(model.stats.failures, 0)
        self.assertEqual(slept, [])                   # 降级无需退避等待

    def test_no_tools_sent_after_downgrade(self):
        transport = FakeTransport([
            (400, {"error": {"message": "function calling unsupported"}}, {}),
            (200, ok_payload(), {}),
            (200, ok_payload(), {}),
        ])
        model, _ = make_model(transport)
        model.chat([{"role": "user", "content": "x"}], tools=self.TOOLS)
        model.chat([{"role": "user", "content": "y"}], tools=self.TOOLS)
        self.assertNotIn("tools", transport.requests[2]["body"])
        self.assertFalse(model.supports_tools)

    def test_other_bad_request_is_not_treated_as_downgrade(self):
        transport = FakeTransport([(400, {"error": {"message": "invalid temperature"}}, {})])
        model, _ = make_model(transport)
        with self.assertRaises(LLMBadRequestError):
            model.chat([{"role": "user", "content": "x"}], tools=self.TOOLS)
        self.assertIsNone(model.supports_tools)

    def test_downgrade_only_when_tools_requested(self):
        transport = FakeTransport([
            (400, {"error": {"message": "tools are not supported"}}, {}),
        ])
        model, _ = make_model(transport)
        with self.assertRaises(LLMBadRequestError):
            model.chat([{"role": "user", "content": "x"}])   # 没带 tools 就不降级
        self.assertIsNone(model.supports_tools)


class ScriptedChatModelTest(unittest.TestCase):
    """脚本模型：顺序返回、用尽复用、responder。"""

    def test_script_order_and_last_reuse(self):
        model = ScriptedChatModel([
            LLMResponse(content="first"),
            LLMResponse(content="second"),
        ])
        messages = [{"role": "user", "content": "x"}]
        self.assertEqual(model.chat(messages).content, "first")
        self.assertEqual(model.chat(messages).content, "second")
        self.assertEqual(model.chat(messages).content, "second")
        self.assertEqual(model.stats.calls, 3)
        self.assertEqual(model.seen_messages, messages)

    def test_empty_script_returns_empty_response(self):
        model = ScriptedChatModel()
        response = model.chat([{"role": "user", "content": "x"}])
        self.assertEqual(response.content, "")
        self.assertEqual(model.stats.calls, 1)

    def test_responder_mode(self):
        received: list = []

        def responder(messages):
            received.append(messages)
            return LLMResponse(content="来自 responder", finish_reason="stop")

        model = ScriptedChatModel(responder=responder)
        response = model.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "来自 responder")
        self.assertEqual(received[0][0]["content"], "hi")

    def test_responder_returning_string_is_coerced(self):
        model = ScriptedChatModel(responder=lambda messages: "纯文本")
        self.assertEqual(model.chat([{"role": "user", "content": "x"}]).content, "纯文本")

    def test_supports_tools_and_extra_kwargs(self):
        model = ScriptedChatModel(supports_tools=False)
        self.assertFalse(model.supports_tools)
        self.assertTrue(ScriptedChatModel().supports_tools)
        # 额外 kwargs 必须被忽略而不是报错
        model.chat([{"role": "user", "content": "x"}], temperature=0.1, max_tokens=5, tools=[])


class RuleChatModelTest(unittest.TestCase):
    """离线规则模型：关键词 → 内置模板。"""

    def setUp(self):
        self.model = RuleChatModel()
        self.messages = [{"role": "user", "content": ""}]

    def _data(self, prompt: str) -> dict:
        self.messages = [{"role": "user", "content": prompt}]
        response = self.model.chat(self.messages)
        payload = response.json()
        self.assertIsInstance(payload, dict, prompt)
        return payload

    def test_flags(self):
        self.assertTrue(self.model.is_deterministic)
        self.assertFalse(self.model.supports_tools)

    def test_review_template(self):
        payload = self._data("review 审查这段代码")
        self.assertIn("issues", payload["data"])
        self.assertTrue(payload["data"]["notes"])

    def test_code_template(self):
        payload = self._data("code 编码实现一个函数")
        data = payload["data"]
        self.assertIn("app/service.py", data["files"])
        self.assertIn("tests/test_service.py", data["tests"])
        self.assertIsInstance(data["plan"], list)
        self.assertTrue(data["plan"])

    def test_fix_template(self):
        payload = self._data("fix 修复补丁")
        patches = payload["data"]["patches"]
        self.assertEqual(len(patches), 2)
        self.assertEqual({item["issue_id"] for item in patches}, {"SEC001", "BUG002"})

    def test_requirement_template(self):
        payload = self._data("requirement 需求分析")
        self.assertTrue(payload["data"]["functional"])
        self.assertTrue(payload["data"]["acceptance"])

    def test_design_template(self):
        payload = self._data("design 架构设计")
        self.assertTrue(payload["data"]["interface"])
        self.assertIn("app/service.py", payload["data"]["files"])

    def test_outline_template(self):
        payload = self._data("outline 大纲")
        self.assertEqual(len(payload["data"]["outline"]), 5)

    def test_document_template(self):
        payload = self._data("document 文档")
        self.assertIn("README.md", payload["data"]["documents"])

    def test_proofread_template(self):
        payload = self._data("proofread 审校")
        self.assertEqual(payload["decision"], "pass")
        self.assertEqual(payload["data"]["issues"], [])

    def test_test_template(self):
        payload = self._data("unittest 测试用例")
        self.assertIn("tests/test_service.py", payload["data"]["tests"])

    def test_unmatched_prompt_falls_back(self):
        payload = self._data("请随便聊两句天气")
        self.assertEqual(payload["decision"], "continue")
        self.assertIn("summary", payload)

    def test_review_wins_when_multiple_keywords(self):
        payload = self._data("review 之后再看 fix 修复")
        self.assertNotIn("patches", payload["data"])

    def test_stats_and_calls_recorded(self):
        self.model.chat([{"role": "user", "content": "code 编码"}])
        self.model.chat([{"role": "user", "content": "code 编码"}])
        self.assertEqual(self.model.stats.calls, 2)
        self.assertEqual(len(self.model.calls), 2)
        self.assertGreater(self.model.stats.prompt_tokens, 0)

    def test_persona_and_note_are_accepted(self):
        model = RuleChatModel(persona="reviewer", note="自定义说明")
        payload = model.chat([{"role": "user", "content": "随便"}]).json()
        self.assertIn("自定义说明", payload["summary"])


class BuildModelTest(unittest.TestCase):
    """工厂：离线 → 规则模型；在线 + scripted → 脚本模型。"""

    def test_offline_returns_rule_model(self):
        model = build_model(Config(mode="offline"))
        self.assertIsInstance(model, RuleChatModel)

    def test_offline_override_wins(self):
        model = build_model(Config(mode="online", api_key="sk-x"), offline=True)
        self.assertIsInstance(model, RuleChatModel)

    def test_scripted_transport(self):
        model = build_model(Config(mode="online", api_key="sk-x", transport="scripted"))
        self.assertIsInstance(model, ScriptedChatModel)

    def test_online_returns_http_model(self):
        model = build_model(Config(mode="online", api_key="sk-x"))
        self.assertIsInstance(model, HttpChatModel)

    def test_offline_never_needs_key(self):
        model = build_model(Config(mode="offline", api_key=""))
        self.assertTrue(getattr(model, "is_deterministic", False))


if __name__ == "__main__":
    unittest.main()
