"""确定性 Python 静态分析规则引擎（复用 Homework1 `cra/analysis.py` 的思路）。

为什么需要这一层？
    LLM 擅长语义判断（逻辑错误、设计问题），但在"数行号、查未使用导入、算复杂度"这类
    机械工作上既慢又容易编造。因此本工程先用确定性规则扫一遍拿到**有据可查的证据**，
    再交给 Agent 去核实、补充与定级——报告里"规则命中"与"模型发现"因此可以分开标注。

实现要点（与 Homework1 一致，此处按 Homework2 契约精简为 22 条规则）：
    * **一次 AST 遍历**：`:class:`_Collector`` 用 ``ast.NodeVisitor`` 走一遍源码，收集
      全部"事实"（裸 except、未使用导入、函数长度/复杂度……），正则只在行级检查
      （密钥、TODO、行长）时辅助使用；
    * **宽容边界**：语法错误不抛异常，而是产出 ``SYN001``；单条规则自身抛异常也不会
      中断整次扫描；单文件命中数受 ``max_hits`` 限制（按严重程度优先保留）；
    * **零第三方依赖**，Python 3.9+ 兼容。

对外接口：
    ``RULES`` / ``RuleHit`` / ``analyze_source`` / ``analyze_python_file`` / ``scan_workspace``
"""

from __future__ import annotations

import ast
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import SEVERITY_ORDER

logger = logging.getLogger("wfa.analysis")

# --------------------------------------------------------------------------- #
# 阈值与常量（集中在一处，便于评审与调参）
# --------------------------------------------------------------------------- #
DEFAULT_MAX_HITS = 30
_MAX_HITS_PER_FILE = DEFAULT_MAX_HITS
_LONG_FUNCTION_LINES = 40
_HIGH_COMPLEXITY = 10
_MAX_ARGUMENTS = 5
_LONG_LINE_LIMIT = 120
_MAX_TOTAL_BYTES = 6_000_000
_READ_BYTES = 200_000

#: 常用于遮蔽检测的内置名（只挑最容易出问题的，避免噪声）。
#: 刻意不含 ``id`` / ``hash`` / ``dir`` 等"很少被调用"的名字，也不含 ``bytes`` / ``format``
#: 等偶发字段名，否则 dataclass 里一个 ``id: str`` 字段就会被误报。
_BUILTINS = frozenset({
    "list", "dict", "set", "tuple", "str", "int", "float", "bool",
    "input", "open", "sum", "min", "max", "filter", "map", "type", "all", "any",
})

#: 在遍历过程中修改容器的方法名（BUG006）。
_MUTATING_METHODS = frozenset({
    "remove", "pop", "append", "insert", "clear", "sort", "extend",
    "add", "discard", "update", "popitem", "setdefault", "reverse",
})

#: 明文密钥的"名字"特征：只对赋值目标 / 字典键 / 关键字参数做 AST 判断，
#: 因此不会命中文档字符串里的示例片段（误报率远低于纯行级正则）。
_SECRET_NAME_PATTERN = re.compile(
    r"""(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"""
    r"""|private[_-]?key|auth[_-]?token|client[_-]?secret)"""
)

#: 明显是占位符/示例值的字面量不算泄露，否则测试夹具会被大面积误报。
_PLACEHOLDER_PATTERN = re.compile(
    r"""(?i)^(x{3,}|y{3,}|\*{3,}|\.{3,}|<[^>]*>|\$\{[^}]*\}|%\(?\w+\)?s|todo|tbd|fixme|
        changeme|placeholder|example|sample|dummy|fake|none|null|your[_-]?\w*|my[_-]?\w*|
        abc123|12345678?|password\d*|passwd\d*|secret\d*|token\d*)""",
    re.VERBOSE,
)

#: 遗留标记（MAINT005）。
_TODO_PATTERN = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.IGNORECASE)

#: 未使用导入分析的例外：这些名字即使"看起来没用"也不算未使用。
_IMPORT_WHITELIST = frozenset({"annotations", "TYPE_CHECKING", "absolute_import", "print_function"})

#: 扫描时默认忽略的目录（优先复用 ``wfa/config.py`` 的口径）。
try:  # pragma: no cover - 兜底分支只在 config 不可用时触发
    from .config import Config as _Config

    DEFAULT_IGNORE_DIRS = tuple(_Config.ignore_dirs)
except Exception:  # noqa: BLE001 - 任何导入问题都不应影响规则引擎可用性
    DEFAULT_IGNORE_DIRS = (
        ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".venv", "venv", "env",
        "node_modules", "dist", "build", "target", "htmlcov", ".wfa", ".dsh",
        "site-packages", ".cache", "vendor", ".wfa_workspace",
    )

_TEXT_KINDS = {
    "python": frozenset({".py", ".pyi"}),
    "code": frozenset({
        ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".h", ".cc",
        ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".ps1", ".psm1",
        ".sql", ".kt", ".swift", ".scala", ".lua", ".pl",
    }),
    "text": frozenset({
        ".md", ".txt", ".rst", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
        ".conf", ".html", ".htm", ".css", ".scss", ".xml", ".csv", ".log", ".env",
        ".gitignore", ".dockerfile", ".properties",
    }),
}

