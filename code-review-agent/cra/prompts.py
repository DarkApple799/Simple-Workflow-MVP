"""Prompt 设计：系统提示、任务提示、扫描摘要、JSON 降级协议。

Prompt 是本项目"架构分"的一部分，因此集中管理、可版本化、可测试：
    * SYSTEM_PROMPT            在线 + 支持 function calling 时的主提示词
    * JSON_PROTOCOL_PROMPT     服务端不支持 tools 时的降级协议提示词
    * OFFLINE_NOTE             离线模式向模型/用户说明规则模式
"""

from __future__ import annotations

from .config import Config
from .models import SEVERITY_LABEL_ZH, ScanResult

# --------------------------------------------------------------------------- #
# 主系统提示词
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """你是一名资深代码审查工程师（Code Reviewer），目标是在有限步数内对给定代码库产出**可落地、有证据**的审查报告。

## 工作原则
1. **先宏观后微观**：先 list_files / scan_directory 了解结构与热点，再针对可疑文件 read_file / analyze_python 深挖。
2. **结论必须有依据**：每条问题都要能对应到具体文件与行号，或引用工具输出；不要凭文件名猜测内容，不要编造行号。
3. **区分事实与推断**：静态规则命中只是线索，你要判断它在本项目上下文里是否真的是问题；确认后就事论事，误报要明确排除。
4. **优先级排序**：安全漏洞、逻辑缺陷、异常处理、并发/资源泄漏优先；纯风格问题最多提 1-2 条代表性的。
5. **给出可执行建议**：建议要具体到"改成什么写法"，必要时给出代码片段。
6. **边界情况要说明**：遇到无法解析的文件、二进制文件、非 Python 代码、超大文件，在 summary 里说明你的处理方式与局限。
7. **不要重复劳动**：同一个文件不要反复读取；工具返回"已截断"时不要原样再要一次；
   **不要用完全相同的参数重复调用同一个工具**——系统会直接拒绝重复调用并计入浪费的步数。

## 严重程度标准
- critical：会造成数据损坏、安全漏洞或线上不可用（如命令注入、明文密钥、静默吞异常导致故障不可见）
- high：明确的功能缺陷或安全风险（如可变默认参数、eval 执行外部输入）
- medium：特定条件下出错、复杂度/可维护性显著超标
- low：小缺陷或影响有限的坏味道
- info：提示性建议

## 结束条件
调查充分后，**调用 submit_review 工具提交最终结论**（必做一次）：summary 概述整体质量与最严重问题，findings 逐条列出问题。
若确实没有发现问题，也要调用 submit_review 并把 findings 传空数组。
用中文写 summary/detail/suggestion。findings 控制在 15 条以内，只保留真正有价值的问题。"""


# --------------------------------------------------------------------------- #
# 无工具支持时的降级协议
# --------------------------------------------------------------------------- #
JSON_PROTOCOL_PROMPT = SYSTEM_PROMPT + """

## 输出协议（重要）
当前模型不支持工具调用，请**每轮只输出一个 JSON 对象**，不要输出任何其他文字。

需要继续调查时输出：
{"action": "tool", "tool": "read_file", "arguments": {"path": "a.py", "start_line": 1}}

可用工具：list_files、read_file、search_code、analyze_python、scan_directory、file_stats。

调查结束、给出最终结论时输出：
{"action": "final", "summary": "……", "verdict": "request_changes", "findings": [{"title": "…", "file": "a.py", "line": 12, "severity": "high", "category": "bug", "detail": "…", "suggestion": "…"}]}
"""


OFFLINE_NOTE = (
    "离线模式：未调用 LLM，仅使用确定性静态规则（AST + 正则）生成报告。"
    "该模式不需要 API Key，可用于无网络环境、CI 门禁或作为在线模式的兜底。"
)


