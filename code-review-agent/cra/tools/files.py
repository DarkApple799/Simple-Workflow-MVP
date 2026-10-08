"""文件类工具：列目录、读文件、全文检索。

这些工具同时是"边界处理"的集中体现，也是安全边界：
    * 拒绝读取工作区之外的路径（防提示词注入导致读取 ~/.ssh/id_rsa）；
    * 二进制文件直接说明原因而不是抛异常；
    * 超大文件按行号窗口分页，而不是把整个文件塞进上下文；
    * GBK/BOM 编码自动识别，读不出来也不崩溃。
"""

from __future__ import annotations

import fnmatch
import os
import re

from ..errors import FileBoundaryError, ToolArgumentError, ToolError
from ..fsutil import (
    classify_file,
    count_lines,
    human_size,
    read_text_file,
    relpath,
    safe_path,
    walk_files,
)
from .base import ToolContext, ensure_in_scope

#: 单个工具输出的字符上限，避免一次工具调用就把上下文撑爆。
MAX_OUTPUT_CHARS = 6000


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（输出过长，已截断，共 {len(text)} 字符）"


def _scoped_file_summary(ctx: ToolContext) -> str:
    """单文件审查模式下，文件清单只有那一个文件——直接把它报给模型。"""
    full = os.path.join(ctx.root, ctx.scope_file or "")
    info = classify_file(full, ctx.cfg)
    lines = f"{info.lines}行" if info.lines is not None else "-"
    return (
        "本次为单文件审查模式，只有 1 个文件可访问（访问其他路径会被拒绝）：\n"
        f"{ctx.scope_file}  [{info.kind}/{info.language}, {human_size(info.size)}, {lines}]"
    )


# --------------------------------------------------------------------------- #
def tool_list_files(ctx: ToolContext, path: str = ".", pattern: str = "*", max_results: int = 80) -> str:
    """列出工作区（或子目录）中的候选文件。"""
    if ctx.scope_file:
        return _scoped_file_summary(ctx)
    target = safe_path(ctx.root, path, must_exist=True)
    if os.path.isfile(target):
        candidates = [target]
    else:
        candidates, _, _, _ = walk_files(target, ctx.cfg)

    rows: list[str] = []
    matched = 0
    for full in candidates:
        rel_to_root = relpath(ctx.root, full)
        if pattern and pattern not in ("*", "") and not (
            fnmatch.fnmatch(rel_to_root, pattern) or fnmatch.fnmatch(os.path.basename(full), pattern)
        ):
            continue
        matched += 1
        if len(rows) >= max_results:
            continue
        info = classify_file(full, ctx.cfg)
        lines = f"{info.lines}行" if info.lines is not None else "-"
        rows.append(f"{rel_to_root}  [{info.kind}/{info.language}, {human_size(info.size)}, {lines}]")

    if not rows:
        return f"在 {relpath(ctx.root, target)} 下没有匹配 {pattern or '*'} 的文件。建议放宽 pattern 或确认路径。"

    header = f"共匹配 {matched} 个文件，显示前 {len(rows)} 个（工作区根目录：{ctx.root}）："
    suffix = "\n（结果已截断，请用 pattern 缩小范围或提高 max_results）" if matched > len(rows) else ""
    return _clip("\n".join([header, *rows]) + suffix)


# --------------------------------------------------------------------------- #
def tool_read_file(
    ctx: ToolContext,
    path: str,
    start_line: int = 1,
    max_lines: int = 0,
) -> str:
    """读取文件内容（带行号），支持从指定行开始的窗口读取。"""
    full = safe_path(ctx.root, path, must_exist=True)
    ensure_in_scope(ctx, full)
    if os.path.isdir(full):
        raise ToolError(f"{path} 是目录，请改用 list_files 查看内容。")

    info = classify_file(full, ctx.cfg)
    if info.kind == "binary":
        raise FileBoundaryError(
            f"{path} 是二进制文件（{human_size(info.size)}），无法按文本审查；"
            "如果确实需要，请说明你想从中确认什么。"
        )
    if info.kind == "empty":
        raise FileBoundaryError(f"{path} 是空文件（0 字节）：没有任何可审查内容。")

    limit_bytes = ctx.cfg.max_file_bytes
    text, encoding, lossy, truncated = read_text_file(full, max_bytes=limit_bytes)
    lines = text.splitlines()
    total = len(lines)

    start = max(int(start_line or 1), 1)
    window = int(max_lines) if max_lines else ctx.cfg.max_read_lines
    window = max(min(window, 2000), 1)
    end = min(start + window - 1, total) if total else start
    selected = lines[start - 1 : end]

    if not selected:
        return f"{relpath(ctx.root, full)} 只有 {total} 行，请求的起始行 {start} 超出范围。"

    body = "\n".join(f"{index:>5} | {line}" for index, line in enumerate(selected, start=start))
    notes = [f"文件：{relpath(ctx.root, full)}（共 {total} 行，编码 {encoding}{'，解码有损' if lossy else ''}）"]
    if truncated:
        notes.append(f"注意：文件超过 {human_size(limit_bytes)}，只读取并分析了前 {human_size(limit_bytes)}。")
    if end < total:
        notes.append(f"当前显示第 {start}-{end} 行；如需后续内容，请带 start_line={end + 1} 再次调用。")
    return _clip("\n".join(notes + ["", body]))


