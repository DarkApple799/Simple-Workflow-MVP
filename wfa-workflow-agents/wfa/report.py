"""报告渲染：把一次运行的结构化结果变成人类可读的 Markdown。

输出分三段：
    1. **运行概览**——工作流、任务、状态、轮次、耗时、模型用量；
    2. **问题与修复**——最终问题清单、修复明细、未修复原因；
    3. **协作轨迹**——消息总线摘要 + 节点时间线 + 产物清单；
    4. **附录**——完整消息日志（可折叠）。

同时提供"流程文档"生成器：把 Workflow 拓扑渲染成 Mermaid / DOT / 表格，供 docs/FLOW.md 使用。
"""

from __future__ import annotations

import json
import time

from . import SEVERITY_LABEL_ZH, SEVERITY_ORDER

STATUS_LABEL = {
    "success": "✅ 成功",
    "partial": "🟡 部分完成（达到上限）",
    "failed": "❌ 失败",
    "aborted": "⛔ 已中断",
    "running": "⏳ 运行中",
    "pending": "⏸ 未开始",
}

KIND_LABEL = {
    "review": "审查",
    "fix": "修复",
    "doc": "文档",
    "requirement": "需求",
    "design": "设计",
    "code": "编码",
    "gate": "门禁",
    "report": "汇报",
    "result": "结果",
}


