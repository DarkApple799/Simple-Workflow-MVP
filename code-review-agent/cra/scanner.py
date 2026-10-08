"""工作区预扫描：把"一堆文件"变成"结构化的审查素材"。

流程：遍历 → 分类（源码/文本/二进制/空/超大）→ 逐个 Python 文件做 AST 规则分析
→ 汇总统计、跳过原因与截断说明。

这一步完全不依赖网络与 LLM，因此即使没有 API Key（--offline）也能产出有价值的结果，
同时也是 Agent 失败时的兜底报告来源。
"""

from __future__ import annotations

import os
import time

from .analysis import FileAnalysis, analyze_source
from .config import Config
from .fsutil import classify_file, count_lines, ensure_root, read_text_file, relpath, walk_files
from .models import Finding, ScanResult


def scan_workspace(
    root: str,
    cfg: Config,
    *,
    only_file: str | None = None,
    include_files: bool = True,
) -> ScanResult:
    """扫描工作区并返回结构化结果。

    Args:
        root: 工作区根目录。
        cfg: 运行配置（含各类限额）。
        only_file: 只审查单个文件时传入其绝对路径。
        include_files: 是否把文件清单也带回来（Web 场景可关掉以减小体积）。
    """
    started = time.time()
    root = ensure_root(root)
    is_single = bool(only_file) and os.path.isfile(only_file or "")
    result = ScanResult(root=root, scope_file=relpath(root, only_file) if is_single else None)

    if is_single:
        paths, skipped, total_bytes, truncated = [only_file], [], os.path.getsize(only_file), False
    else:
        paths, skipped, total_bytes, truncated = walk_files(root, cfg)

    result.skipped = skipped
    result.total_bytes_scanned = total_bytes
    result.truncated = truncated

    if not paths:
        result.notes.append("没有找到任何可审查的文件（可能目录为空，或全部被忽略规则/--include 过滤）。")
        result.duration = time.time() - started
        return result

    code_files = 0
    for path in paths:
        info = classify_file(path, cfg)
        # 补上准确行数：报告里"文件行数"是给人看的，值得多读一次文件
        if info.kind in ("python", "code", "text") and info.kind != "empty":
            info.lines = count_lines(path)
        if include_files:
            result.files.append(info)

        if info.kind != "python":
            if info.kind in ("binary", "too_large", "unreadable", "empty"):
                result.skipped.append({"path": info.path, "reason": info.note or info.kind})
            continue

        code_files += 1
        rel = relpath(root, path)
        try:
            source, encoding, lossy, was_truncated = read_text_file(path, max_bytes=cfg.max_file_bytes)
        except OSError as exc:
            result.skipped.append({"path": rel, "reason": f"读取失败：{exc}"})
            continue
        info.encoding = encoding
        if lossy:
            result.notes.append(f"{rel} 使用 {encoding} 解码，可能包含乱码字符。")
        if was_truncated:
            result.notes.append(f"{rel} 超过 {cfg.max_file_bytes} 字节，仅分析前 {cfg.max_file_bytes} 字节。")

        findings, analysis = analyze_source(source, rel, max_findings=cfg.max_findings_per_file)
        result.findings.extend(findings)
        result.analyses[rel] = _analysis_to_dict(analysis)

    if include_files:
        result.files.sort(key=lambda item: item.path)
    if truncated:
        result.notes.append(
            f"文件数量或总大小超过限额（max_files={cfg.max_files}），本次只扫描了前 {len(paths)} 个文件。"
        )
    if code_files == 0:
        result.notes.append(
            "没有发现 Python 文件：确定性规则未生效，将由 LLM 直接阅读这些文件给出语义审查结论。"
        )

    # 全局问题上限，避免报告膨胀
    if len(result.findings) > cfg.max_findings:
        hidden = len(result.findings) - cfg.max_findings
        result.findings = result.findings[: cfg.max_findings]
        result.notes.append(f"问题总数超过上限，已折叠 {hidden} 条低优先级问题。")

    result.duration = time.time() - started
    return result


def _analysis_to_dict(analysis: FileAnalysis) -> dict:
    return analysis.to_dict()


def scan_single_source(source: str, rel_path: str, cfg: Config) -> list[Finding]:
    """直接分析一段源码字符串（供工具与测试使用）。"""
    findings, _ = analyze_source(source, rel_path, max_findings=cfg.max_findings_per_file)
    return findings
