"""确定性静态分析引擎（基于 Python 标准库 ast）。

为什么要有这一层？
    LLM 擅长语义判断（逻辑错误、设计问题），但在"数行号、查未使用导入、算复杂度"
    这类机械工作上既慢又容易编造。因此本项目先用确定性规则扫一遍，得到**有据可查的
    证据**，再交给 Agent 去验证、定级、补充语义发现——这也是报告里"规则命中"与
    "模型发现"分开标注的原因。

新增规则只需在 RULES 中登记元信息，并在 Analyzer 里 visit 出 Finding。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from .models import Finding

# --------------------------------------------------------------------------- #
# 规则目录：集中定义规则的严重程度与分类，便于评审与测试
# --------------------------------------------------------------------------- #
RULES: dict[str, dict[str, str]] = {
    "SYN001": {"title": "语法错误，无法解析", "severity": "high", "category": "bug"},
    "BUG001": {"title": "裸 except 捕获所有异常", "severity": "medium", "category": "bug"},
    "BUG002": {"title": "异常被静默吞掉（except + pass）", "severity": "high", "category": "bug"},
    "BUG003": {"title": "可变对象作为默认参数", "severity": "high", "category": "bug"},
    "BUG004": {"title": "使用 == None 比较", "severity": "low", "category": "bug"},
    "BUG005": {"title": "用 type() == 判断类型", "severity": "low", "category": "bug"},
    "BUG006": {"title": "修改正在遍历的列表/字典", "severity": "medium", "category": "bug"},
    "SEC001": {"title": "疑似硬编码密钥/口令", "severity": "high", "category": "security"},
    "SEC002": {"title": "使用 eval/exec 执行动态代码", "severity": "high", "category": "security"},
    "SEC003": {"title": "subprocess 使用 shell=True", "severity": "high", "category": "security"},
    "SEC004": {"title": "os.system 直接执行字符串命令", "severity": "high", "category": "security"},
    "SEC005": {"title": "不安全的反序列化（pickle/yaml.load）", "severity": "medium", "category": "security"},
    "SEC006": {"title": "assert 用于输入校验", "severity": "low", "category": "bug"},
    "MAINT001": {"title": "函数过长", "severity": "low", "category": "maintainability"},
    "MAINT002": {"title": "圈复杂度过高", "severity": "medium", "category": "maintainability"},
    "MAINT003": {"title": "参数过多", "severity": "low", "category": "maintainability"},
    "MAINT004": {"title": "缺少文档字符串", "severity": "info", "category": "documentation"},
    "MAINT005": {"title": "未解决的 TODO/FIXME", "severity": "info", "category": "maintainability"},
    "MAINT006": {"title": "未使用的导入", "severity": "low", "category": "maintainability"},
    "MAINT007": {"title": "通配符导入（from x import *）", "severity": "medium", "category": "maintainability"},
    "MAINT008": {"title": "变量名遮蔽内置函数", "severity": "low", "category": "maintainability"},
    "MAINT009": {"title": "调试用 print 语句", "severity": "low", "category": "maintainability"},
    "STYLE001": {"title": "行长度超限", "severity": "info", "category": "style"},
    "STYLE002": {"title": "行尾空白 / 制表符缩进", "severity": "info", "category": "style"},
    "STYLE003": {"title": "文件过长，建议拆分", "severity": "info", "category": "maintainability"},
}

#: 常用于遮蔽检测的内置名（只挑最容易出问题的，避免噪声）。
#: 刻意不含 id/hash/dir 等"很少被调用"的名字，也不含 bytes/object/format 等偶发字段名，
#: 否则 dataclass 里一个 `id: str` 字段就会被误报。
_BUILTINS = frozenset({
    "list", "dict", "set", "tuple", "str", "int", "float", "bool",
    "input", "open", "sum", "min", "max", "filter", "map", "type", "all", "any",
})

_SECRET_PATTERN = re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\b\s*[:=]\s*["']([^"']{6,})["']"""
)

#: 明显是占位符/示例值的字面量不算泄露，否则测试夹具会被大面积误报。
_PLACEHOLDER_PATTERN = re.compile(
    r"""(?i)^(x{3,}|y{3,}|\*{3,}|\.{3,}|<[^>]*>|\$\{[^}]*\}|%\(?\w+\)?s|
        todo|tbd|fixme|changeme|placeholder|example|sample|dummy|fake|none|null|
        your[_-]?\w*|my[_-]?\w*|abc123|12345678?|password\d*|passwd\d*|secret\d*|token\d*)""",
    re.VERBOSE,
)


