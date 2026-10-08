"""文件系统工具：路径安全、编码探测、二进制识别、目录遍历。

这里集中处理"真实世界的脏数据"——边界情况几乎都发生在这一层：
二进制文件、超大文件、空文件、GBK 编码、BOM、软链接死循环、路径越界……
"""

from __future__ import annotations

import codecs
import fnmatch
import os
from typing import Iterable

from .config import CODE_EXTENSIONS, Config, TEXT_EXTENSIONS
from .errors import ConfigError, PathSecurityError
from .models import FileInfo

#: 按优先级尝试的编码；latin-1 兜底（永不失败，但可能乱码）。
#: 注意：先试 utf-8 再试 utf-8-sig —— 顺序反了会把普通 UTF-8 文件误报成"带 BOM"。
_ENCODING_CANDIDATES = ("utf-8", "gb18030", "big5", "cp1252")

#: 探测二进制时读取的字节数。
_SNIFF_BYTES = 8192

#: 无扩展名但明确是文本的文件。
_TEXT_FILENAMES = frozenset({
    "makefile", "dockerfile", "license", "readme", "changelog", "gemfile",
    "procfile", "rakefile", "jenkinsfile", ".env", ".gitignore", ".dockerignore",
})


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def ensure_root(root: str) -> str:
    """校验并规范化审查根目录（也接受单个文件，返回其父目录）。"""
    if not root:
        raise ConfigError("必须提供要审查的路径。")
    path = os.path.abspath(os.path.expanduser(root))
    if not os.path.exists(path):
        raise ConfigError(f"路径不存在：{path}")
    if os.path.isfile(path):
        return os.path.dirname(path) or os.getcwd()
    return path


def safe_path(root: str, user_path: str, *, must_exist: bool = True) -> str:
    """把用户/AI 给出的相对路径解析为绝对路径，并确保没有越出工作区。

    这是防"提示词注入导致读取工作区外文件"的关键防线：
    模型可能被待审查代码里的注释诱导去读 ~/.ssh/id_rsa，这里直接拒绝。
    """
    if user_path is None or str(user_path).strip() == "":
        raise PathSecurityError("路径不能为空。")
    raw = str(user_path).strip().strip('"').strip("'")
    if os.path.isabs(raw):
        candidate = os.path.abspath(raw)
    else:
        candidate = os.path.abspath(os.path.join(root, raw))
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath([real_root, real_candidate]) == real_root
    except ValueError:  # 不同盘符
        inside = False
    if not inside:
        # 报错时顺带给出"该怎么办"：很多人是在 chat 里指定了一个工作区外的文件，
        # 只需要退出对话、改用 review 子命令直接把它作为参数即可。
        raise PathSecurityError(
            f"路径越界，已拒绝访问工作区之外的文件：{user_path}"
            f"（本次工作区是 {root}；若要审查该文件，请退出对话后在系统提示符下执行："
            f'python review.py "{candidate}"）'
        )
    if must_exist and not os.path.exists(real_candidate):
        raise PathSecurityError(f"文件不存在：{user_path}")
    return candidate


def relpath(root: str, path: str) -> str:
    """返回统一使用 / 分隔的相对路径。"""
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


def human_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


# --------------------------------------------------------------------------- #
# 文本与编码
# --------------------------------------------------------------------------- #
def looks_binary(data: bytes) -> bool:
    """判断一段字节是否为二进制：含 NUL，或不可解码的控制字符占比过高。"""
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:_SNIFF_BYTES]
    control = sum(
        1 for byte in sample if byte < 9 or (13 < byte < 32) or byte == 127
    )
    return control / max(len(sample), 1) > 0.30


def decode_bytes(data: bytes) -> tuple[str, str, bool]:
    """按候选编码解码，返回 (文本, 编码名, 是否有损)。

    显式处理 UTF-8 BOM：只有真的带 BOM 才报告 utf-8-sig，
    否则普通 ASCII/UTF-8 文件会被误标成 "utf-8-sig"。
    """
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig(BOM)", False
        except UnicodeDecodeError:
            pass
    for encoding in _ENCODING_CANDIDATES:
        try:
            return data.decode(encoding), encoding, False
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1(replace)", True


