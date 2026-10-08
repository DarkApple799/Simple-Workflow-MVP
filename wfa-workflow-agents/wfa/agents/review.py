"""ReviewAgent：**三级审查**（确定性规则 → 模型工具核实 → 合并判定）。

设计目标（对应 CONTRACT 第 7.1 节）：

1. **阶段一 · 确定性事实**：直接调用 :mod:`wfa.analysis` 规则引擎扫描工作区，
   并把上游编码 Agent 产出的文件内容也纳入规则分析——所以即使没有任何模型能力，
   ``issues`` / ``counts`` / ``verdict`` / ``blocking`` 依然是完整的（``--offline`` 演示依赖它）。
2. **阶段二 · 工具核实**：仅当模型**不是确定性伪模型**（``is_deterministic`` 为假）
   且没有显式声明不支持工具（``supports_tools is not False``）时，才发起
   ``list_files / read_file / analyze_python / search_code`` 工具调用，
   最多 ``review_max_steps`` 步，收集语义发现与工具轨迹。
3. **阶段三 · 合并判定**：规则命中 + 上游/并行审查结论 + 模型发现去重合并，
   输出 ``counts`` / ``blocking`` / ``verdict``，并写一份 Markdown 审查报告。

同一个类通过 ``focus`` 参数化出三种角色：``reviewer``（全部）、``security_reviewer``
（只看安全类问题）、``maintainability_reviewer``（只看可维护性/风格/文档类问题）。
"""

from __future__ import annotations

import logging
import os
import re
import time

from .. import SEVERITY_ORDER
from ..analysis import DEFAULT_IGNORE_DIRS, ScanReport, analyze_source, scan_workspace
from ..graph import AgentResult
from ..util import extract_json_array, extract_json_object
from .base import (
    Agent,
    config_value,
    input_results,
    publish_message,
    result_dict,
    workspace_root,
)

logger = logging.getLogger("wfa.agents.review")

#: 统计口径里的严重程度键（顺序固定，报告与门禁共用）。
SEVERITY_KEYS = ("critical", "high", "medium", "low", "info")
#: 阻断级严重程度（与 ``wfa.BLOCKING_SEVERITIES`` 一致）。
BLOCKING_SEVERITIES = ("critical", "high")
#: 可维护性审查线覆盖的分类。
MAINTAINABILITY_CATEGORIES = ("maintainability", "style", "documentation", "performance")

#: 审查用系统提示词（模型具备工具能力时使用）。
SYSTEM_PROMPT = (
    "你是一名严格的代码审查员，正在与其它 Agent 协作。工作流已经用确定性 AST 规则"
    "扫过一遍代码库，你需要做的是：用工具核实关键疑点、补充规则覆盖不到的语义问题"
    "（逻辑错误、边界条件、接口不一致、并发与资源泄漏等）。\n"
    "工作方式：先调用工具（list_files / read_file / analyze_python / search_code）获取事实，"
    "可以多步调查；信息足够后，直接输出一个 JSON 对象作为最终答复，格式为：\n"
    '{"summary": "一句话结论", "verdict": "pass|pass_with_comments|request_changes|block",'
    ' "issues": [{"title": "...", "file": "相对路径", "line": 12, "severity":'
    ' "critical|high|medium|low|info", "category": "bug|security|maintainability|style|'
    'performance|documentation", "detail": "问题与证据", "suggestion": "修复建议"}]}\n'
    "要求：只报有证据的问题，不要臆造；行号必须来自你实际读到的文件；不要输出多余解释。"
)

