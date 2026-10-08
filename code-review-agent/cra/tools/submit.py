"""终止型工具：Agent 用它提交最终审查结论。

为什么要做成工具而不是"让模型输出一段 JSON 文本"？
    * 参数由 function calling 的 schema 约束，结构正确率显著高于自由文本；
    * 提交动作可以被校验（缺字段、非法严重程度）、被记录（trace 里有据可查）；
    * 模型如果没调用它，Agent 仍能回退到"解析文本 JSON"，形成双保险。
"""

from __future__ import annotations

import json

from ..errors import ToolArgumentError
from ..models import Finding
from .base import ToolContext, ToolResult

MAX_FINDINGS = 60

FINDINGS_SCHEMA = {
    "type": "array",
    "description": "审查发现的问题列表，按严重程度从高到低排列",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "一句话问题标题"},
            "file": {"type": "string", "description": "相对工作区根目录的文件路径"},
            "line": {"type": "integer", "description": "问题所在行号，无法定位时省略"},
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low", "info"],
            },
            "category": {
                "type": "string",
                "enum": [
                    "bug", "security", "performance", "style",
                    "maintainability", "testing", "documentation", "other",
                ],
            },
            "detail": {"type": "string", "description": "问题原理与触发条件，必要时引用代码事实"},
            "suggestion": {"type": "string", "description": "可执行的修复建议，最好给出代码形态"},
            "evidence": {"type": "string", "description": "支撑结论的代码片段或工具输出摘要"},
            "rule_id": {"type": "string", "description": "若与静态规则命中相同，填该规则编号"},
        },
        "required": ["title", "file", "severity", "category", "detail"],
    },
}


def tool_submit_review(
    ctx: ToolContext,
    summary: str,
    findings: list,
    verdict: str = "",
) -> ToolResult:
    """接收最终审查结论（终止型工具）。"""
    if not summary or not str(summary).strip():
        raise ToolArgumentError("summary 不能为空：请用 2-5 句话总结整体质量与最严重的问题。")
    if not isinstance(findings, list):
        raise ToolArgumentError("findings 必须是数组；没有发现问题时传空数组 []。")

    parsed: list[Finding] = []
    errors: list[str] = []
    for index, raw in enumerate(findings[:MAX_FINDINGS], start=1):
        if not isinstance(raw, dict):
            errors.append(f"第 {index} 条不是对象，已跳过")
            continue
        finding = Finding.from_llm_dict(raw)
        if not finding.file:
            errors.append(f"第 {index} 条缺少 file 字段，已跳过")
            continue
        parsed.append(finding)

    if not parsed and not errors and not findings:
        summary_note = "本次未发现需要报告的问题。"
    else:
        summary_note = f"已接收 {len(parsed)} 条发现。"
    if errors:
        summary_note += " 被忽略的条目：" + "；".join(errors[:5])

    ctx.submissions.append(
        {
            "summary": str(summary).strip(),
            "verdict": str(verdict or "").strip(),
            "findings": [finding.to_dict() for finding in parsed],
        }
    )
    return ToolResult.success(
        summary_note + " 你可以直接结束（无需再调用工具），最终报告会由系统渲染。",
        submitted=len(parsed),
        summary=str(summary).strip(),
        verdict=str(verdict or "").strip(),
        findings=[finding.to_dict() for finding in parsed],
    )


def tool_report_progress(ctx: ToolContext, note: str) -> str:
    """把阶段性判断记入轨迹（可选），便于解释 Agent 的推理路径。"""
    ctx.submissions.append({"progress_note": note})
    return json.dumps({"recorded": True, "note": note[:200]}, ensure_ascii=False)


__all__ = ["tool_submit_review", "tool_report_progress", "FINDINGS_SCHEMA"]
