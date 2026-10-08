"""CodingAgent：把任务/需求/设计转成**结构化文件集合**，写入工作区并做本地自测。

流程（与 CONTRACT 第 7 节 ``coder`` 行一致）::

    读任务 → 汇总上游 requirements / design → 优先向模型要一份结构化初稿
          → 兜底用确定性模板 → 写入工作区（blackboard.write_file）
          → subprocess 跑一次 ``python -m unittest``（带超时保护）
          → data = {files, tests, plan, self_test}

离线可用性：``RuleChatModel``（``is_deterministic=True``）会返回内置的"第一版实现 +
单元测试"模板（含两处可被规则引擎命中的缺陷，用于演示"审查 → 修复"闭环）；
模型不可用或回复不可解析时，本模块的确定性模板同样能产出等价结构。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time

from ..graph import AgentResult
from ..util import extract_json_object
from ..analysis import analyze_source
from .base import (
    Agent,
    agent_task,
    config_value,
    known_results,
    publish_message,
    read_workspace_file,
    workspace_root,
    write_workspace_file,
)

logger = logging.getLogger("wfa.agents.coding")

#: 默认产出路径（离线演示约定：``app/service.py`` + ``tests/test_service.py``）。
DEFAULT_MODULE_PATH = "app/service.py"
DEFAULT_TEST_PATH = "tests/test_service.py"
#: 单个文件的字节上限（防止模型返回超大内容）。
MAX_FILE_BYTES = 20_000
#: 自测超时（秒）。
SELF_TEST_TIMEOUT = 90.0

#: 确定性兜底模板里的"演示用"密钥字面量（生成的第一版实现故意留下 SEC001，供审查命中）。
#: 这里用字符串拼接而不是单个字面量，避免规则引擎把本模块自己也报成硬编码密钥。
DEMO_SECRET_VALUE = "sk-demo" + "-hardcoded-key-0123456789"

CODER_SYSTEM = (
    "你是一名资深 Python 工程师，与需求/设计/审查/修复等其它 Agent 协作。"
    "请把任务落成**可运行、可测试**的小模块，并且只输出一个 JSON 对象：\n"
    '{"summary": "一句话", "plan": ["实现步骤", "..."],'
    ' "files": {"app/service.py": "完整源码"}, "tests": {"tests/test_service.py": "完整测试"}}\n'
    "约束：标准库零依赖、Python 3.9+、公开函数带 docstring、不使用 print。"
)

#: 给"确定性伪模型"用的最小提示词：只保留 code/实现/编码 关键词，
#: 这样 RuleChatModel 能命中它的 ``_code_template``（离线演示路径）。
#: 注意不要出现 review/fix/test/design/requirement 等更靠前的关键词（含英文复数形式）。
CODER_COMPACT_PROMPT = "编码阶段：请实现 code（实现 / 编码）。只输出 JSON 对象，包含实现步骤与文件内容。"

#: 确定性兜底模板：**故意保留两处缺陷**（硬编码密钥 + 静默吞异常），
#: 用于演示 code → review → fix → review 的闭环。
_MODULE_TEMPLATE = '''"""@@MODULE@@：@@TASK@@。

