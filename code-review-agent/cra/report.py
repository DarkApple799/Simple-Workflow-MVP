"""报告渲染：把扫描结果与 Agent 结论变成可交付的 Markdown / JSON。

报告是这门作业的"交付物"之一，因此这里刻意做到：
    * 结论先行（先看摘要与统计，再看逐条明细）；
    * 每条问题都有位置、依据、修复建议，便于直接照着改；
    * 明确写出"扫描范围与限制"（跳过哪些文件、是否截断、模型是否降级），
      让读者知道这份报告的边界在哪——这也是评审里"边界情况处理"的体现。
"""

from __future__ import annotations

import json
import os
import time

from .config import Config
from .llm import LLMClient
from .models import (
    AgentOutcome,
    CATEGORY_LABEL_ZH,
    Finding,
    ScanResult,
    SEVERITY_LABEL_ZH,
    SEVERITY_ORDER,
)

VERDICT_LABEL_ZH = {
    "pass": "通过（无阻断问题）",
    "pass_with_comments": "有意见但可通过",
    "request_changes": "要求修改后再合入",
    "block": "阻断：存在必须立即修复的问题",
}


def _escape_cell(text: str, limit: int = 160) -> str:
    cleaned = (text or "").replace("|", "\\|").replace("\n", " ").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or "-"


def _location(finding: Finding) -> str:
    return f"`{finding.file}:{finding.line}`" if finding.line else f"`{finding.file}`"


def render_markdown(
    scan: ScanResult,
    outcome: AgentOutcome,
    *,
    cfg: Config | None = None,
    generated_at: str | None = None,
) -> str:
    """生成完整 Markdown 报告。"""
    now = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    severity_counts = _count(outcome.findings, "severity")
    category_counts = _count(outcome.findings, "category")
    rule_count = sum(1 for f in outcome.findings if "rule" in f.sources)
    agent_count = sum(1 for f in outcome.findings if "agent" in f.sources)

    lines: list[str] = []
    lines.append(f"# 代码审查报告：`{scan.root}`")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 审查模式：{_mode_text(outcome)}")
    if outcome.model and outcome.model not in ("（未使用）", ""):
        lines.append(f"- 使用模型：`{outcome.model}`")
    lines.append(
        f"- 规模：{len(scan.files)} 个文件 / {scan.total_bytes_scanned} 字节；"
        f"耗时 {outcome.duration:.2f}s（预扫描 {scan.duration:.2f}s）"
    )
    lines.append(
        f"- Agent 过程：{outcome.steps} 步推理、{outcome.tool_calls} 次工具调用、"
        f"{outcome.llm_calls} 次模型请求（重试 {outcome.llm_retries} 次）、"
        f"token 用量 {outcome.prompt_tokens}+{outcome.completion_tokens}"
    )
    if outcome.degraded:
        lines.append(f"- ⚠️ 降级说明：{outcome.degraded_reason or '未使用 LLM'}")
    lines.append("")

    # ---- 结论摘要 ----
    lines.append("## 一、结论摘要")
    lines.append("")
    if outcome.verdict and outcome.verdict in VERDICT_LABEL_ZH:
        lines.append(f"**审查结论：{VERDICT_LABEL_ZH[outcome.verdict]}**")
        lines.append("")
    lines.append(outcome.summary.strip() or "（无摘要）")
    lines.append("")
    lines.append("| 严重程度 | 数量 |")
    lines.append("| --- | --- |")
    for severity in ("critical", "high", "medium", "low", "info"):
        lines.append(f"| {SEVERITY_LABEL_ZH[severity]}（{severity}） | {severity_counts.get(severity, 0)} |")
    lines.append(f"| **合计** | **{len(outcome.findings)}** |")
    lines.append("")
    if outcome.findings:
        lines.append(
            f"其中来自确定性规则 {rule_count} 条、来自模型语义分析 {agent_count} 条"
            f"（同一条问题可能同时被两者命中，故两者之和≥合计数）。"
        )
        lines.append("")

    lines.extend(_findings_lines(outcome))
    lines.extend(_stats_lines(outcome, category_counts))
    lines.extend(_scope_lines(scan, outcome, cfg))
    lines.extend(_appendix_lines(outcome))

    lines.append("---")
    lines.append("")
    lines.append(
        "本报告由 Code Review Agent 自动生成：先由确定性规则（Python AST + 正则）产出证据，"
        "再由 LLM Agent 通过工具调用核实与补充语义发现。规则命中可能存在误报，"
        "请结合业务上下文判断。"
    )
    lines.append("")
    return "\n".join(lines)


