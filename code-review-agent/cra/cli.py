"""命令行入口：review / scan / chat / web 四个子命令。

设计取向：默认输出人类友好的中文进度与结果，同时保证退出码语义清晰，
便于直接放进 CI（例如发现 critical 问题就返回非零码，见 --fail-on）。
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .agent import CodeReviewAgent, build_chat_agent
from .config import Config, load_config
from .errors import CodeReviewAgentError, ConfigError, LLMAuthError, LLMError
from .fsutil import ensure_root
from .memory import SessionStore
from .models import Finding, SEVERITY_LABEL_ZH
from .report import render_json, render_markdown, write_json, write_report
from .scanner import scan_workspace

#: 关注点选项
FOCUS_CHOICES = ("bug", "security", "performance", "style", "maintainability", "testing", "documentation")
FOCUS_LABEL_ZH = {
    "bug": "缺陷",
    "security": "安全",
    "performance": "性能",
    "style": "风格",
    "maintainability": "可维护性",
    "testing": "测试",
    "documentation": "文档",
}

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_AUTH = 2
EXIT_DEGRADED = 3


# --------------------------------------------------------------------------- #
# 控制台小工具
# --------------------------------------------------------------------------- #
class Console:
    """极简终端输出（Windows 下也安全，必要时自动关闭颜色）。"""

    COLORS = {
        "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "cyan": "\033[36m",
    }
    SEVERITY_COLOR = {
        "critical": "red", "high": "red", "medium": "yellow",
        "low": "cyan", "info": "dim",
    }

    def __init__(self, color: bool = True, quiet: bool = False) -> None:
        self.quiet = quiet
        self.color = color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    def _wrap(self, text: str, color: str | None) -> str:
        if not self.color or not color:
            return text
        return f"{self.COLORS.get(color, '')}{text}{self.COLORS['reset']}"

    def out(self, text: str = "") -> None:
        if not self.quiet:
            # flush：把输出重定向到日志/管道时也要立刻可见（否则会攒到缓冲区才出现）
            print(text, flush=True)

    def info(self, text: str) -> None:
        self.out(text)

    def step(self, text: str) -> None:
        self.out(self._wrap(text, "blue"))

    def ok(self, text: str) -> None:
        self.out(self._wrap("✓ " + text, "green"))

    def warn(self, text: str) -> None:
        self.out(self._wrap("! " + text, "yellow"))

    def error(self, text: str) -> None:
        print(self._wrap("✗ " + text, "red"), file=sys.stderr)

    def severity(self, text: str, severity: str) -> str:
        return self._wrap(text, self.SEVERITY_COLOR.get(severity))

    def table_row(self, finding: Finding) -> str:
        location = f"{finding.file}:{finding.line}" if finding.line else finding.file
        tag = SEVERITY_LABEL_ZH.get(finding.severity, finding.severity)
        return f"  {self.severity(f'[{tag}]', finding.severity)} {location}  {finding.title}"


def setup_console_encoding() -> None:
    """Windows 控制台默认 GBK，中文/emoji 会报 UnicodeEncodeError，这里统一为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 极少数环境不支持
                pass


def looks_like_shell_command(text: str) -> bool:
    """判断用户是不是把「系统命令」粘进了 chat 的对话输入框。

    典型误用：在 `你 >` 提示符下粘贴 `python review.py examples --out r.md`，
    结果被当成问题发给模型，白烧一堆 token 还答非所问。
    这里只匹配本项目入口和明显是 shell 的动词，避免误伤
    （例如"python 的 GIL 是什么"这类正常提问不会被拦）。
    """
    stripped = (text or "").lstrip("\ufeff").strip().lstrip(">").strip()
    lowered = stripped.lower()
    # 只匹配"确实是 shell 调用"的形式；故意不含裸的 "review.py "、 "webui.py "，
    # 否则"review.py 是怎么组织的"这类正常提问会被误拦。
    prefixes = (
        "python review.py", "python webui.py", "python -m cra", "python.exe review.py",
        "py review.py", "py webui.py", "./review.py", "./webui.py",
        "cd ", "cd/", "git ", "pip ", "pip3 ", "taskkill ",
        "start-web.cmd", "review.cmd",
    )
    return lowered.startswith(prefixes)


