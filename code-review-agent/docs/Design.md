# 设计文档 · Code Review Agent

> 学号：2412190618 ｜ 姓名：钟伟杰 ｜ 配套代码见仓库根目录，使用说明见 `README.md`

---

## 1. 设计目标与关键取舍

| 目标 | 做法 | 取舍说明 |
| --- | --- | --- |
| 体现真正的 Agent 架构（30% 权重） | 显式实现"推理 → 工具调用 → 观察 → 再推理"循环，工具以 JSON Schema 暴露，结论由**终止型工具** `submit_review` 提交 | 不用 LangChain/AutoGen：作业允许原生 API，自研循环让协议、重试、降级完全可见可控，代码量也更小 |
| 功能完整、边界稳（40% 权重） | 规则引擎先产出**可核查的事实**，LLM 负责语义判断；所有异常路径都有确定行为（见第 11 节） | 不追求规则数量堆砌，宁可漏报也不误报（干净样例必须 0 命中，有测试守住） |
| 代码质量（20% 权重） | 分层清晰、类型注解、统一异常体系、可注入依赖（transport / sleep / client）、102 个单元测试 | 少数 CLI 命令处理函数偏长（> 40 行）是刻意保留的可读性权衡，已在自审报告中列为可接受项 |
| 文档（10% 权重） | `README.md`（使用）+ `Design.md`（设计）+ 真实运行产出的示例报告 + 演示脚本 | — |
| 零安装负担 | 只用 Python 标准库（含 `http.server` 实现 Web、`urllib` 调用 LLM、`unittest` 写测试） | 放弃 `flask` / `requests` / `openai` 的便利，换取"老师拿到就能跑" |

**一句话架构**：`确定性规则负责事实与定位，LLM 负责语义与优先级，二者合并成一份可交付报告。`

---

## 2. 总体架构

```
                    ┌───────────────────────────── CLI (cra/cli.py) ─────────────────────────────┐
                    │  review <path>   scan <path>   chat [path]   web --port                    │
                    └───────────────┬──────────────────────────┬─────────────────────────────────┘
                                    │                          │
                      ┌─────────────▼────────────┐   ┌─────────▼──────────┐
                      │  CodeReviewAgent         │   │  Web UI            │
                      │  (cra/agent.py)          │   │  (cra/web/server)  │
                      │  · Agent 循环            │   │  http.server + 单页 │
                      │  · 协议降级 / 去重护栏    │   └─────────┬──────────┘
                      └───┬───────────┬──────────┘             │
                          │           │                        │
        ┌─────────────────▼──┐   ┌────▼─────────────┐          │
        │  LLMClient         │   │  ToolRegistry    │◄─────────┘
        │  cra/llm.py        │   │  cra/tools/*     │
        │  · 重试/退避       │   │  7 个工具         │
        │  · 错误分类        │   └────┬─────────────┘
        │  · tools→JSON 降级 │        │
        └─────────┬──────────┘        │
                  │                   ├──────────────► cra/fsutil.py  (遍历/编码/路径安全)
        ┌─────────▼──────────┐        ├──────────────► cra/analysis.py (AST 25 条规则)
        │  Memory            │        └──────────────► cra/scanner.py  (预扫描)
        │  cra/memory.py     │
        │  · 短期：消息裁剪   │   ┌──────────────────┐   ┌──────────────────┐
        │  · 长期：会话落盘   │   │  prompts.py      │   │  report.py       │
        └────────────────────┘   │  提示词 / 摘要    │   │  MD / JSON 报告  │
                                 └──────────────────┘   └──────────────────┘
```

数据模型（`cra/models.py`）贯穿全链路并可直接 JSON 序列化：
`FileInfo`（文件分类）→ `ScanResult`（扫描结果）→ `Finding`（单条问题，含来源与严重程度）→ `AgentOutcome`（Agent 运行结果，含轨迹与用量）。

---

## 3. 模块职责