#: 语言标注（报告里给人看）。
_LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".go": "go",
    ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".sql": "sql", ".md": "markdown", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".html": "html", ".css": "css",
    ".xml": "xml",
}


# --------------------------------------------------------------------------- #
# 规则目录与命中结构
# --------------------------------------------------------------------------- #
@dataclass
class RuleHit:
    """一条确定性规则命中（与 Homework1 的 ``Finding`` 语义一致，字段按本工程契约命名）。"""

    rule_id: str
    title: str
    file: str
    line: int | None = None
    severity: str = "medium"
    category: str = "other"
    detail: str = ""
    suggestion: str = ""
    evidence: str = ""
    source: str = "rule"

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            self.severity = "medium"
        if self.line is not None:
            try:
                self.line = int(self.line)
                if self.line <= 0:
                    self.line = None
            except (TypeError, ValueError):
                self.line = None

    @property
    def severity_rank(self) -> int:
        """严重程度排序权重（越大越严重）。"""
        return SEVERITY_ORDER.get(self.severity, 2)

    def issue_id(self) -> str:
        """稳定且可读的问题 id：``规则-文件:行``。"""
        suffix = f":{self.line}" if self.line is not None else ""
        return f"{self.rule_id}-{self.file}{suffix}"

    def to_issue(self) -> dict:
        """转换成契约里的 ``Issue`` dict（engine/report/fix 共用）。"""
        return {
            "id": self.issue_id(),
            "title": self.title,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "source": self.source,
            "rule_id": self.rule_id,
            "evidence": self.evidence,
        }

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的 dict（与 :meth:`to_issue` 等价）。"""
        return self.to_issue()


#: 规则元信息：标题 / 严重度 / 分类（detector 在文件后半部分登记）。
_RULE_META: dict = {
    "SYN001": {"title": "语法错误，无法解析", "severity": "high", "category": "bug"},
    "BUG001": {"title": "裸 except 捕获所有异常", "severity": "medium", "category": "bug"},
    "BUG002": {"title": "异常被静默吞掉（except + pass）", "severity": "high", "category": "bug"},
    "BUG003": {"title": "可变对象作为默认参数", "severity": "high", "category": "bug"},
    "BUG004": {"title": "使用 == None 比较", "severity": "low", "category": "bug"},
    "BUG006": {"title": "修改正在遍历的列表/字典", "severity": "medium", "category": "bug"},
    "SEC001": {"title": "疑似硬编码密钥/口令", "severity": "high", "category": "security"},
    "SEC002": {"title": "使用 eval/exec 执行动态代码", "severity": "high", "category": "security"},
    "SEC003": {"title": "subprocess 使用 shell=True", "severity": "high", "category": "security"},
    "SEC004": {"title": "os.system 直接执行字符串命令", "severity": "high", "category": "security"},
    "SEC005": {"title": "不安全的反序列化（pickle/yaml.load）", "severity": "medium",
               "category": "security"},
    "SEC006": {"title": "assert 用于输入校验", "severity": "low", "category": "bug"},
    "MAINT001": {"title": "函数过长", "severity": "low", "category": "maintainability"},
    "MAINT002": {"title": "圈复杂度过高", "severity": "medium", "category": "maintainability"},
    "MAINT003": {"title": "参数过多", "severity": "low", "category": "maintainability"},
    "MAINT004": {"title": "缺少文档字符串", "severity": "info", "category": "documentation"},
    "MAINT005": {"title": "未解决的 TODO/FIXME", "severity": "info", "category": "maintainability"},
    "MAINT006": {"title": "未使用的导入", "severity": "low", "category": "maintainability"},
    "MAINT007": {"title": "通配符导入（from x import *）", "severity": "medium",
                 "category": "maintainability"},
    "MAINT008": {"title": "变量名遮蔽内置函数", "severity": "low", "category": "maintainability"},
    "MAINT009": {"title": "调试用 print 语句", "severity": "low", "category": "maintainability"},
    "STYLE001": {"title": "行长度超限", "severity": "info", "category": "style"},
}


def _hit(rule_id: str, *, file: str, line: int | None, detail: str,
         suggestion: str = "", evidence: str = "") -> RuleHit:
    """按规则目录生成一条命中（标题/严重度/分类自动补齐）。"""
    meta = _RULE_META.get(rule_id, {"title": rule_id, "severity": "medium", "category": "other"})
    return RuleHit(
        rule_id=rule_id,
        title=meta["title"],
        file=file,
        line=line,
        severity=meta["severity"],
        category=meta["category"],
        detail=detail,
        suggestion=suggestion,
        evidence=evidence[:300],
    )


def rule_catalog() -> list:
    """返回规则的元信息清单（不含 detector，供文档与 CLI 展示）。"""
    return [
        {"id": key, "title": meta["title"], "severity": meta["severity"], "category": meta["category"]}
        for key, meta in _RULE_META.items()
    ]


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _is_test_file(path: str) -> bool:
    """测试文件判定（按业界惯例放宽部分规则）。"""
    normalized = (path or "").replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "/tests/" in f"/{normalized}"
    )


def _looks_like_placeholder(value: str) -> bool:
    """判断疑似密钥的值是不是明显的占位符（如 ``<YOUR_KEY>``、``changeme``）。"""
    text = (value or "").strip()
    if len(text) < 10:
        return True
    return bool(_PLACEHOLDER_PATTERN.match(text))


def _unparse(node: ast.AST) -> str:
    """安全地取回表达式的源码文本（3.9 才有 ``ast.unparse``，故加保护）。"""
    unparse = getattr(ast, "unparse", None)
    if unparse is None:  # pragma: no cover - 3.8 兜底
        return ""
    try:
        return unparse(node)
    except Exception:  # noqa: BLE001 - 反解析失败不应影响规则引擎
        return ""


def _expr_key(node: ast.AST) -> str:
    """把表达式压成比较用的键：优先源码文本，退化为名字。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _unparse(node) or node.attr
    return _unparse(node)


