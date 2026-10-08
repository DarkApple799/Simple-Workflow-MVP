"""Agent 循环测试（完全离线、无网络）。

用脚本化的 FakeClient 模拟模型的多轮决策，覆盖：
    * 正常链路：工具调用 → 提交结论 → 规则与模型发现合并；
    * 步数上限：调查不收敛时仍要给出报告；
    * 模型异常：自动降级为规则报告；
    * JSON 协议降级：服务端不支持 tools 时的备选路径。
"""

from __future__ import annotations

import os
import unittest

from cra.agent import CodeReviewAgent
from cra.config import Config
from cra.errors import LLMAuthError
from cra.llm import LLMResponse, LLMStats, ToolCall
from tests.support import TempDirTestCase


class FakeClient:
    """按脚本返回响应，接口与 LLMClient 保持一致。"""

    def __init__(self, script: list, *, supports_tools: bool | None = True) -> None:
        self.script = list(script)
        self.supports_tools = supports_tools
        self.stats = LLMStats()
        self.sent_messages: list[list[dict]] = []
        self.sent_tools: list[list | None] = []

    def chat(self, messages, *, tools=None, response_format=None, temperature=None, max_tokens=None):
        self.sent_messages.append(list(messages))
        self.sent_tools.append(tools)
        self.stats.calls += 1
        self.stats.prompt_tokens += 10
        self.stats.completion_tokens += 5
        item = self.script.pop(0) if self.script else LLMResponse(content="")
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name: str, arguments: dict, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments="{}")


SUBMIT_ARGUMENTS = {
    "summary": "整体可用，但异常处理与密钥管理存在严重问题。",
    "verdict": "request_changes",
    "findings": [
        {
            "title": "硬编码密钥",
            "file": "bad.py",
            "line": 3,
            "severity": "critical",
            "category": "security",
            "detail": "密钥直接写在源码里",
            "suggestion": "改用环境变量",
        }
    ],
}


class AgentTestBase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.root, "bad.py"), "w", encoding="utf-8") as handle:
            handle.write('import os\n\nAPI_KEY = "sk-hardcoded-123456"\n\n\ndef f(x):\n    return x == None\n')
        self.cfg = Config(api_key="test-key", model="test-model", max_steps=4)

    def make_agent(self, client) -> CodeReviewAgent:
        return CodeReviewAgent(self.cfg, client=client)


class TestHappyPath(AgentTestBase):
    def test_tool_call_then_submit(self) -> None:
        client = FakeClient(
            [
                LLMResponse(tool_calls=[tool_call("read_file", {"path": "bad.py"})]),
                LLMResponse(tool_calls=[tool_call("submit_review", SUBMIT_ARGUMENTS, "c2")]),
            ]
        )
        scan_result, outcome = self.make_agent(client).review(self.root)

        self.assertEqual(outcome.mode, "online")
        self.assertFalse(outcome.degraded)
        self.assertEqual(outcome.verdict, "request_changes")
        self.assertEqual(outcome.steps, 2)
        self.assertGreaterEqual(outcome.tool_calls, 2)
        self.assertIn("异常处理", outcome.summary)

        rules = {finding.rule_id for finding in outcome.findings}
        self.assertIn("SEC001", rules, "确定性规则发现应保留")
        self.assertTrue(
            any("agent" in finding.sources for finding in outcome.findings),
            "模型发现应被合并进最终结果",
        )
        self.assertEqual([trace.tool for trace in outcome.trace][:2], ["read_file", "submit_review"])

    def test_second_turn_receives_tool_observation(self) -> None:
        client = FakeClient(
            [
                LLMResponse(tool_calls=[tool_call("read_file", {"path": "bad.py"})]),
                LLMResponse(tool_calls=[tool_call("submit_review", SUBMIT_ARGUMENTS, "c2")]),
            ]
        )
        self.make_agent(client).review(self.root)
        second_turn = client.sent_messages[1]
        roles = [message["role"] for message in second_turn]
        self.assertIn("tool", roles, "工具观察结果必须回灌给模型")
        tool_message = [m for m in second_turn if m["role"] == "tool"][0]
        self.assertIn("API_KEY", tool_message["content"])

    def test_bad_tool_arguments_do_not_crash(self) -> None:
        client = FakeClient(
            [
                LLMResponse(tool_calls=[tool_call("read_file", {"wrong": "arg"})]),
                LLMResponse(tool_calls=[tool_call("submit_review", SUBMIT_ARGUMENTS, "c2")]),
            ]
        )
        _, outcome = self.make_agent(client).review(self.root)
        self.assertFalse(outcome.trace[0].ok)
        self.assertIn("参数", outcome.trace[0].error)


