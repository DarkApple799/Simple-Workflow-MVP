"""上下文记忆：会话消息、长度预算裁剪、会话持久化。

两层记忆：
    1. **短期记忆**（Memory.messages）：当前会话的完整消息序列（含工具调用与观察结果），
       超出字符预算时从最旧的消息开始裁剪，并保证不会把 `tool` 消息与其所属的
       `assistant(tool_calls)` 拆散——拆散会导致多数 OpenAI 兼容接口直接报错。
    2. **长期记忆**（SessionStore）：把会话落盘到 .cra_sessions/*.json，
       `chat --session <id>` 可继续上一次对话，这就是作业要求的"支持上下文记忆"。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

SYSTEM_ROLES = ("system", "developer")


@dataclass
class Memory:
    """消息序列 + 长度预算管理。"""

    system_prompt: str = ""
    max_chars: int = 24000
    keep_recent: int = 12
    messages: list[dict] = field(default_factory=list)
    trimmed_count: int = 0

    def __post_init__(self) -> None:
        if self.system_prompt and not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})

    # ------------------------------------------------------------------ #
    def add_user(self, content: str) -> None:
        self._append({"role": "user", "content": content})

    def add_assistant(self, content: str, tool_calls: list[dict] | None = None) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._append(message)

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self._append({"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content})

    def add_assistant_raw(self, message: dict) -> None:
        self._append(dict(message))

    def _append(self, message: dict) -> None:
        self.messages.append(message)
        self.trim()

    # ------------------------------------------------------------------ #
    def char_count(self) -> int:
        total = 0
        for message in self.messages:
            content = message.get("content")
            if isinstance(content, str):
                total += len(content)
            for call in message.get("tool_calls") or []:
                total += len(json.dumps(call.get("function", {}), ensure_ascii=False))
        return total

    def trim(self) -> None:
        """按字符预算裁剪历史，保留 system 与最近若干轮。"""
        if self.char_count() <= self.max_chars:
            return
        protect = 1 if self.messages and self.messages[0].get("role") in SYSTEM_ROLES else 0
        head = self.messages[:protect]
        tail = self.messages[protect:]
        while tail and len(tail) > self.keep_recent and _chars(head + tail) > self.max_chars:
            tail.pop(0)
            self.trimmed_count += 1
        # 裁剪后开头不能是孤儿 tool 消息
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
            self.trimmed_count += 1
        # 若仍然超预算（工具输出本身很长），直接截断最后一条的正文
        while tail and _chars(head + tail) > self.max_chars and len(tail[-1].get("content", "")) > 200:
            tail[-1]["content"] = tail[-1]["content"][: len(tail[-1]["content"]) // 2] + "\n...（内容已截断以节省上下文）"
        self.messages = head + tail

    def to_messages(self) -> list[dict]:
        self.trim()
        return list(self.messages)

    def reset(self, system_prompt: str | None = None) -> None:
        self.messages = []
        if system_prompt or self.system_prompt:
            self.system_prompt = system_prompt or self.system_prompt
            self.messages.append({"role": "system", "content": self.system_prompt})

    def restore_history(self, previous_messages: list[dict] | None) -> int:
        """把上一次会话的文本消息并回记忆，返回恢复的条数。

        设计要点：用历史消息**替换**（而不是追加）当前内容，只保留本轮新的 system 提示词。
        早期实现是直接 append，结果每恢复一次会话就把"代码库概况"重复插一份，
        白白多占约 2500 字符上下文——这是对照落盘文件时才发现的。
        """
        restored = [
            dict(message)
            for message in (previous_messages or [])
            if message.get("role") in ("user", "assistant") and not message.get("tool_calls")
        ]
        if not restored:
            return 0
        head: list[dict] = []
        if self.messages and self.messages[0].get("role") in SYSTEM_ROLES:
            head = [self.messages[0]]
        self.messages = head + restored
        return len(restored)


def _chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in message.get("tool_calls") or []:
            total += len(json.dumps(call.get("function", {}), ensure_ascii=False))
    return total


# --------------------------------------------------------------------------- #
class SessionStore:
    """会话持久化（长期记忆）。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory

    def new_id(self, prefix: str = "session") -> str:
        return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

    def path_for(self, session_id: str) -> str:
        return os.path.join(self.directory, f"{session_id}.json")

    def save(self, session_id: str, *, messages: list[dict], meta: dict) -> str:
        os.makedirs(self.directory, exist_ok=True)
        payload = {
            "session_id": session_id,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "meta": meta,
            "messages": messages,
        }
        path = self.path_for(session_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def load(self, session_id: str) -> dict | None:
        path = self.path_for(session_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return json.load(handle)

    def list_sessions(self) -> list[str]:
        if not os.path.isdir(self.directory):
            return []
        return sorted(
            name[:-5] for name in os.listdir(self.directory) if name.endswith(".json")
        )