| 模块 | 职责 | 关键设计 |
| --- | --- | --- |
| `cli.py` | 参数解析、进度输出、退出码 | 命令处理与渲染分离；退出码语义化（0/1/2/3）便于接 CI |
| `config.py` | 默认值、`.env` 解析、忽略规则、各类限额 | 所有"魔法数字"集中一处；`public_dict()` 保证密钥永不进入报告 |
| `llm.py` | OpenAI 兼容协议调用 | 传输层可注入（测试零联网）；错误分层；自动 tools 降级 |
| `agent.py` | Agent 主循环 | 拆成 `_run_agent_loop` / `_apply_tool_calls` / `_apply_text_turn` / `_finish_online`；循环状态集中在 `_LoopState` |
| `tools/` | 工具声明、参数校验、执行与轨迹 | 工具异常一律转成"失败观察结果"回灌模型，绝不炸循环 |
| `analysis.py` | 确定性规则引擎 | 一次 AST 遍历；规则元信息集中在 `RULES` 字典；风格类聚合 |
| `scanner.py` | 工作区预扫描 | 分类 + 逐文件分析 + 统计 + 截断说明，不依赖 LLM |
| `fsutil.py` | 文件系统与编码 | 路径越界拒绝；编码嗅探；二进制识别；软链接防环 |
| `memory.py` | 上下文记忆 | 短期裁剪不变式（不产生孤儿 `tool` 消息）+ 会话持久化 |
| `prompts.py` | Prompt 设计 | 系统提示、JSON 降级协议、扫描摘要压缩（限量 4000 字符） |
| `report.py` | 报告渲染 | 结论先行；分严重程度；附录含工具轨迹；第四节写明"报告边界" |
| `web/server.py` | Web 界面 | 标准库 `ThreadingHTTPServer` + 自研 Markdown→HTML；单任务锁 |
| `jsonutil.py` | 脏 JSON 提取 | 括号配对 + 字符串感知扫描，处理代码块/前后废话/尾逗号 |

---

## 4. Agent 运行时序

```
用户                CLI             Agent              ToolRegistry        LLM API
 │  review examples  │                 │                    │                │
 ├──────────────────►│                 │                    │                │
 │                   ├─ 预扫描(无LLM) ─►│                    │                │
 │                   │                 │  ScanResult        │                │
 │                   ├─ 组装 Memory ───►│                    │                │
 │                   │                 ├──── chat(messages, tools) ─────────►│
 │                   │                 │◄─── tool_calls / content ───────────┤
 │                   │                 ├─ execute(name,args) ─►│               │
 │                   │                 │◄── observation ──────┤               │
 │                   │                 ├──── 回灌观察结果，继续推理 ──────────►│
 │                   │                 │        （最多 max_steps 轮）          │
 │                   │                 ├─ submit_review → 终止 │               │
 │                   │◄─ AgentOutcome ─┤                    │                │
 │                   ├─ 合并规则与模型发现（去重）             │                │
 │                   ├─ 渲染 Markdown/JSON 报告               │                │
 │◄─ 摘要 + 问题清单 ─┤                 │                    │                │
```

**两种工具协议**

1. **Function calling（首选）**：把 `ToolRegistry.schemas()` 交给模型，解析 `tool_calls`，用 `role=tool` 回灌观察结果。
2. **JSON 文本协议（自动降级）**：若服务端/模型不支持 tools（HTTP 400 且消息含 tool/function），`LLMClient` 会关闭 tools 并重投；Agent 检测到后把系统提示词换成 `JSON_PROTOCOL_PROMPT`，要求模型每轮只输出
   `{"action":"tool","tool":...,"arguments":{...}}` 或 `{"action":"final","summary":...,"findings":[...]}`。

**终止条件**（任一满足即收尾）：调用 `submit_review` 成功 / 文本里解析出合法结论 / 达到 `max_steps` / 重复调用护栏触发 2 次 / LLM 抛错。
前三种都会继续生成报告；最后一种走规则报告并在 `degraded_reason` 写明原因。

