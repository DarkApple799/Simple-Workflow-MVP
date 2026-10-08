"""Agent 抽象、注册表，以及节点上下文的公共工具。

依赖方向：``agents/*`` 依赖 ``graph``（AgentResult / NodeContext）与更底层的
``errors`` / ``bus`` / ``state``，绝不反向依赖 ``engine`` 与 ``cli``。

本模块提供三类东西：
    * :class:`Agent` —— 所有 Agent 的抽象基类（``name`` / ``role`` / ``run(ctx)``）；
    * :class:`AgentRegistry` —— 按名字查找 Agent 的注册表；
    * :func:`default_registry` —— 注册全部内置 Agent 的工厂；
    * 一组"上下文小工具"：配置取值（``ctx.config`` 是 RunConfig、注入的 config 是
      ``wfa.config.Config``，两者都要兼容）、工作区读写、消息广播、上游结果汇总。
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from ..errors import ConfigError, PathSecurityError
from ..graph import AgentResult, NodeContext

logger = logging.getLogger("wfa.agents")


# --------------------------------------------------------------------------- #
# Agent 抽象
# --------------------------------------------------------------------------- #
class Agent(ABC):
    """Agent 抽象基类。

    子类必须声明唯一的 :attr:`name`，并实现 :meth:`run`：

    ``run(ctx) -> AgentResult``，其中 ``AgentResult.data`` 必须遵守
    ``docs/CONTRACT.md`` 第 5 节末尾的载荷约定（review 出 issues、fix 出 patches……）。
    """

    #: 唯一标识（工作流的节点通过它绑定 Agent）。
    name = "agent"
    #: 人类可读角色名（报告与日志使用）。
    role = "Agent"

    def __init__(self, model=None, *, config=None, bus=None) -> None:
        self.model = model
        self.config = config
        self.bus = bus

    @abstractmethod
    def run(self, ctx: NodeContext) -> AgentResult:
        """执行一次节点任务，返回统一结果。"""

    def describe(self) -> str:
        """自我介绍（``run.py list`` 与报告使用）。"""
        doc = (type(self).__doc__ or "").strip().splitlines()
        brief = doc[0].strip() if doc else ""
        for prefix in (f"{self.role}：", f"{self.role}:", f"{self.name}："):
            if brief.startswith(prefix):
                brief = brief[len(prefix):]
                break
        return f"{self.name}（{self.role}）：{brief}" if brief else f"{self.name}（{self.role}）"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


class AgentRegistry:
    """Agent 注册表：按 ``name`` 注册与查找，缺失时抛 :class:`ConfigError`。"""

    def __init__(self) -> None:
        self._agents: dict = {}

    def register(self, agent: Agent) -> Agent:
        """登记一个 Agent（同名覆盖），返回它自身便于链式调用。"""
        name = str(getattr(agent, "name", "") or "").strip()
        if not name:
            raise ConfigError("Agent 必须有非空的 name：%r" % (type(agent).__name__,))
        self._agents[name] = agent
        return agent

    def get(self, name: str) -> Agent:
        """按名字取 Agent；不存在抛 :class:`ConfigError`（附可用清单）。"""
        agent = self._agents.get(str(name or "").strip())
        if agent is None:
            raise ConfigError("未注册的 Agent %r；可用：%s" % (name, ", ".join(self.names())))
        return agent

    def names(self) -> list:
        """全部已注册的 Agent 名字（排序，保证输出稳定）。"""
        return sorted(self._agents)

    def describe(self) -> list:
        """结构化清单：``[{"name","role","describe"}]``。"""
        return [
            {"name": name, "role": self._agents[name].role, "describe": self._agents[name].describe()}
            for name in self.names()
        ]

    def all(self) -> list:
        """全部 Agent 实例（按名字排序）。"""
        return [self._agents[name] for name in self.names()]

    def __contains__(self, name: object) -> bool:
        return str(name) in self._agents

    def __len__(self) -> int:
        return len(self._agents)


# --------------------------------------------------------------------------- #
# 上下文小工具：配置 / 工作区 / 消息 / 上游结果
# --------------------------------------------------------------------------- #
def config_value(ctx, config, name: str, default=None):
    """取配置项：``ctx.config``（RunConfig）→ 注入的 ``Config`` → ``extra`` → 默认值。

    ``None`` 视为"未设置"（这样 ``ctx.config`` 上没有的键会自然回落到 ``Config``），
    而 ``False`` / ``0`` 是有效值。
    """
    for source in (getattr(ctx, "config", None), config):
        if source is None:
            continue
        value = getattr(source, name, None)
        if value is not None:
            return value
    for source in (getattr(ctx, "config", None), config):
        extra = getattr(source, "extra", None)
        if isinstance(extra, dict) and name in extra:
            return extra[name]
    return default


def state_value(ctx, key: str, default=None):
    """安全读取黑板键值（黑板缺失或异常时返回默认值）。"""
    state = getattr(ctx, "state", None)
    if state is None:
        return default
    try:
        return state.get(key, default)
    except Exception:  # noqa: BLE001 - 读黑板失败不应中断 Agent
        logger.debug("读取黑板键 %s 失败", key, exc_info=True)
        return default


def agent_task(ctx, config=None) -> str:
    """取本次运行的任务描述（优先黑板 ``task``，其次 ``RunConfig.task``）。"""
    for value in (state_value(ctx, "task", ""), config_value(ctx, config, "task", "")):
        if value:
            return str(value).strip()
    return ""


def workspace_root(ctx, config=None, default: str = "") -> str:
    """确定本次节点操作的工作区目录（``ctx.workspace`` → 黑板 → 配置）。"""
    candidates = [
        getattr(ctx, "workspace", ""),
        state_value(ctx, "workspace", ""),
        config_value(ctx, config, "workspace", ""),
    ]
    for candidate in candidates:
        if candidate:
            return os.path.abspath(str(candidate))
    return os.path.abspath(default or os.getcwd())


def safe_workspace_path(root: str, rel: str) -> str:
    """把工作区相对路径解析为绝对路径，越界抛 :class:`PathSecurityError`。"""
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, str(rel or "").replace("/", os.sep)))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        raise PathSecurityError("路径越界，已拒绝访问：%r" % (rel,))
    return target


def write_workspace_file(ctx, rel: str, content: str, *, config=None) -> str:
    """写入工作区文件：优先 ``blackboard.write_file``（自带路径安全），返回绝对路径。"""
    state = getattr(ctx, "state", None)
    if state is not None and hasattr(state, "write_file"):
        try:
            return str(state.write_file(rel, content))
        except PathSecurityError:
            raise
        except Exception as exc:  # noqa: BLE001 - 黑板写入失败则退回直接写文件
            logger.warning("blackboard.write_file(%s) 失败，改为直接写文件：%s", rel, exc)
    path = safe_workspace_path(workspace_root(ctx, config), rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def read_workspace_file(ctx, rel: str, *, config=None):
    """读取工作区文件；不存在返回 ``None``。"""
    state = getattr(ctx, "state", None)
    if state is not None and hasattr(state, "read_file"):
        try:
            value = state.read_file(rel)
            if value is not None:
                return value
        except Exception:  # noqa: BLE001 - 读不到则退回直接读文件
            logger.debug("blackboard.read_file(%s) 失败", rel, exc_info=True)
    path = safe_workspace_path(workspace_root(ctx, config), rel)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def publish_message(ctx, kind: str, payload: dict, *, correlation_id: str = "") -> None:
    """向消息总线广播一条 Agent 消息（失败只记日志，绝不影响主流程）。"""
    bus = getattr(ctx, "bus", None)
    if bus is None:
        return
    try:
        from ..bus import BROADCAST

        node = getattr(ctx, "node", None)
        sender = str(getattr(node, "id", "") or "agent")
        bus.send(kind, sender, BROADCAST, dict(payload or {}),
                 correlation_id=correlation_id, turn=int(getattr(ctx, "turn", 0) or 0))
    except Exception:  # noqa: BLE001 - 消息只是观测手段
        logger.debug("Agent 消息发送失败：kind=%s", kind, exc_info=True)


def result_dict(value) -> dict:
    """把 ``AgentResult`` / dict 统一成 dict 视图（其它类型返回空 dict）。"""
    if isinstance(value, AgentResult):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    return {}


def input_results(ctx) -> list:
    """直接前驱结果：``[(node_id, result_dict), ...]``（按声明顺序）。"""
    rows = []
    for node_id, value in dict(getattr(ctx, "inputs", {}) or {}).items():
        raw = result_dict(value)
        if raw:
            rows.append((str(node_id), raw))
    return rows


def known_results(ctx) -> list:
    """已知的全部节点结果：直接前驱 + 黑板 ``results`` 命名空间（去重）。"""
    rows = input_results(ctx)
    seen = {node_id for node_id, _ in rows}
    results = state_value(ctx, "results", None)
    if isinstance(results, dict):
        for node_id, raw in results.items():
            if isinstance(raw, dict) and str(node_id) not in seen:
                rows.append((str(node_id), raw))
                seen.add(str(node_id))
    return rows


# --------------------------------------------------------------------------- #
# 默认注册表
# --------------------------------------------------------------------------- #
def default_registry(model=None, *, config=None, bus=None) -> AgentRegistry:
    """注册全部内置 Agent，返回 :class:`AgentRegistry`。

    Agent 名字与 ``docs/CONTRACT.md`` 第 7 节保持一致（工作流按名字绑定）：
    ``coder`` / ``requirements`` / ``designer`` / ``planner`` / ``writer`` /
    ``proofreader`` / ``reviewer`` / ``security_reviewer`` /
    ``maintainability_reviewer`` / ``fixer`` / ``legacy_reviewer``。
    ``config`` 为 ``wfa.config.Config``（可为 None），会透传给每个 Agent。
    """
    from .code_review import LegacyReviewAgent
    from .coding import CodingAgent
    from .doc import DocAgent
    from .fix import FixAgent
    from .review import ReviewAgent

    registry = AgentRegistry()
    registry.register(CodingAgent(model, config=config, bus=bus))

    # 需求 / 设计 / 文档类角色：同一个 DocAgent 按 role 参数化
    for role in ("requirements", "designer", "planner", "writer", "proofreader"):
        registry.register(DocAgent(model, config=config, bus=bus, role=role))
    registry.register(DocAgent(model, config=config, bus=bus, role="writer", name="documenter"))

    # 审查类：同一 ReviewAgent 按 focus 参数化（安全线 / 可维护性线 / 汇总）
    registry.register(ReviewAgent(model, config=config, bus=bus, focus="all", name="reviewer",
                                  role="代码审查 Agent"))
    registry.register(ReviewAgent(model, config=config, bus=bus, focus="security",
                                  name="security_reviewer", role="安全审查 Agent"))
    registry.register(ReviewAgent(model, config=config, bus=bus, focus="maintainability",
                                  name="maintainability_reviewer", role="可维护性审查 Agent"))
    registry.register(FixAgent(model, config=config, bus=bus))

    # Homework1 适配器：找不到 cra 工程时会自动回退到 wfa.analysis，不会崩
    cra_root = None
    extra = getattr(config, "extra", None)
    if isinstance(extra, dict):
        cra_root = extra.get("cra_root") or extra.get("hw1_root") or None
    legacy_offline = not bool(getattr(config, "online", False))
    registry.register(LegacyReviewAgent(
        cra_root=cra_root, offline=legacy_offline, max_steps=4, config=config, bus=bus,
    ))
    return registry


__all__ = [
    "Agent",
    "AgentRegistry",
    "default_registry",
    "config_value",
    "state_value",
    "agent_task",
    "workspace_root",
    "safe_workspace_path",
    "write_workspace_file",
    "read_workspace_file",
    "publish_message",
    "result_dict",
    "input_results",
    "known_results",
]