def _looks_like_placeholder(value: str) -> bool:
    """判断疑似密钥的值是不是明显的占位符（如 'test-key'、'<YOUR_KEY>'）。"""
    text = (value or "").strip()
    if len(text) < 10:
        return True
    return bool(_PLACEHOLDER_PATTERN.match(text))


def _is_test_file(path: str) -> bool:
    """测试文件：按业界惯例放宽文档字符串类规则。"""
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{normalized}"
_TODO_PATTERN = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.IGNORECASE)
_LONG_LINE_LIMIT = 120
_LONG_FUNCTION_LINES = 40
_HIGH_COMPLEXITY = 10
_MAX_ARGUMENTS = 5
_LONG_FILE_LINES = 500

#: 明确例外：这些名字即使"看起来没用"也不算未使用。
_IMPORT_WHITELIST = frozenset({"annotations", "TYPE_CHECKING", "absolute_import", "print_function"})


@dataclass
class FileAnalysis:
    """单文件分析摘要，供扫描报告与 Agent 摘要使用。"""

    path: str
    loc: int = 0
    functions: list[dict] = field(default_factory=list)
    classes: int = 0
    max_complexity: int = 0
    avg_complexity: float = 0.0
    has_module_docstring: bool = False
    parse_ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "loc": self.loc,
            "classes": self.classes,
            "functions": self.functions[:20],
            "max_complexity": self.max_complexity,
            "avg_complexity": round(self.avg_complexity, 1),
            "has_module_docstring": self.has_module_docstring,
            "parse_ok": self.parse_ok,
            "error": self.error,
        }


def _finding(rule_id: str, *, file: str, line: int | None, detail: str, suggestion: str, evidence: str = "") -> Finding:
    meta = RULES.get(rule_id, {"title": rule_id, "severity": "medium", "category": "other"})
    return Finding(
        title=meta["title"],
        file=file,
        line=line,
        severity=meta["severity"],
        category=meta["category"],
        detail=detail,
        suggestion=suggestion,
        evidence=evidence[:300],
        rule_id=rule_id,
        source="rule",
    )


def _complexity(node: ast.AST) -> int:
    """近似圈复杂度：1 + 判定点数量。"""
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(len(child.values) - 1, 0)
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        elif isinstance(child, ast.Match):  # Python 3.10+
            score += len(getattr(child, "cases", []) or [])
    return score


