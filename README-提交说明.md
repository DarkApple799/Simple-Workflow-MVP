# Homework 2 提交说明 · 2412190618 钟伟杰

> 本目录是 Homework 2 的完整提交物。
> 工程代码在 `wfa-workflow-agents/`，打包文件在同级 `wfa-workflow-agents.zip`（结构与 Homework 1 的 `agent.zip` 一致：压缩包内第一层就是工程目录）。
>
> **目录内容**：
>
> | 路径 | 是什么 |
> | --- | --- |
> | `wfa-workflow-agents/` | Homework 2 主体工程（含 `.git` 版本库、`tests/`、`docs/`） |
> | `code-review-agent/` | Homework 1 的工程（随包提供，用于**验证"复用项目1的 Agent"**；其中 `.env` 已移除，不会带出任何密钥） |
> | `README-提交说明.md` | 本文件 |
>
> 之所以把 Homework 1 一起放进来：`wfa-workflow-agents` 的 `legacy_review` 节点会自动在**同级目录**寻找 `code-review-agent`（`..\code-review-agent`），
> 因此评审者**不需要配置任何环境变量**，直接跑 `python run.py run --workflow code_review --offline` 就能看到 `legacy_review` 节点的指标为 `legacy: True`（真的调用了 Homework 1 的 `CodeReviewAgent`）。
> 如果单独只拿 `wfa-workflow-agents/`，该节点会自动回退到内置的 22 条规则引擎，流程照常跑通（报告里会写明回退原因）。

## 1. 作业对应关系

| 作业要求 | 本项目对应实现 |
| --- | --- |
| 方向：Mini SE 流程（需求→设计→代码）+ 代码审查流程（编码→审查→修复） | 合成一条端到端流程 `code_review`：`start → requirements → design → code → legacy_review → review → gate_quality ⟲ fix → review → docs → report → end` |
| 至少 2 个 Agent 协作完成完整流程 | 单次运行实际用到 6 个 Agent（`requirements`/`designer`/`coder`/`reviewer`/`fixer`/`documenter`），另注册了 `legacy_reviewer`、`security_reviewer`、`maintainability_reviewer`、`planner`、`writer`、`proofreader`，共 12 个 |
| Agent 间的消息传递或状态共享 | **两者都实现**：`wfa/bus.py`（MessageBus：8 类消息、广播、订阅、请求-应答配对、可序列化）与 `wfa/state.py`（Blackboard：命名空间键值、产物登记、工作区文件、运行状态、检查点持久化） |
| 基本流程控制（顺序执行） | `wfa/graph.py` 的 `Workflow`/`WorkflowBuilder` 声明节点与边，`wfa/engine.py` 按图驱动执行 |
| 条件分支 | `gate_quality` 依据审查结论给出 `retry` / `pass`，出边按 `when` 条件选择 |
| 循环 | `fix → review` 回边，`loop_of="impl"` 计数，受 `--max-iterations` 与 `max_turns` 双重上限保护 |
| 并行执行 | `parallel_review` 节点用 `ThreadPoolExecutor` 并发跑"安全线 + 可维护性线"（`--parallel-review`） |
| 可视化 Workflow 编排界面（可选） | 零依赖 Web 界面 `python webui.py --port 8765`：拓扑 + 运行按钮 + 实时事件流 + 报告阅读 |
| 执行日志与状态追踪 | `NodeRecord` 时间线 + `MessageBus.history()` + Markdown 运行报告（`docs/run_report.md`） |
| 错误处理与恢复机制 | 节点级 `retry` / `on_error=fail\|continue\|fallback`；模型层重试退避与错误分类；`--checkpoint` / `--resume-from` 断点续跑 |
| 复用项目 1 的 Agent 作为 Workflow 节点 | `legacy_review` 节点绑定 `LegacyReviewAgent`，直接复用 Homework 1 `code-review-agent` 的 `CodeReviewAgent`（扫描器 + 25 条 AST 规则 + 工具循环）；找不到时自动回退内置规则引擎 |

## 2. 怎么跑（零第三方依赖，Python 3.9+）

```bat
cd wfa-workflow-agents

python run.py list                                          :: 看 Agent 与流程清单
python run.py run --workflow code_review --offline           :: 离线跑通完整流程（不需要 API Key）
python run.py run --workflow code_review --offline --parallel-review
python run.py run --workflow docs --task "写一份说明"         :: 第二条完全不同领域的流程
python run.py graph --workflow code_review --format md       :: 导出流程拓扑（Mermaid + 节点/边表）
python webui.py --port 8765                                  :: Web 界面 → http://127.0.0.1:8765
python -m unittest discover -s tests -t .                    :: 单元测试
```

Windows 下也可以直接双击 / 运行 `start-web.cmd`（Web 界面）或 `wfa.cmd <参数>`（命令行）。
若想真正调用 LLM：把 `.env.example` 复制为 `.env` 并填 `DEEPSEEK_API_KEY`，再给 `run` 加 `--online`。

## 3. 文档与产出物

| 内容 | 位置 |
| --- | --- |
| 使用说明（快速开始、命令表、Agent/消息/黑板设计、边界情况、FAQ） | `wfa-workflow-agents/README.md` |
| 架构设计（分层架构、依赖方向、数据结构、执行回合伪代码、出边规则、错误恢复、取舍） | `wfa-workflow-agents/docs/Design.md` |
| 流程图（两个流程的 Mermaid 拓扑 + 节点表 + 边表 + 循环说明，由命令真实生成） | `wfa-workflow-agents/docs/FLOW.md` |
| 1~2 分钟演示脚本（逐条命令 + 真实输出 + 讲解词） | `wfa-workflow-agents/docs/DEMO_SCRIPT.md` |
| 真实运行报告示例（Markdown + JSON） | `wfa-workflow-agents/docs/run_report.md`、`run_report.json` |
| 接口契约（模块级冻结接口，团队协作与可扩展性说明） | `wfa-workflow-agents/docs/CONTRACT.md` |

## 4. 与 Homework 1 的关系

1. **能力复用**：`legacy_review` 节点通过 `wfa/agents/code_review.py::LegacyReviewAgent` 复用 Homework 1 的
   `cra` 包（`CodeReviewAgent` / `scanner` / `analysis` 25 条规则 / 工具循环），把 `AgentOutcome.findings`
   转成本工程的 `Issue` 并汇入主线审查。设置 `WFA_LEGACY_CRA_ROOT` 指向 Homework 1 工程目录即可；
   找不到时自动回退到本工程内置的 22 条规则引擎，并如实写明回退原因。
2. **经验复用**：配置层（`.env` 解析、限额、忽略规则）、LLM 客户端（标准库 urllib + 重试退避 + 错误分类 +
   工具协议降级）、JSON 脏数据提取、密钥不落报告的约束、标准库 `unittest` 测试策略，都沿用 Homework 1 的取舍。
3. **差异**：Homework 1 是"单 Agent + 工具循环"，Homework 2 是"多 Agent + 图编排"；
   两者通过适配器解耦，本工程不 import Homework 1 的代码（只在运行时按路径加载），因此可以独立交付。
