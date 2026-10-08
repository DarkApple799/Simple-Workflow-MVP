"""从自由文本中稳健地提取 JSON。

LLM 输出 JSON 时常见的三种脏数据：
    1. 包在 ```json 代码块里；
    2. 前后带解释性文字（"好的，以下是结果：{...} 希望有帮助"）；
    3. 括号中夹杂字符串里的花括号。
这里用"括号配对 + 字符串感知"的扫描来解决第 3 点，而不是用贪婪正则。
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _iter_balanced_objects(text: str):
    """产出文本中所有括号配对平衡的 {...} 片段（忽略字符串内的括号）。"""
    start = None
    depth = 0
    in_string = False
    quote = ""
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in ('"', "'"):
            in_string = True
            quote = char
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : index + 1]
                    start = None


def _repair(candidate: str) -> str:
    """轻量修复：去掉尾随逗号。"""
    return re.sub(r",\s*([}\]])", r"\1", candidate)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从文本中提取第一个可解析的 JSON 对象，失败返回 None。"""
    if not text:
        return None
    stripped = text.strip()

    # 1) 整体就是 JSON
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # 2) 优先看代码块
    for block in _FENCE_RE.findall(stripped):
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            for candidate in _iter_balanced_objects(block):
                try:
                    data = json.loads(_repair(candidate))
                    if isinstance(data, dict):
                        return data
                except (json.JSONDecodeError, ValueError):
                    continue

    # 3) 扫描全文中的平衡括号片段
    for candidate in _iter_balanced_objects(stripped):
        try:
            data = json.loads(_repair(candidate))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def extract_json_array(text: str) -> list | None:
    """提取第一个 JSON 数组（用于只要求返回 findings 列表的降级协议）。"""
    if not text:
        return None
    stripped = text.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    for block in _FENCE_RE.findall(stripped) + [stripped]:
        start = block.find("[")
        end = block.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(_repair(block[start : end + 1]))
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, ValueError):
                continue
    return None