class Analyzer(ast.NodeVisitor):
    """一次遍历完成所有 AST 层面的规则检查。"""

    def __init__(self, source: str, file: str, tree: ast.Module, *, is_test_file: bool = False) -> None:
        self.source = source
        self.lines = source.splitlines()
        self.file = file
        self.tree = tree
        self.is_test_file = is_test_file
        self.findings: list[Finding] = []
        self.functions: list[dict] = []
        self.classes = 0
        self.used_names: set[str] = set()
        self.imports: list[tuple[str, int, str]] = []  # (绑定名, 行号, 原始语句)
        self._depth = 0          # 函数嵌套深度
        self._class_depth = 0    # 类嵌套深度（用于区分"模块级公开函数"与"类方法/内部函数"）

    # ------------------------------------------------------------------ #
    def run(self) -> list[Finding]:
        for node in self.tree.body:  # 未使用导入分析需要先收集全部名字
            self.visit(node)
        self._check_unused_imports()
        self._check_module_docstring()
        self._check_source_lines()
        return self.findings

    # ------------------------------------------------------------------ #
    # 访问器
    # ------------------------------------------------------------------ #
    def visit_Name(self, node: ast.Name) -> None:
        self.used_names.add(node.id)
        if node.id in _BUILTINS and isinstance(node.ctx, ast.Store):
            self.findings.append(
                _finding(
                    "MAINT008",
                    file=self.file,
                    line=node.lineno,
                    detail=f"变量 {node.id!r} 覆盖了内置名，后续若要调用内置函数会被遮蔽。",
                    suggestion=f"改名为 {node.id}_value 之类的非内置名。",
                )
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            self.used_names.add(root.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.imports.append((bound, node.lineno, f"import {alias.name}"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.findings.append(
                    _finding(
                        "MAINT007",
                        file=self.file,
                        line=node.lineno,
                        detail=f"from {node.module} import * 会污染命名空间，且难以静态分析。",
                        suggestion="显式列出需要导入的名字。",
                    )
                )
                continue
            bound = alias.asname or alias.name
            self.imports.append((bound, node.lineno, f"from {node.module} import {alias.name}"))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes += 1
        if not ast.get_docstring(node) and not node.name.startswith("_") and not self.is_test_file:
            self.findings.append(
                _finding(
                    "MAINT004",
                    file=self.file,
                    line=node.lineno,
                    detail=f"公开类 {node.name} 没有文档字符串。",
                    suggestion="补充一行说明该类职责的 docstring。",
                )
            )
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            if handler.type is None:
                self.findings.append(
                    _finding(
                        "BUG001",
                        file=self.file,
                        line=handler.lineno,
                        detail="except: 会连 KeyboardInterrupt、SystemExit 一起吞掉，掩盖真正的错误。",
                        suggestion="改为 except Exception as exc: 并记录日志。",
                    )
                )
            elif isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                body = handler.body
                only_pass = len(body) == 1 and isinstance(body[0], ast.Pass)
                only_continue = len(body) == 1 and isinstance(body[0], ast.Continue)
                if only_pass or only_continue:
                    self.findings.append(
                        _finding(
                            "BUG002",
                            file=self.file,
                            line=handler.lineno,
                            detail="捕获 Exception 后直接 pass/continue，异常信息被完全丢弃，出问题无法定位。",
                            suggestion="至少记录日志（logging.exception）或向上抛出。",
                        )
                    )
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        """检测"边遍历边修改容器"这一经典缺陷。"""
        if isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Name):
            loop_var = node.target.id
            container = node.iter.id
            methods = {"remove", "pop", "append", "insert", "clear", "sort", "extend", "add", "discard"}
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    owner = child.func.value
                    if (
                        child.func.attr in methods
                        and isinstance(owner, ast.Name)
                        and owner.id == container
                    ):
                        self.findings.append(
                            _finding(
                                "BUG006",
                                file=self.file,
                                line=child.lineno,
                                detail=(
                                    f"在 for {loop_var} in {container} 循环体内调用 {container}.{child.func.attr}()，"
                                    "会改变正在遍历的容器，导致元素被跳过或抛 RuntimeError。"
                                ),
                                suggestion=f"改为遍历副本：`for {loop_var} in list({container}):`，或先收集结果再统一修改。",
                            )
                        )
                        return
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant) and comparator.value is None:
                self.findings.append(
                    _finding(
                        "BUG004",
                        file=self.file,
                        line=node.lineno,
                        detail="与 None 比较应使用 is / is not；== 依赖对象的 __eq__ 实现，可能被重载。",
                        suggestion="改为 `is None` 或 `is not None`。",
                    )
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        self._check_dynamic_execution(node, name)
        if isinstance(func, ast.Attribute):
            self._check_owner_calls(node, func)
        self._check_shell_true(node)
        self.generic_visit(node)

    def _report(self, rule_id: str, node: ast.AST, detail: str, suggestion: str) -> None:
        """按行号记录一条问题（多数规则都只需要位置 + 文案）。"""
        self.findings.append(
            _finding(
                rule_id,
                file=self.file,
                line=getattr(node, "lineno", 1),
                detail=detail,
                suggestion=suggestion,
            )
        )

    def _check_dynamic_execution(self, node: ast.Call, name: str) -> None:
        """eval/exec 与 print 这类"看函数名就知道"的检查。"""
        if name in ("eval", "exec"):
            self._report(
                "SEC002",
                node,
                f"{name}() 执行动态代码，若输入可控即为远程代码执行漏洞。",
                "用 ast.literal_eval 或显式的映射表替代。",
            )
        elif name == "print":
            self._report(
                "MAINT009",
                node,
                "使用 print 直接输出：库代码里难以按级别过滤，也不利于线上排查。",
                "库代码改用 logging.getLogger(__name__)；命令行脚本可忽略本提示。",
            )

    def _check_owner_calls(self, node: ast.Call, func: ast.Attribute) -> None:
        """按"模块.函数"识别的危险调用：os.system / pickle.load / yaml.load。"""
        owner = func.value
        if not isinstance(owner, ast.Name):
            return
        if owner.id == "os" and func.attr == "system":
            self._report(
                "SEC004",
                node,
                "os.system 直接拼接字符串执行命令，存在命令注入风险。",
                "改用 subprocess.run([...], shell=False)。",
            )
        elif owner.id == "pickle" and func.attr in ("load", "loads"):
            self._report(
                "SEC005",
                node,
                "pickle 反序列化不可信数据可导致任意代码执行。",
                "改用 json，或对数据来源做严格校验与签名。",
            )
        elif owner.id == "yaml" and func.attr == "load" and not any(kw.arg == "Loader" for kw in node.keywords):
            self._report(
                "SEC005",
                node,
                "yaml.load 未指定 Loader，等价于 unsafe_load。",
                "改用 yaml.safe_load。",
            )

    def _check_shell_true(self, node: ast.Call) -> None:
        """任何调用只要显式传了 shell=True 都值得提醒。"""
        for keyword in node.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                self._report(
                    "SEC003",
                    node,
                    "shell=True 会把参数交给系统 shell 解析，拼接外部输入时导致命令注入。",
                    "改为传列表参数并保持 shell=False。",
                )

    def visit_Assert(self, node: ast.Assert) -> None:
        self.findings.append(
            _finding(
                "SEC006",
                file=self.file,
                line=node.lineno,
                detail="assert 在 python -O 优化模式下会被移除，不能用于校验外部输入。",
                suggestion="改为显式 if + raise ValueError。",
            )
        )
        self.generic_visit(node)

    # ------------------------------------------------------------------ #
    # 细节检查
    # ------------------------------------------------------------------ #
    def _handle_function(self, node: ast.AST) -> None:
        """函数级检查：参数、默认值、长度、复杂度、文档。"""
        name = node.name  # type: ignore[attr-defined]
        lineno = node.lineno  # type: ignore[attr-defined]
        args = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args) + list(node.args.kwonlyargs)
        has_docstring = bool(ast.get_docstring(node))
        is_module_level = self._depth == 0 and self._class_depth == 0

        self._check_argument_shadowing(args, lineno)
        self._check_mutable_defaults(node, name, lineno)
        length, complexity = self._record_function(node, name, lineno, len(args), has_docstring)
        self._check_function_shape(
            name, lineno, length, complexity, len(args), has_docstring, is_module_level
        )
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def _check_argument_shadowing(self, args: list, lineno: int) -> None:
        for argument in args:
            if argument.arg in _BUILTINS:
                self._report(
                    "MAINT008",
                    argument,
                    f"参数名 {argument.arg!r} 遮蔽了内置名，函数体内将无法直接调用同名内置函数。",
                    f"改名为 {argument.arg}_value 之类的非内置名。",
                )

    def _check_mutable_defaults(self, node: ast.AST, name: str, lineno: int) -> None:
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]  # type: ignore[attr-defined]
        for default in defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id in ("list", "dict", "set")
            ):
                self.findings.append(
                    _finding(
                        "BUG003",
                        file=self.file,
                        line=lineno,
                        detail=f"函数 {name} 使用可变对象作为默认参数，该对象在多次调用间共享，容易被意外修改。",
                        suggestion="默认值改用 None，函数体内再 `if param is None: param = []`。",
                    )
                )
                return

    def _record_function(self, node: ast.AST, name: str, lineno: int, arg_count: int, has_docstring: bool) -> tuple[int, int]:
        """登记函数摘要，返回 (长度, 圈复杂度)。"""
        end_lineno = getattr(node, "end_lineno", lineno) or lineno
        length = end_lineno - lineno + 1
        complexity = _complexity(node)
        self.functions.append(
            {
                "name": name,
                "line": lineno,
                "length": length,
                "complexity": complexity,
                "args": arg_count,
                "docstring": has_docstring,
            }
        )
        return length, complexity

    def _check_function_shape(
        self,
        name: str,
        lineno: int,
        length: int,
        complexity: int,
        arg_count: int,
        has_docstring: bool,
        is_module_level: bool = True,
    ) -> None:
        if length > _LONG_FUNCTION_LINES:
            self._report_at(
                "MAINT001", lineno,
                f"函数 {name} 有 {length} 行（阈值 {_LONG_FUNCTION_LINES} 行），职责可能过多。",
                "按「取数据/算逻辑/写输出」拆分出小函数，或抽成类。",
            )
        if complexity >= _HIGH_COMPLEXITY:
            self._report_at(
                "MAINT002", lineno,
                f"函数 {name} 圈复杂度约 {complexity}（阈值 {_HIGH_COMPLEXITY}），分支太多难以测试。",
                "用早返回（guard clause）减少嵌套，或把分支抽成策略函数。",
            )
        if arg_count > _MAX_ARGUMENTS:
            self._report_at(
                "MAINT003", lineno,
                f"函数 {name} 有 {arg_count} 个参数，调用方容易传错。",
                "把相关参数聚合为 dataclass / 配置对象。",
            )
        # 只要求"模块级公开函数"写文档：类方法与内部小函数强制写文档噪声过大
        if is_module_level and not name.startswith("_") and not has_docstring and not self.is_test_file:
            self._report_at(
                "MAINT004", lineno,
                f"公开函数 {name} 缺少文档字符串。",
                "用一行说明「做什么、参数含义、返回什么」。",
            )

    def _report_at(self, rule_id: str, lineno: int, detail: str, suggestion: str) -> None:
        self.findings.append(
            _finding(rule_id, file=self.file, line=lineno, detail=detail, suggestion=suggestion)
        )

    def _check_unused_imports(self) -> None:
        declared = {name for name, _, _ in self.imports}
        self._collect_extra_references()
        for name, lineno, statement in self.imports:
            if name in _IMPORT_WHITELIST or name not in declared:
                continue
            if name not in self.used_names:
                self._report_at(
                    "MAINT006", lineno,
                    f"导入了 {name} 但未使用（{statement}）。",
                    "删除该导入，或确认是否漏用了本应调用的函数。",
                )

    def _collect_extra_references(self) -> None:
        """补扫两类容易被漏掉的名字引用：type(x) == T 与字符串里的名字。"""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Call):
                call = node.left
                if isinstance(call.func, ast.Name) and call.func.id == "type" and call.args:
                    if any(isinstance(operator, ast.Eq) for operator in node.ops):
                        self._report_at(
                            "BUG005", node.lineno,
                            "type(x) == T 不识别子类，也不是多态友好的判断方式。",
                            "改用 isinstance(x, T)。",
                        )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # 兼容 __all__ 与字符串注解里出现的名字
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                    self.used_names.add(token)

    def _check_module_docstring(self) -> None:
        # 测试文件与脚本类文件不强制模块文档（这是业界通行做法，避免噪声淹没真问题）
        if not ast.get_docstring(self.tree) and self.functions and not self.is_test_file:
            self.findings.append(
                _finding(
                    "MAINT004",
                    file=self.file,
                    line=1,
                    detail="模块缺少文档字符串，阅读者无法快速了解文件用途。",
                    suggestion="在文件开头用三引号写一句模块职责说明。",
                )
            )

    def _check_source_lines(self) -> None:
        """逐行检查：遗留标记、明文密钥，以及聚合后的风格问题。"""
        stats = {"long": [], "whitespace": [], "tabs": 0}
        for index, line in enumerate(self.lines, start=1):
            self._check_single_line(index, line, stats)
        self._report_style_aggregates(stats)

    def _check_single_line(self, index: int, line: str, stats: dict) -> None:
        if len(line) > _LONG_LINE_LIMIT and "http" not in line:
            stats["long"].append(index)
        if line.rstrip() != line:
            stats["whitespace"].append(index)
        if "\t" in line:
            stats["tabs"] += 1

        todo = _TODO_PATTERN.search(line)
        if todo:
            self._report_at(
                "MAINT005", index,
                f"遗留标记 {todo.group(1).upper()}：{todo.group(2).strip()[:120]}",
                "确认后修复或转成正式 issue 跟踪。",
            )
        secret = _SECRET_PATTERN.search(line)
        if secret and not _looks_like_placeholder(secret.group(2)):
            self.findings.append(
                _finding(
                    "SEC001",
                    file=self.file,
                    line=index,
                    detail=f"疑似把 {secret.group(1)} 明文写在代码里。",
                    suggestion="改为从环境变量/配置中心读取，并从 git 历史中清理。",
                    evidence=f"{secret.group(1)}=***",
                )
            )

    def _report_style_aggregates(self, stats: dict) -> None:
        """噪声较大的规则按文件聚合成一条，避免报告被上千条风格问题淹没。"""
        long_lines = stats["long"]
        whitespace_lines = stats["whitespace"]
        tabs = stats["tabs"]
        if long_lines:
            self._report_at(
                "STYLE001", long_lines[0],
                f"共 {len(long_lines)} 行超过 {_LONG_LINE_LIMIT} 字符，首行在第 {long_lines[0]} 行。",
                "用格式化工具（black/ruff format）统一处理。",
            )
        if whitespace_lines or tabs:
            self._report_at(
                "STYLE002", whitespace_lines[0] if whitespace_lines else 1,
                f"行尾空白 {len(whitespace_lines)} 处，含制表符的行 {tabs} 行。",
                "开启编辑器的 trim trailing whitespace，并统一使用 4 空格缩进。",
            )
        if len(self.lines) > _LONG_FILE_LINES:
            self._report_at(
                "STYLE003", 1,
                f"文件共 {len(self.lines)} 行（阈值 {_LONG_FILE_LINES}）。",
                "按职责拆分为多个模块。",
            )


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def analyze_source(source: str, file: str, *, max_findings: int = 40) -> tuple[list[Finding], FileAnalysis]:
    """分析一段 Python 源码，返回 (问题列表, 摘要)。

    语法错误不会抛异常，而是转成一条 SYN001 问题并返回——这是刻意的边界处理：
    待审查代码有语法错误是常态，工具必须能继续工作。
    """
    analysis = FileAnalysis(path=file, loc=len(source.splitlines()))
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError as exc:
        return _syntax_error_result(analysis, file, exc)
    except (ValueError, RecursionError, MemoryError) as exc:  # 极端输入兜底（如超深嵌套）
        return _parser_failure_result(analysis, file, exc)

    analyzer = Analyzer(source, file, tree, is_test_file=_is_test_file(file))
    findings = analyzer.run()
    _fill_analysis_summary(analysis, analyzer, tree)
    return _cap_findings(findings, file, max_findings), analysis


