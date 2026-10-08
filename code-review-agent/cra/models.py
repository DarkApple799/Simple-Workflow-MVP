"""核心数据模型：问题、文件、扫描结果、Agent 运行结果。

全部为普通 dataclass，可安全序列化为 JSON——报告、测试与 Web 接口共用同一套结构。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

#: 严重程度由高到低；用于排序与统计。
SEVERITY_ORDER: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}

SEVERITY_LABEL_ZH: dict[str, str] = {
    "critical": "致命",
    "high": "严重",
    "medium": "中等",
    "low": "轻微",
    "info": "提示",
}

CATEGORY_LABEL_ZH: dict[str, str] = {
    "bug": "缺陷",
    "security": "安全",
    "performance": "性能",
    "style": "风格",
    "maintainability": "可维护性",
    "testing": "测试",
    "documentation": "文档",
    "other": "其他",
}


def _fingerprint(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


@dataclass
class Finding:
    """一条代码问题。

    同时承载"确定性规则命中"（source=rule）与"LLM 语义发现"（source=agent），
    合并后二者可互相印证（sources 列表里同时出现说明规则与模型都发现了它）。
    """

    title: str
    file: str
    line: int | None = None
    severity: str = "medium"
    category: str = "other"
    detail: str = ""
    suggestion: str = ""
    evidence: str = ""
    rule_id: str = ""
    source: str = "rule"
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            self.severity = "medium"
        if not self.sources:
            self.sources = [self.source]
        if self.line is not None:
            try:
                self.line = int(self.line)
                if self.line <= 0:
                    self.line = None
            except (TypeError, ValueError):
                self.line = None

    # ------------------------------------------------------------------ #
    @property
    def id(self) -> str:
        return _fingerprint(self.file, self.line, self.category, self.title)

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 2)

    def dedup_key(self) -> tuple:
        """去重键：同一文件 + 同一行附近 + 同一类别视为同一问题。"""
        bucket = None if self.line is None else (self.line // 5) * 5
        return (self.file, bucket, self.category)

    def merge(self, other: "Finding") -> "Finding":
        """把另一条同一位置的发现合并进来：保留更完整的描述与建议。"""
        for source in other.sources:
            if source not in self.sources:
                self.sources.append(source)
        if other.severity_rank > self.severity_rank:
            self.severity = other.severity
        if len(other.detail) > len(self.detail):
            self.detail = other.detail
        if len(other.suggestion) > len(self.suggestion):
            self.suggestion = other.suggestion
        if not self.evidence and other.evidence:
            self.evidence = other.evidence
        self.source = "rule+agent" if len(self.sources) > 1 else self.sources[0]
        return self

    def to_dict(self) -> dict:
        data = asdict(self)
        data["id"] = self.id
        return data

    # ------------------------------------------------------------------ #
    @classmethod
    def from_llm_dict(cls, raw: dict) -> "Finding":
        """把模型输出的自由 JSON 规范化为 Finding（字段名容错）。"""

        def pick(*names: str, default: str = "") -> Any:
            for name in names:
                if name in raw and raw[name] not in (None, ""):
                    return raw[name]
            return default

        return cls(
            title=str(pick("title", "问题", "name", default="未命名问题"))[:200],
            file=str(pick("file", "path", "文件", default="")),
            line=pick("line", "line_no", "行号", default=None),
            severity=str(pick("severity", "level", "严重程度", default="medium")).strip().lower(),
            category=str(pick("category", "type", "类别", default="other")).strip().lower(),
            detail=str(pick("detail", "description", "message", "说明", default="")),
            suggestion=str(pick("suggestion", "fix", "recommendation", "建议", default="")),
            evidence=str(pick("evidence", "snippet", "证据", default=""))[:500],
            rule_id=str(pick("rule_id", "rule", default="")),
            source="agent",
            confidence=float(pick("confidence", default=1.0) or 1.0),
        )


@dataclass
class FileInfo:
    """扫描到的单个文件。"""

    path: str  # 相对工作区根目录
    size: int
    lines: int | None = None
    language: str = "unknown"
    kind: str = "text"  # python | code | text | binary | too_large | empty | unreadable
    encoding: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    """确定性预扫描结果：文件清单 + 规则命中 + 跳过原因。"""

    root: str
    files: list[FileInfo] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: 相对路径 -> analysis.FileAnalysis.to_dict()，供工具按需回取结构化信息
    analyses: dict[str, dict] = field(default_factory=dict)
    #: 单文件审查模式下的目标文件（相对 root 的路径）；None 表示整目录审查。
    #: 工具层据此收窄可访问范围——否则"审一个文件"会退化成"审它所在的一整个目录"。
    scope_file: str | None = None
    skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total_bytes_scanned: int = 0
    truncated: bool = False
    duration: float = 0.0

    # ------------------------------------------------------------------ #
    def code_files(self) -> list[FileInfo]:
        return [f for f in self.files if f.kind in ("python", "code", "text")]

    def counts_by_severity(self) -> dict[str, int]:
        counts = {key: 0 for key in SEVERITY_ORDER}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for info in self.files:
            counts[info.kind] = counts.get(info.kind, 0) + 1
        return counts

    def hotspot_files(self, limit: int = 10) -> list[tuple[str, int, int]]:
        """返回 (文件, 问题数, 最高严重度) 并按问题数倒序。"""
        bucket: dict[str, list[Finding]] = {}
        for finding in self.findings:
            bucket.setdefault(finding.file, []).append(finding)
        rows = [
            (name, len(items), max(item.severity_rank for item in items))
            for name, items in bucket.items()
        ]
        rows.sort(key=lambda row: (-row[1], -row[2], row[0]))
        return rows[:limit]

    def to_dict(self, include_findings: bool = True) -> dict:
        data = {
            "root": self.root,
            "scope_file": self.scope_file,
            "duration": round(self.duration, 3),
            "truncated": self.truncated,
            "total_bytes_scanned": self.total_bytes_scanned,
            "notes": list(self.notes),
            "skipped": list(self.skipped),
            "stats": {
                "files": len(self.files),
                "by_kind": self.counts_by_kind(),
                "by_severity": self.counts_by_severity(),
            },
        }
        if include_findings:
            data["files"] = [f.to_dict() for f in self.files]
            data["findings"] = [f.to_dict() for f in self.findings]
        return data


@dataclass
class ToolTrace:
    """一次工具调用的轨迹，用于报告附录与调试。"""

    step: int
    tool: str
    arguments: dict = field(default_factory=dict)
    ok: bool = True
    error: str = ""
    output_preview: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentOutcome:
    """Agent 运行结果。"""

    summary: str = ""
    verdict: str = ""
    findings: list[Finding] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    degraded: bool = False          # 是否降级（未用 LLM / LLM 中途失败）
    degraded_reason: str = ""
    mode: str = "offline"           # online | offline | degraded
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    llm_retries: int = 0
    duration: float = 0.0
    trace: list[ToolTrace] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_answer: str = ""

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "mode": self.mode,
            "model": self.model,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "llm_calls": self.llm_calls,
                "llm_retries": self.llm_retries,
            },
            "duration": round(self.duration, 3),
            "notes": list(self.notes),
            "trace": [t.to_dict() for t in self.trace],
        }


def merge_findings(*groups: list[Finding]) -> list[Finding]:
    """合并多来源发现：按 (文件, 行号分桶, 类别) 去重，保留信息更全的一条。

    返回结果按严重程度、文件、行号排序，输出稳定（便于测试与 diff）。
    """
    merged: dict[tuple, Finding] = {}
    for group in groups:
        for finding in group:
            key = finding.dedup_key()
            if key in merged:
                merged[key].merge(finding)
            else:
                merged[key] = finding
    result = list(merged.values())
    result.sort(
        key=lambda f: (-f.severity_rank, f.file, f.line if f.line is not None else 0, f.title)
    )
    return result
