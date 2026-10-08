# 设计文档 · wfa（Workflow Agents）

> Homework 2 ｜ 配套代码见仓库根目录，使用说明见 [`README.md`](../README.md)
>
> 本文回答四个问题：**为什么这样分层**、**一次运行到底发生了什么**、
> **失败与异常如何收敛**、**要加东西时改哪里**。

---

## 1. 设计目标与关键取舍

| 目标（对照评审维度） | 做法 | 取舍说明 |
| --- | --- | --- |
| 流程完整性 40% | 图（`Workflow`）定义拓扑，引擎（`WorkflowEngine`）按回合驱动；顺序 / 分支 / 循环 / 并行 / 检查点全部是**引擎的一等公民**，不是特例补丁 | 不用「一堆 if-else 串起来」的写法：那样加一条流程就要改引擎 |
| Agent 协作 30% | 两条正交通道——`MessageBus`（发生了什么）+ `Blackboard`（现在是什么）；Agent 之间**零直接引用** | 总线与黑板都做成纯 JSON 可序列化，代价是 payload 必须保持简单类型 |
| 可扩展性 20% | Agent 通过 `AgentRegistry` 注册、流程通过 `WorkflowBuilder` 声明、模型通过 `ChatModel` 协议替换 | 三层都留了显式扩展点，新增领域不需要改 `engine.py` |
| 文档 10% | `README.md` + `Design.md` + **命令生成**的 `FLOW.md` + 演示脚本 | `FLOW.md` 不手写，避免文档与代码漂移 |
| 零安装负担 | 只用标准库（`urllib` 调 LLM、`http.server` 做 Web、`unittest` 做测试、`ast` 做规则） | 放弃 Flask/requests/openai 的便利，换取「老师拿到就能跑」 |
| 离线可演示 | `RuleChatModel`（确定性模板）+ `wfa/analysis.py`（真规则引擎） | **诚实说明**：离线模型不推理，只保证流程完整与结构正确 |

**一句话架构**：
`图定义「谁在什么条件下做什么」，黑板存「当前事实」，总线记「协作过程」，引擎负责推进与收敛。`

---

## 2. 总体架构

### 2.1 分层架构图

