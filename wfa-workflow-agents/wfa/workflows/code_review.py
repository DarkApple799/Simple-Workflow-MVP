"""预置工作流之一：**代码审查与修复流程**（Mini SE 流程 + 审校闭环）。

流程拓扑（与 docs/CONTRACT.md 第 10 节一致）::

    start → requirements → design → code ─┬─(parallel_review)─ parallel_review ─┬→ review
                                          └───────────────(默认)───────────────┘
    review → gate_quality ─┬─ 有阻断问题且未超迭代上限 ─→ fix ─→ review     （循环）
                           ├─ 有问题但已到上限（partial）─→ report → end
                           └─ 无阻断问题 ─→ docs（可选）→ report → end

设计要点：
    * **顺序**：requirements → design → code；
    * **条件分支**：`gate_quality` 依据审查结论决定走修复回路还是收尾；
    * **循环**：`fix → review` 构成修复迭代（`loop_of="impl"`，受 max_iterations 限制）；
    * **并行**：`parallel_review` 用两个审查 Agent 并发执行安全/可维护性两条审查线；
    * **可扩展**：节点与边全部通过 `WorkflowBuilder` 声明，新增 Agent 只需加节点。
"""

from __future__ import annotations

import logging

from .. import BLOCKING_SEVERITIES
from ..graph import AgentResult, Node, NodeContext, Workflow, WorkflowBuilder

logger = logging.getLogger("wfa.workflows.code_review")

DESCRIPTION = "编码 → 审查 → 修复 的软件工程闭环流程（含需求/设计前置与文档产出）"

#: 节点 id 常量：测试、文档与报告都引用它们，避免拼写漂移。
START = "start"
REQUIREMENTS = "requirements"
DESIGN = "design"
CODE = "code"
LEGACY_REVIEW = "legacy_review"
PARALLEL_REVIEW = "parallel_review"
SECURITY_REVIEW = "security_review"
MAINT_REVIEW = "maintainability_review"
REVIEW = "review"
GATE = "gate_quality"
FIX = "fix"
DOCS = "docs"
REPORT = "report"
END = "end"


# --------------------------------------------------------------------------- #
# 自定义节点：质量门禁 + 汇报节点
# --------------------------------------------------------------------------- #
def _blocking_issues(state) -> list:
    """从共享状态里取出当前判定为阻断级的问题。"""
    blocking = state.get("blocking")
    if isinstance(blocking, list) and blocking:
        return blocking
    issues = state.get("issues") or []
    return [item for item in issues if str(item.get("severity", "")).lower() in BLOCKING_SEVERITIES]


def _lookup(ctx: NodeContext, node_id: str):
    """优先取直接前驱结果，取不到再回落到共享黑板（跨节点读取，避免"非直接前驱读不到"）。"""
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


def gate_quality(node: Node, ctx: NodeContext) -> AgentResult:
    """质量门禁：决定"继续修复"还是"收尾"。

    决策口径（必须可解释，报告里会打印）：
        * 达到迭代上限仍有阻断问题 → ``pass``（收尾，报告标注"未完全收敛"）；
        * 有阻断问题且还有迭代额度 → ``retry``（回到修复节点）；
        * 无阻断问题 → ``pass``。
    """
    state = ctx.state
    review = _lookup(ctx, REVIEW)
    iteration = state.iteration("impl")
    limit = node.max_iterations or ctx.config.max_iterations
    blocking = list(_blocking_issues(state) or (review.data.get("blocking") or []))
    counts = state.get("issue_counts") or review.data.get("counts") or {}
    verdict = state.get("verdict") or review.data.get("verdict") or ""

    if blocking and iteration < limit:
        decision = "retry"
        summary = (
            f"门禁未通过：阻断级问题 {len(blocking)} 条（迭代 {iteration}/{limit}），"
            f"转交修复 Agent。"
        )
    elif blocking:
        decision = "pass"
        summary = (
            f"已达到迭代上限 {limit}，仍有阻断级问题 {len(blocking)} 条；"
            "结束修复循环并在报告中标注「未完全收敛」。"
        )
    else:
        decision = "pass"
        summary = f"门禁通过：无阻断级问题（verdict={verdict or 'n/a'}，累计问题 {counts}）。"

    return AgentResult(
        kind="gate",
        summary=summary,
        decision=decision,
        data={
            "blocking": len(blocking),
            "counts": counts,
            "verdict": verdict,
            "iteration": iteration,
            "limit": limit,
            "converged": not blocking,
        },
    )