---

## 5. 工具层设计

```python
registry.add(name, description, parameters, handler)   # 声明
registry.execute(name, arguments, ctx, step)           # 执行（永不抛异常）
```

- **Schema 即契约**：`Tool.to_schema()` 产出标准 JSON Schema，直接给模型做 function calling。
- **参数校验与纠正**（`validate_arguments`）：模型常把 `5` 写成 `"5"`、把数组写成逗号串，这里做类型纠正；缺必填、未知参数、把对象当数组都返回**可操作的错误文案**，让模型自我修正。
- **失败即观察**：工具异常被收敛为 `[工具执行失败] ...` 文本回灌模型，而不是中断。
- **终止型工具**：`submit_review` 标记 `terminal=True`，其入参就是最终结论（比让模型输出自由文本 JSON 可靠得多）。
- **轨迹留痕**：每次调用记录步号、参数摘要、成败、耗时，报告附录 A 直接展示——这是"架构清晰度"的可见证据。

---

## 6. 上下文记忆设计

| 层 | 载体 | 作用 |
| --- | --- | --- |
| 短期记忆 | `Memory.messages` | 当前会话的完整消息序列（含工具调用与观察），超预算时从最旧开始裁剪 |
| 长期记忆 | `SessionStore`（`.cra_sessions/*.json`） | `chat --session <id>` 可继续上一次对话，跨进程保留上下文 |

**裁剪不变式**：任何时刻不得出现"孤儿 `tool` 消息"（其所属的 `assistant(tool_calls)` 被裁掉）。实现上先按字符预算从头部丢弃，再检查首条是否为 `tool` 并继续丢弃，最后对超长工具输出做正文截断。有专门的单元测试覆盖。

字符预算近似 token 预算（中文约 1 字符 ≈ 1 token，英文更省），默认 24000 字符，避免把 8 步工具输出的原文全塞回模型。

---

## 7. 错误处理与重试策略

| 故障 | 分类 | 策略 |
| --- | --- | --- |
| 401 / 403 | `LLMAuthError`（`retryable=False`） | 立即失败并提示检查 Key，不做无谓重试（CLI 退出码 2） |
| 400 / 404 / 422 | `LLMBadRequestError` | 立即失败；若错误信息提到 tools，则**关闭工具重投一次**（协议降级） |
| 429 | `LLMRateLimitError`（可重试） | 指数退避 + 抖动，尊重 `Retry-After` |
| 5xx | `LLMServerError`（可重试） | 指数退避，最多 `max_retries`（默认 3） |
| DNS/连接/超时 | `LLMNetworkError`（可重试） | 同上；超时由 `Config.timeout`（默认 60s）控制 |
| 响应结构异常 | `LLMResponseError` | 立即失败（说明服务端不兼容） |
| 工具参数非法 | `ToolResult.failure` | 作为观察结果回灌，让模型改参数 |
| 工具内部异常 | `ToolResult.failure` | 同上，循环继续 |
| 模型反复调用同一工具 | 去重护栏 | 跳过执行并提示；两次后提前收尾 |
| 达到步数上限 | 循环出口 | 用已完成调查 + 规则结果出报告，并注明 |
| 以上任一导致无法产出结论 | 降级 | 规则报告兜底，`degraded=True` + `degraded_reason` |

退避公式：`delay = min(base * 2^(attempt-1) + jitter, 20s)`，`base=1.5s`，抖动由固定种子随机数产生（**重试节奏可复现**，便于测试与排障）。

---

## 8. 静态分析引擎设计

**一次 `ast.walk` 完成 25 条规则**（`Analyzer` 为 `ast.NodeVisitor` 子类）：

- 安全类：硬编码密钥（正则 + 占位符过滤）、`eval/exec`、`shell=True`、`os.system`、`pickle/yaml.load`；
- 缺陷类：裸 `except`、静默吞异常、可变默认参数、`== None`、`type()==`、遍历中改容器；
- 可维护性：函数长度、圈复杂度（近似 McCabe）、参数个数、未使用导入、通配符导入、内置名遮蔽、`print`、TODO；
- 风格：行长、行尾空白/制表符、文件过长。

