"""`wfa/graph.py` + `wfa/engine.py` 单元测试：流程定义校验与编排执行语义。

覆盖点（CONTRACT 第 5、8 节，以及 Lead 确认的 6 条实现口径）：
    1. WorkflowBuilder/Workflow 校验：悬空边 / 重复节点 / 缺 entry → NodeError；合法流程
       ``validate()`` 为空；Node 非法 type / 负 retry / 空 id → NodeError；
    2. 顺序流程 success，节点记录、回合数与 stop_reason 正确；
    3. 出边选择顺序敏感：``pass/done`` → 第一个 dst 为 end 的边，否则**最后一条**边；
       ``retry/continue`` → 第一条 ``when/label=="retry"`` 的边，否则第一条；
       ``""`` → 第一条 when 为空的边；``fail`` 有 ``when="fail"`` 边则走该边；
    4. gate retry 循环：两轮后 pass，``loop_of`` 迭代计数正确；loop_of 超 max_iterations → partial；
    5. ``when=False`` → skipped（不执行）；``when`` 抛异常 → 按失败处理；
    6. 失败处理：显式 fallback > ``when="fail"`` 边 > ``on_error``（continue / fail）；
       节点异常重试（attempts/attempt 计数、退换备注）与重试耗尽后拿到原始异常；
    7. ``max_turns`` 截断 → partial；``stop_check``/``abort`` → aborted；
    8. ``stop_on_blocking``：正常走到 end 仍为 success（阻断只体现在 issues/metrics），
       非 end 节点断流且存在 blocking → partial；
    9. parallel 真并发（子节点起止区间重叠）、子结果读取三种方式、单个子节点失败不影响其它；
    10. 事件序列含 turn_start/node_start/node_end（首事件 turn_start、末事件 stop）；
    11. checkpoint → resume：从游标继续、之前节点不重跑、notes 带"已从检查点恢复"；
    12. AgentResult → 黑板约定键（issues/issue_counts/blocking/verdict/fixes/results）、
        NodeOutcome 的 next_node/skipped、RunConfig/RunReport 序列化不含密钥。

全部离线：Agent 用假注册表注入，重试退避用 ``node.retry_delay=0`` 或假 Agent，唯一真实等待
是并行并发用例里的 3×0.2s（用于证明时间区间重叠）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest

from tests.helpers import temp_workspace
from wfa.bus import BROADCAST, PERF_ID, MessageBus
from wfa.engine import EVENTS, RunConfig, RunReport, StopReason, WorkflowEngine
from wfa.errors import ConfigError, NodeError
from wfa.graph import (
    AgentResult,
    Node,
    NodeContext,
    NodeOutcome,
    Workflow,
    WorkflowBuilder,
)
from wfa.state import Blackboard


# --------------------------------------------------------------------------- #
# 测试替身：假 Agent / 假注册表
# --------------------------------------------------------------------------- #
class FakeAgent:
    """可注入 run 逻辑的假 Agent（记录每次调用的上下文）。"""

    def __init__(self, name: str, run_fn=None) -> None:
        self.name = name
        self.role = "测试 Agent %s" % name
        self.model = None
        self.calls: list = []
        self._run_fn = run_fn or (lambda ctx: AgentResult(summary=name))

    def run(self, ctx: NodeContext) -> AgentResult:
        self.calls.append(ctx)
        return self._run_fn(ctx)

    def describe(self) -> str:
        return "假 Agent：%s（调用 %d 次）" % (self.name, len(self.calls))


class FakeRegistry:
    """最小 AgentRegistry：``get`` 缺失时抛 ConfigError（与契约一致）。"""

    def __init__(self, agents=()) -> None:
        self._agents = {}
        for item in agents:
            self.register(item)

    def register(self, agent) -> object:
        self._agents[agent.name] = agent
        return agent

    def get(self, name: str):
        if name not in self._agents:
            raise ConfigError("未注册 Agent：%s" % name)
        return self._agents[name]

    def names(self) -> list:
        return sorted(self._agents)

    def describe(self) -> list:
        return [{"name": item.name, "role": item.role, "describe": item.describe()}
                for item in (self._agents[key] for key in self.names())]


def fixed_agent(name: str, **spec) -> FakeAgent:
    """返回每次调用都产出同一 ``AgentResult(**spec)`` 的假 Agent。"""
    def run(ctx):
        return AgentResult(**dict(spec))
    return FakeAgent(name, run)


def registry(*agents) -> FakeRegistry:
    """把若干假 Agent 打包成注册表。"""
    return FakeRegistry(agents)


def stop_reason(report: RunReport) -> str:
    """取报告里的停止原因。"""
    return str(report.metrics.get("stop_reason", ""))


def node_ids(report: RunReport) -> list:
    """报告中的节点执行顺序。"""
    return [record.node_id for record in report.node_runs]


def statuses(report: RunReport) -> list:
    """报告中各记录的终态。"""
    return [record.status for record in report.node_runs]


class GraphValidationTest(unittest.TestCase):
    """WorkflowBuilder / Workflow / Node 的定义期校验。"""

    def test_valid_workflow_validates_empty(self):
        workflow = (
            WorkflowBuilder("ok")
            .start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end")
            .build()
        )
        self.assertEqual(workflow.validate(), [])
        self.assertEqual(workflow.name, "ok")
        self.assertEqual(workflow.entry, "start")
        self.assertEqual(workflow.entry_node().id, "start")
        self.assertEqual([node.id for node in workflow.nodes()], ["start", "a", "end"])
        self.assertEqual([(edge.src, edge.dst) for edge in workflow.edges()],
                         [("start", "a"), ("a", "end")])

    def test_dangling_edge_raises_node_error(self):
        with self.assertRaises(NodeError) as ctx:
            WorkflowBuilder("bad").agent("a", "alpha").edge("a", "ghost").build()
        self.assertIn("悬空边", str(ctx.exception))

    def test_dangling_source_edge_raises(self):
        builder = WorkflowBuilder("bad-src").agent("a", "alpha").agent("b", "beta")
        builder.edge("ghost", "b")
        with self.assertRaises(NodeError) as ctx:
            builder.build()
        self.assertIn("悬空边", str(ctx.exception))

    def test_duplicate_node_id_raises(self):
        with self.assertRaises(NodeError) as ctx:
            WorkflowBuilder("dup").agent("a", "alpha").agent("a", "beta")
        self.assertIn("重复", str(ctx.exception))

    def test_missing_entry_raises(self):
        # 无 start 节点且有两个"根"（都没有入边）→ entry 不唯一
        builder = (WorkflowBuilder("ambiguous")
                   .agent("a", "alpha").agent("b", "beta").end("end")
                   .edge("a", "end").edge("b", "end"))
        with self.assertRaises(NodeError) as ctx:
            builder.build()
        self.assertIn("entry", str(ctx.exception).lower())

    def test_explicit_entry_must_exist(self):
        builder = WorkflowBuilder("e").agent("a", "alpha")
        with self.assertRaises(NodeError):
            builder.build(entry="ghost")

    def test_workflow_validate_reports_problems_without_raising(self):
        workflow = Workflow("w", entry="missing", nodes=[Node(id="a", agent="alpha")])
        problems = workflow.validate()
        self.assertTrue(any("entry" in item for item in problems))
        with self.assertRaises(NodeError):
            workflow.entry_node()

    def test_node_rejects_invalid_type_and_retry(self):
        with self.assertRaises(NodeError):
            Node(id="a", type="nonsense")
        with self.assertRaises(NodeError):
            Node(id="a", retry=-1)
        with self.assertRaises(NodeError):
            Node(id="")

    def test_workflow_name_required(self):
        with self.assertRaises(NodeError):
            Workflow("")
        with self.assertRaises(NodeError):
            WorkflowBuilder("")

    def test_validate_detects_illegal_on_error(self):
        workflow = Workflow("w", entry="a", nodes=[Node(id="a", agent="x", on_error="weird")])
        self.assertTrue(any("on_error" in item for item in workflow.validate()))

    def test_builder_parallel_requires_children(self):
        builder = WorkflowBuilder("p").start("start").parallel("par", []).end("end")
        builder.chain("start", "par", "end")
        with self.assertRaises(NodeError):
            builder.build()

    def test_to_dict_is_json_serializable(self):
        workflow = (
            WorkflowBuilder("j").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        json.dumps(workflow.to_dict(), ensure_ascii=False)
        json.dumps(workflow.to_mermaid(), ensure_ascii=False)


class SequentialFlowTest(unittest.TestCase):
    """语义 1/2/10/11：顺序流程、回合数、记录与终态。"""

    def _run(self, **cfg):
        alpha = fixed_agent("alpha", summary="写了代码")
        beta = fixed_agent("beta", summary="审查完成")
        workflow = (
            WorkflowBuilder("seq").start("start")
            .agent("a", "alpha").agent("b", "beta").end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        with temp_workspace("engine-seq") as ws:
            config = RunConfig(workspace=ws, max_turns=50, **cfg)
            engine = WorkflowEngine(workflow, registry(alpha, beta), config=config)
            report = engine.run()
        return engine, report, alpha, beta

    def test_success_and_records(self):
        engine, report, alpha, beta = self._run()
        self.assertEqual(report.status, "success")
        self.assertEqual(stop_reason(report), StopReason.COMPLETED)
        self.assertEqual(report.turns, 4)
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])
        self.assertEqual(statuses(report), ["ok"] * 4)
        self.assertEqual(len(alpha.calls), 1)
        self.assertEqual(len(beta.calls), 1)
        self.assertEqual(report.error, "")
        self.assertEqual(engine.state.state.status, "success")

    def test_summary_and_metrics(self):
        engine, report, _alpha, _beta = self._run()
        self.assertEqual(report.metrics["stop_label"], "正常完成")
        self.assertEqual(report.metrics["nodes"], 4)
        self.assertEqual(report.metrics["turns"], 4)
        self.assertGreater(report.messages, 0)
        self.assertEqual(report.metrics["blocking"], 0)
        self.assertEqual(engine.state.summary()["ok"], 4)

    def test_result_written_to_blackboard(self):
        engine, report, _alpha, _beta = self._run()
        stored = engine.state.get("a")
        self.assertEqual(stored["summary"], "写了代码")
        self.assertEqual(stored["kind"], "result")
        self.assertIn("a", engine.state.ns_all("results"))
        self.assertIn("b", engine.state.ns_all("results"))
        record = report.node_runs[1]
        self.assertEqual(record.agent, "alpha")
        self.assertEqual(record.summary, "写了代码")
        self.assertTrue(record.started_at)
        self.assertGreaterEqual(record.duration, 0.0)

    def test_final_snapshot_written(self):
        engine, _report, _alpha, _beta = self._run()
        final = engine.state.state.final
        self.assertEqual(final["status"], "success")
        self.assertEqual(final["workflow"], "seq")


class EdgeSelectionTest(unittest.TestCase):
    """语义 8：decision → 出边选择（顺序敏感）。"""

    def _engine(self, ws, workflow, *agents, **cfg):
        config = RunConfig(workspace=ws, **cfg)
        return WorkflowEngine(workflow, registry(*agents), config=config)

    def _branch(self, decision):
        beta = fixed_agent("beta", summary="B 分支")
        gamma = fixed_agent("gamma", summary="C 分支")
        workflow = (
            WorkflowBuilder("branch").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: AgentResult(summary="A",
                                                                     decision=decision))
            .agent("b", "beta").agent("c", "gamma").end("end")
            .edge("start", "a")
            .edge("a", "b")            # 声明在前
            .edge("a", "end")          # dst 为 end
            .edge("b", "end").edge("c", "end")
            .build()
        )
        return workflow, beta, gamma

    def test_default_decision_takes_first_plain_edge(self):
        workflow, beta, gamma = self._branch("")
        with temp_workspace("edge-default") as ws:
            report = self._engine(ws, workflow, beta, gamma).run()
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])
        self.assertEqual(len(beta.calls), 1)
        self.assertEqual(len(gamma.calls), 0)

    def test_pass_prefers_edge_to_end(self):
        workflow, beta, gamma = self._branch("pass")
        with temp_workspace("edge-pass") as ws:
            report = self._engine(ws, workflow, beta, gamma).run()
        self.assertEqual(node_ids(report), ["start", "a", "end"])
        self.assertEqual(len(beta.calls), 0)

    def test_done_prefers_edge_to_end(self):
        workflow, beta, gamma = self._branch("done")
        with temp_workspace("edge-done") as ws:
            report = self._engine(ws, workflow, beta, gamma).run()
        self.assertEqual(node_ids(report), ["start", "a", "end"])

    def test_pass_falls_back_to_last_edge_without_end_target(self):
        beta = fixed_agent("beta")
        gamma = fixed_agent("gamma")
        workflow = (
            WorkflowBuilder("branch-last").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: AgentResult(summary="A",
                                                                      decision="pass"))
            .agent("b", "beta").agent("c", "gamma").end("end")
            .edge("start", "a").edge("a", "b").edge("a", "c")
            .edge("b", "end").edge("c", "end")
            .build()
        )
        with temp_workspace("edge-last") as ws:
            report = self._engine(ws, workflow, beta, gamma).run()
        self.assertEqual(node_ids(report), ["start", "a", "c", "end"])
        self.assertEqual(len(gamma.calls), 1)
        self.assertEqual(len(beta.calls), 0)

    def test_retry_prefers_retry_edge(self):
        fixer = fixed_agent("fixer", summary="修复")
        workflow = (
            WorkflowBuilder("retry-edge").start("start")
            .gate("gate", execute=lambda node, ctx: AgentResult(summary="门禁", decision="retry"))
            .agent("fix", "fixer").end("end")
            .edge("start", "gate")
            .edge("gate", "end", when="pass")      # 声明在前
            .edge("gate", "fix", when="retry")     # 声明在后
            .edge("fix", "end")
            .build()
        )
        with temp_workspace("edge-retry") as ws:
            report = self._engine(ws, workflow, fixer, ).run()
        self.assertEqual(node_ids(report), ["start", "gate", "fix", "end"])
        self.assertEqual(len(fixer.calls), 1)

    def test_continue_without_retry_edge_takes_first_edge(self):
        beta = fixed_agent("beta")
        gamma = fixed_agent("gamma")
        workflow = (
            WorkflowBuilder("cont-edge").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: AgentResult(summary="A",
                                                                      decision="continue"))
            .agent("b", "beta").agent("c", "gamma").end("end")
            .edge("start", "a").edge("a", "b").edge("a", "c")
            .edge("b", "end").edge("c", "end")
            .build()
        )
        with temp_workspace("edge-cont") as ws:
            report = self._engine(ws, workflow, beta, gamma).run()
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])

    def test_decision_fail_uses_fail_edge(self):
        recover = fixed_agent("recover", summary="降级完成")
        workflow = (
            WorkflowBuilder("fail-edge").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: AgentResult(summary="A",
                                                                      decision="fail"))
            .agent("recover", "recover").end("end")
            .edge("start", "a")
            .edge("a", "end", when="pass")
            .edge("a", "recover", when="fail")
            .edge("recover", "end")
            .build()
        )
        with temp_workspace("edge-fail") as ws:
            report = self._engine(ws, workflow, recover).run()
        self.assertEqual(node_ids(report), ["start", "a", "recover", "end"])
        self.assertEqual(report.node_runs[1].decision, "fail")
        self.assertEqual(report.node_runs[1].status, "ok")   # 有 fail 边，不算节点失败
        self.assertEqual(report.status, "success")

    def test_unknown_decision_falls_back_to_default(self):
        beta = fixed_agent("beta")
        workflow = (
            WorkflowBuilder("unknown-dec").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: AgentResult(summary="A",
                                                                      decision="??? "))
            .agent("b", "beta").end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        with temp_workspace("edge-unknown") as ws:
            report = self._engine(ws, workflow, beta).run()
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])

    def test_next_node_override_wins(self):
        beta = fixed_agent("beta")
        gamma = fixed_agent("gamma")
        workflow = (
            WorkflowBuilder("next-node").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: NodeOutcome(next_node="c"))
            .agent("b", "beta").agent("c", "gamma").end("end")
            .edge("start", "a").edge("a", "b").edge("a", "c")
            .edge("b", "end").edge("c", "end")
            .build()
        )
        with temp_workspace("edge-next") as ws:
            report = self._engine(ws, workflow, beta, gamma).run()
        self.assertEqual(node_ids(report), ["start", "a", "c", "end"])
        self.assertEqual(len(beta.calls), 0)

    def test_invalid_next_node_fails_run(self):
        workflow = (
            WorkflowBuilder("bad-next").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: NodeOutcome(next_node="ghost"))
            .end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("edge-bad-next") as ws:
            report = self._engine(ws, workflow).run()
        self.assertEqual(report.status, "failed")
        self.assertEqual(stop_reason(report), StopReason.ERROR)
        self.assertIn("ghost", report.error)


class LoopTest(unittest.TestCase):
    """语义 7 + 出边循环：gate retry 两轮后 pass；max_iterations / max_turns。"""

    def test_gate_retry_loop_two_rounds_then_pass(self):
        rounds = {"gate": 0}
        # 第二版代码修好了阻断问题，门禁第二次判定通过。
        def fixer_run(ctx):
            return AgentResult(summary="修复第 %s 轮" % ctx.iteration, decision="continue")

        fixer = FakeAgent("fixer", fixer_run)
        workflow = (
            WorkflowBuilder("loop-review").start("start")
            .agent("code", "coder", loop_of="impl")
            .gate("gate_quality", execute=self._gate(rounds))
            .agent("fix", "fixer", loop_of="impl")
            .end("end")
            .edge("start", "code")
            .edge("code", "gate_quality")
            .edge("gate_quality", "end", when="pass")
            .edge("gate_quality", "fix", when="retry")
            .edge("fix", "code")
            .build(max_iterations=3)
        )
        coder = fixed_agent("coder", summary="生成代码", decision="continue")
        with temp_workspace("engine-loop") as ws:
            engine = WorkflowEngine(workflow, registry(coder, fixer),
                                    config=RunConfig(workspace=ws, max_iterations=3))
            report = engine.run()

        self.assertEqual(rounds["gate"], 2)
        self.assertEqual(report.status, "success")
        self.assertEqual(stop_reason(report), StopReason.COMPLETED)
        self.assertEqual(node_ids(report),
                         ["start", "code", "gate_quality", "fix", "code", "gate_quality", "end"])
        self.assertEqual(report.turns, 7)
        self.assertEqual(report.metrics["iterations"], {"impl": 3})
        self.assertEqual(engine.state.iteration("impl"), 3)
        decisions = [record.decision for record in report.node_runs]
        self.assertEqual(decisions.count("retry"), 1)
        self.assertEqual(decisions.count("pass"), 1)

    @staticmethod
    def _gate(rounds: dict):
        def gate(node, ctx):
            rounds["gate"] = rounds.get("gate", 0) + 1
            decision = "retry" if rounds["gate"] < 2 else "pass"
            return AgentResult(summary="门禁判定 %s" % decision, decision=decision)
        return gate

    def test_max_iterations_stops_partial(self):
        workflow = (
            WorkflowBuilder("self-loop")
            .agent("loop", "alpha", loop_of="L", max_iterations=2)
            .edge("loop", "loop")
            .build()
        )
        alpha = fixed_agent("alpha", summary="一轮")
        with temp_workspace("engine-maxiter") as ws:
            engine = WorkflowEngine(workflow, registry(alpha),
                                    config=RunConfig(workspace=ws, max_iterations=3))
            report = engine.run()
        self.assertEqual(report.status, "partial")
        self.assertEqual(stop_reason(report), StopReason.MAX_ITERATIONS)
        # 回合计数在停止判定前自增，因此 turns=3 而实际只执行了 2 个节点。
        self.assertEqual(report.turns, 3)
        self.assertEqual(len(report.node_runs), 2)
        self.assertEqual(report.metrics["iterations"], {"L": 3})
        self.assertEqual(len(alpha.calls), 2)
        self.assertTrue(any("循环 L" in note for note in report.notes))

    def test_config_max_iterations_used_when_node_has_none(self):
        workflow = (
            WorkflowBuilder("cfg-loop")
            .agent("loop", "alpha", loop_of="L")
            .edge("loop", "loop")
            .build()
        )
        alpha = fixed_agent("alpha")
        with temp_workspace("engine-cfg-iter") as ws:
            report = WorkflowEngine(workflow, registry(alpha),
                                    config=RunConfig(workspace=ws, max_iterations=1)).run()
        self.assertEqual(report.status, "partial")
        self.assertEqual(stop_reason(report), StopReason.MAX_ITERATIONS)
        self.assertEqual(report.metrics["iterations"], {"L": 2})
        self.assertEqual(len(alpha.calls), 1)

    def test_max_turns_truncates_to_partial(self):
        workflow = (
            WorkflowBuilder("turn-loop")
            .agent("a", "alpha").agent("b", "beta")
            .edge("a", "b").edge("b", "a")
            .build()
        )
        alpha, beta = fixed_agent("alpha"), fixed_agent("beta")
        with temp_workspace("engine-maxturns") as ws:
            report = WorkflowEngine(workflow, registry(alpha, beta),
                                    config=RunConfig(workspace=ws, max_turns=3)).run()
        self.assertEqual(report.status, "partial")
        self.assertEqual(stop_reason(report), StopReason.MAX_TURNS)
        self.assertEqual(report.turns, 3)
        self.assertEqual(node_ids(report), ["a", "b", "a"])
        self.assertTrue(any("max_turns=3" in note for note in report.notes))

    def test_iteration_visible_in_context(self):
        seen = []

        def record_iteration(node, ctx):
            seen.append(ctx.iteration)
            return AgentResult(summary="第 %s 轮" % ctx.iteration)

        workflow = (
            WorkflowBuilder("iter-ctx")
            .agent("loop", "alpha", loop_of="L", execute=record_iteration)
            .edge("loop", "loop")
            .build()
        )
        with temp_workspace("engine-iter-ctx") as ws:
            WorkflowEngine(workflow, registry(),
                           config=RunConfig(workspace=ws, max_iterations=2)).run()
        self.assertEqual(seen, [1, 2])


class WhenAndSkipTest(unittest.TestCase):
    """语义 2：when 判定与 skipped 记录。"""

    def test_when_false_skips_node(self):
        beta = fixed_agent("beta", summary="不应执行")
        workflow = (
            WorkflowBuilder("skip").start("start")
            .agent("a", "alpha").agent("b", "beta", when=lambda ctx: False).end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        alpha = fixed_agent("alpha")
        with temp_workspace("engine-skip") as ws:
            engine = WorkflowEngine(workflow, registry(alpha, beta),
                                    config=RunConfig(workspace=ws))
            report = engine.run()
        self.assertEqual(len(beta.calls), 0)
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])
        self.assertEqual(report.node_runs[2].status, "skipped")
        self.assertEqual(report.node_runs[2].summary, "when 条件为 False")
        self.assertEqual(report.status, "success")
        self.assertTrue(any("被跳过" in note for note in report.notes))

    def test_when_true_executes(self):
        beta = fixed_agent("beta")
        workflow = (
            WorkflowBuilder("when-ok").start("start")
            .agent("a", "alpha").agent("b", "beta", when=lambda ctx: True).end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        with temp_workspace("engine-when-ok") as ws:
            report = WorkflowEngine(workflow, registry(fixed_agent("alpha"), beta),
                                    config=RunConfig(workspace=ws)).run()
        self.assertEqual(len(beta.calls), 1)
        self.assertEqual(report.node_runs[2].status, "ok")

    def test_when_receives_context(self):
        captured = {}

        def when(ctx):
            captured["node"] = ctx.node.id
            captured["turn"] = ctx.turn
            captured["workspace"] = ctx.workspace
            return ctx.turn >= 1

        workflow = (
            WorkflowBuilder("when-ctx").start("start")
            .agent("a", "alpha", when=when).end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-when-ctx") as ws:
            report = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws)).run()
            self.assertEqual(captured["node"], "a")
            self.assertGreaterEqual(captured["turn"], 1)
            self.assertEqual(captured["workspace"], ws)
            self.assertTrue(os.path.isdir(captured["workspace"]))
        self.assertEqual(report.status, "success")

    def test_when_exception_is_treated_as_failure(self):
        def when(ctx):
            raise RuntimeError("条件里有 bug")

        workflow = (
            WorkflowBuilder("when-crash").start("start")
            .agent("a", "alpha", when=when, on_error="continue").end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-when-crash") as ws:
            report = WorkflowEngine(workflow, registry(), config=RunConfig(workspace=ws)).run()
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertIn("RuntimeError", report.node_runs[1].error)
        self.assertEqual(report.status, "success")

    def test_node_outcome_skipped(self):
        workflow = (
            WorkflowBuilder("outcome-skip").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: NodeOutcome(status="skipped",
                                                                      error="环境不满足"))
            .end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-outcome-skip") as ws:
            report = WorkflowEngine(workflow, registry(), config=RunConfig(workspace=ws)).run()
        self.assertEqual(report.node_runs[1].status, "skipped")
        self.assertEqual(report.node_runs[1].summary, "环境不满足")


class FailureHandlingTest(unittest.TestCase):
    """语义 3/4/5：异常、重试、on_error / fallback / fail 边。"""

    def _engine(self, ws, workflow, *agents, **cfg):
        return WorkflowEngine(workflow, registry(*agents),
                              config=RunConfig(workspace=ws, **cfg))

    def test_missing_agent_fails_node(self):
        workflow = (
            WorkflowBuilder("ghost-agent").start("start")
            .agent("a", "ghost").end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-ghost") as ws:
            report = self._engine(ws, workflow).run()
        self.assertEqual(report.status, "failed")
        self.assertEqual(stop_reason(report), StopReason.ERROR)
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertIn("找不到 Agent", report.node_runs[1].error)
        self.assertIn("ghost", report.error)

    def test_agent_exception_fails_run(self):
        def boom(ctx):
            raise ValueError("boom")

        workflow = (
            WorkflowBuilder("boom").start("start")
            .agent("a", "alpha").end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-boom") as ws:
            engine = self._engine(ws, workflow, FakeAgent("alpha", boom))
            report = engine.run()
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertIn("ValueError", report.node_runs[1].error)
        self.assertIn("boom", report.error)
        self.assertIn("a", engine.state.ns_all("errors"))

    def test_on_error_continue_keeps_going(self):
        def boom(ctx):
            raise ValueError("boom")

        beta = fixed_agent("beta", summary="照常继续")
        workflow = (
            WorkflowBuilder("cont").start("start")
            .agent("a", "alpha", on_error="continue").agent("b", "beta").end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        with temp_workspace("engine-cont") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", boom), beta).run()
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertEqual(report.status, "success")
        self.assertEqual(len(beta.calls), 1)

    def test_on_error_fallback_node(self):
        def boom(ctx):
            raise ValueError("boom")

        recover = fixed_agent("recover", summary="降级处理")
        workflow = (
            WorkflowBuilder("fallback").start("start")
            .agent("a", "alpha", on_error="fallback:recover")
            .agent("recover", "recover").end("end")
            .edge("start", "a").edge("a", "end").edge("recover", "end")
            .build()
        )
        with temp_workspace("engine-fallback") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", boom), recover).run()
        self.assertEqual(node_ids(report), ["start", "a", "recover", "end"])
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertEqual(report.node_runs[2].status, "ok")
        self.assertEqual(report.status, "success")
        self.assertTrue(any("fallback" in note for note in report.notes))

    def test_on_error_fallback_missing_node_fails(self):
        def boom(ctx):
            raise ValueError("boom")

        workflow = (
            WorkflowBuilder("fallback-missing").start("start")
            .agent("a", "alpha", on_error="fallback:ghost").end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-fallback-missing") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", boom)).run()
        self.assertEqual(report.status, "failed")
        self.assertTrue(any("ghost" in note for note in report.notes))
        self.assertEqual(report.node_runs[1].status, "failed")

    def test_exception_routes_to_fail_edge(self):
        def boom(ctx):
            raise ValueError("boom")

        recover = fixed_agent("recover", summary="失败分支")
        workflow = (
            WorkflowBuilder("fail-edge-exc").start("start")
            .agent("a", "alpha").agent("recover", "recover").end("end")
            .edge("start", "a")
            .edge("a", "end", when="pass")
            .edge("a", "recover", when="fail")
            .edge("recover", "end")
            .build()
        )
        with temp_workspace("engine-fail-edge") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", boom), recover).run()
        self.assertEqual(node_ids(report), ["start", "a", "recover", "end"])
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertEqual(report.status, "success")

    def test_decision_fail_without_edge_uses_on_error(self):
        beta = fixed_agent("beta")
        workflow = (
            WorkflowBuilder("decision-fail").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: AgentResult(summary="A",
                                                                      decision="fail"),
                   on_error="continue")
            .agent("b", "beta").end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        with temp_workspace("engine-dec-fail") as ws:
            report = self._engine(ws, workflow, beta).run()
        self.assertEqual(report.node_runs[1].status, "failed")
        self.assertEqual(report.status, "success")
        self.assertEqual(node_ids(report), ["start", "a", "b", "end"])

    def test_retry_then_success_counts_attempts(self):
        attempts = {"n": 0}

        def flaky(ctx):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("第 %d 次失败" % attempts["n"])
            return AgentResult(summary="第三次成功")

        workflow = (
            WorkflowBuilder("retry-ok").start("start")
            .agent("a", "alpha", retry=2, retry_delay=0.0).end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-retry-ok") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", flaky)).run()
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(report.status, "success")
        self.assertEqual(report.node_runs[1].attempt, 3)
        self.assertEqual(report.node_runs[1].status, "ok")
        self.assertTrue(any("第 1 次重试" in note for note in report.notes))
        self.assertTrue(any("第 2 次重试" in note for note in report.notes))

    def test_retry_exhausted_reports_original_error(self):
        def always_fail(ctx):
            raise ValueError("始终失败")

        workflow = (
            WorkflowBuilder("retry-fail").start("start")
            .agent("a", "alpha", retry=1, retry_delay=0.0).end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-retry-fail") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", always_fail)).run()
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.node_runs[1].attempt, 2)
        self.assertIn("ValueError", report.node_runs[1].error)
        self.assertIn("始终失败", report.error)

    def test_retryable_llm_error_uses_retry_policy(self):
        attempts = {"n": 0}

        def flaky(ctx):
            attempts["n"] += 1
            if attempts["n"] == 1:
                from wfa.errors import LLMServerError
                raise LLMServerError("500", status=500)
            return AgentResult(summary="恢复")

        workflow = (
            WorkflowBuilder("retry-llm").start("start")
            .agent("a", "alpha", retry=1, retry_delay=0.0).end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-retry-llm") as ws:
            report = self._engine(ws, workflow, FakeAgent("alpha", flaky)).run()
        self.assertEqual(report.status, "success")
        self.assertEqual(report.node_runs[1].attempt, 2)

    def test_unsupported_return_type_fails_run(self):
        workflow = (
            WorkflowBuilder("bad-return").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: 12345)
            .end("end")
            .chain("start", "a", "end")
            .build()
        )
        with temp_workspace("engine-bad-return") as ws:
            report = self._engine(ws, workflow).run()
        self.assertEqual(report.status, "failed")
        self.assertIn("不支持", report.error)


class StopControlTest(unittest.TestCase):
    """语义 1：stop_check / abort / stop_on_blocking。"""

    def test_stop_check_aborts(self):
        state = {"calls": 0}

        def stop_check():
            state["calls"] += 1
            return state["calls"] > 1

        workflow = (
            WorkflowBuilder("stop-check").start("start")
            .agent("a", "alpha").end("end")
            .chain("start", "a", "end")
            .build()
        )
        alpha = fixed_agent("alpha")
        with temp_workspace("engine-stop") as ws:
            engine = WorkflowEngine(workflow, registry(alpha),
                                    config=RunConfig(workspace=ws), stop_check=stop_check)
            report = engine.run()
        self.assertEqual(report.status, "aborted")
        self.assertEqual(stop_reason(report), StopReason.ABORTED)
        self.assertEqual(len(report.node_runs), 1)
        self.assertEqual(len(alpha.calls), 0)          # 第二回合开始时被停止

    def test_abort_called_inside_node(self):
        holder = {}

        def agent_run(ctx):
            holder["engine"].abort()
            return AgentResult(summary="请求中断")

        workflow = (
            WorkflowBuilder("abort-mid").start("start")
            .agent("a", "alpha").agent("b", "beta").end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        beta = fixed_agent("beta")
        with temp_workspace("engine-abort") as ws:
            engine = WorkflowEngine(workflow, registry(FakeAgent("alpha", agent_run), beta),
                                    config=RunConfig(workspace=ws))
            holder["engine"] = engine
            report = engine.run()
        self.assertEqual(report.status, "aborted")
        self.assertEqual(node_ids(report), ["start", "a"])
        self.assertEqual(len(beta.calls), 0)

    def test_stop_check_exception_treated_as_stop(self):
        def stop_check():
            raise RuntimeError("回调坏了")

        workflow = (
            WorkflowBuilder("stop-err").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        with temp_workspace("engine-stop-err") as ws:
            report = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws),
                                    stop_check=stop_check).run()
        self.assertEqual(report.status, "aborted")

    def _blocking_workflow(self, *, with_end: bool):
        reviewer = fixed_agent(
            "reviewer", kind="review", summary="发现阻断问题",
            data={"issues": [{"id": "i1", "severity": "high"}],
                  "counts": {"high": 1}, "blocking": [{"id": "i1"}], "verdict": "block"},
        )
        builder = (WorkflowBuilder("blocking").start("start")
                   .agent("review", "reviewer"))
        if with_end:
            builder = builder.end("end").chain("start", "review", "end")
        else:
            builder = builder.edge("start", "review")
        return builder.build(), reviewer

    def test_blocking_with_end_is_success(self):
        workflow, reviewer = self._blocking_workflow(with_end=True)
        with temp_workspace("engine-blocking-ok") as ws:
            engine = WorkflowEngine(workflow, registry(reviewer),
                                    config=RunConfig(workspace=ws, stop_on_blocking=True))
            report = engine.run()
        self.assertEqual(report.status, "success")
        self.assertEqual(stop_reason(report), StopReason.COMPLETED)
        self.assertEqual(report.metrics["blocking"], 1)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.counts, {"high": 1})
        self.assertTrue(any("阻断级问题" in note for note in report.notes))

    def test_blocking_without_next_node_stops_partial(self):
        workflow, reviewer = self._blocking_workflow(with_end=False)
        with temp_workspace("engine-blocking-stop") as ws:
            engine = WorkflowEngine(workflow, registry(reviewer),
                                    config=RunConfig(workspace=ws, stop_on_blocking=True))
            report = engine.run()
        self.assertEqual(report.status, "partial")
        self.assertEqual(stop_reason(report), StopReason.STOP)
        self.assertEqual(node_ids(report), ["start", "review"])
        self.assertEqual(report.metrics["blocking"], 1)

    def test_blocking_stop_disabled_completes(self):
        workflow, reviewer = self._blocking_workflow(with_end=False)
        with temp_workspace("engine-blocking-off") as ws:
            report = WorkflowEngine(workflow, registry(reviewer),
                                    config=RunConfig(workspace=ws,
                                                     stop_on_blocking=False)).run()
        self.assertEqual(report.status, "success")
        self.assertEqual(report.metrics["stop_on_blocking"], False)
        self.assertEqual(report.metrics["blocking"], 1)


class BlackboardContractTest(unittest.TestCase):
    """语义 6：review / fix 结果写黑板的约定键。"""

    def test_review_and_fix_keys(self):
        reviewer = fixed_agent(
            "reviewer", kind="review", summary="审查完成",
            data={"issues": [{"id": "SEC001-x", "severity": "high"}],
                  "counts": {"high": 1}, "blocking": [{"id": "SEC001-x"}],
                  "verdict": "request_changes"},
        )
        fixer = fixed_agent(
            "fixer", kind="fix", summary="修复完成",
            data={"patches": [{"file": "a.py", "issue_id": "SEC001-x", "summary": "修好了"}],
                  "fixed": ["SEC001-x"], "remaining": []},
        )
        inspected = {}

        def inspect(ctx):
            inspected["fixes"] = ctx.state.get("fixes")
            inspected["verdict"] = ctx.state.get("verdict")
            inspected["issues"] = ctx.state.get("issues")
            inspected["issue_counts"] = ctx.state.get("issue_counts")
            inspected["blocking"] = ctx.state.get("blocking")
            inspected["results"] = sorted(ctx.state.ns_all("results"))
            return AgentResult(summary="检查完成")

        workflow = (
            WorkflowBuilder("bb").start("start")
            .agent("review", "reviewer").agent("fix", "fixer").agent("check", "checker")
            .end("end")
            .chain("start", "review", "fix", "check", "end")
            .build()
        )
        with temp_workspace("engine-bb") as ws:
            engine = WorkflowEngine(workflow, registry(reviewer, fixer,
                                                      FakeAgent("checker", inspect)),
                                    config=RunConfig(workspace=ws))
            report = engine.run()

        self.assertEqual(inspected["verdict"], "request_changes")
        self.assertEqual(inspected["issue_counts"], {"high": 1})
        self.assertEqual(len(inspected["issues"]), 1)
        self.assertEqual(len(inspected["blocking"]), 1)
        self.assertEqual(inspected["fixes"], [{"file": "a.py", "issue_id": "SEC001-x",
                                              "summary": "修好了"}])
        self.assertEqual(inspected["results"], ["fix", "review", "start"])
        self.assertEqual(report.issues[0]["id"], "SEC001-x")
        self.assertEqual(report.counts, {"high": 1})
        self.assertEqual(engine.state.get("verdict"), "request_changes")

    def test_artifacts_and_metrics_recorded(self):
        producer = fixed_agent("writer", kind="doc", summary="写了文档",
                               artifacts={"README.md": "# 说明"},
                               metrics={"chars": 5})
        workflow = (
            WorkflowBuilder("art").start("start").agent("doc", "writer").end("end")
            .chain("start", "doc", "end").build()
        )
        with temp_workspace("engine-art") as ws:
            engine = WorkflowEngine(workflow, registry(producer),
                                    config=RunConfig(workspace=ws))
            report = engine.run()
        self.assertIn("README.md", report.artifacts)
        self.assertEqual(report.artifacts["README.md"]["producer"], "doc")
        self.assertEqual(engine.state.ns_get("metrics", "doc"), {"chars": 5})

    def test_extra_payload_from_node_outcome(self):
        writer = fixed_agent("writer", kind="doc", summary="写完")
        workflow = (
            WorkflowBuilder("extra").start("start")
            .agent("a", "alpha", execute=lambda node, ctx: NodeOutcome(
                payload={"kind": "doc", "summary": "带载荷", "decision": "continue",
                         "custom": 42}))
            .agent("doc", "writer").end("end")
            .chain("start", "a", "doc", "end").build()
        )
        with temp_workspace("engine-extra") as ws:
            engine = WorkflowEngine(workflow, registry(writer),
                                    config=RunConfig(workspace=ws))
            report = engine.run()
        self.assertEqual(engine.state.get("custom"), 42)
        self.assertEqual(report.node_runs[1].kind, "doc")
        self.assertEqual(report.node_runs[1].summary, "带载荷")

    def test_parallel_previous_run_lookup(self):
        called = {}

        def downstream(ctx):
            called["payload"] = sorted((ctx.payload("review", "children") or {}).keys())
            called["ns"] = sorted(ctx.state.ns_all("review").keys())
            called["first"] = sorted((ctx.first_payload("children") or {}).keys())
            called["preds"] = ctx.predecessors()
            return AgentResult(summary="聚合完成")

        def child_execute(child_id):
            def execute(node, ctx):
                return AgentResult(summary=child_id, data={"cid": child_id})
            return execute

        children = [Node(id=cid, execute=child_execute(cid)) for cid in ("sec", "maint")]
        workflow = (
            WorkflowBuilder("par-read").start("start")
            .parallel("review", children)
            .agent("down", "downstream")
            .end("end")
            .chain("start", "review", "down", "end")
            .build()
        )
        with temp_workspace("engine-par-read") as ws:
            WorkflowEngine(workflow, registry(FakeAgent("downstream", downstream)),
                           config=RunConfig(workspace=ws)).run()
        self.assertEqual(called["payload"], ["maint", "sec"])
        self.assertEqual(called["ns"], ["maint", "sec"])
        self.assertEqual(called["first"], ["maint", "sec"])
        self.assertEqual(called["preds"], ["review"])


class ParallelTest(unittest.TestCase):
    """语义 9：并行节点真并发、子结果合并、失败隔离。"""

    def _slow_child(self, name, delay=0.2):
        def execute(node, ctx):
            record = {"node": name, "start": time.monotonic(), "end": 0.0}
            with self._lock:
                self.spans[name] = record
            time.sleep(delay)
            record["end"] = time.monotonic()
            return AgentResult(summary="%s 完成" % name, data={"node": name})
        return execute

    def setUp(self):
        self._lock = threading.Lock()
        self.spans = {}

    def test_children_run_concurrently(self):
        children = [Node(id=cid, execute=self._slow_child(cid)) for cid in ("c1", "c2", "c3")]
        extras_seen = {}

        def parent_execute(node, ctx):
            extras_seen["children"] = sorted((ctx.extras.get("children") or {}).keys())
            return AgentResult(summary="并行审查汇总", decision="pass")

        workflow = (
            WorkflowBuilder("par").start("start")
            .parallel("par_review", children, execute=parent_execute, description="并行审查")
            .agent("down", "downstream").end("end")
            .chain("start", "par_review", "down", "end")
            .build()
        )
        events = []
        downstream = fixed_agent("downstream", summary="下游")
        with temp_workspace("engine-parallel") as ws:
            engine = WorkflowEngine(workflow, registry(downstream),
                                    config=RunConfig(workspace=ws, parallel_workers=3),
                                    on_event=lambda name, data: events.append((name, data)))
            started = time.monotonic()
            report = engine.run()
            wall = time.monotonic() - started

        self.assertEqual(report.status, "success")
        self.assertEqual(len(downstream.calls), 1)
        self.assertLess(wall, 0.55, "三个 0.2s 子节点必须并发（串行需 > 0.6s）")
        self.assertEqual(extras_seen["children"], ["c1", "c2", "c3"])

        # 三种读取方式之一：state.ns_all(parallel_id)
        ns = engine.state.ns_all("par_review")
        self.assertEqual(sorted(ns), ["c1", "c2", "c3"])
        # 父节点结果 data["children"]
        self.assertEqual(sorted(engine.state.get("par_review")["data"]["children"]),
                         ["c1", "c2", "c3"])

        names = [name for name, _data in events]
        self.assertIn("parallel_start", names)
        self.assertIn("parallel_end", names)
        self.assertIn("c1", [data.get("node") for name, data in events if name == "node_end"])

        parallel_end = [data for name, data in events if name == "parallel_end"][0]
        timings = parallel_end["timings"]
        self.assertEqual(sorted(timings), ["c1", "c2", "c3"])
        starts = [item["start"] for item in timings.values()]
        ends = [item["end"] for item in timings.values()]
        self.assertGreater(min(ends), max(starts), "子节点时间区间必须重叠（真并发）")
        for item in timings.values():
            self.assertGreaterEqual(item["duration"], 0.15)

    def test_parallel_child_failure_is_isolated(self):
        def boom(node, ctx):
            raise RuntimeError("子节点崩了")

        children = [Node(id="ok", execute=lambda node, ctx: AgentResult(summary="ok")),
                    Node(id="bad", execute=boom)]
        workflow = (
            WorkflowBuilder("par-fail").start("start")
            .parallel("par", children).end("end")
            .chain("start", "par", "end")
            .build()
        )
        with temp_workspace("engine-par-fail") as ws:
            engine = WorkflowEngine(workflow, registry(),
                                    config=RunConfig(workspace=ws, parallel_workers=2))
            report = engine.run()
        self.assertEqual(report.status, "success")
        stored = engine.state.get("par")
        self.assertEqual(sorted(stored["data"]["children"]), ["ok"])
        self.assertIn("bad", stored["data"]["errors"])
        self.assertEqual(stored["decision"], "continue")
        self.assertIn("bad", report.node_runs[1].result["data"]["errors"])

    def test_all_parallel_children_fail_makes_node_fail(self):
        def boom(node, ctx):
            raise RuntimeError("全崩")

        children = [Node(id="b1", execute=boom), Node(id="b2", execute=boom)]
        workflow = (
            WorkflowBuilder("par-allfail").start("start")
            .parallel("par", children).end("end")
            .chain("start", "par", "end")
            .build()
        )
        with temp_workspace("engine-par-allfail") as ws:
            report = WorkflowEngine(workflow, registry(),
                                    config=RunConfig(workspace=ws)).run()
        self.assertEqual(report.status, "failed")
        self.assertIn("决策为 fail", report.error)

    def test_parallel_event_pair_present(self):
        children = [Node(id="c", execute=lambda node, ctx: AgentResult(summary="c"))]
        workflow = (
            WorkflowBuilder("par-evt").start("start")
            .parallel("par", children).end("end")
            .chain("start", "par", "end").build()
        )
        with temp_workspace("engine-par-evt") as ws:
            engine = WorkflowEngine(workflow, registry(), config=RunConfig(workspace=ws))
            engine.run()
        names = [event["event"] for event in engine.events]
        self.assertLess(names.index("parallel_start"), names.index("parallel_end"))


class EventStreamTest(unittest.TestCase):
    """语义 10：事件序列与 EVENTS 常量。"""

    def test_event_constants(self):
        self.assertEqual(len(EVENTS), 8)
        for name in ("turn_start", "turn_end", "node_start", "node_end", "node_failed",
                     "stop", "parallel_start", "parallel_end"):
            self.assertIn(name, EVENTS)

    def test_sequential_event_sequence(self):
        workflow = (
            WorkflowBuilder("evt").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        seen = []
        with temp_workspace("engine-evt") as ws:
            engine = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws),
                                    on_event=lambda name, data: seen.append(name))
            engine.run()
        expected = ["turn_start", "node_start", "node_end", "turn_end",
                    "turn_start", "node_start", "node_end", "turn_end",
                    "turn_start", "node_start", "node_end", "turn_end",
                    "stop"]
        self.assertEqual(seen, expected)
        self.assertEqual([event["event"] for event in engine.events], expected)
        self.assertTrue(all(event["seq"] == index
                            for index, event in enumerate(engine.events, 1)))
        self.assertTrue(all(event["event"] in EVENTS for event in engine.events))

    def test_node_failed_event_on_failure(self):
        def boom(ctx):
            raise ValueError("x")

        workflow = (
            WorkflowBuilder("evt-fail").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        with temp_workspace("engine-evt-fail") as ws:
            engine = WorkflowEngine(workflow, registry(FakeAgent("alpha", boom)),
                                    config=RunConfig(workspace=ws))
            engine.run()
        failed = [event["data"]["node"] for event in engine.events
                  if event["event"] == "node_failed"]
        finished = [event["data"]["node"] for event in engine.events
                    if event["event"] == "node_end"]
        self.assertEqual(failed, ["a"])
        self.assertNotIn("a", finished)

    def test_event_callback_exception_does_not_break_run(self):
        def on_event(name, data):
            raise RuntimeError("UI 回调坏了")

        workflow = (
            WorkflowBuilder("evt-cb").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        with temp_workspace("engine-evt-cb") as ws:
            report = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws),
                                    on_event=on_event).run()
        self.assertEqual(report.status, "success")

    def test_bus_receives_system_messages(self):
        bus = MessageBus()
        self.assertFalse(bool(bus))          # 空 MessageBus 因 __len__ 为假值
        workflow = (
            WorkflowBuilder("evt-bus").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        with temp_workspace("engine-evt-bus") as ws:
            engine = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws), bus=bus)
            engine.run()
        self.assertIs(engine.bus, bus)
        self.assertGreater(len(bus.messages), 0)
        self.assertTrue(all(message.kind == "system" for message in bus.messages))
        lines = bus.log_lines()
        self.assertTrue(any("run_start" in line for line in lines))
        self.assertTrue(any("run_stop" in line for line in lines))
        self.assertIn("run_start", lines[0])

    def test_empty_bus_is_adopted_not_replaced(self):
        """回归：显式传入的**空** MessageBus 必须被采用（历史上被 `bus or ...` 丢弃）。"""
        bus = MessageBus()
        self.assertFalse(bool(bus))
        workflow = (
            WorkflowBuilder("evt-empty-bus").start("start").agent("a", "alpha").end("end")
            .chain("start", "a", "end").build()
        )
        with temp_workspace("engine-empty-bus") as ws:
            engine = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws), bus=bus)
            report = engine.run()
        self.assertIs(engine.bus, bus)
        self.assertGreater(len(bus.messages), 1)
        # 报告里的消息数是终态汇总那一刻的快照，不含随后的 run_stop 消息。
        self.assertEqual(report.messages, len(bus.messages) - 1)
        self.assertEqual(engine.state.bus, bus)

    def test_state_bus_used_when_no_bus_argument(self):
        """只传 state 时，引擎应复用黑板上挂的总线。"""
        bus = MessageBus()
        bus.send("system", PERF_ID, BROADCAST, {"text": "预置"})
        with temp_workspace("engine-state-bus") as ws:
            board = Blackboard(workspace=ws, bus=bus)
            workflow = (
                WorkflowBuilder("evt-state-bus").start("start").agent("a", "alpha").end("end")
                .chain("start", "a", "end").build()
            )
            engine = WorkflowEngine(workflow, registry(fixed_agent("alpha")),
                                    config=RunConfig(workspace=ws), state=board)
            engine.run()
        self.assertIs(engine.bus, bus)


class CheckpointResumeTest(unittest.TestCase):
    """语义 12：checkpoint → resume。"""

    def _workflow_and_agents(self):
        alpha = fixed_agent("alpha", summary="A 完成")
        beta = fixed_agent("beta", summary="B 完成")
        gamma = fixed_agent("gamma", summary="C 完成")
        workflow = (
            WorkflowBuilder("resume").start("start")
            .agent("a", "alpha").agent("b", "beta").agent("c", "gamma").end("end")
            .chain("start", "a", "b", "c", "end")
            .build()
        )
        return workflow, alpha, beta, gamma

    def test_checkpoint_then_resume(self):
        workflow, alpha, beta, gamma = self._workflow_and_agents()
        agents = registry(alpha, beta, gamma)
        with temp_workspace("engine-ckpt") as ws:
            path = os.path.join(ws, "ckpt", "checkpoint.json")
            engine = WorkflowEngine(workflow, agents,
                                    config=RunConfig(workspace=ws, max_turns=3))
            first = engine.run()
            self.assertEqual(first.status, "partial")
            self.assertEqual(stop_reason(first), StopReason.MAX_TURNS)
            self.assertEqual(node_ids(first), ["start", "a", "b"])
            self.assertEqual(first.turns, 3)

            written = engine.checkpoint(path)
            self.assertEqual(written, os.path.abspath(path))
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            for key in ("workflow", "config", "blackboard", "bus", "cursor"):
                self.assertIn(key, raw)
            self.assertEqual(raw["cursor"]["next_node"], "c")
            self.assertEqual(raw["cursor"]["last_node"], "b")
            self.assertNotIn("api_key", json.dumps(raw, ensure_ascii=False))

            engine2, second = WorkflowEngine.resume(path, agents, workflow=workflow)
            self.assertEqual(second.notes[0], "已从检查点恢复")
            self.assertTrue(any("重置" in note for note in second.notes))
            self.assertEqual(second.status, "success")
            self.assertEqual(node_ids(second), ["start", "a", "b", "c", "end"])
            # 回合预算按"单次运行"计：恢复后只跑了 c、end 两个回合（历史记录保留）。
            self.assertEqual(second.turns, 2)
            self.assertEqual(len(alpha.calls), 1)          # a 不重跑
            self.assertEqual(len(beta.calls), 1)
            self.assertEqual(len(gamma.calls), 1)
            self.assertIsInstance(engine2.state, Blackboard)
            self.assertIsNotNone(engine2.state.get("a"))   # 黑板已恢复

    def test_checkpoint_without_workflow_object_needs_executors(self):
        """不传 workflow 对象时，execute/when 无法序列化 → resume 会给出提示备注。"""
        def custom(node, ctx):
            return AgentResult(summary="自定义")

        workflow = (
            WorkflowBuilder("resume-exec").start("start")
            .agent("a", "alpha").agent("x", "", execute=custom).end("end")
            .chain("start", "a", "x", "end").build()
        )
        alpha = fixed_agent("alpha")
        agents = registry(alpha)
        with temp_workspace("engine-ckpt-exec") as ws:
            path = os.path.join(ws, "ckpt.json")
            engine = WorkflowEngine(workflow, agents,
                                    config=RunConfig(workspace=ws, max_turns=2))
            engine.run()
            engine.checkpoint(path)

            remounted = {"called": 0}

            def remount(node, ctx):
                remounted["called"] += 1
                return AgentResult(summary="重挂后执行")

            _engine2, report = WorkflowEngine.resume(
                path, agents, executors={"x": remount}, conditions={})
        self.assertTrue(any("已从检查点恢复" in note for note in report.notes))
        self.assertEqual(remounted["called"], 1)
        self.assertEqual(report.status, "success")

    def test_resume_resets_turn_budget(self):
        """检查点 max_turns=2，恢复时不传 config 也能跑到 success（预算是单次运行的）。

        若回合预算仍按累积计算（旧行为），恢复后 state.turn 已是 2，第一个回合就会被
        max_turns 截断成 partial；这里用"剩余 2 个节点 ≤ max_turns=2"来区分两种语义。
        """
        a1 = fixed_agent("alpha", summary="A")
        b1 = fixed_agent("beta", summary="B")
        workflow = (
            WorkflowBuilder("budget").start("start")
            .agent("a", "alpha").agent("b", "beta").end("end")
            .chain("start", "a", "b", "end")
            .build()
        )
        agents = registry(a1, b1)
        with temp_workspace("engine-ckpt-budget") as ws:
            path = os.path.join(ws, "ckpt.json")
            engine = WorkflowEngine(workflow, agents,
                                    config=RunConfig(workspace=ws, max_turns=2))
            first = engine.run()
            self.assertEqual(first.status, "partial")
            self.assertEqual(first.turns, 2)
            self.assertEqual(node_ids(first), ["start", "a"])
            engine.checkpoint(path)

            _engine2, second = WorkflowEngine.resume(path, agents, workflow=workflow)
        self.assertEqual(second.status, "success")
        self.assertEqual(second.turns, 2)                    # 只算本次运行（b、end）
        self.assertEqual(node_ids(second), ["start", "a", "b", "end"])
        self.assertEqual(len(a1.calls), 1)
        self.assertEqual(len(b1.calls), 1)
        self.assertTrue(any("重置" in note for note in second.notes))

    def test_resume_with_explicit_config_keeps_it(self):
        workflow, alpha, beta, gamma = self._workflow_and_agents()
        agents = registry(alpha, beta, gamma)
        with temp_workspace("engine-ckpt-cfg") as ws:
            path = os.path.join(ws, "ckpt.json")
            engine = WorkflowEngine(workflow, agents,
                                    config=RunConfig(workspace=ws, max_turns=2))
            engine.run()
            engine.checkpoint(path)
            _engine2, second = WorkflowEngine.resume(
                path, agents, workflow=workflow,
                config=RunConfig(workspace=ws, max_turns=1))
        # 显式传入的 max_turns=1 生效：恢复后第一个回合即被截断。
        self.assertEqual(second.status, "partial")
        self.assertEqual(stop_reason(second), StopReason.MAX_TURNS)

    def test_resume_from_explicit_node(self):
        workflow, alpha, beta, gamma = self._workflow_and_agents()
        with temp_workspace("engine-resume-from") as ws:
            engine = WorkflowEngine(workflow, registry(alpha, beta, gamma),
                                    config=RunConfig(workspace=ws))
            report = engine.run(resume_from="b")
        self.assertEqual(node_ids(report), ["b", "c", "end"])
        self.assertEqual(len(alpha.calls), 0)
        self.assertTrue(any("从节点 b 继续执行" in note for note in report.notes))

    def test_resume_from_unknown_node_fails(self):
        workflow, alpha, beta, gamma = self._workflow_and_agents()
        with temp_workspace("engine-resume-bad") as ws:
            engine = WorkflowEngine(workflow, registry(alpha, beta, gamma),
                                    config=RunConfig(workspace=ws))
            report = engine.run(resume_from="ghost")
        self.assertEqual(report.status, "failed")
        self.assertIn("ghost", report.error)


class ConfigAndReportTest(unittest.TestCase):
    """RunConfig / RunReport 的序列化与安全性。"""

    def test_run_config_roundtrip(self):
        config = RunConfig(task="写代码", workspace="C:/ws", mode="online",
                           model="deepseek-chat", max_turns=7, max_iterations=2,
                           review_max_steps=3, stop_on_blocking=False, verbose=True,
                           parallel_workers=4, extra={"workflow": "code_review"})
        restored = RunConfig.from_dict(config.to_dict())
        self.assertEqual(restored, config)

    def test_run_config_to_dict_drops_secrets_and_objects(self):
        config = RunConfig(extra={
            "api_key": "sk-secret", "DEEPSEEK_TOKEN": "t", "password": "p",
            "model": object(), "registry": object(), "note": "保留我",
        })
        extra = config.to_dict()["extra"]
        self.assertEqual(extra, {"note": "保留我"})
        text = json.dumps(config.to_dict(), ensure_ascii=False)
        self.assertNotIn("sk-secret", text)
        self.assertNotIn("DEEPSEEK_TOKEN", text)

    def test_run_config_from_dict_ignores_unknown(self):
        config = RunConfig.from_dict({"task": "t", "unknown": 1, "extra": "bad"})
        self.assertEqual(config.task, "t")
        self.assertEqual(config.extra, {})

    def test_run_report_roundtrip_and_markdown(self):
        report = RunReport(run_id="run-1", workflow="code_review", status="partial",
                           turns=3, duration=1.5, messages=4,
                           issues=[{"title": "问题", "severity": "high", "file": "a.py",
                                    "line": 3, "detail": "细节"}],
                           counts={"high": 1}, artifacts={"r.md": {"kind": "report",
                                                                   "producer": "doc"}},
                           metrics={"stop_reason": "max_turns", "iterations": {"impl": 2}},
                           notes=["备注一"], error="")
        payload = report.to_dict()
        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["messages"], 4)
        text = report.to_markdown()
        self.assertIn("# 运行报告：code_review", text)
        self.assertIn("## 节点执行", text)
        self.assertIn("## 问题（issues：1）", text)
        self.assertIn("## 产物（artifacts：1）", text)
        self.assertIn("max_turns", text)
        self.assertIn("备注一", text)

    def test_stop_reason_labels_complete(self):
        for reason in StopReason.ALL:
            self.assertIn(reason, StopReason.LABELS)
            self.assertTrue(StopReason.LABELS[reason])

    def test_report_via_render_run_report_accepts_runreport_shape(self):
        """回归：engine 的 RunReport.to_dict() 里 messages 是 int（条数）。

        ``render_run_report`` 必须同时兼容"消息列表"与"消息条数"两种形态，
        否则 CLI 的 ``run → write_report`` 主路径会在渲染时崩掉。
        """
        from wfa.report import render_run_report

        payload = RunReport(run_id="run-2", workflow="code_review", status="success",
                            turns=2, messages=12).to_dict()
        text = render_run_report(payload)
        self.assertIn("| 消息数 | 12 |", text)
        self.assertIn("### 4.2 消息总线", text)


class RouteNodeTest(unittest.TestCase):
    """``type=="route"`` 节点的分支选择（显式 next_node / 默认边 / decision）。"""

    def _workflow(self, router):
        return (
            WorkflowBuilder("route-flow").start("start")
            .route("router", execute=router, description="路由节点")
            .agent("a", "alpha").agent("b", "beta").end("end")
            .edge("start", "router")
            .edge("router", "a")               # 默认边（when 为空）
            .edge("router", "b", when="alt")   # 备选边
            .edge("a", "end").edge("b", "end")
            .build()
        )

    def _run(self, ws, router, state_value=None):
        alpha = fixed_agent("alpha", summary="A 分支")
        beta = fixed_agent("beta", summary="B 分支")
        board = Blackboard(workspace=ws)
        if state_value is not None:
            board.set("target", state_value)
        engine = WorkflowEngine(self._workflow(router), registry(alpha, beta),
                                config=RunConfig(workspace=ws), state=board)
        return engine.run(), alpha, beta

    def _router(self, node, ctx):
        target = ctx.state.get("target") or ""
        if target:
            return NodeOutcome(next_node=target)
        return AgentResult(summary="无目标，走默认边")

    def test_route_node_type_and_validation(self):
        workflow = self._workflow(self._router)
        self.assertEqual(workflow.node("router").type, "route")
        self.assertEqual(workflow.validate(), [])

    def test_route_default_edge_when_no_override(self):
        with temp_workspace("engine-route-default") as ws:
            report, alpha, beta = self._run(ws, self._router)
        self.assertEqual(node_ids(report), ["start", "router", "a", "end"])
        self.assertEqual(len(alpha.calls), 1)
        self.assertEqual(len(beta.calls), 0)

    def test_route_next_node_override_selects_each_branch(self):
        for target, expected in (("a", "a"), ("b", "b")):
            with self.subTest(target=target):
                with temp_workspace("engine-route-%s" % target) as ws:
                    report, alpha, beta = self._run(ws, self._router, state_value=target)
                self.assertEqual(node_ids(report), ["start", "router", expected, "end"])
                self.assertEqual(len(alpha.calls), 1 if target == "a" else 0)
                self.assertEqual(len(beta.calls), 1 if target == "b" else 0)

    def test_route_decision_pass_falls_back_to_last_edge(self):
        def router(node, ctx):
            return AgentResult(summary="通过", decision="pass")

        with temp_workspace("engine-route-pass") as ws:
            report, alpha, beta = self._run(ws, router)
        # router 的两条出边都没有 dst 为 end 的边 → 按"最后一条边"规则走 b
        self.assertEqual(node_ids(report), ["start", "router", "b", "end"])
        self.assertEqual(len(beta.calls), 1)

    def test_route_invalid_next_node_fails_run(self):
        def router(node, ctx):
            return NodeOutcome(next_node="ghost")

        with temp_workspace("engine-route-bad") as ws:
            report, _alpha, _beta = self._run(ws, router)
        self.assertEqual(report.status, "failed")
        self.assertIn("ghost", report.error)

    def test_route_is_exercised_by_real_workflows(self):
        # 预置流程里的 report / publish 都是 route 节点，这里顺带确认类型没被改坏。
        from wfa.workflows.code_review import REPORT, build_code_review_workflow
        from wfa.workflows.docs import PUBLISH, build_docs_workflow

        self.assertEqual(build_code_review_workflow().node(REPORT).type, "route")
        self.assertEqual(build_docs_workflow().node(PUBLISH).type, "route")


class ContextHelperTest(unittest.TestCase):
    """NodeContext / AgentResult / NodeOutcome 的小工具。"""

    def setUp(self):
        self.board = Blackboard()
        self.bus = MessageBus()
        self.result = AgentResult(kind="review", summary="结论", decision="pass",
                                  data={"issues": [1], "counts": {"high": 1}})
        self.ctx = NodeContext(node=Node(id="n"), state=self.board, bus=self.bus,
                               inputs={"up": self.result, "plain": {"data": {"issues": [2]}}},
                               iteration=2, turn=3, workspace=self.board.workspace,
                               config=RunConfig(), extras={})

    def test_input_accessors(self):
        self.assertIs(self.ctx.input_result("up"), self.result)
        self.assertIsInstance(self.ctx.input_result("plain"), AgentResult)
        self.assertIsNone(self.ctx.input_result("missing"))
        self.assertEqual(self.ctx.input_result("missing", self.result), self.result)
        self.assertEqual(self.ctx.payload("up", "issues"), [1])
        self.assertEqual(self.ctx.payload("plain", "issues"), [2])
        self.assertIsNone(self.ctx.payload("up", "nope"))
        self.assertEqual(self.ctx.first_payload("issues"), [1])
        self.assertEqual(sorted(self.ctx.all_payloads("issues")), ["plain", "up"])
        self.assertEqual(self.ctx.predecessors(), ["up", "plain"])

    def test_blackboard_shortcuts_and_log(self):
        self.board.set("k", "v")
        self.assertEqual(self.ctx.blackboard_get("k"), "v")
        self.assertIsNone(self.ctx.blackboard_get("missing"))
        self.ctx.log("一句日志")          # 只写 logging，不应抛异常

    def test_agent_result_roundtrip(self):
        self.assertEqual(AgentResult.from_dict(self.result.to_dict()), self.result)
        self.assertTrue(self.result.ok)
        self.assertFalse(AgentResult(decision="fail").ok)
        json.dumps(self.result.to_dict(), ensure_ascii=False)

    def test_agent_result_from_dict_tolerates_garbage(self):
        result = AgentResult.from_dict({"data": None, "notes": None})
        self.assertEqual(result.data, {})
        self.assertEqual(result.notes, [])
        self.assertEqual(AgentResult.from_dict(None).kind, "result")

    def test_node_outcome(self):
        outcome = NodeOutcome(next_node="x", status="ok", payload={"a": 1})
        self.assertTrue(outcome)
        self.assertEqual(outcome.to_dict()["next_node"], "x")
        self.assertFalse(NodeOutcome(status="failed"))
        self.assertFalse(NodeOutcome(status="skipped"))

    def test_workflow_engine_requires_workflow(self):
        with self.assertRaises(NodeError):
            WorkflowEngine(None, registry())


if __name__ == "__main__":
    unittest.main()
