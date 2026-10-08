"""节点、边与流程定义（CONTRACT 第 5 节）。

流程层只描述"图"本身，不关心"怎么跑"：

* ``AgentResult`` / ``NodeOutcome``：节点执行的结果与状态；
* ``Node`` / ``Edge`` / ``Workflow``：节点、边与流程（含 Mermaid / DOT 导出）；
* ``WorkflowBuilder``：流式声明 API（文档与测试都用它）；
* ``NodeContext``：节点执行上下文（由 engine 构造；对 Blackboard / MessageBus 做鸭子类型引用）。

依赖方向：本模块只 import ``errors``；对 ``bus`` / ``state`` 仅在类型标注（``TYPE_CHECKING``）中引用，
因此可以独立 import，绝不会造成循环依赖（不 import ``agents`` / ``cli`` / ``engine``）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .errors import NodeError

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .bus import MessageBus
    from .state import Blackboard

logger = logging.getLogger("wfa.graph")

#: 合法节点类型。
NODE_TYPES: Tuple[str, ...] = ("agent", "gate", "route", "parallel", "start", "end")

#: 合法决策值（``AgentResult.decision``）。
DECISIONS: Tuple[str, ...] = ("", "continue", "pass", "retry", "fail", "done")

#: ``on_error`` 的合法取值（``fallback:<node_id>`` 为前缀形式）。
ON_ERROR_VALUES: Tuple[str, ...] = ("fail", "continue")

_MISSING = object()


def jsonable(value: Any) -> Any:
    """把任意对象转成可 JSON 序列化的结构（不可序列化时退化为 ``str``）。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return str(value)


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class AgentResult:
    """一次节点执行的业务结果（所有 Agent 的统一返回类型）。

    ``data`` 必须遵守 CONTRACT 第 5 节的键名约定（见模块文档与 docs/CONTRACT.md）。
    """

    kind: str = "result"                  # result | review | fix | doc | requirement | error
    summary: str = ""                     # 一句话摘要（给人看）
    decision: str = ""                    # "" | continue | pass | retry | fail | done
    data: Dict[str, Any] = field(default_factory=dict)      # 结构化载荷
    artifacts: Dict[str, str] = field(default_factory=dict)  # 产物名 -> 内容
    metrics: Dict[str, Any] = field(default_factory=dict)   # 任意指标
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """序列化为纯 JSON 结构。"""
        return {
            "kind": self.kind,
            "summary": self.summary,
            "decision": self.decision,
            "data": jsonable(self.data),
            "artifacts": jsonable(self.artifacts),
            "metrics": jsonable(self.metrics),
            "notes": list(self.notes or []),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AgentResult":
        """从 ``to_dict()`` 的结构还原（容忍缺字段与多余字段）。"""
        raw = dict(data or {})
        return cls(
            kind=str(raw.get("kind") or "result"),
            summary=str(raw.get("summary") or ""),
            decision=str(raw.get("decision") or ""),
            data=dict(raw.get("data") or {}),
            artifacts=dict(raw.get("artifacts") or {}),
            metrics=dict(raw.get("metrics") or {}),
            notes=list(raw.get("notes") or []),
        )

    @property
    def ok(self) -> bool:
        """便捷判断：决策不是 ``fail`` 即视为"可继续"。"""
        return (self.decision or "").strip().lower() != "fail"


@dataclass
class NodeOutcome:
    """节点执行的"流程侧"结果：状态、错误、额外回写与强制跳转。

    ``execute`` 既可以返回 ``AgentResult``，也可以返回本类型：
    ``next_node`` 优先于出边选择规则；``payload`` 会被回写到黑板顶层键。
    """

    next_node: Optional[str] = None        # None 表示用 graph 默认出边
    status: str = "ok"                     # ok | failed | skipped
    error: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)   # 额外回写黑板的键值

    def to_dict(self) -> dict:
        return {
            "next_node": self.next_node,
            "status": self.status,
            "error": self.error,
            "payload": jsonable(self.payload),
        }

    def __bool__(self) -> bool:
        """``status == "ok"`` 时为真（便于 ``if outcome:`` 书写）。"""
        return self.status == "ok"