def read_text_file(path: str, *, max_bytes: int) -> tuple[str, str, bool, bool]:
    """读取文本文件，返回 (文本, 编码, 是否有损, 是否被截断)。

    超过 max_bytes 时只读前 max_bytes 字节，并明确告知调用方"被截断了"，
    避免把半个文件当成完整内容交给模型。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = size > max_bytes or len(raw) > max_bytes
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    text, encoding, lossy = decode_bytes(raw)
    return text, encoding, lossy, truncated


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def detect_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return CODE_EXTENSIONS.get(ext, "Text" if ext in TEXT_EXTENSIONS else "unknown")


def classify_file(path: str, cfg: Config) -> FileInfo:
    """判断单个文件的类型与可读性，不读取全文（只嗅探文件头）。"""
    rel = path.replace("\\", "/")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return FileInfo(path=rel, size=0, kind="unreadable", note=f"stat 失败：{exc}")

    if size == 0:
        return FileInfo(path=rel, size=0, lines=0, kind="empty", note="空文件")

    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path).lower()
    language = detect_language(path)

    if size > cfg.max_file_bytes:
        return FileInfo(
            path=rel, size=size, kind="too_large", language=language,
            note=f"超过单文件上限 {human_size(cfg.max_file_bytes)}，仅登记不分析",
        )

    if ext in CODE_EXTENSIONS or ext in TEXT_EXTENSIONS or name in _TEXT_FILENAMES:
        kind = "python" if ext in (".py", ".pyi") else ("code" if ext in CODE_EXTENSIONS else "text")
    else:
        # 未知扩展名：嗅探文件头，文本则按 text 处理，否则算二进制
        try:
            with open(path, "rb") as handle:
                head = handle.read(_SNIFF_BYTES)
        except OSError as exc:
            return FileInfo(path=rel, size=size, kind="unreadable", note=f"读取失败：{exc}")
        if looks_binary(head):
            return FileInfo(path=rel, size=size, kind="binary", language="binary", note="二进制文件，跳过")
        kind = "text"

    note = ""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_SNIFF_BYTES)
        if looks_binary(head):
            kind, note = "binary", "内容疑似二进制，跳过"
    except OSError:
        pass
    return FileInfo(path=rel, size=size, kind=kind, language=language, note=note)


def count_lines(path: str) -> int:
    """统计文件行数（二进制安全：按字节流计数）。"""
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# 遍历
# --------------------------------------------------------------------------- #
def _match_any(patterns: Iterable[str], *candidates: str) -> bool:
    for pattern in patterns:
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern):
                return True
            if fnmatch.fnmatch(candidate.lower(), pattern.lower()):
                return True
    return False


def should_skip_dir(name: str, rel_dir: str, cfg: Config) -> bool:
    """判断目录是否应跳过（内置忽略名单 + 用户 --exclude）。"""
    if name.lower() in {item.lower() for item in cfg.ignore_dirs}:
        return True
    return _match_any(cfg.exclude, rel_dir + "/", name)


def should_skip_file(name: str, rel_path: str, cfg: Config) -> str | None:
    """返回跳过原因，None 表示不跳过。"""
    if _match_any(cfg.ignore_globs, name, rel_path):
        return "命中忽略规则"
    if cfg.exclude and _match_any(cfg.exclude, rel_path, name):
        return "命中 --exclude"
    if cfg.include and not _match_any(cfg.include, rel_path, name):
        return "不匹配 --include"
    return None


def walk_files(
    root: str, cfg: Config
) -> tuple[list[str], list[dict], int, bool]:
    """遍历工作区，返回 (文件绝对路径列表, 跳过记录, 累计字节, 是否因限额截断)。

    特性：结果排序稳定；默认不跟随软链接（防死循环）；超过 max_files 立即停止并标记截断。
    """
    collected: list[str] = []
    skipped: list[dict] = []
    total_bytes = 0
    truncated = False
    seen_dirs: set[str] = set()

    for current, dir_names, file_names in os.walk(root, followlinks=cfg.follow_symlinks):
        real_current = os.path.realpath(current)
        if real_current in seen_dirs:
            dir_names[:] = []
            continue
        seen_dirs.add(real_current)

        rel_dir = relpath(root, current)
        rel_dir = "" if rel_dir == "." else rel_dir
        dir_names.sort()
        file_names.sort()

        kept_dirs = []
        for name in dir_names:
            candidate = f"{rel_dir}/{name}" if rel_dir else name
            if os.path.islink(os.path.join(current, name)) and not cfg.follow_symlinks:
                skipped.append({"path": candidate + "/", "reason": "软链接目录，默认不跟随"})
                continue
            if should_skip_dir(name, candidate, cfg):
                skipped.append({"path": candidate + "/", "reason": "忽略目录/依赖缓存"})
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs

        for name in file_names:
            full = os.path.join(current, name)
            rel = relpath(root, full)
            reason = should_skip_file(name, rel, cfg)
            if reason:
                skipped.append({"path": rel, "reason": reason})
                continue
            if os.path.islink(full) and not cfg.follow_symlinks:
                skipped.append({"path": rel, "reason": "软链接文件，默认不跟随"})
                continue
            if len(collected) >= cfg.max_files:
                truncated = True
                break
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                skipped.append({"path": rel, "reason": f"stat 失败：{exc}"})
                continue
            collected.append(full)
            total_bytes += size
            if total_bytes >= cfg.max_total_bytes:
                truncated = True
                break
        if truncated:
            break

    return collected, skipped, total_bytes, truncated
