"""工具集：注册表 + 各工具的函数式实现。

Agent 与工具之间只通过 JSON Schema 与字符串观察结果交互，
因此新增工具只需：写一个 handler → 在 build_default_registry 里登记。
"""

from __future__ import annotations

from .analyze import tool_analyze_python
from .base import Tool, ToolContext, ToolRegistry, ToolResult, validate_arguments
from .files import tool_file_stats, tool_list_files, tool_read_file, tool_search_code
from .scan import tool_scan_directory
from .submit import FINDINGS_SCHEMA, tool_submit_review

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "validate_arguments",
    "build_default_registry",
    "FINDINGS_SCHEMA",
]


def build_default_registry() -> ToolRegistry:
    """构造内含全部默认工具的注册表（6 个工具，其中 1 个终止型）。"""
    registry = ToolRegistry()

    registry.add(
        name="list_files",
        description=(
            "列出工作区（或子目录）中的文件及其类型、大小、行数。"
            "开始审查、或不确定该看哪些文件时先调用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区根目录的目录或文件，默认 '.'"},
                "pattern": {"type": "string", "description": "文件名通配过滤，例如 '*.py'，默认 '*'"},
                "max_results": {"type": "integer", "description": "最多返回多少个文件，默认 80"},
            },
            "required": [],
        },
        handler=tool_list_files,
    )

    registry.add(
        name="read_file",
        description=(
            "读取文件的带行号内容。大文件请用 start_line/max_lines 分页读取，"
            "不要一次索要整个文件。二进制与空文件会明确报错。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区根目录的文件路径"},
                "start_line": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
                "max_lines": {"type": "integer", "description": "本次最多读取多少行，默认 400"},
            },
            "required": ["path"],
        },
        handler=tool_read_file,
    )

    registry.add(
        name="search_code",
        description=(
            "在工作区文本文件中按正则检索，返回 文件:行号 与命中行。"
            "适合定位危险调用（如 eval、shell=True）、密钥字面量、异常处理模式。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式，例如 'except\\\\s*:'"},
                "glob": {"type": "string", "description": "只搜索匹配该通配符的文件，默认 '*'"},
                "max_results": {"type": "integer", "description": "最多返回多少处命中，默认 20"},
                "context_lines": {"type": "integer", "description": "每处命中额外显示前后几行，默认 0"},
                "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 true"},
            },
            "required": ["pattern"],
        },
        handler=tool_search_code,
    )

    registry.add(
        name="analyze_python",
        description=(
            "对单个 .py 文件执行确定性 AST 规则分析（复杂度、可变默认参数、裸 except、"
            "未使用导入、硬编码密钥等），返回结构化 JSON。语法错误的文件也会返回可读的诊断。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区根目录的 .py 文件路径"},
            },
            "required": ["path"],
        },
        handler=tool_analyze_python,
    )

    registry.add(
        name="scan_directory",
        description=(
            "对子目录做一次确定性扫描，返回文件统计、规则命中排行与热点文件。"
            "用于先宏观定位问题集中的文件（每次会话最多调用 3 次）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区根目录的目录，默认 '.'"},
                "top_n": {"type": "integer", "description": "热点文件展示数量，默认 8"},
            },
            "required": [],
        },
        handler=tool_scan_directory,
    )

    registry.add(
        name="file_stats",
        description="统计目录规模（文件数、总行数、类型分布），用于快速建立整体印象。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区根目录的目录，默认 '.'"},
            },
            "required": [],
        },
        handler=tool_file_stats,
    )

    registry.add(
        name="submit_review",
        description=(
            "提交最终审查结论。完成调查后必须调用本工具一次："
            "summary 概述整体质量，findings 逐条给出问题（含 file/line/severity/category/detail/suggestion）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "2-5 句话的整体结论"},
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "pass_with_comments", "request_changes", "block"],
                    "description": "审查结论：通过 / 有意见但可通过 / 要求修改 / 阻断合并",
                },
                "findings": FINDINGS_SCHEMA,
            },
            "required": ["summary", "findings"],
        },
        handler=tool_submit_review,
        terminal=True,
    )

    return registry