#: 统一的 ``execute`` 签名：``execute(node, ctx) -> AgentResult | NodeOutcome``。
ExecuteFn = Callable[["Node", "NodeContext"], Any]


# --------------------------------------------------------------------------- #
# 节点与边
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    """流程中的一个节点：绑定 Agent 或自定义 ``execute``。"""

    id: str
    agent: str = ""                        # Agent.name（execute 存在时可为空）
    execute: Optional[ExecuteFn] = None    # 自定义执行函数（gate / route / parallel / 自定义节点）
    type: str = "agent"                    # agent | gate | route | parallel | start | end
    next: List[str] = field(default_factory=list)   # 出边（默认按顺序尝试第一个可运行的）
    when: Optional[Callable[["NodeContext"], bool]] = None  # 入边条件（False 则跳过该节点）
    retry: int = 0                         # 失败重试次数（不含首次）
    retry_delay: float = 0.2               # 重试退避基数（秒）
    on_error: str = "fail"                 # fail | continue | fallback:<node_id>
    fallback: Optional[str] = None         # on_error 为 fallback 时的目标节点
    loop_of: str = ""                      # 所属循环标识（用于 iterations 计数）
    max_iterations: int = 0                # 该循环最多跑几次（0 = 用 global max_iterations）
    children: List["Node"] = field(default_factory=list)    # type == parallel 时使用
    options: Dict[str, Any] = field(default_factory=dict)   # 其它扩展参数
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "type": self.type,
            "next": list(self.next or []),
            "retry": self.retry,
            "retry_delay": self.retry_delay,
            "on_error": self.on_error,
            "fallback": self.fallback,
            "loop_of": self.loop_of,
            "max_iterations": self.max_iterations,
            "children": [child.to_dict() for child in (self.children or [])],
            "options": jsonable(self.options),
            "description": self.description,
            # 运行期对象（不可序列化）只记录"是否存在"，供 resume 提示
            "has_execute": self.execute is not None,
            "has_when": self.when is not None,
        }

    def __post_init__(self) -> None:
        if not self.id:
            raise NodeError("节点 id 不能为空")
        if self.type not in NODE_TYPES:
            raise NodeError("节点 %s 的 type 非法：%r（可选 %s）"
                            % (self.id, self.type, "/".join(NODE_TYPES)))
        if self.retry < 0:
            raise NodeError("节点 %s 的 retry 不能为负数" % self.id)


@dataclass
class Edge:
    """一条有向边；``when`` / ``label`` 用于决策路由（如 ``retry`` / ``fail`` / ``pass``）。"""

    src: str
    dst: str
    when: str = ""
    label: str = ""

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "when": self.when, "label": self.label}


