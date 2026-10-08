"""`wfa/config.py` 单元测试：.env 解析、环境变量优先级与密钥不出配置。

覆盖点：
    1. ``parse_env_file``：注释 / 空行 / 引号包裹 / 行尾注释 / UTF-8 BOM / 非法行 / 文件缺失；
    2. ``load_dotenv_file``：用 ``setdefault`` 载入，**不覆盖**已存在的真实环境变量；
    3. ``Config.from_env``：环境变量生效、显式 overrides 优先、非法值回落到默认、
       bool / list / float / int 各类解析；
    4. ``resolve_api_key`` / ``has_api_key`` / ``online`` 的鉴权语义；
    5. ``public_dict`` / ``describe`` 绝不含 api_key（与报告、日志同一条底线）；
    6. ``load_config`` 的 .env 查找顺序（显式 env_file > 工作区 > 工程目录 > 当前目录）。

全部离线；环境变量改动都用 ``mock.patch.dict`` 隔离并自动还原。
"""

from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock

from tests.helpers import temp_workspace
from wfa.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ENV_KEYS,
    Config,
    load_config,
    load_dotenv_file,
    parse_env_file,
)
from wfa.errors import ConfigError

ALL_ENV_KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "WFA_TEMPERATURE", "WFA_TIMEOUT", "WFA_MAX_RETRIES", "WFA_RETRY_BASE_DELAY",
    "WFA_MAX_TOKENS", "WFA_TRANSPORT", "WFA_MAX_TURNS", "WFA_MAX_ITERATIONS",
    "WFA_MAX_NODES", "WFA_PARALLEL_WORKERS", "WFA_STOP_ON_BLOCKING",
    "WFA_REVIEW_MAX_STEPS", "WFA_MAX_FILES", "WFA_MAX_FILE_BYTES", "WFA_MODE",
    "WFA_WRITE_WORKSPACE", "WFA_VERBOSE", "WFA_COLOR", "WFA_IGNORE_DIRS",
    "WFA_IGNORE_GLOBS",
    # Homework1 适配器根目录（会写进 Config.extra["cra_root"]）
    "WFA_LEGACY_CRA_ROOT", "WFA_CRA_ROOT", "CRA_ROOT", "HW1_ROOT",
)


@contextlib.contextmanager
def clean_env(**extra):
    """临时移除所有相关环境变量（可再传需要设置的键值），退出时原样还原。

    注意必须**删除**而不是置空：``load_dotenv_file`` 用 ``setdefault`` 载入 .env，
    已存在（即使为空字符串）的键不会被覆盖，那样就测不出 .env 生效了。
    """
    saved = {key: os.environ.pop(key, None) for key in ALL_ENV_KEYS}
    try:
        os.environ.update(extra)
        yield
    finally:
        for key in ALL_ENV_KEYS:
            if saved.get(key) is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]
        for key in extra:
            if key not in ALL_ENV_KEYS:
                os.environ.pop(key, None)


class ParseEnvFileTest(unittest.TestCase):
    """.env 解析。"""

    def test_basic_and_comments_and_quotes(self):
        content = (
            "# 这是注释\n"
            "\n"
            "DEEPSEEK_API_KEY=sk-abc123\n"
            "DEEPSEEK_MODEL = deepseek-chat \n"
            "WFA_MAX_TURNS=7\n"
            "QUOTED=\"带 空格 的值\"\n"
            "SINGLE='单引号值'\n"
            "INLINE=value # 行尾注释\n"
            "NO_EQUALS_LINE\n"
        )
        with temp_workspace("cfg-parse") as ws:
            path = os.path.join(ws, ".env")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            parsed = parse_env_file(path)
        self.assertEqual(parsed["DEEPSEEK_API_KEY"], "sk-abc123")
        self.assertEqual(parsed["DEEPSEEK_MODEL"], "deepseek-chat")
        self.assertEqual(parsed["WFA_MAX_TURNS"], "7")
        self.assertEqual(parsed["QUOTED"], "带 空格 的值")
        self.assertEqual(parsed["SINGLE"], "单引号值")
        self.assertEqual(parsed["INLINE"], "value")
        self.assertNotIn("NO_EQUALS_LINE", parsed)
        self.assertNotIn("# 这是注释", parsed)

    def test_utf8_bom_is_tolerated(self):
        with temp_workspace("cfg-bom") as ws:
            path = os.path.join(ws, ".env")
            with open(path, "wb") as handle:
                handle.write("\ufeffWFA_MODE=online\n".encode("utf-8"))
            self.assertEqual(parse_env_file(path), {"WFA_MODE": "online"})

    def test_missing_file_returns_empty(self):
        with temp_workspace("cfg-missing") as ws:
            self.assertEqual(parse_env_file(os.path.join(ws, "nope.env")), {})

    def test_load_dotenv_does_not_override_existing_env(self):
        with temp_workspace("cfg-dotenv") as ws:
            path = os.path.join(ws, ".env")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("WFA_MAX_TURNS=99\nWFA_MODE=online\n")
            with mock.patch.dict(os.environ, {"WFA_MAX_TURNS": "5"}, clear=False):
                loaded = load_dotenv_file(path)
                self.assertEqual(loaded["WFA_MAX_TURNS"], "99")
                self.assertEqual(os.environ["WFA_MAX_TURNS"], "5")     # 真实环境变量优先
                self.assertEqual(os.environ["WFA_MODE"], "online")     # 未设置的键才补上