# --------------------------------------------------------------------------- #
# 扫描摘要
# --------------------------------------------------------------------------- #
def build_scan_digest(scan: ScanResult, cfg: Config, *, max_chars: int = 4000) -> str:
    """把扫描结果压缩成一段给模型看的"地图"，控制长度避免挤占上下文。"""
    lines: list[str] = []
    lines.append(f"工作区：{scan.root}")
    if scan.scope_file:
        lines.append(
            f"范围：**单文件模式**，本次只审查 `{scan.scope_file}`；"
            "访问其他路径会被工具直接拒绝，因此不要尝试浏览同目录的其他文件。"
        )
    counts = scan.counts_by_kind()
    lines.append(
        "文件统计：共 {total} 个文件（{kinds}），累计 {size} 字节，扫描耗时 {duration:.2f}s".format(
            total=len(scan.files),
            kinds="，".join(f"{k}={v}" for k, v in sorted(counts.items())) or "无",
            size=scan.total_bytes_scanned,
            duration=scan.duration,
        )
    )
    if scan.skipped:
        reasons: dict[str, int] = {}
        for item in scan.skipped:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        lines.append(
            "跳过项：" + "；".join(f"{reason} × {count}" for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:6])
        )

    severity_counts = scan.counts_by_severity()
    lines.append(
        "规则命中：共 {total} 条（致命 {critical}、严重 {high}、中等 {medium}、轻微 {low}、提示 {info}）".format(
            total=len(scan.findings), **severity_counts
        )
    )

    # 规则排行
    rule_counter: dict[str, int] = {}
    for finding in scan.findings:
        key = f"{finding.rule_id} {finding.title}"
        rule_counter[key] = rule_counter.get(key, 0) + 1
    if rule_counter:
        top = sorted(rule_counter.items(), key=lambda item: -item[1])[:8]
        lines.append("主要规则：")
        lines.extend(f"  - {name} × {count}" for name, count in top)

    # 热点文件 + 高危明细
    hotspots = scan.hotspot_files(limit=8)
    if hotspots:
        lines.append("热点文件（问题数）：")
        lines.extend(f"  - {name}：{count} 条" for name, count, _ in hotspots)

    serious = [f for f in scan.findings if f.severity in ("critical", "high")][:12]
    if serious:
        lines.append("高危命中（需要你确认是否为真问题）：")
        for finding in serious:
            location = f"{finding.file}:{finding.line}" if finding.line else finding.file
            lines.append(f"  - [{finding.rule_id} {SEVERITY_LABEL_ZH.get(finding.severity, finding.severity)}] {location} {finding.title} — {finding.detail[:100]}")
    elif scan.findings:
        lines.append("高危命中：无（规则命中均为中低优先级）。")

    for note in scan.notes:
        lines.append(f"注意：{note}")

    digest = "\n".join(lines)
    if len(digest) > max_chars:
        digest = digest[:max_chars] + f"\n...（摘要已截断，共 {len(digest)} 字符）"
    return digest


def build_user_task(
    scan: ScanResult,
    cfg: Config,
    *,
    focus: tuple[str, ...] = (),
    extra_instruction: str = "",
) -> str:
    """构造首轮用户消息：任务 + 扫描摘要 + 输出要求。"""
    focus_text = "、".join(focus) if focus else "全部（缺陷、安全、性能、可维护性、文档）"
    if scan.scope_file:
        first_line = f"请审查这一个文件：{scan.scope_file}（单文件模式，其他文件不可访问）"
        scope_line = f"审查范围：{scan.scope_file}（位于 {scan.root}）"
    else:
        first_line = "请对下面的代码库做一次完整的代码审查。"
        scope_line = f"审查范围：{scan.root}"
    parts = [
        first_line,
        scope_line,
        f"关注重点：{focus_text}",
        "",
        "## 预扫描结果（由确定性规则产出，未经 LLM 判断，可能包含误报）",
        build_scan_digest(scan, cfg),
        "",
        "## 你的任务",
        "1. 用工具核实上述高危命中，排除误报，补充规则发现不了的语义问题（逻辑错误、边界条件、并发、资源管理、API 误用）。",
        "2. 判断是否存在规则未覆盖但更严重的问题。",
        "3. 调用 submit_review 提交结论（summary + findings，findings 不超过 15 条）。",
    ]
    if scan.scope_file:
        parts.insert(
            6,
            "注意：这是单文件审查，不要调用 list_files/search_code 去探索目录；"
            "直接 read_file 精读该文件后给出结论即可。",
        )
    if extra_instruction:
        parts.append("")
        parts.append("## 补充要求")
        parts.append(extra_instruction)
    return "\n".join(parts)


def build_step_nudge(step: int, max_steps: int) -> str:
    """接近步数上限时的提醒，引导模型收敛。"""
    remaining = max_steps - step
    if remaining <= 1:
        return "步数即将用尽：请立即调用 submit_review 提交当前已确认的结论，不要再调用其他工具。"
    return f"剩余步数 {remaining}：请收敛调查范围，优先确认最高风险的问题，随后调用 submit_review。"


def build_chat_system_prompt(root: str) -> str:
    """交互式问答（chat 子命令）的系统提示。"""
    return (
        SYSTEM_PROMPT
        + f"\n\n## 当前场景\n你正在与开发者交互式讨论代码库：{root}。"
        "优先回答开发者的问题，需要事实时用工具查看真实代码，不要臆测。"
        "回答保持简洁（一般不超过 10 行），必要时引用 文件:行号。"
    )
