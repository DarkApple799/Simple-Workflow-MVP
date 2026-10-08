"""通用工具层：JSON 提取、时间与 ID、文本截断、路径安全、文件读写、重试策略。

本模块是 `bus` / `state` / `graph` / `engine` 的公共地基，只依赖标准库与
`wfa.errors`，不引入任何第三方包，也不反向依赖上层模块。

几个值得说明的取舍：

* `extract_json_object` / `extract_json_array` 用"括号配对 + 字符串感知"扫描，
  而不是贪婪正则：模型输出常把 `{` 写进字符串里，正则很容易截错位置。
  同时容忍 ```json 代码块包裹、前后解释性文字、尾随逗号这三类脏数据。
* `safe_join` 是"提示词注入导致读取工作区外文件"的关键防线，见其文档字符串。
* `RetryPolicy` 只对 `LLMError.retryable=True` 的异常重试，其余异常立刻抛出，
  避免把程序 bug（TypeError、KeyError）当成"网络抖动"重试一遍。
"""

from __future__ import annotations

import codecs
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Iterator, TypeVar

from .errors import LLMError, PathSecurityError, ToolError

logger = logging.getLogger("wfa.util")

T = TypeVar("T")

#: 匹配 ```json ... ``` / ``` ... ``` 代码块。
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)

#: 修复尾随逗号：`{"a": 1,}` → `{"a": 1}`。
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


# --------------------------------------------------------------------------- #
# JSON 提取
# --------------------------------------------------------------------------- #
def _iter_balanced(text: str, opener: str, closer: str) -> Iterator[str]:
    """产出文本中所有"括号配对平衡"的片段（忽略双引号字符串内的括号）。

    只把 `"` 当作字符串定界符（JSON 规范如此）。若也把 `'` 当作定界符，
    像 `Here's the result: {"a": 1}` 这类文本会因为撇号而"进入字符串状态"，
    导致后面的花括号被全部忽略——这是 Homework1 里踩过的坑。
    """
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == opener:
            if depth == 0:
                start = index
            depth += 1
        elif char == closer and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start : index + 1]
                start = None


def _repair(candidate: str) -> str:
    """轻量修复：去掉尾随逗号（LLM 最常见的 JSON 语法错误）。"""
    return _TRAILING_COMMA_RE.sub(r"\1", candidate)


def _try_load(candidate: str) -> Any:
    """尝试解析一段文本，失败返回 `_MISS` 哨兵。"""
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        try:
            return json.loads(_repair(candidate))
        except (json.JSONDecodeError, ValueError):
            return _MISS


_MISS = object()


def extract_json_object(text: str) -> dict | None:
    """从自由文本中提取第一个**可解析的 JSON 对象**，失败返回 None。

    依次尝试：整体解析 → ```json 代码块 → 全文括号配对扫描。
    """
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    data = _try_load(stripped)
    if isinstance(data, dict):
        return data

    for block in _FENCE_RE.findall(stripped):
        block = block.strip()
        if not block:
            continue
        data = _try_load(block)
        if isinstance(data, dict):
            return data
        for candidate in _iter_balanced(block, "{", "}"):
            data = _try_load(candidate)
            if isinstance(data, dict):
                return data

    for candidate in _iter_balanced(stripped, "{", "}"):
        data = _try_load(candidate)
        if isinstance(data, dict):
            return data
    return None


def extract_json_array(text: str) -> list | None:
    """从自由文本中提取第一个**可解析的 JSON 数组**，失败返回 None。"""
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    data = _try_load(stripped)
    if isinstance(data, list):
        return data

    for block in list(_FENCE_RE.findall(stripped)) + [stripped]:
        block = block.strip()
        if not block:
            continue
        data = _try_load(block)
        if isinstance(data, list):
            return data
        for candidate in _iter_balanced(block, "[", "]"):
            data = _try_load(candidate)
            if isinstance(data, list):
                return data
    return None


# --------------------------------------------------------------------------- #
# 时间 / ID / 文本
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    """当前本地时间，格式 `"2026-10-08T12:00:00"`（秒级，便于排序与阅读）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id(prefix: str = "id") -> str:
    """生成短随机 id，形如 `"msg-3f9a1c02"`（8 位十六进制，足够避碰）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def truncate(text: str, limit: int = 400, suffix: str = "…") -> str:
    """按字符数截断文本（结果长度不超过 `limit`，超限时以 `suffix` 收尾）。

    `limit` 很小（小于等于 suffix 长度）时只保留 suffix 的前 `limit` 个字符，
    保证"结果长度 ≤ limit"这一不变量始终成立。
    """
    text = "" if text is None else str(text)
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    if len(suffix) >= limit:
        return suffix[:limit]
    return text[: limit - len(suffix)] + suffix


