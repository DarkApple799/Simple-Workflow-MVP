"""FixAgent：按问题清单做**确定性重写**，产出 patch 并把修复结果写回工作区。

修复闭环（配合 ``gate_quality`` 的 retry 出边）::

    review 的 issues → 逐条匹配确定性修复策略 → 生成片段级 patch（before/after）
                    → 把修好的整份文件写回工作区 → 复审时 blocking 归零

离线可用性：修复完全由本模块的规则化重写实现（不依赖模型）；模型只在可用时用来
**润色 patch 说明**（``RuleChatModel`` 会返回内置修复模板，其 ``patches[].issue_id``
与规则编号一致，因此可以直接复用它的说明文字）。

无法安全自动推断的问题（结构性重构、通配符导入、语法错误……）一律进 ``remaining``
并写明原因——宁可如实上报，也不生成会破坏代码的"修复"。
"""

from __future__ import annotations

import ast
import logging
import re
import time
from dataclasses import dataclass

from ..graph import AgentResult
from ..util import extract_json_object
from .base import (
    Agent,
    config_value,
    input_results,
    publish_message,
    read_workspace_file,
    state_value,
    write_workspace_file,
)
from .coding import module_name
from .review import issues_from_result, merge_issues, normalize_issue

logger = logging.getLogger("wfa.agents.fix")

#: patch 最多处理的问题条数（防止单次修复无限膨胀）。
MAX_PATCHES = 60

FIX_SYSTEM = (
    "你是一名修复工程师，与审查 Agent 协作。请针对给定的静态检查结论，给出最小、安全的"
    "修复建议，只输出一个 JSON 对象：\n"
    '{"summary": "一句话", "patches": [{"rule_id": "SEC001", "file": "app/service.py",'
    ' "summary": "这次修复做了什么"}]}\n'
    "不要输出完整源码。"
)

#: 给"确定性伪模型"用的最小提示词：命中 RuleChatModel 的 ``_fix_template``。
#: 注意不能出现 review/审查/issue/问题清单 这些更靠前的关键词，否则会命中审查模板。
FIX_COMPACT_PROMPT = "修复阶段：请针对缺陷给出 fix / patch 建议。只输出 JSON：patches[rule_id, summary, file]。"