# --------------------------------------------------------------------------- #
# 执行上下文
# --------------------------------------------------------------------------- #
@dataclass
class NodeContext:
    """节点执行上下文（由 engine 构造）。

    ``inputs`` 只包含**直接前驱**节点最近一次成功的结果；
    ``extras`` 是引擎注入的临时数据（如 parallel 合并结果），普通节点不必关心。
    """

    node: Node
    state: "Blackboard"
    bus: "MessageBus"
    inputs: Dict[str, AgentResult] = field(default_factory=dict)
    iteration: int = 0                     # 当前节点所属循环已执行的次数
    turn: int = 0
    workspace: str = ""
    config: Any = None                     # RunConfig（engine 模块定义，鸭子类型使用）
    extras: Dict[str, Any] = field(default_factory=dict)

    # ---- 上游结果访问 ----
    def input_result(self, node_id: str, default: Optional[AgentResult] = None) -> Optional[AgentResult]:
        """取某个直接前驱的结果（不存在返回 ``default``）。"""
        value = self.inputs.get(node_id)
        if value is None:
            return default
        if isinstance(value, AgentResult):
            return value
        if isinstance(value, dict):
            return AgentResult.from_dict(value)
        return default

    def payload(self, node_id: str, key: str, default: Any = None) -> Any:
        """取上游 ``result.data[key]``。"""
        value = self.inputs.get(node_id)
        if isinstance(value, AgentResult):
            data = value.data
        elif isinstance(value, dict):
            data = value.get("data") or {}
        else:
            data = None
        if isinstance(data, dict) and key in data:
            return data[key]
        return default

    def first_payload(self, key: str, default: Any = None) -> Any:
        """按 ``inputs`` 声明顺序找第一个含 ``key`` 的上游载荷。"""
        for node_id in list(self.inputs.keys()):
            value = self.payload(node_id, key, _MISSING)
            if value is not _MISSING:
                return value
        return default

    def all_payloads(self, key: str) -> Dict[str, Any]:
        """收集所有上游中携带 ``key`` 的载荷：``{上游节点 id: 值}``。"""
        result: Dict[str, Any] = {}
        for node_id in list(self.inputs.keys()):
            value = self.payload(node_id, key, _MISSING)
            if value is not _MISSING:
                result[node_id] = value
        return result

    def predecessors(self) -> List[str]:
        """直接前驱节点 id 列表（按声明顺序）。"""
        return list(self.inputs.keys())

    def blackboard_get(self, key: str, default: Any = None) -> Any:
        """读黑板（等价 ``ctx.state.get``，为 Agent 提供短路径）。"""
        return self.state.get(key, default)

    def log(self, message: str) -> None:
        """记录一条节点级日志（统一走 logging，不 print）。"""
        logger.info("[%s] %s", self.node.id, message)