**降低误报的设计**：

1. 规则元信息（严重程度/类别/标题）集中在 `RULES`，测试逐一验证每条规则都能触发，并有一条测试保证"`RULES` 里没有未实现的幽灵规则"。
2. 风格类问题**按文件聚合成一条**（"共 37 行超过 120 字符，首行在第 12 行"）。
3. 单文件问题上限 30 条，且**按严重程度优先保留**（否则高危问题会被大量文档戳淹没），折叠数量用 `LIMIT001` 明确告知。
4. 测试文件放宽文档字符串规则；只要求**模块级公开函数/公开类**写文档（类方法与内部小函数不强制）。
5. 密钥规则过滤明显占位符（`test-key`、`<YOUR_KEY>`、短于 10 字符的值）。
6. `type(x) == T` 的检测放在名字引用收集阶段统一处理，避免重复遍历。

**分析摘要**（`FileAnalysis`）：LOC、函数数、类数、最大/平均复杂度、是否有模块文档、解析是否成功。它既进报告，也进给模型的扫描摘要。

---

## 9. 报告设计

`render_markdown` 由若干纯函数拼装（`_findings_lines` / `_stats_lines` / `_scope_lines` / `_appendix_lines`），便于测试与替换：

```
# 代码审查报告：<根目录>
  元信息：模式 / 模型 / 规模 / 耗时 / 步数 / 工具调用 / token / 降级说明
## 一、结论摘要          —— verdict + 自然语言结论 + 严重程度统计表 + 来源构成
## 二、问题清单          —— critical/high/medium/low 逐条详述（位置/类别/规则/来源/说明/建议/证据）
                             info 级压缩为表格
## 三、分类统计          —— 类别分布 + 问题最集中的文件
## 四、扫描范围与限制     —— 过滤条件、限额、跳过的文件、截断与降级说明
## 附录 A：Agent 工具调用轨迹
## 附录 B：规则命中明细
```

**为什么把"范围与限制"写进报告**：审查结论的可信度取决于边界。报告明确告诉读者哪些文件被跳过、是否触发限额、模型是否降级，读者才知道该信到什么程度。

JSON 版本（`render_json`）用于 CI：包含 findings 数组、统计、Agent 用量与扫描明细；有单元测试断言**其中不含 API Key**。

---

## 10. 安全设计

| 风险 | 防护 |
| --- | --- |
| 提示词注入诱导读取工作区外文件 | `fsutil.safe_path()` 用 `realpath` + `commonpath` 校验，越界直接抛 `PathSecurityError`（有测试） |
| 审单个文件时越界读取同目录其他文件 | `ToolContext.scope_file` + `tools.base.ensure_in_scope()`：单文件模式下所有工具只允许访问被指定的那一个文件（有单测；见第 13 节自审记录里这条真实 bug） |
| 密钥进入报告/日志 | `Config.public_dict()` 剔除 `api_key`；报告只写模型名；有单元测试断言报告与 JSON 中不含 Key |
| 密钥进入 git | `.env` 在 `.gitignore` 中，只提交 `.env.example` |
| 工具参数被模型乱填 | JSON Schema 校验 + 类型纠正 + 未知参数拒绝 |
| Web 界面被并发打爆 | 单任务锁，重复请求返回 429 |
| 输出注入到 HTML | Markdown→HTML 前先 `html.escape`（有测试验证 `<script>` 被转义） |
| 误执行待审查代码 | 本工具**只读**：不执行待审查代码、不修改被审查仓库（`submit_review` 只写内存） |

---

## 11. 边界情况设计清单

见 `README.md` 第 7 节表格。补充实现位置：