def sha1_text(text: str) -> str:
    """内容指纹：UTF-8 编码后的 SHA-1 前 10 位（用于变更检测/去重）。"""
    text = "" if text is None else str(text)
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]


def jsonable(value: Any) -> Any:
    """把任意对象转换成"可被 `json.dump` 序列化"的纯 Python 结构。

    检查点 / 报告要求纯 JSON（契约第 4 节），但黑板上可能被放进 dataclass、
    set、自定义对象等。这里统一降级：dataclass → dict，set → list，
    带 `to_dict()` 的对象 → 其结果，其余 → `str(value)`。绝不抛异常。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        try:
            return jsonable(converter())
        except Exception:  # pragma: no cover - 降级路径，保证不中断
            logger.debug("to_dict() 失败，降级为 str()：%r", value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return jsonable(dataclasses.asdict(value))
        except Exception:  # pragma: no cover
            pass
    return str(value)


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
def _norm(path: str) -> str:
    """规范化绝对路径：`normcase` 统一大小写与分隔符（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.abspath(path))


def _is_within(root: str, path: str) -> bool:
    """判断 `path` 是否位于 `root` 之内（含 root 自身）。

    用 `normcase` 消除 Windows 的大小写/分隔符差异，用 `commonpath` 做前缀比较
    （而不是字符串 `startswith`，否则 `C:\\ws` 会误判 `C:\\ws-evil` 为子路径）。
    不同盘符时 `commonpath` 抛 ValueError，直接判定为越界。
    """
    root_norm = _norm(root)
    path_norm = _norm(path)
    if root_norm == path_norm:
        return True
    try:
        return os.path.commonpath([root_norm, path_norm]) == root_norm
    except ValueError:  # 不同盘符 / 混合绝对相对
        return False


def safe_relpath(root: str, path: str) -> str:
    """返回相对于 `root` 的路径，统一用正斜杠（跨平台一致，便于写文档与哈希）。"""
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # Windows 上不同盘符无法求相对路径
        return str(path).replace("\\", "/")
    return rel.replace("\\", "/")


def safe_join(root: str, rel: str) -> str:
    """把相对路径解析为工作区内的绝对路径；越界抛 `PathSecurityError`。

    这是防"提示词注入读取工作区外文件"的关键防线：模型可能被待审查代码里的
    注释诱导去读 `~/.ssh/id_rsa` 或 `../../etc/passwd`，这里一律拒绝。

    拒绝的情况包括：空路径、含 NUL 字符、绝对路径、`..` 逃逸、符号链接指向外部、
    以及 Windows 大小写/分隔符混用（`SUB\\..\\..\\out.txt`）。

    规范化：整段路径的首尾空白与首尾引号会被去掉（`" a.txt "` → `a.txt`，
    `'x.py'` → `x.py`），避免用户/AI 粘贴路径时带来的歧义。
    """
    if rel is None or str(rel).strip() == "":
        raise PathSecurityError("路径不能为空。")
    raw = str(rel).strip().strip('"').strip("'")
    if "\x00" in raw:
        raise PathSecurityError("路径包含非法字符 NUL，已拒绝。")

    root_abs = os.path.abspath(root or os.getcwd())
    # 统一分隔符后再判断是否绝对路径，兼容用户在 Windows 上写 "a/b.txt"。
    normalized = raw.replace("\\", os.sep).replace("/", os.sep)

    if os.name == "nt":
        # Windows 会丢弃路径组件结尾的空格与点（`a. ` 与 `a` 指向同一文件），
        # 于是 `.. ` 这种写法在"校验用的路径"与"Win32 实际打开的路径"之间产生歧义。
        # 经验证这种写法在当前系统上只会打开失败、并不构成越界，但为了不留下
        # "校验通过、写入报 FileNotFoundError"的困惑，这里直接判为非法路径。
        for part in normalized.split(os.sep):
            if part in ("", ".", ".."):
                continue
            if part != part.rstrip(". "):
                raise PathSecurityError(
                    f"路径包含 Windows 非法的结尾字符（空格或点），已拒绝：{rel}"
                )

    if os.path.isabs(normalized):
        candidate = os.path.abspath(normalized)
    else:
        candidate = os.path.abspath(os.path.join(root_abs, normalized))

    # realpath 会解析符号链接，防止"工作区内的软链指向外部"绕过检查。
    real_root = os.path.realpath(root_abs)
    real_candidate = os.path.realpath(candidate)
    if not _is_within(real_root, real_candidate):
        raise PathSecurityError(
            f"路径越界，已拒绝访问工作区之外的位置：{rel}（工作区：{root_abs}）"
        )
    return candidate