# --------------------------------------------------------------------------- #
# 行定位与源码小工具
# --------------------------------------------------------------------------- #
_BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*(#.*)?$")
_EXCEPT_HEADER = re.compile(r"^\s*except\b.*:\s*(#.*)?$")
_DEF_HEADER = re.compile(r"^\s*(?:async\s+)?(def|class)\s+\w+")
_IMPORT_LINE = re.compile(r"^\s*(?:import|from)\s+\w")
_FOR_STMT = re.compile(r"^\s*for\s+\w+\s+in\s+")


def _render(lines: list) -> str:
    """把行列表还原为源码文本（统一以换行结尾）。"""
    return "\n".join(lines).rstrip("\n") + "\n"


def _indent_of(line: str) -> str:
    """取一行的缩进空白。"""
    return line[: len(line) - len(line.lstrip())]


def _locate(lines: list, issue: dict, pattern):
    """定位目标行：优先 ``issue["line"]``，否则全文件找最接近的一行。"""
    expected = issue.get("line") if isinstance(issue, dict) else None
    if isinstance(expected, int) and 1 <= expected <= len(lines):
        if pattern.search(lines[expected - 1]):
            return expected - 1
    best = None
    for index, line in enumerate(lines):
        if not pattern.search(line):
            continue
        if best is None:
            return index
        if expected and abs(index + 1 - expected) < abs(best + 1 - expected):
            best = index
    return best


def _header_end(lines: list, start: int) -> int:
    """找到 ``def`` / ``class`` 声明结束的那一行（支持跨行签名）。"""
    depth = 0
    for offset in range(start, min(start + 12, len(lines))):
        depth += lines[offset].count("(") - lines[offset].count(")")
        if depth <= 0 and lines[offset].rstrip().endswith(":"):
            return offset
    return start


def _after_docstring(lines: list) -> int:
    """返回模块文档字符串之后的行下标（没有 docstring 时返回首个非空行）。"""
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return 0
    stripped = lines[index].lstrip()
    for quote in ('"""', "'''"):
        if not stripped.startswith(quote):
            continue
        if stripped.count(quote) >= 2:
            return index + 1
        for offset in range(index + 1, len(lines)):
            if quote in lines[offset]:
                return offset + 1
        return index + 1
    return index


def _import_insert_index(lines: list) -> int:
    """找到 import 块的插入位置（``from __future__`` 必须保持在最前面）。"""
    start = _after_docstring(lines)
    last = -1
    for index in range(start, min(start + 80, len(lines))):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("import ") or stripped.startswith("from "):
            last = index
            continue
        break
    return last + 1 if last >= 0 else start


def _has_import(content: str, module: str) -> bool:
    """源码里是否已经导入过某模块。"""
    pattern = r"(?m)^\s*(?:import\s+%s\b|from\s+%s\s+import\b)" % (re.escape(module), re.escape(module))
    return bool(re.search(pattern, content))


def _ensure_import(content: str, statement: str) -> str:
    """确保源码里有 ``statement`` 这条 import（已存在则原样返回）。"""
    parts = statement.split()
    module = parts[1].split(".")[0] if len(parts) > 1 else statement
    if _has_import(content, module):
        return content
    lines = content.splitlines()
    lines.insert(_import_insert_index(lines), statement)
    return _render(lines)


def _ensure_logger(content: str, module: str) -> str:
    """确保源码里有 ``import logging`` 与模块级 ``logger``。"""
    content = _ensure_import(content, "import logging")
    if re.search(r"(?m)^\s*logger\s*=", content):
        return content
    lines = content.splitlines()
    index = _import_insert_index(lines)
    lines[index:index] = ["", 'logger = logging.getLogger("%s")' % module]
    return _render(lines)


def _ensure_as_exc(header: str) -> str:
    """给 except 头补上 ``as exc``（已有别名则原样返回）。"""
    if re.search(r"\bas\s+\w+", header):
        return header
    return re.sub(r":(\s*(?:#.*)?)$", r" as exc:\1", header)


def _find_ignored_body(lines: list, header: int, indent_len: int):
    """在 except 头之后找到 ``pass`` / ``continue`` 那一行。"""
    for offset in range(header + 1, min(header + 6, len(lines))):
        stripped = lines[offset].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("pass", "continue") and len(_indent_of(lines[offset])) > indent_len:
            return offset
        return None
    return None


def _in_loop(content: str, lineno: int) -> bool:
    """判断第 ``lineno`` 行的 except 是否位于循环体内（决定是否补 ``continue``）。"""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    parents: dict = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.lineno == lineno:
            current: object = node
            while id(current) in parents:
                current = parents[id(current)]
                if isinstance(current, (ast.For, ast.While)):
                    return True
            return False
    return False


@dataclass
class _FixOutcome:
    """一次确定性重写的结果：新内容 + 片段级 patch。"""

    content: str
    before: str
    after: str
    summary: str


def _first_diff(before_text: str, after_text: str) -> tuple:
    """取两段文本的首个差异块（用于整文件级 patch 的展示）。"""
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    start = 0
    limit = min(len(before_lines), len(after_lines))
    while start < limit and before_lines[start] == after_lines[start]:
        start += 1
    end_before, end_after = len(before_lines), len(after_lines)
    while (end_before > start and end_after > start
           and before_lines[end_before - 1] == after_lines[end_after - 1]):
        end_before -= 1
        end_after -= 1
    return ("\n".join(before_lines[start:end_before]) or "（无差异）",
            "\n".join(after_lines[start:end_after]) or "（无差异）")


# --------------------------------------------------------------------------- #
# 确定性修复策略（规则编号 → 重写函数）
# --------------------------------------------------------------------------- #
def _fix_bug001(content: str, issue: dict, hint: dict):
    """裸 except → ``except Exception as exc:`` 并记录日志。"""
    content = _ensure_logger(content, hint.get("module") or "module")
    lines = content.splitlines()
    index = _locate(lines, issue, _BARE_EXCEPT)
    if index is None:
        return None
    indent = _indent_of(lines[index])
    before = lines[index]
    after_lines = [f"{indent}except Exception as exc:",
                   f'{indent}    logger.warning("捕获异常: %s", exc)']
    lines[index:index + 1] = after_lines
    return _FixOutcome(_render(lines), before, "\n".join(after_lines),
                       "裸 except 改为 except Exception 并记录日志")


def _fix_bug002(content: str, issue: dict, hint: dict):
    """``except ...: pass/continue`` → 记录日志（循环内继续 ``continue``）。"""
    content = _ensure_logger(content, hint.get("module") or "module")
    lines = content.splitlines()
    index = _locate(lines, issue, _EXCEPT_HEADER)
    if index is None:
        return None
    indent = _indent_of(lines[index])
    body = _find_ignored_body(lines, index, len(indent))
    if body is None:
        return None
    loop = _in_loop(content, index + 1)
    after_lines = [_ensure_as_exc(lines[index]),
                   f'{indent}    logger.warning("异常已记录并跳过: %s", exc)']
    if loop:
        after_lines.append(f"{indent}    continue")
    before = "\n".join(lines[index:body + 1])
    lines[index:body + 1] = after_lines
    summary = "异常不再静默丢弃：记录日志" + ("并跳过当前项" if loop else "")
    return _FixOutcome(_render(lines), before, "\n".join(after_lines), summary)


def _fix_bug003(content: str, issue: dict, hint: dict):
    """可变默认参数 → 默认 ``None`` + 函数体内兜底赋值（仅处理单行签名）。"""
    lines = content.splitlines()
    index = _locate(lines, issue, _DEF_HEADER)
    if index is None:
        return None
    if _header_end(lines, index) != index or not lines[index].rstrip().endswith(":"):
        return None
    match = re.search(r"\b(\w+)\s*=\s*(\[\]|\{\}|list\(\)|dict\(\)|set\(\))\s*(?=[,)])", lines[index])
    if not match:
        return None
    name = match.group(1)
    default = {"[]": "[]", "list()": "[]", "{}": "{}", "dict()": "{}", "set()": "set()"}[match.group(2)]
    new_header = lines[index][:match.start()] + f"{name}=None" + lines[index][match.end():]
    indent = _indent_of(lines[index])
    before = lines[index]
    replacement = [new_header, f"{indent}    if {name} is None:", f"{indent}        {name} = {default}"]
    lines[index:index + 1] = replacement
    return _FixOutcome(_render(lines), before, "\n".join(replacement),
                       f"可变默认参数 {name} 改为 None 并在函数体内初始化")


def _fix_bug004(content: str, issue: dict, hint: dict):
    """``== None`` / ``!= None`` → ``is None`` / ``is not None``。"""
    lines = content.splitlines()
    index = _locate(lines, issue, re.compile(r"[=!]=\s*None\b"))
    if index is None:
        return None
    before = lines[index]
    after = re.sub(r"!=\s*None\b", "is not None", before)
    after = re.sub(r"(?<![=!<>])==\s*None\b", "is None", after)
    if after == before:
        return None
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "与 None 的比较改用 is / is not")


