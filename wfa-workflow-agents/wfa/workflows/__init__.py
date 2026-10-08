"""预置工作流：声明"哪些 Agent 以什么拓扑协作"。"""

from __future__ import annotations

from .code_review import build_code_review_workflow
from .docs import build_docs_workflow
from .registry import WORKFLOWS, build_workflow, list_workflows

__all__ = [
    "build_code_review_workflow",
    "build_docs_workflow",
    "build_workflow",
    "list_workflows",
    "WORKFLOWS",
]