def _container_key(node: ast.AST) -> str:
    """取"正在被遍历的容器"键，并把 ``.keys()/.items()/.values()`` 归一化掉。"""
    key = _expr_key(node)
    for suffix in (".keys()", ".items()", ".values()"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _target_name(target: ast.AST) -> str:
    """取赋值目标的"名字"（``Name`` / ``Attribute``；下标等复杂目标返回空串）。"""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _complexity(node: ast.AST) -> int:
    """近似圈复杂度：1 + 判定点数量（与 Homework1 口径一致）。"""
    score = 1
    match_type = getattr(ast, "Match", None)
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While,
                              ast.ExceptHandler, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(len(child.values) - 1, 0)
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        elif match_type is not None and isinstance(child, match_type):  # Python 3.10+
            score += len(getattr(child, "cases", []) or [])
    return score


# --------------------------------------------------------------------------- #
# 一次遍历收集事实
# --------------------------------------------------------------------------- #
@dataclass
class _FuncInfo:
    """函数摘要（长度 / 复杂度 / 参数 / 文档 / 是否模块级）。"""

    name: str
    line: int
    length: int
    complexity: int
    args: int
    has_docstring: bool
    is_module_level: bool


@dataclass
class _Facts:
    """一次 AST 遍历产出的全部事实，供各规则 detector 只读消费。"""

    source: str = ""
    file: str = ""
    lines: list = field(default_factory=list)
    is_test_file: bool = False
    has_module_docstring: bool = False
    functions: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    imports: list = field(default_factory=list)          # (绑定名, 行号, 原始语句)
    used_names: set = field(default_factory=set)
    unused_imports: list = field(default_factory=list)   # (绑定名, 行号, 原始语句)
    bare_excepts: list = field(default_factory=list)
    silent_excepts: list = field(default_factory=list)
    mutable_defaults: list = field(default_factory=list)  # (函数名, 行号, 默认值类型)
    none_compares: list = field(default_factory=list)
    container_mutations: list = field(default_factory=list)  # (调用节点, 容器键)
    eval_exec: list = field(default_factory=list)        # (调用节点, 函数名)
    shell_true: list = field(default_factory=list)
    os_system: list = field(default_factory=list)
    unsafe_deserialize: list = field(default_factory=list)  # (调用节点, 原因)
    asserts: list = field(default_factory=list)
    wildcard_imports: list = field(default_factory=list)  # (行号, 模块名)
    shadowed: list = field(default_factory=list)          # (节点, 名字)
    prints: list = field(default_factory=list)
    todos: list = field(default_factory=list)             # (行号, 标记, 内容)
    secrets: list = field(default_factory=list)           # (行号, 名称, 证据)
    long_lines: list = field(default_factory=list)


class _Collector(ast.NodeVisitor):
    """单次遍历的收集器：只收集事实，不做判断（判断放在 detector 里）。"""

    def __init__(self, tree: ast.Module, source: str, file: str) -> None:
        self.tree = tree
        self.source = source
        self.file = file
        self.facts = _Facts(source=source, file=file, lines=source.splitlines())
        self.facts.is_test_file = _is_test_file(file)
        self.facts.has_module_docstring = bool(ast.get_docstring(tree))
        self._depth = 0
        self._class_depth = 0

    # -- 入口 -------------------------------------------------------------- #
    def run(self) -> _Facts:
        """遍历模块顶层语句，最后补上行级与导入级检查。"""
        for node in self.tree.body:
            self.visit(node)
        self._finish_imports()
        self._finish_source_lines()
        return self.facts

    # -- 名字与导入 -------------------------------------------------------- #
    def visit_Name(self, node: ast.Name) -> None:
        self.facts.used_names.add(node.id)
        if isinstance(node.ctx, ast.Store) and node.id in _BUILTINS:
            self.facts.shadowed.append((node, node.id))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root: ast.AST = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            self.facts.used_names.add(root.id)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # 字符串里出现的名字（__all__、字符串注解）也算"使用过"，避免未使用导入误报。
        if isinstance(node.value, str):
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                self.facts.used_names.add(token)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.facts.imports.append((bound, node.lineno, f"import {alias.name}"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self.facts.wildcard_imports.append((node.lineno, module))
                continue
            bound = alias.asname or alias.name
            self.facts.imports.append((bound, node.lineno, f"from {module} import {alias.name}"))
        self.generic_visit(node)

    # -- 类与函数 ---------------------------------------------------------- #
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.facts.classes.append(node)
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node) -> None:
        """函数级检查：参数遮蔽、可变默认值、长度、复杂度、文档。"""
        args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        for extra in (node.args.vararg, node.args.kwarg):
            if extra is not None:
                args.append(extra)
        self._check_arg_shadowing(args)

        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        for default in defaults:
            kind = self._mutable_default_kind(default)
            if kind:
                self.facts.mutable_defaults.append((node.name, node.lineno, kind))
                break

        end_lineno = getattr(node, "end_lineno", node.lineno) or node.lineno
        self.facts.functions.append(_FuncInfo(
            name=node.name,
            line=node.lineno,
            length=end_lineno - node.lineno + 1,
            complexity=_complexity(node),
            args=len(args),
            has_docstring=bool(ast.get_docstring(node)),
            is_module_level=(self._depth == 0 and self._class_depth == 0),
        ))
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def _check_arg_shadowing(self, args: list) -> None:
        """参数名遮蔽内置名（``def f(list): ...``）同样要提示。"""
        for argument in args:
            if argument.arg in _BUILTINS:
                self.facts.shadowed.append((argument, argument.arg))

    @staticmethod
    def _mutable_default_kind(default: ast.AST) -> str:
        """识别可变默认参数，返回类型描述；不可变返回空串。"""
        if isinstance(default, ast.List):
            return "[]"
        if isinstance(default, ast.Dict):
            return "{}"
        if isinstance(default, ast.Set):
            return "{...}"
        if isinstance(default, ast.Call) and isinstance(default.func, ast.Name):
            if default.func.id in ("list", "dict", "set"):
                return f"{default.func.id}()"
        return ""

    # -- 异常 -------------------------------------------------------------- #
    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            if handler.type is None:
                self.facts.bare_excepts.append(handler)
                continue
            body = handler.body
            only_ignored = len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue))
            if only_ignored:
                self.facts.silent_excepts.append(handler)
        self.generic_visit(node)

    # -- 遍历中修改容器 ---------------------------------------------------- #
    def visit_For(self, node: ast.For) -> None:
        if isinstance(node.iter, (ast.Name, ast.Attribute)):
            key = _container_key(node.iter)
            if key:
                for child in ast.walk(node):
                    if self._is_container_mutation(child, key):
                        self.facts.container_mutations.append((child, key))
                        break
        self.generic_visit(node)

    @staticmethod
    def _is_container_mutation(child: ast.AST, key: str) -> bool:
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
            return False
        if child.func.attr not in _MUTATING_METHODS:
            return False
        return _expr_key(child.func.value) == key

    # -- 明文密钥（AST 判断：赋值 / 字典键 / 关键字参数） -------------------- #
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_secret(_target_name(target), node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_secret(_target_name(node.target), node.value, node.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self._record_secret(key.value, value, node.lineno)
        self.generic_visit(node)

    def _record_secret(self, name: str, value: ast.AST | None, line: int) -> None:
        """只有"名字像密钥 + 值是字符串字面量 + 不是占位符"才登记为疑似泄露。"""
        if not name or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return
        if not _SECRET_NAME_PATTERN.search(name) or _looks_like_placeholder(value.value):
            return
        if any(item[0] == line and item[1] == name for item in self.facts.secrets):
            return
        self.facts.secrets.append((line, name, f"{name}=***"))

    # -- 比较 -------------------------------------------------------------- #
    def visit_Compare(self, node: ast.Compare) -> None:
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                if comparator.value is None:
                    self.facts.none_compares.append(node)
                    break
        self.generic_visit(node)

    # -- 调用 -------------------------------------------------------------- #
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        if name in ("eval", "exec"):
            self.facts.eval_exec.append((node, name))
        elif name == "print" and isinstance(func, ast.Name):
            self.facts.prints.append(node)
        for keyword in node.keywords:
            if keyword.arg:
                self._record_secret(keyword.arg, keyword.value, node.lineno)
        if isinstance(func, ast.Attribute):
            self._check_owner_calls(node, func)
        self._check_shell_true(node)
        self.generic_visit(node)

    def _check_owner_calls(self, node: ast.Call, func: ast.Attribute) -> None:
        """按"模块.函数"识别的危险调用：os.system / pickle.load / yaml.load。"""
        owner = func.value
        if not isinstance(owner, ast.Name):
            return
        if owner.id == "os" and func.attr == "system":
            self.facts.os_system.append(node)
            return
        if owner.id in ("pickle", "cPickle", "_pickle", "dill", "marshal"):
            if func.attr in ("load", "loads"):
                self.facts.unsafe_deserialize.append(
                    (node, f"{owner.id}.{func.attr} 反序列化不可信数据可导致任意代码执行")
                )
            return
        if owner.id == "yaml" and func.attr == "load" and self._yaml_loader_is_unsafe(node):
            self.facts.unsafe_deserialize.append(
                (node, "yaml.load 未使用 SafeLoader，等价于 unsafe_load")
            )

    @staticmethod
    def _yaml_loader_is_unsafe(node: ast.Call) -> bool:
        """``yaml.load`` 是否未指定 Loader 或使用了不安全的 Loader。"""
        for keyword in node.keywords:
            if keyword.arg != "Loader":
                continue
            loader = keyword.value.attr if isinstance(keyword.value, ast.Attribute) else _expr_key(keyword.value)
            return loader in ("", "Loader", "UnsafeLoader", "CLoader")
        return True

    def _check_shell_true(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                if keyword.value.value is True:
                    self.facts.shell_true.append(node)

    # -- assert ------------------------------------------------------------ #
    def visit_Assert(self, node: ast.Assert) -> None:
        self.facts.asserts.append(node)
        self.generic_visit(node)

    # -- 收尾检查 ---------------------------------------------------------- #
    def _finish_imports(self) -> None:
        declared = {name for name, _, _ in self.facts.imports}
        for name, lineno, statement in self.facts.imports:
            if name in _IMPORT_WHITELIST or name not in declared:
                continue
            if name not in self.facts.used_names:
                self.facts.unused_imports.append((name, lineno, statement))

    def _finish_source_lines(self) -> None:
        """行级检查：遗留标记与行长（密钥检测已改为 AST 判断，见 visit_Assign）。"""
        for index, line in enumerate(self.facts.lines, start=1):
            if len(line) > _LONG_LINE_LIMIT and "http" not in line:
                self.facts.long_lines.append(index)
            todo = _TODO_PATTERN.search(line)
            if todo:
                self.facts.todos.append((index, todo.group(1).upper(), todo.group(2).strip()[:120]))


_FACTS_ATTR = "_wfa_analysis_facts"


def _facts_for(tree: ast.Module, source: str, file: str) -> _Facts:
    """取（或构建）某棵语法树对应的事实集合——保证"一次遍历"。"""
    cached = getattr(tree, _FACTS_ATTR, None)
    if isinstance(cached, _Facts) and cached.source is source and cached.file == file:
        return cached
    facts = _Collector(tree, source, file).run()
    try:
        setattr(tree, _FACTS_ATTR, facts)
    except Exception:  # noqa: BLE001 - 只影响缓存，不影响正确性
        logger.debug("无法缓存 AST 事实集合", exc_info=True)
    return facts


# --------------------------------------------------------------------------- #
# 各规则 detector：签名统一为 (tree, source, file) -> list[RuleHit]
# --------------------------------------------------------------------------- #
def _detect_syn001(tree, source: str, file: str) -> list:
    """语法错误：解析失败时才产出命中（解析成功返回空）。"""
    try:
        ast.parse(source, filename=file)
    except SyntaxError as exc:
        return [_syntax_hit(file, exc)]
    except (ValueError, RecursionError, MemoryError) as exc:
        return [_parse_failure_hit(file, exc)]
    return []


def _detect_bug001(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("BUG001", file=file, line=handler.lineno,
             detail="except: 会连 KeyboardInterrupt/SystemExit 一起吞掉，掩盖真正的错误。",
             suggestion="改为 except Exception as exc: 并记录日志。")
        for handler in facts.bare_excepts
    ]


def _detect_bug002(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("BUG002", file=file, line=handler.lineno,
             detail="捕获异常后直接 pass/continue，异常信息被完全丢弃，出问题无法定位。",
             suggestion="至少记录日志（logging.exception / logger.warning）或向上抛出。")
        for handler in facts.silent_excepts
    ]


def _detect_bug003(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("BUG003", file=file, line=line,
             detail=f"函数 {name} 使用可变对象 {kind} 作为默认参数，该对象在多次调用间共享。",
             suggestion="默认值改用 None，函数体内再 `if param is None: param = []`。")
        for name, line, kind in facts.mutable_defaults
    ]


def _detect_bug004(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("BUG004", file=file, line=node.lineno,
             detail="与 None 比较应使用 is / is not；== 依赖对象的 __eq__ 实现，可能被重载。",
             suggestion="改为 `is None` 或 `is not None`。")
        for node in facts.none_compares
    ]


def _detect_bug006(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    hits = []
    for node, key in facts.container_mutations:
        method = node.func.attr if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else "?"
        hits.append(_hit(
            "BUG006", file=file, line=node.lineno,
            detail=f"在 for 循环体内调用 {key}.{method}()，会改变正在遍历的容器，导致元素被跳过或报错。",
            suggestion=f"改为遍历副本：`for item in list({key}):`，或先收集结果再统一修改。",
        ))
    return hits


def _detect_sec001(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("SEC001", file=file, line=line,
             detail=f"疑似把 {name} 明文写在代码里。",
             suggestion="改为从环境变量/配置中心读取，并从 git 历史中清理该值。",
             evidence=evidence)
        for line, name, evidence in facts.secrets
    ]


def _detect_sec002(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("SEC002", file=file, line=node.lineno,
             detail=f"{name}() 执行动态代码，若输入可控即为远程代码执行漏洞。",
             suggestion="用 ast.literal_eval 或显式的映射表替代。")
        for node, name in facts.eval_exec
    ]


def _detect_sec003(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("SEC003", file=file, line=node.lineno,
             detail="shell=True 会把参数交给系统 shell 解析，拼接外部输入时导致命令注入。",
             suggestion="改为传列表参数并保持 shell=False。")
        for node in facts.shell_true
    ]


def _detect_sec004(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("SEC004", file=file, line=node.lineno,
             detail="os.system 直接拼接字符串执行命令，存在命令注入风险。",
             suggestion="改用 subprocess.run([...], shell=False, check=False)。")
        for node in facts.os_system
    ]


def _detect_sec005(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("SEC005", file=file, line=node.lineno, detail=reason,
             suggestion="pickle 改用 json；yaml.load 改用 yaml.safe_load，并对数据来源做校验。")
        for node, reason in facts.unsafe_deserialize
    ]


def _detect_sec006(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("SEC006", file=file, line=node.lineno,
             detail="assert 在 python -O 优化模式下会被移除，不能用于校验外部输入。",
             suggestion="改为显式 if + raise ValueError。")
        for node in facts.asserts
    ]


def _detect_maint001(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT001", file=file, line=info.line,
             detail=f"函数 {info.name} 有 {info.length} 行（阈值 {_LONG_FUNCTION_LINES} 行），职责可能过多。",
             suggestion="按「取数据/算逻辑/写输出」拆分出小函数，或抽成类。")
        for info in facts.functions if info.length > _LONG_FUNCTION_LINES
    ]


def _detect_maint002(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT002", file=file, line=info.line,
             detail=f"函数 {info.name} 圈复杂度约 {info.complexity}（阈值 {_HIGH_COMPLEXITY}），分支太多难以测试。",
             suggestion="用早返回（guard clause）减少嵌套，或把分支抽成策略函数。")
        for info in facts.functions if info.complexity >= _HIGH_COMPLEXITY
    ]


def _detect_maint003(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT003", file=file, line=info.line,
             detail=f"函数 {info.name} 有 {info.args} 个参数（阈值 {_MAX_ARGUMENTS}），调用方容易传错。",
             suggestion="把相关参数聚合为 dataclass / 配置对象。")
        for info in facts.functions if info.args > _MAX_ARGUMENTS
    ]


def _detect_maint004(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    if facts.is_test_file:
        return []
    hits = _module_docstring_hits(facts, file)
    hits.extend(_function_docstring_hits(facts, file))
    hits.extend(_class_docstring_hits(facts, file))
    return hits


def _module_docstring_hits(facts: _Facts, file: str) -> list:
    """模块级文档字符串检查（无函数的小文件不强制）。"""
    if facts.has_module_docstring or not facts.functions:
        return []
    return [_hit("MAINT004", file=file, line=1,
                 detail="模块缺少文档字符串，阅读者无法快速了解文件用途。",
                 suggestion="在文件开头用三引号写一句模块职责说明。")]


def _function_docstring_hits(facts: _Facts, file: str) -> list:
    """模块级公开函数的文档字符串检查（类方法与内部小函数不强制）。"""
    return [
        _hit("MAINT004", file=file, line=info.line,
             detail=f"公开函数 {info.name} 缺少文档字符串。",
             suggestion="用一行说明「做什么、参数含义、返回什么」。")
        for info in facts.functions
        if info.is_module_level and not info.name.startswith("_") and not info.has_docstring
    ]


def _class_docstring_hits(facts: _Facts, file: str) -> list:
    """公开类的文档字符串检查。"""
    return [
        _hit("MAINT004", file=file, line=node.lineno,
             detail=f"公开类 {node.name} 没有文档字符串。",
             suggestion="补充一行说明该类职责的 docstring。")
        for node in facts.classes
        if not node.name.startswith("_") and not ast.get_docstring(node)
    ]


def _detect_maint005(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT005", file=file, line=line, detail=f"遗留标记 {tag}：{text}",
             suggestion="确认后修复，或转成正式的 issue 跟踪。")
        for line, tag, text in facts.todos
    ]


def _detect_maint006(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT006", file=file, line=line,
             detail=f"导入了 {name} 但未使用（{statement}）。",
             suggestion="删除该导入，或确认是否漏用了本应调用的函数。")
        for name, line, statement in facts.unused_imports
    ]


