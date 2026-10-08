"""共享状态（Blackboard）：节点间交换数据的唯一"公共黑板"。

一个 Workflow 里所有 Agent 都只通过 `Blackboard` 读写共享状态，从而避免
Agent 之间直接互相持有引用。黑板提供四类能力：

1. **命名空间键值**（`set/get/ns_*`）：`coder` 写 `code.files`、`reviewer` 写
   `review.issues`，互不干扰；`snapshot("review")` 可拿到某名前缀的整块视图。
2. **产物登记**（`put_artifact/artifact/artifacts`）：报告渲染只需要读元信息，
   不必再翻黑板任意键。
3. **工作区文件**（`write_file/read_file/file_hash/list_files`）：所有路径都经
   `util.safe_join` 校验，越界抛 `PathSecurityError`（防提示词注入读到工作区外）。
4. **运行状态**（`record/bump_iteration/summary` + `RunState`）：回合、循环次数、
   节点执行记录只增不减，支撑 `--resume` 与最终报告。

持久化（`save/load`）刻意只使用**纯 JSON**：dataclass 手工 `to_dict/from_dict`，
不做 pickle。理由是检查点要能跨进程、跨版本、可被人直接阅读与 diff。

线程安全：并行分支（`type=="parallel"`）会并发写黑板，因此键值、产物、记录
等所有写操作都在一把可重入锁内完成。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from .bus import MessageBus
from .errors import NodeError
from .util import (
    jsonable,
    new_id,
    now_iso,
    read_text,
    safe_join,
    safe_relpath,
    sha1_text,
    write_text,
)

logger = logging.getLogger("wfa.state")

#: `list_files()` 遍历工作区时跳过的目录（依赖缓存与版本库元数据，无业务价值）。
_IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        ".venv", "venv", "env", "node_modules", "dist", "build", "htmlcov",
    }
)


# --------------------------------------------------------------------------- #
# 运行记录
# --------------------------------------------------------------------------- #
@dataclass
class NodeRecord:
    """一次节点执行的记录（`RunState.records` 的元素）。

    Attributes:
        node_id: 节点 id
        agent: 执行该节点的 Agent 名
        turn: 该节点在流程的第几个回合执行
        status: ok | failed | skipped
        started_at: 开始时间（`now_iso()`）
        duration: 耗时（秒）
        summary: 一句话结果摘要
        kind: `AgentResult.kind`（result | review | fix | doc | requirement）
        decision: `AgentResult.decision`（continue | pass | retry | fail | done）
        result: `AgentResult.to_dict()` 的完整快照
        messages: 本节点产生/消费的消息 id
        error: 失败原因（status="failed" 时）
        attempt: 第几次尝试（重试后 > 1）
    """

    node_id: str = ""
    agent: str = ""
    turn: int = 0
    status: str = "ok"
    started_at: str = ""
    duration: float = 0.0
    summary: str = ""
    kind: str = ""
    decision: str = ""
    result: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    error: str = ""
    attempt: int = 1

    def to_dict(self) -> dict:
        """转为纯 JSON 字典（字段名与 dataclass 一致）。"""
        return {
            "node_id": self.node_id,
            "agent": self.agent,
            "turn": int(self.turn or 0),
            "status": self.status,
            "started_at": self.started_at,
            "duration": round(float(self.duration or 0.0), 6),
            "summary": self.summary,
            "kind": self.kind,
            "decision": self.decision,
            "result": jsonable(self.result),
            "messages": [str(item) for item in (self.messages or [])],
            "error": self.error,
            "attempt": int(self.attempt or 1),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NodeRecord":
        """从字典还原；未知字段忽略，缺失字段用默认值（容忍旧检查点）。"""
        data = dict(data or {})
        result = data.get("result")
        messages = data.get("messages")
        return cls(
            node_id=str(data.get("node_id") or ""),
            agent=str(data.get("agent") or ""),
            turn=_as_int(data.get("turn"), 0),
            status=str(data.get("status") or "ok"),
            started_at=str(data.get("started_at") or ""),
            duration=_as_float(data.get("duration"), 0.0),
            summary=str(data.get("summary") or ""),
            kind=str(data.get("kind") or ""),
            decision=str(data.get("decision") or ""),
            result=dict(result) if isinstance(result, dict) else {},
            messages=list(messages) if isinstance(messages, (list, tuple)) else [],
            error=str(data.get("error") or ""),
            attempt=_as_int(data.get("attempt"), 1) or 1,
        )


@dataclass
class RunState:
    """整次运行的元状态（终态摘要写入 `final`，供报告渲染）。

    Attributes:
        run_id: 运行 id（`run-xxxxxxxx`）
        workflow: 流程名
        status: pending | running | success | partial | failed | aborted
        turn: 已执行的回合数
        iterations: 各循环（`loop_of`）已执行次数
        records: 节点执行记录（**只增不减**）
        final: 终态摘要（报告用）
        error: 致命错误信息
    """

    run_id: str = ""
    workflow: str = ""
    status: str = "pending"
    turn: int = 0
    iterations: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0
    records: list = field(default_factory=list)
    final: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        """转为纯 JSON 字典（records 逐个走 `NodeRecord.to_dict()`）。"""
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "status": self.status,
            "turn": int(self.turn or 0),
            "iterations": {str(key): _as_int(value, 0) for key, value in (self.iterations or {}).items()},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(float(self.duration or 0.0), 6),
            "records": [record.to_dict() for record in (self.records or [])],
            "final": jsonable(self.final),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunState":
        """从字典还原（records 逐个走 `NodeRecord.from_dict()`）。"""
        data = dict(data or {})
        raw_records = data.get("records")
        records = [
            item if isinstance(item, NodeRecord) else NodeRecord.from_dict(item)
            for item in (raw_records if isinstance(raw_records, (list, tuple)) else [])
            if isinstance(item, (dict, NodeRecord))
        ]
        iterations = data.get("iterations")
        final = data.get("final")
        return cls(
            run_id=str(data.get("run_id") or ""),
            workflow=str(data.get("workflow") or ""),
            status=str(data.get("status") or "pending"),
            turn=_as_int(data.get("turn"), 0),
            iterations={str(key): _as_int(value, 0) for key, value in (iterations or {}).items()}
            if isinstance(iterations, dict)
            else {},
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            duration=_as_float(data.get("duration"), 0.0),
            records=records,
            final=dict(final) if isinstance(final, dict) else {},
            error=str(data.get("error") or ""),
        )


def _as_int(value: Any, default: int = 0) -> int:
    """宽松整数转换（JSON 里可能是字符串或 None）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽松浮点转换。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 黑板
