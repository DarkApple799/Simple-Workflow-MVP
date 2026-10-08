"""示例：写法规范、几乎不会被规则命中的对照文件。

用来对比说明——审查工具不应该对好代码"硬凑问题"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class User:
    """用户数据模型。"""

    user_id: int
    name: str
    role: str = "member"
    active: bool = True


def load_users(path: str | Path) -> list[User]:
    """从 JSON 文件读取用户列表。

    Args:
        path: JSON 文件路径，内容应为对象数组。

    Returns:
        用户对象列表；文件不存在时返回空列表。

    Raises:
        ValueError: 文件内容不是数组时抛出。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return []
    with file_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"期望数组，实际是 {type(payload).__name__}")
    return [
        User(
            user_id=int(item["id"]),
            name=str(item["name"]),
            role=str(item.get("role", "member")),
            active=bool(item.get("active", True)),
        )
        for item in payload
    ]


def count_active(users: list[User]) -> int:
    """统计处于激活状态的用户数量。"""
    return sum(1 for user in users if user.active)


def find_admins(users: list[User]) -> list[User]:
    """筛选出管理员用户。"""
    return [user for user in users if user.role == "admin"]


def describe(users: list[User]) -> str:
    """生成一行人类可读的统计描述。"""
    total = len(users)
    active = count_active(users)
    admins = len(find_admins(users))
    return f"用户总数 {total}，其中激活 {active}，管理员 {admins}"
