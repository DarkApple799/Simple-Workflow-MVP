"""LegacyReviewAgent：**复用 Homework1 的代码审查能力**（对应 CONTRACT 第 9 节）。

Homework1（``code-review-agent``）是一个完整的代码审查 Agent，本工程不重复造轮子，
而是把它当作"历史能力资产"接进来：

    加 ``sys.path`` → ``from cra.agent import CodeReviewAgent``
    → 复用它的扫描器（classify / 限额 / 跳过原因）、25 条 AST 规则、工具循环
    → 把 ``AgentOutcome.findings`` 逐个转成本工程的 ``Issue`` dict

三条硬约束：
    * **找不到 cra 时绝不崩溃**：回退到 :mod:`wfa.analysis` 规则引擎，
      ``metrics["legacy"] = False``，并在 ``notes`` 里说明原因；
    * 无论走哪条路径，返回的 ``data`` 都满足 review 约定（issues/counts/verdict/blocking）；
    * 只做只读分析，不写工作区。
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time

from .. import SEVERITY_ORDER
from ..analysis import DEFAULT_IGNORE_DIRS, scan_workspace
from ..config import PROJECT_ROOT
from ..graph import AgentResult
from .base import Agent, config_value, publish_message, workspace_root
from .review import (
    blocking_issues,
    count_severities,
    merge_issues,
    render_review_markdown,
    verdict_for,
)

logger = logging.getLogger("wfa.agents.legacy")

#: 查找 Homework1 工程时可以读的环境变量（按顺序尝试）。
CRA_ENV_KEYS = ("WFA_LEGACY_CRA_ROOT", "WFA_CRA_ROOT", "CRA_ROOT", "HW1_ROOT")

#: 相对本工程的常见相对路径（Homework1 与本工程放在同一父目录时可直接命中）。
CRA_RELATIVE_PATHS = (
    os.path.join("..", "code-review-agent"),
    os.path.join("..", "hw1", "code-review-agent"),
    os.path.join("vendor", "code-review-agent"),
    os.path.join("third_party", "code-review-agent"),
)


def _finding_to_issue(finding) -> dict | None:
    """把 Homework1 的 ``Finding``（dataclass 或 dict）转成本工程的 ``Issue`` dict。"""
    if isinstance(finding, dict):
        data = dict(finding)
    elif hasattr(finding, "to_dict"):
        data = dict(finding.to_dict() or {})
    else:
        data = dict(getattr(finding, "__dict__", {}) or {})
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    severity = str(data.get("severity") or "medium").strip().lower()
    if severity not in SEVERITY_ORDER:
        severity = "medium"
    file = str(data.get("file") or "").strip()
    line = _as_line(data.get("line"))
    rule_id = str(data.get("rule_id") or "").strip()
    return {
        "id": str(data.get("id") or "").strip() or f"{rule_id or 'HW1'}-{file}:{line}",
        "title": title[:200],
        "file": file,
        "line": line,
        "severity": severity,
        "category": str(data.get("category") or "other").strip().lower() or "other",
        "detail": str(data.get("detail") or ""),
        "suggestion": str(data.get("suggestion") or ""),
        "source": "legacy",
        "rule_id": rule_id,
        "evidence": str(data.get("evidence") or "")[:300],
    }


def _as_line(value):
    """把行号规范成 int 或 None。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class LegacyReviewAgent(Agent):
    """Homework1 审查能力适配器：能找到 cra 就用它，找不到就回退内置规则引擎。"""

    name = "legacy_reviewer"
    role = "Homework1 代码审查 Agent（cra 适配器）"

    def __init__(self, *, cra_root: str | None = None, offline: bool = True, max_steps: int = 4,
                 config=None, bus=None, name: str = "legacy_reviewer", role: str | None = None,
                 model=None) -> None:
        super().__init__(model, config=config, bus=bus)
        self.cra_root = str(cra_root or "").strip()
        self.offline = bool(offline)
        self.max_steps = max(1, int(max_steps or 4))
        self.name = name
        if role:
            self.role = role
        self._import_error = ""
        self._resolved_root = ""

    # ------------------------------------------------------------------ #
    def run(self, ctx) -> AgentResult:
        """执行审查：优先 Homework1，失败则回退内置规则引擎（绝不抛异常）。"""
        started = time.time()
        root = workspace_root(ctx, self.config)
        notes: list = []
        metrics: dict = {"legacy": False, "cra_root": self.cra_root or "未指定"}

        issues = self._legacy_issues(root, notes, metrics)
        if not metrics["legacy"]:
            issues = self._fallback_issues(root, notes, metrics)

        issues = merge_issues(issues)
        counts = count_severities(issues)
        blocking = blocking_issues(issues)
        verdict = verdict_for(counts)
        metrics["duration"] = round(time.time() - started, 3)
        metrics["issues"] = len(issues)
        metrics["blocking"] = len(blocking)
        summary = self._summary(metrics, counts, verdict, blocking)
        artifact = self._report(root, metrics, counts, verdict, issues, blocking)
        publish_message(ctx, "review", {
            "reviewer": self.name, "legacy": bool(metrics["legacy"]),
            "verdict": verdict, "blocking": len(blocking),
        })
        return AgentResult(
            kind="review",
            summary=summary,
            decision="retry" if blocking else "pass",
            data={"issues": issues, "counts": counts, "verdict": verdict, "blocking": blocking},
            artifacts={f"review/{self.name}.md": artifact},
            metrics=metrics,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # Homework1 路径
    # ------------------------------------------------------------------ #
    def _legacy_issues(self, root: str, notes: list, metrics: dict) -> list:
        """用 Homework1 的 ``CodeReviewAgent`` 跑一次审查；失败返回空列表并写 notes。"""
        if not root or not os.path.isdir(root):
            notes.append("工作区不存在，未执行 Homework1 适配器，回退内置规则引擎。")
            return []
        try:
            agent_module = self._import_cra()
            cra_config = self._cra_config()
            scan, outcome = agent_module.CodeReviewAgent(cra_config).review(root)
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级，不向外抛
            self._import_error = f"{type(exc).__name__}: {exc}"
            logger.info("复用 Homework1 失败，回退内置规则引擎：%s", self._import_error)
            notes.append("未能复用 Homework1 的 cra 包（%s），已回退到 wfa.analysis 规则引擎。"
                         % self._shorten(self._import_error))
            return []
        metrics.update({
            "legacy": True,
            "cra_root": self._resolved_root,
            "legacy_files": len(getattr(scan, "files", []) or []),
            "legacy_findings": len(getattr(outcome, "findings", []) or []),
            "legacy_steps": int(getattr(outcome, "steps", 0) or 0),
            "legacy_tool_calls": int(getattr(outcome, "tool_calls", 0) or 0),
            "legacy_mode": str(getattr(outcome, "mode", "") or ""),
        })
        notes.append("已复用 Homework1 的 cra 包（CodeReviewAgent：扫描器 + 25 条 AST 规则 + 工具循环）。")
        for note in list(getattr(outcome, "notes", []) or [])[:3]:
            notes.append("Homework1：%s" % note)
        return [item for item in (_finding_to_issue(finding)
                                 for finding in (getattr(outcome, "findings", []) or [])) if item]

    def _import_cra(self):
        """把候选根目录加入 ``sys.path`` 后导入 ``cra.agent``。"""
        self._resolved_root = ""
        for candidate in self._candidate_roots():
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
        importlib.invalidate_caches()
        try:
            module = importlib.import_module("cra.agent")
            self._resolved_root = self._module_root(module)
            return module
        except Exception as first:  # noqa: BLE001 - 换路径再试一次
            logger.debug("首次导入 cra.agent 失败：%s", first)
        for name in [key for key in sys.modules if key == "cra" or key.startswith("cra.")]:
            sys.modules.pop(name, None)
        module = importlib.import_module("cra.agent")
        self._resolved_root = self._module_root(module)
        return module

    @staticmethod
    def _module_root(module) -> str:
        """取 cra 包所在工程根目录（展示用）。"""
        path = getattr(module, "__file__", "") or ""
        return os.path.dirname(os.path.dirname(os.path.abspath(path))) if path else ""

    def _candidate_roots(self) -> list:
        """收集候选的 Homework1 工程根目录（只保留真实存在的目录）。"""
        raw: list = [self.cra_root]
        for key in CRA_ENV_KEYS:
            raw.append(os.environ.get(key, ""))
        extra = getattr(self.config, "extra", None)
        if isinstance(extra, dict):
            raw.extend([extra.get("cra_root") or "", extra.get("hw1_root") or ""])
        for relative in CRA_RELATIVE_PATHS:
            raw.append(os.path.abspath(os.path.join(PROJECT_ROOT, relative)))
            raw.append(os.path.abspath(os.path.join(os.getcwd(), relative)))
        seen: set = set()
        roots: list = []
        for item in raw:
            path = str(item or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            real = os.path.abspath(path)
            if os.path.isdir(real):
                roots.append(real)
        return roots

    def _cra_config(self):
        """构造 Homework1 的 ``Config``：沿用其扫描限额，离线时绝不触网。"""
        module = importlib.import_module("cra.config")
        cra_config = module.Config.from_env()
        cra_config.offline = self.offline or not cra_config.has_api_key()
        for attr in ("max_files", "max_file_bytes", "max_findings", "max_findings_per_file"):
            value = config_value(None, self.config, attr, None)
            if value is not None and hasattr(cra_config, attr):
                setattr(cra_config, attr, int(value))
        if hasattr(cra_config, "max_steps"):
            cra_config.max_steps = self.max_steps
        return cra_config

    @staticmethod
    def _shorten(message: str, limit: int = 160) -> str:
        """压缩错误信息，避免 notes 过长。"""
        text = " ".join(str(message or "").split())
        return text[:limit]

    # ------------------------------------------------------------------ #
    # 回退路径
    # ------------------------------------------------------------------ #
    def _fallback_issues(self, root: str, notes: list, metrics: dict) -> list:
        """回退：用 :mod:`wfa.analysis` 规则引擎产出完整审查结论。"""
        if not root or not os.path.isdir(root):
            if not notes:
                notes.append("工作区不存在，回退规则引擎未扫描任何文件。")
            return []
        try:
            report = scan_workspace(root, ignore_dirs=DEFAULT_IGNORE_DIRS)
        except Exception as exc:  # noqa: BLE001 - 兜底路径也必须无异常
            notes.append("回退规则引擎执行失败：%s" % exc)
            return []
        metrics["fallback_rule_hits"] = len(report.hits)
        notes.append("回退路径使用 wfa.analysis：扫描 %d 个文件，规则命中 %d 条。"
                     % (len(report.files), len(report.hits)))
        return [hit.to_issue() for hit in report.hits]

    # ------------------------------------------------------------------ #
    def _summary(self, metrics: dict, counts: dict, verdict: str, blocking: list) -> str:
        """一句话结论。"""
        source = "Homework1 cra 包" if metrics["legacy"] else "内置规则引擎（回退）"
        return ("审查完成（来源：%s）：critical=%d, high=%d, medium=%d, low=%d, info=%d，"
                "阻断 %d 条，结论 %s。"
                % (source, counts.get("critical", 0), counts.get("high", 0), counts.get("medium", 0),
                   counts.get("low", 0), counts.get("info", 0), len(blocking), verdict))

    def _report(self, root: str, metrics: dict, counts: dict, verdict: str,
                issues: list, blocking: list) -> str:
        """渲染 Markdown 报告（标注来源是 Homework1 还是回退路径）。"""
        return render_review_markdown(
            root=root, focus=self.name, counts=counts, verdict=verdict, issues=issues,
            blocking=blocking,
            meta={"复用 Homework1": "是" if metrics["legacy"] else "否（已回退）",
                  "Homework1 根目录": metrics.get("cra_root") or "-",
                  "规则命中来源": "cra 25 条 AST 规则" if metrics["legacy"] else "wfa.analysis 22 条规则"},
        )


__all__ = ["LegacyReviewAgent", "CRA_ENV_KEYS", "CRA_RELATIVE_PATHS"]