class ConfigFromEnvTest(unittest.TestCase):
    """环境变量 → Config。"""

    def test_defaults(self):
        with clean_env():
            config = Config.from_env()
        self.assertEqual(config.mode, "offline")
        self.assertEqual(config.base_url, DEFAULT_BASE_URL)
        self.assertEqual(config.model, DEFAULT_MODEL)
        self.assertEqual(config.max_turns, 50)
        self.assertEqual(config.max_iterations, 3)
        self.assertEqual(config.parallel_workers, 3)
        self.assertTrue(config.stop_on_blocking)
        self.assertTrue(config.write_workspace)
        self.assertFalse(config.online)

    def test_env_values_are_honored(self):
        with clean_env(DEEPSEEK_API_KEY="sk-env", DEEPSEEK_MODEL="deepseek-reasoner",
                       WFA_MODE="online", WFA_MAX_TURNS="11", WFA_MAX_ITERATIONS="4",
                       WFA_TEMPERATURE="0.7", WFA_TIMEOUT="12.5", WFA_MAX_RETRIES="1",
                       WFA_RETRY_BASE_DELAY="0.25", WFA_MAX_TOKENS="256",
                       WFA_PARALLEL_WORKERS="5", WFA_STOP_ON_BLOCKING="false",
                       WFA_WRITE_WORKSPACE="0", WFA_REVIEW_MAX_STEPS="6",
                       WFA_TRANSPORT="scripted", WFA_MAX_FILES="9",
                       WFA_MAX_FILE_BYTES="1234", WFA_VERBOSE="yes", WFA_COLOR="off"):
            config = Config.from_env()
        self.assertEqual(config.api_key, "sk-env")
        self.assertEqual(config.model, "deepseek-reasoner")
        self.assertEqual(config.mode, "online")
        self.assertEqual(config.max_turns, 11)
        self.assertEqual(config.max_iterations, 4)
        self.assertAlmostEqual(config.temperature, 0.7)
        self.assertAlmostEqual(config.timeout, 12.5)
        self.assertEqual(config.max_retries, 1)
        self.assertAlmostEqual(config.retry_base_delay, 0.25)
        self.assertEqual(config.max_tokens, 256)
        self.assertEqual(config.parallel_workers, 5)
        self.assertFalse(config.stop_on_blocking)
        self.assertFalse(config.write_workspace)
        self.assertEqual(config.review_max_steps, 6)
        self.assertEqual(config.transport, "scripted")
        self.assertEqual(config.max_files, 9)
        self.assertEqual(config.max_file_bytes, 1234)
        self.assertTrue(config.verbose)
        self.assertFalse(config.color)

    def test_overrides_win_and_none_is_ignored(self):
        with clean_env(WFA_MAX_TURNS="3"):
            config = Config.from_env(max_turns=9, mode="online", unknown_key="x",
                                     verbose=None)
        self.assertEqual(config.max_turns, 9)
        self.assertEqual(config.mode, "online")
        self.assertFalse(config.verbose)
        self.assertFalse(hasattr(config, "unknown_key"))

    def test_invalid_numbers_fall_back_to_defaults(self):
        with clean_env(WFA_MAX_TURNS="不是数字", WFA_TEMPERATURE="abc", WFA_TIMEOUT="",
                       WFA_MAX_FILES="x"):
            config = Config.from_env()
        self.assertEqual(config.max_turns, 50)
        self.assertAlmostEqual(config.temperature, 0.2)
        self.assertAlmostEqual(config.timeout, 60.0)
        self.assertEqual(config.max_files, 200)

    def test_list_env_is_split(self):
        with clean_env(WFA_IGNORE_DIRS=".git, build ,node_modules",
                       WFA_IGNORE_GLOBS="*.min.js,*.map"):
            config = Config.from_env()
        self.assertEqual(config.ignore_dirs, (".git", "build", "node_modules"))
        self.assertEqual(config.ignore_globs, ("*.min.js", "*.map"))

    def test_list_env_default_when_empty(self):
        with clean_env():
            config = Config.from_env()
        self.assertIn(".git", config.ignore_dirs)
        self.assertIn("__pycache__", config.ignore_dirs)

    def test_boolean_env_variants(self):
        for raw, expected in (("1", True), ("true", True), ("yes", True), ("on", True),
                              ("y", True), ("0", False), ("no", False), ("", True)):
            with self.subTest(raw=raw):
                with clean_env(WFA_STOP_ON_BLOCKING=raw):
                    self.assertEqual(Config.from_env().stop_on_blocking, expected)

    def test_env_keys_constant(self):
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "WFA_MODE",
                    "WFA_MAX_TURNS", "WFA_MAX_ITERATIONS"):
            self.assertIn(key, ENV_KEYS)