def _fix_bug006(content: str, issue: dict, hint: dict):
    """遍历时修改容器 → 改为遍历副本 ``list(...)``。"""
    lines = content.splitlines()
    expected = issue.get("line") if isinstance(issue, dict) else None
    index = None
    for offset, line in enumerate(lines):
        if not _FOR_STMT.match(line):
            continue
        if expected is None:
            index = offset
            break
        if offset + 1 <= expected:
            index = offset
    if index is None:
        return None
    match = re.match(r"^(\s*for\s+\w+\s+in\s+)([^:]+?)(\s*:.*)$", lines[index])
    if not match:
        return None
    expression = match.group(2).strip()
    if expression.startswith("list(") or expression.startswith("tuple("):
        return None
    before = lines[index]
    after = f"{match.group(1)}list({expression}){match.group(3)}"
    lines[index] = after
    return _FixOutcome(_render(lines), before, after,
                       f"改为遍历副本 list({expression})，避免边遍历边修改")


def _fix_sec001(content: str, issue: dict, hint: dict):
    """硬编码密钥 → 从环境变量读取。"""
    content = _ensure_import(content, "import os")
    pattern = re.compile(
        r"""(?P<prefix>["']?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"""
        r"""|private[_-]?key|auth[_-]?token|client[_-]?secret)["']?\s*[:=]\s*)"""
        r"""(?P<quote>["'])(?P<value>[^"']{6,})(?P=quote)""",
        re.IGNORECASE,
    )
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    match = pattern.search(before)
    if not match:
        return None
    name = re.sub(r"[^A-Za-z0-9_]", "", match.group("prefix")).upper() or "APP_SECRET"
    after = (before[:match.start()] + match.group("prefix")
             + 'os.environ.get("%s", "")' % name + before[match.end():])
    lines[index] = after
    return _FixOutcome(_render(lines), before, after,
                       f"密钥 {name} 改为从环境变量读取，不再硬编码在源码里")