# --------------------------------------------------------------------------- #
# 流程
# --------------------------------------------------------------------------- #
class Workflow:
    """由节点与边构成的流程图定义。"""

    def __init__(self, name: str, *, description: str = "", entry: str = "",
                 nodes: Optional[List[Node]] = None, edges: Optional[List[Edge]] = None,
                 max_turns: int = 50, max_iterations: int = 3) -> None:
        if not name:
            raise NodeError("流程 name 不能为空")
        self._name = name
        self._description = description
        self._entry = entry
        self._nodes: Dict[str, Node] = {}
        self._edges: List[Edge] = []
        self._max_turns = int(max_turns)
        self._max_iterations = int(max_iterations)
        for node in (nodes or []):
            self.add_node(node)
        for edge in (edges or []):
            self.add_edge(edge.src, edge.dst, when=edge.when, label=edge.label)

    # ---- 基本属性 ----
    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def entry(self) -> str:
        return self._entry

    @property
    def max_turns(self) -> int:
        return self._max_turns

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    # ---- 结构维护 ----
    def node(self, node_id: str) -> Optional[Node]:
        """按 id 取节点（不存在返回 None）。"""
        return self._nodes.get(node_id)

    def nodes(self) -> List[Node]:
        """全部节点（声明顺序）。"""
        return list(self._nodes.values())

    def edges(self) -> List[Edge]:
        """全部边（声明顺序）。"""
        return list(self._edges)

    def add_node(self, node: Node) -> Node:
        """加入节点；id 重复抛 ``NodeError``。"""
        if node.id in self._nodes:
            raise NodeError("节点 id 重复：%s" % node.id)
        self._nodes[node.id] = node
        return node

    def add_edge(self, src: str, dst: str, *, when: str = "", label: str = "") -> Edge:
        """加入一条边，并同步维护 ``src`` 节点的 ``next`` 顺序。"""
        edge = Edge(src=src, dst=dst, when=when, label=label)
        self._edges.append(edge)
        source = self._nodes.get(src)
        if source is not None and dst not in (source.next or []):
            source.next = list(source.next or []) + [dst]
        return edge

    def entry_node(self) -> Optional[Node]:
        """入口节点：显式 ``entry`` 优先，否则取第一个节点。"""
        if self._entry:
            node = self._nodes.get(self._entry)
            if node is None:
                raise NodeError("entry 节点不存在：%s" % self._entry)
            return node
        for node in self._nodes.values():
            return node
        return None

    def successors(self, node_id: str) -> List[Node]:
        """出边目标节点（按声明顺序，去重）。"""
        result: List[Node] = []
        seen = set()
        for target_id in self._out_ids(node_id):
            if target_id in seen:
                continue
            seen.add(target_id)
            target = self._nodes.get(target_id)
            if target is not None:
                result.append(target)
        return result

    def _out_ids(self, node_id: str) -> List[str]:
        """出边目标 id：``node.next`` 声明顺序 + 边声明顺序（去重）。"""
        node = self._nodes.get(node_id)
        ids: List[str] = []
        if node is not None:
            ids.extend([dst for dst in (node.next or [])])
        ids.extend([edge.dst for edge in self._edges if edge.src == node_id])
        unique: List[str] = []
        for item in ids:
            if item not in unique:
                unique.append(item)
        return unique

    def back_edges(self) -> List[Tuple[str, str]]:
        """找出回边（指向 DFS 栈上祖先的边），用于图文档标注与"体现循环"。"""
        result: List[Tuple[str, str]] = []
        colour: Dict[str, int] = {}

        def visit(node_id: str) -> None:
            colour[node_id] = 1
            for target in self._out_ids(node_id):
                if target not in self._nodes:
                    continue
                state = colour.get(target, 0)
                if state == 1:
                    result.append((node_id, target))
                elif state == 0:
                    visit(target)
            colour[node_id] = 2

        try:
            for node in self.nodes():
                if colour.get(node.id, 0) == 0:
                    visit(node.id)
        except RecursionError:  # pragma: no cover - 超长链路的兜底
            logger.warning("流程 %s 过深，跳过回边分析", self._name)
        return result

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "name": self._name,
            "description": self._description,
            "entry": self._entry,
            "max_turns": self._max_turns,
            "max_iterations": self._max_iterations,
            "nodes": [node.to_dict() for node in self.nodes()],
            "edges": [edge.to_dict() for edge in self._edges],
        }

    # ---- 图文档 ----
    def _ident_map(self) -> Dict[str, str]:
        """Mermaid 标识符映射（把任意 id 转成合法且唯一的标识符）。"""
        mapping: Dict[str, str] = {}
        used = set()
        for node in self.nodes():
            mapping[node.id] = _unique_ident(_safe_ident(node.id), used)
            for child in (node.children or []):
                key = "%s__%s" % (node.id, child.id)
                mapping[key] = _unique_ident(_safe_ident(child.id, prefix="%s__" % node.id), used)
        return mapping

    def to_mermaid(self) -> str:
        """导出 Mermaid ``flowchart`` 文本（含并行子图与回边标注）。"""
        idents = self._ident_map()
        back = set(self.back_edges())
        lines = ["flowchart TD"]
        for node in self.nodes():
            lines.append("    " + _mermaid_node(idents.get(node.id, node.id), node))
            if node.type == "parallel" and node.children:
                subgraph = "sg_%s" % idents.get(node.id, node.id)
                lines.append('    subgraph %s["%s / 并行子节点"]' % (subgraph, _mermaid_text(node.id)))
                lines.append("        direction LR")
                for child in node.children:
                    key = "%s__%s" % (node.id, child.id)
                    lines.append("        " + _mermaid_node(idents.get(key, key), child))
                lines.append("    end")
                for child in node.children:
                    key = "%s__%s" % (node.id, child.id)
                    lines.append("    %s -.-> %s"
                                 % (idents.get(node.id, node.id), idents.get(key, key)))
        for edge in self._edges:
            src = idents.get(edge.src, _safe_ident(edge.src))
            dst = idents.get(edge.dst, _safe_ident(edge.dst))
            label = edge.label or edge.when or ""
            if (edge.src, edge.dst) in back:
                label = ("%s ↺回边" % label).strip()
                arrow = "-.->"
            else:
                arrow = "-->"
            if label:
                lines.append('    %s %s|"%s"| %s' % (src, arrow, _mermaid_text(label), dst))
            else:
                lines.append("    %s %s %s" % (src, arrow, dst))
        return "\n".join(lines) + "\n"

    def to_dot(self) -> str:
        """导出 Graphviz DOT 文本（回边用虚线 + ``constraint=false`` 标注）。"""
        back = set(self.back_edges())
        lines = ['digraph "%s" {' % _dot_escape(self._name),
                 "    rankdir=TD;",
                 '    label="%s";' % _dot_escape(self._name or "workflow"),
                 '    labelloc="t";',
                 '    node [shape=box, style="rounded,filled", fillcolor="#f5f7fa", fontname="Helvetica"];',
                 '    edge [fontname="Helvetica", fontsize=10];']
        for node in self.nodes():
            shape = _DOT_SHAPES.get(node.type, "box")
            label = _dot_escape(_dot_node_label(node))
            lines.append('    "%s" [shape=%s, label="%s"];' % (_dot_escape(node.id), shape, label))
            if node.type == "parallel" and node.children:
                lines.append('    subgraph "cluster_%s" {' % _dot_escape(node.id))
                lines.append('        label="%s 并行子节点";' % _dot_escape(node.id))
                lines.append("        style=dashed; color=\"#95a5a6\";")
                for child in node.children:
                    child_id = "%s__%s" % (node.id, child.id)
                    lines.append('        "%s" [shape=%s, label="%s"];'
                                 % (_dot_escape(child_id), _DOT_SHAPES.get(child.type, "box"),
                                    _dot_escape(_dot_node_label(child))))
                lines.append("    }")
                for child in node.children:
                    child_id = "%s__%s" % (node.id, child.id)
                    lines.append('    "%s" -> "%s" [style=dotted, arrowhead=none];'
                                 % (_dot_escape(node.id), _dot_escape(child_id)))
        for edge in self._edges:
            attrs = []
            label = edge.label or edge.when or ""
            if label:
                attrs.append('label="%s"' % _dot_escape(label))
            if (edge.src, edge.dst) in back:
                attrs.extend(['color="#c0392b"', "style=dashed", "constraint=false"])
                attrs.append('xlabel="回边"')
            tail = (" [" + ", ".join(attrs) + "]") if attrs else ""
            lines.append('    "%s" -> "%s"%s;' % (_dot_escape(edge.src), _dot_escape(edge.dst), tail))
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ---- 校验 ----
    def validate(self) -> List[str]:
        """返回问题列表；空列表 = 合法。"""
        problems: List[str] = []
        if not self._nodes:
            problems.append("流程没有任何节点（缺少 entry）")
            return problems

        for node in self.nodes():
            if node.type == "parallel":
                if not node.children:
                    problems.append("并行节点 %s 没有子节点" % node.id)
                for child in node.children:
                    if not child.id:
                        problems.append("并行节点 %s 存在无 id 的子节点" % node.id)
            if node.on_error not in ON_ERROR_VALUES and not node.on_error.startswith("fallback:"):
                problems.append("节点 %s 的 on_error 非法：%r" % (node.id, node.on_error))
            if node.on_error.startswith("fallback:") and not node.on_error.split(":", 1)[1].strip():
                problems.append("节点 %s 的 on_error=fallback: 缺少目标节点" % node.id)
            if node.fallback and node.fallback not in self._nodes:
                problems.append("节点 %s 的 fallback 不存在：%s" % (node.id, node.fallback))
            for target in (node.next or []):
                if target not in self._nodes:
                    problems.append("悬空边：%s -> %s（目标节点不存在）" % (node.id, target))
            if node.type in ("gate", "route") and node.execute is None and not node.agent:
                logger.warning("节点 %s 是 %s 但没有 execute 也没有 agent，运行时会失败", node.id, node.type)

        for edge in self._edges:
            if edge.src not in self._nodes:
                problems.append("悬空边：%s -> %s（源节点不存在）" % (edge.src, edge.dst))
            if edge.dst not in self._nodes:
                problems.append("悬空边：%s -> %s（目标节点不存在）" % (edge.src, edge.dst))

        if self._entry and self._entry not in self._nodes:
            problems.append("entry 节点不存在：%s" % self._entry)
        if not self._entry:
            problems.extend(self._entry_ambiguity())
        unique: List[str] = []
        for problem in problems:
            if problem not in unique:
                unique.append(problem)
        return unique

    def _entry_ambiguity(self) -> List[str]:
        """未显式指定 entry 时：若存在多个"根"节点且没有 start 节点，则入口不唯一。"""
        if any(node.type == "start" for node in self.nodes()):
            return []
        incoming = set()
        for edge in self._edges:
            incoming.add(edge.dst)
        for node in self.nodes():
            for target in (node.next or []):
                incoming.add(target)
        roots = [node.id for node in self.nodes() if node.id not in incoming]
        if len(roots) > 1:
            return ["缺少 entry 节点：存在多个入口候选 %s" % ("/".join(sorted(roots)))]
        return []