def report_run(node: Node, ctx: NodeContext) -> AgentResult:
    """汇总节点：把共享状态里的产物汇总成一份人类可读的运行小结。

    读数优先走共享黑板（`state[节点 id]`），这样即使 future 中间插入了别的节点也不会读空。
    """
    state = ctx.state
    counts = state.get("issue_counts") or {}
    issues = state.get("issues") or []
    fixed = state.get("fixed") or []
    remaining = state.get("remaining") or []

    gate = _lookup(ctx, GATE).data
    if not fixed or not remaining:
        # 修复明细由 fix 节点写入，可能不在门禁的直接输出里，这里统一从黑板兜底。
        fix_data = _lookup(ctx, FIX).data
        fixed = fixed or list(fix_data.get("fixed") or [])
        remaining = remaining or list(fix_data.get("remaining") or [])

    lines = [
        "## 运行小结",
        "",
        f"- 工作流：{ctx.config.extra.get('workflow', 'code_review')}",
        f"- 任务：{ctx.config.task or '（未提供）'}",
        f"- 工作区：{state.get('workspace') or ctx.workspace}",
        f"- 回合数：{ctx.turn}",
        f"- 问题统计：{counts}",
        f"- 已修复：{len(fixed)} 条；未修复：{len(remaining)} 条",
        f"- 是否收敛：{'是' if gate.get('converged') else '否（达到迭代上限）'}",
        "",
        "## 最终问题清单",
        "",
    ]
    if issues:
        for index, issue in enumerate(issues, 1):
            lines.append(
                f"{index}. [{issue.get('severity', '?')}] {issue.get('title', '')} "
                f"({issue.get('file', '')}:{issue.get('line') or '-'})"
            )
    else:
        lines.append("（无）")

    summary = f"流程结束：共 {len(issues)} 条问题，修复 {len(fixed)} 条，未修复 {len(remaining)} 条。"
    return AgentResult(
        kind="report",
        summary=summary,
        decision="done",
        data={"counts": counts, "issues": issues, "fixed": fixed, "remaining": remaining},
        artifacts={"report/run_summary.md": "\n".join(lines)},
    )