def _fix_sec002(content: str, issue: dict, hint: dict):
    """``eval(...)`` → ``ast.literal_eval(...)``；``exec`` 无法安全重写则交给人工。"""
    pattern = re.compile(r"(?<![\w.])(eval|exec)\s*\(")
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    match = pattern.search(lines[index])
    if match is None or match.group(1) == "exec":
        return None
    content = _ensure_import(content, "import ast")
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    after = pattern.sub("ast.literal_eval(", before)
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "eval 改为 ast.literal_eval")


def _fix_sec003(content: str, issue: dict, hint: dict):
    """``shell=True`` → ``shell=False``。"""
    pattern = re.compile(r"shell\s*=\s*True")
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    after = pattern.sub("shell=False", before)
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "去掉 shell=True，避免命令注入")


def _fix_sec004(content: str, issue: dict, hint: dict):
    """``os.system(cmd)`` → ``subprocess.run(cmd, shell=False, check=False)``。"""
    content = _ensure_import(content, "import subprocess")
    pattern = re.compile(r"os\.system\(([^()]*)\)")
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    match = pattern.search(before)
    if not match:
        return None
    argument = match.group(1).strip() or '""'
    after = (before[:match.start()]
             + f"subprocess.run({argument}, shell=False, check=False)"
             + before[match.end():])
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "os.system 改为 subprocess.run（shell=False）")


def _fix_sec005(content: str, issue: dict, hint: dict):
    """不安全反序列化 → ``json`` / ``yaml.safe_load``。"""
    pickle_pattern = re.compile(r"\b(?:pickle|cPickle|_pickle|dill|marshal)\.(?P<attr>loads?)\s*\(")
    yaml_pattern = re.compile(r"\byaml\.load\s*\(")
    if pickle_pattern.search(content):
        content = _ensure_import(content, "import json")
        pattern, replacement = pickle_pattern, None
    elif yaml_pattern.search(content):
        pattern, replacement = yaml_pattern, "yaml.safe_load("
    else:
        return None
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    match = pattern.search(before)
    if match is None:
        return None
    if replacement is None:
        replacement = "json.load(" if match.groupdict().get("attr") == "load" else "json.loads("
    after = pattern.sub(replacement, before)
    lines[index] = after
    return _FixOutcome(_render(lines), before, after,
                       "不安全反序列化改为 json / yaml.safe_load")


def _fix_maint004(content: str, issue: dict, hint: dict):
    """补文档字符串（模块级 / 函数 / 类）。"""
    if int(issue.get("line") or 0) == 1 and "模块" in str(issue.get("detail") or ""):
        return _add_module_docstring(content, issue)
    lines = content.splitlines()
    index = _locate(lines, issue, _DEF_HEADER)
    if index is None:
        return None
    match = re.match(r"\s*(?:async\s+)?(def|class)\s+(\w+)", lines[index])
    name = match.group(2) if match else "该对象"
    header_end = _header_end(lines, index)
    if not lines[header_end].rstrip().endswith(":"):
        return None
    following = lines[header_end + 1].strip() if header_end + 1 < len(lines) else ""
    if following.startswith('"""') or following.startswith("'''"):
        return None
    indent = _indent_of(lines[index])
    doc = f'{indent}    """{name}：{name} 的职责说明（由 FixAgent 自动补充）。"""'
    before = "\n".join(lines[index:header_end + 1])
    lines.insert(header_end + 1, doc)
    return _FixOutcome(_render(lines), before, before + "\n" + doc,
                       f"为 {name} 补充文档字符串")


