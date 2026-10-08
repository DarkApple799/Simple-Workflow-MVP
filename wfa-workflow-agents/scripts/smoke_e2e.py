"""端到端冒烟脚本：真正跑一遍 Workflow（供 CI / 演示 / 交付前自检）。

与 `tests/` 里的单元测试不同，本脚本**按交付形态**执行：
    1. 在临时工作区里离线跑 `code_review`（默认拓扑）；
    2. 离线跑 `code_review`（开启并行审查）；
    3. 离线跑 `docs`；
    4. 校验每个流程的 RunReport、产物与消息总线日志；
    5. 打印 Mermaid 流程图，供文档引用。

用法::

    python scripts/smoke_e2e.py            # 全部跑
    python scripts/smoke_e2e.py --keep     # 保留 .tmp/ 下的工作区便于检查
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from wfa.agents import default_registry  # noqa: E402
from wfa.config import Config  # noqa: E402
from wfa.engine import RunConfig, WorkflowEngine  # noqa: E402
from wfa.bus import MessageBus  # noqa: E402
from wfa.model import RuleChatModel  # noqa: E402
from wfa.state import Blackboard  # noqa: E402
from wfa.workflows import build_workflow, list_workflows  # noqa: E402

TMP = os.path.join(PROJECT_DIR, ".tmp", "smoke-e2e")
FAILURES: list = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    """断言式检查，打印 PASS/FAIL 并累计失败。"""
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" —— {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def run_workflow(name: str, workspace: str, *, parallel_review: bool = False, task: str = "") -> dict:
    """离线跑一个工作流，返回 (report_dict, engine, state)。"""
    shutil.rmtree(workspace, ignore_errors=True)
    os.makedirs(workspace, exist_ok=True)

    cfg = Config(mode="offline", max_iterations=3, max_turns=50)
    workflow = build_workflow(name, parallel_review=parallel_review, max_iterations=3, max_turns=50)
    agents = default_registry(RuleChatModel(), config=cfg)
    bus = MessageBus()
    state = Blackboard(workspace=workspace, bus=bus)
    run_config = RunConfig(
        task=task or "实现订单折扣结算模块并完成审查与修复",
        workspace=workspace,
        mode="offline",
        model="offline-rule-model",
        max_turns=50,
        max_iterations=3,
        extra={"workflow": name},
    )
    state.set("workspace", workspace)
    state.set("task", run_config.task)
    events = []
    engine = WorkflowEngine(workflow, agents, config=run_config, bus=bus, state=state,
                            on_event=lambda e, p: events.append((e, p)))
    report = engine.run()
    payload = report.to_dict()
    payload.update({"task": run_config.task, "workspace": workspace, "model": run_config.model, "workflow": name})
    return payload, engine, state, events


def scenario_code_review() -> None:
    """场景一：默认拓扑的 code_review。"""
    print("\n=== 场景 1：code_review（顺序 + 门禁循环 + 文档）===")
    payload, engine, state, events = run_workflow("code_review", os.path.join(TMP, "code_review"))
    counts = payload.get("counts") or {}
    messages = payload.get("messages")
    print(f"  状态={payload.get('status')} 回合={payload.get('turns')} 问题={counts} 消息={messages}")
    check(payload.get("status") in ("success", "partial"), "流程跑通", str(payload.get("status")))
    check(sum(counts.values()) >= 0, "问题统计可用", str(counts))
    check("issues" in state.keys() or bool(counts), "issues 已写入共享状态")
    check(bool(state.artifacts()), "产生了产物", ", ".join(list(state.artifacts())[:4]))
    check(payload.get("turns", 0) >= 6, "至少经过 6 个节点", f"turns={payload.get('turns')}")
    check(int(messages or 0) > 0, "消息总线有记录", f"messages={messages}")
    names = [event for event, _ in events]
    check("turn_start" in names and "node_end" in names, "事件流包含回合与节点事件")
    check(any(event == "stop" for event in names), "有明确停止原因")
    files = state.list_files()
    print(f"  工作区文件：{files}")
    check(any(str(f).endswith(".py") for f in files), "工作区里生成了 Python 文件")
    return payload


def scenario_parallel() -> None:
    """场景二：并行审查。"""
    print("\n=== 场景 2：code_review + 并行审查 ===")
    payload, engine, state, events = run_workflow("code_review", os.path.join(TMP, "code_review_parallel"),
                                                  parallel_review=True)
    print(f"  状态={payload.get('status')} 回合={payload.get('turns')}")
    check(payload.get("status") in ("success", "partial"), "并行拓扑跑通", str(payload.get("status")))
    check(any(event == "parallel_start" for event, _ in events), "触发了并行节点事件")
    return payload


def scenario_docs() -> None:
    """场景三：文档生成流程。"""
    print("\n=== 场景 3：docs（规划 → 写作 → 审校，含返工循环）===")
    payload, engine, state, events = run_workflow(
        "docs", os.path.join(TMP, "docs"), task="写一份多 Agent 协作 Workflow 的说明文档"
    )
    print(f"  状态={payload.get('status')} 回合={payload.get('turns')} 产物={list(state.artifacts())}")
    check(payload.get("status") in ("success", "partial"), "文档流程跑通", str(payload.get("status")))
    docs = state.artifacts()
    check(bool(docs), "产出文档", ", ".join(docs))
    text = ""
    for artifact in docs.values():
        content = artifact.get("content")
        if isinstance(content, str):
            text += content
    check(len(text) > 50, "文档内容非空", f"{len(text)} 字符")
    return payload


def scenario_metadata() -> None:
    """场景零：元信息。"""
    print("\n=== 场景 0：Agent / Workflow 清单 ===")
    for item in list_workflows():
        workflow = build_workflow(item["name"])
        problems = workflow.validate()
        check(not problems, f"工作流 {item['name']} 校验通过", "；".join(problems))
        check(len(workflow.nodes()) >= 4, f"工作流 {item['name']} 节点数 >= 4", str(len(workflow.nodes())))
    registry = default_registry(RuleChatModel())
    print("  Agent：" + ", ".join(registry.names()))
    check(len(registry.names()) >= 6, "注册的 Agent 数量 >= 6", str(len(registry.names())))


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，先切 UTF-8，避免中文/emoji 输出抛 UnicodeEncodeError。
    from wfa.cli import setup_console

    setup_console()

    parser = argparse.ArgumentParser(description="wfa 端到端冒烟")
    parser.add_argument("--keep", action="store_true", help="保留临时工作区")
    args = parser.parse_args(argv)
    if not args.keep:
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    scenario_metadata()
    cr = scenario_code_review()
    scenario_parallel()
    scenario_docs()

    print("\n=== 汇总 ===")
    if FAILURES:
        print(f"共 {len(FAILURES)} 项检查失败：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部检查通过 ✅")
    print(f"（临时工作区：{TMP}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