# --------------------------------------------------------------------------- #
# 流式 Builder
# --------------------------------------------------------------------------- #
class WorkflowBuilder:
    """流式声明 API：``WorkflowBuilder("x").start().agent(...).edge(...).build()``。"""

    def __init__(self, name: str, *, description: str = "") -> None:
        if not name:
            raise NodeError("流程 name 不能为空")
        self._name = name
        self._description = description
        self._nodes: List[Node] = []
        self._edges: List[Edge] = []
        self._entry = ""
        self._ids = set()

    # ---- 内部 ----
    def _add(self, node: Node) -> "WorkflowBuilder":
        if node.id in self._ids:
            raise NodeError("节点 id 重复：%s" % node.id)
        self._ids.add(node.id)
        self._nodes.append(node)
        return self

    # ---- 节点声明 ----
    def agent(self, node_id: str, agent: str, *, execute: Optional[ExecuteFn] = None,
              description: str = "", retry: int = 0, retry_delay: float = 0.2,
              on_error: str = "fail", loop_of: str = "", max_iterations: int = 0,
              when: Optional[Callable[["NodeContext"], bool]] = None) -> "WorkflowBuilder":
        """声明一个绑定 Agent 的普通节点（``execute`` 存在时优先于 Agent）。"""
        return self._add(Node(id=node_id, agent=agent, execute=execute, type="agent",
                              retry=retry, retry_delay=retry_delay, on_error=on_error,
                              loop_of=loop_of, max_iterations=max_iterations, when=when,
                              description=description))

    def gate(self, node_id: str, *, execute: ExecuteFn, description: str = "",
             loop_of: str = "", max_iterations: int = 0, retry: int = 0,
             on_error: str = "fail") -> "WorkflowBuilder":
        """声明门禁节点：由 ``execute`` 依据上游结果决定 ``pass`` / ``retry`` / ``fail``。"""
        return self._add(Node(id=node_id, execute=execute, type="gate", description=description,
                              loop_of=loop_of, max_iterations=max_iterations, retry=retry,
                              on_error=on_error))

    def route(self, node_id: str, *, execute: ExecuteFn, description: str = "",
              loop_of: str = "", max_iterations: int = 0, retry: int = 0,
              on_error: str = "fail") -> "WorkflowBuilder":
        """声明路由节点（多分支选择）。"""
        return self._add(Node(id=node_id, execute=execute, type="route", description=description,
                              loop_of=loop_of, max_iterations=max_iterations, retry=retry,
                              on_error=on_error))

    def parallel(self, node_id: str, children: List[Node], *, execute: Optional[ExecuteFn] = None,
                 description: str = "", retry: int = 0, retry_delay: float = 0.2,
                 on_error: str = "fail") -> "WorkflowBuilder":
        """声明并行节点：``children`` 用线程池真并发，合并到 ``data["children"]``。"""
        return self._add(Node(id=node_id, type="parallel", children=list(children or []),
                              execute=execute, description=description, retry=retry,
                              retry_delay=retry_delay, on_error=on_error))

    def start(self, node_id: str = "start") -> "WorkflowBuilder":
        """声明开始节点（同时作为默认 entry）。"""
        self._add(Node(id=node_id, type="start"))
        if not self._entry:
            self._entry = node_id
        return self

    def end(self, node_id: str = "end") -> "WorkflowBuilder":
        """声明结束节点。"""
        return self._add(Node(id=node_id, type="end"))

    def node(self, node: Node) -> "WorkflowBuilder":
        """直接加入一个已构造好的 ``Node``（用于自定义节点或并行子节点复用）。"""
        return self._add(node)

    # ---- 边声明 ----
    def edge(self, src: str, dst: str, *, when: str = "", label: str = "") -> "WorkflowBuilder":
        """声明一条边；``when`` / ``label`` 参与决策路由。"""
        self._edges.append(Edge(src=src, dst=dst, when=when, label=label))
        return self

    def chain(self, *node_ids: str) -> "WorkflowBuilder":
        """便捷方法：把一串节点按顺序串起来。"""
        for src, dst in zip(node_ids, node_ids[1:]):
            self.edge(src, dst)
        return self

    # ---- 构建 ----
    def build(self, *, entry: Optional[str] = None, max_turns: int = 50,
              max_iterations: int = 3) -> Workflow:
        """构建并校验流程；问题非空抛 ``NodeError``。"""
        workflow = Workflow(self._name, description=self._description,
                            entry=entry if entry is not None else self._entry,
                            max_turns=max_turns, max_iterations=max_iterations)
        for node in self._nodes:
            workflow.add_node(node)
        for edge in self._edges:
            workflow.add_edge(edge.src, edge.dst, when=edge.when, label=edge.label)
        problems = workflow.validate()
        if problems:
            raise NodeError("流程 %s 定义不合法：%s" % (self._name, "；".join(problems)))
        return workflow

    def to_dict(self) -> dict:
        return {"name": self._name, "description": self._description,
                "nodes": [node.to_dict() for node in self._nodes],
                "edges": [edge.to_dict() for edge in self._edges]}


