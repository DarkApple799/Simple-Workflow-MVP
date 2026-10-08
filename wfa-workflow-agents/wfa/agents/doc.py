"""DocAgent：**一个类按 role 参数化**出需求的全部文档类角色。

角色（工作流节点按名字绑定，名字与 CONTRACT 第 7 节一致）::

    requirements  需求 Agent   → kind="requirement"，产出 requirement + documents + outline
    designer      设计 Agent   → 产出 design（模块划分 / 接口 / 文件清单）+ design.md
    planner       大纲 Agent   → 产出 outline + outline.md
    writer        文档 Agent   → 产出 documents（README.md 等）
    proofreader   审校 Agent   → 读上游文档，产出 proofread_notes.md（修订意见）

离线可用性：``RuleChatModel`` 会按提示词关键词返回内置模板（需求 / 设计 / 大纲 / 文档 /
审校），本模块解析其 JSON；如果拿不到模型输出，则用下面的确定性模板生成同等结构的
Markdown——所以 ``--offline`` 演示里这条链路始终有产物。
"""

from __future__ import annotations

import logging
import os
import re
import time

from ..errors import ConfigError
from ..graph import AgentResult
from ..util import extract_json_object
from .base import (
    Agent,
    agent_task,
    config_value,
    known_results,
    publish_message,
    state_value,
    workspace_root,
    write_workspace_file,
)

logger = logging.getLogger("wfa.agents.doc")

#: 支持的角色 → 默认 Agent 名字。
ROLES = ("requirements", "designer", "planner", "writer", "proofreader")

DOC_SYSTEM = (
    "你是软件团队里的文档/需求/设计工程师，与其它 Agent 协作。只输出一个 JSON 对象，"
    "不要输出多余解释。"
)

#: 给"确定性伪模型"用的最小提示词：每个角色只保留能命中对应内置模板的关键词。
ROLE_COMPACT_PROMPTS = {
    "requirements": "需求阶段：请输出 requirement（需求）清单。只输出 JSON。",
    "designer": "设计阶段：请给出 design（设计）与文件清单。只输出 JSON。",
    "planner": "大纲阶段：请给出文档 outline（大纲 / 章节）。只输出 JSON。",
    "writer": "文档阶段：请生成 document 文档（readme）。只输出 JSON。",
    "proofreader": "审校阶段：请对文档做 proofread（审校 / 校对）。只输出 JSON。",
}

#: 确定性兜底：需求模板。
DEFAULT_REQUIREMENT = {
    "goal": "交付一个小而完整、可运行、可测试的 Python 模块",
    "functional": [
        "提供核心业务函数，并做入参校验与错误处理",
        "统一用 logging 输出，不把异常静默吞掉",
        "附带单元测试，覆盖正常路径与边界路径",
    ],
    "non_functional": ["零第三方依赖", "Python 3.9+", "单文件可读、函数带 docstring"],
    "acceptance": ["python -m unittest 全绿", "静态规则无 critical/high 命中"],
}

#: 确定性兜底：设计模板（file 清单会驱动编码 Agent 的产出路径）。
DEFAULT_DESIGN = {
    "module": "app/service.py",
    "interface": [
        {"name": "normalize_records", "args": ["records"], "returns": "list"},
        {"name": "summarize", "records": "records", "returns": "dict"},
    ],
    "files": ["app/service.py", "tests/test_service.py"],
    "notes": "接口保持纯函数式，便于单元测试与静态检查。",
}

#: 确定性兜底：文档大纲。
DEFAULT_OUTLINE = ["项目简介", "快速开始", "接口说明", "文件清单", "质量结论"]

#: 审校规则阈值。
MIN_SECTIONS = 2
MAX_LINE_LENGTH = 160


# --------------------------------------------------------------------------- #
# Markdown 渲染
# --------------------------------------------------------------------------- #
def _bullets(values) -> list:
    """把标量/列表渲染成 Markdown 列表行。"""
    if isinstance(values, (list, tuple)):
        return [f"- {item}" for item in values]
    return [f"- {values}"]


def render_requirements(requirement: dict, task: str) -> str:
    """渲染需求说明 Markdown。"""
    lines = ["# 需求说明", "", f"- 任务：{task or '（未提供）'}", ""]
    if requirement.get("goal"):
        lines += ["## 目标", "", str(requirement["goal"]), ""]
    for title, key in (("功能需求", "functional"), ("非功能需求", "non_functional"),
                       ("验收标准", "acceptance")):
        values = requirement.get(key)
        if values:
            lines += [f"## {title}", ""] + _bullets(values) + [""]
    return "\n".join(lines).rstrip() + "\n"


