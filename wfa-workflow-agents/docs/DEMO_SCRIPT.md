# 演示脚本 · wfa（多 Agent 协作的流程编排）

> **时长**：1 分 30 秒 ~ 2 分钟（正常语速）
> **前置**：Python 3.9+；**不需要**安装依赖、**不需要** API Key、**不需要**联网
> **本文所有"预期屏幕输出"都是真实运行结果**（Windows 11 / Python 3.13 / 项目根目录 `G:\DeepSeek工程\wfa-homework2`）
> 重新演示前先执行第 0 节的清理命令，否则工作区里会残留上次的产物。

---

## 0. 演示前准备（10 秒，不计入时长）

```bat
cd /d G:\DeepSeek工程\wfa-homework2
```

> Windows 的 `cd` **不换盘符**，必须用 `cd /d`；或者直接用 `wfa.cmd` / `start-web.cmd`，它们内部已经 `cd /d "%~dp0"`。

清掉上次的产物，让演示"从零开始"：

```bat
rmdir /s /q examples\workflow_workspace
rmdir /s /q examples\docs_workspace
```

**讲解词**：
> "这是 Homework 2 的项目 `wfa`。它把『需求→设计→代码』和『编码→审查→修复』两条流程合成一条端到端的多 Agent 协作流程。
> 零第三方依赖，纯标准库 —— 所以这里我不装任何东西、也没配 Key，全程离线。"

---

## 1. 有哪些 Agent 和流程？—— `list`（10 秒）

```bat
python run.py list
```

**预期屏幕输出（逐字，EXIT=0）**：

```text
wfa v1.0.0 —— 可用的 Agent 与 Workflow

Agent：
  - coder                  编码 Agent
  - designer               设计 Agent
  - documenter             文档 Agent
  - fixer                  修复 Agent
  - legacy_reviewer        Homework1 代码审查 Agent（cra 适配器）
  - maintainability_reviewer 可维护性审查 Agent
  - planner                大纲 Agent
  - proofreader            审校 Agent
  - requirements           需求 Agent
  - reviewer               代码审查 Agent
  - security_reviewer      安全审查 Agent
  - writer                 文档 Agent

Workflow：
  - code_review    编码 → 审查 → 修复 的软件工程闭环流程（含需求/设计前置与文档产出）
      节点 11 个：start, requirements, design, code, legacy_review, review, gate_quality, fix, docs, report, end
  - docs           主题 → 大纲 → 内容 → 审校 的内容创作流程（规划/写作/审校三个 Agent）
      节点 7 个：start, plan, write, proofread, gate_doc, publish, end
```

**讲解词**：
> "12 个 Agent、2 条流程。`code_review` 这 11 个节点里，**顺序、条件分支、循环**都有；
> 再加 `--parallel-review` 还会多一个**并行**容器节点。
> 注意 `legacy_review` 那个节点 —— 它绑定的是从 Homework 1 复用来的审查 Agent；
> 另外 `documenter` 和 `writer` 是两个不同角色的文档 Agent（一个给代码流程收尾，一个给写作流程写正文）。"

---

## 2. 主角：离线跑完整的「编码 → 审查 → 修复」闭环（40 秒）

```bat
python run.py run --workflow code_review --offline --verbose
```

> 前提：本机的 Homework 1 工程能被发现（本机是 `..\code-review-agent`，零配置）。
> 如果演示机**没有** Homework 1 工程，只有 `legacy_review` 那一行的摘要会变成
> `审查完成（来源：内置规则引擎（回退））：…`、问题统计里的 `low` 数量可能为 1，
> **其余行与回合数完全一致**（节点自己回退，不报错）。

**预期屏幕输出（逐字完整输出，EXIT=0）**：

