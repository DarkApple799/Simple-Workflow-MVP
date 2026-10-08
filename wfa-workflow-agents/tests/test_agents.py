"""`wfa/agents/` 单元测试：注册表、上下文工具、四个内置 Agent 与 Homework1 适配器。

覆盖点（对应 CONTRACT 第 6、7、9 节）：
    1. ``AgentRegistry`` / ``default_registry``：名字齐全（coder/requirements/designer/writer/
       documenter/planner/proofreader/reviewer/security_reviewer/maintainability_reviewer/
       fixer/legacy_reviewer）、缺失抛 ``ConfigError``、``describe()`` 结构；
    2. ``base`` 上下文工具：``config_value`` 优先级（RunConfig → 注入的 Config → extra → 默认）、
       ``state_value`` / ``agent_task`` / ``workspace_root`` / ``safe_workspace_path``（越界抛
       ``PathSecurityError``）/ 工作区读写 / ``publish_message`` / ``input_results`` / ``known_results``；
    3. Issue 工具链：``normalize_issue``（中英字段名、非法 severity/line 归一）、
       ``merge_issues``（按位置去重 + 取更严重定级 + 唯一 id）、``count_severities``（固定 5 键）、
       ``blocking_issues``、``verdict_for`` 判定表、``findings_from_text`` 脏文本提取；
    4. ``CodingAgent``（离线 RuleChatModel 与 model=None 两条路径）：``data`` 满足
       ``files/tests/plan/self_test`` 契约、写入工作区、补 tests/__init__.py、产物 code/plan.md；
       ``write_workspace=False`` 时只返回不落盘；
    5. ``ReviewAgent``：离线走 analysis 规则引擎（不调 LLM），``issues/counts/verdict/blocking``
       互相一致（verdict==verdict_for(counts)、blocking 恰为 high/critical 子集、
       counts 之和==issues 数）；focus 过滤（security / maintainability）；
       注入"真实模型"（is_deterministic=False）时验证工具循环与语义发现合并，模型抛异常时降级不崩；
    6. ``FixAgent``：确定性重写后工作区里 SEC001/BUG002 消失、无法自动修的规则进 remaining；
       无待修问题时返回空结构不崩；
    7. ``DocAgent``：五个角色的 ``data`` 契约（requirement/design/outline/documents/comments）、
       model=None 的确定性模板、非法角色抛 ``ConfigError``；
    8. ``LegacyReviewAgent``：无论 cra 是否可用都返回完整 review 契约；
       强制 ``cra`` 不可用时回退 wfa.analysis（``metrics["legacy"]=False`` + notes 说明原因 + 不崩）。

全部离线、确定性；唯一真实子进程是 CodingAgent 的本地自测（在工作区内跑一次 unittest）。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from tests.helpers import temp_workspace
from wfa.agents import (
    AgentRegistry,
    CodingAgent,
    DocAgent,
    FixAgent,
    LegacyReviewAgent,
    ReviewAgent,
    default_registry,
)
from wfa.agents import code_review as legacy_module
from wfa.agents.base import (
    agent_task,
    config_value,
    input_results,
    known_results,
    publish_message,
    read_workspace_file,
    result_dict,
    safe_workspace_path,
    state_value,
    workspace_root,
    write_workspace_file,
)
from wfa.agents.coding import clean_file_map, module_name
from wfa.agents.fix import MANUAL_RULES, render_fix_report
from wfa.agents.review import (
    blocking_issues,
    count_severities,
    extract_issues_from_payload,
    findings_from_text,
    issues_from_result,
    merge_issues,
    normalize_issue,
    verdict_for,
)
from wfa.bus import BROADCAST, MessageBus
from wfa.config import Config
from wfa.engine import RunConfig
from wfa.errors import ConfigError, PathSecurityError
from wfa.graph import AgentResult, Node, NodeContext
from wfa.model import LLMResponse, LLMStats, RuleChatModel, ScriptedChatModel, ToolCall
from wfa.state import Blackboard

#: 一段"故意写坏"的模块：硬编码密钥 + 静默吞异常 + 未使用导入 + 缺 docstring。
BAD_MODULE = '''"""示例业务模块（含故意缺陷）。"""

import json
import os

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
'''

EXPECTED_AGENT_NAMES = [
    "coder", "designer", "documenter", "fixer", "legacy_reviewer",
    "maintainability_reviewer", "planner", "proofreader", "requirements",
    "reviewer", "security_reviewer", "writer",
]


def make_ctx(ws, *, node_id="node", agent="", inputs=None, iteration=1, turn=1,
             config=None, state=None):
    """构造一个最小 NodeContext（工作区指向临时目录）。"""
    board = state if state is not None else Blackboard(workspace=ws)
    return NodeContext(node=Node(id=node_id, agent=agent), state=board, bus=board.bus,
                       inputs=dict(inputs or {}), iteration=iteration, turn=turn,
                       workspace=ws, config=config or RunConfig(workspace=ws), extras={})


def write_bad_module(ws, rel="app/service.py") -> str:
    """在工作区里放一个含缺陷的模块，返回相对路径。"""
    path = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(BAD_MODULE)
    return rel


class FakeLLM:
    """is_deterministic=False 的假模型（用于触发 ReviewAgent 的模型工具阶段）。"""

    is_deterministic = False

    def __init__(self, responses, supports_tools=True):
        self.supports_tools = supports_tools
        self.responses = list(responses)
        self.calls: list = []
        self.stats = LLMStats()

    def chat(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def llm_json(summary="模型结论", issues=None, verdict="pass") -> LLMResponse:
    """构造一条"最终答复"格式的 LLMResponse。"""
    return LLMResponse(content=json.dumps(
        {"summary": summary, "verdict": verdict, "issues": issues or []}, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class AgentRegistryTest(unittest.TestCase):
    """AgentRegistry 与 default_registry。"""

    def test_default_registry_names(self):
        registry = default_registry(RuleChatModel())
        self.assertEqual(registry.names(), EXPECTED_AGENT_NAMES)
        self.assertEqual(len(registry), 12)

    def test_expected_agents_are_present(self):
        registry = default_registry(RuleChatModel())
        for name in ("coder", "reviewer", "security_reviewer", "fixer",
                     "documenter", "writer", "planner", "proofreader", "legacy_reviewer"):
            with self.subTest(name=name):
                self.assertIn(name, registry)
                self.assertTrue(registry.get(name).name == name)

    def test_get_unknown_raises_config_error(self):
        registry = default_registry(RuleChatModel())
        with self.assertRaises(ConfigError) as ctx:
            registry.get("nope")
        self.assertIn("nope", str(ctx.exception))

    def test_describe_structure(self):
        items = default_registry(RuleChatModel()).describe()
        self.assertEqual([item["name"] for item in items], EXPECTED_AGENT_NAMES)
        for item in items:
            with self.subTest(name=item["name"]):
                self.assertEqual(set(item), {"name", "role", "describe"})
                self.assertTrue(item["role"])
                self.assertTrue(item["describe"])

    def test_register_rejects_empty_name(self):
        class Nameless:
            name = ""

        with self.assertRaises(ConfigError):
            AgentRegistry().register(Nameless())

    def test_default_registry_without_model(self):
        registry = default_registry(None)
        self.assertEqual(registry.names(), EXPECTED_AGENT_NAMES)

    def test_all_returns_instances(self):
        registry = default_registry(RuleChatModel())
        self.assertEqual([item.name for item in registry.all()], EXPECTED_AGENT_NAMES)


# --------------------------------------------------------------------------- #
# 上下文工具
# --------------------------------------------------------------------------- #
class BaseHelpersTest(unittest.TestCase):
    """base.py 的配置 / 状态 / 路径 / 消息工具。"""

    def test_config_value_precedence(self):
        config = Config(model="来自 Config", max_files=7)
        ctx_config = RunConfig(model="来自 RunConfig")

        class Ctx:
            pass

        ctx = Ctx()
        ctx.config = ctx_config
        # ctx.config 优先
        self.assertEqual(config_value(ctx, config, "model"), "来自 RunConfig")
        # ctx.config 没有的键回落到注入的 Config
        self.assertEqual(config_value(ctx, config, "max_files"), 7)
        # 都没有则用默认值
        self.assertEqual(config_value(ctx, config, "missing", "默认"), "默认")
        # extra 作为最后兜底
        config.extra = {"custom": 42}
        self.assertEqual(config_value(ctx, config, "custom"), 42)

    def test_config_value_keeps_falsy_values(self):
        ctx = type("Ctx", (), {"config": RunConfig()})()
        config = Config(stop_on_blocking=False, write_workspace=False)
        # RunConfig 上有 stop_on_blocking=True → ctx.config 优先
        self.assertTrue(config_value(ctx, config, "stop_on_blocking"))
        # RunConfig 没有 write_workspace → 落到注入的 Config，False 不被当成"未设置"
        self.assertFalse(config_value(ctx, config, "write_workspace", True))
        # ctx.config 里显式的 False 同样有效
        ctx2 = type("Ctx", (), {"config": RunConfig(stop_on_blocking=False)})()
        self.assertFalse(config_value(ctx2, config, "stop_on_blocking"))

    def test_state_value_and_agent_task(self):
        with temp_workspace("agent-state") as ws:
            board = Blackboard(workspace=ws)
            ctx = make_ctx(ws, state=board)
            self.assertEqual(state_value(ctx, "missing", "d"), "d")
            board.set("task", "黑板任务")
            self.assertEqual(agent_task(ctx, Config(extra={"task": "配置任务"})), "黑板任务")
            # 黑板为空时用 ctx.config（RunConfig）的 task；RunConfig 的 "" 视为"已设置"
            board.set("task", "")
            configured = make_ctx(ws, state=board,
                                  config=RunConfig(workspace=ws, task="RunConfig 任务"))
            self.assertEqual(agent_task(configured, Config(extra={"task": "配置任务"})),
                             "RunConfig 任务")
            self.assertEqual(agent_task(ctx, None), "")

    def test_workspace_root(self):
        with temp_workspace("agent-root") as ws:
            ctx = make_ctx(ws)
            self.assertEqual(workspace_root(ctx), os.path.abspath(ws))

    def test_safe_workspace_path(self):
        with temp_workspace("agent-safe") as ws:
            target = safe_workspace_path(ws, "a/b.txt")
            self.assertEqual(os.path.abspath(target), os.path.join(ws, "a", "b.txt"))
            with self.assertRaises(PathSecurityError):
                safe_workspace_path(ws, "../escape.txt")

    def test_write_and_read_workspace_file(self):
        with temp_workspace("agent-io") as ws:
            ctx = make_ctx(ws)
            written = write_workspace_file(ctx, "pkg/mod.py", "print(1)\n")
            self.assertTrue(os.path.isfile(written))
            self.assertEqual(read_workspace_file(ctx, "pkg/mod.py"), "print(1)\n")
            self.assertIsNone(read_workspace_file(ctx, "missing.py"))
            with self.assertRaises(PathSecurityError):
                write_workspace_file(ctx, "../evil.py", "x")

    def test_publish_message_broadcasts(self):
        with temp_workspace("agent-bus") as ws:
            board = Blackboard(workspace=ws)
            ctx = make_ctx(ws, node_id="coder", state=board)
            publish_message(ctx, "result", {"files": ["a.py"]})
            messages = board.bus.messages
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].kind, "result")
            self.assertEqual(messages[0].sender, "coder")
            self.assertEqual(messages[0].recipient, BROADCAST)
            self.assertEqual(messages[0].payload["files"], ["a.py"])

    def test_result_dict_and_input_results(self):
        result = AgentResult(kind="review", data={"issues": [1]})
        self.assertEqual(result_dict(result)["kind"], "review")
        self.assertEqual(result_dict({"a": 1}), {"a": 1})
        self.assertEqual(result_dict(None), {})

        with temp_workspace("agent-inputs") as ws:
            board = Blackboard(workspace=ws)
            ctx = make_ctx(ws, state=board,
                           inputs={"up": result, "dict_node": {"kind": "result"},
                                   "bad": 123})
            rows = input_results(ctx)
            self.assertEqual([node_id for node_id, _ in rows], ["up", "dict_node"])
            # known_results 追加黑板 results 键（{node_id: result_dict}）
            board.set("results", {"old": {"kind": "result", "data": {}}})
            self.assertEqual([node_id for node_id, _ in known_results(ctx)],
                             ["up", "dict_node", "old"])

    def test_agent_describe_and_repr(self):
        agent = CodingAgent(RuleChatModel())
        self.assertIn("coder", agent.describe())
        self.assertTrue(agent.describe())
        self.assertIn("CodingAgent", repr(agent))


# --------------------------------------------------------------------------- #
# Issue 工具链
# --------------------------------------------------------------------------- #
class IssueHelpersTest(unittest.TestCase):
    """normalize_issue / merge_issues / 统计 / verdict。"""

    def test_normalize_issue_english_keys(self):
        issue = normalize_issue({"title": "裸 except", "file": "a.py", "line": "12",
                                 "severity": "HIGH", "category": "Bug",
                                 "detail": "说明", "suggestion": "建议", "rule_id": "BUG001"})
        self.assertEqual(issue["severity"], "high")
        self.assertEqual(issue["category"], "bug")
        self.assertEqual(issue["line"], 12)
        self.assertEqual(issue["id"], "BUG001-a.py:12")
        self.assertEqual(issue["source"], "agent")
        self.assertEqual(issue["rule_id"], "BUG001")

    def test_normalize_issue_chinese_and_defaults(self):
        issue = normalize_issue({"问题": "问题标题", "文件": "b.py", "严重程度": "未知",
                                 "行号": "abc"})
        self.assertEqual(issue["title"], "问题标题")
        self.assertEqual(issue["file"], "b.py")
        self.assertEqual(issue["severity"], "medium")
        self.assertIsNone(issue["line"])
        self.assertTrue(issue["id"])

    def test_normalize_issue_requires_title(self):
        self.assertIsNone(normalize_issue({"file": "a.py"}))
        self.assertIsNone(normalize_issue("不是 dict"))
        self.assertIsNone(normalize_issue(None))

    def test_normalize_issue_keeps_explicit_id(self):
        issue = normalize_issue({"id": "custom-1", "title": "t", "file": "a.py", "line": 1,
                                 "severity": "low"})
        self.assertEqual(issue["id"], "custom-1")

    def test_merge_issues_dedup_and_severity(self):
        low = normalize_issue({"title": "同位置问题", "file": "a.py", "line": 3,
                               "severity": "low", "category": "bug", "detail": "短"})
        high = normalize_issue({"title": "同位置问题", "file": "a.py", "line": 3,
                                "severity": "high", "category": "bug",
                                "detail": "更长的描述文字", "source": "rule"})
        merged = merge_issues([low], [high])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["severity"], "high")
        self.assertEqual(merged[0]["detail"], "更长的描述文字")
        self.assertEqual(merged[0]["source"], "rule+agent")

    def test_merge_issues_sorts_by_severity(self):
        issues = merge_issues([
            {"title": "low", "file": "a.py", "line": 1, "severity": "low", "category": "bug"},
            {"title": "high", "file": "b.py", "line": 2, "severity": "high", "category": "bug"},
            {"title": "info", "file": "c.py", "line": 3, "severity": "info", "category": "style"},
        ])
        self.assertEqual([item["severity"] for item in issues], ["high", "low", "info"])

    def test_merge_issues_unique_ids(self):
        group = [
            {"id": "same", "title": "一", "file": "a.py", "line": 1, "severity": "low",
             "category": "bug"},
            {"id": "same", "title": "二", "file": "a.py", "line": 2, "severity": "low",
             "category": "bug"},
        ]
        merged = merge_issues(group)
        self.assertEqual(len({item["id"] for item in merged}), 2)

    def test_merge_issues_skips_garbage(self):
        self.assertEqual(merge_issues([None, "字符串", {}]), [])

    def test_counts_and_blocking(self):
        issues = merge_issues([
            {"title": "c", "file": "a.py", "line": 1, "severity": "critical", "category": "bug"},
            {"title": "h", "file": "a.py", "line": 2, "severity": "high", "category": "bug"},
            {"title": "m", "file": "a.py", "line": 3, "severity": "medium", "category": "bug"},
        ])
        counts = count_severities(issues)
        self.assertEqual(set(counts), {"critical", "high", "medium", "low", "info"})
        self.assertEqual(counts["critical"], 1)
        self.assertEqual(sum(counts.values()), len(issues))
        self.assertEqual([item["severity"] for item in blocking_issues(issues)],
                         ["critical", "high"])

    def test_verdict_table(self):
        self.assertEqual(verdict_for({"critical": 1, "high": 0, "medium": 0, "low": 0, "info": 0}),
                         "block")
        self.assertEqual(verdict_for({"critical": 0, "high": 2, "medium": 0, "low": 0, "info": 0}),
                         "request_changes")
        self.assertEqual(verdict_for({"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0}),
                         "pass_with_comments")
        self.assertEqual(verdict_for({"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}),
                         "pass")

    def test_findings_from_text(self):
        text = ('我先看看。\n```json\n{"summary": "结论", "issues": ['
                '{"title": "逻辑错误", "file": "a.py", "line": 3, "severity": "high",'
                ' "category": "bug", "detail": "d", "suggestion": "s"}]}\n```\n完毕。')
        found = findings_from_text(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "high")
        self.assertEqual(found[0]["title"], "逻辑错误")

    def test_findings_from_array_text(self):
        found = findings_from_text('[{"title": "t", "file": "a.py", "line": 1,'
                                   ' "severity": "low", "category": "style"}]')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["category"], "style")

    def test_findings_from_text_tolerates_garbage(self):
        self.assertEqual(findings_from_text(""), [])
        self.assertEqual(findings_from_text("没有 JSON"), [])

    def test_extract_issues_from_payload_nested(self):
        payload = {"data": {"issues": [{"title": "嵌套", "file": "a.py", "line": 1,
                                        "severity": "low", "category": "bug"}]}}
        self.assertEqual(len(extract_issues_from_payload(payload)), 1)
        self.assertEqual(len(extract_issues_from_payload([{"title": "t", "severity": "low"}])), 1)
        self.assertEqual(extract_issues_from_payload(None), [])

    def test_issues_from_result_with_children(self):
        result = AgentResult(kind="result", data={
            "children": {
                "a": {"kind": "review", "data": {"issues": [
                    {"title": "并行问题", "file": "a.py", "line": 1, "severity": "low",
                     "category": "bug"}]}},
            }
        })
        found = issues_from_result(result)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "并行问题")
        self.assertEqual(issues_from_result(None), [])


# --------------------------------------------------------------------------- #
# CodingAgent
# --------------------------------------------------------------------------- #
class CodingAgentTest(unittest.TestCase):
    """编码 Agent：离线模型与确定性模板两条路径。"""

    def test_offline_contract(self):
        with temp_workspace("agent-coder") as ws:
            ctx = make_ctx(ws, node_id="code", agent="coder")
            result = CodingAgent(RuleChatModel()).run(ctx)

        self.assertEqual(result.kind, "result")
        self.assertEqual(result.decision, "continue")
        self.assertIn("app/service.py", result.data["files"])
        self.assertIn("tests/test_service.py", result.data["tests"])
        self.assertIn("tests/__init__.py", result.data["tests"])
        self.assertTrue(result.data["plan"])
        self_test = result.data["self_test"]
        self.assertEqual(set(self_test), {"command", "returncode", "stdout"})
        self.assertIsInstance(self_test["returncode"], int)
        self.assertIn("code/plan.md", result.artifacts)
        self.assertEqual(result.metrics["files"], len(result.data["files"]))
        self.assertTrue(result.metrics["offline"])

    def test_offline_writes_workspace(self):
        with temp_workspace("agent-coder-write") as ws:
            ctx = make_ctx(ws, node_id="code", agent="coder")
            CodingAgent(RuleChatModel()).run(ctx)
            self.assertTrue(os.path.isfile(os.path.join(ws, "app", "service.py")))
            self.assertTrue(os.path.isfile(os.path.join(ws, "tests", "test_service.py")))
            self.assertTrue(os.path.isfile(os.path.join(ws, "tests", "__init__.py")))
            with open(os.path.join(ws, "app", "service.py"), "r", encoding="utf-8") as handle:
                self.assertIn("API_KEY", handle.read())

    def test_template_without_model_contains_demo_defects(self):
        with temp_workspace("agent-coder-none") as ws:
            ctx = make_ctx(ws, node_id="code", agent="coder")
            agent = CodingAgent(None)
            result = agent.run(ctx)
            self.assertIn("app/service.py", result.data["files"])
            self.assertTrue(result.metrics["offline"])
            hits = agent.scan_own_output(result.data["files"])
            self.assertIn("SEC001", hits)
            self.assertIn("BUG002", hits)

    def test_write_workspace_disabled(self):
        with temp_workspace("agent-coder-nowrite") as ws:
            ctx = make_ctx(ws, node_id="code", agent="coder")
            result = CodingAgent(None, config=Config(write_workspace=False)).run(ctx)
            self.assertEqual(os.listdir(ws), [])
            self.assertTrue(any("禁用工作区写入" in note for note in result.notes))
            self.assertEqual(result.data["self_test"]["returncode"], -1)

    def test_preserve_existing_on_later_iteration(self):
        with temp_workspace("agent-coder-iter2") as ws:
            board = Blackboard(workspace=ws)
            board.write_file("app/service.py", "# 修复后的内容\n")
            ctx = make_ctx(ws, node_id="code", agent="coder", iteration=2, state=board)
            result = CodingAgent(None).run(ctx)
            self.assertEqual(result.data["files"]["app/service.py"], "# 修复后的内容\n")
            self.assertTrue(any("保留工作区" in note for note in result.notes))

    def test_module_name_and_clean_file_map(self):
        self.assertEqual(module_name("app/service.py"), "app.service")
        self.assertEqual(module_name("service.py"), "service")
        self.assertEqual(module_name(""), "app.service")
        self.assertEqual(clean_file_map({"a.py": "x"}), {"a.py": "x"})
        self.assertEqual(clean_file_map({"a.py": ["x", "y"]}), {"a.py": "x\ny"})
        self.assertEqual(clean_file_map({"a.py": ""}), {})
        self.assertEqual(clean_file_map(["x"], default_path="a.py"), {"a.py": "x"})


# --------------------------------------------------------------------------- #
# ReviewAgent
# --------------------------------------------------------------------------- #
class ReviewAgentTest(unittest.TestCase):
    """审查 Agent：离线规则路径、focus 过滤与契约一致性。"""

    def _review(self, ws, *, focus="all", name="reviewer", config=None, model=None,
                iteration=0, inputs=None):
        board = Blackboard(workspace=ws)
        ctx = make_ctx(ws, node_id="review", agent=name, iteration=iteration,
                       state=board, config=config, inputs=inputs)
        agent = ReviewAgent(model if model is not None else RuleChatModel(),
                            config=config, focus=focus, name=name)
        return agent.run(ctx), board

    def test_offline_review_contract(self):
        with temp_workspace("agent-review") as ws:
            write_bad_module(ws)
            result, _board = self._review(ws)

        self.assertEqual(result.kind, "review")
        self.assertEqual(result.decision, "retry")           # 有阻断级问题
        data = result.data
        self.assertEqual(set(data), {"issues", "counts", "verdict", "blocking"})
        self.assertTrue(data["issues"])
        self.assertTrue(result.metrics["offline"])
        rule_ids = {issue["rule_id"] for issue in data["issues"]}
        self.assertIn("SEC001", rule_ids)
        self.assertIn("BUG002", rule_ids)
        self.assertIn("MAINT006", rule_ids)
        self.assertIn("review/reviewer.md", result.artifacts)

    def test_counts_blocking_verdict_are_consistent(self):
        with temp_workspace("agent-review-consistency") as ws:
            write_bad_module(ws)
            result, _ = self._review(ws)
        data = result.data
        issues, counts, blocking, verdict = (data["issues"], data["counts"],
                                            data["blocking"], data["verdict"])
        self.assertEqual(set(counts), {"critical", "high", "medium", "low", "info"})
        self.assertEqual(sum(counts.values()), len(issues))
        self.assertEqual(verdict, verdict_for(counts))
        self.assertEqual(len(blocking), counts["critical"] + counts["high"])
        self.assertTrue(all(item["severity"] in ("critical", "high") for item in blocking))
        for issue in issues:
            for key in ("id", "title", "file", "line", "severity", "category",
                        "detail", "suggestion", "source", "rule_id"):
                self.assertIn(key, issue)
        self.assertEqual(verdict, "request_changes")
        self.assertEqual(counts["high"], 2)                  # SEC001 + BUG002

    def test_summary_mentions_scan(self):
        with temp_workspace("agent-review-summary") as ws:
            write_bad_module(ws)
            result, _ = self._review(ws)
        self.assertIn("审查完成", result.summary)
        self.assertGreater(result.metrics["files"], 0)
        self.assertEqual(result.metrics["focus"], "all")

    def test_empty_workspace_is_clean(self):
        with temp_workspace("agent-review-empty") as ws:
            result, _ = self._review(ws)
        self.assertEqual(result.data["issues"], [])
        self.assertEqual(result.data["blocking"], [])
        self.assertEqual(result.data["verdict"], "pass")
        self.assertEqual(result.decision, "pass")
        self.assertEqual(sum(result.data["counts"].values()), 0)

    def test_focus_security_filters_categories(self):
        with temp_workspace("agent-review-sec") as ws:
            write_bad_module(ws)
            result, _ = self._review(ws, focus="security", name="security_reviewer")
        self.assertTrue(result.data["issues"])
        self.assertTrue(all(item["category"] == "security" for item in result.data["issues"]))
        self.assertIn("SEC001", {item["rule_id"] for item in result.data["issues"]})

    def test_focus_maintainability_filters_categories(self):
        with temp_workspace("agent-review-maint") as ws:
            write_bad_module(ws)
            result, _ = self._review(ws, focus="maintainability",
                                     name="maintainability_reviewer")
        allowed = {"maintainability", "style", "documentation", "performance"}
        self.assertTrue(all(item["category"] in allowed for item in result.data["issues"]))

    def test_upstream_review_issues_are_merged(self):
        upstream = AgentResult(kind="review", data={"issues": [
            {"title": "上游发现", "file": "virtual.py", "line": 5, "severity": "high",
             "category": "bug", "source": "agent"}]})
        with temp_workspace("agent-review-upstream") as ws:
            result, _ = self._review(ws, inputs={"upstream": upstream})
        titles = {item["title"] for item in result.data["issues"]}
        self.assertIn("上游发现", titles)
        self.assertEqual(result.metrics["upstream_comments"], 1)

    def test_missing_workspace_does_not_crash(self):
        with temp_workspace("agent-review-missing") as ws:
            missing = os.path.join(ws, "no-such-dir")
            board = Blackboard(workspace=missing)
            ctx = make_ctx(ws, node_id="review", state=board)
            ctx.workspace = missing
            result = ReviewAgent(RuleChatModel()).run(ctx)
        self.assertEqual(result.data["issues"], [])
        self.assertTrue(any("不存在" in note for note in result.notes))


class ReviewAgentToolStageTest(unittest.TestCase):
    """模型工具核实阶段：工具循环、语义发现合并、异常降级。"""

    def _run(self, ws, model, config=None, board=None):
        ctx = make_ctx(ws, node_id="review", agent="reviewer", state=board, config=config)
        return ReviewAgent(model, config=config).run(ctx)

    def test_tool_loop_then_structured_answer(self):
        tool_call = ToolCall(id="call_1", name="list_files", arguments={"pattern": "*.py"},
                             raw_arguments='{"pattern": "*.py"}')
        model = FakeLLM([
            LLMResponse(content="", tool_calls=[tool_call]),
            llm_json(issues=[{"title": "语义问题", "file": "app/service.py", "line": 8,
                              "severity": "medium", "category": "bug",
                              "detail": "模型补充的语义问题", "suggestion": "加校验"}]),
        ], supports_tools=True)
        with temp_workspace("agent-review-llm") as ws:
            write_bad_module(ws)
            result = self._run(ws, model)

        self.assertEqual(len(model.calls), 2)
        self.assertIsNotNone(model.calls[0]["tools"])         # 第一次带工具
        titles = {item["title"] for item in result.data["issues"]}
        self.assertIn("语义问题", titles)
        self.assertIn("SEC001", {item["rule_id"] for item in result.data["issues"]})
        self.assertEqual(result.metrics["tool_calls"], 1)
        self.assertEqual(result.metrics["llm_issues"], 1)
        self.assertGreaterEqual(result.metrics["llm_steps"], 2)
        self.assertTrue(result.metrics["traces"][0]["tool"] == "list_files")
        self.assertTrue(any("模型补充" in note for note in result.notes))
        self.assertFalse(result.metrics["offline"])

    def test_model_without_tool_support_skips_llm_stage(self):
        """``supports_tools=False`` 的模型不具备"工具核实"能力，审查只走确定性规则路径。"""
        model = FakeLLM([llm_json()], supports_tools=False)
        with temp_workspace("agent-review-notools") as ws:
            write_bad_module(ws)
            result = self._run(ws, model)
        self.assertEqual(model.calls, [])
        self.assertNotIn("llm_steps", result.metrics)
        self.assertTrue(result.metrics["offline"])
        self.assertIn("SEC001", {item["rule_id"] for item in result.data["issues"]})

    def test_model_failure_degrades_to_rules(self):
        model = FakeLLM([RuntimeError("模型挂了")])
        with temp_workspace("agent-review-degrade") as ws:
            write_bad_module(ws)
            result = self._run(ws, model)
        self.assertTrue(result.metrics["llm_degraded"])
        self.assertIn("SEC001", {item["rule_id"] for item in result.data["issues"]})
        self.assertTrue(any("降级" in note for note in result.notes))

    def test_tool_failure_is_reported_to_model(self):
        tool_call = ToolCall(id="c1", name="read_file", arguments={"path": "../secret.txt"})
        model = FakeLLM([
            LLMResponse(content="", tool_calls=[tool_call]),
            llm_json(),
        ], supports_tools=True)
        with temp_workspace("agent-review-toolfail") as ws:
            write_bad_module(ws)
            result = self._run(ws, model)
        traces = result.metrics["traces"]
        self.assertEqual(traces[0]["tool"], "read_file")
        self.assertFalse(traces[0]["ok"])
        self.assertTrue(traces[0]["error"])

    def test_max_steps_forces_final_turn(self):
        tool_call = ToolCall(id="c1", name="list_files", arguments={})
        model = FakeLLM([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="", tool_calls=[tool_call]),
            llm_json(summary="最终结论"),
        ], supports_tools=True)
        with temp_workspace("agent-review-maxsteps") as ws:
            write_bad_module(ws)
            result = self._run(ws, model, config=RunConfig(workspace=ws, review_max_steps=2))
        self.assertEqual(result.metrics["llm_steps"], 2)
        self.assertEqual(len(model.calls), 3)                 # 2 步 + 最终回合


# --------------------------------------------------------------------------- #
# FixAgent
# --------------------------------------------------------------------------- #
class FixAgentTest(unittest.TestCase):
    """修复 Agent：确定性重写、patch 契约与工作区回写。"""

    def _review_issues(self, ws):
        ctx = make_ctx(ws, node_id="review", agent="reviewer")
        return ReviewAgent(RuleChatModel()).run(ctx).data["issues"]

    def test_deterministic_fix_clears_blocking(self):
        with temp_workspace("agent-fix") as ws:
            write_bad_module(ws)
            issues = self._review_issues(ws)
            board = Blackboard(workspace=ws)
            board.set("issues", issues)
            ctx = make_ctx(ws, node_id="fix", agent="fixer", iteration=1, state=board)

            result = FixAgent(None).run(ctx)

            self.assertEqual(result.kind, "fix")
            self.assertEqual(result.decision, "continue")
            data = result.data
            self.assertEqual(set(data), {"patches", "fixed", "remaining"})
            self.assertTrue(data["patches"])
            self.assertTrue(data["fixed"])
            for patch in data["patches"]:
                self.assertEqual(set(patch), {"file", "issue_id", "summary", "before", "after",
                                              "rule_id"})
            self.assertIn("fix/fix_report.md", result.artifacts)
            # 黑板约定键
            self.assertEqual(board.get("fixed"), data["fixed"])
            self.assertEqual(board.get("patches"), data["patches"])

            with open(os.path.join(ws, "app", "service.py"), "r", encoding="utf-8") as handle:
                fixed_source = handle.read()
            self.assertNotIn("sk-hardcoded-demo-key", fixed_source)
            self.assertIn("os.environ.get", fixed_source)

            # 复审：阻断级问题清零
            review = ReviewAgent(RuleChatModel()).run(
                make_ctx(ws, node_id="review2", agent="reviewer", iteration=2, state=board))
            self.assertEqual(review.data["blocking"], [])
            self.assertNotIn("SEC001", {item["rule_id"] for item in review.data["issues"]})
            self.assertNotIn("BUG002", {item["rule_id"] for item in review.data["issues"]})

    def test_manual_rules_go_to_remaining(self):
        with temp_workspace("agent-fix-manual") as ws:
            write_bad_module(ws)
            board = Blackboard(workspace=ws)
            board.set("issues", [
                {"id": "SYN001-app/service.py:1", "title": "语法错误", "file": "app/service.py",
                 "line": 1, "severity": "high", "category": "bug", "rule_id": "SYN001"},
                {"id": "MAINT007-app/service.py:3", "title": "通配符导入",
                 "file": "app/service.py", "line": 3, "severity": "medium",
                 "category": "maintainability", "rule_id": "MAINT007"},
            ])
            result = FixAgent(None).run(make_ctx(ws, node_id="fix", state=board))
        self.assertEqual(result.data["patches"], [])
        self.assertEqual(result.data["fixed"], [])
        self.assertEqual(len(result.data["remaining"]), 2)
        reasons = {item["rule_id"]: item["reason"] for item in result.data["remaining"]}
        self.assertEqual(reasons["SYN001"], MANUAL_RULES["SYN001"])
        self.assertIn("通配符", reasons["MAINT007"])

    def test_no_issues_returns_empty_contract(self):
        with temp_workspace("agent-fix-empty") as ws:
            board = Blackboard(workspace=ws)
            result = FixAgent(None).run(make_ctx(ws, node_id="fix", state=board))
        self.assertEqual(result.data, {"patches": [], "fixed": [], "remaining": []})
        self.assertEqual(result.kind, "fix")
        self.assertTrue(any("没有可修复" in note for note in result.notes))

    def test_missing_file_goes_to_remaining(self):
        with temp_workspace("agent-fix-missingfile") as ws:
            board = Blackboard(workspace=ws)
            board.set("issues", [
                {"id": "SEC001-ghost.py:1", "title": "硬编码密钥", "file": "ghost.py",
                 "line": 1, "severity": "high", "category": "security", "rule_id": "SEC001"},
            ])
            result = FixAgent(None).run(make_ctx(ws, node_id="fix", state=board))
        self.assertEqual(len(result.data["remaining"]), 1)
        self.assertIn("都不存在", result.data["remaining"][0]["reason"])

    def test_offline_model_adopts_repaired_file(self):
        with temp_workspace("agent-fix-model") as ws:
            write_bad_module(ws)
            board = Blackboard(workspace=ws)
            board.set("issues", self._review_issues(ws))
            result = FixAgent(RuleChatModel()).run(make_ctx(ws, node_id="fix", state=board))
            summaries = [patch["summary"] for patch in result.data["patches"]]
            self.assertTrue(any("环境变量" in text for text in summaries))
            with open(os.path.join(ws, "app", "service.py"), "r", encoding="utf-8") as handle:
                self.assertNotIn("sk-hardcoded-demo-key", handle.read())

    def test_fix_report_renderer(self):
        text = render_fix_report(
            [{"issue_id": "SEC001-a.py:1", "file": "a.py", "summary": "改成环境变量",
              "before": "API_KEY = 'x'", "after": 'API_KEY = os.environ["K"]'}],
            ["SEC001-a.py:1"], [{"severity": "low", "title": "遗留", "file": "b.py", "line": 2,
                                 "reason": "需人工"}], {"a.py": "/tmp/a.py"})
        self.assertIn("# 修复报告", text)
        self.assertIn("改成环境变量", text)
        self.assertIn("需人工", text)
        self.assertIn("a.py", text)


# --------------------------------------------------------------------------- #
# DocAgent
# --------------------------------------------------------------------------- #
class DocAgentTest(unittest.TestCase):
    """文档 Agent：五个角色与离线模板。"""

    def test_requirements_role(self):
        with temp_workspace("agent-doc-req") as ws:
            result = DocAgent(RuleChatModel(), role="requirements").run(
                make_ctx(ws, node_id="requirements"))
        self.assertEqual(result.kind, "requirement")
        self.assertIn("requirement", result.data)
        self.assertTrue(result.data["outline"])
        self.assertTrue(result.data["documents"])
        self.assertIn("docs/requirements.md", result.artifacts)

    def test_designer_role_gives_file_list(self):
        with temp_workspace("agent-doc-design") as ws:
            result = DocAgent(RuleChatModel(), role="designer").run(
                make_ctx(ws, node_id="design"))
        self.assertIn("design", result.data)
        self.assertTrue(result.data["design"]["files"])
        self.assertIn("docs/design.md", result.data["documents"])

    def test_planner_role(self):
        with temp_workspace("agent-doc-plan") as ws:
            result = DocAgent(RuleChatModel(), role="planner").run(make_ctx(ws, node_id="plan"))
        self.assertTrue(result.data["outline"])
        self.assertIn("docs/outline.md", result.data["documents"])

    def test_writer_role(self):
        with temp_workspace("agent-doc-write") as ws:
            result = DocAgent(RuleChatModel(), role="writer").run(make_ctx(ws, node_id="write"))
            documents = result.data["documents"]
            self.assertTrue(documents)
            self.assertTrue(any(name.endswith(".md") for name in documents))
            self.assertEqual(result.decision, "pass")
            self.assertTrue(os.path.isfile(os.path.join(ws, "README.md")))

    def test_writer_without_model_uses_template(self):
        with temp_workspace("agent-doc-write-none") as ws:
            result = DocAgent(None, role="writer").run(make_ctx(ws, node_id="write"))
        self.assertTrue(result.data["documents"])
        self.assertTrue(any("模板" in note for note in result.notes))

    def test_documenter_alias_writes_readme(self):
        with temp_workspace("agent-doc-documenter") as ws:
            agent = DocAgent(RuleChatModel(), role="writer", name="documenter")
            self.assertEqual(agent.name, "documenter")
            result = agent.run(make_ctx(ws, node_id="docs"))
        self.assertTrue(result.data["documents"])

    def test_proofreader_reads_upstream_documents(self):
        upstream = AgentResult(kind="doc", data={"documents": {
            "README.md": "# 标题\n\n## 小节\n\n正文内容。\n"}})
        with temp_workspace("agent-doc-proof") as ws:
            result = DocAgent(RuleChatModel(), role="proofreader").run(
                make_ctx(ws, node_id="proofread", inputs={"write": upstream}))
        self.assertIn("docs/proofread_notes.md", result.data["documents"])
        self.assertEqual(result.decision, "done")
        self.assertIn("审校", result.summary)

    def test_proofreader_flags_empty_document(self):
        upstream = AgentResult(kind="doc", data={"documents": {"EMPTY.md": "   "}})
        with temp_workspace("agent-doc-proof-empty") as ws:
            result = DocAgent(RuleChatModel(), role="proofreader").run(
                make_ctx(ws, node_id="proofread", inputs={"write": upstream}))
        self.assertTrue(result.artifacts)
        notes = result.data["documents"]["docs/proofread_notes.md"]
        self.assertIn("文档为空", notes)

    def test_invalid_role_raises_config_error(self):
        with self.assertRaises(ConfigError):
            DocAgent(RuleChatModel(), role="nope")

    def test_write_disabled_keeps_documents_in_result(self):
        with temp_workspace("agent-doc-nowrite") as ws:
            agent = DocAgent(RuleChatModel(), role="writer", config=Config(write_workspace=False))
            result = agent.run(make_ctx(ws, node_id="write"))
            self.assertTrue(result.data["documents"])
            self.assertEqual(os.listdir(ws), [])
            self.assertTrue(any("禁用工作区写入" in note for note in result.notes))

    def test_outline_from_upstream_design(self):
        upstream = AgentResult(kind="doc", data={"files": ["app/service.py"]})
        with temp_workspace("agent-doc-upstream") as ws:
            result = DocAgent(None, role="writer").run(
                make_ctx(ws, node_id="write", inputs={"design": upstream}))
            body = result.data["documents"].get("README.md", "")
            self.assertIn("app/service.py", body)


# --------------------------------------------------------------------------- #
# LegacyReviewAgent
# --------------------------------------------------------------------------- #
class LegacyReviewAgentTest(unittest.TestCase):
    """Homework1 适配器：可用则复用，不可用则回退且不崩。"""

    class _NoCraImportlib:
        """把 ``importlib`` 换成"cra 永远导入失败"的替身。"""

        @staticmethod
        def invalidate_caches():
            return None

        @staticmethod
        def import_module(name, *args, **kwargs):
            if str(name).startswith("cra"):
                raise ModuleNotFoundError("No module named 'cra'")
            raise AssertionError("不该导入其它模块：%s" % name)

    def _review(self, ws, *, cra_root=None, config=None):
        board = Blackboard(workspace=ws)
        ctx = make_ctx(ws, node_id="legacy", agent="legacy_reviewer", state=board)
        return LegacyReviewAgent(cra_root=cra_root, offline=True, config=config).run(ctx)

    def test_contract_holds_with_or_without_cra(self):
        with temp_workspace("agent-legacy") as ws:
            write_bad_module(ws)
            result = self._review(ws)
        self.assertEqual(result.kind, "review")
        self.assertEqual(set(result.data), {"issues", "counts", "verdict", "blocking"})
        self.assertIn("legacy", result.metrics)
        self.assertIsInstance(result.metrics["legacy"], bool)
        self.assertEqual(sum(result.data["counts"].values()), len(result.data["issues"]))
        self.assertEqual(result.data["verdict"], verdict_for(result.data["counts"]))
        self.assertEqual(result.decision, "retry" if result.data["blocking"] else "pass")
        self.assertIn("review/legacy_reviewer.md", result.artifacts)
        if result.metrics["legacy"]:
            self.assertIn("Homework1", result.summary)
            self.assertIn("legacy_findings", result.metrics)
        else:
            self.assertIn("回退", result.summary)

    def test_fallback_when_cra_unavailable(self):
        with temp_workspace("agent-legacy-fallback") as ws:
            write_bad_module(ws)
            with mock.patch.object(legacy_module, "importlib", self._NoCraImportlib):
                result = self._review(ws)

        self.assertFalse(result.metrics["legacy"])
        self.assertEqual(result.metrics["cra_root"], "未指定")
        self.assertGreater(result.metrics.get("fallback_rule_hits", 0), 0)
        self.assertTrue(any("未能复用 Homework1" in note for note in result.notes))
        self.assertIn("回退", result.summary)
        rule_ids = {item["rule_id"] for item in result.data["issues"]}
        self.assertIn("SEC001", rule_ids)
        self.assertIn("BUG002", rule_ids)
        self.assertTrue(result.data["blocking"])

    def test_fallback_on_missing_workspace(self):
        with temp_workspace("agent-legacy-nows") as ws:
            missing = os.path.join(ws, "gone")
            board = Blackboard(workspace=missing)
            ctx = make_ctx(ws, node_id="legacy", state=board)
            ctx.workspace = missing
            with mock.patch.object(legacy_module, "importlib", self._NoCraImportlib):
                result = LegacyReviewAgent(cra_root="", offline=True).run(ctx)
        self.assertFalse(result.metrics["legacy"])
        self.assertEqual(result.data["issues"], [])
        self.assertTrue(result.notes)

    def test_cra_root_from_config_extra_is_ignored_when_missing(self):
        with temp_workspace("agent-legacy-craroot") as ws:
            write_bad_module(ws)
            config = Config(extra={"cra_root": os.path.join(ws, "no-such-hw1")})
            with mock.patch.object(legacy_module, "importlib", self._NoCraImportlib):
                result = self._review(ws, cra_root=os.path.join(ws, "no-such-hw1"),
                                      config=config)
        self.assertFalse(result.metrics["legacy"])
        self.assertTrue(any("回退" in note for note in result.notes))

    def test_registry_registers_legacy_agent(self):
        agent = default_registry(RuleChatModel()).get("legacy_reviewer")
        self.assertIsInstance(agent, LegacyReviewAgent)
        self.assertEqual(agent.name, "legacy_reviewer")

    def test_cra_env_keys(self):
        from wfa.agents.code_review import CRA_ENV_KEYS

        self.assertEqual(tuple(CRA_ENV_KEYS),
                         ("WFA_LEGACY_CRA_ROOT", "WFA_CRA_ROOT", "CRA_ROOT", "HW1_ROOT"))

    def test_legacy_cra_root_env_reaches_registry(self):
        """`.env`/环境变量里的 WFA_LEGACY_CRA_ROOT 必须一路传到适配器的 cra_root。"""
        with temp_workspace("agent-legacy-env") as ws:
            with mock.patch.dict(os.environ, {"WFA_LEGACY_CRA_ROOT": ws}, clear=False):
                config = Config.from_env()
            self.assertEqual(config.extra.get("cra_root"), ws)
            agent = default_registry(RuleChatModel(), config=config).get("legacy_reviewer")
        self.assertEqual(agent.cra_root, ws)


# --------------------------------------------------------------------------- #
# 离线端到端（Agent 之间通过黑板/总线协作）
# --------------------------------------------------------------------------- #
class AgentCollaborationTest(unittest.TestCase):
    """coder → reviewer → fixer → reviewer：离线闭环让阻断级问题清零。"""

    def test_coder_reviewer_fixer_reviewer_loop(self):
        with temp_workspace("agent-e2e") as ws:
            board = Blackboard(workspace=ws)
            model = RuleChatModel()

            code = CodingAgent(model).run(
                make_ctx(ws, node_id="code", agent="coder", iteration=1, state=board))
            self.assertIn("app/service.py", code.data["files"])

            review1 = ReviewAgent(model).run(
                make_ctx(ws, node_id="review", agent="reviewer", iteration=1, state=board))
            self.assertTrue(review1.data["blocking"], "第一版实现应被规则引擎查出阻断级问题")
            self.assertEqual(review1.data["verdict"], "request_changes")
            board.set("issues", review1.data["issues"])
            board.set("issue_counts", review1.data["counts"])
            board.set("verdict", review1.data["verdict"])
            board.set("blocking", review1.data["blocking"])

            fix = FixAgent(model).run(
                make_ctx(ws, node_id="fix", agent="fixer", iteration=1, state=board))
            self.assertTrue(fix.data["patches"])

            review2 = ReviewAgent(model).run(
                make_ctx(ws, node_id="review2", agent="reviewer", iteration=2, state=board))
            self.assertEqual(review2.data["blocking"], [])
            self.assertIn(review2.data["verdict"], ("pass", "pass_with_comments"))
            self.assertEqual(review2.decision, "pass")

    def test_doc_chain_requirements_design_writer_proofreader(self):
        with temp_workspace("agent-doc-chain") as ws:
            board = Blackboard(workspace=ws)
            model = RuleChatModel()
            req = DocAgent(model, role="requirements").run(
                make_ctx(ws, node_id="requirements", state=board))
            design = DocAgent(model, role="designer").run(
                make_ctx(ws, node_id="design", state=board))
            plan = DocAgent(model, role="planner").run(
                make_ctx(ws, node_id="plan", state=board))
            write = DocAgent(model, role="writer").run(
                make_ctx(ws, node_id="write", state=board,
                         inputs={"design": AgentResult.from_dict(design.to_dict())}))
            proof = DocAgent(model, role="proofreader").run(
                make_ctx(ws, node_id="proofread", state=board,
                         inputs={"write": AgentResult.from_dict(write.to_dict())}))

        self.assertTrue(req.data["requirement"])
        self.assertTrue(design.data["design"]["files"])
        self.assertTrue(plan.data["outline"])
        self.assertTrue(write.data["documents"])
        self.assertIn("docs/proofread_notes.md", proof.artifacts)


if __name__ == "__main__":
    unittest.main()