def _add_module_docstring(content: str, issue: dict):
    """在文件开头补一句模块文档字符串。"""
    lines = content.splitlines()
    if _after_docstring(lines) > 0:
        return None
    doc = '"""模块职责说明（由 FixAgent 自动补充）。"""'
    lines[0:0] = [doc, ""]
    return _FixOutcome(_render(lines), "（文件开头）", doc, "补充模块文档字符串")


def _fix_maint005(content: str, issue: dict, hint: dict):
    """遗留标记 → ``# NOTE``（保留原文说明，便于追溯）。"""
    pattern = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b")
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    after = pattern.sub("# NOTE", before)
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "遗留标记改为 NOTE 并纳入跟踪")


def _fix_maint006(content: str, issue: dict, hint: dict):
    """未使用导入 → 注释掉该行（保留原文便于恢复）。"""
    lines = content.splitlines()
    index = _locate(lines, issue, _IMPORT_LINE)
    if index is None:
        return None
    before = lines[index]
    if before.count("(") > before.count(")"):
        return None  # 跨行 import，注释掉会留下悬空括号
    indent = _indent_of(before)
    after = f"{indent}# {before.strip()}  # 未使用，已由 FixAgent 注释"
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "注释掉未使用的导入")


def _fix_maint009(content: str, issue: dict, hint: dict):
    """``print(...)`` → ``logger.info(...)``。"""
    content = _ensure_logger(content, hint.get("module") or "module")
    pattern = re.compile(r"(?<![\w.])print\s*\(")
    lines = content.splitlines()
    index = _locate(lines, issue, pattern)
    if index is None:
        return None
    before = lines[index]
    after = pattern.sub("logger.info(", before)
    lines[index] = after
    return _FixOutcome(_render(lines), before, after, "调试 print 改为 logger.info")


#: 规则编号 → 确定性修复策略。
FIXERS: dict = {
    "BUG001": _fix_bug001,
    "BUG002": _fix_bug002,
    "BUG003": _fix_bug003,
    "BUG004": _fix_bug004,
    "BUG006": _fix_bug006,
    "SEC001": _fix_sec001,
    "SEC002": _fix_sec002,
    "SEC003": _fix_sec003,
    "SEC004": _fix_sec004,
    "SEC005": _fix_sec005,
    "MAINT004": _fix_maint004,
    "MAINT005": _fix_maint005,
    "MAINT006": _fix_maint006,
    "MAINT009": _fix_maint009,
}

#: 明确"必须人工处理"的规则（附原因，写进 remaining）。
MANUAL_RULES = {
    "SYN001": "语法错误需要人工修复，自动重写可能进一步破坏结构",
    "MAINT001": "函数过长需要结构性拆分，自动化重写风险高",
    "MAINT002": "圈复杂度需要重构控制流，自动化重写风险高",
    "MAINT003": "参数过多需要重新设计接口，自动化重写风险高",
    "MAINT007": "通配符导入需要确认实际用到的名字，无法自动展开",
    "MAINT008": "遮蔽内置名需要重命名并同步所有引用，交由人工处理",
    "STYLE001": "行长度问题交给格式化工具（black/ruff）统一处理",
    "SEC006": "assert 的校验语义需要人工改写为显式异常",
}