```text
wfa v1.0.0 | 工作流 code_review | mode=offline model=deepseek-chat max_turns=50 max_iterations=3 workspace_writes=on
工作区：G:\DeepSeek工程\wfa-homework2\examples\workflow_workspace
  ▶ turn_start       回合 1/50
  · node_start       [start] agent=- 迭代=0
  ✓ node_end         [start] status=ok decision=- start 完成
  ◀ turn_end         {"turn": 1, "node": "start", "status": "ok", "decision": "", "next": "requirements", "duration": 0.0}
  ▶ turn_start       回合 2/50
  · node_start       [requirements] agent=requirements 迭代=0
  ✓ node_end         [requirements] status=ok decision=continue 已把任务拆成可执行的软件需求（离线模板）。
  ◀ turn_end         {"turn": 2, "node": "requirements", "status": "ok", "decision": "continue", "next": "design", "duration": 0.0012}
  ▶ turn_start       回合 3/50
  · node_start       [design] agent=designer 迭代=0
  ✓ node_end         [design] status=ok decision=continue 已给出模块划分与接口设计（离线模板）。
  ◀ turn_end         {"turn": 3, "node": "design", "status": "ok", "decision": "continue", "next": "code", "duration": 0.0011}
  ▶ turn_start       回合 4/50
  · node_start       [code] agent=coder 迭代=0
  ✓ node_end         [code] status=ok decision=continue 编码完成：产出 1 个文件、2 个测试文件，自测返回码 1。
  ◀ turn_end         {"turn": 4, "node": "code", "status": "ok", "decision": "continue", "next": "legacy_review", "duration": 0.2579}
  ▶ turn_start       回合 5/50
  · node_start       [legacy_review] agent=legacy_reviewer 迭代=0
  ✓ node_end         [legacy_review] status=ok decision=retry 审查完成（来源：Homework1 cra 包）：critical=0, high=2, medium=0, low=2, info=3，阻断 2 条，结论 request_changes。
  ◀ turn_end         {"turn": 5, "node": "legacy_review", "status": "ok", "decision": "retry", "next": "review", "duration": 0.0368}
  ▶ turn_start       回合 6/50
  · node_start       [review] agent=reviewer 迭代=0
  ✓ node_end         [review] status=ok decision=retry 审查完成：扫描 5 个文件，规则命中 7 条，合并后 7 条问题（critical=0, high=2，阻断 2 条），结论 request_changes。
  ◀ turn_end         {"turn": 6, "node": "review", "status": "ok", "decision": "retry", "next": "gate_quality", "duration": 0.0021}
  ▶ turn_start       回合 7/50
  · node_start       [gate_quality] agent=- 迭代=0
  ✓ node_end         [gate_quality] status=ok decision=retry 门禁未通过：阻断级问题 2 条（迭代 1/3），转交修复 Agent。
  ◀ turn_end         {"turn": 7, "node": "gate_quality", "status": "ok", "decision": "retry", "next": "fix", "duration": 0.0}
  ▶ turn_start       回合 8/50
  · node_start       [fix] agent=fixer 迭代=0
  ✓ node_end         [fix] status=ok decision=continue 修复完成：生成 5 条 patch，已修 4 条，未修 3 条，写回 1 个文件。
  ◀ turn_end         {"turn": 8, "node": "fix", "status": "ok", "decision": "continue", "next": "review", "duration": 0.0023}
  ▶ turn_start       回合 9/50
  · node_start       [review] agent=reviewer 迭代=0
  ✓ node_end         [review] status=ok decision=pass 审查完成：扫描 5 个文件，规则命中 1 条，合并后 2 条问题（critical=0, high=0，阻断 0 条），结论 pass_with_comments。
  ◀ turn_end         {"turn": 9, "node": "review", "status": "ok", "decision": "pass", "next": "gate_quality", "duration": 0.0028}
  ▶ turn_start       回合 10/50
  · node_start       [gate_quality] agent=- 迭代=0
  ✓ node_end         [gate_quality] status=ok decision=pass 门禁通过：无阻断级问题（verdict=pass_with_comments，累计问题 {'critical': 0, 'high': 0, 'medium': 0, 'low': 2, 'info': 0}）。
  ◀ turn_end         {"turn": 10, "node": "gate_quality", "status": "ok", "decision": "pass", "next": "docs", "duration": 0.0}
  ▶ turn_start       回合 11/50
  · node_start       [docs] agent=documenter 迭代=0
  ✓ node_end         [docs] status=ok decision=pass 已按大纲生成文档（离线模板）。
  ◀ turn_end         {"turn": 11, "node": "docs", "status": "ok", "decision": "pass", "next": "report", "duration": 0.0008}
  ▶ turn_start       回合 12/50
  · node_start       [report] agent=- 迭代=0
  ✓ node_end         [report] status=ok decision=done 流程结束：共 2 条问题，修复 4 条，未修复 3 条。
  ◀ turn_end         {"turn": 12, "node": "report", "status": "ok", "decision": "done", "next": "end", "duration": 0.0001}
  ▶ turn_start       回合 13/50
  · node_start       [end] agent=- 迭代=0
  ✓ node_end         [end] status=ok decision=- end 完成
  ◀ turn_end         {"turn": 13, "node": "end", "status": "ok", "decision": "", "next": null, "duration": 0.0}
  ■ stop             reason=completed status=success

状态：success | 回合：13 | 耗时：0.3063s | 停止原因：completed
问题：{'critical': 0, 'high': 0, 'medium': 0, 'low': 2, 'info': 0} | 消息：22 | 产物：8
报告：G:\DeepSeek工程\wfa-homework2\docs\run_report.md
```

