"""配置层：.env 解析、运行参数与各类限额。

设计原则（与 Homework1 的 `cra/config.py` 保持一致的取舍）：
    * 零第三方依赖：自己解析极简 `.env`，不引入 python-dotenv；
    * 所有可调参数集中在一处：CLI 参数 > 环境变量 > 默认值；
    * 配置对象可以安全序列化进报告（`public_dict()` 永远不含密钥）。

Homework2 的配置额外关注"流程编排"相关参数：最大回合、最大迭代、审查步数、
并行工作线程数等。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

#: 本项目的安装目录：让 `wfa` 在任意工作目录下都能找到随项目分发的 `.env`。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: `.env` 中与 Workflow Agent 相关的键（用于文档与提示）。
ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "WFA_MODE",
    "WFA_MAX_TURNS",
    "WFA_MAX_ITERATIONS",
    "WFA_LEGACY_CRA_ROOT",
)


def parse_env_file(path: str) -> dict:
    """解析极简 `.env`（`KEY=VALUE`，`#` 为注释）。

    容忍 UTF-8 BOM、值被引号包裹、行尾注释。
    """
    result: dict = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()
            result[key] = value
    return result


def load_dotenv_file(path: str) -> dict:
    """载入指定 `.env` 文件（不覆盖已存在的真实环境变量）。"""
    loaded = parse_env_file(path)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def _env_list(key: str, default: tuple = ()) -> tuple:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass
class Config:
    """一次运行的全部可调参数（模型 + 编排 + 限额 + 输出）。"""

    # ---- LLM ----
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    timeout: float = 60.0
    max_retries: int = 3
    retry_base_delay: float = 1.5
    max_tokens: int = 4096
    transport: str = "http"  # http | sdk | scripted | rule（后两者用于离线/测试）

    # ---- 编排（engine）----
    max_turns: int = 50
    max_iterations: int = 3
    max_nodes: int = 200
    parallel_workers: int = 3
    stop_on_blocking: bool = True
    checkpoint_every: int = 1

    # ---- 各 Agent 限额 ----
    review_max_steps: int = 4
    code_max_files: int = 12
    code_max_bytes: int = 20_000
    doc_sections: int = 5

    # ---- 工作区扫描限额 ----
    max_files: int = 200
    max_file_bytes: int = 200_000
    max_total_bytes: int = 6_000_000
    max_read_lines: int = 400
    max_findings_per_file: int = 30
    max_findings: int = 300

    # ---- 范围过滤 ----
    ignore_dirs: tuple = (
        ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".venv", "venv", "env",
        "node_modules", "dist", "build", "target", "htmlcov", ".wfa", ".dsh",
        "site-packages", ".cache", "vendor", ".wfa_workspace",
    )
    ignore_globs: tuple = (
        "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json",
        "*.pyc", "*.pyo", "*.so", "*.dll", "*.exe", "*.zip", "*.tar", "*.gz",
        "*.whl", "*.jpg", "*.jpeg", "*.png", "*.gif", "*.ico", "*.pdf", "*.mp4",
    )
    follow_symlinks: bool = False

    # ---- 运行模式与输出 ----
    mode: str = "offline"  # offline | online
    write_workspace: bool = True
    verbose: bool = False
    color: bool = True
    language: str = "zh"

    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """从环境变量构造配置；`overrides` 中的非 None 值优先。"""
        # 环境变量里的 Homework1 工程路径塞进 extra，供 LegacyReviewAgent 复用 cra 包。
        # （.env / 环境变量 / --env-file 三条路都会最终进入 os.environ，因此这里读一次即可。）
        extra: dict = {}
        legacy_root = (
            os.environ.get("WFA_LEGACY_CRA_ROOT", "").strip()
            or os.environ.get("WFA_CRA_ROOT", "").strip()
            or os.environ.get("CRA_ROOT", "").strip()
            or os.environ.get("HW1_ROOT", "").strip()
        )
        if legacy_root:
            extra["cra_root"] = legacy_root
        cfg = cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=_env_str("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            model=_env_str("DEEPSEEK_MODEL", DEFAULT_MODEL),
            temperature=_env_float("WFA_TEMPERATURE", 0.2),
            timeout=_env_float("WFA_TIMEOUT", 60.0),
            max_retries=_env_int("WFA_MAX_RETRIES", 3),
            retry_base_delay=_env_float("WFA_RETRY_BASE_DELAY", 1.5),
            max_tokens=_env_int("WFA_MAX_TOKENS", 4096),
            transport=_env_str("WFA_TRANSPORT", "http"),
            max_turns=_env_int("WFA_MAX_TURNS", 50),
            max_iterations=_env_int("WFA_MAX_ITERATIONS", 3),
            max_nodes=_env_int("WFA_MAX_NODES", 200),
            parallel_workers=_env_int("WFA_PARALLEL_WORKERS", 3),
            stop_on_blocking=_env_bool("WFA_STOP_ON_BLOCKING", True),
            review_max_steps=_env_int("WFA_REVIEW_MAX_STEPS", 4),
            max_files=_env_int("WFA_MAX_FILES", 200),
            max_file_bytes=_env_int("WFA_MAX_FILE_BYTES", 200_000),
            mode=_env_str("WFA_MODE", "offline"),
            write_workspace=_env_bool("WFA_WRITE_WORKSPACE", True),
            verbose=_env_bool("WFA_VERBOSE", False),
            color=_env_bool("WFA_COLOR", True),
            ignore_dirs=_env_list("WFA_IGNORE_DIRS", cls.ignore_dirs),
            ignore_globs=_env_list("WFA_IGNORE_GLOBS", cls.ignore_globs),
            extra=extra,
        )
        for key, value in (overrides or {}).items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    # ------------------------------------------------------------------ #
    def resolve_api_key(self) -> str:
        """返回 API Key；缺失时抛出带引导信息的 `ConfigError`。"""
        if self.api_key:
            return self.api_key
        from .errors import ConfigError

        raise ConfigError(
            "未找到 DEEPSEEK_API_KEY。\n"
            "  1) 复制 .env.example 为 .env，填入 https://platform.deepseek.com/api_keys 申请的 Key；\n"
            "  2) 或设置环境变量 DEEPSEEK_API_KEY；\n"
            "  3) 或使用 --offline 模式：以确定性规则 + 脚本化模型跑通整个 Workflow（无需 Key）。"
        )

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def online(self) -> bool:
        return self.mode == "online" and self.has_api_key()

    def public_dict(self) -> dict:
        """用于报告元信息：绝不包含 api_key。"""
        data = asdict(self)
        data.pop("api_key", None)
        return data

    def describe(self) -> str:
        """一行式摘要，用于启动日志。"""
        return (
            f"mode={self.mode} model={self.model} max_turns={self.max_turns} "
            f"max_iterations={self.max_iterations} workspace_writes={'on' if self.write_workspace else 'off'}"
        )


def load_config(root: str | None = None, env_file: str | None = None, **overrides) -> Config:
    """按优先级查找并载入 `.env`，然后构造配置。

    查找顺序（先找到先用）：
        1) 显式指定的 `--env-file`；
        2) `<工作区根目录>/.env`；
        3) 本项目自带的 `.env`（这样在任意目录调用 `run.py` 也能读到密钥）；
        4) 当前工作目录的 `.env`。
    """
    for candidate in _env_candidates(root, env_file):
        if candidate and os.path.isfile(candidate):
            load_dotenv_file(candidate)
            break
    return Config.from_env(**overrides)


def _env_candidates(root: str | None, env_file: str | None) -> list:
    candidates = []
    if env_file:
        candidates.append(env_file)
    if root:
        candidates.append(os.path.join(root, ".env"))
    candidates.append(os.path.join(PROJECT_ROOT, ".env"))
    candidates.append(os.path.join(os.getcwd(), ".env"))
    return candidates


__all__ = [
    "Config",
    "load_config",
    "parse_env_file",
    "load_dotenv_file",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "PROJECT_ROOT",
    "ENV_KEYS",
]