class TestDegradation(AgentTestBase):
    def test_max_steps_reached(self) -> None:
        cfg = Config(api_key="test-key", max_steps=3)
        client = FakeClient([LLMResponse(tool_calls=[tool_call("list_files", {})]) for _ in range(5)])
        scan_result, outcome = CodeReviewAgent(cfg, client=client).review(self.root)

        self.assertEqual(outcome.steps, 3)
        self.assertTrue(outcome.degraded)
        self.assertTrue(any("步数" in note for note in outcome.notes))
        self.assertEqual(len(outcome.findings), len(scan_result.findings))

    def test_llm_failure_falls_back_to_rules(self) -> None:
        client = FakeClient([LLMAuthError("鉴权失败（HTTP 401）：invalid key")])
        scan_result, outcome = self.make_agent(client).review(self.root)

        self.assertEqual(outcome.mode, "degraded")
        self.assertTrue(outcome.degraded)
        self.assertIn("鉴权", outcome.degraded_reason)
        self.assertEqual(len(outcome.findings), len(scan_result.findings))
        self.assertTrue(outcome.summary)

    def test_no_api_key_uses_rules_only(self) -> None:
        cfg = Config(api_key="")
        agent = CodeReviewAgent(cfg)  # 不应尝试创建客户端
        scan_result, outcome = agent.review(self.root)
        self.assertTrue(outcome.degraded)
        self.assertEqual(len(outcome.findings), len(scan_result.findings))

    def test_offline_flag_sets_offline_mode(self) -> None:
        cfg = Config(api_key="test-key", offline=True)
        _, outcome = CodeReviewAgent(cfg).review(self.root)
        self.assertEqual(outcome.mode, "offline")

    def test_duplicate_tool_calls_are_skipped(self) -> None:
        """模型原地打转时不应把步数耗光，也不应重复执行同一调用。"""
        cfg = Config(api_key="test-key", max_steps=5)
        client = FakeClient([LLMResponse(tool_calls=[tool_call("list_files", {"path": "."})]) for _ in range(5)])
        _, outcome = CodeReviewAgent(cfg, client=client).review(self.root)
        self.assertEqual(len(outcome.trace), 1, "完全相同的调用只应真正执行一次")
        self.assertTrue(any("重复" in note for note in outcome.notes))

    def test_unparsable_answer_still_produces_findings(self) -> None:
        client = FakeClient([LLMResponse(content="我觉得这段代码不太行。") for _ in range(4)])
        scan_result, outcome = CodeReviewAgent(
            Config(api_key="test-key", max_steps=2), client=client
        ).review(self.root)
        self.assertTrue(outcome.degraded)
        self.assertEqual(len(outcome.findings), len(scan_result.findings))
        self.assertIn("不太行", outcome.raw_answer)


class TestJsonProtocol(AgentTestBase):
    def test_json_tool_then_final(self) -> None:
        client = FakeClient(
            [
                LLMResponse(content='{"action": "tool", "tool": "read_file", "arguments": {"path": "bad.py"}}'),
                LLMResponse(
                    content=(
                        "```json\n"
                        '{"action": "final", "summary": "存在硬编码密钥。", "verdict": "block",'
                        ' "findings": [{"title": "硬编码密钥", "file": "bad.py", "line": 3,'
                        ' "severity": "critical", "category": "security", "detail": "明文密钥"}]}'
                        "\n```"
                    )
                ),
            ],
            supports_tools=False,
        )
        _, outcome = self.make_agent(client).review(self.root)

        self.assertEqual(outcome.mode, "online")
        self.assertFalse(outcome.degraded)
        self.assertEqual(outcome.verdict, "block")
        self.assertTrue(any("agent" in finding.sources for finding in outcome.findings))
        self.assertIsNone(client.sent_tools[0], "JSON 协议下不应传 tools")

    def test_json_protocol_observation_is_fed_back(self) -> None:
        client = FakeClient(
            [
                LLMResponse(content='{"action": "tool", "tool": "list_files", "arguments": {"path": "."}}'),
                LLMResponse(content='{"action": "final", "summary": "ok", "findings": []}'),
            ],
            supports_tools=False,
        )
        self.make_agent(client).review(self.root)
        self.assertTrue(
            any("bad.py" in message.get("content", "") for message in client.sent_messages[1]),
            "工具结果应作为 user 消息回灌",
        )


class TestSingleFileMode(AgentTestBase):
    """回归测试：`review(only_file=...)` 时，Agent 不得读取目标文件之外的内容。"""

    def setUp(self) -> None:
        super().setUp()
        self.other = os.path.join(self.root, "other.txt")
        with open(self.other, "w", encoding="utf-8") as handle:
            handle.write("SECRET=do-not-read-me\n")

    def test_neighbour_file_cannot_be_read(self) -> None:
        client = FakeClient(
            [
                LLMResponse(tool_calls=[tool_call("read_file", {"path": "other.txt"})]),
                LLMResponse(tool_calls=[tool_call("submit_review", SUBMIT_ARGUMENTS, "c2")]),
            ]
        )
        target = os.path.join(self.root, "bad.py")
        _, outcome = self.make_agent(client).review(self.root, only_file=target)

        self.assertFalse(outcome.trace[0].ok)
        self.assertIn("单个文件", outcome.trace[0].error)
        observation = [m for m in client.sent_messages[1] if m["role"] == "tool"][0]
        self.assertIn("单个文件", observation["content"])
        self.assertNotIn("do-not-read-me", observation["content"])

    def test_target_file_is_readable(self) -> None:
        client = FakeClient(
            [
                LLMResponse(tool_calls=[tool_call("read_file", {"path": "bad.py"})]),
                LLMResponse(tool_calls=[tool_call("submit_review", SUBMIT_ARGUMENTS, "c2")]),
            ]
        )
        _, outcome = self.make_agent(client).review(self.root, only_file=os.path.join(self.root, "bad.py"))
        self.assertTrue(outcome.trace[0].ok)
        self.assertEqual(outcome.trace[0].tool, "read_file")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