| 场景 | 实现位置 |
| --- | --- |
| 语法错误不中断 | `analysis._syntax_error_result` |
| 二进制/空/超大文件 | `fsutil.classify_file` + `tools/analyze.py` |
| 编码嗅探（UTF-8/BOM/GB18030/Big5/CP1252/latin-1 兜底） | `fsutil.decode_bytes` |
| 路径越界 | `fsutil.safe_path` |
| 忽略规则与限额 | `fsutil.walk_files` + `config.Config` |
| 软链接防环 | `fsutil.walk_files`（`seen_dirs` + 默认不跟随） |
| 重复工具调用护栏 | `agent._register_call` |
| 协议降级 | `llm.LLMClient._request` + `agent._run_agent_loop` |
| 报告"不完整"提示 | `report._scope_lines`（`scan.truncated` / `scan.notes` / `outcome.notes`） |

---

## 12. 测试策略

标准库 `unittest`，**102 个用例**，`python -m unittest discover -s tests -t .` 一条命令跑完。

| 文件 | 用例数 | 关注点 |
| --- | --- | --- |
| `test_analysis.py` | 22 | 25 条规则逐一触发、干净代码零误报、语法错误兜底、上限优先保留高危、规则目录完整性 |
| `test_fsutil.py` | 15 | 编码、二进制、越界、忽略规则、限额、路径规范 |
| `test_llm.py` | 11 | 重试/退避、401 不重试、429/5xx 重试、tools 降级、脏参数解析 |
| `test_tools.py` | 21 | 参数校验与纠正、工具错误语义、终止型工具、轨迹记录 |
| `test_agent.py` | 13 | 正常链路、观察回灌、步数上限、LLM 异常降级、JSON 协议降级、重复护栏 |
| `test_report_web.py` | 14 | 报告章节、密钥不泄露、Markdown→HTML 转义、Web 真起服务打请求 |
| `test_jsonutil_memory.py` | 12 | 脏 JSON 提取、记忆裁剪不变式、会话持久化 |

三个让测试"真而不脆"的技巧：

1. **假传输层**：`LLMClient(transport=...)` 注入脚本化响应，测试 LLM 相关逻辑完全离线、可复现重试节奏。
2. **假客户端**：`FakeClient` 模拟多轮工具调用，端到端验证 Agent 循环而不用真调 API。
3. **临时目录**：`tests/support.py` 统一用 `os.makedirs` 构造可写临时目录（不用 `mkdtemp`，规避受限环境下的权限问题），清理失败不影响结论。

---

## 13. 自我审查（Dogfooding）记录

项目完成后，用它审查自己（`python review.py scan .`），并按结果做了真实修复——这也是本工具"能用"的最好证据。

| 阶段 | 命中总数 | 严重(high) | 说明 |
| --- | --- | --- | --- |
| 首次自审 | 300 | 15 | 发现 9 处未使用导入、`_online_review` 150 行/复杂度 48、密钥规则误报测试夹具等 |
| 修复后 | **153** | **9** | `cra/` 目录下 **0 条高危**；剩余 9 条全部来自 `examples/` 里故意写坏的演示文件（8 条）与规则自测夹具（1 条） |

主要修复项：

