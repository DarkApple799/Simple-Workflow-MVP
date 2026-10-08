# wfa · Workflow Agents —— 多 Agent 协作的软件工程流程编排

> Homework 2 提交项目
>
> 把 **「需求 → 设计 → 代码」** 与 **「编码 → 审查 → 修复」** 两条 Mini SE 流程合成
> 一条**端到端、可离线跑通**的多 Agent 协作流程：
> 多个 Agent 各司其职，通过**消息总线**通信、通过**共享黑板**交换产物，
> 由**流程执行器**按图拓扑驱动，支持**顺序 / 条件分支 / 循环 / 并行 / 失败恢复 / 断点续跑**。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 亮点：对照四条评审标准](#2-亮点对照四条评审标准)
- [3. 快速开始](#3-快速开始)
- [4. 命令与参数](#4-命令与参数)
- [5. 目录结构](#5-目录结构)
- [6. Workflow 是怎么跑起来的](#6-workflow-是怎么跑起来的)
- [7. Agent 清单](#7-agent-清单)
- [8. 消息总线与共享状态](#8-消息总线与共享状态)
- [9. 高级流程控制](#9-高级流程控制)
- [10. 复用 Homework 1 的审查 Agent](#10-复用-homework-1-的审查-agent)
- [11. 测试](#11-测试)
- [12. 边界情况处理](#12-边界情况处理)
- [13. 常见问题](#13-常见问题)
- [14. 交付物](#14-交付物)

---

## 1. 这是什么

给定一句任务描述，`wfa` 会自动跑完一条**软件工程闭环**：

1. **需求 Agent** 把任务拆成功能/非功能需求与验收标准；
2. **设计 Agent** 给出模块划分、接口签名与文件清单；
3. **编码 Agent** 生成实现与单元测试，写入工作区并做一次本地自测；
4. **复用来的审查 Agent**（`legacy_review` 节点）调用 **Homework 1 的 `CodeReviewAgent`**（扫描器 + 25 条 AST 规则 + 工具循环）先过一遍；
5. **审查 Agent** 做三级审查（确定性规则引擎 → 工具核实 → 语义判定），合并上游结论，产出分级问题清单与结论 `verdict`；
6. **质量门禁** 根据「是否存在阻断级问题」决定**回到修复节点**还是**放行收尾**；
7. **修复 Agent** 按问题清单改写代码、产出 patch，然后**回到审查**（这就是循环）；
8. **文档 Agent** 依据代码与审查结论生成使用文档；
9. **汇报节点** 汇总成一份运行报告。

```text
start → requirements → design → code → legacy_review（复用 HW1 的审查 Agent）
                                             │
                                             ├─(默认)────────────────────────────────┐
                                             └─(并行)─ security_review ┐            │
                                                        维持性线 ──────┴→ parallel_review
                                                                                    │
                                        review ← ───────────────────────────────────┘
                                          │
                                          ├── retry ──→ gate_quality ──→ fix ──┐
                                          ↑                                    │
                                          └──────────── 回边：修复后复审 ←──────┘
                                          │
                                          └── pass ──→ docs → report → end
```

更精确地说，`gate_quality` 的两条出边是：

```text
review → gate_quality ─┬─ when="retry" ─→ fix → review      （修复循环，受 --max-iterations 限制）
                       └─ when="pass"  ─→ docs → report → end
```

另外内置了第二条**完全不同领域**的流程 `docs`（主题 → 大纲 → 内容 → 审校 → 发布），
用来证明同一套引擎/总线/黑板可以承载新领域，**不需要修改一行引擎代码**。

**一句话架构**：`图定义流程，黑板共享产物，总线传递消息，引擎驱动回合；模型层可替换，离线也有确定结果。`

---

## 2. 亮点：对照四条评审标准

### 流程完整性（40%）

| 作业要求 | 本项目的实现 |
| --- | --- |
| 针对 Mini SE 流程 需求→设计→代码 | `requirements` → `design` → `code` 三个串行节点（见 `wfa/workflows/code_review.py`） |
| 针对 编码→审查→修复 | `code` → `legacy_review` → `review` → `gate_quality` → `fix` → `review` 闭环 |
| 从项目 1 复用 Agent 作为 Workflow 节点 | `legacy_review` 节点绑定 `legacy_reviewer`（`LegacyReviewAgent`），真的调用 Homework 1 的 `CodeReviewAgent`；找不到就回退内置规则引擎，**流程不失败** |
| 条件分支 | `gate_quality` 依据审查结论给出 `retry` / `pass`，出边按 `when` 条件选择 |
| 循环 | `fix → review` 回边，`loop_of="impl"` 计数，受 `--max-iterations` 上限保护 |
| 并行 | `parallel_review` 节点用 `ThreadPoolExecutor` 并发跑「安全线 + 可维护性线」 |
| 执行日志与状态追踪 | 每条消息、每个节点、每个回合都有记录：`NodeRecord` + `MessageBus.history()` + Markdown 报告 |
| 错误处理与恢复 | 节点级 `retry` / `on_error=fail\|continue\|fallback`；模型层重试退避与错误分类；`--checkpoint` / `--resume-from` 断点续跑 |
| 可视化编排界面（可选） | `python webui.py` 零依赖 Web 界面：拓扑 + 运行按钮 + 实时日志 + 报告阅读 |

### Agent 协作（30%）

- **≥2 个 Agent 协作完成完整流程**：`code_review` 默认用到 7 个 Agent（`requirements` `designer` `coder` `legacy_reviewer` `reviewer` `fixer` `documenter`），开启 `--parallel-review` 后是 9 个；`docs` 用 `planner`/`writer`/`proofreader` 3 个。
- **Agent 间消息传递**：`wfa/bus.py` 的 `MessageBus` 支持 `task / result / question / answer / review / feedback / system / error`
  八类消息、广播、订阅、`wait_for` 请求-应答配对。
- **Agent 间状态共享**：`wfa/state.py` 的 `Blackboard` 提供命名空间键值、产物登记、工作区文件与运行状态；
  节点只依赖黑板，**不互相持有引用**。
- **协作轨迹可见**：运行报告第 4 节直接列出「节点时间线」与「消息总线」两张表。

### 可扩展性（20%）

- **新增一个 Agent = 三步**：写类 → 注册 → 在图里加节点（见 `docs/Design.md` 第 8 节）。
- **新增一条 Workflow = 三步**：用 `WorkflowBuilder` 声明 → 登记进 `WORKFLOWS` → 自动出现在 CLI 与 Web 界面里。
- **模型层可替换**：`HttpChatModel`（标准库 urllib）/ `ScriptedChatModel`（测试）/ `RuleChatModel`（离线确定性模板）
  共享同一个 `chat()` 接口，Agent 完全不感知用的是哪一个。
- **零第三方依赖**：只用 Python 标准库，`requirements.txt` 无必需依赖。

### 文档（10%）

| 文档 | 内容 |
| --- | --- |
| `README.md`（本文） | 使用说明、命令表、Agent/消息/黑板设计、边界情况、FAQ |
| `docs/Design.md` | 分层架构、依赖方向、核心数据结构、执行回合伪代码、出边决策规则、错误恢复策略、测试策略、取舍 |
| `docs/FLOW.md` | 由 `python run.py graph --format md` **真实生成**的两个流程拓扑（Mermaid + 节点表 + 边表 + 循环说明） |
| `docs/DEMO_SCRIPT.md` | 1~2 分钟演示脚本（逐条命令 + 预期屏幕输出 + 讲解词） |
| `docs/run_report.md` | 真实运行产出的运行报告（示例） |

> **不吹牛声明**：离线模式（`--offline`，也是默认模式）下，模型是**确定性模板模型 `RuleChatModel`**
> —— 它按提示词关键词返回内置模板，**不做任何推理**；真正的语义审查能力来自
> `wfa/analysis.py` 的确定性规则引擎。只有在线模式（`--online` + 配置了 `DEEPSEEK_API_KEY`）
> 才会调用真实 LLM。这样设计是为了让评审者**不装依赖、不要 Key、不要网络**就能看到完整流程。

---

## 3. 快速开始

### 3.1 环境要求

- Python **3.9+**（开发与验证环境：Python 3.13 / Windows 11）
- **不需要安装任何第三方包**：`pip install -r requirements.txt` 是空操作（里面没有必需依赖）

```bash
# 可选：确认环境
python --version
```

### 3.2 四条命令（零依赖、零配置）

```bash
# ① 看看有哪些 Agent 与 Workflow（不跑流程，秒回）
python run.py list

# ② 离线跑完整的「编码 → 审查 → 修复」闭环（无需 API Key、无需网络）
python run.py run --workflow code_review --offline --verbose

# ③ 跑第二条流程：文档生成（主题 → 大纲 → 内容 → 审校 → 发布）
python run.py run --workflow docs --task "写一份多 Agent 协作框架的使用说明"

# ④ 打开零依赖 Web 界面（拓扑 + 运行 + 实时日志）
python webui.py --port 8765        # → http://127.0.0.1:8765
```

> **必须带端口号访问**：只输入 `127.0.0.1` 走的是 80 端口，本服务不在那里。

> **产物目录**：`run` 默认把产物写到 `examples/workflow_workspace/`（`--workspace` 可改）。
> 两条流程共用默认目录时，后一条会看到前一条留下的文件；想隔离就各给一个 `--workspace`，
> 例如 `--workspace examples/docs_workspace`。`examples/sample_project/TASK.md` 是一张**示例输入任务卡**，
> 需要"让 Workflow 面对一份外部材料"时可以把 `--workspace examples/sample_project` 指过去（注意会写进该目录）。

### 3.3 加上并行审查与迭代上限

```bash
# 打开并行审查：安全线 + 可维护性线两个 Agent 并发跑
python run.py run --workflow code_review --parallel-review --max-iterations 3 --verbose

# 把报告与 JSON 落到指定位置，并保存检查点
python run.py run --workflow code_review \
  --out docs/run_report.md --json-out docs/run_report.json \
  --checkpoint .wfa/checkpoint.json
```

### 3.4 在线模式（调用真实 LLM）

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

`.env` 内容：

```ini
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
WFA_MODE=online
```

然后显式加 `--online`（不加就是离线，**不会**因为存在 Key 就偷偷联网）：

```bash
python run.py run --workflow code_review --online --verbose
```

> `.env` 已在 `.gitignore` 中，不会被提交；`Config.public_dict()` 保证报告与日志里**绝不包含密钥**。

### 3.5 Windows 一键脚本（可选）

不想记命令、也不想处理盘符切换，直接用项目根目录的两个脚本：

| 脚本 | 作用 |
| --- | --- |
| `wfa.cmd <子命令> [参数]` | 在**任意目录**下调用 CLI，自动 `cd /d` 到项目目录并切 UTF-8 代码页 |
| `start-web.cmd` | 双击即启动 Web 界面并自动打开浏览器 |

```bat
wfa.cmd list
wfa.cmd run --workflow code_review --offline --verbose
wfa.cmd graph --workflow code_review --format md --out docs\FLOW.md
start-web.cmd
```

---

## 4. 命令与参数

### 4.1 子命令总览

| 子命令 | 作用 |
| --- | --- |
| `run` | 执行一个工作流，输出运行报告（Markdown / JSON），可选保存检查点 |
| `list` | 列出全部 Agent（名称 + 角色）与全部 Workflow（名称 + 说明 + 节点） |
| `graph` | 导出流程拓扑：`mermaid` / `dot` / `md`（`md` = 完整流程文档） |
| `report` | 把 `--json-out` 产出的 JSON 重新渲染成 Markdown 报告 |

### 4.2 `run` 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--workflow`, `-w` | `code_review` | 工作流名（`code_review` / `docs`） |
| `--task`, `-t` | 内置示例任务 | 给流程的任务描述（会写进黑板键 `task` 与报告） |
| `--workspace` | `examples/workflow_workspace` | 产物写入的工作区目录（所有文件读写的安全边界） |
| `--online` | 关 | 调用真实 LLM（需要 `DEEPSEEK_API_KEY`） |
| `--offline` | 开 | 强制离线：确定性规则引擎 + 模板模型 |
| `--model` | `.env` 的 `DEEPSEEK_MODEL` | 覆盖模型名 |
| `--max-turns` | `50` | 整个流程的最大回合数（安全阀） |
| `--max-iterations` | `3` | 每个循环（`loop_of`）的最大迭代次数 |
| `--review-max-steps` | `4` | 审查 Agent 最多几步工具调用 |
| `--parallel-review` | 关 | 启用并行审查（安全线 + 可维护性线） |
| `--workers` | `3` | 并行节点的线程数 |
| `--ignore-blocking` | 关 | 把 `stop_on_blocking` 设为 False：存在阻断问题时流程也不降级为 `partial`（退出码仍按阻断问题返回 3） |
| `--no-write-workspace` | 关 | 只读模式：不往工作区写文件 |
| `--out` | `docs/run_report.md` | Markdown 报告输出路径 |
| `--json-out` | 空 | JSON 结果输出路径 |
| `--checkpoint` | 空 | 把运行检查点写到该路径 |
| `--resume-from` | 空 | 从检查点文件恢复运行 |
| `--env-file` | 空 | 显式指定 `.env` |
| `--verbose`, `-v` | 关 | 加 `-v`：打印**全量事件**（含每回合的 `turn_end` JSON）并把 `wfa.*` 的 INFO 日志打到 stderr；不加 `-v` 时只打印关键事件（`node_start`/`node_end`/`node_failed`/`stop`/`parallel_start`） |

### 4.3 `graph` / `report` 参数

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `graph` | `--workflow` / `--format mermaid\|dot\|md` / `--parallel-review` / `--out` | `md` 会生成完整流程文档（Mermaid + 节点表 + 边表 + 循环说明 + DOT） |
| `report` | `--json <文件>` / `--out <文件>` | 从 JSON 重新渲染报告；不写 `--out` 就直接打印到终端 |

### 4.4 退出码（便于接 CI）

| 退出码 | 含义 | 触发场景 |
| --- | --- | --- |
| `0` | 成功 | 流程 `status ∈ {success, partial}` 且无阻断级问题 |
| `1` | 参数 / 配置错误 | 未知工作流名、`ConfigError`、在线模式缺 Key |
| `2` | 鉴权 / 在线模型不可用 | ① LLM 返回 401 / 403（`LLMAuthError`）；② **在线模式配了 Key 但模型调用全部失败**（此时报告会降级为确定性规则结论，stderr 打印 `[在线模式告警]`，便于 CI 发现"其实没联网"） |
| `3` | 存在阻断级问题 | 报告的 `metrics.blocking`（= critical + high 计数）非 0；`--ignore-blocking` 只影响流程终态是否会提前停止，不改变本退出码 |
| `4` | 运行失败 / 中断 | 抛 `WorkflowError`、`Ctrl+C`、流程 `failed` |

### 4.5 `webui.py` 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址（只绑本机，不对外暴露） |
| `--port` | `8765` | 监听端口 |
| `--no-open` | 关 | 不自动打开浏览器 |
| `--online` | 关 | Web 触发的运行默认走在线模式 |
| `--model` | `.env` | 覆盖模型名 |
| `--no-write-workspace` | 关 | 只分析不写工作区 |
| `--verbose`, `-v` | 关 | 详细日志 |

Web 接口（零依赖 `http.server`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 单页 UI（拓扑 + 运行表单 + 实时日志） |
| GET | `/api/health` | 健康检查 |
| GET | `/api/workflows` | 工作流清单（含节点、边、Mermaid 源码） |
| GET | `/api/agents` | Agent 清单（名称 + 角色 + 自我介绍） |
| GET | `/api/runs` | 全部运行记录 |
| POST | `/api/run` | 启动一次运行（后台线程），返回 `run_id` |
| GET | `/api/runs/<id>?since=N` | 运行状态 + **增量**事件流 + 报告 |
| GET | `/api/runs/<id>/report` | 运行报告（`text/markdown`） |

---

## 5. 目录结构

```text
wfa-homework2/
├── run.py                      # CLI 便捷入口：python run.py <子命令>
├── webui.py                    # Web 便捷入口：python webui.py
├── wfa.cmd / start-web.cmd     # Windows 一键脚本（自动 cd /d + UTF-8）
├── requirements.txt            # 零必需依赖（可选：openai SDK 传输层）
├── .env.example / .gitignore
├── wfa/                        # 主包
│   ├── __init__.py             # 版本号、严重程度/类别标签、阻断口径常量
│   ├── config.py               # 配置层：.env 解析、模型/编排/扫描限额
│   ├── errors.py               # 分层异常（配置 / 节点 / 工具 / LLM / 流程控制）
│   ├── util.py                 # 通用工具：脏 JSON 提取、路径安全、重试策略
│   ├── model.py                # LLM 接口层：HTTP / 脚本化 / 离线规则模型
│   ├── bus.py                  # Agent 间消息总线（发布订阅 + 请求应答 + 日志）
│   ├── state.py                # 共享状态：Blackboard + RunState + NodeRecord
│   ├── graph.py                # 节点 / 边 / Workflow / WorkflowBuilder / AgentResult
│   ├── analysis.py             # 确定性 Python 规则引擎（AST 规则集）
│   ├── engine.py               # 编排执行器：回合循环、出边决策、并行、检查点
│   ├── report.py               # 报告渲染：运行报告 + 流程文档（Mermaid/DOT）
│   ├── cli.py                  # 命令行：run / list / graph / report
│   ├── agents/                 # Agent 实现
│   │   ├── base.py             # Agent 抽象 + AgentRegistry + default_registry
│   │   ├── coding.py           # coder
│   │   ├── review.py           # reviewer / security_reviewer / maintainability_reviewer
│   │   ├── fix.py              # fixer
│   │   ├── doc.py              # DocAgent：requirements / designer / planner / writer / proofreader（+documenter 实例）
│   │   └── code_review.py      # legacy_reviewer（复用 Homework 1 的适配器）
│   ├── workflows/              # 预置流程
│   │   ├── code_review.py      # 编码 → 审查 → 修复 闭环
│   │   ├── docs.py             # 主题 → 大纲 → 内容 → 审校 → 发布
│   │   └── registry.py         # WORKFLOWS 注册表 + build_workflow
│   └── web/                    # 零依赖 Web 界面
│       ├── server.py           # http.server 路由 + 运行注册表
│       └── static/index.html   # 单页 UI
├── examples/                   # 演示材料
│   ├── sample_project/         # 示例输入：一张任务卡 TASK.md（可当 --workspace 用）
│   ├── workflow_workspace/     # code_review 的默认产物目录（可随时删除重建）
│   └── docs_workspace/         # docs 流程演示用的独立产物目录
├── tests/                      # 标准库 unittest 单元测试
└── docs/                       # 文档交付物
    ├── CONTRACT.md             # 模块接口契约（团队内部冻结版）
    ├── Design.md               # 架构设计
    ├── FLOW.md                 # 流程拓扑（由 graph 命令生成）
    ├── DEMO_SCRIPT.md          # 演示脚本
    ├── run_report.md           # 真实运行报告示例
    └── run_report.json         # 同一份运行的 JSON 结果
```

---

## 6. Workflow 是怎么跑起来的

### 6.1 运行时时序（一次 `run` 的完整链路）

```text
用户              CLI(wfa/cli.py)   Engine            Agent             MessageBus       Blackboard
 │  run --offline     │                │                 │                   │               │
 ├───────────────────►│                │                 │                   │               │
 │                    ├─ load_config ──┤                 │                   │               │
 │                    ├─ build_workflow（声明节点与边）   │                   │               │
 │                    ├─ build_model（offline → RuleChatModel）              │               │
 │                    ├─ default_registry(model)          │                   │               │
 │                    ├─ MessageBus() / Blackboard() ─────┼───────────────────┼──────────────►│
 │                    ├─ WorkflowEngine(..., on_event) ──►│                   │               │
 │                    │                ├─ turn += 1       │                   │               │
 │                    │                ├─ 取当前节点 → when 判定（False 记 skipped）           │
 │                    │                ├─ agents.get(node.agent)              │               │
 │                    │                ├─ agent.run(NodeContext) ────────────►│               │
 │                    │                │                 ├─ state.require/get ───────────────►│
 │                    │                │                 ├─ bus.send("task"/"result"/...) ──►│
 │                    │                │                 ├─ state.set / put_artifact ───────►│
 │                    │                │◄─ AgentResult ──┤                   │               │
 │                    │                ├─ 写黑板：results.<node> / 顶层 issues/counts/verdict │
 │                    │                ├─ state.record(NodeRecord) ──────────────────────────►│
 │                    │                ├─ bus.send("system", 回合小结) ──────►│               │
 │                    │                ├─ 按 decision 选择出边（见 6.3）      │               │
 │                    │                └─ 循环下一回合 …… 直到无出边 / 上限 / 终止            │
 │                    │◄─ RunReport ───┤                 │                   │               │
 │                    ├─ write_report(docs/run_report.md) │                   │               │
 │◄─ 状态 / 回合 / 问题统计 / 报告路径 ─┤                 │                   │               │
```

### 6.2 回合（turn）语义

一个「回合」= 引擎处理**一个节点**的一次执行（并行节点算一个回合，内部并发跑子节点）。

| 阶段 | 行为 |
| --- | --- |
| `before_turn` | `turn += 1`；若超过 `max_turns` → 停止，`status="partial"`；若外部 `stop_check()` 为真 → `status="aborted"` |
| 节点准入 | 节点的 `when(ctx)` 为 `False` → 记 `skipped`，跳到后继，不执行 |
| 找 Agent | `agents.get(node.agent)`；找不到按 `on_error="fail"` 处理 |
| 执行 | `agent.run(NodeContext)`；失败按节点 `retry` 重试（退避 `retry_delay`） |
| 循环计数 | 节点 `loop_of` 非空时先 `bump_iteration(loop_of)`；超过 `max_iterations` → `status="partial"` |
| 写状态 | `AgentResult` 写入 `results.<node_id>`；若 `kind=="review"` 额外写顶层 `issues/issue_counts/verdict/blocking`；`kind=="fix"` 写 `fixes` |
| 记日志 | `state.record(NodeRecord)` + 向总线发一条 `system` 消息 + 触发 `on_event` 回调 |
| 出边 | 按 `decision` 选择下一个节点（见下表） |
| 终态 | 走到 `end` / 无出边 → `success`（原因 `COMPLETED`）；若**断流点不是 `end` 节点**且 `stop_on_blocking=True` 且黑板 `blocking` 非空 → 提前停止为 `partial`（原因 `STOP`） |

### 6.3 出边决策规则

引擎优先看 `AgentResult.decision`，其次看边的 `when` / 声明顺序：

| `decision` | 选择规则 |
| --- | --- |
| `pass` / `done` | 选第一条 `dst == "end"` 的出边；没有则选最后一条出边 |
| `retry` / `continue` | 选 `when == "retry"`（或 `label == "retry"`）的出边；没有则选第一条出边 |
| `fail` | 选 `when == "fail"` 的出边；没有则按节点 `on_error` 语义处理 |
| `""`（空） | 按声明顺序选第一条 `when` 为空的出边 |
| 无出边 | 流程正常结束，`status="success"` |

### 6.4 `code_review` 节点表

| 节点 | 类型 | Agent | 作用 | 循环 | 失败策略 |
| --- | --- | --- | --- | --- | --- |
| `start` | start | - | 流程入口（空操作） | - | - |
| `requirements` | agent | `requirements` | 把任务拆成需求与验收标准 | - | fail |
| `design` | agent | `designer` | 模块划分、接口签名、文件清单 | - | fail |
| `code` | agent | `coder` | 生成实现与测试，写入工作区并自测 | `impl` | fail |
| `legacy_review` | agent | `legacy_reviewer` | **复用 Homework 1 的 `CodeReviewAgent`**（扫描器 + 25 条 AST 规则 + 工具循环）；找不到 cra 工程时回退内置规则引擎 | - | fail |
| `parallel_review` | parallel | - | 并发容器（仅 `--parallel-review` 时存在） | - | fail |
| ↳ `security_review` | agent | `security_reviewer` | 安全线审查（并行**子节点**，不单独计入节点表） | - | fail |
| ↳ `maintainability_review` | agent | `maintainability_reviewer` | 可维护性线审查（并行**子节点**） | - | fail |
| `review` | agent | `reviewer` | 汇总审查结论（规则 + 模型双引擎 + 上游结论去重/陈旧性核对） | - | fail |
| `gate_quality` | gate | - | 质量门禁：决定修复还是收尾 | - | fail |
| `fix` | agent | `fixer` | 按问题清单修复并产出 patch | `impl` | fail |
| `docs` | agent | `documenter` | 生成使用文档（`when` 为假时跳过） | - | fail |
| `report` | route | - | 汇总运行小结 | - | fail |
| `end` | end | - | 流程出口（空操作） | - | - |

> 顶层节点共 **11 个**（`--parallel-review` 时 12 个）；`↳` 两行是并行容器的 `children`，
> 由引擎在容器内部并发执行，**不作为独立节点出现在拓扑表的「节点清单」里**。

### 6.5 `code_review` 边表

| 源 | 目标 | 条件 | 含义 |
| --- | --- | --- | --- |
| `start` | `requirements` | 默认 | 顺序进入需求分析 |
| `requirements` | `design` | 默认 | 需求 → 设计 |
| `design` | `code` | 默认 | 设计 → 编码 |
| `code` | `legacy_review` | 默认 | 编码 → 复用来的 HW1 审查节点 |
| `legacy_review` | `parallel_review` 或 `review` | 默认 | HW1 审查 → 主审查（`--parallel-review` 决定是否插入并发容器） |
| `parallel_review` | `review` | 默认 | 汇总并行结论 |
| `review` | `gate_quality` | 默认 | 审查 → 门禁 |
| `gate_quality` | `fix` | `when="retry"` | 有阻断问题且未到迭代上限 → 修复 |
| `gate_quality` | `docs`（或 `report`） | `when="pass"` | 门禁通过 → 收尾 |
| `fix` | `review` | 默认 | **回边：修复后复审** |
| `docs` | `report` | 默认 | 文档 → 汇报 |
| `report` | `end` | 默认 | 结束 |

> 并行子节点**没有自己的边**：它们挂在 `parallel_review` 的 `children` 上，
> 引擎渲染时用点线（`-.->`）连到容器表示"并发"，不属于顺序流转的边。
>
> 完整拓扑（含 Mermaid 图、全部节点/边、DOT 源码）见 [docs/FLOW.md](docs/FLOW.md)，
> 它由 `python run.py graph --workflow code_review --format md --out docs/FLOW.md` **真实生成**，不是手写。

### 6.6 `docs` 节点与边（第二条流程）

| 节点 | Agent | 作用 | 循环 |
| --- | --- | --- | --- |
| `start` | - | 流程入口（空操作） | - |
| `plan` | `planner` | 产出章节大纲 | - |
| `write` | `writer` | 按大纲写正文（返工时叠加审校意见） | `doc` |
| `proofread` | `proofreader` | 审校事实、结构与表述 | - |
| `gate_doc` | - | 审校结论决定返工还是发布 | - |
| `publish` | - | 汇总最终文档并登记产物 | - |
| `end` | - | 流程出口（空操作） | - |

| 源 | 目标 | 条件 |
| --- | --- | --- |
| `start` | `plan` | 默认 |
| `plan` | `write` | 默认 |
| `write` | `proofread` | 默认 |
| `proofread` | `gate_doc` | 默认 |
| `gate_doc` | `write` | `when="retry"`（回边，需修订） |
| `gate_doc` | `publish` | `when="pass"` |
| `publish` | `end` | 默认 |

---

## 7. Agent 清单

共 **12 个**注册 Agent（`python run.py list` 的真实输出）。它们由 **4 个类**参数化而来：
`DocAgent`（5 个角色 + `documenter` 实例）、`ReviewAgent`（3 条审查线）、`CodingAgent`、`FixAgent`，
外加一个复用 Homework 1 的 `LegacyReviewAgent`。

| Agent（注册名） | 角色 | 实现 | 主要输入 | 主要输出（`AgentResult.data`） | 产物（`artifacts`） |
| --- | --- | --- | --- | --- | --- |
| `requirements` | 需求 Agent | `DocAgent(role="requirements")` | `task` | `requirement`（功能/非功能/验收）、`outline[]`、`documents{}` | `docs/requirements.md` |
| `designer` | 设计 Agent | `DocAgent(role="designer")` | 需求 + 任务 | `design`（模块划分/接口/文件清单）、`documents{}` | `docs/design.md` |
| `coder` | 编码 Agent | `CodingAgent` | `task` + 需求 + 设计 | `plan[]`、`files{}`、`tests{}`、`self_test{}` | `code/plan.md` + 工作区源码与测试文件 |
| `reviewer` | 代码审查 Agent（`focus="all"`） | `ReviewAgent` | 工作区 + 上游 `files` + 并行分支结论 | `issues[]`、`counts`、`verdict`、`blocking[]` | `review/reviewer.md` |
| `security_reviewer` | 安全审查 Agent（`focus="security"`） | `ReviewAgent` | 同上 | 同上（只保留安全类问题） | `review/security_reviewer.md` |
| `maintainability_reviewer` | 可维护性审查 Agent（`focus="maintainability"`） | `ReviewAgent` | 同上 | 同上（可维护性/风格/文档/性能） | `review/maintainability_reviewer.md` |
| `fixer` | 修复 Agent | `FixAgent` | `issues` / `blocking` | `patches[]`、`fixed[]`、`remaining[]` | `fix/fix_report.md` + 修好的工作区文件 |
| `documenter` | 文档 Agent（`code_review` 的收尾节点） | `DocAgent(role="writer", name="documenter")` | 代码 + 审查结论 | `documents{}`（文件名 → Markdown） | 工作区里的 `README.md` 等 |
| `planner` | 大纲 Agent（`docs` 流程） | `DocAgent(role="planner")` | `task` | `outline[]` | `docs/outline.md` |
| `writer` | 写作 Agent（`docs` 流程） | `DocAgent(role="writer")` | 大纲（返工时叠加审校意见） | `documents{}` | 工作区里的文档 |
| `proofreader` | 审校 Agent（`docs` 流程） | `DocAgent(role="proofreader")` | 上游文档 | `documents{}`、`verdict`、`issues[]` | `docs/proofread_notes.md` |
| `legacy_reviewer` | Homework 1 适配 Agent（`code_review` 的 `legacy_review` 节点） | `LegacyReviewAgent` | 工作区 + `WFA_LEGACY_CRA_ROOT` | `issues[]`、`counts`、`verdict`、`blocking[]`（由 Homework 1 `AgentOutcome.findings` 转换） | `review/legacy_reviewer.md` |

> **两个容易混的名字**：`documenter` 是 `code_review` 流程收尾用的文档 Agent；
> `writer` 是 `docs` 流程里的写作 Agent。两者都是 `DocAgent`，但**是两个独立注册实例、服务于不同流程**。
>
> 具体类名与构造参数见 `wfa/agents/*.py`；`legacy_reviewer` 的复用手法见[第 10 节](#10-复用-homework-1-的审查-agent)。
>
> **关于「产物」这一列**：它是 `AgentResult.artifacts`，会登记进共享黑板并出现在运行报告第 5 节。
> 其中**写侧** Agent 的产物会同时落到工作区磁盘上（`docs/requirements.md`、`docs/design.md`、
> `app/service.py`、`tests/*.py`、`README.md`），而审查 / 修复 / 汇报类产物
> （`review/*.md`、`fix/fix_report.md`、`code/plan.md`、`report/run_summary.md`）只登记在黑板里，
> 作为报告与检查点的一部分，不单独写文件。

---

## 8. 消息总线与共享状态

两条协作通道分工明确：**总线传「发生了什么」，黑板存「现在是什么」**。

### 8.1 消息总线（`wfa/bus.py`）

| 消息类型 | 语义 | 典型发送方 → 接收方 |
| --- | --- | --- |
| `task` | 派发任务 | `workflow` → 某节点 |
| `result` | 上报结果 | 某节点 → `workflow` |
| `question` | 提问（`bus.ask()`） | 节点 → 节点 |
| `answer` | 应答（`bus.reply()`，自动带 `correlation_id`） | 节点 → 提问方 |
| `review` | 审查结论送达 | `reviewer` → `workflow`/`fixer` |
| `feedback` | 修复/审校意见回灌 | `fixer`/`proofreader` → 上游 |
| `system` | 引擎的回合小结、状态变更 | `workflow`（`PERF_ID`）→ 广播 |
| `error` | 节点失败原因 | 某节点 → `workflow` |

关键 API：

```python
bus.send(kind, sender, recipient, payload, *, correlation_id="", turn=0) -> Message
bus.ask(sender, recipient, payload)                 # 发 question
bus.reply(request, sender, payload)                 # 发 answer，自动配对
bus.subscribe(recipient) -> Subscription            # subscription.drain(timeout)
bus.inbox(recipient) / bus.history(kind=..., limit=...) / bus.wait_for(recipient, kind=...)
bus.to_dict() / MessageBus.from_dict(data)          # 检查点可带完整消息历史
bus.log_lines()                                     # 一行一条的人类可读日志
```

- `BROADCAST = "*"`：广播会投递给**所有订阅者**（含引擎自身 `PERF_ID = "workflow"`）。
- 线程安全：`threading.Condition` + 可重入锁；`wait_for` 会先扫描收件箱里尚未被取走的匹配消息，
  所以并行分支下「应答比等待先到」也不会漏。
- 消息全部可 JSON 序列化（`Message.to_dict()`），因此检查点能完整带上协作历史。

### 8.2 黑板（`wfa/state.py`）

| 黑板键 / 命名空间 | 写入者 | 读取者 | 含义 |
| --- | --- | --- | --- |
| `task` | CLI | 需求/规划 Agent | 用户任务描述 |
| `workspace` | CLI | 全部 Agent | 工作区绝对路径 |
| `results.<node_id>` | engine | 下游节点 / 报告 | 每个节点的 `AgentResult.to_dict()` |
| `<node_id>` | engine | 调试 | 同上（顶层便捷键） |
| `issues` | review 类 Agent | 门禁 / 修复 / 报告 | 最终问题清单（`Issue` 字典列表） |
| `issue_counts` | review 类 Agent | 门禁 / 报告 | `{critical,high,medium,low,info}` |
| `verdict` | review 类 Agent | 门禁 / 报告 | `pass` / `pass_with_comments` / `request_changes` / `block` |
| `blocking` | review 类 Agent | 门禁 / CLI 退出码 | critical/high 子集 |
| `fixes` | fixer | 报告 | 修复 patch 列表 |
| `fixed` / `remaining` | fixer | 汇报节点 | 已修 / 未修 issue 列表 |
| `documents` | `documenter`（code_review）/ `writer`（docs） | 汇报节点 | 文件名 → Markdown 内容 |
| `metrics.<node_id>` | engine | 报告 | 各节点 `AgentResult.metrics` |
| `errors.<node_id>` | engine | 报告 / 排障 | 节点失败原因（`on_error` 分支也会写） |
| `<parallel_node>`（命名空间） | engine | 下游节点 | 并行容器的子结果 `{child_id: result}` |
| `state.iterations["impl"]` | engine | 门禁 | `impl` 循环已执行次数 |
| `state.records[]` | engine | 报告 / resume | 节点执行记录（只增不减） |

还提供：命名空间 API（`ns_set/ns_get/ns_all/ns_merge`）、产物登记（`put_artifact/artifacts`）、
工作区文件（`write_file/read_file/file_hash/list_files`，全部经 `safe_join` 做路径越界检查）、
指标卡（`summary()`）、持久化（`save/load`，纯 JSON、不 pickle）。

---

## 9. 高级流程控制

| 能力 | 怎么用 | 实现位置 |
| --- | --- | --- |
| **顺序** | `builder.edge(a, b)` 声明先后 | `wfa/graph.py` + `wfa/engine.py` |
| **条件分支** | 节点返回 `AgentResult.decision`，或给边加 `when="retry"`；节点也可用 `when=lambda ctx: ...` 做准入判断 | `engine` 出边决策 + `Node.when` |
| **循环** | 声明回边（`fix → review`、`gate_doc → write`），并给成员节点标 `loop_of="impl"`；`--max-iterations` 限制次数 | `state.bump_iteration` + `Node.max_iterations` |
| **并行** | `builder.parallel(node_id, children=[...])`；引擎用 `ThreadPoolExecutor(max_workers=config.parallel_workers)` 并发跑子节点 | `engine` 的 `parallel` 分支 |
| **失败重试** | 节点级 `retry`（次数）+ `retry_delay`（退避）；模型层 `HttpChatModel` 自己还会按 `LLMError.retryable` 做指数退避 | `engine` + `util.RetryPolicy` + `wfa/model.py` |
| **失败降级** | 节点 `on_error="continue"`（记 failed 继续后继）或 `"fallback:<node_id>"`（跳到备用节点）；Homework 1 不可用时自动回退到内置规则引擎 | `engine` + `wfa/agents/code_review.py` |
| **超时/中止** | `max_turns`（全局回合上限）、`abort()` / `Ctrl+C`（`stop_check`）→ `status="aborted"` | `engine.run` |
| **阻断即降级** | `stop_on_blocking=True`（默认）只在"**有阻断级问题 + 流程在非 `end` 节点断流**"时把终态降级为 `partial`（原因 `STOP`）；正常走到 `end` 一律 `success`，阻断问题通过 `issues` / `counts` / `metrics.blocking` 表达（CLI 据此返回退出码 3）。`--ignore-blocking` 关闭提前停止 | `engine._terminal_status` + `engine._drive` |
| **检查点 / 续跑** | `--checkpoint .wfa/checkpoint.json` 保存 `{workflow, config, blackboard, bus}`；`--resume-from .wfa/checkpoint.json` 从最后一条记录的 `next_node` 继续 | `WorkflowEngine.checkpoint/resume` |
| **可观测** | 事件流默认就打印**关键事件**（`node_start` / `node_end` / `node_failed` / `stop` / `parallel_start`）；`-v` 打开全量事件（含每回合的 `turn_end` JSON）与 `wfa.*` 的 INFO 日志。报告第 4 节含节点时间线 + 消息表 | `wfa/cli.py` 的 `EventPrinter` + `wfa/report.py` |

---

## 10. 复用 Homework 1 的审查 Agent

Homework 1 的 `code-review-agent` 已经实现了 **25 条 AST 规则 + 工作区扫描器 + 工具调用循环**，
本项目**不重写**这部分能力，而是把它包装成一个 Workflow 节点来复用：
`code_review` 流程里的 **`legacy_review`** 节点，绑定 Agent **`legacy_reviewer`**
（`wfa/agents/code_review.py::LegacyReviewAgent`），拓扑位置是 `code → legacy_review → review`
（开启 `--parallel-review` 时是 `legacy_review → parallel_review → review`）。

**适配器做的事**：

1. 按下面的**候选顺序**找到 Homework 1 工程根目录（就是含 `cra` 包的那个目录）；
2. 把该目录加入 `sys.path` 后 `importlib.import_module("cra.agent")`，调 `CodeReviewAgent(config).review(root)`；
3. 复用其扫描器、25 条 AST 规则与工具循环，把 `AgentOutcome.findings` **转换成本项目统一的 `Issue` 字典**
   （`id/title/file/line/severity/category/detail/suggestion/source/rule_id`）；
4. 成功时 `AgentResult.metrics["legacy"] = True`（并附带 `legacy_files` / `legacy_findings` / `legacy_steps` /
   `legacy_tool_calls` / `legacy_mode`），`notes` 里注明"已复用 Homework1 的 cra 包"；
5. 它的 `issues` 作为上游结论汇入 `review` 节点（`ReviewAgent` 会去重合并，并按当前工作区核对陈旧性）。

**实测证据**（本机刻意**不设**任何环境变量，靠"同级目录"发现——Homework 1 就在 `..\code-review-agent`）：

```text
  ✓ node_end  [legacy_review] status=ok decision=retry 审查完成（来源：Homework1 cra 包）：
              critical=0, high=2, medium=0, low=2, info=3，阻断 2 条，结论 request_changes。
```

对应节点的 `metrics`（从 `--json-out` 里读出来的真实值）：

```python
{'legacy': True, 'legacy_files': 6, 'legacy_findings': 1, 'legacy_steps': 0,
 'legacy_tool_calls': 0, 'legacy_mode': 'offline', 'issues': 1, 'blocking': 0}
```

### 10.1 怎么配置 Homework 1 的路径

`.env`（或环境变量）里设一个就够：

```ini
# .env —— 见 .env.example 最后一行
WFA_LEGACY_CRA_ROOT=D:\path\to\code-review-agent
```

```bat
:: 或者只对当前会话生效
set WFA_LEGACY_CRA_ROOT=D:\path\to\code-review-agent
```

`load_config()` 会把它读进 `Config.extra["cra_root"]`（该键已进 `config.ENV_KEYS`），再传给 `LegacyReviewAgent`。
程序化注入同样支持：

```python
cfg = load_config(root=ws, extra={"cra_root": r"D:\hw1\code-review-agent"})
# 或直接：LegacyReviewAgent(cra_root=r"D:\hw1\code-review-agent", offline=True)
```

适配器自己还会按下面的顺序兜底查找（`_candidate_roots()`，逐项尝试、只保留真实存在的目录）：

| 顺序 | 来源 | 示例 |
| --- | --- | --- |
| 1 | 构造参数 `cra_root` ← `Config.extra["cra_root"]` / `["hw1_root"]`（由 `WFA_LEGACY_CRA_ROOT` 填充） | `WFA_LEGACY_CRA_ROOT=D:\hw1\code-review-agent` |
| 2 | 环境变量 `WFA_CRA_ROOT` → `CRA_ROOT` → `HW1_ROOT` | `set WFA_CRA_ROOT=D:\hw1\code-review-agent` |
| 3 | 相对本工程的常见位置 | `..\code-review-agent`、`..\hw1\code-review-agent`、`vendor\code-review-agent`、`third_party\code-review-agent` |

> 所以把 Homework 1 工程放在本工程的**同级目录**（`..\code-review-agent`）时，**零配置**就能被发现 ——
> 上面的实测证据就是这么来的。

### 10.2 找不到 Homework 1 时：自动回退，流程照旧跑通

| 情况 | 行为 |
| --- | --- |
| 环境变量与 `extra` 都没配、相对路径也不存在 | 适配器自动改用 `wfa/analysis.py` 内置规则引擎 |
| 路径不存在 / `import cra` 失败 | 捕获异常、`notes` 写明原因，`metrics["legacy"] = False`，流程继续 |
| Homework 1 运行中抛错 | 同上，回退到内置规则结果，审查结论仍完整 |
| 工作区不存在 | `notes` 记录"工作区不存在，未执行 Homework1 适配器"，直接走回退 |

回退时的真实输出（把三个环境变量清空、并临时清空相对路径候选后实测）：

```text
摘要   = 审查完成（来源：内置规则引擎（回退））：critical=0, high=0, medium=0, low=1, info=0，阻断 0 条，结论 pass_with_comments。
metrics= {'legacy': False, 'fallback_rule_hits': 1, 'issues': 1, 'blocking': 0, ...}
notes  = ["未能复用 Homework1 的 cra 包（ModuleNotFoundError: No module named 'cra'），已回退到 wfa.analysis 规则引擎。",
          "回退路径使用 wfa.analysis：扫描 6 个文件，规则命中 1 条。"]
```

**因此 `legacy_review` 不是硬依赖**：节点永远存在、永远有结论，
"有没有 Homework 1"只影响 `metrics["legacy"]` 是 `True` 还是 `False`，
以及报告里那一句"来源：Homework1 cra 包 / 内置规则引擎（回退）"。
内置 `wfa/analysis.py` 的规则集（`SYN/BUG/SEC/MAINT/STYLE` 五大类）
保证**没有任何外部工程时审查依然完整**。

### 10.3 可选的第二层复用：把 HW1 发现合并进汇总线

`ReviewAgent` 还预留了一条更深的复用口子（把 HW1 的 findings 直接并进主审查的结论，而不是作为上游节点）：

```python
from wfa.agents import default_registry
from wfa.agents.code_review import LegacyReviewAgent

agents = default_registry(model, config=cfg)
legacy = LegacyReviewAgent(cra_root=cfg.extra.get("cra_root"), offline=True, config=cfg, bus=bus)
agents.get("reviewer").legacy = legacy     # 汇总线会合并 HW1 的 findings（metrics 里出现 legacy: True）
```

> 这体现的是"**复用是可插拔的**"：既可以当独立节点（默认已接），也可以作为能力注入——两种粒度都不需要改引擎。

---

## 11. 测试

标准库 `unittest`，一条命令跑完（不需要 pytest、不需要网络、不需要 API Key）：

```bash
python -m unittest discover -s tests -t .
```

**当前状态：`Ran 516 tests ... OK`（全绿，exit 0；本机 Python 3.13 / Windows 11，约 5.3 秒）**

| 测试文件 | 用例数 | 关注点 |
| --- | --- | --- |
| `test_util.py` | 59 | 脏 JSON 提取（代码块/前后废话/尾逗号/字符串内括号）、路径越界拒绝、编码回退、`RetryPolicy` 重试语义 |
| `test_model.py` | 61 | 401 不重试 / 429 与 5xx 重试、`tools` 降级、脏参数解析、`build_model` 分派（全部走注入的假传输层，零联网） |
| `test_bus_state.py` | 51 | 发布订阅、广播可见性、`wait_for` 应答先到不丢、黑板命名空间/产物/路径安全、`save`/`load` 往返 |
| `test_analysis.py` | 52 | 每条 AST 规则都能触发、干净代码零误报、语法错误兜底、上限优先保留高危 |
| `test_graph_engine.py` | 92 | `validate()` 检出问题图、Mermaid/DOT 回边、出边决策、`when` 跳过、循环上限 → `partial`、`max_turns`、`on_error` 三态、并行合并、`checkpoint`/`resume` |
| `test_agents.py` | 76 | 各 Agent 的 `data` 契约、离线路径必须产出非空结果、修复→复审后 `blocking` 归零、HW1 适配器成功/回退两条路径 |
| `test_workflows.py` | 37 | 两个流程的拓扑与门禁口径（含 `--parallel-review` 变体、`legacy_review` 节点位置）、启动即可校验通过 |
| `test_report.py` | 27 | 报告章节完整、**报告与 JSON 不含 API Key**、表格单元格转义 |
| `test_web.py` | 24 | 真实起 `ThreadingHTTPServer` 打请求：`/api/health`、`/api/workflows`、`/api/run` + 轮询、`/report` |
| `test_config.py` | 24 | `.env` 解析（BOM/引号/行尾注释）、环境变量优先级、`WFA_LEGACY_CRA_ROOT` → `extra["cra_root"]`、`public_dict()` 不含密钥 |
| `test_cli.py` | 13 | `list` / `graph` / `report` 子命令、退出码语义（0 / 1 / 3） |

- 测试临时工作区统一放在工程根目录的 `.tmp/`（不用 `tempfile`，规避受限环境的权限问题）。
- 全部 LLM 相关测试使用 `ScriptedChatModel`（脚本化响应）与注入的假传输层，**完全离线、可复现**。
- 仓库里没有 conftest/pytest 依赖；`tests/conftest.py` 只是给 IDE 用的路径辅助，不引入 pytest 语义。
- 建议跑测试时隔离字节码缓存（我们遇到过旧 `__pycache__` 导致的假失败）：

```bat
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
python -m unittest discover -s tests -t .
```

---

## 12. 边界情况处理

| 场景 | 系统行为 |
| --- | --- |
| 没有 API Key | `--offline` 正常跑完（默认行为）；在线模式抛 `ConfigError` 并给出三种解决方式 |
| 没有网络（在线模式） | `LLMNetworkError`（可重试）→ 指数退避重试 `max_retries` 次；仍失败则节点按 `on_error` 处理 |
| 401 / 403 | 立即失败、不重试；若整次运行中模型调用全部失败，报告降级为规则结论，CLI 打印 `[在线模式告警]` 并返回退出码 **2**，提示检查 Key |
| 429 / 5xx | 指数退避 + 抖动（固定随机种子，节奏可复现），尊重 `Retry-After` |
| 服务端不支持 tools | `HttpChatModel` 自动关闭 tools 重投一次（协议降级），Agent 改走 JSON 文本协议 |
| 模型返回脏 JSON | `util.extract_json_object` 括号配对 + 字符串感知扫描 + 去尾逗号，容忍 ```json 包裹 |
| 规则引擎解析失败（语法错误） | 转成 `SYN001` 问题，**不中断**流程 |
| 代码有阻断级问题 | 门禁给 `retry` → 进入修复循环；到达 `--max-iterations` 仍有问题 → 收尾并在报告标注「未完全收敛」 |
| 修复后仍有问题 | 回到 `review` 复审，直到收敛或触顶 |
| 问题无法修复 | 记入 `remaining` 并说明原因，报告第 3 节列出 |
| `when` 为假的节点 | 记 `skipped`，不执行、不报错 |
| 找不到节点绑定的 Agent | 按 `on_error="fail"` 处理（有 fallback 则跳 fallback） |
| 单个节点抛异常 | 按节点 `retry` 重试；仍失败则 `fail`（或 `continue` / `fallback`） |
| 路径越界（`../`、绝对路径、软链外指） | `safe_join` 抛 `PathSecurityError`，**直接拒绝**（防提示词注入读工作区外文件） |
| 文件编码不是 UTF-8 | 依次尝试 UTF-8(BOM) → GB18030 → latin-1，读出内容不炸 |
| 超大文件 / 文件数超限 | 按 `max_file_bytes` / `max_files` 截断，并在报告注明"结果不完整" |
| 达到 `--max-turns` | `status="partial"`，用已有结果出报告 |
| `Ctrl+C` | `stop_check` → `status="aborted"`，CLI 退出码 **4** |
| 检查点文件损坏 | `NodeError`，提示重新运行（不会静默产出错误结果） |
| Web 端口被占用 | 启动时报 `OSError`，换 `--port` 即可 |
| 并行子节点部分失败 | 子节点失败被吸收，`parallel` 节点仍产出合并结果，失败原因进 `notes` |

---

## 13. 常见问题

**Q：必须联网 / 必须有 Key 吗？**
不用。`--offline`（默认）下用确定性规则引擎 + 模板模型 `RuleChatModel` 跑完整流程。
`RuleChatModel` **不做推理**，只按关键词返回内置模板；真正的审查能力来自 `wfa/analysis.py` 的 AST 规则。

**Q：必须用 DeepSeek 吗？**
不用。任何 **OpenAI 兼容**接口都行：改 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL`
（通义千问、智谱、Moonshot、本地 vLLM 等）。也可以用 `WFA_TRANSPORT=sdk` 走官方 `openai` SDK。

**Q：为什么不用 LangGraph / AutoGen / CrewAI？**
作业允许原生实现，且自研编排让**出边决策、循环上限、并行合并、错误恢复、检查点**全部可见可控可测试；
本项目只有 `wfa/model.py` 这一层是外部协议边界，换框架只需替换它。详见 `docs/Design.md` 第 9 节。

**Q：Windows 控制台中文乱码？**
`run.py` 启动时会自动把 stdout/stderr 切到 UTF-8；`wfa.cmd` 还会 `chcp 65001`。
若仍乱码，手动执行 `chcp 65001` 或设置 `PYTHONIOENCODING=utf-8`。

**Q：在 cmd 里 `cd G:\...` 没反应，提示符还在 C 盘？**
cmd 的 `cd` **不换盘符**，要用 `cd /d G:\...`；或者先输 `G:` 再 `cd`。
最省事的做法是用 `wfa.cmd` / `start-web.cmd`，它们内部已经 `cd /d "%~dp0"`。

**Q：打开 `http://127.0.0.1` 显示"拒绝连接"？**
必须带端口：`http://127.0.0.1:8765`。

**Q：端口 8765 被占用怎么办？**
`python webui.py --port 8899`，然后把地址里的端口一并改掉。

**Q：在线模式跑一次要花多少钱？**
按节点数 × 每节点 1~几次模型请求估算；`RuleChatModel` 离线模式 **0 成本**。
想省钱：`--no-write-workspace` 只分析不写文件、降低 `--review-max-steps`、缩小 `--workspace` 范围。

**Q：为什么报告里说我"未完全收敛"？**
说明 `--max-iterations` 用尽时仍有 critical/high 问题。提高 `--max-iterations` 或先人工修复再跑。

**Q：产物写到哪了？**
默认 `examples/workflow_workspace/`（可用 `--workspace` 改）；报告默认 `docs/run_report.md`。
整个目录可以随时删掉重建。

---

## 14. 交付物

| 内容 | 位置 |
| --- | --- |
| 使用说明（本文） | [`README.md`](README.md) |
| 架构设计（分层、数据结构、决策规则、错误恢复、测试策略、取舍） | [`docs/Design.md`](docs/Design.md) |
| 流程拓扑（由命令真实生成：Mermaid + 节点表 + 边表 + 循环） | [`docs/FLOW.md`](docs/FLOW.md) |
| 1~2 分钟演示脚本（逐条命令 + 预期输出 + 讲解词） | [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) |
| 真实运行报告示例（Markdown） | [`docs/run_report.md`](docs/run_report.md) |
| 同一份运行的 JSON 结果（`run.py report --json` 的输入） | [`docs/run_report.json`](docs/run_report.json) |
| 模块接口契约（团队内部冻结版） | [`docs/CONTRACT.md`](docs/CONTRACT.md) |