# --------------------------------------------------------------------------- #
class Blackboard:
    """节点间共享状态：命名空间键值 + 产物登记 + 工作区文件 + 运行状态。

    Args:
        workspace: 工作区根目录（所有相对路径都以此为边界）；留空则取当前目录。
        bus: 复用的消息总线；不传则新建一个。
        state: 复用的 `RunState`（`--resume` 时由调用方注入）；不传则新建。
    """

    def __init__(
        self,
        *,
        workspace: str = "",
        bus: MessageBus | None = None,
        state: RunState | None = None,
    ) -> None:
        self._workspace = os.path.abspath(workspace) if workspace else os.getcwd()
        self._bus = bus if bus is not None else MessageBus()
        #: 运行状态（裸属性，engine 需要直接改 `state.state.final`）。
        self.state = state if state is not None else RunState(run_id=new_id("run"), workflow="")
        self._kv: dict = {}
        self._artifacts: dict = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 基础属性
    # ------------------------------------------------------------------ #
    @property
    def bus(self) -> MessageBus:
        """本黑板绑定的消息总线。"""
        return self._bus

    @property
    def workspace(self) -> str:
        """工作区绝对路径（文件读写的边界）。"""
        return self._workspace

    @property
    def run_id(self) -> str:
        """本次运行的 id（等价于 `state.run_id`）。"""
        return self.state.run_id

    # ------------------------------------------------------------------ #
    # 键值
    # ------------------------------------------------------------------ #
    def set(self, key: str, value: Any) -> None:
        """写入一个键（覆盖旧值）。"""
        with self._lock:
            self._kv[str(key)] = value

    def get(self, key: str, default: Any = None) -> Any:
        """读取一个键，缺失返回 `default`。"""
        with self._lock:
            return self._kv.get(str(key), default)

    def require(self, key: str) -> Any:
        """读取一个**必需**键；缺失抛 `NodeError`（错误信息带上已有键便于排查）。"""
        with self._lock:
            if str(key) in self._kv:
                return self._kv[str(key)]
            known = ", ".join(sorted(self._kv)[:12]) or "（空）"
        raise NodeError(f"黑板缺少必需键：{key}；当前已有键：{known}")

    def update(self, mapping: dict) -> None:
        """批量写入（等价于逐个 `set`）。"""
        for key, value in dict(mapping or {}).items():
            self.set(key, value)

    def keys(self) -> list[str]:
        """全部键（排序）。"""
        with self._lock:
            return sorted(self._kv)

    def snapshot(self, prefix: str = "") -> dict:
        """前缀视图：返回以 `prefix` 开头的所有键值（`prefix=""` 表示全部）。"""
        prefix = str(prefix or "")
        with self._lock:
            return {
                key: value
                for key, value in self._kv.items()
                if not prefix or key.startswith(prefix)
            }

    def ns_set(self, ns: str, key: str, value: Any) -> None:
        """命名空间写入：等价 `set(f"{ns}.{key}")`。"""
        self.set(f"{ns}.{key}", value)

    def ns_get(self, ns: str, key: str, default: Any = None) -> Any:
        """命名空间读取：等价 `get(f"{ns}.{key}", default)`。"""
        return self.get(f"{ns}.{key}", default)

    def ns_all(self, ns: str) -> dict:
        """取整个命名空间（去掉前缀后的键值对）；`ns=""` 时返回全部键值。"""
        ns = str(ns or "")
        if not ns:
            return self.snapshot("")
        prefix = f"{ns}."
        with self._lock:
            return {
                key[len(prefix):]: value
                for key, value in self._kv.items()
                if key.startswith(prefix)
            }

    def ns_merge(self, ns: str, mapping: dict) -> None:
        """把一个字典合并进命名空间（逐键 `ns_set`）。"""
        for key, value in dict(mapping or {}).items():
            self.ns_set(ns, key, value)

    # ------------------------------------------------------------------ #
    # 产物
    # ------------------------------------------------------------------ #
    def put_artifact(
        self, name: str, content: Any, *, kind: str = "doc", producer: str = ""
    ) -> dict:
        """登记一份产物，返回产物元数据字典（含内容，便于链路透传）。

        结构：`{"name","kind","producer","created_at","content"}`；同名产物后写覆盖先写。
        """
        record = {
            "name": str(name),
            "kind": str(kind or "doc"),
            "producer": str(producer or ""),
            "created_at": now_iso(),
            "content": content,
        }
        with self._lock:
            self._artifacts[str(name)] = record
        return record

    def artifact(self, name: str) -> dict | None:
        """按名取产物元数据，缺失返回 None。"""
        with self._lock:
            record = self._artifacts.get(str(name))
            return dict(record) if record is not None else None

    def artifacts(self) -> dict:
        """全部产物：`{name: 元数据字典}`（浅拷贝，避免外部误改内部状态）。"""
        with self._lock:
            return {name: dict(record) for name, record in self._artifacts.items()}

    # ------------------------------------------------------------------ #
    # 工作区文件
    # ------------------------------------------------------------------ #
    def write_file(self, rel: str, content: str) -> str:
        """把内容写入工作区内的相对路径，返回绝对路径；越界抛 `PathSecurityError`。"""
        target = safe_join(self._workspace, rel)
        write_text(target, content)
        logger.debug("写入工作区文件：%s", safe_relpath(self._workspace, target))
        return target

    def read_file(self, rel: str, default: str | None = None) -> str | None:
        """读取工作区内的文本文件；不存在返回 `default`（越界仍然抛异常）。"""
        target = safe_join(self._workspace, rel)
        if not os.path.isfile(target):
            return default
        return read_text(target)

    def file_hash(self, rel: str) -> str:
        """文件内容指纹（10 位）；文件不存在返回空串。"""
        target = safe_join(self._workspace, rel)
        if not os.path.isfile(target):
            return ""
        return sha1_text(read_text(target))

    def file_exists(self, rel: str) -> bool:
        """路径是否存在且为文件（越界抛 `PathSecurityError`）。"""
        return os.path.isfile(safe_join(self._workspace, rel))

    def list_files(self, *, pattern: str = "*") -> list[str]:
        """列出工作区内文件，返回**排序后的正斜杠相对路径**。

        `pattern` 同时匹配相对路径与文件名（`fnmatch` 语法），默认全部。
        """
        root = self._workspace
        if not os.path.isdir(root):
            return []
        results: list[str] = []
        for current, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(
                name
                for name in dir_names
                if name not in _IGNORED_DIRS and not name.startswith(".wfa")
            )
            for name in sorted(file_names):
                rel = safe_relpath(root, os.path.join(current, name))
                if pattern in ("", "*") or fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    results.append(rel)
        return sorted(results)

    # ------------------------------------------------------------------ #
    # 运行状态
    # ------------------------------------------------------------------ #
    def record(self, record: NodeRecord) -> None:
        """追加一条节点执行记录，并把 `state.turn` 提升为记录中的最大回合。"""
        with self._lock:
            self.state.records.append(record)
            turns = _as_int(record.turn, 0)
            if turns > _as_int(self.state.turn, 0):
                self.state.turn = turns

    def bump_iteration(self, loop_id: str) -> int:
        """把某个循环的计数 +1 并返回新值（engine 用它判断 max_iterations）。"""
        with self._lock:
            current = _as_int(self.state.iterations.get(str(loop_id), 0), 0) + 1
            self.state.iterations[str(loop_id)] = current
            return current

    def iteration(self, loop_id: str) -> int:
        """读取某个循环已执行的次数（不存在返回 0）。"""
        with self._lock:
            return _as_int(self.state.iterations.get(str(loop_id), 0), 0)

    def summary(self) -> dict:
        """指标卡：轮次、节点数、消息数、产物数、耗时、状态等（报告与日志共用）。"""
        with self._lock:
            records = list(self.state.records)
            counts = {"ok": 0, "failed": 0, "skipped": 0}
            for record in records:
                status = record.status or "ok"
                counts[status] = counts.get(status, 0) + 1
            return {
                "run_id": self.state.run_id,
                "workflow": self.state.workflow,
                "status": self.state.status,
                "turn": _as_int(self.state.turn, 0),
                "turns": _as_int(self.state.turn, 0),
                "nodes": len(records),
                "records": len(records),
                "ok": counts.get("ok", 0),
                "failed": counts.get("failed", 0),
                "skipped": counts.get("skipped", 0),
                "messages": len(self._bus.messages),
                "artifacts": len(self._artifacts),
                "keys": len(self._kv),
                "iterations": dict(self.state.iterations),
                "started_at": self.state.started_at,
                "finished_at": self.state.finished_at,
                "duration": round(_as_float(self.state.duration, 0.0), 4),
            }

    # ------------------------------------------------------------------ #
    # 持久化（纯 JSON，供 checkpoint / resume）
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        """导出 `{"kv", "artifacts", "state", "metrics"}`，保证 `json.dump` 可用。"""
        with self._lock:
            return {
                "kv": jsonable(self._kv),
                "artifacts": {
                    name: jsonable(record) for name, record in self._artifacts.items()
                },
                "state": self.state.to_dict(),
                "metrics": jsonable(self.summary()),
            }

    def save(self, path: str) -> str:
        """把黑板写成 UTF-8 JSON 文件（自动建父目录），返回写入路径。"""
        target = os.path.abspath(path)
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = self.to_dict()
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        logger.info("黑板已保存：%s（键 %d / 产物 %d）", target, len(payload["kv"]), len(payload["artifacts"]))
        return target

    @classmethod
    def load(
        cls, path: str, *, workspace: str = "", bus: MessageBus | None = None
    ) -> "Blackboard":
        """从 `save()` 写出的 JSON 还原黑板（消息历史由调用方注入 bus 恢复）。

        文件缺失或格式非法时抛 `NodeError`（属于 `WorkflowError`，CLI 可统一处理）。
        """
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise NodeError(f"读取检查点失败：{path}（{exc}）") from exc
        except json.JSONDecodeError as exc:
            raise NodeError(f"检查点不是合法 JSON：{path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise NodeError(f"检查点格式非法（应为 JSON 对象）：{path}")

        kv = data.get("kv")
        artifacts = data.get("artifacts")
        board = cls(
            workspace=workspace,
            bus=bus,
            state=RunState.from_dict(data.get("state") or {}),
        )
        board._kv = dict(kv) if isinstance(kv, dict) else {}
        board._artifacts = (
            {str(name): dict(record) for name, record in artifacts.items() if isinstance(record, dict)}
            if isinstance(artifacts, dict)
            else {}
        )
        return board

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Blackboard run_id={self.state.run_id!r} keys={len(self._kv)} "
            f"artifacts={len(self._artifacts)} records={len(self.state.records)}>"
        )


__all__ = ["NodeRecord", "RunState", "Blackboard"]