# --------------------------------------------------------------------------- #
def tool_search_code(
    ctx: ToolContext,
    pattern: str,
    glob: str = "*",
    max_results: int = 20,
    context_lines: int = 0,
    ignore_case: bool = True,
) -> str:
    """在工作区源码中做正则检索，返回 文件:行号 与命中行。"""
    if not pattern:
        raise ToolArgumentError("pattern 不能为空。")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolArgumentError(f"正则表达式非法：{exc}；如需匹配字面量请转义特殊字符。") from exc

    if ctx.scope_file:
        # 单文件模式：只在这一个文件里检索
        candidates = [os.path.join(ctx.root, ctx.scope_file)]
    else:
        candidates, _, _, _ = walk_files(ctx.root, ctx.cfg)
    hits: list[str] = []
    scanned = 0
    for full in candidates:
        rel = relpath(ctx.root, full)
        if glob not in ("*", "") and not (
            fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(os.path.basename(full), glob)
        ):
            continue
        info = classify_file(full, ctx.cfg)
        if info.kind not in ("python", "code", "text"):
            continue
        scanned += 1
        try:
            text, _, _, _ = read_text_file(full, max_bytes=ctx.cfg.max_file_bytes)
        except OSError:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines, start=1):
            if not regex.search(line):
                continue
            block = [f"{rel}:{index}: {line.strip()[:200]}"]
            for offset in range(1, int(context_lines) + 1):
                if index - 1 - offset >= 0:
                    block.insert(0, f"{rel}:{index - offset}: {lines[index - 1 - offset].strip()[:200]}")
                if index - 1 + offset < len(lines):
                    block.append(f"{rel}:{index + offset}: {lines[index + offset].strip()[:200]}")
            hits.append("\n".join(block))
            if len(hits) >= max_results:
                break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"在 {scanned} 个文本文件中没有匹配 {pattern!r}。可尝试更短的关键词，或先用 list_files 确认文件范围。"
    header = f"匹配 {len(hits)} 处（已扫描 {scanned} 个文件，glob={glob}）："
    return _clip("\n".join([header, *hits]))


# --------------------------------------------------------------------------- #
def tool_file_stats(ctx: ToolContext, path: str = ".") -> str:
    """统计目录规模：文件数、行数、语言分布（给 Agent 建立整体印象）。"""
    if ctx.scope_file:
        return _scoped_file_summary(ctx)
    target = safe_path(ctx.root, path, must_exist=True)
    candidates, skipped, total_bytes, truncated = walk_files(target, ctx.cfg)
    by_kind: dict[str, int] = {}
    total_lines = 0
    for full in candidates:
        info = classify_file(full, ctx.cfg)
        by_kind[info.kind] = by_kind.get(info.kind, 0) + 1
        if info.kind in ("python", "code", "text"):
            total_lines += count_lines(full)
    parts = [
        f"目录：{relpath(ctx.root, target)}",
        f"文件数：{len(candidates)}，总大小：{human_size(total_bytes)}，代码/文本总行数：{total_lines}",
        "类型分布：" + "，".join(f"{k}={v}" for k, v in sorted(by_kind.items())),
        f"被忽略/跳过：{len(skipped)} 项" + ("（已触发数量限额，结果被截断）" if truncated else ""),
    ]
    return "\n".join(parts)


__all__ = ["tool_list_files", "tool_read_file", "tool_search_code", "tool_file_stats"]
