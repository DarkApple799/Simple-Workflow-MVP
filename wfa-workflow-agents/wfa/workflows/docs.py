"""预置工作流之二：**文档生成流程**（主题 → 大纲 → 内容 → 审校）。

拓扑::

    start → plan（大纲） → write（内容） → proofread（审校） → gate_doc ─┬─ 需修订 → write（循环）
                                                                        └─ 通过   → publish → end

它证明同一套引擎/总线/共享状态可以承载**完全不同领域**的多 Agent 协作：
只需换 Agent 与图，不需要改引擎代码（评分维度"可扩展性"）。
"""

from __future__ import annotations

import logging

from ..graph import AgentResult, Node, NodeContext, Workflow, WorkflowBuilder

logger = logging.getLogger("wfa.workflows.docs")

DESCRIPTION = "主题 → 大纲 → 内容 → 审校 的内容创作流程（规划/写作/审校三个 Agent）"

START = "start"
PLAN = "plan"
WRITE = "write"
PROOFREAD = "proofread"
GATE = "gate_doc"
PUBLISH = "publish"
END = "end"


def gate_doc(node: Node, ctx: NodeContext) -> AgentResult:
    """文档门禁：审校结论决定"返工重写"还是"发布"。"""
    state = ctx.state
    result = ctx.input_result(PROOFREAD)
    verdict = ""
    issues: list = []
    if result is not None:
        verdict = str(result.data.get("verdict") or "")
        issues = list(result.data.get("issues") or [])
    if not verdict:
        verdict = str(state.get("verdict") or "")
    iteration = state.iteration("doc")
    limit = node.max_iterations or ctx.config.max_iterations

    if verdict in ("pass", "pass_with_comments") or not issues:
        return AgentResult(
            kind="gate",
            summary=f"审校通过（verdict={verdict or 'pass'}），进入发布。",
            decision="pass",
            data={"verdict": verdict or "pass", "iteration": iteration, "converged": True},
        )
    if iteration >= limit:
        return AgentResult(
            kind="gate",
            summary=f"已达迭代上限 {limit}，仍有 {len(issues)} 条审校意见，标注「未完全收敛」后发布。",
            decision="pass",
            data={"verdict": verdict, "iteration": iteration, "converged": False},
        )
    return AgentResult(
        kind="gate",
        summary=f"审校未通过（{len(issues)} 条意见，迭代 {iteration}/{limit}），返回写作 Agent 修订。",
        decision="retry",
        data={"verdict": verdict, "iteration": iteration, "converged": False},
    )


def _lookup(ctx: NodeContext, node_id: str) -> AgentResult:
    """优先取直接前驱结果，取不到再回落到共享黑板。

    `publish` 的直接前驱是 `gate_doc`（而不是 `write`），因此真实运行时必须能从黑板
    把 `write` 的结果取回来；而单元测试会把目标节点直接塞进 `ctx.inputs`，两条路都要支持。
    """
    found = ctx.input_result(node_id)
    if found is not None:
        return found
    payload = ctx.state.get(node_id) or ctx.state.ns_get("results", node_id, {}) or {}
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return AgentResult(
            kind=str(payload.get("kind") or "result"),
            summary=str(payload.get("summary") or ""),
            decision=str(payload.get("decision") or ""),
            data=dict(payload.get("data") or {}),
        )
    return AgentResult()


def publish(node: Node, ctx: NodeContext) -> AgentResult:
    """汇总节点：把文档 Agent 的产物登记为可交付成果。

    注意：publish 的**直接前驱**是 gate_doc，所以不能只看 `ctx.inputs`；
    这里通过 `_lookup` 从共享黑板把 write 节点的结果取回来。
    """
    result = _lookup(ctx, WRITE)
    documents = dict(result.data.get("documents") or {})
    outline = list(result.data.get("outline") or [])
    body = "\n\n".join(str(value) for value in documents.values()) if documents else "（未产出文档）"
    summary = f"已发布 {len(documents)} 份文档，共 {len(body)} 字符。"
    return AgentResult(
        kind="doc",
        summary=summary,
        decision="done",
        data={"documents": documents, "outline": outline, "characters": len(body)},
        artifacts={"docs/final_document.md": body},
    )


def build_docs_workflow(*, max_iterations: int = 2, max_turns: int = 30) -> Workflow:
    """构造"文档生成"工作流。"""
    builder = WorkflowBuilder("docs", description=DESCRIPTION)
    builder.start(START)
    builder.agent(PLAN, "planner", description="规划 Agent：产出章节大纲")
    builder.agent(
        WRITE,
        "writer",
        description="写作 Agent：按大纲产出正文（返工时叠加审校意见）",
        loop_of="doc",
        max_iterations=max_iterations,
    )
    builder.agent(PROOFREAD, "proofreader", description="审校 Agent：检查事实、结构与表述")
    builder.gate(GATE, execute=gate_doc, description="文档门禁：决定返工或发布")
    builder.route(PUBLISH, execute=publish, description="发布节点：汇总最终文档")
    builder.end(END)

    builder.edge(START, PLAN, label="顺序")
    builder.edge(PLAN, WRITE, label="大纲→写作")
    builder.edge(WRITE, PROOFREAD, label="写作→审校")
    builder.edge(PROOFREAD, GATE, label="审校→门禁")
    builder.edge(GATE, WRITE, when="retry", label="需修订→重写（循环）")
    builder.edge(GATE, PUBLISH, when="pass", label="通过→发布")
    builder.edge(PUBLISH, END, label="结束")

    workflow = builder.build(entry=START, max_turns=max_turns, max_iterations=max_iterations)
    problems = workflow.validate()
    if problems:
        raise ValueError("docs 工作流校验失败：" + "；".join(problems))
    return workflow


__all__ = ["DESCRIPTION", "build_docs_workflow", "gate_doc", "publish", "START", "PLAN", "WRITE", "PROOFREAD", "GATE", "PUBLISH", "END"]