def _syntax_error_result(analysis: FileAnalysis, file: str, exc: SyntaxError) -> tuple[list[Finding], FileAnalysis]:
    """把语法错误转成一条可读的 SYN001 发现。"""
    line = exc.lineno or 1
    detail = f"第 {line} 行解析失败：{exc.msg}"
    if exc.text:
        detail += f"；原文：{exc.text.strip()[:120]}"
    analysis.parse_ok = False
    analysis.error = detail
    finding = _finding(
        "SYN001",
        file=file,
        line=line,
        detail=detail,
        suggestion="先修复语法错误，否则该文件无法参与后续任何静态分析。",
    )
    return [finding], analysis


def _parser_failure_result(analysis: FileAnalysis, file: str, exc: Exception) -> tuple[list[Finding], FileAnalysis]:
    """ast 本身处理不了的输入（超深嵌套等）也不应让整次审查失败。"""
    analysis.parse_ok = False
    analysis.error = f"{type(exc).__name__}: {exc}"
    finding = _finding(
        "SYN001",
        file=file,
        line=1,
        detail=f"解析器无法处理该文件：{analysis.error}",
        suggestion="检查是否存在超深嵌套或畸形源码。",
    )
    return [finding], analysis


def _fill_analysis_summary(analysis: FileAnalysis, analyzer: Analyzer, tree: ast.Module) -> None:
    analysis.functions = analyzer.functions
    analysis.classes = analyzer.classes
    analysis.has_module_docstring = bool(ast.get_docstring(tree))
    if analyzer.functions:
        complexities = [item["complexity"] for item in analyzer.functions]
        analysis.max_complexity = max(complexities)
        analysis.avg_complexity = sum(complexities) / len(complexities)


def _cap_findings(findings: list[Finding], file: str, max_findings: int) -> list[Finding]:
    """控制单文件问题数量：按严重程度优先保留（否则高危问题会被大量风格问题挤掉）。"""
    if len(findings) <= max_findings:
        return findings
    ordered = sorted(findings, key=lambda item: -item.severity_rank)
    hidden = ordered[max_findings:]
    detail = f"另有 {len(hidden)} 条低优先级问题未在报告正文展开（可用 scan 子命令查看完整明细）。"
    hidden_high = sum(1 for item in hidden if item.severity_rank >= 3)
    if hidden_high:
        detail += f" 其中包含 {hidden_high} 条高危问题，已优先保留高危项。"
    return ordered[:max_findings] + [
        Finding(
            title="该文件问题较多，已折叠",
            file=file,
            line=None,
            severity="info",
            category="other",
            detail=detail,
            suggestion="建议先修高危问题，再批量跑格式化工具处理风格问题。",
            rule_id="LIMIT001",
            source="rule",
        )
    ]
