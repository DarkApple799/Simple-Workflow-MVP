"""`wfa/workflows/` 预置工作流单元测试：拓扑契约、注册表与自定义节点。

覆盖点（对应 CONTRACT 第 10 节）：
    1. ``build_code_review_workflow`` 的拓扑与 CONTRACT 完全一致
       （start → requirements → design → code → review → gate_quality → {fix↺review | docs → report} → end），
       节点类型/Agent 绑定/loop_of/边与 when 标注、``validate()`` 为空、
       ``parallel_review=True`` 时插入 parallel_review 容器（两个子节点）且仍是合法图、
       ``include_docs=False`` 时去掉 docs 节点；
    2. ``build_docs_workflow`` 的拓扑（start → plan → write → proofread → gate_doc → {write↺ | publish} → end）；
    3. ``registry``：``build_workflow`` 已知/未知名字、kwargs 按签名过滤、
       ``list_workflows()`` 元信息；
    4. 自定义节点（不需要任何 Agent、纯确定性）：
       ``gate_quality`` 的 pass/retry/达上限三态、``report_run`` 汇总产物、
       ``gate_doc`` 的通过/返工/达上限三态、``publish`` 汇总文档。

本文件覆盖"图与纯函数节点" + "预置图接引擎（自定义 execute）" + **依赖真实 Agent 的离线端到端**
（``WorkflowEndToEndTest``：RuleChatModel + default_registry，全程离线、不需要 API Key）。
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import temp_workspace
from wfa.bus import MessageBus
from wfa.engine import RunConfig
from wfa.errors import ConfigError
from wfa.graph import AgentResult, Node, NodeContext
from wfa.state import Blackboard
from wfa.workflows import build_workflow, list_workflows
from wfa.workflows.code_review import (
    CODE,
    DESCRIPTION as CODE_REVIEW_DESC,
    DESIGN,
    DOCS,
    END,
    FIX,
    GATE,
    LEGACY_REVIEW,
    MAINT_REVIEW,
    PARALLEL_REVIEW,
    REPORT,
    REQUIREMENTS,
    REVIEW,
    SECURITY_REVIEW,
    START,
    build_code_review_workflow,
    gate_quality,
    report_run,
)
from wfa.workflows.docs import (
    DESCRIPTION as DOCS_DESC,
    GATE as DOC_GATE,
    PLAN,
    PROOFREAD,
    PUBLISH,
    START as DOC_START,
    END as DOC_END,
    WRITE,
    build_docs_workflow,
    gate_doc,
    publish,
)


def edge_pairs(workflow) -> list:
    """(src, dst, when) 三元组列表，便于断言拓扑。"""
    return [(edge.src, edge.dst, edge.when) for edge in workflow.edges()]


def make_ctx(node: Node, **state_keys) -> NodeContext:
    """构造一个最小 NodeContext（不经过引擎），供自定义节点单测使用。"""
    board = Blackboard()
    board.update(state_keys)
    return NodeContext(node=node, state=board, bus=MessageBus(), inputs={},
                       iteration=0, turn=1, workspace=board.workspace,
                       config=RunConfig(max_iterations=2), extras={})


class CodeReviewTopologyTest(unittest.TestCase):
    """code_review 拓扑与校验。"""

    def setUp(self):
        self.workflow = build_code_review_workflow()

    def test_valid_and_entry(self):
        self.assertEqual(self.workflow.validate(), [])
        self.assertEqual(self.workflow.name, "code_review")
        self.assertEqual(self.workflow.entry, START)
        self.assertEqual(self.workflow.entry_node().id, START)
        self.assertTrue(CODE_REVIEW_DESC)

    def test_node_sequence_and_types(self):
        self.assertEqual([node.id for node in self.workflow.nodes()],
                         [START, REQUIREMENTS, DESIGN, CODE, LEGACY_REVIEW, REVIEW,
                          GATE, FIX, DOCS, REPORT, END])
        self.assertEqual(self.workflow.node(START).type, "start")
        self.assertEqual(self.workflow.node(END).type, "end")
        self.assertEqual(self.workflow.node(GATE).type, "gate")
        self.assertEqual(self.workflow.node(REPORT).type, "route")

    def test_agent_bindings(self):
        expected = {
            REQUIREMENTS: "requirements",
            DESIGN: "designer",
            CODE: "coder",
            LEGACY_REVIEW: "legacy_reviewer",
            REVIEW: "reviewer",
            FIX: "fixer",
            DOCS: "documenter",
        }
        for node_id, agent in expected.items():
            with self.subTest(node=node_id):
                self.assertEqual(self.workflow.node(node_id).agent, agent)

    def test_loop_and_limits(self):
        for node_id in (CODE, FIX):
            with self.subTest(node=node_id):
                self.assertEqual(self.workflow.node(node_id).loop_of, "impl")
                self.assertEqual(self.workflow.node(node_id).max_iterations, 3)
        self.assertEqual(self.workflow.max_iterations, 3)
        self.assertEqual(self.workflow.max_turns, 50)

    def test_edges(self):
        self.assertEqual(edge_pairs(self.workflow), [
            (START, REQUIREMENTS, ""),
            (REQUIREMENTS, DESIGN, ""),
            (DESIGN, CODE, ""),
            (CODE, LEGACY_REVIEW, ""),
            (LEGACY_REVIEW, REVIEW, ""),
            (REVIEW, GATE, ""),
            (GATE, FIX, "retry"),
            (GATE, DOCS, "pass"),
            (FIX, REVIEW, ""),
            (DOCS, REPORT, ""),
            (REPORT, END, ""),
        ])

    def test_successors(self):
        self.assertEqual([node.id for node in self.workflow.successors(GATE)], [FIX, DOCS])
        self.assertEqual([node.id for node in self.workflow.successors(CODE)], [LEGACY_REVIEW])
        self.assertEqual([node.id for node in self.workflow.successors(LEGACY_REVIEW)], [REVIEW])

    def test_docs_when_condition(self):
        docs = self.workflow.node(DOCS)
        self.assertIsNotNone(docs.when)
        ctx = make_ctx(docs)
        self.assertTrue(docs.when(ctx))

    def test_custom_node_max_iterations_from_argument(self):
        workflow = build_code_review_workflow(max_iterations=1)
        self.assertEqual(workflow.node(CODE).max_iterations, 1)
        self.assertEqual(workflow.max_iterations, 1)
        self.assertEqual(workflow.validate(), [])

    def test_without_docs(self):
        workflow = build_code_review_workflow(include_docs=False)
        self.assertEqual(workflow.validate(), [])
        self.assertIn((GATE, REPORT, "pass"), edge_pairs(workflow))
        self.assertNotIn((DOCS, REPORT, ""), edge_pairs(workflow))
        # include_docs=False 时 docs 节点不可达（无入边）。
        self.assertNotIn(DOCS, {edge.dst for edge in workflow.edges()})

    def test_parallel_review_variant(self):
        workflow = build_code_review_workflow(parallel_review=True)
        self.assertEqual(workflow.validate(), [])
        parallel = workflow.node(PARALLEL_REVIEW)
        self.assertIsNotNone(parallel)
        self.assertEqual(parallel.type, "parallel")
        self.assertEqual([child.id for child in parallel.children], [SECURITY_REVIEW, MAINT_REVIEW])
        self.assertEqual([child.agent for child in parallel.children],
                         ["security_reviewer", "maintainability_reviewer"])
        self.assertIn((CODE, LEGACY_REVIEW, ""), edge_pairs(workflow))
        self.assertIn((LEGACY_REVIEW, PARALLEL_REVIEW, ""), edge_pairs(workflow))
        self.assertIn((PARALLEL_REVIEW, REVIEW, ""), edge_pairs(workflow))
        self.assertNotIn((CODE, REVIEW, ""), edge_pairs(workflow))
        self.assertIn("subgraph", workflow.to_mermaid())

    def test_legacy_review_node_uses_homework1_adapter(self):
        node = self.workflow.node(LEGACY_REVIEW)
        self.assertEqual(node.agent, "legacy_reviewer")
        self.assertEqual(node.type, "agent")
        self.assertIn("Homework1", node.description)
        # 节点绑定的 Agent 名字必须能在默认注册表里解析到（否则运行时会失败）。
        from wfa.agents import default_registry
        from wfa.model import RuleChatModel

        registry = default_registry(RuleChatModel())
        self.assertIn(node.agent, registry.names())
        self.assertEqual(registry.get(node.agent).name, "legacy_reviewer")

    def test_serializable(self):
        import json
        json.dumps(self.workflow.to_dict(), ensure_ascii=False)
        self.assertIn("flowchart TD", self.workflow.to_mermaid())
        self.assertIn("digraph", self.workflow.to_dot())


class DocsTopologyTest(unittest.TestCase):
    """docs 拓扑与校验。"""

    def setUp(self):
        self.workflow = build_docs_workflow()

    def test_valid_and_nodes(self):
        self.assertEqual(self.workflow.validate(), [])
        self.assertEqual(self.workflow.name, "docs")
        self.assertEqual(self.workflow.entry, DOC_START)
        self.assertEqual([node.id for node in self.workflow.nodes()],
                         [DOC_START, PLAN, WRITE, PROOFREAD, DOC_GATE, PUBLISH, DOC_END])
        self.assertEqual(self.workflow.node(WRITE).agent, "writer")
        self.assertEqual(self.workflow.node(PLAN).agent, "planner")
        self.assertEqual(self.workflow.node(PROOFREAD).agent, "proofreader")
        self.assertEqual(self.workflow.node(WRITE).loop_of, "doc")
        self.assertTrue(DOCS_DESC)

    def test_edges(self):
        self.assertEqual(edge_pairs(self.workflow), [
            (DOC_START, PLAN, ""),
            (PLAN, WRITE, ""),
            (WRITE, PROOFREAD, ""),
            (PROOFREAD, DOC_GATE, ""),
            (DOC_GATE, WRITE, "retry"),
            (DOC_GATE, PUBLISH, "pass"),
            (PUBLISH, DOC_END, ""),
        ])

    def test_max_iterations_argument(self):
        workflow = build_docs_workflow(max_iterations=4, max_turns=9)
        self.assertEqual(workflow.node(WRITE).max_iterations, 4)
        self.assertEqual(workflow.max_turns, 9)
        self.assertEqual(workflow.validate(), [])


class RegistryTest(unittest.TestCase):
    """工作流注册表。"""

    def test_list_workflows(self):
        items = list_workflows()
        self.assertEqual([item["name"] for item in items], ["code_review", "docs"])
        for item in items:
            with self.subTest(name=item["name"]):
                self.assertTrue(item["description"])
                self.assertTrue(item["agents"])

    def test_build_known_names(self):
        for name in ("code_review", "docs"):
            with self.subTest(name=name):
                workflow = build_workflow(name)
                self.assertEqual(workflow.name, name)
                self.assertEqual(workflow.validate(), [])

    def test_build_unknown_name_raises_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            build_workflow("nope")
        self.assertIn("nope", str(ctx.exception))

    def test_kwargs_are_filtered_by_signature(self):
        # docs 没有 parallel_review 参数，不应因此 TypeError
        workflow = build_workflow("docs", parallel_review=True, max_iterations=1,
                                  max_turns=7, unknown_option=1)
        self.assertEqual(workflow.name, "docs")
        self.assertEqual(workflow.max_iterations, 1)
        self.assertEqual(workflow.max_turns, 7)

    def test_code_review_kwargs_are_forwarded(self):
        workflow = build_workflow("code_review", parallel_review=True, max_iterations=2,
                                  max_turns=12)
        self.assertIsNotNone(workflow.node(PARALLEL_REVIEW))
        self.assertEqual(workflow.max_iterations, 2)
        self.assertEqual(workflow.max_turns, 12)


class GateQualityTest(unittest.TestCase):
    """code_review 的质量门禁（纯函数节点，不需要 Agent）。"""

    def _gate(self, board: dict, iteration: int = 0, limit: int = 2):
        node = Node(id=GATE, type="gate", execute=gate_quality, max_iterations=limit)
        ctx = make_ctx(node, **board)
        for _ in range(iteration):
            ctx.state.bump_iteration("impl")
        return gate_quality(node, ctx)

    def test_no_blocking_passes(self):
        result = self._gate({"issues": [{"id": "i", "severity": "low"}],
                             "issue_counts": {"low": 1}, "verdict": "pass_with_comments"})
        self.assertEqual(result.decision, "pass")
        self.assertEqual(result.kind, "gate")
        self.assertTrue(result.data["converged"])
        self.assertIn("门禁通过", result.summary)

    def test_blocking_with_budget_retries(self):
        result = self._gate({"blocking": [{"id": "i"}], "issue_counts": {"high": 1}}, iteration=0)
        self.assertEqual(result.decision, "retry")
        self.assertFalse(result.data["converged"])
        self.assertIn("转交修复", result.summary)
        self.assertEqual(result.data["iteration"], 0)
        self.assertEqual(result.data["limit"], 2)

    def test_blocking_at_limit_passes_with_warning(self):
        result = self._gate({"blocking": [{"id": "i"}]}, iteration=2, limit=2)
        self.assertEqual(result.decision, "pass")
        self.assertIn("迭代上限", result.summary)

    def test_blocking_inferred_from_high_severity_issues(self):
        result = self._gate({"issues": [{"id": "i", "severity": "high"}]})
        self.assertEqual(result.decision, "retry")

    def test_uses_config_limit_when_node_has_none(self):
        node = Node(id=GATE, type="gate", execute=gate_quality)
        ctx = make_ctx(node, blocking=[{"id": "i"}])
        ctx.config.max_iterations = 1
        result = gate_quality(node, ctx)
        self.assertEqual(result.decision, "retry")
        self.assertEqual(result.data["limit"], 1)


class ReportNodeTest(unittest.TestCase):
    """code_review 的汇报节点（纯函数节点）。"""

    def test_summary_and_artifact(self):
        node = Node(id=REPORT, type="route", execute=report_run)
        ctx = make_ctx(node, issues=[{"id": "i1", "severity": "high", "title": "硬编码密钥",
                                      "file": "app/service.py", "line": 8}],
                       issue_counts={"high": 1}, fixed=["i1"], remaining=[],
                       workspace="C:/ws")
        ctx.inputs[GATE] = AgentResult(kind="gate", data={"converged": True})

        result = report_run(node, ctx)

        self.assertEqual(result.kind, "report")
        self.assertEqual(result.decision, "done")
        self.assertIn("共 1 条问题", result.summary)
        self.assertIn("report/run_summary.md", result.artifacts)
        body = result.artifacts["report/run_summary.md"]
        self.assertIn("## 运行小结", body)
        self.assertIn("硬编码密钥", body)
        self.assertIn("是否收敛：是", body)
        self.assertIn("C:/ws", body)

    def test_unconverged_and_empty_issue_list(self):
        node = Node(id=REPORT, type="route", execute=report_run)
        ctx = make_ctx(node, issues=[], issue_counts={}, fixed=[], remaining=["x"])
        ctx.inputs[GATE] = AgentResult(kind="gate", data={"converged": False})
        result = report_run(node, ctx)
        self.assertIn("未修复 1 条", result.summary)
        self.assertIn("是否收敛：否", result.artifacts["report/run_summary.md"])
        self.assertIn("（无）", result.artifacts["report/run_summary.md"])


class GateDocAndPublishTest(unittest.TestCase):
    """docs 的门禁与发布节点（纯函数节点）。"""

    def _ctx(self, *, verdict="", issues=None, iteration=0, limit=2):
        node = Node(id=DOC_GATE, type="gate", execute=gate_doc, max_iterations=limit)
        ctx = make_ctx(node)
        ctx.inputs[PROOFREAD] = AgentResult(kind="review",
                                            data={"verdict": verdict, "issues": issues or []})
        for _ in range(iteration):
            ctx.state.bump_iteration("doc")
        return node, ctx

    def test_pass_verdict_converges(self):
        node, ctx = self._ctx(verdict="pass")
        result = gate_doc(node, ctx)
        self.assertEqual(result.decision, "pass")
        self.assertTrue(result.data["converged"])

    def test_comments_verdict_converges(self):
        node, ctx = self._ctx(verdict="pass_with_comments", issues=[{"id": 1}])
        self.assertEqual(gate_doc(node, ctx).decision, "pass")

    def test_issues_with_budget_retries(self):
        node, ctx = self._ctx(verdict="request_changes", issues=[{"id": 1}], iteration=0)
        result = gate_doc(node, ctx)
        self.assertEqual(result.decision, "retry")
        self.assertFalse(result.data["converged"])
        self.assertIn("返回写作", result.summary)

    def test_issues_at_limit_publishes(self):
        node, ctx = self._ctx(verdict="request_changes", issues=[{"id": 1}], iteration=2, limit=2)
        result = gate_doc(node, ctx)
        self.assertEqual(result.decision, "pass")
        self.assertFalse(result.data["converged"])
        self.assertIn("迭代上限", result.summary)

    def test_publish_collects_documents(self):
        node = Node(id=PUBLISH, type="route", execute=publish)
        ctx = make_ctx(node)
        ctx.inputs[WRITE] = AgentResult(kind="doc", data={
            "documents": {"README.md": "# 标题", "GUIDE.md": "## 指南"},
            "outline": ["标题", "指南"],
        })
        result = publish(node, ctx)
        self.assertEqual(result.kind, "doc")
        self.assertIn("已发布 2 份文档", result.summary)
        self.assertEqual(result.data["outline"], ["标题", "指南"])
        self.assertGreater(result.data["characters"], 0)
        self.assertIn("docs/final_document.md", result.artifacts)
        self.assertIn("# 标题", result.artifacts["docs/final_document.md"])

    def test_publish_without_documents(self):
        node = Node(id=PUBLISH, type="route", execute=publish)
        ctx = make_ctx(node)
        result = publish(node, ctx)
        self.assertIn("未产出文档", result.artifacts["docs/final_document.md"])


class WorkflowEngineIntegrationTest(unittest.TestCase):
    """把预置工作流接到引擎上（用自定义 execute 节点，不依赖任何 Agent）。

    验证"预置图 + 引擎"的组合可用：顺序、门禁分支、循环与收尾节点都能跑通。
    """

    def test_docs_workflow_runs_with_custom_executes(self):
        from wfa.engine import WorkflowEngine

        workflow = build_docs_workflow(max_iterations=2)
        board = {"write_rounds": []}

        def plan_execute(node, ctx):
            return AgentResult(kind="doc", summary="大纲完成", data={"outline": ["一", "二"]})

        def write_execute(node, ctx):
            board["write_rounds"].append(ctx.iteration)
            return AgentResult(kind="doc", summary="写作完成",
                               data={"documents": {"README.md": "# 文档"},
                                     "outline": ["一", "二"]})

        def proofread_execute(node, ctx):
            # 第一轮给出意见 → 门禁 retry；第二轮通过。
            if len(board["write_rounds"]) < 2:
                return AgentResult(kind="review", summary="需修订",
                                   data={"verdict": "request_changes", "issues": [{"id": "c1"}]})
            return AgentResult(kind="review", summary="通过", data={"verdict": "pass", "issues": []})

        for node_id, fn in ((PLAN, plan_execute), (WRITE, write_execute),
                            (PROOFREAD, proofread_execute)):
            workflow.node(node_id).execute = fn
            workflow.node(node_id).agent = ""

        with temp_workspace("wf-docs") as ws:
            engine = WorkflowEngine(workflow, None,
                                    config=RunConfig(workspace=ws, max_iterations=2))
            report = engine.run()

        self.assertEqual(report.status, "success")
        self.assertEqual(board["write_rounds"], [1, 2])
        self.assertEqual([record.node_id for record in report.node_runs],
                         [DOC_START, PLAN, WRITE, PROOFREAD, DOC_GATE, WRITE, PROOFREAD,
                          DOC_GATE, PUBLISH, DOC_END])
        self.assertIn("docs/final_document.md", report.artifacts)
        self.assertEqual(report.metrics["iterations"], {"doc": 2})

    def test_code_review_workflow_runs_with_custom_executes(self):
        from wfa.engine import WorkflowEngine

        workflow = build_code_review_workflow(max_iterations=2)
        rounds = {"fix": 0}

        def review_execute(node, ctx):
            # 第一轮有阻断问题 → retry 到 fix；第二轮清零。
            if rounds["fix"] == 0:
                return AgentResult(kind="review", summary="发现阻断", data={
                    "issues": [{"id": "i1", "severity": "high"}], "counts": {"high": 1},
                    "blocking": [{"id": "i1"}], "verdict": "block"})
            return AgentResult(kind="review", summary="已清零", data={
                "issues": [], "counts": {}, "blocking": [], "verdict": "pass"})

        def fix_execute(node, ctx):
            rounds["fix"] += 1
            return AgentResult(kind="fix", summary="修复完成", data={
                "patches": [{"file": "a.py", "issue_id": "i1", "summary": "修好"}],
                "fixed": ["i1"], "remaining": []})

        def noop_execute(node, ctx):
            return AgentResult(summary="%s 完成" % node.id)

        def legacy_execute(node, ctx):
            # 复用 Homework1 的审查节点：这里用确定性结论代替（不依赖 cra 是否安装）。
            return AgentResult(kind="review", summary="项目1 审查完成",
                               data={"issues": [], "counts": {}, "blocking": [],
                                     "verdict": "pass"})

        for node_id in (START, REQUIREMENTS, DESIGN, CODE, LEGACY_REVIEW, DOCS, END):
            workflow.node(node_id).execute = noop_execute
            if workflow.node(node_id).type not in ("start", "end"):
                workflow.node(node_id).agent = ""
        workflow.node(LEGACY_REVIEW).execute = legacy_execute
        workflow.node(REVIEW).execute = review_execute
        workflow.node(REVIEW).agent = ""
        workflow.node(FIX).execute = fix_execute
        workflow.node(FIX).agent = ""

        with temp_workspace("wf-code-review") as ws:
            engine = WorkflowEngine(workflow, None,
                                    config=RunConfig(workspace=ws, max_iterations=2))
            report = engine.run()

        self.assertEqual(report.status, "success")
        self.assertEqual(rounds["fix"], 1)
        self.assertIn(LEGACY_REVIEW, [record.node_id for record in report.node_runs])
        self.assertEqual(report.issues, [])              # 第二轮审查已清零
        self.assertEqual(report.metrics["blocking"], 0)
        self.assertIn("report/run_summary.md", report.artifacts)


class WorkflowEndToEndTest(unittest.TestCase):
    """离线端到端：RuleChatModel + default_registry 跑完两个预置流程。

    这是"整个工程能离线跑通"的总验收：
        * code_review：coder 产出"故意留缺陷"的第一版 → reviewer 查出阻断级问题 →
          gate retry → fixer 修干净 → reviewer 复审阻断清零 → 文档 → 汇报 → end；
        * docs：planner 大纲 → writer 文档 → proofreader 审校 → gate → publish → end。
    全程不调用真实模型、不需要 API Key。
    """

    TASK = "按需求实现一个小而完整的 Python 模块，并完成审查、修复与文档"

    def _run(self, ws, workflow, *, max_turns=50, max_iterations=3):
        from wfa.agents import default_registry
        from wfa.bus import MessageBus
        from wfa.config import Config
        from wfa.engine import RunConfig, WorkflowEngine
        from wfa.model import RuleChatModel

        model = RuleChatModel()
        config = Config(mode="offline", model="offline-rule-model", write_workspace=True)
        agents = default_registry(model, config=config)
        run_config = RunConfig(task=self.TASK, workspace=ws, mode="offline",
                               model="offline-rule-model", max_turns=max_turns,
                               max_iterations=max_iterations, review_max_steps=4,
                               stop_on_blocking=True)
        bus = MessageBus()
        state = Blackboard(workspace=ws, bus=bus)
        state.set("workspace", ws)
        state.set("task", self.TASK)
        engine = WorkflowEngine(workflow, agents, config=run_config, bus=bus, state=state)
        return engine.run(), engine, bus

    def test_code_review_end_to_end_offline(self):
        workflow = build_code_review_workflow(max_iterations=2)
        with temp_workspace("wf-e2e-code-review") as ws:
            report, engine, bus = self._run(ws, workflow, max_turns=50, max_iterations=2)

            self.assertEqual(report.status, "success")
            self.assertEqual(report.workflow, "code_review")
            self.assertGreater(report.messages, 0)
            self.assertGreater(len(report.node_runs), 0)

            # 1) 第一版确实被查出问题（阻断级非空），且真的走了修复回路
            order = [record.node_id for record in report.node_runs]
            self.assertLess(order.index(CODE), order.index(LEGACY_REVIEW))
            self.assertLess(order.index(LEGACY_REVIEW), order.index(REVIEW))

            legacy_records = [record for record in report.node_runs
                              if record.node_id == LEGACY_REVIEW]
            self.assertEqual(len(legacy_records), 1, "复用 Homework1 的节点必须被执行")
            legacy_metrics = legacy_records[0].result["metrics"]
            self.assertIn("legacy", legacy_metrics)
            self.assertIsInstance(legacy_metrics["legacy"], bool)
            self.assertEqual(set(legacy_records[0].result["data"]),
                             {"issues", "counts", "verdict", "blocking"})

            reviews = [record for record in report.node_runs if record.node_id == REVIEW]
            self.assertEqual(len(reviews), 2, "应当经历 review → fix → review 两轮")
            first_issues = reviews[0].result["data"]["issues"]
            self.assertTrue(first_issues, "第一版实现必须能被规则引擎查出问题")
            self.assertTrue(reviews[0].result["data"]["blocking"])
            self.assertEqual(reviews[0].decision, "retry")
            fix_records = [record for record in report.node_runs if record.node_id == FIX]
            self.assertEqual(len(fix_records), 1)
            self.assertTrue(fix_records[0].result["data"]["patches"])

            # 2) 修复后阻断级问题清零
            self.assertEqual(reviews[1].result["data"]["blocking"], [])
            self.assertEqual(report.metrics["blocking"], 0)
            self.assertEqual(report.counts.get("high", 0), 0)
            self.assertEqual(report.counts.get("critical", 0), 0)
            reviews[1].result["data"]["issues"]

            # 3) 代码真的写进工作区，且硬编码密钥已被修复
            self.assertTrue(os.path.isfile(os.path.join(ws, "app", "service.py")))
            with open(os.path.join(ws, "app", "service.py"), "r", encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("sk-demo-hardcoded-key", source)

            # 4) 报告与文档产物齐全
            self.assertIn("report/run_summary.md", report.artifacts)
            self.assertIn("运行小结", report.artifacts["report/run_summary.md"]["content"])
            self.assertTrue(os.path.isfile(os.path.join(ws, "README.md")))
            self.assertIn("docs/requirements.md", report.artifacts)
            self.assertIn("code/plan.md", report.artifacts)
            self.assertIn("review/reviewer.md", report.artifacts)

            # 5) 协作轨迹：总线有消息，门禁有 retry→pass 的决策记录
            self.assertGreater(len(bus.messages), 0)
            gate_decisions = [record.decision for record in report.node_runs
                              if record.node_id == GATE]
            self.assertEqual(gate_decisions, ["retry", "pass"])
            self.assertEqual(report.metrics["iterations"].get("impl"), 2)
            self.assertEqual(engine.state.state.status, "success")

    def test_docs_end_to_end_offline(self):
        workflow = build_docs_workflow(max_iterations=2)
        with temp_workspace("wf-e2e-docs") as ws:
            report, _engine, bus = self._run(ws, workflow, max_turns=30, max_iterations=2)

            self.assertEqual(report.status, "success")
            self.assertEqual(report.workflow, "docs")
            self.assertGreater(report.messages, 0)

            # planner → writer → proofreader → gate → publish → end
            self.assertEqual([record.node_id for record in report.node_runs],
                             [DOC_START, PLAN, WRITE, PROOFREAD, DOC_GATE, PUBLISH, DOC_END])

            # 文档非空：写节点产出的 documents 有实际内容，并被写入工作区
            write_records = [record for record in report.node_runs if record.node_id == WRITE]
            documents = write_records[0].result["data"]["documents"]
            self.assertTrue(documents)
            content = next(iter(documents.values()))
            self.assertGreater(len(content.strip()), 20)
            self.assertTrue(os.path.isfile(os.path.join(ws, "README.md")))
            with open(os.path.join(ws, "README.md"), "r", encoding="utf-8") as handle:
                self.assertGreater(len(handle.read().strip()), 20)

            # 产物登记：大纲 / 正文 / 审校意见
            self.assertIn("docs/outline.md", report.artifacts)
            self.assertIn("docs/proofread_notes.md", report.artifacts)
            self.assertTrue(any(name.endswith(".md") for name in report.artifacts))
            self.assertTrue(report.metrics["iterations"].get("doc", 0) >= 1)
            self.assertGreater(len(bus.messages), 0)


if __name__ == "__main__":
    unittest.main()