> **关于数值浮动**：上面是逐字输出，其中 `duration` 与"耗时"随机器/磁盘状态小幅浮动（本机 0.27~0.30s），
> **回合数、决策、问题统计、产物数完全一致** —— 离线模式下整条流程是确定性的。
>
> **关于 `--verbose`**：不加 `-v` 时只打印**关键事件**（`node_start` / `node_end` / `node_failed` / `stop` / `parallel_start`），
> 输出更短；加了 `-v` 才打印全量事件（含每回合的 `turn_end` JSON），并把 `wfa.*` 的 INFO 日志打到 stderr。
> 下面演示用的是 `-v`，因为要展示"回合"这一层。

**讲解词（重点，手指点这四行）**：
> "看这四行 ——
> 第 5 回合 `legacy_review`：这是**从 Homework 1 复用来的审查 Agent**，摘要里直接写着
> `来源：Homework1 cra 包`，它把 Homework 1 的扫描器 + 25 条 AST 规则 + 工具循环当成本流程的一个节点在跑；
> 第 6 回合 `review` 是 `decision=retry`：审查发现 **2 条阻断级问题**（high=2）；
> 第 7 回合 `gate_quality` 也是 `decision=retry`：**门禁把流程打回修复节点**（迭代 1/3）；
> 第 8 回合 `fix` 修完、第 9 回合**回到 `review` 复审**，问题从 7 条降到 2 条、阻断归零，第 10 回合门禁才 `pass`。
> `review → gate_quality → fix → review` 这条回边就是**循环**，`--max-iterations 3` 是它的保险丝。"

> **复用是可降级的**：`legacy_review` 只有在能找到 Homework 1 工程时才真的调用 cra 包
> （本机是零配置发现的：Homework 1 就在 `..\code-review-agent`）。
> 找不到时它会自动回退到内置的 `wfa/analysis.py` 规则引擎，摘要变成
> `审查完成（来源：内置规则引擎（回退））：…`，**流程照旧跑通、退出码不变**。

**可选加深（时间够时）**：把保险丝拧到 1 圈，看"没收敛也收尾 + 退出码"：

```bat
python run.py run --workflow code_review --offline --max-iterations 1 --verbose
```

**预期关键输出（逐字，注意最后的 EXIT=3）**：

