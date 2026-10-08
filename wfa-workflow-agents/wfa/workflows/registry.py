"""预置工作流注册表：按名字构造流程，供 CLI / Web / 测试统一入口。"""

from __future__ import annotations

import inspect
import logging

from ..errors import ConfigError

logger = logging.getLogger("wfa.workflows")

from .code_review import DESCRIPTION as CODE_REVIEW_DESC, build_code_review_workflow
from .docs import DESCRIPTION as DOCS_DESC, build_docs_workflow

#: 名字 -> (构造函数, 说明, 参与的 Agent 角色)
WORKFLOWS: dict = {
    "code_review": {
        "builder": build_code_review_workflow,
        "description": CODE_REVIEW_DESC,
        "agents": ["requirements", "designer", "coder", "reviewer", "fixer", "writer"],
    },
    "docs": {
        "builder": build_docs_workflow,
        "description": DOCS_DESC,
        "agents": ["planner", "writer", "proofreader"],
    },
}


def build_workflow(name: str, **kwargs):
    """按名字构造工作流；未知名字抛出 `ConfigError`（附可用列表）。

    各流程的构造参数并不完全相同（例如只有 `code_review` 有 `parallel_review`），
    而调用方（CLI / Web）统一把一揽子参数透传进来。这里**按被调函数的签名过滤**，
    于是新增流程时不必迁就旧流程的参数集，也不会因多传参数而 `TypeError`。
    """
    entry = WORKFLOWS.get(name)
    if entry is None:
        raise ConfigError(f"未知工作流 {name!r}。可用：{', '.join(sorted(WORKFLOWS))}")
    builder = entry["builder"]
    accepted = set(inspect.signature(builder).parameters)
    filtered = {key: value for key, value in kwargs.items() if key in accepted}
    dropped = sorted(set(kwargs) - accepted)
    if dropped:
        logger.debug("工作流 %s 忽略不支持的参数：%s", name, ", ".join(dropped))
    return builder(**filtered)


def list_workflows() -> list:
    """返回工作流清单（不含构造出的图，避免副作用）。"""
    return [
        {"name": name, "description": entry["description"], "agents": list(entry["agents"])}
        for name, entry in WORKFLOWS.items()
    ]


__all__ = ["WORKFLOWS", "build_workflow", "list_workflows"]