# --------------------------------------------------------------------------- #
# 修复报告
# --------------------------------------------------------------------------- #
def render_fix_report(patches: list, fixed: list, remaining: list, written: dict) -> str:
    """渲染修复报告（Markdown），记录已修 / 未修与写回的文件。"""
    lines = [
        "# 修复报告", "",
        f"- 生成 patch：{len(patches)} 条",
        f"- 已修复问题：{len(fixed)} 条",
        f"- 未修复问题：{len(remaining)} 条",
        f"- 写回工作区文件：{len(written)} 个" + (f"（{', '.join(sorted(written))}）" if written else ""),
        "", "## Patch 明细", "",
    ]
    for patch in patches:
        lines.append(f"### {patch.get('issue_id')} · {patch.get('file')}")
        lines.append(f"- 修复说明：{patch.get('summary')}")
        lines.append("- before：")
        lines.append("```python")
        lines.append(str(patch.get("before") or ""))
        lines.append("```")
        lines.append("- after：")
        lines.append("```python")
        lines.append(str(patch.get("after") or ""))
        lines.append("```")
    lines.extend(["", "## 未修复问题", ""])
    if not remaining:
        lines.append("（无）")
    for item in remaining:
        lines.append(f"- [{item.get('severity')}] {item.get('title')} "
                     f"(`{item.get('file')}:{item.get('line') or '-'}`)：{item.get('reason')}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# FixAgent
# --------------------------------------------------------------------------- #
class FixAgent(Agent):
    """修复 Agent：按 issue 列表做确定性重写，产出 patch 并写回工作区。"""

    name = "fixer"
    role = "修复 Agent"

    def __init__(self, model=None, *, config=None, bus=None, name: str = "fixer",
                 role: str = "修复 Agent") -> None:
        super().__init__(model, config=config, bus=bus)
        self.name = name
        self.role = role

    # ------------------------------------------------------------------ #
    def run(self, ctx) -> AgentResult:
        """执行修复，返回 ``kind="fix"`` 的结果。"""
        started = time.time()
        notes: list = []
        issues = self._collect_issues(ctx)
        if not issues:
            notes.append("黑板 issues 为空（或没有前驱审查结果），本次没有可修复的问题。")
            return self._empty_result(started, notes)
        summaries, files, rows = self._model_suggestions(ctx, issues)
        patches, fixed, remaining, contents = self._apply_fixes(ctx, issues, summaries, notes)
        self._apply_model_repair(contents, patches, files, rows, notes)
        written = self._write_back(ctx, contents, notes)
        self._publish_patches(ctx, patches, fixed, remaining)
        report = render_fix_report(patches, fixed, remaining, written)
        summary = ("修复完成：生成 %d 条 patch，已修 %d 条，未修 %d 条，写回 %d 个文件。"
                   % (len(patches), len(fixed), len(remaining), len(written)))
        metrics = {
            "issues": len(issues), "patches": len(patches), "fixed": len(fixed),
            "remaining": len(remaining), "written": len(written),
            "offline": bool(getattr(self.model, "is_deterministic", False)) or self.model is None,
            "duration": round(time.time() - started, 3),
        }
        return AgentResult(
            kind="fix",
            summary=summary,
            decision="continue",
            data={"patches": patches, "fixed": fixed, "remaining": remaining},
            artifacts={"fix/fix_report.md": report},
            metrics=metrics,
            notes=notes,
        )

    def _empty_result(self, started: float, notes: list) -> AgentResult:
        """没有任何待修问题时的结果（结构仍满足契约）。"""
        return AgentResult(
            kind="fix",
            summary="没有可修复的问题（issues 为空）。",
            decision="continue",
            data={"patches": [], "fixed": [], "remaining": []},
            metrics={"issues": 0, "patches": 0, "fixed": 0, "remaining": 0,
                     "duration": round(time.time() - started, 3)},
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    def _collect_issues(self, ctx) -> list:
        """取待修问题：黑板 ``issues`` → 上游审查结果 → 黑板 ``blocking``。"""
        raw = state_value(ctx, "issues", None)
        issues = []
        if isinstance(raw, list):
            issues = [item for item in (normalize_issue(entry) for entry in raw) if item]
        if not issues:
            for _, value in input_results(ctx):
                issues.extend(issues_from_result(value))
        if not issues:
            blocking = state_value(ctx, "blocking", None)
            if isinstance(blocking, list):
                issues = [item for item in (normalize_issue(entry) for entry in blocking) if item]
        return merge_issues(issues)

    def _apply_fixes(self, ctx, issues: list, summaries: dict, notes: list) -> tuple:
        """逐条应用确定性修复策略；返回 (patches, fixed, remaining, 修改后的内容)。"""
        patches: list = []
        fixed: list = []
        remaining: list = []
        contents: dict = {}
        seen: set = set()
        limit = int(config_value(ctx, self.config, "max_findings", 300) or 300)
        for index, issue in enumerate(issues[:min(limit, MAX_PATCHES)], 1):
            issue_id = str(issue.get("id") or "ISSUE-%d" % index)
            if issue_id in seen:
                continue
            seen.add(issue_id)
            outcome = self._fix_one(ctx, issue, issue_id, contents, summaries, remaining, notes)
            if outcome is None:
                continue
            patches.append(outcome)
            fixed.append(issue_id)
        return patches, fixed, remaining, contents

    def _fix_one(self, ctx, issue: dict, issue_id: str, contents: dict, summaries: dict,
                 remaining: list, notes: list):
        """修复单条问题；成功返回 patch dict，失败写 ``remaining`` 并返回 None。"""
        rel = str(issue.get("file") or "").strip()
        rule_id = str(issue.get("rule_id") or "").strip().upper()
        if not rel:
            remaining.append(self._remaining(issue, "缺少 file 字段，无法定位文件"))
            return None
        content = self._load(ctx, rel, contents, notes)
        if content is None:
            remaining.append(self._remaining(issue, "文件在工作区与上游结果里都不存在"))
            return None
        fixer = FIXERS.get(rule_id)
        if fixer is None:
            reason = MANUAL_RULES.get(rule_id, "没有针对 %s 的确定性修复策略" % (rule_id or "未知规则"))
            remaining.append(self._remaining(issue, reason))
            return None
        try:
            outcome = fixer(content, issue, {"module": module_name(rel)})
        except Exception as exc:  # noqa: BLE001 - 单个修复策略失败不影响其它问题
            logger.warning("修复 %s（%s）失败：%s", issue_id, rule_id, exc)
            remaining.append(self._remaining(issue, "自动重写失败：%s" % exc))
            return None
        if outcome is None or outcome.before == outcome.after:
            remaining.append(self._remaining(issue, "自动重写无法安全推断该位置的改法，需人工确认"))
            return None
        contents[rel] = outcome.content
        return {
            "file": rel,
            "issue_id": issue_id,
            "summary": summaries.get(issue_id) or summaries.get(rule_id) or outcome.summary,
            "before": outcome.before,
            "after": outcome.after,
            "rule_id": rule_id,
        }

    def _load(self, ctx, rel: str, contents: dict, notes: list):
        """取某个文件的当前工作副本（工作区 → 上游 files 载荷）。"""
        if rel in contents:
            return contents[rel]
        content = read_workspace_file(ctx, rel, config=self.config)
        if content is not None:
            contents.setdefault(rel, content)
            return content
        for _, raw in input_results(ctx):
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            for key in ("files", "tests"):
                mapping = data.get(key)
                if isinstance(mapping, dict) and rel in mapping and isinstance(mapping[rel], str):
                    notes.append("文件 %s 不在工作区，改用上游 Agent 提供的内容作为修复基线。" % rel)
                    contents.setdefault(rel, mapping[rel])
                    return mapping[rel]
        return None

    def _write_back(self, ctx, contents: dict, notes: list) -> dict:
        """把修好的整份文件写回工作区（``write_workspace=True`` 时）。"""
        if not contents:
            return {}
        if not bool(config_value(ctx, self.config, "write_workspace", True)):
            notes.append("已禁用工作区写入（write_workspace=False），patch 只在结果里返回。")
            return {}
        written: dict = {}
        for rel, content in contents.items():
            try:
                written[rel] = write_workspace_file(ctx, rel, content, config=self.config)
            except Exception as exc:  # noqa: BLE001 - 写回失败要如实上报
                notes.append("写回 %s 失败：%s" % (rel, exc))
        if written:
            notes.append("已把修复后的内容写回工作区：%s。" % ", ".join(sorted(written)))
        return written

    # ------------------------------------------------------------------ #
    def _model_suggestions(self, ctx, issues: list) -> tuple:
        """问模型要修复建议：``(说明文字, 整文件修复版本, patch 行)``。

        说明文字用来润色确定性 patch 的 summary；``files`` 只在**离线确定性伪模型**
        （``is_deterministic=True``，交付内置的"修好版本"）时才会被采纳为最终内容。
        """
        model = self.model
        if model is None:
            return {}, {}, []
        compact = bool(getattr(model, "is_deterministic", False))
        prompt = FIX_COMPACT_PROMPT if compact else self._fix_prompt(ctx, issues)
        messages = [{"role": "system", "content": FIX_SYSTEM}, {"role": "user", "content": prompt}]
        if compact:
            # 确定性伪模型按关键词挑内置模板：系统提示里的关键词会抢先命中，故只发用户消息。
            messages = [{"role": "user", "content": prompt}]
        try:
            response = model.chat(messages)
        except Exception as exc:  # noqa: BLE001 - 拿不到建议就用确定性文案
            logger.warning("修复 Agent 调用模型失败，使用确定性 patch 说明：%s", exc)
            return {}, {}, []
        payload = extract_json_object(str(getattr(response, "content", "") or ""))
        if not isinstance(payload, dict):
            return {}, {}, []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        rows = [row for row in (data.get("patches") or []) if isinstance(row, dict)]
        summaries: dict = {}
        for row in rows:
            key = str(row.get("issue_id") or row.get("rule_id") or "").strip()
            if key and row.get("summary"):
                summaries[key] = str(row["summary"])
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        return summaries, files, rows

    def _apply_model_repair(self, contents: dict, patches: list, files: dict, rows: list,
                            notes: list) -> None:
        """把模型给出的整文件修复版本采纳为最终内容（仅离线确定性模板）。"""
        model = self.model
        if not files or not contents or model is None:
            return
        if not getattr(model, "is_deterministic", False):
            return  # 在线模型不做整文件重写，避免引入不可控改动
        by_file = {str(row.get("file") or ""): row for row in rows}
        for rel, content in files.items():
            rel = str(rel)
            if rel not in contents or not isinstance(content, str) or not content.strip():
                continue
            previous = contents[rel]
            if content == previous:
                continue
            before, after = _first_diff(previous, content)
            if before == after:
                continue
            row = by_file.get(rel) or {}
            contents[rel] = content
            patches.append({
                "file": rel,
                "issue_id": "MODEL-%s" % rel,
                "summary": str(row.get("summary") or "采纳模型给出的整文件修复版本（离线模板）"),
                "before": before,
                "after": after,
                "rule_id": "MODEL",
            })
            notes.append("采纳了模型给出的整文件修复版本：%s（离线模板，作为最终内容写入）。" % rel)

    def _fix_prompt(self, ctx, issues: list) -> str:
        """给真实模型用的修复提示（含问题清单摘要）。"""
        rows = [
            "- [%s] %s (%s:%s) rule=%s" % (item.get("severity"), item.get("title"),
                                           item.get("file"), item.get("line"),
                                           item.get("rule_id"))
            for item in issues[:20]
        ]
        return "请为下列静态检查结论给出修复建议：\n" + "\n".join(rows)

    @staticmethod
    def _remaining(issue: dict, reason: str) -> dict:
        """记录一条无法自动修复的问题（含原因），供报告与门禁解释。"""
        return {
            "id": issue.get("id"), "title": issue.get("title"), "file": issue.get("file"),
            "line": issue.get("line"), "severity": issue.get("severity"),
            "rule_id": issue.get("rule_id"), "reason": reason,
        }

    def _publish_patches(self, ctx, patches: list, fixed: list, remaining: list) -> None:
        """广播修复结果，并把 ``fixed`` / ``remaining`` 写进黑板（report 节点会读）。"""
        state = getattr(ctx, "state", None)
        if state is not None:
            for key, value in (("fixed", fixed), ("remaining", remaining), ("patches", patches)):
                try:
                    state.set(key, value)
                except Exception:  # noqa: BLE001 - 黑板写入失败不影响返回结果
                    logger.debug("写入黑板键 %s 失败", key, exc_info=True)
        publish_message(ctx, "fix", {
            "agent": self.name, "patches": len(patches), "fixed": len(fixed),
            "remaining": len(remaining), "files": sorted({p.get("file") for p in patches}),
        })


__all__ = ["FixAgent", "FIXERS", "MANUAL_RULES", "render_fix_report", "MAX_PATCHES"]