```text
┌──────────────────────────────────────────────────────────────────────────────────┐
│  入口层        run.py ──► wfa/cli.py            webui.py ──► wfa/web/server.py   │
│                run / list / graph / report      http.server + 单页 UI            │
└───────────────────────────┬──────────────────────────────┬───────────────────────┘
                            │                              │
┌───────────────────────────▼──────────────────────────────▼───────────────────────┐
│  编排层        wfa/engine.py  WorkflowEngine · RunConfig · RunReport             │
│                · 回合循环 turn loop   · 出边决策 decision → edge                 │
│                · 并行 ThreadPoolExecutor   · 检查点 checkpoint / resume           │
└───────────┬───────────────────────────────┬──────────────────────────────────────┘
            │                               │
┌───────────▼──────────────┐   ┌────────────▼──────────────────────────────────────┐
│  声明层  wfa/workflows/*  │   │  执行单元层  wfa/agents/*                          │
│  code_review / docs       │   │  requirements designer coder documenter            │
│  WorkflowBuilder 声明拓扑  │   │  writer planner proofreader fixer reviewer         │
│                           │   │  security/maintainability_reviewer                 │
│                           │   │  legacy_reviewer（复用 Homework 1）                │
└───────────┬──────────────┘   └────────────┬──────────────────────────────────────┘
            │                               │
┌───────────▼───────────────────────────────▼──────────────────────────────────────┐
│  图与规则层   wfa/graph.py（Node / Edge / Workflow / AgentResult / NodeContext）   │
│               wfa/analysis.py（AST 规则引擎：SYN/BUG/SEC/MAINT/STYLE）             │
└───────────┬──────────────────────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────────────────────┐
│  协作层      wfa/bus.py（MessageBus）        wfa/state.py（Blackboard / RunState）│
└───────────┬──────────────────────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────────────────────┐
│  基础层   wfa/model.py（LLM 接口）  wfa/util.py  wfa/errors.py  wfa/config.py     │
│           HttpChatModel / ScriptedChatModel / RuleChatModel                       │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 依赖方向（严格单向，禁止逆向 import）

```text
            util, errors, model, config
                      ▲
                 bus, state
                      ▲
                   graph
                      ▲
              agents/*   workflows/*      analysis
                      ▲
                   engine
                      ▲
              cli, web, tests
```

**为什么坚持单向**：`engine` 只认识 `graph` 抽象的 `Node`/`AgentResult`，
不认识任何具体 Agent；`agents` 只认识 `NodeContext`/`Blackboard`，不认识 `engine`。
这样 `docs` 流程才能复用 `code_review` 的一切基础设施而不产生耦合。

**唯一的方向例外已消除**：`cli.py` / `web/server.py` 里的 `from .agents import ...`
等导入放在**函数内部**（延迟导入），避免入口层在模块加载期把整棵树拉起来，
也让"只解析参数不跑流程"（`run.py --help`、`run.py list`）保持轻快。

---

## 3. 核心数据结构

四类结构贯穿全链路，且**全部可 JSON 序列化**（检查点与报告直接依赖这一点）。

### 3.1 `AgentResult` —— 节点的唯一返回契约

```python
@dataclass
class AgentResult:
    kind: str = "result"           # result | requirement | code | review | fix | doc | gate | report
    summary: str = ""              # 一句话摘要（给人看，进报告与事件流）
    decision: str = ""             # "" | continue | pass | retry | fail | done  （驱动出边）
    data: dict = {}                # 结构化载荷（见下方约定表）
    artifacts: dict[str, str] = {} # 产物名 -> 内容（进黑板产物表）
    metrics: dict = {}             # 任意指标（进报告 metrics）
    notes: list[str] = []
```

`data` 的键是**跨 Agent 的隐式契约**，门禁/汇报/报告都依赖它：

| 产出者 | key | 类型 | 含义 |
| --- | --- | --- | --- |
| 编码类 | `files` | `dict[str,str]` | 相对路径 → 文件内容 |
| 编码类 | `tests` | `dict[str,str]` | 测试文件 |
| 编码类 | `plan` | `list[str]` | 实现步骤 |
| 编码类 | `self_test` | `dict` | `{"command","returncode","stdout"}` |
| 审查类 | `issues` | `list[dict]` | `Issue`：`id,title,file,line,severity,category,detail,suggestion,source,rule_id` |
| 审查类 | `counts` | `dict` | `{critical,high,medium,low,info}` |
| 审查类 | `verdict` | `str` | `pass` / `pass_with_comments` / `request_changes` / `block` |
| 审查类 | `blocking` | `list[dict]` | critical/high 子集 |
| 修复类 | `patches` | `list[dict]` | `{file,issue_id,summary,before,after}` |
| 修复类 | `fixed` / `remaining` | `list` | 已修 id / 未修 issue（含原因） |
| 文档类 | `documents` | `dict[str,str]` | 文件名 → Markdown |
| 文档类 | `outline` | `list[str]` | 章节标题 |

**为什么用 `kind` 而不是靠类名判断**：`engine` 只按 `kind` 决定往黑板写哪些顶层键
（`review` → `issues/issue_counts/verdict/blocking`，`fix` → `fixes`），
新 Agent 只要复用既有 `kind` 就自动获得同样的下游能力。

### 3.2 `Node` / `Edge` / `Workflow` —— 拓扑即数据

```python
@dataclass
class Node:
    id: str
    agent: str = ""                       # AgentRegistry 里的名字
    execute: ExecuteFn | None = None      # 自定义节点逻辑（gate/route 用），签名 (node, ctx) -> AgentResult
    type: str = "agent"                   # agent | gate | route | parallel | start | end
    next: list[str] = []                  # 出边（按声明顺序）
    when: Callable[[NodeContext], bool] | None = None   # 入边准入条件
    retry: int = 0                        # 失败重试次数（不含首次）
    retry_delay: float = 0.2
    on_error: str = "fail"                # fail | continue | fallback:<node_id>
    fallback: str | None = None
    loop_of: str = ""                     # 所属循环 id（iterations 计数用）
    max_iterations: int = 0               # 0 = 用全局 max_iterations
    children: list[Node] = []             # type == "parallel" 的子节点
    options: dict = {}
    description: str = ""

@dataclass
class Edge:
    src: str; dst: str; when: str = ""; label: str = ""
```

`NodeContext` 是节点能看到的**全部世界**（由引擎构造，鸭子类型引用黑板与总线）：

| 成员 | 含义 |
| --- | --- |
| `node` / `state` / `bus` | 当前节点、共享黑板、消息总线 |
| `inputs` | **直接前驱**最近一次成功的 `AgentResult`（`{前驱 id: AgentResult}`） |
| `input_result(id)` / `payload(id, key)` / `first_payload(key)` / `all_payloads(key)` | 取上游结果或上游 `data` 里的键 |
| `iteration` / `turn` / `workspace` / `config` | 循环迭代次数、当前回合、工作区、`RunConfig` |
| `extras` | 引擎注入的临时数据（并行容器会把合并结果放在 `extras["children"]`） |

`execute` 的返回类型有两种：`AgentResult`（业务结果，最常用）或 `NodeOutcome`
（`{next_node, status, error, payload}`——用于**强制跳转**、自报 `skipped`、额外回写黑板键）。
引擎归一化两者，因此自定义节点既能"像 Agent 一样"只回报结果，也能直接操控流转。

`Workflow` 是节点表 + 边表 + 元信息（`entry`、`max_turns`、`max_iterations`），
并提供四个**文档与测试都依赖**的方法：

| 方法 | 用途 |
| --- | --- |
| `successors(node_id)` | 引擎的出边候选（按声明顺序） |
| `validate()` | 返回问题列表（悬空边、不可达节点、缺 entry…）；空 = 合法 |
| `to_mermaid()` | 文档与 Web 界面的拓扑图 |
| `to_dot()` | Graphviz DOT（含回边标注） |

`WorkflowBuilder` 是声明式门面：`start/agent/gate/route/parallel/edge/build`，
`build()` 内部**自动校验**，有问题直接抛 `NodeError` —— 让错误的图在构造期就失败，
而不是跑到一半才发现边写错。

### 3.3 `Message` —— 协作过程的最小记录

```python
@dataclass
class Message:
    kind: str          # task | result | question | answer | review | feedback | system | error
    sender: str        # 节点 id 或 PERF_ID("workflow")
    recipient: str     # 节点 id 或 BROADCAST("*")
    payload: dict = {}
    id: str = ""               # 自动 msg-xxxxxxxx
    created_at: str = ""       # 自动 now_iso()
    turn: int = 0              # 产生该消息的回合
    correlation_id: str = ""   # 请求-应答配对（reply() 自动回填）
```

设计要点：

1. **日志是唯一真相**：`publish()` 先追加进 `_messages`，再投递到订阅队列；
   `inbox()` 从日志里按 `recipient` 过滤。这样"广播可见性"与"历史回看"天然一致。
2. **`wait_for` 的竞态处理**：维护每个收件人的 `_wait_cursors`，
   先在收件箱中**尚未被取走**的匹配消息里找，找不到才阻塞等待——
   并行分支下"应答比等待先到"是常态，这样不会漏。
3. **`BROADCAST` 投给所有订阅者**，并强制包含 `PERF_ID`，保证引擎总能观测到全局消息。

### 3.4 `Blackboard` / `RunState` / `NodeRecord` —— 共享事实

`Blackboard` 是四件事的聚合门面，全部在一把可重入锁内操作（并行分支会并发写）：

| 能力 | API | 说明 |
| --- | --- | --- |
| 命名空间键值 | `set/get/require/update/keys/snapshot` + `ns_*` | `coder` 写 `code.files`、`reviewer` 写 `review.issues` 互不干扰 |
| 产物登记 | `put_artifact/artifact/artifacts` | 报告只读元信息，不必翻任意键 |
| 工作区文件 | `write_file/read_file/file_hash/list_files` | 全部经 `util.safe_join`，越界抛 `PathSecurityError` |
| 运行状态 | `record/bump_iteration/iteration/summary` | `NodeRecord` **只增不减**，支撑报告与 `--resume-from` |

`RunState.status` 的取值集合是**流程终态的完整枚举**：
`pending | running | success | partial | failed | aborted`。
`partial` 专门表示"被上限截断 / 被策略提前停止，但产出仍然可用"（例如达到 `max_turns`、
循环触顶、或在非 `end` 节点带着阻断级问题断流），
这样 CLI 能把它判成"部分成功"而不是失败 —— **流程能终止** 比"卡死等完美结果"更符合工程预期。
注意：走到 `end` 的正常收尾**永远**是 `success`；此时若仍有阻断级问题，
它们通过 `RunReport.issues` / `counts` / `metrics["blocking"]` 表达并映射到 CLI 退出码 3，
避免"流程明明跑完了却报 partial"的语义歧义。

---

## 4. 执行回合伪代码

`WorkflowEngine.run()` 的核心循环（与 `docs/CONTRACT.md` 第 8 节语义一一对应）：

```python
def run(self, *, resume_from=None):
    current = resume_from or workflow.entry_node().id          # ① 起点（或恢复点）
    while current is not None:
        # ---- before_turn：回合与安全阀 ----
        turn += 1
        if turn > config.max_turns:      -> stop(MAX_TURNS);      status = "partial"
        if stop_check() is True:         -> stop(ABORTED);        status = "aborted"

        node = workflow.node(current)

        # ---- 节点准入 ----
        if node.when is not None and not node.when(ctx(node)):
            state.record(NodeRecord(status="skipped"));  current = next_after(node); continue

        # ---- 循环计数 ----
        if node.loop_of:
            iteration = state.bump_iteration(node.loop_of)
            limit = node.max_iterations or config.max_iterations
            if iteration > limit:        -> stop(MAX_ITERATIONS); status = "partial"

        # ---- 找执行体：agent 名字 或 自定义 execute ----
        if node.execute is not None:
            body = lambda: node.execute(node, ctx(node))
        else:
            agent = agents.get(node.agent)                      # 缺失 -> on_error 语义
            body = lambda: agent.run(ctx(node))

        # ---- 并行节点：并发跑 children，再合并 ----
        if node.type == "parallel":
            on_event("parallel_start", {...})
            with ThreadPoolExecutor(max_workers=config.parallel_workers) as pool:
                pairs = pool.map(lambda c: (c.id, run_child(c)), node.children)
            result = AgentResult(data={"children": {cid: r.to_dict() for cid, r in pairs}})
            if node.execute: result = node.execute(node, ctx(node, merged=result))
            state.ns_merge(node.id, {cid: r.to_dict() for cid, r in pairs})
            on_event("parallel_end", {...})

        # ---- 执行 + 节点级重试 ----
        else:
            result = RetryPolicy(node.retry, base_delay=node.retry_delay).run(body)

        # ---- 失败处理 ----
        except NodeError as err:
            state.record(NodeRecord(status="failed", error=...))
            bus.send("error", current, PERF_ID, {...})
            if node.on_error == "continue":   current = next_after(node); continue
            if node.fallback:                 current = node.fallback;    continue
            status = "failed"; break

        # ---- 成功：写黑板 ----
        state.set(current, result.to_dict())
        state.ns_merge("results", {current: result.to_dict()})
        if result.kind == "review":
            state.update({"issues": ..., "issue_counts": ..., "verdict": ..., "blocking": ...})
        if result.kind == "fix":
            state.set("fixes", result.data.get("patches", []))
        for name, content in result.artifacts.items():
            state.put_artifact(name, content, kind="doc", producer=current)   # 产物登记为 kind="doc"

        # ---- 记账 + 广播 ----
        state.record(NodeRecord(status="ok", decision=result.decision, ...))
        bus.send("system", PERF_ID, BROADCAST, {"node": current, "decision": result.decision}, turn=turn)
        on_event("node_end", {...})

        # ---- 出边决策（见第 5 节）----
        current = self._next_node(node, result)                 # None => 流程结束
        checkpoint_if_needed()

    return build_report(state, bus, status)
```

**并行语义的关键约定**：并行子节点的结果被合并成一个 `data["children"]` 字典，
若容器节点提供了 `execute`，则把合并结果再交给它产出**最终** `AgentResult`。
同时子结果写进 `state.ns_merge(node.id, ...)`，因此下游既能拿聚合视图也能拿单个分支。

---

## 5. 出边决策规则

引擎的函数式核心是 `_next_node(node, result) -> str | None`，规则**确定且可测试**：

| 优先级 | `result.decision` | 选边规则 |
| --- | --- | --- |
| 1 | `pass` / `done` | 第一条 `dst == "end"` 的出边；没有则最后一条出边 |
| 2 | `retry` / `continue` | `when == "retry"` 或 `label == "retry"` 的出边；没有则第一条出边 |
| 3 | `fail` | `when == "fail"` 的出边；没有则按 `on_error` 语义（continue / fallback / 终止） |
| 4 | `""`（空） | 按声明顺序取第一条 `when` 为空的出边 |
| 5 | 无出边 | 返回 `None` → 流程结束 |
| 6 | 终态校准 | 正常走到 `end` / 无出边 → `success` + `COMPLETED`；若**断流点不是 `end` 节点**且 `stop_on_blocking=True` 且黑板 `blocking` 非空 → 提前停止为 `partial` + `STOP` |

**为什么把决策权交给 Agent 而不是图**：
「有没有阻断级问题」这类判断需要读黑板（`issues` / `verdict` / 迭代计数），
写在 `gate` 节点的 `execute` 里比写成边的布尔表达式更直观，也更容易在报告里解释
（门禁的 `summary` 会原样进报告，读者能看到"为什么走了修复分支"）。

**为什么还要 `when`**：并行子节点的边（`parallel_review → security_review`）是**结构边**，
不应该由数据决定；而 `gate_quality → fix / docs` 是**语义边**，由 `decision` 决定。
两类边共存，靠 `when` 标注区分。

---

## 6. 错误处理与恢复策略

### 6.1 分层异常 → 策略映射

| 异常 | 层次 | 可重试 | 系统行为 |
| --- | --- | --- | --- |
| `ConfigError` | 配置 | — | CLI 退出码 1；提示缺失项与三种解决方式 |
| `NodeError` / `AgentError` | 节点 | — | 节点 `retry` 次；仍失败按 `on_error`（fail / continue / fallback） |
| `ToolError` | 工具 | — | 工具调用失败被收敛成"失败观察结果"回灌 Agent，不炸流程 |
| `PathSecurityError` | 工具（安全） | 否 | **直接拒绝且不降级**：安全防线不得被宽泛 `except` 吞掉 |
| `LLMAuthError`（401/403） | LLM | 否 | 立即失败，CLI 退出码 2 |
| `LLMBadRequestError`（400/404/422） | LLM | 否 | 立即失败；若提到 tools 则**关闭 tools 重投一次**（协议降级） |
| `LLMRateLimitError`（429） | LLM | 是 | 指数退避 + 抖动，尊重 `Retry-After` |
| `LLMServerError`（5xx） | LLM | 是 | 指数退避，最多 `max_retries` |
| `LLMNetworkError` | LLM | 是 | 同上（DNS / 连接 / 读超时） |
| `LLMResponseError` | LLM | 否 | 立即失败（服务端不兼容），提示换模型 |
| `WorkflowAborted` | 流程 | — | `status="aborted"`，退出码 4 |

**重试由数据决定**：`RetryPolicy` 只读 `LLMError.retryable` 这个属性，不做 `isinstance` 类型分派——
将来新增错误类型不必改重试逻辑。退避公式 `min(base * 2^(n-1) + jitter, 20s)`，
抖动来自**固定种子**的随机数（`random.Random(20261008)`），因此重试节奏可复现，便于排障与测试。

### 6.2 三层恢复能力

| 层级 | 机制 | 恢复粒度 | 适用场景 |
| --- | --- | --- | --- |
| 调用级 | `HttpChatModel` 内部重试 | 单次 HTTP 请求 | 网络抖动、限流、5xx |
| 节点级 | `Node.retry` + `retry_delay` | 单个节点的一次执行 | Agent 内部偶发失败（模型输出不可用） |
| 流程级 | `on_error=fail\|continue\|fallback:<node>` | 拓扑上的走向 | 某条线整体不可用（例如并行分支全挂） |
| 运行级 | `checkpoint()` / `resume()` | 整次运行 | 长流程中断后续跑、跨进程迁移 |
| 能力级 | `LegacyReviewAgent` 回退到内置规则引擎 | 一路证据源 | Homework 1 工程不存在/导入失败 |

**关于"复用 Homework 1"的设计**：`code_review` 流程里有一个独立节点 **`legacy_review`**（`code → legacy_review → review`，
并行版为 `legacy_review → parallel_review → review`），绑定 Agent `legacy_reviewer`。
它把 Homework 1 的 `CodeReviewAgent`（扫描器 + 25 条 AST 规则 + 工具循环）当作**普通节点**执行，
产出的 `issues` 走和其它审查 Agent 完全相同的通路（`data["issues"]` → 黑板顶层 `issues` → 门禁）。
采用**节点级复用**而不是"在 ReviewAgent 里内联调用"，好处有三：

1. **拓扑可见**：`docs/FLOW.md` 的节点表里直接能看到这个复用来的节点，对应作业要求"可复用项目1的 Agent 作为 Workflow 节点"；
2. **可观测**：它在报告第 4 节的节点时间线里有自己的一行（含耗时与结论摘要），
   `metrics["legacy"]` 明确标注是 `True`（真的用了 cra 包）还是 `False`（回退到内置规则引擎）；
3. **可降级**：找不到 Homework 1 工程时它自己回退到 `wfa/analysis.py`，**节点不失败、流程不中断** —— 复用是可插拔的增强，不是硬依赖。

`ReviewAgent` 另外还留了一条更深的注入口子（`ReviewAgent(legacy=...)`），
把 HW1 的 findings 直接并进主审查结论；两种粒度都不需要改引擎。

**停止原因（`RunReport.metrics["stop_reason"]`）是终态的另一半信息**：
`completed`（正常走到终点）/ `max_turns` / `max_iterations` / `aborted` / `error` / `stop`（按策略停止，例如 `stop_on_blocking`）。
`status` 回答"结果能不能用"，`stop_reason` 回答"为什么停"，两者一起写进报告，
避免读者把"达到上限"误读成"出错了"。

**节点级重试的一个实现细节**：`RetryPolicy` 按契约只重试 `LLMError.retryable=True`，
而 `Node.retry` 需要覆盖**任意**节点异常（程序 bug、断言失败、文件错误）。
引擎内部用一个私有包装类型 `_RetryableNodeFailure` 把任意异常临时标记为可重试，
重试耗尽后再用 `.original` 还原真实异常写进记录与报告 —— 这样既不污染公共 `RetryPolicy` 语义，
又让"节点重试"与"模型重试"复用同一套指数退避实现。

### 6.3 检查点与续跑

```python
engine.checkpoint("wfa/.wfa/checkpoint.json")   # 写 {workflow, config, blackboard, bus}
engine2, report = WorkflowEngine.resume(path, agents)   # 恢复黑板 + 消息历史，从断点继续
```

- 序列化**只用纯 JSON**（dataclass 手工 `to_dict/from_dict`，不 pickle）：
  检查点能跨进程、跨版本、可被人直接阅读与 diff。
- `util.jsonable()` 做统一降级（dataclass → dict、set → list、未知对象 → `str()`），
  **绝不因为"黑板上放了个怪对象"而保存失败**。
- `resume` 从 `resume_from` 参数或最后一条记录的 `next_node` 继续，并在报告的
  `notes` 里写明"已从检查点恢复"，避免"这份报告是两次运行拼起来的"被误读。

---

## 7. 并行执行设计

```python
builder.parallel("parallel_review", children=[
    Node(id="security_review",        agent="security_reviewer"),
    Node(id="maintainability_review", agent="maintainability_reviewer"),
])
```

| 关注点 | 处理 |
| --- | --- |
| 并发模型 | `ThreadPoolExecutor`（Agent 的耗时主要是等待 LLM/IO，线程足够且无需引入 async） |
| 并发度 | `config.parallel_workers`（CLI `--workers`，默认 3） |
| 结果合并 | `AgentResult.data = {"children": {child_id: result.to_dict()}}`；容器节点可再加工 |
| 黑板写入 | 子结果写入 `state.ns_merge(container_id, {...})`；黑板所有写操作加锁 |
| 事件流 | `parallel_start` / `parallel_end` 两个事件，Web 界面、默认的 CLI 关键事件流与 `-v` 的全量事件流都能看到 |
| 单分支失败 | 该子节点的异常被收敛为 `status="failed"` 的子结果，**不拖垮**整个并行节点；原因进 `notes` |
| 消息顺序 | 总线用 `Condition` 保证发布顺序一致；`wait_for` 用游标避免"应答先到"竞态 |

**为什么不用多进程**：Agent 是 IO 密集型，且大量共享内存状态（黑板、总线、模型统计），
多进程需要全套序列化与合并协议，收益不成比例。真实瓶颈在 LLM 的网络往返。

---

## 8. 可扩展性：加东西时改哪里

### 8.1 新增一个 Agent（三步）

```python
# ① 写类：wfa/agents/my_agent.py
from .base import Agent
from ..graph import AgentResult

class MyAgent(Agent):
    name = "my_agent"
    role = "我的 Agent"

    def __init__(self, model, *, config=None):
        self.model = model
        self.config = config

    def run(self, ctx) -> AgentResult:
        upstream = ctx.input_result("code")          # 读上游结果
        facts = ctx.state.get("task", "")            # 读共享黑板
        ctx.bus.send("task", self.name, "workflow", {"scope": "..."})   # 通知
        return AgentResult(kind="result", summary="…", data={...})

# ② 注册：wfa/agents/__init__.py 的 default_registry 里 registry.register(MyAgent(model, config=config))

# ③ 加节点：wfa/workflows/code_review.py 里 builder.agent("my_step", "my_agent") + builder.edge(...)
```

**不需要改** `engine.py` / `bus.py` / `state.py` / `report.py`。
如果复用既有 `kind`（`review`/`fix`/`doc`），连黑板顶层键的写入都是自动的。

### 8.2 新增一条 Workflow（三步）

```python
# ① 写文件：wfa/workflows/my_flow.py  用 WorkflowBuilder 声明节点与边，build() 会自动校验
# ② 登记：wfa/workflows/registry.py 的 WORKFLOWS 里加 {"builder": build_my_flow, "description": ..., "agents": [...]}
# ③ 完事：CLI `--workflow my_flow`、Web 下拉框、`graph` 命令**自动**支持
```

`docs` 流程就是这条路径的实证：它与 `code_review` 领域完全不同，
但因为共享 `engine`/`bus`/`state`，`workflows/docs.py` 只有 100 余行。

### 8.3 替换模型层（一处改动）

`ChatModel` 是一个鸭子类型协议：`chat(messages, *, tools=None, temperature=None, max_tokens=None) -> LLMResponse`。

| 实现 | 用途 | 特点 |
| --- | --- | --- |
| `HttpChatModel` | 生产（默认） | 标准库 `urllib`，OpenAI 兼容，自带重试/分类/`tools` 降级；传输层可注入 |
| `ScriptedChatModel` | 单元测试 | 按脚本返回预置响应，`responder=` 可完全接管，**零联网** |
| `RuleChatModel` | 离线演示 | 关键词 → 内置模板，确定性、可复现，**不推理** |

要换供应商：改 `.env` 的 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`。
要换协议实现：写一个新的 `ChatModel`，改 `model.build_model(cfg)` 一行。
要换官方 SDK：`WFA_TRANSPORT=sdk`（`SdkTransport` 惰性 import，未安装时给出明确提示）。

### 8.4 其它扩展点

| 想改什么 | 改哪里 |
| --- | --- |
| 新增审查规则 | `wfa/analysis.py` 的 `RULES`（每条含 id/title/severity/category/detector） |
| 改门禁口径 | `wfa/workflows/code_review.py::gate_quality`（或用 `--ignore-blocking` 关闭阻断输出） |
| 改报告格式 | `wfa/report.py::render_run_report`（纯函数拼装，易测易换） |
| 加 Web 接口 | `wfa/web/server.py::WorkflowHTTPHandler.do_GET/do_POST` + `WorkflowService` |

---

## 9. 边界与取舍

### 9.1 为什么不用 LangGraph / AutoGen / CrewAI

| 维度 | 自研（本项目） | 框架 |
| --- | --- | --- |
| 依赖 | 0（纯标准库） | 数十个传递依赖，评审环境要能装 |
| 可控性 | 出边决策、并行合并、重试、检查点**全在 `engine.py` 里**，可读可改可测 | 语义封装在黑盒里，出问题要读框架源码 |
| 可测性 | `ScriptedChatModel` + 假传输层即可全离线测试 | Mock 框架内部对象，测试脆弱 |
| 与作业要求 | 作业要求"顺序执行流程控制 + 消息传递或状态共享 + 条件分支/循环/并行" | 需要做"框架用法"的二次解释 |
| 代价 | 要自己写回合循环（约 400 行） | 少写代码，但推理链变短、可控性下降 |

**结论**：作业的评分点是"**流程编排能力**"本身，自研让这份能力**看得见**；
框架在这里是负资产。若将来要接生态，只需替换 `wfa/model.py` 与 `wfa/graph.py` 两个边界。

### 9.2 为什么用标准库 `http.server`

| 维度 | `http.server` + 单页 UI | Flask / FastAPI |
| --- | --- | --- |
| 依赖 | 无 | 需安装（评审者要 `pip install`） |
| 需求覆盖 | 5 个路由 + 静态文件 + 后台线程 + 轮询增量事件，**够用** | 需要的其实也只是这些 |
| 规模 | `server.py` 约 450 行，可通读 | 框架 + 插件反而更多 |
| 代价 | 需自己写路由、序列化、并发处理（`ThreadingHTTPServer` 已解决） | 省事，但违背"零依赖"卖点 |

**增量事件**是刻意设计的接口：`GET /api/runs/<id>?since=N` 只返回新事件，
前端用 1 秒轮询即可获得"实时"体验，而不必引入 WebSocket（会带来额外依赖与复杂度）。

### 9.3 其它取舍

| 取舍点 | 选择 | 理由 |
| --- | --- | --- |
| 离线模型是否"假装"智能 | **明确标注** `RuleChatModel` 是模板，不推理 | 诚实性；避免评审者误以为规则命中的是 LLM 的语义发现 |
| 门禁到上限仍有问题 | `decision="pass"` + 报告标注「未完全收敛」 | 流程必须能终止；"跑不完"比"跑完但标注风险"更糟 |
| 事件回调 vs 队列 | `on_event: Callable[[str, dict], None]` | 同步回调最简单；CLI 直接打印，Web 直接 append 到 `RunRecord` |
| 黑板加锁粒度 | 单把可重入锁 | 写入都是小操作，粗锁带来的争用远小于调试成本 |
| 回合 = 一个节点 | 并行容器算一个回合 | 让"回合数"与"业务步骤数"对应，报告更可读 |
| 不执行待审查代码 | `coder` 只做"写出文件 + 可选 `self_test`"，审查侧纯静态分析 | 安全；也避免演示环境里的副作用 |

---

## 10. 测试策略

标准库 `unittest`，一条命令：`python -m unittest discover -s tests -t .`

**当前状态：`Ran 516 tests ... OK`（全绿，exit 0）**，按文件分布：

| 测试文件 | 用例数 | 关注点 | 手法 |
| --- | --- | --- | --- |
| `test_util.py` | 59 | 脏 JSON 提取（代码块/前后废话/尾逗号/字符串内括号）、路径越界拒绝、编码回退、`RetryPolicy` | 纯函数 + 参数化用例；注入 `sleep=lambda _: None` |
| `test_model.py` | 61 | 401 不重试 / 429 与 5xx 重试 / `tools` 降级 / 脏参数解析 / `build_model` 分派 | **注入假传输层**，零联网 |
| `test_bus_state.py` | 51 | 发布订阅、广播可见性、`wait_for` 应答先到不丢、黑板命名空间/产物/路径安全、`save/load` 往返 | 线程用例 + 超时保护；临时工作区（`.tmp/`） |
| `test_analysis.py` | 52 | 每条规则都能触发、干净代码零误报、语法错误兜底、上限优先保留高危 | 内含缺陷的样例源码 |
| `test_graph_engine.py` | 92 | `validate()` 检出问题图、Mermaid/DOT 回边、出边决策、`when` 跳过、循环上限 → `partial`、`max_turns`、`on_error` 三态、并行合并、`checkpoint`/`resume` | `ScriptedChatModel` + 极简测试图 |
| `test_agents.py` | 76 | 各 Agent 的 `data` 契约、离线路径必须产出非空结果、修复→复审后 `blocking` 归零、HW1 适配器成功/回退两条路径 | 假模型 + 断言 `data` schema |
| `test_workflows.py` | 37 | 两个流程的拓扑与门禁口径（含 `--parallel-review` 变体、`legacy_review` 节点位置）、启动即可校验通过 | 直接构造 `Workflow` 断言节点/边 |
| `test_report.py` | 27 | 章节完整、**报告与 JSON 不含 API Key**、表格单元格转义 | 断言字符串 |
| `test_web.py` | 24 | `/api/health`、`/api/workflows`、`/api/run` + 轮询、`/report` | 真实起 `ThreadingHTTPServer` 打请求 |
| `test_config.py` | 24 | `.env` 解析（BOM/引号/行尾注释）、环境变量优先级、`WFA_LEGACY_CRA_ROOT` → `extra["cra_root"]`、`public_dict()` 不含密钥 | 临时 `.env` 文件 + `os.environ` 隔离 |
| `test_cli.py` | 13 | `list` / `graph` / `report` 子命令、退出码语义（0 / 1 / 3） | `main([...])` 直调 |

三条"让测试真而不脆"的原则：

1. **一切外部 IO 都可注入**：`HttpChatModel(transport=...)`、`RetryPolicy(sleep=...)`、
   工作区用 `tests/helpers.temp_workspace()`（放工程内 `.tmp/`，规避受限环境的临时目录权限问题）。
2. **端到端用脚本化模型**：`ScriptedChatModel` 让"完整流程"可断言，而不依赖模型输出质量。
3. **离线路径是硬约束**：有测试专门保证 `--offline` 下规则引擎结果非空——
   这是"评审者不装 Key 也能看到效果"的守门测试。

> 跑测试时建议隔离字节码缓存（`PYTHONDONTWRITEBYTECODE=1`），并**不要在测试运行期间改动 `wfa/` 或 `tests/`**：
> 旧 `__pycache__` 会造成"假失败"。平台差异上也做过处理——临时目录用工程内 `.tmp/`，
> 路径断言用 `safe_relpath` 统一正斜杠。

---

## 11. 与 Homework 1 的关系

| 维度 | Homework 1（`code-review-agent`） | Homework 2（本项目） |
| --- | --- | --- |
| 主体 | 单个 Agent 的**智能与工具使用** | 多个 Agent 的**协作与流程编排** |
| 核心 | Agent 循环：推理 → 工具调用 → 观察 → 再推理 | 图 + 引擎：顺序 / 分支 / 循环 / 并行 / 恢复 |
| 复用方式 | —— | `code_review` 里的 `legacy_review` 节点绑定 `legacy_reviewer`（`LegacyReviewAgent`），把 Homework 1 的 `CodeReviewAgent` **作为 Workflow 节点**执行 |
| 复用的具体能力 | 25 条 AST 规则、工作区扫描器、工具循环 | `LegacyReviewAgent` 把候选根目录加进 `sys.path` 后 `importlib.import_module("cra.agent")`，调 `CodeReviewAgent(cfg).review(root)`，再把 `AgentOutcome.findings` 转成统一 `Issue` |
| 根目录发现顺序 | —— | `cra_root` 参数（← `Config.extra["cra_root"]`，由 `WFA_LEGACY_CRA_ROOT` 填充）→ 环境变量 `WFA_CRA_ROOT`/`CRA_ROOT`/`HW1_ROOT` → 相对路径 `..\code-review-agent` 等四个候选 |
| 复用是否成功如何体现 | —— | 节点 `metrics["legacy"]` 与摘要里的「来源：Homework1 cra 包 / 内置规则引擎（回退）」；`legacy_files` / `legacy_findings` / `legacy_steps` / `legacy_tool_calls` / `legacy_mode` 一并进报告指标卡 |
| 复用失败的兜底 | —— | 回退 `wfa/analysis.py`（本工程内置规则引擎，规则 id 与 HW1 同源风格），节点仍成功、流程继续 |
| 经验继承 | 提示词设计、重试退避、错误分类、路径越界防护、脏 JSON 提取、`Config.public_dict()` 不泄露密钥 | 这些经验被重新实现进 `model.py` / `util.py` / `errors.py` / `config.py`，并保持零依赖 |

**关系一句话**：Homework 2 **把 Homework 1 的 Agent 变成流程里的一个节点**，
既满足"从项目 1 复用 Agent 作为 Workflow 节点"的要求，又让复用是**可选增强**而非硬依赖。

复用点与不可用回退的配置方式见 [`README.md` 第 10 节](../README.md#10-复用-homework-1-的审查-agent)。

---

## 12. 已知限制与改进方向

| 限制 | 现状 | 改进方向 |
| --- | --- | --- |
| 规则引擎只覆盖 Python | 其它语言交给 LLM 语义审查 | 引入 `tree-sitter` 或语言专用 linter 适配器 |
| 离线模型不推理 | `RuleChatModel` 是关键词模板 | 接入本地小模型（llama.cpp / Ollama）作为"离线但真推理"的第三档 |
| 并行是线程级 | `ThreadPoolExecutor`，单进程 | 需要横向扩展时引入任务队列（但会破坏零依赖） |
| 检查点是快照 | 保存整块黑板，文件会随产物变大 | 改为增量日志 + 重放（event sourcing） |
| 无成本预估 | 只能事后统计 token | 按工作区规模事前给出预算提示 |
| 修复无法自动验证 | 修复后仅由审查 Agent 复审 | 增加"改完跑测试"的闭环节点（`self_test` 已有雏形） |
| Web 无鉴权 | 只绑定 `127.0.0.1` | 需要局域网访问时加 Token 或反向代理 |

---

## 13. 模块职责速查

| 模块 | 职责 | 关键设计 |
| --- | --- | --- |
| `wfa/errors.py` | 分层异常 | `retryable` 是唯一重试判据；安全异常不可被吞 |
| `wfa/util.py` | 脏 JSON、路径安全、重试、文件读写 | 括号配对扫描（不用贪婪正则）；`commonpath` 而非 `startswith` 判越界 |
| `wfa/config.py` | `.env` 解析 + 限额集中 | CLI > 环境变量 > 默认值；`public_dict()` 永不输出密钥 |
| `wfa/model.py` | LLM 接口层 | 三实现一协议；传输层可注入；tools 自动降级 |
| `wfa/bus.py` | 消息总线 | 日志即真相；`wait_for` 游标防竞态 |
| `wfa/state.py` | 共享黑板 | 四类能力聚合；粗锁换简单；纯 JSON 持久化 |
| `wfa/graph.py` | 图与结果契约 | `WorkflowBuilder.build()` 自动校验；`AgentResult.data` 是跨 Agent 契约 |
| `wfa/analysis.py` | 确定性规则引擎 | 一次 AST 遍历；规则元信息集中；上限按严重程度优先保留 |
| `wfa/engine.py` | 编排执行器 | 回合循环 + 出边决策 + 并行 + 检查点；`on_event` 可观测 |
| `wfa/report.py` | 报告渲染 | 纯函数拼装；单元格转义；流程文档生成 |
| `wfa/cli.py` | 命令行 | 退出码语义化；Windows 控制台 UTF-8；事件打印 |
| `wfa/web/server.py` | 零依赖 Web | `ThreadingHTTPServer`；增量事件轮询；后台线程跑流程 |