# --------------------------------------------------------------------------- #
# 图文档辅助
# --------------------------------------------------------------------------- #
_DOT_SHAPES = {
    "agent": "box",
    "gate": "diamond",
    "route": "diamond",
    "parallel": "component",
    "start": "ellipse",
    "end": "ellipse",
}

_MERMAID_SHAPES = {
    "agent": ('["%s"]', '["%s"]'),
    "start": ('(["%s"])', '(["%s"])'),
    "end": ('(["%s"])', '(["%s"])'),
    "gate": ('{"%s"}', '{"%s"}'),
    "route": ('{"%s"}', '{"%s"}'),
    "parallel": ('{{"%s"}}', '{{"%s"}}'),
}


def _safe_ident(text: str, prefix: str = "") -> str:
    """把任意字符串转成合法的 Mermaid 标识符。"""
    ident = re.sub(r"[^0-9A-Za-z_]", "_", "%s%s" % (prefix, text))
    if not ident or ident[0].isdigit():
        ident = "n_" + ident
    return ident


def _unique_ident(ident: str, used: set) -> str:
    candidate = ident
    index = 1
    while candidate in used:
        index += 1
        candidate = "%s_%d" % (ident, index)
    used.add(candidate)
    return candidate


def _mermaid_text(text: str) -> str:
    """Mermaid 标签文本：去引号、换行转 ``<br/>``。"""
    return str(text).replace('"', "'").replace("\n", "<br/>").replace("|", "/")


def _mermaid_node(ident: str, node: Node) -> str:
    """生成一行 Mermaid 节点声明。"""
    label = node.id
    if node.agent:
        label += "<br/>" + node.agent
    if node.type != "agent":
        label += "<br/>(" + node.type + ")"
    template = _MERMAID_SHAPES.get(node.type, ('["%s"]', '["%s"]'))[0]
    return "%s %s" % (ident, template % _mermaid_text(label))


def _dot_escape(text: str) -> str:
    """DOT 字符串转义。"""
    return (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", ""))


def _dot_node_label(node: Node) -> str:
    """DOT 节点标签（多行展示 id / agent / type）。"""
    parts = [node.id]
    if node.agent:
        parts.append(node.agent)
    if node.type != "agent":
        parts.append("(%s)" % node.type)
    return "\n".join(parts)


__all__ = [
    "AgentResult",
    "NodeOutcome",
    "ExecuteFn",
    "Node",
    "Edge",
    "Workflow",
    "WorkflowBuilder",
    "NodeContext",
    "NODE_TYPES",
    "DECISIONS",
    "ON_ERROR_VALUES",
    "jsonable",
]