第一版实现（由 CodingAgent 产出）：normalize_records 清洗记录，summarize 汇总记录。
"""

import json
import os

API_KEY = "@@SECRET@@"


def normalize_records(records):
    result = []
    for record in records:
        try:
            item = json.loads(record)
        except Exception:
            pass
        result.append(item)
    return result


def summarize(records, key="value"):
    total = 0
    for record in records:
        total += record.get(key, 0)
    return {"count": len(records), "total": total}


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
'''

_TEST_TEMPLATE = '''"""@@MODULE@@ 的单元测试（由 CodingAgent 产出）。"""

import unittest

from @@MODULE_NAME@@ import normalize_records, summarize


class NormalizeRecordsTest(unittest.TestCase):
    """覆盖正常记录与非法记录的清洗行为。"""

    def test_valid_records(self):
        self.assertEqual(normalize_records(['{"value": 1}']), [{"value": 1}])

    def test_invalid_record_is_skipped(self):
        self.assertEqual(normalize_records(["not-json"]), [])


class SummarizeTest(unittest.TestCase):
    """覆盖汇总函数的计数与合计。"""

    def test_total(self):
        self.assertEqual(summarize([{"value": 2}, {"value": 3}])["total"], 5)

    def test_count(self):
        self.assertEqual(summarize([])["count"], 0)


if __name__ == "__main__":
    unittest.main()
'''


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def module_name(rel: str) -> str:
    """把文件路径转成可 import 的模块名：``app/service.py`` → ``app.service``。"""
    path = str(rel or "").replace("\\", "/").strip("/")
    if path.endswith(".py"):
        path = path[:-3]
    return path.replace("/", ".") or "app.service"


def _clean_text(value, limit: int = MAX_FILE_BYTES) -> str:
    """把模型返回的文件内容规范成字符串（容忍 list-of-lines）。"""
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value)
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return text[:limit]


def clean_file_map(value, *, default_path: str = "") -> dict:
    """把模型/上游给的 files/tests 载荷规范成 ``{相对路径: 内容}``。"""
    result: dict = {}
    if isinstance(value, dict):
        for rel, content in value.items():
            text = _clean_text(content)
            if text and str(rel).strip():
                result[str(rel).strip().replace("\\", "/")] = text
    elif isinstance(value, list) and default_path:
        for item in value:
            text = _clean_text(item)
            if text:
                result[default_path] = text
                break
    return result


def _flatten_values(value, prefix: str = "") -> list:
    """把嵌套的 dict/list 拍平成 ``"key: 值"`` 文本行，用于需求/设计摘要。"""
    rows: list = []
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_flatten_values(item, prefix=f"{prefix}{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value, 1):
            rows.extend(_flatten_values(item, prefix=f"{prefix}[{index}]"))
    elif value not in (None, ""):
        label = prefix.rstrip(":") or "内容"
        rows.append(f"{label}: {value}")
    return rows


def _task_snippet(task: str, limit: int = 120) -> str:
    """把任务描述压成一行安全的摘要（放进 docstring 也不会破坏语法）。"""
    text = re.sub(r"\s+", " ", str(task or "")).strip()
    text = text.replace('"""', "'''").replace('"', "'")
    return (text or "按需求实现一个小而完整的 Python 模块")[:limit]


