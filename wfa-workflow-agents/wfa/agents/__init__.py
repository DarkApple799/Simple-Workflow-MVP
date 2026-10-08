"""Agent 层：所有内置 Agent 与注册表。

模块划分::

    base.py         Agent 抽象 + AgentRegistry + default_registry + 上下文工具
    coding.py       CodingAgent（coder）：需求/设计 → 文件集合 → 写工作区 → 自测
    review.py       ReviewAgent（reviewer / security_reviewer / maintainability_reviewer）
    fix.py          FixAgent（fixer）：确定性重写 + patch + 写回工作区
    doc.py          DocAgent：requirements / designer / planner / writer / proofreader
    code_review.py  LegacyReviewAgent（legacy_reviewer）：复用 Homework1 的 cra 包

用法::

    from wfa.agents import default_registry
    agents = default_registry(model, config=cfg)
    agents.get("reviewer").run(ctx)
"""

from __future__ import annotations

from .base import Agent, AgentRegistry, default_registry
from .code_review import LegacyReviewAgent
from .coding import CodingAgent
from .doc import DocAgent
from .fix import FixAgent, render_fix_report
from .review import ReviewAgent, merge_issues, normalize_issue, verdict_for

__all__ = [
    "Agent",
    "AgentRegistry",
    "default_registry",
    "CodingAgent",
    "ReviewAgent",
    "FixAgent",
    "DocAgent",
    "LegacyReviewAgent",
    "normalize_issue",
    "merge_issues",
    "verdict_for",
    "render_fix_report",
]