#: 工具定义（OpenAI function calling 风格）。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出工作区文件（可按 glob 过滤，如 *.py）",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "glob 模式，默认 *"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内某个文件的文本内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对路径"},
                    "max_lines": {"type": "integer", "description": "最多读取行数，默认 200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_python",
            "description": "用确定性 AST 规则引擎分析某个 Python 文件，返回命中明细",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "工作区相对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在工作区文本文件里按正则/关键字搜索，返回 file:line 命中",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "正则或普通关键字"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": ["query"],
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# Issue 规范化 / 合并 / 统计
# --------------------------------------------------------------------------- #
def _pick(raw: dict, *names, default=None):
    """从 dict 里按候选键名取第一个非空值（容忍模型输出的字段名漂移）。"""
    for name in names:
        value = raw.get(name)
        if value not in (None, ""):
            return value
    return default


def _int_or_none(value):
    """把行号之类的值规范为 int 或 None。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_issue(raw, *, source: str = "agent", default_file: str = ""):
    """把自由 dict 规范化为契约里的 ``Issue``；缺少标题则返回 ``None``。"""
    if not isinstance(raw, dict):
        return None
    title = str(_pick(raw, "title", "问题", "name", default="") or "").strip()
    if not title:
        return None
    file = str(_pick(raw, "file", "path", "文件", default=default_file) or "").strip()
    severity = str(_pick(raw, "severity", "level", "严重程度", default="medium")).strip().lower()
    if severity not in SEVERITY_ORDER:
        severity = "medium"
    category = str(_pick(raw, "category", "type", "类别", default="other")).strip().lower() or "other"
    line = _int_or_none(_pick(raw, "line", "line_no", "行号"))
    rule_id = str(_pick(raw, "rule_id", "rule", default="") or "").strip()
    issue_id = str(raw.get("id") or "").strip() or f"{rule_id or 'ISSUE'}-{file}:{line}"
    return {
        "id": issue_id,
        "title": title[:200],
        "file": file,
        "line": line,
        "severity": severity,
        "category": category,
        "detail": str(_pick(raw, "detail", "description", "message", "说明", default="") or ""),
        "suggestion": str(_pick(raw, "suggestion", "fix", "recommendation", "建议", default="") or ""),
        "source": str(_pick(raw, "source", default=source) or source),
        "rule_id": rule_id,
        "evidence": str(_pick(raw, "evidence", "snippet", "证据", default="") or "")[:300],
    }


def _dedup_key(issue: dict) -> tuple:
    """去重键：同一文件 + 同一行 + 同一类别视为同一问题。"""
    return (issue.get("file") or "", issue.get("line"), issue.get("category") or "other")


def _merge_pair(base: dict, other: dict) -> dict:
    """合并同一位置的两条问题：保留更严重的定级与更完整的描述。"""
    if SEVERITY_ORDER.get(other.get("severity"), 2) > SEVERITY_ORDER.get(base.get("severity"), 2):
        base["severity"] = other["severity"]
    for key in ("detail", "suggestion", "evidence"):
        if len(str(other.get(key) or "")) > len(str(base.get(key) or "")):
            base[key] = other[key]
    if base.get("source") != other.get("source"):
        base["source"] = "rule+agent"
    if not base.get("rule_id") and other.get("rule_id"):
        base["rule_id"] = other["rule_id"]
    return base


def merge_issues(*groups) -> list:
    """合并多来源问题：按位置去重，按严重程度排序，``id`` 保证唯一。"""
    merged: dict = {}
    for group in groups:
        for item in group or []:
            issue = item if isinstance(item, dict) and "severity" in item else normalize_issue(item)
            if not issue:
                continue
            key = _dedup_key(issue)
            merged[key] = _merge_pair(merged[key], issue) if key in merged else issue
    result = list(merged.values())
    result.sort(key=lambda item: (
        -SEVERITY_ORDER.get(item.get("severity"), 2),
        str(item.get("file") or ""),
        item.get("line") or 0,
        str(item.get("title") or ""),
    ))
    return _ensure_unique_ids(result)


def _ensure_unique_ids(issues: list) -> list:
    """同一 (文件, 行) 上可能有多条同名问题，给 id 补序号避免覆盖。"""
    used: dict = {}
    for issue in issues:
        issue_id = str(issue.get("id") or "")
        if issue_id in used:
            used[issue_id] += 1
            issue["id"] = f"{issue_id}#{used[issue_id]}"
        else:
            used[issue_id] = 1
    return issues


def count_severities(issues) -> dict:
    """按严重程度统计（5 个键始终存在，便于报告与门禁稳定读取）。"""
    counts = {key: 0 for key in SEVERITY_KEYS}
    for issue in issues or []:
        severity = str((issue or {}).get("severity") or "medium").lower()
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def blocking_issues(issues) -> list:
    """取阻断级子集（critical / high）。"""
    return [
        issue for issue in (issues or [])
        if str(issue.get("severity") or "").lower() in BLOCKING_SEVERITIES
    ]


def verdict_for(counts: dict) -> str:
    """按统计口径给结论：critical→block；high→request_changes；其余→pass_with_comments/pass。"""
    if counts.get("critical"):
        return "block"
    if counts.get("high"):
        return "request_changes"
    if any(counts.get(key) for key in ("medium", "low", "info")):
        return "pass_with_comments"
    return "pass"


def extract_issues_from_payload(payload) -> list:
    """从模型/上游返回的 JSON 里提取 issue 列表（容忍 findings/issues/problems 等键）。"""
    if isinstance(payload, list):
        return [item for item in (normalize_issue(raw) for raw in payload) if item]
    if not isinstance(payload, dict):
        return []
    found: list = []
    for key in ("issues", "findings", "problems"):
        value = payload.get(key)
        if isinstance(value, list):
            found.extend(item for item in (normalize_issue(raw) for raw in value) if item)
    data = payload.get("data")
    if isinstance(data, dict):
        found.extend(extract_issues_from_payload(data))
    return found


def findings_from_text(text: str) -> list:
    """从模型回复文本里抠出 issue 列表（JSON 对象或数组都容忍）。"""
    if not text:
        return []
    payload = extract_json_object(text)
    if payload:
        found = extract_issues_from_payload(payload)
        if found:
            return found
    array = extract_json_array(text)
    if isinstance(array, list):
        return [item for item in (normalize_issue(raw) for raw in array) if item]
    return []


def issues_from_result(value) -> list:
    """从上游 ``AgentResult`` / dict（含 parallel 的 ``children`` 包装）递归取出 issues。"""
    raw = result_dict(value)
    if not raw:
        return []
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    found = extract_issues_from_payload(data)
    children = data.get("children") if isinstance(data, dict) else None
    if isinstance(children, dict):
        for child in children.values():
            found.extend(issues_from_result(child))
    return [item for item in found if item]


# --------------------------------------------------------------------------- #
# 审查报告
# --------------------------------------------------------------------------- #
def render_review_markdown(*, root: str, focus: str, counts: dict, verdict: str,
                           issues: list, blocking: list, meta: dict | None = None) -> str:
    """渲染一份人读的 Markdown 审查报告。"""
    lines = [
        "# 代码审查报告",
        "",
        f"- 工作区：`{root}`",
        f"- 审查线：{focus}",
        f"- 结论：**{verdict}**",
        f"- 统计：critical={counts.get('critical', 0)} / high={counts.get('high', 0)} / "
        f"medium={counts.get('medium', 0)} / low={counts.get('low', 0)} / info={counts.get('info', 0)}",
        "",
    ]
    for key, value in dict(meta or {}).items():
        lines.append(f"- {key}：{value}")
    lines.extend(["", "## 阻断级问题", ""])
    if blocking:
        for index, issue in enumerate(blocking, 1):
            lines.append(f"{index}. **[{issue.get('severity')}] {issue.get('title')}** "
                         f"(`{issue.get('file')}:{issue.get('line') or '-'}`)")
            if issue.get("detail"):
                lines.append(f"   - 问题：{issue.get('detail')}")
            if issue.get("suggestion"):
                lines.append(f"   - 建议：{issue.get('suggestion')}")
    else:
        lines.append("（无）")
    lines.extend(["", "## 全部问题（按严重程度）", ""])
    if not issues:
        lines.append("（无）")
    for issue in issues:
        lines.append(f"- [{issue.get('severity')}][{issue.get('category')}] {issue.get('title')} "
                     f"(`{issue.get('file')}:{issue.get('line') or '-'}`, "
                     f"rule={issue.get('rule_id') or '-'}, source={issue.get('source')})")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 只读工具集（模型核实阶段使用）
# --------------------------------------------------------------------------- #
class _ReviewToolbox:
    """审查 Agent 的只读工具集：list_files / read_file / analyze_python / search_code。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self.traces: list = []

    def schemas(self) -> list:
        """返回工具定义列表。"""
        return list(TOOL_SCHEMAS)

    def execute(self, name: str, arguments: dict, step: int = 0) -> dict:
        """执行一次工具调用，返回 ``{"ok","output","error"}``（异常一律转为 error）。"""
        started = time.time()
        handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "analyze_python": self._analyze_python,
            "search_code": self._search_code,
        }
        handler = handlers.get(str(name or ""))
        if handler is None:
            result = {"ok": False, "output": "", "error": f"未知工具：{name}"}
        else:
            try:
                result = handler(dict(arguments or {}))
            except Exception as exc:  # noqa: BLE001 - 工具失败要让模型看到原因
                result = {"ok": False, "output": "", "error": f"{type(exc).__name__}: {exc}"}
        self.traces.append({
            "step": step, "tool": str(name), "ok": bool(result.get("ok")),
            "arguments": dict(arguments or {}), "error": str(result.get("error") or ""),
            "duration": round(time.time() - started, 4),
        })
        return result

    # -- 路径安全 ---------------------------------------------------------- #
    def _path(self, rel: str) -> str:
        target = os.path.realpath(os.path.join(self.root, str(rel or "").replace("/", os.sep)))
        root = os.path.realpath(self.root)
        if target != root and not target.startswith(root + os.sep):
            raise ValueError(f"路径越界，已拒绝访问：{rel!r}")
        return target

    def _relative(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace("\\", "/")

    # -- 具体工具 ---------------------------------------------------------- #
    def _list_files(self, arguments: dict) -> dict:
        import fnmatch

        pattern = str(arguments.get("pattern") or "*")
        found: list = []
        for current, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(name for name in dirnames
                                 if name not in DEFAULT_IGNORE_DIRS and not name.startswith("."))
            for filename in sorted(filenames):
                if fnmatch.fnmatch(filename, pattern) or pattern == "*":
                    found.append(self._relative(os.path.join(current, filename)))
                if len(found) >= 200:
                    break
            if len(found) >= 200:
                break
        return {"ok": True, "output": "\n".join(found) or "（工作区没有文件）", "error": ""}

    def _read_file(self, arguments: dict) -> dict:
        path = self._path(str(arguments.get("path") or ""))
        if not os.path.isfile(path):
            return {"ok": False, "output": "", "error": f"文件不存在：{arguments.get('path')}"}
        limit = max(1, min(int(arguments.get("max_lines") or 200), 600))
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read(200_000).splitlines()
        head = lines[:limit]
        body = "\n".join(f"{index:>4}| {text}" for index, text in enumerate(head, 1))
        if len(lines) > limit:
            body += f"\n...（共 {len(lines)} 行，已截断）"
        return {"ok": True, "output": f"# {arguments.get('path')}\n{body}", "error": ""}

    def _analyze_python(self, arguments: dict) -> dict:
        rel = str(arguments.get("path") or "")
        path = self._path(rel)
        if not os.path.isfile(path):
            return {"ok": False, "output": "", "error": f"文件不存在：{rel}"}
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            hits = analyze_source(handle.read(), self._relative(path))
        rows = [f"{hit.rule_id} L{hit.line}: {hit.title} —— {hit.detail}" for hit in hits]
        return {"ok": True, "output": "\n".join(rows) or "（规则引擎没有命中）", "error": ""}

    def _search_code(self, arguments: dict) -> dict:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"ok": False, "output": "", "error": "query 不能为空"}
        limit = max(1, min(int(arguments.get("max_results") or 20), 100))
        try:
            matcher = re.compile(query)
        except re.error:
            matcher = re.compile(re.escape(query))
        rows: list = []
        for current, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(name for name in dirnames
                                 if name not in DEFAULT_IGNORE_DIRS and not name.startswith("."))
            for filename in sorted(filenames):
                if len(rows) >= limit:
                    break
                path = os.path.join(current, filename)
                rows.extend(self._search_file(path, matcher, limit - len(rows)))
        return {"ok": True, "output": "\n".join(rows) or "（没有匹配）", "error": ""}

    def _search_file(self, path: str, matcher, budget: int) -> list:
        """在单个文件里搜索；非文本文件静默跳过。"""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(200_000)
        except OSError:
            return []
        rows = []
        for index, line in enumerate(content.splitlines(), 1):
            if matcher.search(line):
                rows.append(f"{self._relative(path)}:{index}: {line.strip()[:160]}")
                if len(rows) >= budget:
                    break
        return rows