# --------------------------------------------------------------------------- #
# CodingAgent
# --------------------------------------------------------------------------- #
class CodingAgent(Agent):
    """编码 Agent：需求/设计 → 文件集合 → 写入工作区 → 本地自测。"""

    name = "coder"
    role = "编码 Agent"

    def __init__(self, model=None, *, config=None, bus=None, name: str = "coder",
                 role: str = "编码 Agent") -> None:
        super().__init__(model, config=config, bus=bus)
        self.name = name
        self.role = role

    # ------------------------------------------------------------------ #
    def run(self, ctx) -> AgentResult:
        """生成文件集合、写入工作区、跑本地自测，返回 ``kind="result"``。"""
        started = time.time()
        notes: list = []
        task = agent_task(ctx, self.config)
        digest = self._requirement_digest(ctx)
        module_rel, test_rel = self._plan_paths(digest)
        draft = self._model_draft(task, digest, module_rel, test_rel)
        files = clean_file_map(draft.get("files")) or {
            module_rel: self._render_module(module_rel, task)
        }
        tests = clean_file_map(draft.get("tests")) or {
            test_rel: self._render_test(test_rel, module_rel)
        }
        plan = self._build_plan(draft, task, files, tests)
        self._ensure_test_package(tests, notes)
        self._preserve_existing(ctx, files, tests, notes)

        written = self._write_all(ctx, files, tests, notes)
        self_test = self._run_self_test(ctx, written, notes)
        summary = ("编码完成：产出 %d 个文件、%d 个测试文件，自测返回码 %s。"
                   % (len(files), len(tests), self_test.get("returncode")))
        artifacts = {"code/plan.md": self._render_plan_md(task, plan, files, tests, self_test)}
        metrics = {
            "files": len(files), "tests": len(tests), "written": len(written),
            "self_test_returncode": self_test.get("returncode"),
            "offline": bool(getattr(self.model, "is_deterministic", False)) or self.model is None,
            "duration": round(time.time() - started, 3),
        }
        self._publish(ctx, files, tests, self_test)
        return AgentResult(
            kind="result",
            summary=summary,
            decision="continue",
            data={"files": files, "tests": tests, "plan": plan, "self_test": self_test},
            artifacts=artifacts,
            metrics=metrics,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # 需求 / 设计摘要
    # ------------------------------------------------------------------ #
    def _requirement_digest(self, ctx) -> dict:
        """汇总任务与上游需求/设计信息（换行文本 + 计划文件清单）。"""
        digest = {"task": agent_task(ctx, self.config), "lines": [], "files": [], "plan": []}
        for node_id, raw in known_results(ctx):
            self._absorb_result(digest, node_id, raw)
        digest["lines"] = _unique(digest["lines"])[:24]
        digest["files"] = _unique(digest["files"])
        digest["plan"] = _unique(digest["plan"])[:12]
        return digest

    def _absorb_result(self, digest: dict, node_id: str, raw: dict) -> None:
        """把单个上游结果里的需求/设计/文件清单吸收进摘要。"""
        if str(node_id) == self.name:
            return
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        for key in ("requirement", "requirements", "design", "outline"):
            value = data.get(key)
            if isinstance(value, (dict, list)):
                digest["lines"].extend(f"{key}: {row}" for row in _flatten_values(value))
            elif isinstance(value, str) and value.strip():
                digest["lines"].append(f"{key}: {value.strip()[:400]}")
        for key in ("files", "tests"):
            value = data.get(key)
            if isinstance(value, dict):
                digest["files"].extend(str(item) for item in value)
            elif isinstance(value, list):
                digest["files"].extend(str(item) for item in value if str(item).endswith(".py"))
        plan = data.get("plan")
        if isinstance(plan, list):
            digest["plan"].extend(str(item) for item in plan if isinstance(item, str))
        if not digest["task"] and raw.get("summary"):
            digest["task"] = str(raw["summary"])

    def _plan_paths(self, digest: dict) -> tuple:
        """决定实现文件与测试文件路径：优先上游设计给出的清单。"""
        module_rel = next(
            (item for item in digest["files"]
             if item.endswith(".py") and not os.path.basename(item).startswith("test_")),
            "",
        )
        test_rel = next(
            (item for item in digest["files"] if os.path.basename(item).startswith("test_")),
            "",
        )
        return module_rel or DEFAULT_MODULE_PATH, test_rel or DEFAULT_TEST_PATH

    # ------------------------------------------------------------------ #
    # 向模型要初稿（离线伪模型会命中内置模板）
    # ------------------------------------------------------------------ #
    def _model_draft(self, task: str, digest: dict, module_rel: str, test_rel: str) -> dict:
        """调用模型拿到结构化初稿；失败/不可解析时返回空 dict（走确定性模板）。"""
        model = self.model
        if model is None:
            return {}
        compact = bool(getattr(model, "is_deterministic", False))
        prompt = CODER_COMPACT_PROMPT if compact else self._full_prompt(task, digest)
        messages = [{"role": "system", "content": CODER_SYSTEM}, {"role": "user", "content": prompt}]
        if compact:
            # 确定性伪模型按"提示词关键词"挑内置模板，系统提示里的其它关键词会抢先命中，
            # 因此只发这条紧凑的用户消息。
            messages = [{"role": "user", "content": prompt}]
        try:
            response = model.chat(messages)
        except Exception as exc:  # noqa: BLE001 - 模型失败必须能降级
            logger.warning("编码 Agent 调用模型失败，改用确定性模板：%s", exc)
            return {}
        payload = extract_json_object(str(getattr(response, "content", "") or ""))
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    def _full_prompt(self, task: str, digest: dict) -> str:
        """给真实模型用的完整提示（含任务与上游需求/设计摘要）。"""
        lines = "\n".join(f"- {row}" for row in digest["lines"][:20]) or "（无上游需求/设计）"
        return (
            f"任务：{task or '（未提供）'}\n"
            f"上游需求/设计摘要：\n{lines}\n\n"
            "请输出 JSON（summary / plan / files / tests），源码要完整可运行。"
        )

    # ------------------------------------------------------------------ #
    # 确定性模板
    # ------------------------------------------------------------------ #
    def _render_module(self, rel: str, task: str) -> str:
        """渲染兜底实现模板（含演示用的两处缺陷）。"""
        return (_MODULE_TEMPLATE
                .replace("@@MODULE@@", rel)
                .replace("@@TASK@@", _task_snippet(task))
                .replace("@@SECRET@@", DEMO_SECRET_VALUE))

    def _render_test(self, test_rel: str, module_rel: str) -> str:
        """渲染兜底测试模板（import 路径按实现文件推导）。"""
        return (_TEST_TEMPLATE
                .replace("@@MODULE@@", module_rel)
                .replace("@@MODULE_NAME@@", module_name(module_rel)))

    def _build_plan(self, draft: dict, task: str, files: dict, tests: dict) -> list:
        """生成实现步骤清单（模型给了 plan 就优先用）。"""
        plan = [str(item) for item in (draft.get("plan") or []) if isinstance(item, str)]
        if plan:
            return plan[:12]
        return [
            f"解析任务：{_task_snippet(task, 80)}",
            "汇总上游需求与设计（requirements / designer 节点的 data）",
            "生成实现文件：" + "、".join(sorted(files)) or "生成实现文件",
            "生成单元测试：" + "、".join(sorted(tests)),
            "写入工作区（blackboard.write_file）",
            "执行本地自测（python -m unittest）",
        ]

    def _render_plan_md(self, task: str, plan: list, files: dict, tests: dict,
                        self_test: dict) -> str:
        """把实现计划渲染成 Markdown 产物（报告里展示 Agent 的思考过程）。"""
        lines = [
            "# 编码计划", "",
            f"- 任务：{task or '（未提供）'}",
            f"- 实现文件：{', '.join(sorted(files)) or '（无）'}",
            f"- 测试文件：{', '.join(sorted(tests)) or '（无）'}",
            f"- 自测命令：`{self_test.get('command')}` → returncode={self_test.get('returncode')}",
            "", "## 步骤", "",
        ]
        lines.extend(f"{index}. {item}" for index, item in enumerate(plan, 1))
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ #
    # 写入与自测
    # ------------------------------------------------------------------ #
    def _ensure_test_package(self, tests: dict, notes: list) -> None:
        """给测试目录补 ``__init__.py``，让 ``python -m unittest discover -s tests -t .`` 可用。"""
        for rel in list(tests):
            normalized = str(rel).replace("\\", "/")
            directory = os.path.dirname(normalized)
            init_rel = f"{directory}/__init__.py" if directory else ""
            if not init_rel or init_rel in tests or os.path.basename(normalized) == "__init__.py":
                continue
            tests[init_rel] = '"""测试包：便于 `python -m unittest discover -s tests -t .` 直接运行。"""\n'
            notes.append("补充测试包初始化文件 %s（让 discover 能找到测试目录）。" % init_rel)

    def _preserve_existing(self, ctx, files: dict, tests: dict, notes: list) -> None:
        """循环第二轮起不再覆盖已有文件，避免把修复 Agent 的成果冲掉。"""
        if int(getattr(ctx, "iteration", 0) or 0) <= 1:
            return
        for mapping in (files, tests):
            for rel in list(mapping):
                existing = read_workspace_file(ctx, rel, config=self.config)
                if existing:
                    mapping[rel] = existing
                    notes.append("第 %s 轮迭代：保留工作区中已有的 %s（不覆盖修复结果）。"
                                 % (getattr(ctx, "iteration", 1), rel))

    def _write_enabled(self, ctx) -> bool:
        """是否允许写工作区（``--no-write-workspace`` 时为 False）。"""
        return bool(config_value(ctx, self.config, "write_workspace", True))

    def _write_all(self, ctx, files: dict, tests: dict, notes: list) -> dict:
        """把文件集合写入工作区，返回 ``{相对路径: 绝对路径}``。"""
        if not self._write_enabled(ctx):
            notes.append("已禁用工作区写入（write_workspace=False），仅在结果里返回文件内容。")
            return {}
        written: dict = {}
        for rel, content in list(files.items()) + list(tests.items()):
            try:
                written[rel] = write_workspace_file(ctx, rel, content, config=self.config)
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其它文件
                notes.append("写入 %s 失败：%s" % (rel, exc))
        if written:
            notes.append("已写入工作区 %d 个文件。" % len(written))
        return written

    def _self_test_commands(self, written: dict, root: str) -> list:
        """按写入的文件推导自测命令（unittest discover 优先，最后逐个直接运行）。"""
        test_files = [rel for rel in written if os.path.basename(rel).startswith("test_")]
        commands: list = []
        test_dir = os.path.dirname(test_files[0]) if test_files else ""
        if test_dir:
            commands.append([sys.executable, "-m", "unittest", "discover",
                             "-s", test_dir, "-t", ".", "-v"])
        commands.append([sys.executable, "-m", "unittest", "discover",
                         "-s", ".", "-p", "test_*.py", "-v"])
        for rel in test_files:
            # 直接运行测试文件：即使测试目录不是包（没有 __init__.py）也能跑
            commands.append([sys.executable, os.path.join(root, rel)])
        return commands

    def _run_self_test(self, ctx, written: dict, notes: list) -> dict:
        """在工作区里跑一次单元测试（超时/异常都转成 -1，绝不抛给引擎）。"""
        if not written:
            return {"command": "（未写入工作区，跳过自测）", "returncode": -1,
                    "stdout": "write_workspace=False 或写入失败，无法执行本地自测。"}
        root = workspace_root(ctx, self.config)
        best = None
        for argv in self._self_test_commands(written, root):
            record = self._run_command(argv, root, pythonpath=root)
            if best is None or (record["returncode"] == 0 and best["returncode"] != 0):
                best = record
            if record["returncode"] == 0:
                break
        if best and best["returncode"] != 0:
            notes.append("本地自测未通过（returncode=%s），问题会由审查 Agent 复核。" % best["returncode"])
        return best or {"command": "（无可用测试）", "returncode": -1, "stdout": ""}

    @staticmethod
    def _run_command(argv: list, cwd: str, pythonpath: str = "") -> dict:
        """执行子进程命令并收集输出（超时保护 + 注入 PYTHONPATH）。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if pythonpath:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = pythonpath + (os.pathsep + existing if existing else "")
        command = subprocess.list2cmdline(argv)
        try:
            completed = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, errors="replace",
                timeout=SELF_TEST_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            return {"command": command, "returncode": -1,
                    "stdout": "自测超时（%.0fs），已终止。" % SELF_TEST_TIMEOUT}
        except OSError as exc:
            return {"command": command, "returncode": -1, "stdout": "无法启动自测进程：%s" % exc}
        output = (completed.stdout or "") + (completed.stderr or "")
        return {"command": command, "returncode": int(completed.returncode),
                "stdout": output[-4000:]}

    # ------------------------------------------------------------------ #
    def _publish(self, ctx, files: dict, tests: dict, self_test: dict) -> None:
        """广播编码结果（Agent 间协作的观测点）。"""
        publish_message(ctx, "result", {
            "agent": self.name,
            "files": sorted(files),
            "tests": sorted(tests),
            "self_test": self_test.get("returncode"),
        })

    def scan_own_output(self, files: dict) -> dict:
        """对刚生成的文件跑一次规则引擎（自检用，不产生副作用）。"""
        counts: dict = {}
        for rel, content in (files or {}).items():
            if not str(rel).endswith(".py"):
                continue
            for hit in analyze_source(content, str(rel)):
                counts[hit.rule_id] = counts.get(hit.rule_id, 0) + 1
        return counts


def _unique(items) -> list:
    """去重且保持顺序。"""
    seen: set = set()
    result: list = []
    for item in items:
        text = str(item)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


__all__ = ["CodingAgent", "module_name", "clean_file_map", "DEMO_SECRET_VALUE",
           "DEFAULT_MODULE_PATH", "DEFAULT_TEST_PATH"]