```text
  ✓ node_end         [legacy_review] status=ok decision=retry 审查完成（来源：Homework1 cra 包）：critical=0, high=2, medium=0, low=2, info=3，阻断 2 条，结论 request_changes。
  ✓ node_end         [review] status=ok decision=retry 审查完成：扫描 5 个文件，规则命中 7 条，合并后 7 条问题（critical=0, high=2，阻断 2 条），结论 request_changes。
  ✓ node_end         [gate_quality] status=ok decision=pass 已达到迭代上限 1，仍有阻断级问题 2 条；结束修复循环并在报告中标注「未完全收敛」。
  ✓ node_end         [report] status=ok decision=done 流程结束：共 7 条问题，修复 0 条，未修复 0 条。
  ■ stop             reason=completed status=success

状态：success | 回合：10 | 耗时：0.3069s | 停止原因：completed
问题：{'critical': 0, 'high': 2, 'medium': 0, 'low': 2, 'info': 3} | 消息：17 | 产物：7
报告：G:\DeepSeek工程\wfa-homework2\docs\run_report.md
存在阻断级问题 2 条（详见报告的问题清单）
```
```text
EXIT=3
```

> 两个值得讲的点：
> 1. **流程能终止**：迭代额度用尽时，门禁不再打回修复，而是 `decision=pass` 放行并在报告里标注「未完全收敛」——
>    这样"跑不完"不会变成"卡死"。
> 2. **退出码语义化**：终态是 `success`（流程确实跑到 `end` 了），但仍有 2 条阻断级问题，
>    所以 CLI 返回 **3** 而不是 0 —— 可以直接接进 CI 当质量门禁。
>    `扫描 N 个文件` 的数量取决于工作区里当时有多少文件，会随演示前后的清理状态变化。

---

## 3. 并行：安全线 + 可维护性线同时跑（15 秒）

```bat
python run.py run --workflow code_review --parallel-review --offline --verbose
```

**预期屏幕输出（关键片段，EXIT=0；`turn_end` JSON 行已省略）**：

```text
  ✓ node_end         [code] status=ok decision=continue 编码完成：产出 1 个文件、2 个测试文件，自测返回码 1。
  ▶ turn_start       回合 5/50
  · node_start       [legacy_review] agent=legacy_reviewer 迭代=0
  ✓ node_end         [legacy_review] status=ok decision=retry 审查完成（来源：Homework1 cra 包）：critical=0, high=2, medium=0, low=2, info=3，阻断 2 条，结论 request_changes。
  ▶ turn_start       回合 6/50
  · node_start       [parallel_review] agent=- 迭代=0
  ⑃ parallel_start   [parallel_review] 子节点 ['security_review', 'maintainability_review']
  · node_start       [security_review] agent=security_reviewer 迭代=0
  · node_start       [maintainability_review] agent=maintainability_reviewer 迭代=0
  ✓ node_end         [security_review] status=ok decision=retry 审查完成：扫描 5 个文件，规则命中 7 条，合并后 1 条问题（critical=0, high=1，阻断 1 条），结论 request_changes。
  ✓ node_end         [maintainability_review] status=ok decision=pass 审查完成：扫描 5 个文件，规则命中 7 条，合并后 5 条问题（critical=0, high=0，阻断 0 条），结论 pass_with_comments。
  ⑃ parallel_end     [parallel_review] {"node": "parallel_review", "turn": 6, "duration": 0.0043, "succeeded": 2, "failed": 0, ...
  ✓ node_end         [parallel_review] status=ok decision=pass 并行节点 parallel_review：2/2 个子节点完成
  ▶ turn_start       回合 7/50
  · node_start       [review] agent=reviewer 迭代=0
  ✓ node_end         [review] status=ok decision=retry 审查完成：扫描 5 个文件，规则命中 7 条，合并后 7 条问题（critical=0, high=2，阻断 2 条），结论 request_changes。
```

