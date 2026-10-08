# Simple Workflow MVP · 多 Agent 协作的 Workflow 编排

> 课程作业 **Homework 2** 的实现：用 Python 标准库从零实现一个支持 **顺序 / 条件分支 / 循环 / 并行 / 失败恢复 / 断点续跑** 的多 Agent Workflow 编排框架。
> **零第三方依赖**：clone 下来直接 `python run.py ...` 就能跑，评审时不需要 `pip install`、不需要 API Key、不需要联网。

---

## 仓库结构

```
Simple-Workflow-MVP/
├── wfa-workflow-agents/     Homework 2 主体工程（本文档的主角）
│   ├── run.py               命令行入口（run / list / graph / report）
│   ├── webui.py             零依赖 Web 界面（拓扑 + 运行 + 实时事件流 + 报告）
│   ├── wfa/                 主包：图编排、执行引擎、消息总线、共享黑板、12 个 Agent
│   ├── tests/               516 个标准库 unittest 用例（全绿、零联网）
│   ├── docs/                设计文档、流程图、演示脚本、真实运行报告
│   └── examples/            示例工作区
└── code-review-agent/       Homework 1 的代码审查 Agent（随仓库提供）
                             └─ wfa 的 legacy_review 节点会自动在同级目录发现它，
                                直接复用其"扫描器 + 25 条 AST 规则 + 工具循环"
```

> 两个工程放在同级目录不是随手为之：`wfa-workflow-agents` 的流程图里有一个 **`legacy_review` 节点**，
> 它对应作业要求"**复用项目 1 开发的 Agent 作为 Workflow 节点**"。该节点会在同级目录寻找 `code-review-agent`，
> 找到就真的调用 Homework 1 的 `CodeReviewAgent`（报告指标里出现 `legacy: True`）；
> 找不到则自动回退到内置的 22 条规则引擎，**流程照常跑通**并如实标注回退原因。

---

## 30 秒上手

```bash
cd wfa-workflow-agents

# ① 看有哪些 Agent 与流程
python run.py list

# ② 离线跑通完整流程（不需要 API Key / 不需要网络）
python run.py run --workflow code_review --offline -v

# ③ 开启并行审查（安全线 + 可维护性线并发）
python run.py run --workflow code_review --offline --parallel-review

# ④ 第二条完全不同领域的流程（证明换流程不用改引擎）
python run.py run --workflow docs --task "写一份多 Agent 协作说明" --offline

# ⑤ 导出流程拓扑图（Mermaid + 节点表 + 边表）
python run.py graph --workflow code_review --format md

# ⑥ Web 界面
python webui.py --port 8765        # → http://127.0.0.1:8765

# ⑦ 单元测试
python -m unittest discover -s tests -t .
```

**Windows 一键脚本**：`start-web.cmd`（启动 Web 界面）、`wfa.cmd <参数>`（在任意目录调用命令行）。

想真正调用 LLM：把 `.env.example` 复制为 `.env`，填入 `DEEPSEEK_API_KEY`，然后给 `run` 加 `--online`。

---

## 主流程长什么样

```
start → requirements → design → code → legacy_review → review → gate_quality
                     ↖─────────────── fix ←──(retry)──────────────┘
                      └─(pass)→ docs → report → end
```

| 环节 | 体现了什么 |
| --- | --- |
| `requirements → design → code` | 顺序执行（Mini SE 流程） |
| `code → review` | 编码 → 审查（代码审查流程） |
| `gate_quality` 的 `retry` / `pass` 出边 | **条件分支**：有问题回修复、没问题收尾 |
| `fix → review` 回边 + `loop_of`/迭代上限 | **循环**：修完复审，直到收敛或到上限 |
| `parallel_review` 子图（`--parallel-review`） | **并行**：`ThreadPoolExecutor` 真并发跑两条审查线 |
| `legacy_review` 节点 | **复用 Homework 1 的 Agent** |
| 节点 `retry` / `on_error` / `fallback`，`--checkpoint`/`--resume-from` | **错误处理与恢复** |
| `MessageBus` + `Blackboard` | **Agent 间消息传递与状态共享**（作业明确要求的两种机制都实现了） |

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [`wfa-workflow-agents/README.md`](wfa-workflow-agents/README.md) | 完整使用说明：命令与参数、退出码、Agent 清单、总线/黑板设计、高级流程控制、边界情况、FAQ |
| [`wfa-workflow-agents/docs/Design.md`](wfa-workflow-agents/docs/Design.md) | 架构设计：分层架构、依赖方向、数据结构、执行回合伪代码、出边决策规则、错误恢复策略、可扩展性、取舍 |
| [`wfa-workflow-agents/docs/FLOW.md`](wfa-workflow-agents/docs/FLOW.md) | 三个流程的真实拓扑图（Mermaid + 节点表 + 边表 + 循环说明） |
| [`wfa-workflow-agents/docs/DEMO_SCRIPT.md`](wfa-workflow-agents/docs/DEMO_SCRIPT.md) | 1.5~2 分钟演示脚本（逐条命令 + 真实输出 + 讲解词） |
| [`wfa-workflow-agents/docs/CONTRACT.md`](wfa-workflow-agents/docs/CONTRACT.md) | 模块级接口契约（并行开发时冻结接口，也是"可扩展性"的说明） |
| [`wfa-workflow-agents/docs/run_report.md`](wfa-workflow-agents/docs/run_report.md) | 真实运行产出的运行报告示例（同目录另有 JSON 版） |
| [`code-review-agent/README.md`](code-review-agent/README.md) | Homework 1：单 Agent + 工具循环的代码审查 Agent |

---

## 环境

- Python **3.9+**（开发验证环境：Python 3.13 / Windows 11）
- 依赖：**无**（`requirements.txt` 里没有必需项；可选 `pip install openai` 仅用于把传输层换成官方 SDK）
- 离线模式（默认）下模型是确定性模板模型 `RuleChatModel`，真正的语义审查能力来自确定性规则引擎；
  只有 `--online` + 配置好 Key 才会调用真实 LLM —— 这样评审者**不装依赖、不要 Key、不要网络**就能看到完整流程。
