"""配置层：.env 解析、运行参数、忽略规则与各类限额。

所有可调参数集中在这里，CLI 参数会覆盖环境变量，环境变量再覆盖默认值。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

#: 本项目的安装目录：用于在任何工作目录下都能找到随项目分发的 .env
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 默认忽略的目录：版本控制元数据、依赖、缓存、构建产物、虚拟环境。
DEFAULT_IGNORE_DIRS: tuple[str, ...] = (
    ".git", ".hg", ".svn", ".idea", ".vscode", ".vs", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".venv", "venv",
    "env", "node_modules", "bower_components", "dist", "build", "target",
    "out", ".next", ".nuxt", "coverage", "htmlcov", ".cra_sessions", ".dsh",
    "site-packages", ".cache", "vendor",
)

#: 默认忽略的文件（压缩包、二进制、锁文件、压缩后的前端产物）。
DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json",
    "*.pyc", "*.pyo", "*.so", "*.dll", "*.exe", "*.zip", "*.tar", "*.gz",
    "*.whl", "*.jpg", "*.jpeg", "*.png", "*.gif", "*.ico", "*.pdf", "*.mp4",
)

#: 被当作"源码"参与审查的扩展名。
CODE_EXTENSIONS: dict[str, str] = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".go": "Go",
    ".rs": "Rust", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".kt": "Kotlin", ".swift": "Swift",
    ".scala": "Scala", ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".lua": "Lua", ".pl": "Perl", ".r": "R", ".vue": "Vue",
    ".svelte": "Svelte", ".m": "Objective-C",
}

#: 文档/配置类文本：可以被读取与检索，但不做 AST 分析。
TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".scss", ".xml", ".csv", ".env", ".properties", ".gradle",
})

#: 忽略规则里不区分大小写比较的目录名。
_BUILTIN_ENV_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")


def parse_env_file(path: str) -> dict[str, str]:
    """解析极简 .env 文件（KEY=VALUE，# 为注释）。

    刻意不引入 python-dotenv：本项目追求零依赖，而 .env 语法本身足够简单。
    会容忍 UTF-8 BOM、引号包裹、行尾注释。
    """
    result: dict[str, str] = {}
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
            # 去掉引号与行尾注释（仅当值未被引号包裹时）
            if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()
            result[key] = value
    return result


def load_env(root: str | None = None, filename: str = ".env") -> dict[str, str]:
    """把 <root>/.env 载入 os.environ（不覆盖已存在的真实环境变量）。"""
    env_path = os.path.join(root, filename) if root else filename
    loaded = parse_env_file(env_path)
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


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


@dataclass
class Config:
    """一次运行的全部可调参数。"""

    # ---- LLM ----
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    timeout: float = 60.0
    max_retries: int = 3
    retry_base_delay: float = 1.5
    max_tokens: int = 4096
    transport: str = "http"  # http | sdk | fake（fake 仅测试用）

    # ---- Agent ----
    max_steps: int = 8
    offline: bool = False
    focus: tuple[str, ...] = ()
    language: str = "zh"

    # ---- 扫描限额（边界处理的核心参数）----
    max_files: int = 200
    max_file_bytes: int = 200_000
    max_total_bytes: int = 6_000_000
    max_read_lines: int = 400
    max_findings_per_file: int = 30
    max_findings: int = 300

    # ---- 范围过滤 ----
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    ignore_dirs: tuple[str, ...] = DEFAULT_IGNORE_DIRS
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS
    follow_symlinks: bool = False

    # ---- 输出 ----
    verbose: bool = False
    color: bool = True
    trace: bool = True

    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """从环境变量构造配置，overrides 中非 None 的值优先。"""
        cfg = cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=_env_str("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            model=_env_str("DEEPSEEK_MODEL", DEFAULT_MODEL),
            temperature=float(_env_str("CRA_TEMPERATURE", "0.2")),
            timeout=float(_env_str("CRA_TIMEOUT", "60")),
            max_retries=_env_int("CRA_MAX_RETRIES", 3),
            max_tokens=_env_int("CRA_MAX_TOKENS", 4096),
            max_steps=_env_int("CRA_MAX_STEPS", 8),
            transport=_env_str("CRA_TRANSPORT", "http"),
            offline=_env_bool("CRA_OFFLINE", False),
            max_files=_env_int("CRA_MAX_FILES", 200),
            max_file_bytes=_env_int("CRA_MAX_FILE_BYTES", 200_000),
            verbose=_env_bool("CRA_VERBOSE", False),
            color=_env_bool("CRA_COLOR", True),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    # ------------------------------------------------------------------ #
    def resolve_api_key(self) -> str:
        """返回 API Key；缺失时抛出带引导信息的 ConfigError。"""
        if self.api_key:
            return self.api_key
        from .errors import ConfigError

        raise ConfigError(
            "未找到 DEEPSEEK_API_KEY。\n"
            "  1) 复制 .env.example 为 .env，填入 https://platform.deepseek.com/api_keys 申请的 Key；\n"
            "  2) 或设置环境变量 DEEPSEEK_API_KEY；\n"
            "  3) 或加 --offline 参数，仅使用确定性静态规则做审查（无需 Key）。"
        )

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> dict:
        """用于报告元信息：绝不包含 api_key。"""
        data = asdict(self)
        data.pop("api_key", None)
        return data

    def describe_scope(self) -> str:
        parts = []
        if self.include:
            parts.append("include=" + ",".join(self.include))
        if self.exclude:
            parts.append("exclude=" + ",".join(self.exclude))
        return "；".join(parts) if parts else "全部源码文件（自动忽略依赖与构建目录）"


def load_config(root: str | None = None, env_file: str | None = None, **overrides) -> Config:
    """按优先级查找并载入 .env，然后构造配置。

    查找顺序（先找到先用）：
        1) 显式指定的 --env-file；
        2) <审查根目录>/.env；
        3) 本项目自带的 .env（这样在任意目录调用 review.py 也能读到密钥）；
        4) 当前工作目录的 .env。
    """
    for candidate in _env_candidates(root, env_file):
        if candidate and os.path.isfile(candidate):
            load_dotenv_file(candidate)
            break
    return Config.from_env(**overrides)


def _env_candidates(root: str | None, env_file: str | None) -> list[str]:
    candidates: list[str] = []
    if env_file:
        candidates.append(env_file)
    if root:
        candidates.append(os.path.join(root, ".env"))
    candidates.append(os.path.join(PROJECT_ROOT, ".env"))
    candidates.append(os.path.join(os.getcwd(), ".env"))
    return candidates


def load_dotenv_file(path: str) -> dict[str, str]:
    """载入指定 .env 文件（不覆盖已存在的真实环境变量）。"""
    loaded = parse_env_file(path)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded
