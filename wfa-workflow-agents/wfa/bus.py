"""Agent 间消息总线：发布 / 订阅 + 收件箱 + 等待应答 + 运行日志。

`wfa/bus.py` 是"多 Agent 协作"的通信底座，抽象极简但覆盖编排器需要的能力：

* **收件箱（inbox）**：每条消息都按 recipient 归档，任何一方随时可以回看；
  广播（`BROADCAST = "*"`）对所有订阅者可见，因此 `inbox()` 也要包含广播。
* **订阅（Subscription）**：`threading.Condition` 实现，`drain(timeout)` 支持
  "立即取走"与"最多等 N 秒"，供并行分支与 Web 实时日志使用。
* **等待应答（wait_for）**：请求-应答是 Agent 协作的常见形态
  （`ask()` 发 question、`reply()` 带 correlation_id 回 answer）。
  `wait_for` 先扫描收件箱里**尚未被 wait_for 取走**的匹配消息，再阻塞等待新消息——
  这样即使应答在 `wait_for` 之前到达（多线程下很常见）也不会漏掉。
* **纯 JSON 可序列化**：`to_dict()` / `from_dict()` 让检查点能带上完整消息历史。

线程安全：所有公开方法都在同一把可重入锁（`Condition` 内部锁）下操作，
`publish` 结束后 notify_all 唤醒所有 `wait_for` 等待者。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from .util import jsonable, new_id, now_iso, truncate

logger = logging.getLogger("wfa.bus")

#: 广播/系统消息的固定 sender（也可作为"引擎自身"的收件人）。
PERF_ID = "workflow"

#: 广播收件人。
BROADCAST = "*"

#: 等待新消息时的轮询上限：即使 notify 丢失，也能在 0.5 秒内重新检查。
_WAIT_SLICE = 0.5


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    """一条 Agent 间消息。

    Attributes:
        kind: task | result | question | answer | review | feedback | system | error
        sender: 发送方（节点 id 或 PERF_ID）
        recipient: 接收方（节点 id 或 BROADCAST）
        payload: 结构化载荷（建议保持纯 JSON 可序列化）
        id: 消息 id，留空时自动生成 `msg-xxxxxxxx`
        created_at: 创建时间，留空时自动填 `now_iso()`
        turn: 产生该消息的流程回合
        correlation_id: 请求-应答配对（`reply()` 自动回填）
    """

    kind: str = ""
    sender: str = ""
    recipient: str = ""
    payload: dict = field(default_factory=dict)
    id: str = ""
    created_at: str = ""
    turn: int = 0
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if not self.payload:
            self.payload = {}
        elif not isinstance(self.payload, dict):
            self.payload = {"value": self.payload}
        if not self.id:
            self.id = new_id("msg")
        if not self.created_at:
            self.created_at = now_iso()
        try:
            self.turn = int(self.turn or 0)
        except (TypeError, ValueError):
            self.turn = 0

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        """转为纯 JSON 字典（payload 做降级处理，保证可落盘）。"""
        return {
            "kind": self.kind,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": jsonable(self.payload),
            "id": self.id,
            "created_at": self.created_at,
            "turn": self.turn,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """从字典还原；缺字段时使用默认值（容忍旧版检查点）。"""
        data = dict(data or {})
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {} if payload is None else {"value": jsonable(payload)}
        return cls(
            kind=str(data.get("kind") or ""),
            sender=str(data.get("sender") or ""),
            recipient=str(data.get("recipient") or ""),
            payload=payload,
            id=str(data.get("id") or ""),
            created_at=str(data.get("created_at") or ""),
            turn=data.get("turn") or 0,
            correlation_id=str(data.get("correlation_id") or ""),
        )

    def brief(self) -> str:
        """一行式摘要（日志用），不含 payload 细节。"""
        return f"{self.kind}: {self.sender} → {self.recipient}"


def _matches(message: Message, kind: str | None, correlation_id: str | None) -> bool:
    """判断消息是否满足 `wait_for` 的过滤条件。"""
    if kind is not None and message.kind != kind:
        return False
    if correlation_id is not None and message.correlation_id != correlation_id:
        return False
    return True


# --------------------------------------------------------------------------- #
# 订阅
# --------------------------------------------------------------------------- #
class Subscription:
    """某个收件人的消息订阅（线程安全队列 + 条件变量）。

    `drain(timeout)` 的语义：
        * `timeout == 0`：立即返回（可能为空列表），不阻塞；
        * `timeout > 0`：有消息立刻返回，否则最多等 `timeout` 秒；
        * `timeout is None`：一直等到有消息或订阅被关闭（谨慎使用）。
    """

    def __init__(self, recipient: str = "") -> None:
        self.recipient = recipient
        self._queue: list[Message] = []
        self._closed = False
        self._condition = threading.Condition()

    # ------------------------------------------------------------------ #
    def _push(self, message: Message) -> None:
        """内部投递：加入队列并唤醒等待者（已关闭则丢弃）。"""
        with self._condition:
            if self._closed:
                return
            self._queue.append(message)
            self._condition.notify_all()

    def drain(self, timeout: float = 0.0) -> list[Message]:
        """取走当前队列中的全部消息；必要时阻塞至多 `timeout` 秒。"""
        with self._condition:
            if not self._queue and not self._closed:
                if timeout is None:
                    self._condition.wait_for(lambda: self._queue or self._closed)
                elif timeout > 0:
                    self._condition.wait_for(lambda: self._queue or self._closed, timeout)
            messages, self._queue = self._queue, []
            return messages

    def close(self) -> None:
        """关闭订阅（幂等）：唤醒所有等待者，后续消息不再入队。"""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        """订阅是否已关闭。"""
        return self._closed

    @property
    def pending(self) -> int:
        """当前排队但尚未取走的消息数（诊断用）。"""
        return len(self._queue)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return f"<Subscription {self.recipient!r} pending={len(self._queue)} closed={self._closed}>"


# --------------------------------------------------------------------------- #
# 消息总线
# --------------------------------------------------------------------------- #
class MessageBus:
    """进程内消息总线：日志 + 收件箱 + 订阅 + 等待。

    Args:
        echo: 为 True 时把每条消息写进 `wfa.bus` 日志（`--verbose` 用）。
    """

    def __init__(self, *, echo: bool = False) -> None:
        self.echo = bool(echo)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._messages: list[Message] = []
        self._subscriptions: dict[str, Subscription] = {}
        self._consumed: dict[str, set] = {}

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(self, message: Message) -> Message:
        """发布一条消息：写入日志、投递给 recipient 的收件箱；广播投给所有订阅者。"""
        with self._condition:
            # 先入日志（日志是收件箱视图的唯一真相），再投递到订阅队列。
            self._messages.append(message)
            for target in self._delivery_targets(message):
                subscription = self._subscriptions.get(target)
                if subscription is not None:
                    subscription._push(message)
            if self.echo:
                logger.info("bus %s", message.brief())
            self._condition.notify_all()
        return message

    def _delivery_targets(self, message: Message) -> list[str]:
        """计算订阅投递目标；BROADCAST 投给所有订阅者（含 workflow）。"""
        if message.recipient != BROADCAST:
            return [message.recipient]
        targets = list(self._subscriptions.keys())
        if PERF_ID not in targets:
            targets.append(PERF_ID)
        return targets

    def send(
        self,
        kind: str,
        sender: str,
        recipient: str,
        payload: dict | None = None,
        *,
        correlation_id: str = "",
        turn: int = 0,
    ) -> Message:
        """构造并发布一条消息（最常用的入口）。"""
        message = Message(
            kind=kind,
            sender=sender,
            recipient=recipient,
            payload=dict(payload or {}),
            correlation_id=correlation_id,
            turn=turn,
        )
        return self.publish(message)

    def ask(self, sender: str, recipient: str, payload: dict, *, turn: int = 0) -> Message:
        """向 `recipient` 发问（kind="question"），返回请求消息以便配对应答。"""
        return self.send("question", sender, recipient, payload, turn=turn)

    def reply(self, request: Message, sender: str, payload: dict) -> Message:
        """应答 `request`（kind="answer"），自动带上 correlation_id 并回给提问方。"""
        correlation = request.correlation_id or request.id
        return self.send(
            "answer",
            sender,
            request.sender,
            payload,
            correlation_id=correlation,
            turn=request.turn,
        )

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def subscribe(self, recipient: str) -> Subscription:
        """订阅某个收件人的消息流（重复订阅返回同一个订阅对象）。"""
        with self._condition:
            subscription = self._subscriptions.get(recipient)
            if subscription is not None and not subscription.closed:
                return subscription
            subscription = Subscription(recipient)
            self._subscriptions[recipient] = subscription
            return subscription

    def unsubscribe(self, recipient: str) -> None:
        """取消订阅并关闭对应订阅对象（幂等）。"""
        with self._condition:
            subscription = self._subscriptions.pop(recipient, None)
        if subscription is not None:
            subscription.close()

    def subscribers(self) -> list[str]:
        """当前订阅者名单（排序，诊断/报告用）。"""
        with self._condition:
            return sorted(self._subscriptions)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def inbox(self, recipient: str) -> list[Message]:
        """某方收到的全部消息（直接投递 + 广播），按时间顺序。"""
        with self._condition:
            return self._inbox_locked(recipient)

    def _inbox_locked(self, recipient: str) -> list[Message]:
        """`inbox()` 的加锁版本（调用方需已持有锁）。"""
        return [
            message
            for message in self._messages
            if message.recipient == recipient or message.recipient == BROADCAST
        ]

    @property
    def messages(self) -> list[Message]:
        """全部已发布消息（按发布顺序的浅拷贝）。"""
        with self._condition:
            return list(self._messages)

    def history(
        self,
        *,
        kind: str | None = None,
        recipient: str | None = None,
        sender: str | None = None,
        limit: int = 0,
    ) -> list[Message]:
        """按条件过滤历史消息；`limit > 0` 时只返回**最近** limit 条（仍按时间升序）。"""
        with self._condition:
            result = [
                message
                for message in self._messages
                if (kind is None or message.kind == kind)
                and (recipient is None or message.recipient == recipient)
                and (sender is None or message.sender == sender)
            ]
        if limit and limit > 0:
            return result[-limit:]
        return result

    def wait_for(
        self,
        recipient: str,
        *,
        kind: str | None = None,
        correlation_id: str | None = None,
        timeout: float = 0.0,
    ) -> Message | None:
        """等待一条满足条件的消息，超时返回 None。

        语义：返回收件箱中**尚未被 `wait_for` 取走**的第一条匹配消息；若当前没有，
        则阻塞至多 `timeout` 秒等新消息。`timeout=0` 表示立即返回、不阻塞。
        已被 `wait_for` 取走的消息按 id 记账，因此重复调用不会拿到同一条消息，
        而"应答比等待先到"的竞态也不会漏消息。
        """
        try:
            budget = None if timeout is None else max(0.0, float(timeout))
        except (TypeError, ValueError):
            budget = 0.0
        deadline = None if budget is None else time.monotonic() + budget

        with self._condition:
            consumed = self._consumed.setdefault(recipient, set())
            while True:
                for candidate in self._inbox_locked(recipient):
                    if candidate.id in consumed:
                        continue
                    if _matches(candidate, kind, correlation_id):
                        consumed.add(candidate.id)
                        return candidate

                if deadline is None:
                    self._condition.wait(_WAIT_SLICE)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, _WAIT_SLICE))

    # ------------------------------------------------------------------ #
    # 序列化 / 日志
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        """导出为 `{"messages": [...], "counts": {kind: n}}`（纯 JSON）。"""
        counts: dict = {}
        for message in self.messages:
            counts[message.kind] = counts.get(message.kind, 0) + 1
        return {"messages": [message.to_dict() for message in self.messages], "counts": counts}

    @classmethod
    def from_dict(cls, data: dict) -> "MessageBus":
        """从 `to_dict()` 的结果还原总线（恢复完整消息历史与收件箱视图）。"""
        bus = cls()
        if not isinstance(data, dict):
            return bus
        for item in data.get("messages") or []:
            if isinstance(item, Message):
                bus._messages.append(item)
            elif isinstance(item, dict):
                bus._messages.append(Message.from_dict(item))
        return bus

    def log_lines(self) -> list[str]:
        """人类可读的一行一条日志（供 `--verbose` 与 `docs/run_log.md`）。"""
        lines: list[str] = []
        for index, message in enumerate(self.messages, start=1):
            detail = f"{message.sender} → {message.recipient}"
            if message.correlation_id:
                detail += f" [corr={message.correlation_id}]"
            try:
                payload = json.dumps(jsonable(message.payload), ensure_ascii=False)
            except (TypeError, ValueError):  # pragma: no cover - jsonable 已兜底
                payload = str(message.payload)
            lines.append(
                f"#{index:03d} {message.created_at or '-'} turn={message.turn} "
                f"{message.kind}: {detail} {truncate(payload, 160)}"
            )
        return lines

    def counts(self) -> dict:
        """各类型消息数量（报告指标卡用）。"""
        result: dict[str, int] = {}
        for message in self.messages:
            result[message.kind] = result.get(message.kind, 0) + 1
        return result

    def __len__(self) -> int:  # pragma: no cover - 便于交互式查看
        return len(self._messages)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MessageBus messages={len(self._messages)} subscribers={sorted(self._subscriptions)}>"


__all__ = [
    "PERF_ID",
    "BROADCAST",
    "Message",
    "Subscription",
    "MessageBus",
]