def _findings_lines(outcome: AgentOutcome) -> list[str]:
    """第二节：按严重程度分组的问题清单；info 级用表格压缩展示。"""
    lines = ["## 二、问题清单", ""]
    if not outcome.findings:
        return lines + ["未发现需要报告的问题。", ""]
    for severity in ("critical", "high", "medium", "low"):
        group = [finding for finding in outcome.findings if finding.severity == severity]
        if not group:
            continue
        lines.append(f"### {SEVERITY_LABEL_ZH[severity]}（{severity}，{len(group)} 条）")
        lines.append("")
        for index, finding in enumerate(group, start=1):
            lines.extend(_finding_block(finding, f"{severity[0].upper()}{index}"))
    lines.extend(_info_table(outcome.findings))
    return lines


def _finding_block(finding: Finding, label: str) -> list[str]:
    """单条问题的详细小节：位置、依据、建议、证据。"""
    lines = [f"#### {label}. {finding.title}", ""]
    meta = [
        f"位置：{_location(finding)}",
        f"类别：{CATEGORY_LABEL_ZH.get(finding.category, finding.category)}",
    ]
    if finding.rule_id:
        meta.append(f"规则：`{finding.rule_id}`")
    meta.append("来源：" + "+".join("规则" if source == "rule" else "模型" for source in finding.sources))
    lines.append("- " + "；".join(meta))
    if finding.detail:
        lines.append(f"- 说明：{finding.detail}")
    if finding.suggestion:
        lines.append(f"- 建议：{finding.suggestion}")
    if finding.evidence:
        lines += ["- 证据：", "", "```", finding.evidence.strip()[:400], "```"]
    lines.append("")
    return lines


def _info_table(findings: list[Finding]) -> list[str]:
    info_group = [finding for finding in findings if finding.severity == "info"]
    if not info_group:
        return []
    lines = [
        f"### {SEVERITY_LABEL_ZH['info']}（info，{len(info_group)} 条）",
        "",
        "这一类多为文档与格式提示，汇总成表格便于批量处理：",
        "",
        "| 位置 | 问题 | 规则 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for finding in info_group:
        lines.append(
            f"| {_location(finding)} | {_escape_cell(finding.title, 60)} | "
            f"`{finding.rule_id or '-'}` | {_escape_cell(finding.detail, 120)} |"
        )
    return lines + [""]


def _stats_lines(outcome: AgentOutcome, category_counts: dict[str, int]) -> list[str]:
    """第三节：按类别统计 + 问题最集中的文件。"""
    lines = ["## 三、分类统计", ""]
    if category_counts:
        lines += ["| 类别 | 数量 |", "| --- | --- |"]
        for category, count in sorted(category_counts.items(), key=lambda item: -item[1]):
            lines.append(f"| {CATEGORY_LABEL_ZH.get(category, category)} | {count} |")
        lines.append("")
    hotspots = _hotspots(outcome.findings)
    if hotspots:
        lines += ["问题最集中的文件：", "", "| 文件 | 问题数 | 最高严重程度 |", "| --- | --- | --- |"]
        for name, count, worst in hotspots[:10]:
            lines.append(f"| `{_escape_cell(name, 80)}` | {count} | {SEVERITY_LABEL_ZH.get(worst, worst)} |")
        lines.append("")
    return lines


def _scope_lines(scan: ScanResult, outcome: AgentOutcome, cfg: Config | None) -> list[str]:
    """第四节：扫描范围与限制——让读者知道这份报告的边界。"""
    lines = ["## 四、扫描范围与限制", "", f"- 审查根目录：`{scan.root}`"]
    if cfg is not None:
        lines.append(f"- 范围过滤：{cfg.describe_scope()}")
        lines.append(
            f"- 限额：单文件 ≤ {cfg.max_file_bytes} 字节、最多 {cfg.max_files} 个文件、"
            f"单文件问题上限 {cfg.max_findings_per_file} 条、Agent 最多 {cfg.max_steps} 步"
        )
    kinds = _count_files(scan)
    lines.append("- 文件类型分布：" + "，".join(f"{kind}={count}" for kind, count in sorted(kinds.items())))
    if scan.truncated:
        lines.append("- ⚠️ 本次扫描触发了数量/大小限额，结果**不完整**。")
    if scan.skipped:
        reasons: dict[str, int] = {}
        for item in scan.skipped:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        summary = "；".join(f"{reason} × {count}" for reason, count in sorted(reasons.items(), key=lambda x: -x[1]))
        lines.append(f"- 跳过项：{summary}")
    for note in scan.notes + outcome.notes:
        lines.append(f"- 说明：{note}")
    return lines + [""]


def _appendix_lines(outcome: AgentOutcome) -> list[str]:
    """附录：Agent 工具调用轨迹与规则命中明细。"""
    lines: list[str] = []
    if outcome.trace:
        lines += [
            "## 附录 A：Agent 工具调用轨迹",
            "",
            "| 步 | 工具 | 参数摘要 | 结果 |",
            "| --- | --- | --- | --- |",
        ]
        for item in outcome.trace:
            args = ", ".join(f"{key}={value}" for key, value in (item.arguments or {}).items())
            result_text = "成功" if item.ok else f"失败：{_escape_cell(item.error, 60)}"
            lines.append(f"| {item.step} | `{item.tool}` | {_escape_cell(args, 80)} | {result_text} |")
        lines.append("")
    rule_table = _rule_summary(outcome.findings)
    if rule_table:
        lines += [
            "## 附录 B：规则命中明细",
            "",
            "| 规则 | 问题 | 次数 | 严重程度 |",
            "| --- | --- | --- | --- |",
        ]
        for rule_id, title, count, severity in rule_table:
            lines.append(f"| `{rule_id}` | {_escape_cell(title)} | {count} | {SEVERITY_LABEL_ZH.get(severity, severity)} |")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
def render_json(scan: ScanResult, outcome: AgentOutcome, cfg: Config | None = None) -> dict:
    """机器可读版本，便于接入 CI。"""
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": scan.root,
        "mode": outcome.mode,
        "degraded": outcome.degraded,
        "degraded_reason": outcome.degraded_reason,
        "verdict": outcome.verdict,
        "summary": outcome.summary,
        "counts": {
            "files": len(scan.files),
            "findings": len(outcome.findings),
            "by_severity": _count(outcome.findings, "severity"),
            "by_category": _count(outcome.findings, "category"),
        },
        "findings": [finding.to_dict() for finding in outcome.findings],
        "agent": outcome.to_dict(),
        "scan": scan.to_dict(include_findings=True),
    }
    if cfg is not None:
        payload["config"] = cfg.public_dict()
    return payload