1. **未使用导入** 9 处 → 全部清理（该规则命中的正是 `time` / `relpath` / `Memory` / `render_json` 等）。
2. **`agent._online_review` 150 行、圈复杂度 48** → 拆分为 `_run_agent_loop` / `_apply_tool_calls` / `_apply_text_turn` / `_apply_json_tool_call` / `_finish_online`，状态收敛到 `_LoopState`。
3. **`analysis.visit_Call` 77 行 / 复杂度 26** → 拆成 `_check_dynamic_execution` / `_check_owner_calls` / `_check_shell_true` 三个职责单一的方法。
4. **`report.render_markdown` 178 行** → 拆成 6 个纯函数（`_findings_lines` / `_finding_block` / `_info_table` / `_stats_lines` / `_scope_lines` / `_appendix_lines`）。
5. **密钥规则误报**（把 `test-key`、测试夹具当成真密钥）→ 增加占位符与长度过滤；测试代码改用字符串拼接构造密钥，避免自伤。
6. **文档字符串噪声 154 条** → 规则收紧为"只要求模块级公开函数/公开类"，测试文件豁免。
7. **`_collect_extra_references` 复杂度 12**、`analysis.analyze_source` 77 行 → 拆出 `_syntax_error_result` / `_parser_failure_result` / `_fill_analysis_summary` / `_cap_findings`。
8. **单文件审查的作用域逃逸（实测发现的真 bug）**：用它审一个 C 文件时，Agent 顺手把该文件所在目录（当时是桌面）的其他文件也读了、还对这些文件做了密钥检索。根因是"扫描只扫一个文件，但工具作用域仍指向父目录"。修复：给 `ScanResult` / `ToolContext` 增加 `scope_file`，所有工具经 `ensure_in_scope()` 校验，越界返回可解释的失败观察结果；同时提示词与摘要明确写出"单文件模式"。新增 13 个单测/回归测试（总测试数 104 → 117）。
9. **`analyze_python` 对非 Python 文件误报**：把 `.c` 交给 Python 解析器只会得到一条"语法错误"假发现（模型得自己识破）。改为识别扩展名后明确拒绝并引导改用 `read_file`。
10. **`scan_directory` 的 `files_scanned` 恒为 0**：该工具以 `include_files=False` 扫描后又去统计文件数，数值无意义。改为 `include_files=True`（文件清单只用于计数，不进入上下文）。

**保留的"可接受"项**（刻意不改）：CLI 命令处理函数（`cmd_review` / `cmd_chat`）约 70 行，属于流程编排，拆碎反而降低可读性；`MAINT003`（参数 6~7 个）多为带默认值的公开 API，聚合反而增加使用成本；`analysis.py` 692 行（`STYLE003`）是因为规则目录与实现集中在一起便于对照阅读。

---

## 14. 性能与成本

- **预扫描**：53 个文件 / 650KB 约 0.2s（纯 AST，无网络）。
- **一次完整在线审查**（10 个文件规模）：7~8 次模型请求、2 万~5 万 token、15~25s。
- **可调旋钮**：`--max-steps`（推理轮数）、`--max-files` / `--max-file-bytes`（扫描规模）、`--focus`（缩小范围）、`scan` 子命令（0 成本）。
- **上下文控制**：扫描摘要限 4000 字符；单次工具输出限 6000 字符；记忆预算 24000 字符；单文件问题上限 30 条。

---

## 15. 已知限制与改进方向

| 限制 | 现状 | 改进方向 |
| --- | --- | --- |
| 规则仅覆盖 Python | 其他语言交给 LLM 语义审查 | 引入 `tree-sitter` 或语言专用 linter 适配器 |
| 圈复杂度为近似值 | AST 判定点计数 | 引入精确的 McCabe 实现或复用 `radon` |
| 无跨文件数据流分析 | 单文件规则 + LLM | 增加调用图与污点分析（面向安全规则） |
| 修复建议无法自动验证 | 只给出建议文本 | 增加"生成 patch → 运行测试"的闭环工具 |
| 单机单进程 | Web 界面单任务锁 | 引入任务队列与并发额度控制 |
| 成本不可预估 | 事后统计 token | 事前按文件规模估算并给出预算提示 |

---

## 16. 代码规模

| 区域 | 文件数 | 行数 | 说明 |
| --- | --- | --- | --- |
| `cra/`（Agent 与规则引擎） | 15 | 4 984 | 主实现 |
| `tests/`（单元测试） | 8 | 1 192 | 102 个用例 |
| `examples/`（演示样例） | 9 | 3 875 | 含 3602 行自动生成文件（边界演示） |
| 入口脚本 | 2 | 41 | `review.py` / `webui.py` |
| **合计** | **40** | **10 092** | 另有 `README.md` / `Design.md` / 示例报告 / 演示脚本 |