**讲解词**：
> "`--parallel-review` 在复用来的 `legacy_review` 之后，把审查再拆成两条独立专线：
> 安全线只报安全类问题、可维护性线只报可维护性/风格类问题。
> `parallel_start` / `parallel_end` 两个事件说明它们是**同一回合内真并发**（线程池）跑的，
> 两条线各自给出 `request_changes` 和 `pass_with_comments`，合并后交给主审查 Agent 汇总成 2 条阻断问题。
> 单条线挂掉不会拖垮整条流程 —— 失败信息会进 `notes`。"

> 这一版是 **14 回合**（比默认多一个并行容器回合），终态同样 `success` 且**收敛**：
> `状态：success | 回合：14 | 耗时：0.3172s | 停止原因：completed`，
> `问题：{'critical': 0, 'high': 0, 'medium': 0, 'low': 2, 'info': 0} | 消息：25 | 产物：8`，**EXIT=0**。
> 注意并行线把「安全类」和「可维护性类」拆开报，合并后与单线审查的结论一致（都收敛到 `low=2`、阻断归零），
> 说明拆线不会漏报 —— 这是"并行不降低审查质量"的直接证据。

---

## 4. 同一套引擎跑另一个领域：`docs` 流程（20 秒）

```bat
python run.py run --workflow docs --offline --task "写一份多 Agent 协作说明" --workspace examples\docs_workspace --verbose
```

> 加 `--workspace` 是为了让这段演示的产物与上一段**互不干扰**；
> 不加也能跑（默认工作区与 `code_review` 共用，只是规则扫描会把上次留下的代码也算进去）。

**预期屏幕输出（逐字完整输出，EXIT=0）**：

```text
wfa v1.0.0 | 工作流 docs | mode=offline model=deepseek-chat max_turns=50 max_iterations=3 workspace_writes=on
工作区：G:\DeepSeek工程\wfa-homework2\examples\docs_workspace
  ▶ turn_start       回合 1/50
  · node_start       [start] agent=- 迭代=0
  ✓ node_end         [start] status=ok decision=- start 完成
  ◀ turn_end         {"turn": 1, "node": "start", "status": "ok", "decision": "", "next": "plan", "duration": 0.0001}
  ▶ turn_start       回合 2/50
  · node_start       [plan] agent=planner 迭代=0
  ✓ node_end         [plan] status=ok decision=continue 已生成文档大纲（离线模板）。
  ◀ turn_end         {"turn": 2, "node": "plan", "status": "ok", "decision": "continue", "next": "write", "duration": 0.0012}
  ▶ turn_start       回合 3/50
  · node_start       [write] agent=writer 迭代=0
  ✓ node_end         [write] status=ok decision=pass 已按大纲生成文档（离线模板）。
  ◀ turn_end         {"turn": 3, "node": "write", "status": "ok", "decision": "pass", "next": "proofread", "duration": 0.0008}
  ▶ turn_start       回合 4/50
  · node_start       [proofread] agent=proofreader 迭代=0
  ✓ node_end         [proofread] status=ok decision=done 审校完成：检查 1 份文档，给出 0 条修订意见。（产物 1 份，写入 1 份）
  ◀ turn_end         {"turn": 4, "node": "proofread", "status": "ok", "decision": "done", "next": "gate_doc", "duration": 0.0009}
  ▶ turn_start       回合 5/50
  · node_start       [gate_doc] agent=- 迭代=0
  ✓ node_end         [gate_doc] status=ok decision=pass 审校通过（verdict=pass），进入发布。
  ◀ turn_end         {"turn": 5, "node": "gate_doc", "status": "ok", "decision": "pass", "next": "publish", "duration": 0.0}
  ▶ turn_start       回合 6/50
  · node_start       [publish] agent=- 迭代=0
  ✓ node_end         [publish] status=ok decision=done 已发布 1 份文档，共 211 字符。
  ◀ turn_end         {"turn": 6, "node": "publish", "status": "ok", "decision": "done", "next": "end", "duration": 0.0}
  ▶ turn_start       回合 7/50
  · node_start       [end] agent=- 迭代=0
  ✓ node_end         [end] status=ok decision=- end 完成
  ◀ turn_end         {"turn": 7, "node": "end", "status": "ok", "decision": "", "next": null, "duration": 0.0}
  ■ stop             reason=completed status=success

状态：success | 回合：7 | 耗时：0.0036s | 停止原因：completed
问题：{} | 消息：11 | 产物：4
报告：G:\DeepSeek工程\wfa-homework2\docs\run_report.md
```