def make_progress(console: Console):
    """把 Agent 的进度事件翻译成终端输出（工具调用、步数、降级提示）。"""

    def hook(event: str, payload: dict) -> None:
        if event == "scan_done":
            console.info(f"  预扫描完成：{payload.get('files')} 个文件，规则命中 {payload.get('findings')} 条")
        elif event == "step":
            console.step(f"  [第 {payload.get('step')}/{payload.get('max_steps')} 步 · {payload.get('protocol')}]")
        elif event == "tool_call":
            args = ", ".join(f"{k}={v}" for k, v in (payload.get("arguments") or {}).items())
            console.info(f"    → 调用工具 {payload.get('tool')}({args[:160]})")
        elif event == "tool_result":
            mark = "成功" if payload.get("ok") else "失败"
            console.info(f"      ← {mark}：{str(payload.get('preview', ''))[:120]}")
        elif event == "protocol_switch":
            console.warn(f"  模型不支持工具调用，已切换为 JSON 协议（{payload.get('reason')}）")
        elif event == "degraded":
            console.warn(f"  已降级为规则报告：{payload.get('reason')}")

    return hook


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _port(value: str) -> int:
    """argparse 类型：把端口限制在 1~65535，非法值给出干净的错误提示而不是堆栈。"""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"端口必须是整数，收到 {value!r}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"端口必须在 1~65535 之间，收到 {port}")
    return port


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（review / scan / chat / web 四个子命令）。"""
    parser = argparse.ArgumentParser(
        prog="cra",
        description="Code Review Agent —— 基于 LLM 的代码审查助手（Agent 循环 + 工具调用 + 上下文记忆）",
        epilog=(
            "示例：\n"
            "  python review.py examples --out report.md        # 审查目录，输出 Markdown 报告\n"
            "  python review.py . --focus security --max-steps 6 # 只看安全，限制 6 步\n"
            "  python review.py . --offline                     # 不联网，仅确定性规则\n"
            "  python review.py chat examples                   # 交互式问答（带记忆）\n"
            "  python review.py web --port 8765                 # 打开轻量 Web 界面\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"Code Review Agent {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser) -> None:
        # --version 同时挂在各子命令上，这样 `python review.py --version`（会被补成 review --version）也能用
        p.add_argument("--version", action="version", version=f"Code Review Agent {__version__}")
        p.add_argument("--verbose", action="store_true", help="打印调试日志")
        p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
        p.add_argument("--quiet", action="store_true", help="只输出最终结果")
        p.add_argument("--env-file", default=".env", help="密钥配置文件，默认 .env")

    # -- review --
    review = sub.add_parser("review", help="审查目录或文件并生成报告")
    review.add_argument("path", nargs="?", default=".", help="要审查的目录或文件，默认当前目录")
    review.add_argument("--out", default="code_review_report.md", help="Markdown 报告输出路径")
    review.add_argument("--json-out", default="", help="额外输出 JSON 报告（便于 CI 消费）")
    review.add_argument("--no-md", action="store_true", help="不写 Markdown 报告，只在终端打印")
    review.add_argument("--focus", default="", help="关注点，逗号分隔：" + ",".join(FOCUS_CHOICES))
    review.add_argument("--instruction", default="", help="追加给 Agent 的自定义要求")
    review.add_argument("--max-steps", type=int, default=None, help="Agent 最大步数（默认 8）")
    review.add_argument("--max-files", type=int, default=None, help="最多扫描多少文件（默认 200）")
    review.add_argument("--include", action="append", default=[], help="只包含匹配的文件，可多次指定")
    review.add_argument("--exclude", action="append", default=[], help="排除匹配的文件，可多次指定")
    review.add_argument("--model", default="", help="覆盖模型名，例如 deepseek-reasoner")
    review.add_argument("--offline", action="store_true", help="不调用 LLM，仅确定性规则")
    review.add_argument("--fail-on", choices=["none", "high", "critical"], default="none",
                        help="达到该严重程度时以退出码 3 结束（CI 用）")
    review.add_argument("--show", type=int, default=15, help="终端最多列出多少条问题")
    add_common(review)

    # -- scan --
    scan = sub.add_parser("scan", help="只做确定性静态扫描（不调用 LLM）")
    scan.add_argument("path", nargs="?", default=".", help="要扫描的目录或文件")
    scan.add_argument("--json-out", default="", help="把扫描结果写成 JSON")
    scan.add_argument("--top", type=int, default=20, help="展示前多少条问题")
    scan.add_argument("--max-files", type=int, default=None, help="最多扫描多少文件")
    add_common(scan)

    # -- chat --
    chat = sub.add_parser("chat", help="就代码库进行交互式问答（带上下文记忆）")
    chat.add_argument("path", nargs="?", default=".", help="代码库目录")
    chat.add_argument("--session", default="", help="会话 ID：继续 / 命名一次对话")
    chat.add_argument("--question", default="", help="只问一个问题后退出（非交互模式）")
    chat.add_argument("--model", default="", help="覆盖模型名")
    chat.add_argument("--offline", action="store_true", help="离线：不调用 LLM（仅演示记忆与工具）")
    add_common(chat)

    # -- web --
    web = sub.add_parser("web", help="启动轻量 Web 界面")
    web.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    web.add_argument("--port", type=_port, default=8765, help="监听端口，默认 8765（范围 1~65535）")
    web.add_argument("--no-open", action="store_true", help="不自动打开浏览器（默认会自动打开）")
    web.add_argument("--offline", action="store_true", help="界面强制使用离线规则模式")
    add_common(web)

    return parser


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def _config_from_args(args: argparse.Namespace, root: str | None = None) -> Config:
    include = tuple(args.include) if getattr(args, "include", None) else ()
    exclude = tuple(args.exclude) if getattr(args, "exclude", None) else ()
    offline = bool(getattr(args, "offline", False))
    focus = tuple(
        item.strip().lower()
        for item in str(getattr(args, "focus", "") or "").split(",")
        if item.strip()
    )
    return load_config(
        root,
        env_file=getattr(args, "env_file", None),
        include=include,
        exclude=exclude,
        offline=offline,
        focus=focus,
        max_steps=getattr(args, "max_steps", None),
        max_files=getattr(args, "max_files", None),
        model=getattr(args, "model", "") or None,
        verbose=bool(getattr(args, "verbose", False)),
        color=not bool(getattr(args, "no_color", False)),
    )


def cmd_review(args: argparse.Namespace) -> int:
    """review 子命令：跑完整 Agent 审查并写出报告。"""
    console = Console(color=not args.no_color, quiet=args.quiet)
    root = ensure_root(args.path)
    only_file = args.path if os.path.isfile(args.path) else None
    cfg = _config_from_args(args, os.path.dirname(args.env_file) or None)
    if args.focus:
        invalid = [f for f in cfg.focus if f not in FOCUS_CHOICES]
        if invalid:
            console.error(f"--focus 取值非法：{', '.join(invalid)}；可选：{', '.join(FOCUS_CHOICES)}")
            return EXIT_USAGE

    console.out(f"代码审查 Agent v{__version__}")
    console.out(f"目标：{root}{'（单文件模式）' if only_file else ''}")
    console.out(
        f"模式：{'离线规则模式' if cfg.offline or not cfg.has_api_key() else f'在线 Agent（{cfg.model}）'}"
        f"；范围：{_scope_text(cfg)}"
    )
    console.out("")

    agent = CodeReviewAgent(cfg, progress=make_progress(console))
    try:
        scan_result, outcome = agent.review(
            root,
            focus=cfg.focus,
            extra_instruction=args.instruction,
            only_file=only_file,
        )
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except LLMAuthError as exc:
        console.error(f"鉴权失败：{exc}")
        return EXIT_AUTH
    except CodeReviewAgentError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.warn("已被用户中断；未生成报告。")
        return EXIT_USAGE

    console.out("")
    if outcome.degraded:
        console.warn(f"结果已降级：{outcome.degraded_reason}")
    console.ok(f"审查完成：{len(outcome.findings)} 条问题，耗时 {outcome.duration:.2f}s")
    console.out("")
    console.out(outcome.summary)
    console.out("")

    visible = outcome.findings[: max(args.show, 0)]
    if visible:
        console.out(f"问题列表（前 {len(visible)} 条，按严重程度排序）：")
        for finding in visible:
            console.out(console.table_row(finding))
        if len(outcome.findings) > len(visible):
            console.out(f"  ...（其余 {len(outcome.findings) - len(visible)} 条见报告文件）")
        console.out("")

    markdown = render_markdown(scan_result, outcome, cfg=cfg)
    if not args.no_md:
        path = write_report(args.out, markdown)
        console.ok(f"Markdown 报告：{path}")
    if args.json_out:
        path = write_json(args.json_out, render_json(scan_result, outcome, cfg))
        console.ok(f"JSON 报告：{path}")

    if args.fail_on != "none":
        threshold = 3 if args.fail_on == "high" else 4
        if any(finding.severity_rank >= threshold for finding in outcome.findings):
            console.warn(f"--fail-on {args.fail_on}：存在达到阈值的问题，退出码 {EXIT_DEGRADED}")
            return EXIT_DEGRADED
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    """scan 子命令：只跑确定性静态规则，不调用 LLM。"""
    console = Console(color=not args.no_color, quiet=args.quiet)
    root = ensure_root(args.path)
    only_file = args.path if os.path.isfile(args.path) else None
    cfg = _config_from_args(args, os.path.dirname(args.env_file) or None)
    result = scan_workspace(root, cfg, only_file=only_file)

    console.out(f"静态扫描（不调用 LLM）：{result.root}")
    console.out(
        f"文件 {len(result.files)} 个 / {result.total_bytes_scanned} 字节；"
        f"命中 {len(result.findings)} 条；耗时 {result.duration:.2f}s"
    )
    counts = result.counts_by_severity()
    console.out(
        "严重程度分布：" + "，".join(f"{SEVERITY_LABEL_ZH[k]}={v}" for k, v in counts.items() if v)
    )
    console.out("")
    for finding in result.findings[: max(args.top, 0)]:
        console.out(console.table_row(finding))
    if len(result.findings) > args.top:
        console.out(f"...（共 {len(result.findings)} 条）")
    for note in result.notes:
        console.warn(note)
    if args.json_out:
        write_json(args.json_out, result.to_dict())
        console.ok(f"扫描结果：{os.path.abspath(args.json_out)}")
    return EXIT_OK


def cmd_chat(args: argparse.Namespace) -> int:
    """chat 子命令：带工具与上下文记忆的交互式问答（会话可持久化）。"""
    console = Console(color=not args.no_color, quiet=args.quiet)
    root = ensure_root(args.path)
    cfg = _config_from_args(args, os.path.dirname(args.env_file) or None)
    if cfg.offline or not cfg.has_api_key():
        console.error("chat 需要可用的 API Key（或去掉 --offline）：对话模式无法用规则替代。")
        return EXIT_USAGE

    store = SessionStore(os.path.join(os.getcwd(), ".cra_sessions"))
    session_id = args.session or store.new_id("chat")
    agent, memory, context = build_chat_agent(cfg, root, progress=make_progress(console))

    previous = store.load(session_id) if args.session else None
    if previous:
        restored = memory.restore_history(previous.get("messages"))
        console.info(f"已恢复会话 {session_id}（{restored} 条历史消息）")

    console.out(f"代码问答会话（{root}）；输入 /help 查看命令，/exit 退出。会话 ID：{session_id}")

    def handle(question: str) -> int:
        try:
            answer = agent.ask(question, memory, context)
        except LLMAuthError as exc:
            console.error(f"鉴权失败：{exc}")
            return EXIT_AUTH
        except LLMError as exc:
            console.error(f"调用失败：{exc}")
            return EXIT_USAGE
        console.out("")
        console.out(answer)
        console.out("")
        store.save(session_id, messages=memory.to_messages(), meta={"root": root, "model": cfg.model})
        return EXIT_OK

    if args.question:
        return handle(args.question)

    while True:
        try:
            # lstrip BOM：管道/脚本喂进来的第一行可能带 UTF-8 BOM（Windows 上常见）
            question = input("你 > ").lstrip("\ufeff").strip()
        except (EOFError, KeyboardInterrupt):
            console.out("")
            break
        if not question:
            continue
        if question in ("/exit", "/quit", ":q"):
            break
        if question == "/help":
            console.out("/help 帮助；/clear 清空记忆；/files 列出文件；/save 保存会话；/exit 退出")
            continue
        if question == "/clear":
            memory.trimmed_count = 0
            memory.messages = memory.messages[:1]
            console.ok("已清空对话记忆（保留代码库概况）。")
            continue
        if question == "/files":
            from .tools.files import tool_list_files

            console.out(tool_list_files(context, path=".", max_results=30))
            continue
        if question == "/save":
            path = store.save(session_id, messages=memory.to_messages(), meta={"root": root})
            console.ok(f"会话已保存：{path}")
            continue
        if looks_like_shell_command(question):
            console.warn("这看起来是系统命令行，不是给 Agent 的提问。")
            console.info("  这个 `你 >` 是对话输入框：要执行命令请先输入 /exit 退出，再在系统提示符下运行。")
            console.info("  如果确实想让我查代码，请用自然语言提问，例如：buggy_service.py 有什么安全问题？")
            continue
        code = handle(question)
        if code not in (EXIT_OK,):
            return code
    console.info(f"会话已保存：{store.path_for(session_id)}")
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    """web 子命令：启动零依赖的本地 Web 界面。"""
    console = Console(color=not args.no_color, quiet=args.quiet)
    from .web.server import serve

    cfg = _config_from_args(args, os.path.dirname(args.env_file) or None)
    if args.offline:
        cfg.offline = True
    serve(cfg, host=args.host, port=args.port, open_browser=not args.no_open, console=console, cwd=os.getcwd())
    return EXIT_OK


def _scope_text(cfg: Config) -> str:
    parts = []
    if cfg.include:
        parts.append("include=" + ",".join(cfg.include))
    if cfg.exclude:
        parts.append("exclude=" + ",".join(cfg.exclude))
    if cfg.focus:
        parts.append("关注=" + ",".join(FOCUS_LABEL_ZH.get(f, f) for f in cfg.focus))
    return "；".join(parts) if parts else "全部源码文件"


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """命令行总入口：解析参数 → 分发子命令 → 返回退出码。"""
    setup_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK

    if getattr(args, "verbose", False):
        import logging

        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    handlers = {"review": cmd_review, "scan": cmd_scan, "chat": cmd_chat, "web": cmd_web}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_USAGE
    except ConfigError as exc:
        print(f"✗ 配置错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:  # 管道被关闭（如 | head）
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