def _detect_maint007(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT007", file=file, line=line,
             detail=f"from {module} import * 会污染命名空间，且难以静态分析。",
             suggestion="显式列出需要导入的名字。")
        for line, module in facts.wildcard_imports
    ]


def _detect_maint008(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    hits = []
    for node, name in facts.shadowed:
        kind = "参数名" if isinstance(node, ast.arg) else "变量名"
        hits.append(_hit("MAINT008", file=file, line=getattr(node, "lineno", 1),
                         detail=f"{kind} {name!r} 遮蔽了内置名，函数体内将无法直接调用同名内置函数。",
                         suggestion=f"改名为 {name}_value 之类的非内置名。"))
    return hits


def _detect_maint009(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    return [
        _hit("MAINT009", file=file, line=node.lineno,
             detail="使用 print 直接输出：库代码里难以按级别过滤，也不利于线上排查。",
             suggestion="库代码改用 logging.getLogger(__name__)；命令行入口可忽略本提示。")
        for node in facts.prints
    ]


def _detect_style001(tree, source: str, file: str) -> list:
    facts = _facts_for(tree, source, file)
    if not facts.long_lines:
        return []
    first = facts.long_lines[0]
    return [_hit("STYLE001", file=file, line=first,
                 detail=f"共 {len(facts.long_lines)} 行超过 {_LONG_LINE_LIMIT} 字符，首行在第 {first} 行。",
                 suggestion="用 black/ruff format 统一格式化，或拆分长表达式。")]


Detector = Callable[[ast.Module, str, str], list]

_DETECTORS: dict = {
    "SYN001": _detect_syn001,
    "BUG001": _detect_bug001,
    "BUG002": _detect_bug002,
    "BUG003": _detect_bug003,
    "BUG004": _detect_bug004,
    "BUG006": _detect_bug006,
    "SEC001": _detect_sec001,
    "SEC002": _detect_sec002,
    "SEC003": _detect_sec003,
    "SEC004": _detect_sec004,
    "SEC005": _detect_sec005,
    "SEC006": _detect_sec006,
    "MAINT001": _detect_maint001,
    "MAINT002": _detect_maint002,
    "MAINT003": _detect_maint003,
    "MAINT004": _detect_maint004,
    "MAINT005": _detect_maint005,
    "MAINT006": _detect_maint006,
    "MAINT007": _detect_maint007,
    "MAINT008": _detect_maint008,
    "MAINT009": _detect_maint009,
    "STYLE001": _detect_style001,
}

#: 规则清单：每条含 id / title / severity / category / detector。
RULES: list = [
    {
        "id": rule_id,
        "title": meta["title"],
        "severity": meta["severity"],
        "category": meta["category"],
        "detector": _DETECTORS[rule_id],
    }
    for rule_id, meta in _RULE_META.items()
]

RULES_BY_ID: dict = {rule["id"]: rule for rule in RULES}


# --------------------------------------------------------------------------- #
# 对外接口：单文件分析
# --------------------------------------------------------------------------- #
def _syntax_hit(file: str, exc: SyntaxError) -> RuleHit:
    """把语法错误转成一条可读的 SYN001 命中。"""
    line = exc.lineno or 1
    detail = f"第 {line} 行解析失败：{exc.msg}"
    if exc.text:
        detail += f"；原文：{exc.text.strip()[:120]}"
    return _hit("SYN001", file=file, line=line, detail=detail,
                suggestion="先修复语法错误，否则该文件无法参与后续任何静态分析。")


def _parse_failure_hit(file: str, exc: Exception) -> RuleHit:
    """ast 本身处理不了的输入（超深嵌套等）同样不应让整次审查失败。"""
    return _hit("SYN001", file=file, line=1,
                detail=f"解析器无法处理该文件：{type(exc).__name__}: {exc}",
                suggestion="检查是否存在超深嵌套或畸形源码。")


def analyze_source(source: str, rel: str, *, max_hits: int = DEFAULT_MAX_HITS) -> list:
    """分析一段 Python 源码，返回规则命中列表。

    语法错误不会抛异常，而是转成一条 ``SYN001`` 返回——待审查代码有语法错误是常态，
    工具必须能继续工作。单条规则自身出错也不会影响其它规则。
    """
    try:
        tree = ast.parse(source or "", filename=rel)
    except SyntaxError as exc:
        return [_syntax_hit(rel, exc)]
    except (ValueError, RecursionError, MemoryError) as exc:
        return [_parse_failure_hit(rel, exc)]

    hits: list = []
    for rule in RULES:
        if rule["id"] == "SYN001":
            continue  # 解析已成功，语法错误规则自然为空（该 detector 仍可独立调用）
        detector = rule["detector"]
        try:
            hits.extend(detector(tree, source, rel))
        except Exception:  # noqa: BLE001 - 单条规则失败绝不影响整次扫描
            logger.warning("规则 %s 在 %s 上执行失败，已跳过", rule["id"], rel, exc_info=True)

    if max_hits > 0 and len(hits) > max_hits:
        hits.sort(key=lambda item: (-item.severity_rank, item.line or 0, item.rule_id))
        hits = hits[:max_hits]
    return hits


def _read_source(path: str, *, max_bytes: int = _READ_BYTES) -> str:
    """读取源码文本：优先 UTF-8，容忍 BOM 与个别编码问题（替换字符而非抛错）。"""
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes)
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            logger.debug("按 %s 解码失败，尝试下一种编码：%s", encoding, exc)
    return raw.decode("utf-8", errors="replace")


def analyze_python_file(path: str, *, rel: str | None = None,
                        max_hits: int = DEFAULT_MAX_HITS) -> list:
    """分析单个 Python 文件；读取失败时返回空列表并记日志（不抛异常）。"""
    if not path or not os.path.isfile(path):
        logger.debug("待分析文件不存在：%s", path)
        return []
    label = rel or os.path.basename(path)
    try:
        source = _read_source(path)
    except OSError as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
        return []
    return analyze_source(source, label, max_hits=max_hits)


# --------------------------------------------------------------------------- #
# 对外接口：工作区扫描
# --------------------------------------------------------------------------- #
@dataclass
class ScanReport:
    """工作区扫描报告：文件清单 + 规则命中 + 跳过原因 + 备注。"""

    root: str
    files: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    duration: float = 0.0

    def counts(self) -> dict:
        """按严重程度统计命中数量（5 个键始终存在）。"""
        result = {key: 0 for key in SEVERITY_ORDER}
        for hit in self.hits:
            result[hit.severity] = result.get(hit.severity, 0) + 1
        return result

    def issues(self) -> list:
        """命中转换成契约里的 ``Issue`` dict 列表。"""
        return [hit.to_issue() for hit in self.hits]

    def python_files(self) -> list:
        """只返回被规则引擎分析过的 Python 文件路径。"""
        return [item["path"] for item in self.files if item.get("kind") == "python"]

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的 dict（报告与 Web 接口共用）。"""
        return {
            "root": self.root,
            "duration": round(self.duration, 3),
            "files": list(self.files),
            "hits": [hit.to_issue() for hit in self.hits],
            "skipped": list(self.skipped),
            "notes": list(self.notes),
            "counts": self.counts(),
            "stats": {
                "files": len(self.files),
                "python_files": len(self.python_files()),
                "hits": len(self.hits),
            },
        }


def _classify(path: str) -> tuple:
    """按扩展名分类文件，返回 (kind, language)。"""
    ext = os.path.splitext(path)[1].lower()
    for kind, extensions in _TEXT_KINDS.items():
        if ext in extensions:
            return kind, _LANGUAGE_BY_EXT.get(ext, ext.lstrip(".") or "unknown")
    return "binary", _LANGUAGE_BY_EXT.get(ext, ext.lstrip(".") or "unknown")


def _should_skip_dir(name: str, ignore_dirs) -> bool:
    """目录是否在忽略名单内（大小写不敏感）。"""
    return name.lower() in {item.lower() for item in ignore_dirs}


def _iter_paths(root: str, ignore_dirs, max_files: int) -> tuple:
    """遍历工作区，返回 (文件绝对路径列表, 是否被截断)。"""
    paths: list = []
    truncated = False
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if not _should_skip_dir(name, ignore_dirs) and not name.startswith(".")
        )
        for filename in sorted(filenames):
            if filename.startswith(".") and not filename.startswith(".env"):
                continue
            paths.append(os.path.join(current, filename))
            if len(paths) >= max_files:
                truncated = True
                return paths, truncated
    return paths, truncated


def scan_workspace(root: str, *, ignore_dirs=DEFAULT_IGNORE_DIRS, max_files: int = 200,
                   max_file_bytes: int = 200_000) -> ScanReport:
    """扫描工作区：列出文件并对 Python 文件跑全部规则。

    纯确定性、零网络、零 LLM，因此 ``--offline`` 演示与 Agent 降级路径都可以依赖它。
    """
    started = time.time()
    report = ScanReport(root=os.path.abspath(root) if root else "")
    if not root or not os.path.isdir(root):
        report.notes.append(f"工作区不存在或不是目录：{root!r}，未扫描任何文件。")
        report.duration = time.time() - started
        return report

    paths, truncated = _iter_paths(report.root, ignore_dirs, max(0, int(max_files)))
    total_bytes = 0
    python_files = 0
    for path in paths:
        rel = os.path.relpath(path, report.root).replace("\\", "/")
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            report.skipped.append({"path": rel, "reason": f"无法读取文件属性：{exc}"})
            continue
        kind, language = _classify(path)
        entry = {"path": rel, "size": size, "lines": None, "kind": kind, "language": language}
        report.files.append(entry)
        if total_bytes + size > _MAX_TOTAL_BYTES:
            report.skipped.append({"path": rel, "reason": "超过本次扫描的总字节预算"})
            continue
        total_bytes += size
        if kind != "python":
            if kind == "binary":
                report.skipped.append({"path": rel, "reason": "二进制或未知类型，跳过规则分析"})
            continue
        if size > max_file_bytes:
            report.skipped.append({"path": rel, "reason": f"文件超过 {max_file_bytes} 字节，跳过规则分析"})
            continue
        try:
            source = _read_source(path, max_bytes=max_file_bytes)
        except OSError as exc:
            report.skipped.append({"path": rel, "reason": f"读取失败：{exc}"})
            continue
        python_files += 1
        entry["lines"] = len(source.splitlines())
        report.hits.extend(analyze_source(source, rel, max_hits=_MAX_HITS_PER_FILE))

    if truncated:
        report.notes.append(f"文件数量超过限额 max_files={max_files}，本次只扫描前 {len(paths)} 个文件。")
    if python_files == 0:
        report.notes.append("工作区内没有可分析的 Python 文件，确定性规则未产生任何命中。")
    if report.skipped:
        report.notes.append(f"共有 {len(report.skipped)} 个文件被跳过（类型/大小/读取原因见 skipped）。")
    report.duration = time.time() - started
    return report


__all__ = [
    "RuleHit",
    "RULES",
    "RULES_BY_ID",
    "ScanReport",
    "DEFAULT_MAX_HITS",
    "DEFAULT_IGNORE_DIRS",
    "analyze_source",
    "analyze_python_file",
    "scan_workspace",
    "rule_catalog",
]