**产物落盘**（`examples\docs_workspace` 下，真实文件）：

```text
README.md                    ← 写作 Agent 产出的正文
docs\outline.md              ← 规划 Agent 的大纲
docs\proofread_notes.md      ← 审校 Agent 的意见
```

**讲解词**：
> "这条流程跟代码没关系 —— 规划、写作、审校三个 Agent。
> 但引擎、消息总线、共享黑板、报告**一行都没改**：换的只是 Agent 和拓扑。
> 这就是可扩展性 —— 新增领域 = 写 Agent + 用 `WorkflowBuilder` 声明图。"

---

## 5. Web 界面：拓扑可视化 + 实时日志（20 秒）

```bat
start-web.cmd
```

> 等价于 `python webui.py --port 8765`（会自动打开浏览器）。手动访问：

```text
http://127.0.0.1:8765
```

**界面上依次指出四点**（下表的接口响应是真实抓取的）：

| 看什么 | 真实证据 |
| --- | --- |
| 流程下拉框 + 拓扑图 | `GET /api/workflows` → `code_review, docs`；`code_review` 的节点为 `start, requirements, design, code, review, gate_quality, fix, docs, report, end`，并返回同源 Mermaid 源码 |
| Agent 清单 | `GET /api/agents` → `coder, designer, documenter, fixer, legacy_reviewer, maintainability_reviewer, planner, proofreader, requirements, reviewer, security_reviewer, writer` |
| 点「运行」 | `POST /api/run` → `{"run_id": "run-34217a33", "status": "running"}`（后台线程执行，立即返回；`run_id` 每次不同） |
| 实时日志 + 报告 | `GET /api/runs/<id>?since=N` 增量事件（实测 `event_count=49`、`status=success`、`turns=12`）；`GET /api/runs/<id>/report` 返回 `text/markdown` 报告（3023 字节） |

**讲解词**：
> "Web 界面零依赖：标准库 `http.server` + 一个单页 HTML，没有 Flask，也没有前端构建。
> 点运行，日志区就是引擎 `on_event` 的事件流 —— 前端每秒用 `?since=N` 只取**增量**，所以不刷屏。
> 页面上的拓扑图跟 `docs/FLOW.md` 是同一个来源。"

> 端口被占用就换：`python webui.py --port 8899`，地址里的端口要一起改。
> 演示时地址**必须带端口**，只输 `127.0.0.1` 会连到 80 端口然后失败。

---

## 6. 看报告：协作轨迹一目了然（15 秒）

```bat
notepad docs\run_report.md
```

> 也可以用 JSON 重新渲染一遍（证明报告是"从结构化数据生成的"，不是拼字符串）：

```bat
python run.py report --json docs\run_report.json
```

**报告结构（章节名就是真实输出）**：

| 章节 | 内容 |
| --- | --- |
| 1. 运行概览 | 工作流、任务、工作区、状态、回合数、耗时、消息数、问题总数、模型（离线时写明 `offline-rule-model`） |
| 1.1 指标 | 指标卡：`artifacts` / `blocking` / `duration` / `iterations` / `llm`（模型调用次数与 token）… |
| 2. 最终问题清单 | 每条问题的严重程度、类别、`文件:行号`、问题、建议；详情可折叠展开 |
| 3. 修复明细 | 修复 Agent 的 patch：文件、关联 issue、修复说明 |
| 4. Agent 协作轨迹 | **4.1 节点时间线**（回合/节点/Agent/类型/状态/耗时/结论）+ **4.2 消息总线**（逐条消息） |
| 5. 产物清单 | 产物名称、类型、产出者、大小（本次真实运行 8 份：`docs/requirements.md`、`docs/design.md`、`code/plan.md`、`review/legacy_reviewer.md`、`review/reviewer.md`、`fix/fix_report.md`、`README.md`、`report/run_summary.md`） |
| 6. 备注与告警 | **有备注时才出现**：例如"存在 N 个阻断级问题（stop_on_blocking=True）""已从检查点恢复""节点 X 第 N 次重试" |

