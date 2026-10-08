"""wfa 的 Web 层：零依赖 HTTP 服务 + 单页界面。"""

from __future__ import annotations

from .server import RunRecord, RunRegistry, WorkflowService, main, serve

__all__ = ["RunRecord", "RunRegistry", "WorkflowService", "serve", "main"]