# --------------------------------------------------------------------------- #
# 文本读写
# --------------------------------------------------------------------------- #
def _decode(raw: bytes) -> str:
    """按 UTF-8 → gb18030 → latin-1 依次尝试解码（后两者兜中文老文件/二进制）。"""
    if raw.startswith(codecs.BOM_UTF8):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def read_text(path: str, *, max_bytes: int = 200_000) -> str:
    """读取文本文件（默认最多 20 万字节，超出部分截断）。

    常见编码都能读：UTF-8（含 BOM）、GB18030，最后 latin-1 兜底不抛异常。
    读取失败抛 `ToolError`（而不是裸 OSError），方便调用方统一处理。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max(0, int(max_bytes)) + 1)
    except OSError as exc:
        raise ToolError(f"读取文件失败：{path}（{exc}）") from exc
    if len(raw) > max_bytes:
        raw = raw[: max(0, int(max_bytes))]
        logger.warning("文件超过 %s 字节，内容已截断：%s", max_bytes, path)
    return _decode(raw)


def write_text(path: str, text: str) -> None:
    """写入文本文件：自动建父目录，UTF-8 **无 BOM**，行尾固定 `\\n`。

    行尾不随平台变化，保证"写入 → 读回 → 指纹"在不同系统上结果一致。
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not isinstance(text, str):
        # 编码 Agent 可能误传 dict/list：不崩溃，但要留下告警，方便定位"文件内容为何是 repr"。
        logger.warning("write_text 收到非字符串内容（%s），将按 str() 写入：%s", type(text).__name__, path)
    payload = text if isinstance(text, str) else str(text)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    except OSError as exc:
        raise ToolError(f"写入文件失败：{path}（{exc}）") from exc


# --------------------------------------------------------------------------- #
# 重试策略
# --------------------------------------------------------------------------- #
class RetryPolicy:
    """指数退避重试策略（只对 `LLMError.retryable=True` 的异常生效）。

    * 其它异常（含不可重试的 LLM 错误、程序 bug）**立刻抛出**，不做无谓等待；
    * 退避时长：`min(base_delay * 2**(第几次失败-1), max_delay)`；
    * `sleep` 可注入（测试里传 `lambda _: None`，就不必真的等）。

    Attributes:
        attempts: 上一次 `run()` 实际调用 `fn` 的次数（含首次）。
        retries: 上一次 `run()` 中真正重试的次数（= attempts - 1）。
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 0.4,
        max_delay: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_retries = max(0, int(max_retries))
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(0.0, float(max_delay))
        self._sleep = sleep
        self._attempts = 0
        self._retries = 0

    # ------------------------------------------------------------------ #
    def delay_for(self, retry_number: int) -> float:
        """第 `retry_number` 次重试（从 1 开始）前应等待的秒数。"""
        if retry_number < 1:
            return 0.0
        return min(self.base_delay * (2 ** (retry_number - 1)), self.max_delay)

    def run(
        self,
        fn: Callable[[], T],
        *,
        on_retry: Callable[[int, Exception], None] | None = None,
    ) -> T:
        """执行 `fn()`，按策略重试；全部失败则抛出最后一个异常。

        Args:
            fn: 无参可调用对象（通常是闭包，用来重放一次 LLM 调用）。
            on_retry: 每次重试前回调 `(本次失败的 attempt 序号, 异常)`，用于打日志。
        """
        self._attempts = 0
        self._retries = 0
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 2):
            self._attempts += 1
            try:
                return fn()
            except LLMError as exc:
                if not getattr(exc, "retryable", False) or attempt > self.max_retries:
                    raise
                last_error = exc
                self._retries += 1
                if on_retry is not None:
                    on_retry(attempt, exc)
                delay = self.delay_for(attempt)
                if delay > 0:
                    self._sleep(delay)

        # 逻辑上不可达：循环内要么 return，要么 raise。
        if last_error is not None:  # pragma: no cover
            raise last_error
        raise RuntimeError("RetryPolicy.run 异常退出")  # pragma: no cover

    # ------------------------------------------------------------------ #
    @property
    def attempts(self) -> int:
        """上一次 `run()` 的调用次数（含首次）。"""
        return self._attempts

    @property
    def retries(self) -> int:
        """上一次 `run()` 的重试次数。"""
        return self._retries


__all__ = [
    "extract_json_object",
    "extract_json_array",
    "now_iso",
    "new_id",
    "truncate",
    "sha1_text",
    "jsonable",
    "safe_relpath",
    "safe_join",
    "read_text",
    "write_text",
    "RetryPolicy",
]