# --------------------------------------------------------------------------- #
# ReviewAgent
# --------------------------------------------------------------------------- #
class ReviewAgent(Agent):
    """代码审查 Agent：确定性规则 + 模型工具核实 + 判定（同一实现参数化三条审查线）。"""

    name = "reviewer"
    role = "代码审查 Agent"

    def __init__(self, model=None, *, config=None, bus=None, focus: str = "all",
                 legacy=None, name: str = "reviewer", role: str = "代码审查 Agent") -> None:
        super().__init__(model, config=config, bus=bus)
        self.focus = str(focus or "all").strip().lower()
        self.legacy = legacy
        self.name = name
        self.role = role

    # ------------------------------------------------------------------ #
    def run(self, ctx) -> AgentResult:
        """执行三级审查并返回 ``kind="review"`` 的结果。"""
        started = time.time()
        root = workspace_root(ctx, self.config)
        notes: list = []
        metrics: dict = {"focus": self.focus, "offline": not self._llm_available()}

        report = self._scan(ctx, root, notes)
        issues = self._rule_issues(ctx, report, notes)

        upstream, upstream_nodes, stale = self._collect_upstream(ctx, report)
        if upstream:
            issues.extend(upstream)
            notes.append("合并了上游审查结论：%s（%d 条）。" % (", ".join(upstream_nodes), len(upstream)))
        if stale:
            notes.append("上游结果中有 %d 条结论在当前工作区已不再命中，已忽略（视为已修复或已陈旧）。" % stale)
        if self.legacy is not None:
            issues.extend(self._run_legacy(ctx, notes, metrics))

        if self._llm_available():
            llm_issues, llm_notes, llm_metrics = self._run_llm_stage(ctx, root, report, notes)
            issues.extend(llm_issues)
            notes.extend(llm_notes)
            metrics.update(llm_metrics)

        issues = self._apply_focus(merge_issues(issues))
        max_issues = int(config_value(ctx, self.config, "max_findings", 300) or 300)
        if len(issues) > max_issues:
            notes.append("问题总数超过 max_findings=%d，仅保留前 %d 条。" % (max_issues, max_issues))
            issues = issues[:max_issues]

        counts = count_severities(issues)
        blocking = blocking_issues(issues)
        verdict = verdict_for(counts)
        summary = self._summary(root, report, counts, verdict, blocking)
        metrics.update({
            "files": len(report.files),
            "python_files": len(report.python_files()),
            "rule_hits": len(report.hits),
            "upstream_comments": len(upstream),
            "issues": len(issues),
            "blocking": len(blocking),
            "duration": round(time.time() - started, 3),
        })
        artifact = render_review_markdown(
            root=root, focus=self.focus, counts=counts, verdict=verdict,
            issues=issues, blocking=blocking,
            meta={"规则命中": len(report.hits), "扫描文件": len(report.files),
                  "模型阶段": "已执行" if self._llm_available() else "离线/确定性路径"},
        )
        self._publish(ctx, verdict, counts, blocking)
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
    # 阶段一：确定性规则
    # ------------------------------------------------------------------ #
    def _scan(self, ctx, root: str, notes: list) -> ScanReport:
        """扫描工作区；目录不存在时返回空报告并写备注。"""
        if not root or not os.path.isdir(root):
            notes.append("工作区 %r 不存在，规则引擎未扫描任何文件。" % root)
            return ScanReport(root=root or "")
        ignore_dirs = tuple(config_value(ctx, self.config, "ignore_dirs", DEFAULT_IGNORE_DIRS))
        report = scan_workspace(
            root,
            ignore_dirs=ignore_dirs or DEFAULT_IGNORE_DIRS,
            max_files=int(config_value(ctx, self.config, "max_files", 200) or 200),
            max_file_bytes=int(config_value(ctx, self.config, "max_file_bytes", 200_000) or 200_000),
        )
        notes.extend(report.notes)
        return report

    def _rule_issues(self, ctx, report: ScanReport, notes: list) -> list:
        """规则命中 + 上游文件内容的规则分析（保证 issue 不依赖模型能力）。"""
        issues = [hit.to_issue() for hit in report.hits]
        if report.hits:
            notes.append("确定性规则引擎命中 %d 条问题（分析 %d 个 Python 文件）。"
                         % (len(report.hits), len(report.python_files())))
        for node_id, raw in input_results(ctx):
            issues.extend(self._analyze_upstream_files(node_id, raw, report.root, notes))
        return issues

    def _analyze_upstream_files(self, node_id: str, raw: dict, root: str, notes: list) -> list:
        """上游 Agent 给出的文件内容也过一遍规则引擎。

        注意：**工作区里已存在的文件以工作区为准**——否则修复 Agent 改好文件后，
        迭代里的上游（coder）结果会把旧内容的问题重新带回来，导致门禁永远 retry。
        """
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        issues: list = []
        for key in ("files", "tests"):
            mapping = data.get(key)
            if not isinstance(mapping, dict):
                continue
            for rel, content in mapping.items():
                if not isinstance(content, str) or not str(rel).endswith(".py"):
                    continue
                if root and os.path.isfile(os.path.join(root, str(rel).replace("/", os.sep))):
                    continue
                hits = analyze_source(content, str(rel))
                if hits:
                    notes.append("对上游 %s 的 %s 做规则分析，命中 %d 条。" % (node_id, rel, len(hits)))
                issues.extend(hit.to_issue() for hit in hits)
        return issues

    def _collect_upstream(self, ctx, report: ScanReport) -> tuple:
        """合并上游审查结论：只吸收规则引擎无法复现的部分。

        工作流的 retry 回路是 ``gate → fix → review``，并行审查节点不会在本迭代重跑，
        因此上游结果里可能残留"上一轮"的结论。这里用当前工作区的规则命中做核对：
        文件已存在且对应规则不再命中 → 视为已修复，忽略该条（避免门禁永远 retry）。
        """
        found: list = []
        nodes: list = []
        stale = 0
        for node_id, raw in input_results(ctx):
            issues = issues_from_result(raw)
            if not issues:
                continue
            kept = [item for item in issues if self._keep_upstream(item, report)]
            stale += len(issues) - len(kept)
            if kept:
                found.extend(kept)
                nodes.append(node_id)
        return found, nodes, stale

    @staticmethod
    def _keep_upstream(issue: dict, report: ScanReport) -> bool:
        """核对上游结论在当前工作区是否仍然成立。

        注意：`source` 可能是复合值（例如并行子审查产出的 `"rule+agent"`）。
        只有**纯语义发现**（来源含 agent 且没有 rule_id 可供复现）才无条件保留；
        只要带了 `rule_id`，就必须用"当前工作区是否仍然命中该规则"来核对陈旧性，
        否则第一版的旧结论会被反复合并进来，导致门禁永远 retry（并行审查尤其明显）。
        """
        source = str(issue.get("source") or "agent").lower()
        rule_id = str(issue.get("rule_id") or "").strip()
        if "agent" in source and not rule_id:  # 纯模型语义发现：规则引擎无法复现，保留
            return True
        file = str(issue.get("file") or "")
        if not rule_id:
            return True
        if any(hit.file == file and hit.rule_id == rule_id for hit in report.hits):
            return True
        path = os.path.join(report.root, file.replace("/", os.sep)) if report.root else ""
        return not path or not os.path.isfile(path)  # 文件不在工作区 → 无法核对，保留

    def _run_legacy(self, ctx, notes: list, metrics: dict) -> list:
        """调用注入的 Homework1 适配器（LegacyReviewAgent）并合并其结论。"""
        try:
            result = self.legacy.run(ctx)
        except Exception as exc:  # noqa: BLE001 - 适配器失败不影响主审查
            notes.append("Homework1 适配器执行失败，已忽略：%s" % exc)
            metrics["legacy"] = False
            return []
        metrics["legacy"] = bool(result.metrics.get("legacy"))
        if result.metrics.get("legacy"):
            notes.append("已复用 Homework1 的 cra 审查能力，并合并其发现。")
        return [item for item in (result.data.get("issues") or []) if isinstance(item, dict)]

    # ------------------------------------------------------------------ #
    # 阶段二：模型工具核实
    # ------------------------------------------------------------------ #
    def _llm_available(self) -> bool:
        """是否具备"真实模型"能力：非确定性伪模型 + 没有显式声明不支持工具。"""
        model = self.model
        if model is None:
            return False
        if getattr(model, "is_deterministic", False):
            return False
        return getattr(model, "supports_tools", None) is not False

    def _run_llm_stage(self, ctx, root: str, report: ScanReport, notes: list) -> tuple:
        """工具核实 + 语义补充；任何异常都降级为"只用规则结论"。"""
        metrics = {"llm_steps": 0, "tool_calls": 0, "llm_degraded": False, "llm_issues": 0}
        if self.model is None:
            return [], [], metrics
        max_steps = int(config_value(ctx, self.config, "review_max_steps", 4) or 4)
        toolbox = _ReviewToolbox(root)
        try:
            text = self._tool_loop(ctx, toolbox, report, max_steps, metrics)
        except Exception as exc:  # noqa: BLE001 - LLM 失败必须降级而不是崩
            logger.warning("审查 Agent 的模型阶段失败，降级为规则结论：%s", exc)
            notes.append("模型工具核实阶段失败，已降级为确定性规则结论：%s" % exc)
            metrics["llm_degraded"] = True
            return [], [], metrics
        issues = findings_from_text(text)
        metrics["llm_issues"] = len(issues)
        if issues:
            notes.append("模型补充了 %d 条语义发现（已与规则命中合并去重）。" % len(issues))
        elif text:
            notes.append("模型未给出结构化发现，仅保留规则结论。")
        return issues, [], metrics

    def _tool_loop(self, ctx, toolbox: _ReviewToolbox, report: ScanReport,
                   max_steps: int, metrics: dict) -> str:
        """推理 → 工具调用 → 观察 → 再推理，返回最终答复文本。"""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_prompt(ctx, report)},
        ]
        use_tools = getattr(self.model, "supports_tools", None) is not False
        final_text = ""
        for step in range(1, max(1, max_steps) + 1):
            metrics["llm_steps"] = step
            response = self.model.chat(messages, tools=toolbox.schemas() if use_tools else None)
            tool_calls = list(getattr(response, "tool_calls", []) or [])
            if not tool_calls:
                final_text = str(getattr(response, "content", "") or "")
                break
            messages.append({
                "role": "assistant",
                "content": str(getattr(response, "content", "") or ""),
                "tool_calls": [self._tool_call_part(call) for call in tool_calls],
            })
            for call in tool_calls:
                result = toolbox.execute(getattr(call, "name", ""),
                                         getattr(call, "arguments", {}) or {}, step)
                metrics["tool_calls"] += 1
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(getattr(call, "id", "") or ""),
                    "name": str(getattr(call, "name", "") or ""),
                    "content": str(result.get("output") or result.get("error") or "（空结果）")[:4000],
                })
        if not final_text:
            final_text = self._final_llm_turn(messages)
        metrics["traces"] = toolbox.traces
        return final_text

    def _final_llm_turn(self, messages: list) -> str:
        """步数用尽时再要一次结构化结论（不带工具）。"""
        messages.append({"role": "user",
                         "content": "请立即输出最终 JSON 结论（summary + verdict + issues），不要再调用工具。"})
        response = self.model.chat(messages, tools=None)
        return str(getattr(response, "content", "") or "")

    @staticmethod
    def _tool_call_part(call) -> dict:
        """把 ToolCall 还原成回传模型的消息结构。"""
        if hasattr(call, "to_message_part"):
            return dict(call.to_message_part())
        return {
            "id": str(getattr(call, "id", "") or ""),
            "type": "function",
            "function": {"name": str(getattr(call, "name", "") or ""),
                         "arguments": str(getattr(call, "raw_arguments", "") or "{}")},
        }

    def _user_prompt(self, ctx, report: ScanReport) -> str:
        """拼装模型的第一条用户消息（任务 + 规则扫描摘要）。"""
        from .base import agent_task

        hotspots = [(item["path"], item["lines"]) for item in report.files[:20]]
        digest = "\n".join(f"- {path}（{lines or '?'} 行）" for path, lines in hotspots) or "（无文件）"
        hits = "\n".join(f"- {hit.rule_id} {hit.file}:{hit.line} {hit.title}"
                         for hit in report.hits[:40]) or "（无规则命中）"
        return (
            f"任务：{agent_task(ctx, self.config) or '（未提供）'}\n"
            f"审查线：{self.focus}\n"
            f"工作区文件清单（最多 20 个）：\n{digest}\n\n"
            f"确定性规则已命中的问题（最多 40 条，供你核实，不必重复报告）：\n{hits}\n\n"
            "请用工具核实，并补充规则覆盖不到的语义问题，最后输出 JSON 结论。"
        )

    # ------------------------------------------------------------------ #
    # 阶段三：判定与输出
    # ------------------------------------------------------------------ #
    def _apply_focus(self, issues: list) -> list:
        """按审查线过滤问题（``all`` 不过滤）。"""
        if self.focus == "security":
            return [item for item in issues if str(item.get("category")) == "security"]
        if self.focus == "maintainability":
            return [item for item in issues
                    if str(item.get("category")) in MAINTAINABILITY_CATEGORIES]
        return list(issues)

    def _summary(self, root: str, report: ScanReport, counts: dict, verdict: str,
                 blocking: list) -> str:
        """一句话结论（报告与日志都读它）。"""
        if not report.files:
            return "未找到可审查的文件（工作区 %s 为空？），结论 %s。" % (root, verdict)
        return (
            "审查完成：扫描 %d 个文件，规则命中 %d 条，合并后 %d 条问题"
            "（critical=%d, high=%d，阻断 %d 条），结论 %s。"
            % (len(report.files), len(report.hits),
               sum(counts.values()), counts.get("critical", 0), counts.get("high", 0),
               len(blocking), verdict)
        )

    def _publish(self, ctx, verdict: str, counts: dict, blocking: list) -> None:
        """向总线广播审查结论（供报告与 UI 展示 Agent 间协作）。"""
        publish_message(ctx, "review", {
            "reviewer": self.name, "focus": self.focus, "verdict": verdict,
            "counts": counts, "blocking": len(blocking),
        })


__all__ = [
    "ReviewAgent",
    "SYSTEM_PROMPT",
    "TOOL_SCHEMAS",
    "SEVERITY_KEYS",
    "normalize_issue",
    "merge_issues",
    "count_severities",
    "blocking_issues",
    "verdict_for",
    "extract_issues_from_payload",
    "findings_from_text",
    "issues_from_result",
    "render_review_markdown",
]
