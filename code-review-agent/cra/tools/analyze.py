"""分析类工具：把确定性 AST 规则以工具形式暴露给 Agent。

与 scanner 的区别：scanner 是"一次全量预扫描"，本工具是"按需单文件深挖"，
让 Agent 能针对可疑文件追问细节（例如确认某个 except 到底吞了什么）。
"""

from __future__ import annotations

import json
import os

from ..analysis import analyze_source
from ..errors import FileBoundaryError, ToolError
from ..fsutil import classify_file, human_size, read_text_file, relpath, safe_path
from .base import ToolContext, ensure_in_scope

MAX_FINDINGS_IN_OUTPUT = 30


def tool_analyze_python(ctx: ToolContext, path: str) -> str:
    """对单个 Python 文件跑全部确定性规则，返回结构化 JSON。"""
    full = safe_path(ctx.root, path, must_exist=True)
    ensure_in_scope(ctx, full)
    if os.path.isdir(full):
        raise ToolError(f"{path} 是目录；本工具只接受单个 .py 文件，请先用 list_files 选择文件。")

    info = classify_file(full, ctx.cfg)
    if info.kind == "binary":
        raise FileBoundaryError(f"{path} 是二进制文件，无法做 AST 分析。")
    if info.kind == "empty":
        raise FileBoundaryError(f"{path} 是空文件，没有任何可分析内容。")
    if info.kind != "python":
        # 把 .c/.js 之类丢给 Python 解析器只会得到一个"语法错误"假发现，这里直接说清楚
        raise FileBoundaryError(
            f"{relpath(ctx.root, full)} 不是 Python 源码（识别为 {info.language}），AST 规则不适用。"
            "请改用 read_file 阅读内容并做语义审查。"
        )

    rel = relpath(ctx.root, full)
    text, encoding, lossy, truncated = read_text_file(full, max_bytes=ctx.cfg.max_file_bytes)
    findings, analysis = analyze_source(text, rel, max_findings=ctx.cfg.max_findings_per_file)

    payload = {
        "file": rel,
        "encoding": encoding,
        "decoded_lossy": lossy,
        "truncated": truncated,
        "parse_ok": analysis.parse_ok,
        "parse_error": analysis.error,
        "loc": analysis.loc,
        "classes": analysis.classes,
        "max_complexity": analysis.max_complexity,
        "avg_complexity": round(analysis.avg_complexity, 1),
        "functions": [
            {
                "name": item["name"],
                "line": item["line"],
                "length": item["length"],
                "complexity": item["complexity"],
                "has_docstring": item["docstring"],
            }
            for item in analysis.functions[:20]
        ],
        "rule_findings": [
            {
                "rule_id": finding.rule_id,
                "line": finding.line,
                "severity": finding.severity,
                "category": finding.category,
                "title": finding.title,
                "detail": finding.detail,
                "suggestion": finding.suggestion,
            }
            for finding in findings[:MAX_FINDINGS_IN_OUTPUT]
        ],
        "rule_finding_total": len(findings),
        "note": "以上为确定性规则结果，仅作为线索；请结合上下文判断是否为真问题。",
    }
    if truncated:
        payload["note"] += f" 该文件超过 {human_size(ctx.cfg.max_file_bytes)}，仅分析了前一部分。"
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["tool_analyze_python"]