def write_report(path: str, content: str) -> str:
    """写报告文件（UTF-8，自动建目录）。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return os.path.abspath(path)


def write_json(path: str, payload: dict) -> str:
    return write_report(path, json.dumps(payload, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
def _count(findings: list[Finding], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = getattr(finding, attribute)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _count_files(scan: ScanResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in scan.files:
        counts[info.kind] = counts.get(info.kind, 0) + 1
    return counts


def _hotspots(findings: list[Finding], limit: int = 10) -> list[tuple[str, int, str]]:
    bucket: dict[str, list[Finding]] = {}
    for finding in findings:
        bucket.setdefault(finding.file, []).append(finding)
    rows = []
    for name, items in bucket.items():
        worst = max(items, key=lambda f: f.severity_rank).severity
        rows.append((name, len(items), worst))
    rows.sort(key=lambda row: (-row[1], -SEVERITY_ORDER.get(row[2], 0), row[0]))
    return rows[:limit]


def _rule_summary(findings: list[Finding]) -> list[tuple[str, str, int, str]]:
    bucket: dict[str, dict] = {}
    for finding in findings:
        if not finding.rule_id:
            continue
        entry = bucket.setdefault(finding.rule_id, {"title": finding.title, "count": 0, "severity": finding.severity})
        entry["count"] += 1
        if SEVERITY_ORDER.get(finding.severity, 0) > SEVERITY_ORDER.get(entry["severity"], 0):
            entry["severity"] = finding.severity
    rows = [(rule_id, data["title"], data["count"], data["severity"]) for rule_id, data in bucket.items()]
    rows.sort(key=lambda row: (-row[2], row[0]))
    return rows


def _mode_text(outcome: AgentOutcome) -> str:
    if outcome.mode == "online" and not outcome.degraded:
        return "在线 Agent（LLM + 工具调用）"
    if outcome.mode == "offline":
        return "离线规则模式（未调用 LLM）"
    return "规则模式（在线流程降级）"


def llm_usage_line(client: LLMClient | None) -> str:
    """给 CLI 打印一行 token 统计。"""
    if client is None:
        return ""
    stats = client.stats.as_dict()
    return (
        f"模型调用 {stats['calls']} 次（重试 {stats['retries']} 次），"
        f"token {stats['prompt_tokens']}+{stats['completion_tokens']}"
    )
