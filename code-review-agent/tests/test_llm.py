"""LLM 客户端测试：重试、降级、错误分类、响应解析。

全部使用注入的假传输层，因此**不需要联网、不需要 API Key**，
这也正是把 transport 设计成可注入的原因。
"""

from __future__ import annotations

import json
import unittest

from cra.config import Config
from cra.errors import LLMAuthError, LLMBadRequestError, LLMResponseError, LLMServerError
from cra.llm import LLMClient, Usage


def ok_response(content: str = "hi", tool_calls: list | None = None, usage: dict | None = None) -> tuple:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    payload = {
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return 200, payload, {}


def http_error(status: int, message: str, headers: dict | None = None) -> tuple:
    return status, {"error": {"message": message}}, headers or {}


def make_client(responses: list, *, cfg: Config | None = None):
    """构造带假传输层的客户端，同时记录请求体与 sleep 调用。"""
    config = cfg or Config(api_key="test-key", max_retries=3, retry_base_delay=0.01)
    sent: list[dict] = []
    sleeps: list[float] = []

    def transport(url: str, headers: dict, body: bytes, timeout: float):
        sent.append(json.loads(body.decode("utf-8")))
        index = min(len(sent) - 1, len(responses) - 1)
        item = responses[index]
        if isinstance(item, Exception):
            raise item
        return item

    client = LLMClient(config, transport=transport, sleep=sleeps.append)
    return client, sent, sleeps


class TestParsing(unittest.TestCase):
    def test_plain_content_and_usage(self) -> None:
        client, sent, _ = make_client([ok_response("你好")])
        response = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "你好")
        self.assertEqual(response.usage, Usage(10, 5, 15))
        self.assertEqual(sent[0]["model"], client.model)

    def test_tool_call_arguments_parsed(self) -> None:
        call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
        client, _, _ = make_client([ok_response("", [call])])
        response = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(response.tool_calls[0].parse_error, "")

    def test_malformed_tool_arguments_are_reported(self) -> None:
        call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{bad json"}}
        client, _, _ = make_client([ok_response("", [call])])
        response = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertTrue(response.tool_calls[0].parse_error)
        self.assertEqual(response.tool_calls[0].arguments, {})

    def test_missing_choices_raises(self) -> None:
        client, _, _ = make_client([(200, {"nope": True}, {})])
        with self.assertRaises(LLMResponseError):
            client.chat([{"role": "user", "content": "hi"}])


class TestRetryAndErrors(unittest.TestCase):
    def test_retries_on_server_error_then_succeeds(self) -> None:
        client, sent, sleeps = make_client([http_error(500, "boom"), ok_response("ok")])
        response = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "ok")
        self.assertEqual(client.stats.retries, 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(sleeps), 1)

    def test_rate_limit_is_retried(self) -> None:
        client, sent, _ = make_client([http_error(429, "slow down", {"Retry-After": "1"}), ok_response()])
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.stats.retries, 1)

    def test_auth_error_is_not_retried(self) -> None:
        client, sent, _ = make_client([http_error(401, "invalid api key")])
        with self.assertRaises(LLMAuthError):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(sent), 1)
        self.assertEqual(client.stats.retries, 0)

    def test_network_exception_is_retried_then_raises(self) -> None:
        client, _, sleeps = make_client([OSError("connection reset")])
        with self.assertRaises(Exception):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(sleeps), client.cfg.max_retries)

    def test_error_classification(self) -> None:
        client, _, _ = make_client([http_error(400, "bad model")])
        with self.assertRaises(LLMBadRequestError):
            client.chat([{"role": "user", "content": "hi"}])
        client2, _, _ = make_client([http_error(503, "unavailable")])
        with self.assertRaises(LLMServerError):
            client2.chat([{"role": "user", "content": "hi"}])


class TestToolSupportDegradation(unittest.TestCase):
    def test_server_without_tools_support_degrades_automatically(self) -> None:
        responses = [http_error(400, "this model does not support tools"), ok_response("无工具的答复")]
        client, sent, _ = make_client(responses)
        response = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertFalse(client.supports_tools)
        self.assertEqual(response.content, "无工具的答复")
        self.assertIn("tools", sent[0])
        self.assertNotIn("tools", sent[1], "降级后不应继续发送 tools 字段")

    def test_tools_are_not_sent_after_degradation(self) -> None:
        client, sent, _ = make_client([http_error(400, "tools unsupported"), ok_response("a"), ok_response("b")])
        client.chat([{"role": "user", "content": "1"}], tools=[{"type": "function"}])
        client.chat([{"role": "user", "content": "2"}], tools=[{"type": "function"}])
        self.assertNotIn("tools", sent[-1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