def render_run_report(payload: dict) -> str:
    """把 `RunReport.to_dict()`（或等价 dict）渲染成 Markdown 文本。"""
    payload = payload or {}
    workflow = payload.get("workflow", "?")
    status = payload.get("status", "?")
    metrics = payload.get("metrics") or {}
    counts = payload.get("counts") or {}
    issues = payload.get("issues") or []
    fixes = payload.get("fixes") or []
    artifacts = payload.get("artifacts") or {}
    node_runs = payload.get("node_runs") or []
    # `messages` 在 engine 的 RunReport 里是 int（条数），在 Web 载荷里可能是消息列表；
    # 这里统一成列表，条数由 _message_count 负责。
    raw_messages = payload.get("messages") or []
    messages = raw_messages if isinstance(raw_messages, list) else []

    lines = [
        f"# 运行报告 · {workflow}",
        "",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　|　运行 ID：`{payload.get('run_id', '-')}`",
        "",
        "## 1. 运行概览",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 工作流 | `{workflow}` |",
        f"| 任务 | {payload.get('task') or '（未提供）'} |",
        f"| 工作区 | `{payload.get('workspace') or '-'}` |",
        f"| 状态 | {STATUS_LABEL.get(status, status)} |",
        f"| 回合数 | {payload.get('turns', 0)} |",
        f"| 耗时 | {payload.get('duration', 0)} s |",
        f"| 消息数 | {_message_count(payload, raw_messages)} |",
        f"| 问题总数 | {sum(counts.values()) if counts else len(issues)} |",
        f"| 模型 | {payload.get('model') or '离线（未使用）'} |",
        "",
    ]

    if metrics:
        lines += ["### 1.1 指标", "", "| 指标 | 值 |", "| --- | --- |"]
        for key, value in sorted(metrics.items()):
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            lines.append(f"| {key} | {rendered} |")
        lines.append("")

    if counts:
        lines += ["### 1.2 问题分级", "", "| 严重程度 | 数量 |", "| --- | --- |"]
        for severity in sorted(counts, key=lambda item: -SEVERITY_ORDER.get(item, 0)):
            lines.append(f"| {severity}（{SEVERITY_LABEL_ZH.get(severity, severity)}） | {counts[severity]} |")
        lines.append("")

    # ---- 问题清单 ----
    lines += ["## 2. 最终问题清单", ""]
    if not issues:
        lines += ["本次运行未发现问题（或问题已在修复循环中全部关闭）。", ""]
    else:
        lines += ["| # | 严重程度 | 类别 | 位置 | 问题 | 建议 |", "| --- | --- | --- | --- | --- | --- |"]
        for index, issue in enumerate(issues, 1):
            if isinstance(issue, str):
                lines.append(f"| {index} | - | - | - | {issue} | - |")
                continue
            location = f"`{issue.get('file', '')}`" + (f":{issue.get('line')}" if issue.get("line") else "")
            lines.append(
                f"| {index} | {issue.get('severity', '')} | {issue.get('category', '')} | {location} "
                f"| {_cell(issue.get('title', ''))} | {_cell(issue.get('suggestion', ''))} |"
            )
        lines.append("")

        details = [issue for issue in issues if isinstance(issue, dict) and issue.get("detail")]
        if details:
            lines += ["<details><summary>展开问题详情</summary>", ""]
            for index, issue in enumerate(details, 1):
                lines += [
                    f"**{index}. {issue.get('title', '')}**（{issue.get('file', '')}:{issue.get('line') or '-'}）",
                    "",
                    f"- 规则/来源：`{issue.get('rule_id') or issue.get('source') or '-'}`",
                    f"- 说明：{issue.get('detail', '')}",
                    f"- 建议：{issue.get('suggestion', '') or '（无）'}",
                    "",
                ]
            lines += ["</details>", ""]

    # ---- 修复明细 ----
    lines += ["## 3. 修复明细", ""]
    if not fixes:
        lines += ["本次运行没有产生修复动作。", ""]
    else:
        lines += ["| # | 文件 | 关联问题 | 修复说明 |", "| --- | --- | --- | --- |"]
        for index, patch in enumerate(fixes, 1):
            if isinstance(patch, str):
                lines.append(f"| {index} | - | - | {patch} |")
                continue
            lines.append(
                f"| {index} | `{patch.get('file', '')}` | {patch.get('issue_id', '-')} "
                f"| {_cell(patch.get('summary', ''))} |"
            )
        lines.append("")

    # ---- 协作轨迹 ----
    lines += ["## 4. Agent 协作轨迹", "", "### 4.1 节点时间线", ""]
    if node_runs:
        lines += [
            "| 回合 | 节点 | Agent | 类型 | 状态 | 耗时(s) | 结论摘要 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for record in node_runs:
            lines.append(
                f"| {record.get('turn', '-')} | `{record.get('node_id', '')}` | {record.get('agent', '')} "
                f"| {KIND_LABEL.get(record.get('kind', ''), record.get('kind', ''))} "
                f"| {record.get('status', '')} | {record.get('duration', 0)} "
                f"| {_cell(record.get('summary', ''))} |"
            )
        lines.append("")
    else:
        lines += ["（无节点记录）", ""]

    lines += ["### 4.2 消息总线", ""]
    if messages:
        lines += ["| # | 回合 | 发送方 | 接收方 | 类型 | 载荷摘要 |", "| --- | --- | --- | --- | --- | --- |"]
        for index, message in enumerate(messages, 1):
            if isinstance(message, str):
                lines.append(f"| {index} | - | - | - | - | {message} |")
                continue
            payload_text = message.get("payload")
            if isinstance(payload_text, (dict, list)):
                payload_text = json.dumps(payload_text, ensure_ascii=False)
            lines.append(
                f"| {index} | {message.get('turn', '-')} | {message.get('sender', '')} "
                f"| {message.get('recipient', '')} | {message.get('kind', '')} | {_cell(str(payload_text or ''))} |"
            )
        lines.append("")
    else:
        lines += ["（无消息）", ""]

    # ---- 产物 ----
    lines += ["## 5. 产物清单", ""]
    if artifacts:
        lines += ["| 名称 | 类型 | 产出者 | 大小 |", "| --- | --- | --- | --- |"]
        for name, artifact in artifacts.items():
            if not isinstance(artifact, dict):
                lines.append(f"| `{name}` | - | - | - |")
                continue
            content = artifact.get("content")
            size = len(content) if isinstance(content, str) else "-"
            lines.append(
                f"| `{name}` | {artifact.get('kind', '-')} | {artifact.get('producer', '-')} | {size} |"
            )
        lines.append("")
    else:
        lines += ["（无产物）", ""]

    if payload.get("notes"):
        lines += ["## 6. 备注与告警", ""]
        lines += [f"- {note}" for note in payload["notes"]]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_flow_document(workflow) -> str:
    """把工作流拓扑渲染成 docs/FLOW.md 的正文。"""
    lines = [
        f"# 流程说明 · {workflow.name}",
        "",
        workflow_description(workflow),
        "",
        "## 1. 流程图（Mermaid）",
        "",
        "```mermaid",
        workflow.to_mermaid().rstrip(),
        "```",
        "",
        "## 2. 节点清单",
        "",
        "| 节点 | 类型 | Agent | 说明 | 重试 | 失败策略 | 所属循环 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for node in workflow.nodes():
        lines.append(
            f"| `{node.id}` | {node.type} | {node.agent or '-'} | {node.description or '-'} "
            f"| {node.retry} | {node.on_error} | {node.loop_of or '-'} |"
        )
    lines += ["", "## 3. 边（转移条件）", "", "| 源 | 目标 | 条件 | 标签 |", "| --- | --- | --- | --- |"]
    for edge in workflow.edges():
        lines.append(f"| `{edge.src}` | `{edge.dst}` | {edge.when or '默认'} | {edge.label or '-'} |")
    lines += ["", "## 4. 循环与分支", ""]
    loops = sorted({node.loop_of for node in workflow.nodes() if node.loop_of})
    if loops:
        for loop in loops:
            members = [node.id for node in workflow.nodes() if node.loop_of == loop]
            lines.append(f"- 循环 `{loop}`：成员节点 {members}，最大迭代 {workflow.max_iterations} 次")
    else:
        lines.append("- 无显式循环")
    lines.append("- 条件分支：见第 3 节中带 `when` 的边（`retry` 回边 / `pass` 收尾边）")
    lines.append("")
    lines += ["## 5. Graphviz DOT（可选）", "", "```dot", workflow.to_dot().rstrip(), "```", ""]
    return "\n".join(lines)


def workflow_description(workflow) -> str:
    """取工作流的说明文本（不同实现可能存成属性或 dict 字段）。"""
    for attr in ("description", "_description"):
        value = getattr(workflow, attr, "")
        if isinstance(value, str) and value:
            return value
    return ""


def _cell(text: str, limit: int = 120) -> str:
    """把单元格内容压成单行并限长，避免破坏 Markdown 表格。"""
    flat = " ".join(str(text or "").split())
    flat = flat.replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _message_count(payload: dict, messages) -> int:
    """兼容两种报告形态：`messages`（int，engine 的 RunReport）或消息列表。"""
    value = payload.get("message_count")
    if isinstance(value, int):
        return value
    if isinstance(messages, int):
        return messages
    if isinstance(messages, list):
        return len(messages)
    return 0


def write_report(payload: dict, path: str) -> str:
    """把运行报告写入文件（自动建目录），返回写入的路径。"""
    from .util import write_text

    write_text(path, render_run_report(payload))
    return path


__all__ = [
    "render_run_report",
    "render_flow_document",
    "write_report",
    "workflow_description",
    "STATUS_LABEL",
]