def render_design(design: dict, task: str) -> str:
    """渲染设计说明 Markdown（含接口表与文件清单）。"""
    lines = ["# 设计说明", "", f"- 任务：{task or '（未提供）'}",
             f"- 目标模块：`{design.get('module') or 'app/service.py'}`", "",
             "## 接口设计", ""]
    for item in design.get("interface") or []:
        if isinstance(item, dict):
            args = item.get("args") or []
            lines.append(f"- `{item.get('name')}({', '.join(str(a) for a in args)})` "
                         f"→ {item.get('returns') or 'Any'}")
        else:
            lines.append(f"- `{item}`")
    lines += ["", "## 文件清单", ""] + _bullets(design.get("files") or []) + [""]
    if design.get("notes"):
        lines += ["## 说明", "", str(design["notes"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_outline(outline: list, title: str = "文档大纲") -> str:
    """渲染大纲 Markdown。"""
    lines = [f"# {title}", ""]
    lines += [f"{index}. {section}" for index, section in enumerate(outline, 1)]
    return "\n".join(lines) + "\n"


def render_documents(documents: dict, task: str) -> str:
    """把 ``{文件名: 内容}`` 汇总成一份索引（便于报告展示）。"""
    lines = ["# 文档产物", "", f"- 任务：{task or '（未提供）'}", ""]
    for name in sorted(documents):
        lines.append(f"- `{name}`（{len(str(documents[name]).splitlines())} 行）")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 审校
# --------------------------------------------------------------------------- #
def proofread_documents(documents: dict) -> list:
    """确定性审校：标题、章节数、占位符、超长行、空文档。"""
    comments: list = []
    for name in sorted(documents or {}):
        text = str(documents[name] or "")
        comments.extend(_proofread_one(name, text))
    return comments


def _proofread_one(name: str, text: str) -> list:
    """检查单份文档并返回修订意见列表。"""
    rows: list = []
    lines = text.splitlines()
    if not text.strip():
        return [{"file": name, "level": "high", "detail": "文档为空，需要在发布前补全内容。"}]
    headings = [line for line in lines if line.startswith("#")]
    if not any(line.startswith("# ") for line in lines):
        rows.append({"file": name, "level": "medium",
                     "detail": "缺少一级标题（`# 标题`），不利于目录生成。"})
    if len(headings) < MIN_SECTIONS:
        rows.append({"file": name, "level": "medium",
                     "detail": "章节数少于 %d 个，建议补充小节。" % MIN_SECTIONS})
    long_lines = [index for index, line in enumerate(lines, 1) if len(line) > MAX_LINE_LENGTH]
    if long_lines:
        rows.append({"file": name, "level": "low",
                     "detail": "有 %d 行超过 %d 字符（首行 %d），建议拆行。"
                               % (len(long_lines), MAX_LINE_LENGTH, long_lines[0])})
    placeholders = re.findall(r"(?i)\b(TODO|TBD|FIXME|待补充|待完善)\b", text)
    if placeholders:
        rows.append({"file": name, "level": "medium",
                     "detail": "仍存在占位内容：%s。" % ", ".join(sorted(set(placeholders)))})
    return rows


def render_proofread_notes(comments: list, documents: dict) -> str:
    """渲染审校意见 Markdown。"""
    lines = ["# 审校意见", "", f"- 审校文档数：{len(documents)}",
             f"- 修订意见数：{len(comments)}", ""]
    if not comments:
        lines.append("未发现需要修订的问题，可以通过。")
    else:
        lines += ["## 修订建议", ""]
        for item in comments:
            lines.append(f"- [{item.get('level')}] `{item.get('file')}`：{item.get('detail')}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# DocAgent
# --------------------------------------------------------------------------- #
class DocAgent(Agent):
    """文档 Agent：按 ``role`` 生成需求 / 设计 / 大纲 / 文档 / 审校意见。"""

    name = "writer"
    role = "文档 Agent"

    def __init__(self, model=None, *, config=None, bus=None, role: str = "writer",
                 name: str | None = None) -> None:
        super().__init__(model, config=config, bus=bus)
        role = str(role or "writer").strip().lower()
        if role not in ROLES:
            raise ConfigError("DocAgent 不支持的角色 %r；可选：%s" % (role, ", ".join(ROLES)))
        self.doc_role = role
        self.name = name or role
        self.role = {"requirements": "需求 Agent", "designer": "设计 Agent",
                     "planner": "大纲 Agent", "writer": "文档 Agent",
                     "proofreader": "审校 Agent"}[role]

    # ------------------------------------------------------------------ #
    def run(self, ctx) -> AgentResult:
        """按角色生成产物；文档会写入工作区（``write_workspace=True`` 时）。"""
        started = time.time()
        notes: list = []
        context = self._collect_context(ctx)
        builder = getattr(self, "_build_" + self.doc_role)
        payload, model_summary = builder(ctx, context, notes)
        documents = payload.get("documents") or {}
        written = self._write_documents(ctx, documents, notes)
        summary = self._summary(payload, documents, written, model_summary)
        metrics = {
            "role": self.doc_role,
            "documents": len(documents),
            "outline": len(payload.get("outline") or []),
            "written": len(written),
            "model_used": bool(model_summary) or self._model_available(),
            "duration": round(time.time() - started, 3),
        }
        self._publish(ctx, payload, documents)
        return AgentResult(
            kind=payload.get("kind") or "doc",
            summary=summary,
            decision=payload.get("decision") or "continue",
            data=self._result_data(payload, documents),
            artifacts=dict(documents),
            metrics=metrics,
            notes=notes,
        )

    @staticmethod
    def _result_data(payload: dict, documents: dict) -> dict:
        """按 CONTRACT 组装 data：documents/outline 为公共键，需求与设计各带结构化载荷。"""
        data = {"documents": dict(documents), "outline": list(payload.get("outline") or [])}
        for key in ("requirement", "design"):
            if payload.get(key):
                data[key] = payload[key]
        return data

    # ------------------------------------------------------------------ #
    # 上下文
    # ------------------------------------------------------------------ #
    def _collect_context(self, ctx) -> dict:
        """汇总任务、上游需求/设计/文档/审查结论。"""
        context = {
            "task": agent_task(ctx, self.config),
            "requirement": {},
            "design": {},
            "outline": [],
            "documents": {},
            "files": [],
            "issues": state_value(ctx, "issues", []) or [],
            "counts": state_value(ctx, "issue_counts", {}) or {},
            "verdict": state_value(ctx, "verdict", "") or "",
        }
        for node_id, raw in known_results(ctx):
            if str(node_id) == self.name:
                continue
            self._absorb(context, raw)
        return context

    @staticmethod
    def _absorb(context: dict, raw: dict) -> None:
        """把单个上游结果里的文档/需求/设计信息吸收进上下文。"""
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        for key in ("requirement", "design"):
            value = data.get(key)
            if isinstance(value, dict) and value:
                context[key].update(value)
        for key in ("outline", "files"):
            value = data.get(key)
            if isinstance(value, list):
                context[key].extend(value)
            elif isinstance(value, dict) and key == "files":
                context[key].extend(value.keys())
        documents = data.get("documents")
        if isinstance(documents, dict):
            context["documents"].update(documents)
        issues = data.get("issues")
        if isinstance(issues, list) and issues:
            context["issues"] = issues

    # ------------------------------------------------------------------ #
    # 各角色
    # ------------------------------------------------------------------ #
    def _build_requirements(self, ctx, context: dict, notes: list) -> tuple:
        """需求角色：requirement 结构 + requirements.md。"""
        model_data, model_summary = self._ask_model(ctx, context)
        requirement = _as_dict(model_data.get("requirement")) or _as_dict(model_data) or {}
        if not requirement:
            requirement = dict(DEFAULT_REQUIREMENT)
            notes.append("未取到模型需求模板，使用确定性需求模板。")
        outline = ["目标", "功能需求", "非功能需求", "验收标准"]
        documents = {"docs/requirements.md": render_requirements(requirement, context["task"])}
        return {
            "kind": "requirement",
            "decision": "continue",
            "requirement": requirement,
            "outline": outline,
            "documents": documents,
            "summary": "需求已拆解为功能 / 非功能 / 验收标准。",
        }, model_summary

    def _build_designer(self, ctx, context: dict, notes: list) -> tuple:
        """设计角色：design 结构（含文件清单）+ design.md。"""
        model_data, model_summary = self._ask_model(ctx, context)
        design = _as_dict(model_data.get("design")) or _as_dict(model_data) or {}
        if not design:
            design = dict(DEFAULT_DESIGN)
            notes.append("未取到模型设计模板，使用确定性设计模板。")
        files = design.get("files")
        if not isinstance(files, list) or not files:
            design["files"] = list(DEFAULT_DESIGN["files"])
        outline = ["目标模块", "接口设计", "文件清单", "实现说明"]
        documents = {"docs/design.md": render_design(design, context["task"])}
        return {
            "kind": "doc",
            "decision": "continue",
            "design": design,
            "outline": outline,
            "documents": documents,
            "summary": "设计已给出模块划分、接口与文件清单（%s）。" % ", ".join(design["files"]),
        }, model_summary

    def _build_planner(self, ctx, context: dict, notes: list) -> tuple:
        """大纲角色：outline + outline.md。"""
        model_data, model_summary = self._ask_model(ctx, context)
        outline = [str(item) for item in (model_data.get("outline") or []) if str(item).strip()]
        if not outline:
            outline = list(context.get("outline") or DEFAULT_OUTLINE)
            notes.append("未取到模型大纲，使用确定性大纲。")
        documents = {"docs/outline.md": render_outline(outline)}
        return {
            "kind": "doc",
            "decision": "continue",
            "outline": outline,
            "documents": documents,
            "summary": "文档大纲共 %d 个章节。" % len(outline),
        }, model_summary

    def _build_writer(self, ctx, context: dict, notes: list) -> tuple:
        """文档角色：documents（README.md 等）。"""
        model_data, model_summary = self._ask_model(ctx, context)
        documents = _as_str_map(model_data.get("documents"))
        if not documents:
            documents = self._default_documents(context)
            notes.append("未取到模型文档模板，使用确定性文档模板。")
        outline = [str(item) for item in (model_data.get("outline") or [])
                   if str(item).strip()] or list(context.get("outline") or DEFAULT_OUTLINE)
        return {
            "kind": "doc",
            "decision": "pass",
            "outline": outline,
            "documents": documents,
            "summary": "已生成 %d 份文档：%s。" % (len(documents), ", ".join(sorted(documents))),
        }, model_summary

    def _build_proofreader(self, ctx, context: dict, notes: list) -> tuple:
        """审校角色：读上游文档并产出 proofread_notes.md。"""
        documents = dict(context.get("documents") or {})
        if not documents:
            documents = self._documents_from_workspace(ctx, notes)
        if not documents:
            notes.append("没有可供审校的文档（上游未产出 documents，工作区也没有 .md 文件）。")
        comments = proofread_documents(documents)
        notes_md = render_proofread_notes(comments, documents)
        return {
            "kind": "doc",
            "decision": "done",
            "outline": sorted(documents),
            "documents": {"docs/proofread_notes.md": notes_md},
            "comments": comments,
            "summary": "审校完成：检查 %d 份文档，给出 %d 条修订意见。" % (len(documents), len(comments)),
        }, ""

    def _documents_from_workspace(self, ctx, notes: list) -> dict:
        """从工作区读取 ``.md`` 文件作为审校输入。"""
        root = workspace_root(ctx, self.config)
        if not os.path.isdir(root):
            return {}
        documents: dict = {}
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for filename in sorted(filenames):
                if not filename.endswith(".md"):
                    continue
                path = os.path.join(current, filename)
                rel = os.path.relpath(path, root).replace("\\", "/")
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        documents[rel] = handle.read(200_000)
                except OSError as exc:
                    notes.append("读取 %s 失败：%s" % (rel, exc))
                if len(documents) >= 20:
                    return documents
        return documents

    def _default_documents(self, context: dict) -> dict:
        """确定性 README 模板（用任务、设计、审查结论填充）。"""
        design = context.get("design") or {}
        files = context.get("files") or design.get("files") or []
        lines = ["# 项目文档", "", "## 项目简介", "",
                 context["task"] or "由多 Agent Workflow 生成的小而完整的 Python 模块。", "",
                 "## 接口说明", ""]
        interfaces = design.get("interface") or []
        if interfaces:
            for item in interfaces:
                name = item.get("name") if isinstance(item, dict) else item
                lines.append(f"- `{name}`：见 `{design.get('module') or 'app/service.py'}` 实现。")
        else:
            lines.append("- 详见源码中的公开函数。")
        lines += ["", "## 文件清单", ""] + _bullets(files or ["app/service.py"]) + [""]
        lines += ["## 快速开始", "", "```bash", "python -m unittest discover -s tests -t .", "```", ""]
        lines += ["## 质量结论", "",
                  f"- 问题统计：{context.get('counts') or {}}",
                  f"- 审查结论：{context.get('verdict') or 'n/a'}",
                  f"- 问题总数：{len(context.get('issues') or [])}", ""]
        return {"README.md": "\n".join(lines)}

    # ------------------------------------------------------------------ #
    # 模型
    # ------------------------------------------------------------------ #
    def _model_available(self) -> bool:
        """是否有可用模型（任何非 None 的模型都算，包括离线伪模型）。"""
        return self.model is not None

    def _ask_model(self, ctx, context: dict) -> tuple:
        """问模型要角色产物；返回 ``(data, summary)``，失败返回 ``({}, "")``。"""
        model = self.model
        if model is None:
            return {}, ""
        compact = bool(getattr(model, "is_deterministic", False))
        prompt = ROLE_COMPACT_PROMPTS[self.doc_role] if compact else self._full_prompt(ctx, context)
        messages = [{"role": "system", "content": DOC_SYSTEM}, {"role": "user", "content": prompt}]
        if compact:
            # 确定性伪模型按关键词挑内置模板：系统提示里的关键词会抢先命中，故只发用户消息。
            messages = [{"role": "user", "content": prompt}]
        try:
            response = model.chat(messages)
        except Exception as exc:  # noqa: BLE001 - 模型失败必须能降级
            logger.warning("文档 Agent（%s）调用模型失败，改用确定性模板：%s", self.doc_role, exc)
            return {}, ""
        payload = extract_json_object(str(getattr(response, "content", "") or ""))
        if not isinstance(payload, dict):
            return {}, ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return data, str(payload.get("summary") or "")

    def _full_prompt(self, ctx, context: dict) -> str:
        """给真实模型用的完整提示（含任务与上游上下文摘要）。"""
        design = context.get("design") or {}
        requirement = context.get("requirement") or {}
        return (
            f"角色：{self.doc_role}（{self.role}）\n"
            f"任务：{context['task'] or '（未提供）'}\n"
            f"上游需求：{requirement}\n"
            f"上游设计：{design}\n"
            f"审查结论：verdict={context.get('verdict') or 'n/a'}，counts={context.get('counts') or {}}\n"
            "请输出 JSON，键名按角色使用 requirement / design / outline / documents。"
        )

    # ------------------------------------------------------------------ #
    def _write_documents(self, ctx, documents: dict, notes: list) -> dict:
        """把文档写入工作区（``write_workspace=False`` 时只留在产物里）。"""
        if not documents:
            return {}
        if not bool(config_value(ctx, self.config, "write_workspace", True)):
            notes.append("已禁用工作区写入，文档仅作为产物返回。")
            return {}
        written: dict = {}
        for name, content in documents.items():
            rel = str(name)
            if not os.path.splitext(rel)[1]:
                rel += ".md"
            try:
                written[rel] = write_workspace_file(ctx, rel, str(content), config=self.config)
            except Exception as exc:  # noqa: BLE001 - 单个文档失败不影响其它文档
                notes.append("写入文档 %s 失败：%s" % (rel, exc))
        if written:
            notes.append("已写入 %d 份文档：%s。" % (len(written), ", ".join(sorted(written))))
        return written

    @staticmethod
    def _summary(payload: dict, documents: dict, written: dict, model_summary: str) -> str:
        """一句话摘要（优先使用模型给的 summary）。"""
        if model_summary:
            return model_summary
        return "%s（产物 %d 份，写入 %d 份）" % (
            payload.get("summary") or "文档角色完成", len(documents), len(written))

    def _publish(self, ctx, payload: dict, documents: dict) -> None:
        """广播文档类产物。"""
        publish_message(ctx, "result", {
            "agent": self.name, "role": self.doc_role,
            "documents": sorted(documents),
            "outline": list(payload.get("outline") or [])[:10],
        })


def _as_dict(value) -> dict:
    """把值规范成 dict（非 dict 返回空）。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_str_map(value) -> dict:
    """把值规范成 ``{名字: 文本内容}``。"""
    if not isinstance(value, dict):
        return {}
    result: dict = {}
    for name, content in value.items():
        if isinstance(content, list):
            content = "\n".join(str(item) for item in content)
        if isinstance(content, str) and content.strip():
            result[str(name)] = content
    return result


__all__ = [
    "DocAgent",
    "ROLES",
    "DEFAULT_REQUIREMENT",
    "DEFAULT_DESIGN",
    "DEFAULT_OUTLINE",
    "render_requirements",
    "render_design",
    "render_outline",
    "render_documents",
    "proofread_documents",
    "render_proofread_notes",
]