class LegacyCraRootEnvTest(unittest.TestCase):
    """Homework1 适配器根目录：环境变量 → ``Config.extra["cra_root"]``。"""

    def test_legacy_root_lands_in_extra(self):
        with temp_workspace("cfg-legacy-root") as ws:
            with clean_env(WFA_LEGACY_CRA_ROOT=ws):
                config = Config.from_env()
        self.assertEqual(config.extra.get("cra_root"), ws)

    def test_priority_order(self):
        with clean_env(WFA_CRA_ROOT="B", CRA_ROOT="C", HW1_ROOT="D"):
            self.assertEqual(Config.from_env().extra["cra_root"], "B")
        with clean_env(CRA_ROOT="C", HW1_ROOT="D"):
            self.assertEqual(Config.from_env().extra["cra_root"], "C")
        with clean_env(HW1_ROOT="D"):
            self.assertEqual(Config.from_env().extra["cra_root"], "D")

    def test_no_legacy_root_means_no_extra_key(self):
        with clean_env():
            config = Config.from_env()
        self.assertNotIn("cra_root", config.extra)

    def test_whitespace_only_value_is_ignored(self):
        with clean_env(WFA_LEGACY_CRA_ROOT="   "):
            self.assertNotIn("cra_root", Config.from_env().extra)


class ApiKeyAndPublicDictTest(unittest.TestCase):
    """密钥语义与安全序列化。"""

    SECRET = "sk-super-secret-config-value"

    def test_resolve_api_key(self):
        self.assertEqual(Config(api_key=self.SECRET).resolve_api_key(), self.SECRET)
        self.assertTrue(Config(api_key=self.SECRET).has_api_key())
        with self.assertRaises(ConfigError) as ctx:
            Config(api_key="").resolve_api_key()
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))
        self.assertFalse(Config(api_key="").has_api_key())

    def test_online_requires_key_and_mode(self):
        self.assertTrue(Config(api_key=self.SECRET, mode="online").online)
        self.assertFalse(Config(api_key=self.SECRET, mode="offline").online)
        self.assertFalse(Config(api_key="", mode="online").online)

    def test_public_dict_and_describe_hide_key(self):
        config = Config(api_key=self.SECRET, mode="online")
        self.assertNotIn("api_key", config.public_dict())
        self.assertNotIn(self.SECRET, str(config.public_dict()))
        self.assertNotIn(self.SECRET, config.describe())
        self.assertIn("mode=online", config.describe())
        self.assertIn("max_turns=", config.describe())

    def test_public_dict_keeps_non_secret_fields(self):
        public = Config(api_key=self.SECRET, model="deepseek-chat").public_dict()
        self.assertEqual(public["model"], "deepseek-chat")
        self.assertEqual(public["max_turns"], 50)


class LoadConfigTest(unittest.TestCase):
    """``load_config`` 的 .env 查找顺序。"""

    def test_explicit_env_file_wins(self):
        with temp_workspace("cfg-load") as ws:
            explicit = os.path.join(ws, "explicit.env")
            root_env = os.path.join(ws, "root", ".env")
            os.makedirs(os.path.dirname(root_env), exist_ok=True)
            with open(explicit, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("WFA_MAX_TURNS=21\n")
            with open(root_env, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("WFA_MAX_TURNS=31\n")
            with clean_env():
                config = load_config(root=os.path.dirname(root_env), env_file=explicit)
        self.assertEqual(config.max_turns, 21)

    def test_root_env_is_used(self):
        with temp_workspace("cfg-root") as ws:
            with open(os.path.join(ws, ".env"), "w", encoding="utf-8", newline="\n") as handle:
                handle.write("WFA_MAX_ITERATIONS=8\nWFA_MODE=online\n")
            with clean_env():
                config = load_config(root=ws)
        self.assertEqual(config.max_iterations, 8)
        self.assertEqual(config.mode, "online")

    def test_overrides_beat_env_file(self):
        with temp_workspace("cfg-override") as ws:
            with open(os.path.join(ws, ".env"), "w", encoding="utf-8", newline="\n") as handle:
                handle.write("WFA_MAX_TURNS=17\n")
            with clean_env():
                config = load_config(root=ws, max_turns=3)
        self.assertEqual(config.max_turns, 3)

    def test_missing_env_file_is_fine(self):
        with temp_workspace("cfg-noenv") as ws:
            with clean_env():
                config = load_config(root=ws, env_file=os.path.join(ws, "nope.env"))
        self.assertEqual(config.mode, "offline")
        self.assertEqual(config.max_turns, 50)


if __name__ == "__main__":
    unittest.main()