# --------------------------------------------------------------------------- #
# 流程构造
# --------------------------------------------------------------------------- #
def build_code_review_workflow(
    *,
    parallel_review: bool = False,
    max_iterations: int = 3,
    max_turns: int = 50,
    include_docs: bool = True,
) -> Workflow:
    """构造"代码审查与修复"工作流。

    Args:
        parallel_review: 是否启用并行审查（安全线 + 可维护性线并发）。
        max_iterations: 修复循环最多迭代次数。
        max_turns: 整个流程的最大回合数（安全阀）。
        include_docs: 是否在收尾阶段生成文档。

    Returns:
        校验通过的 `Workflow`。
    """
    builder = WorkflowBuilder("code_review", description=DESCRIPTION)
    builder.start(START)

    builder.agent(
        REQUIREMENTS,
        "requirements",
        description="需求 Agent：把任务拆成功能/非功能需求与验收标准",
    )
    builder.agent(
        DESIGN,
        "designer",
        description="设计 Agent：给出模块划分、接口签名与文件清单",
    )
    builder.agent(
        CODE,
        "coder",
        description="编码 Agent：生成实现与单元测试，写入工作区并本地自测",
        loop_of="impl",
        max_iterations=max_iterations,
    )
    # 复用 Homework1 的审查 Agent 作为独立节点（"可复用项目1的 Agent 作为 Workflow 节点"）。
    # 找不到 cra 工程时适配器自动回退到内置规则引擎，节点不会失败。
    builder.agent(
        LEGACY_REVIEW,
        "legacy_reviewer",
        description="复用 Homework1 的代码审查 Agent（扫描器 + 25 条 AST 规则 + 工具循环）",
    )

    if parallel_review:
        # 并行子节点由 Node.children 驱动（引擎负责并发调度与拓扑渲染），
        # 因此这里**不**再为"容器 → 子节点"声明边：子节点不是顶层节点，声明了就是悬空边。
        builder.parallel(
            PARALLEL_REVIEW,
            children=[
                Node(id=SECURITY_REVIEW, agent="security_reviewer", description="安全线审查"),
                Node(id=MAINT_REVIEW, agent="maintainability_reviewer", description="可维护性线审查"),
            ],
            description="并行审查：安全线 + 可维护性线同时执行",
        )
        builder.agent(
            REVIEW,
            "reviewer",
            description="审查 Agent：汇总并行审查结论并复核（规则 + 模型双引擎）",
        )
    else:
        builder.agent(
            REVIEW,
            "reviewer",
            description="审查 Agent：确定性规则 + 模型语义双引擎审查",
        )

    builder.gate(GATE, execute=gate_quality, description="质量门禁：决定继续修复还是收尾")

    builder.agent(
        FIX,
        "fixer",
        description="修复 Agent：按问题清单修复代码并产出 patch",
        loop_of="impl",
        max_iterations=max_iterations,
    )
    if include_docs:
        builder.agent(
            DOCS,
            "documenter",
            description="文档 Agent：依据代码与审查结论生成使用文档",
            when=lambda ctx: include_docs,
        )
    builder.route(REPORT, execute=report_run, description="汇报节点：汇总运行结果")
    builder.end(END)

    # ---- 边 ----
    builder.edge(START, REQUIREMENTS, label="顺序")
    builder.edge(REQUIREMENTS, DESIGN, label="需求→设计")
    builder.edge(DESIGN, CODE, label="设计→编码")
    builder.edge(CODE, LEGACY_REVIEW, label="编码→复用项目1审查")
    builder.edge(LEGACY_REVIEW, PARALLEL_REVIEW if parallel_review else REVIEW, label="审查→主线审查")
    if parallel_review:
        builder.edge(PARALLEL_REVIEW, REVIEW, label="汇总")
    builder.edge(REVIEW, GATE, label="审查→门禁")
    builder.edge(GATE, FIX, when="retry", label="不通过→修复")
    if include_docs:
        builder.edge(GATE, DOCS, when="pass", label="通过→文档")
    else:
        builder.edge(GATE, REPORT, when="pass", label="通过→汇报")
    # `fix → review` 是回边：声明顺序不影响语义，但保持与流程叙述一致的顺序。
    builder.edge(FIX, REVIEW, label="修复→复审（循环）")
    if include_docs:
        builder.edge(DOCS, REPORT, label="文档→汇报")
    builder.edge(REPORT, END, label="结束")

    workflow = builder.build(entry=START, max_turns=max_turns, max_iterations=max_iterations)
    problems = workflow.validate()
    if problems:
        raise ValueError("code_review 工作流校验失败：" + "；".join(problems))
    return workflow


__all__ = [
    "DESCRIPTION",
    "build_code_review_workflow",
    "gate_quality",
    "report_run",
    "START",
    "REQUIREMENTS",
    "DESIGN",
    "CODE",
    "REVIEW",
    "GATE",
    "FIX",
    "DOCS",
    "REPORT",
    "END",
    "PARALLEL_REVIEW",
    "SECURITY_REVIEW",
    "MAINT_REVIEW",
]