**讲解词**：
> "报告里最有说服力的是第 4 节：**节点时间线**说明流程到底怎么走的，
> **消息总线**逐条列出 Agent 之间传了什么消息。
> 也就是说『多 Agent 协作』不是口头说的，是被完整记录下来的。"

**收尾一句话**：
> "总结：**顺序、条件分支、循环、并行**四种流程控制全在；Agent 之间靠**消息总线**通信、靠**共享黑板**交换产物；
> 支持**失败重试、降级、检查点续跑**；还复用了 Homework 1 的审查 Agent 作为流程里的一个节点。
> 全部零依赖、离线可跑。"

---

## 附 A：完整时间轴

| 时间 | 步骤 | 命令 | 讲什么 |
| --- | --- | --- | --- |
| 0:00–0:10 | ① 清单 | `python run.py list` | 12 个 Agent、2 条流程、11 个节点 |
| 0:10–0:50 | ② 离线闭环 | `python run.py run -w code_review --offline --verbose` | **门禁 retry + 修复循环**（核心） |
| 0:50–1:05 | ③ 并行 | 加 `--parallel-review` | 两条审查线并发 + 结果合并 |
| 1:05–1:25 | ④ 换领域 | `python run.py run -w docs -t "..." --workspace examples\docs_workspace` | 可扩展性：同一引擎、新领域 |
| 1:25–1:45 | ⑤ Web | `start-web.cmd` | 拓扑可视化 + 增量事件流 |
| 1:45–2:00 | ⑥ 报告 | `notepad docs\run_report.md` | 协作轨迹被完整记录 |

> 只有 1 分钟时的最小演示：② + ⑥（`list` 一句带过；Web 只说"另外还有零依赖界面"）。

---

## 附 B：如果现场翻车

| 现象 | 处理 |
| --- | --- |
| `python: command not found` | 换 `py run.py ...`，或确认 Python 已加入 PATH |
| 中文乱码 | `chcp 65001` 后重跑；`run.py` 启动时也会自动把 stdout/stderr 切到 UTF-8 |
| `cd G:\...` 没反应（提示符还在 C 盘） | 用 `cd /d G:\...`，或直接 `wfa.cmd <子命令>` |
| 浏览器打不开页面 | 地址必须带端口 `http://127.0.0.1:8765`；端口被占用就换 `--port 8899` |
| 想展示真实 LLM 能力 | 复制 `.env.example` 为 `.env` 填 Key，然后加 `--online`；**离线模式不联网**，现场断网也能演示 |
| 上次运行结果干扰演示 | 删掉 `examples\workflow_workspace` 与 `examples\docs_workspace` 后重跑 |
| 屏幕太窄、事件流换行难看 | 事件流（`turn_start` / `node_start` / `turn_end`…）**默认就会打印**；`--verbose` 只是额外把 `wfa.*` 的 INFO 日志打到 stderr。想少看输出就改用 `--workflow docs`（7 回合）或 `| more` |

---

## 附 C：一条命令看拓扑（可选补充）

```bat
python run.py graph --workflow code_review --format md
python run.py graph --workflow code_review --format md --parallel-review
python run.py graph --workflow docs --format md
```

> 这三条命令的输出就是 [`docs/FLOW.md`](FLOW.md) 的第 1、2、3 节 —— 该文件由这三条命令**真实生成**。
> 演示时如果想强调"图不是手画的、是代码里声明的"，可以直接跑其中一条。
