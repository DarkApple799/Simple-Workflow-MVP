"""扫描类工具：让 Agent 能对某个子目录做一次确定性汇总（而不是全文读入）。

Agent 因此具备"先宏观、再微观"的能力：先看目录级统计定位热点文件，再逐文件深挖。
调用次数有上限，避免模型陷入反复扫描的循环。
"""

from __future__ import annotations

import json
import os

from ..fsutil import relpath, safe_path
from ..scanner import scan_workspace
from .base import ToolContext, ensure_in_scope

MAX_SCAN_CALLS = 3
MAX_LISTED_FINDINGS = 25


def tool_scan_directory(ctx: ToolContext, path: str = ".", top_n: int = 8) -> str:
    """对子目录执行确定性扫描，返回统计 + 规则命中摘要。"""
    ctx.scan_calls += 1
    if ctx.scan_calls > MAX_SCAN_CALLS:
        return (
            f"本次会话已扫描 {MAX_SCAN_CALLS} 次，为避免重复劳动不再扫描。"
            "请基于已有结果直接给出结论，或用 read_file / analyze_python 深挖具体文件。"
        )

    if ctx.scope_file:
        # 单文件模式：忽略 path 参数，只汇总被指定的那个文件
        target = os.path.join(ctx.root, ctx.scope_file)
    else:
        target = safe_path(ctx.root, path, must_exist=True)
    ensure_in_scope(ctx, target)
    base = target if os.path.isdir(target) else os.path.dirname(target)
    # include_files=True：下面要统计 files_scanned，文件清单只用于计数（不进 JSON、不进上下文）
    sub = scan_workspace(base, ctx.cfg, only_file=None if os.path.isdir(target) else target, include_files=True)

    rule_counter: dict[str, int] = {}
    for finding in sub.findings:
        key = f"{finding.rule_id} {finding.title}"
        rule_counter[key] = rule_counter.get(key, 0) + 1

    hotspot = [
        {"file": name, "issues": count, "worst_severity_rank": rank}
        for name, count, rank in sub.hotspot_files(limit=int(top_n) or 8)
    ]
    payload = {
        "base_dir": relpath(ctx.root, base),
        "files_scanned": len(sub.files),
        "skipped": len(sub.skipped),
        "truncated": sub.truncated,
        "duration_seconds": round(sub.duration, 2),
        "findings_total": len(sub.findings),
        "top_rules": sorted(rule_counter.items(), key=lambda item: -item[1])[:10],
        "hotspot_files": hotspot,
        "notes": sub.notes,
        "sample_findings": [
            {
                "file": finding.file,
                "line": finding.line,
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "title": finding.title,
            }
            for finding in sub.findings[:MAX_LISTED_FINDINGS]
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["tool_scan_directory"]
