# Code Review Agent · 代码审查 Agent

> Homework 1 提交项目
>
> 一个用 **Python 标准库 + LLM 原生 API** 实现的代码审查 Agent：
> 自己决定调用哪些工具、读哪些文件、如何验证结论，最后产出一份可交付的审查报告。

---

## 1. 这是什么

给定一个代码库目录，Agent 会：

1. **先做确定性预扫描**（Python AST + 正则），拿到"事实"：文件清单、复杂度、异常处理、硬编码密钥、命令注入风险……
2. **再让 LLM 通过工具调用去核实与深挖**：读文件、检索、单文件深挖、目录统计，多轮推理直到形成结论；
3. **最后产出报告**：按严重程度分级的问题清单（含位置、依据、修复建议），附 Agent 的工具调用轨迹与扫描范围说明。

亮点：

- **不是"prompt 套壳"**：有真正的 Agent 循环（推理 → 工具调用 → 观察 → 再推理），有终止型工具 `submit_review`，有步数上限、重复调用护栏、协议降级。
- **规则 + LLM 双引擎**：规则负责事实与定位（不编造行号），LLM 负责语义判断（逻辑错误、设计问题）。
- **零第三方依赖**：只用标准库，`python review.py <目录>` 就能跑，评审时不需要 `pip install` 任何东西。
- **边界情况是显式设计的**：二进制文件、空文件、语法错误、GBK 编码、超大文件、路径越界、API 限流/超时/不支持工具，全部有确定行为（见第 7 节）。
- **可离线降级**：没有 API Key 或网络异常时，自动退化为静态规则报告，永远有结果可用。

---

## 2. 快速开始

### 2.1 环境要求

- Python **3.9+**（开发与验证环境：Python 3.13 / Windows 11）
- **无需安装任何第三方包**（`requirements.txt` 里没有必需依赖）

### 2.2 配置 API Key

```bash
# 复制模板，填入自己的 Key（获取地址 https://platform.deepseek.com/api_keys）
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

`.env` 内容：

```ini
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

> `.env` 已在 `.gitignore` 中，不会被提交；报告与日志中也绝不包含密钥（有单元测试专门守这条线）。

### 2.3 四条命令

```bash
# ① 审查一个目录，生成 Markdown + JSON 报告
python review.py examples --out docs/sample_report.md --json-out docs/sample_report.json

# ② 只看确定性静态规则（不联网、不花钱）
python review.py scan examples --top 20

# ③ 交互式问答：针对代码库提问，带上下文记忆
python review.py chat examples
python review.py chat examples --question "buggy_service.py 里最危险的问题是什么？"

# ④ 轻量 Web 界面（默认自动打开浏览器；只输入 127.0.0.1 不行，要带端口）
python webui.py --port 8765          # → http://127.0.0.1:8765
python webui.py --port 8765 --no-open  # 不自动开浏览器
```

没有 Key 也想看效果？加 `--offline`：

```bash
python review.py examples --offline --out offline_report.md
```

### 2.4 Windows 一键启动（可选）

不想记命令、也不想处理盘符切换，直接双击或用 cmd 运行项目根目录下的两个脚本：

| 脚本 | 作用 |
| --- | --- |
| `start-web.cmd` | 自动切到项目目录并启动 Web 界面，然后打开浏览器 |
| `review.cmd <路径> [参数]` | 在**任意目录**下审查指定目录，例如 `review.cmd "D:\proj" scan` |

---

## 3. 命令与参数

| 命令 | 作用 | 常用参数 |
| --- | --- | --- |
| `review` | 完整审查（规则 + LLM Agent）并生成报告 | `--out`、`--json-out`、`--focus`、`--max-steps`、`--include/--exclude`、`--offline`、`--model`、`--fail-on` |
| `scan` | 仅确定性静态扫描（无 LLM、秒级） | `--json-out`、`--top`、`--max-files` |
| `chat` | 就代码库交互式问答（`/help` 查看命令） | `--question`、`--session`（继续上次会话） |
| `web` | 启动零依赖 Web 界面 | `--host`、`--port`、`--no-open`、`--offline` |

几个例子：

```bash
# 只关注安全，最多 6 步推理
python review.py . --focus security --max-steps 6

# 只审查单个文件，并把高危问题作为 CI 失败条件（退出码 3）
python review.py src/service.py --fail-on high --json-out report.json

# 排除测试与生成代码
python review.py . --exclude "*/tests/*" --exclude "*/migrations/*"

# 换更强推理模型（更慢更贵）
python review.py . --model deepseek-reasoner
```

退出码：`0` 成功；`1` 参数/配置错误；`2` 鉴权失败；`3` 命中 `--fail-on` 阈值（便于接 CI）。

---

## 4. 目录结构

```
code-review-agent/
├── review.py                 # 便捷入口：python review.py <路径>
├── webui.py                  # 便捷入口：python webui.py
├── requirements.txt          # 零必需依赖（可选：openai SDK 传输层）
├── .env.example / .gitignore
├── cra/                      # 主包
│   ├── cli.py                # 命令行（review / scan / chat / web）
│   ├── config.py             # 配置、.env 解析、忽视规则与各类限额
│   ├── llm.py                # LLM 客户端：重试退避、错误分类、工具协议降级
│   ├── agent.py              # Agent 主循环（核心）
│   ├── memory.py             # 上下文记忆 + 会话持久化
│   ├── prompts.py            # Prompt 设计（系统提示 / JSON 降级协议 / 扫描摘要）
│   ├── analysis.py           # 确定性静态分析引擎（25 条规则）
│   ├── scanner.py            # 工作区预扫描
│   ├── fsutil.py             # 文件遍历、编码探测、二进制识别、路径安全
│   ├── report.py             # Markdown / JSON 报告渲染
│   ├── jsonutil.py           # 从脏文本中稳健提取 JSON
│   ├── models.py             # Finding / FileInfo / ScanResult / AgentOutcome
│   ├── errors.py             # 分层异常
│   ├── tools/                # 工具集（7 个，含 1 个终止型）
│   └── web/                  # 零依赖 Web 界面（http.server + 单页 UI）
├── examples/                 # 演示样例：一份"问题密集"文件 + 干净对照文件 + 边界样例
├── tests/                    # 102 个单元测试（标准库 unittest）
└── docs/                     # Design.md、示例报告、演示脚本
```

---

## 5. Agent 是怎么工作的

```
用户: review examples
   │
   ├─ ① 预扫描（无 LLM，0.04s）
   │     遍历 → 分类（源码/文本/二进制/空/超大）→ AST 规则 → 66 条命中 + 热点文件
   │
   ├─ ② 组装记忆与提示词（含扫描摘要，模型一开始就有"地图"）
   │
   ├─ ③ Agent 循环（最多 8 步）
   │     ┌─ 模型推理 ──→ 发起工具调用（可并行多个）
   │     │      ↓
   │     │   工具执行（读文件 / 检索 / AST 深挖 / 目录扫描）
   │     │      ↓
   │     └─ 观察结果回灌 → 继续推理 …… 直到调用 submit_review
   │
   ├─ ④ 合并"规则命中"与"模型发现"（按文件+行号+类别去重，取更完整的描述）
   │
   └─ ⑤ 渲染报告：结论摘要 → 分级问题清单 → 分类统计 → 扫描范围与限制 → 附录（工具轨迹）
```

内置工具（7 个）：

| 工具 | 作用 |
| --- | --- |
| `list_files` | 列出文件与类型、大小、行数 |
| `read_file` | 按行号窗口读取（大文件分页，不炸上下文） |
| `search_code` | 正则检索，返回 `文件:行号` |
| `analyze_python` | 单文件全量 AST 规则，返回结构化 JSON |
| `scan_directory` | 子目录确定性扫描（统计 + 热点，限 3 次） |
| `file_stats` | 目录规模与语言分布 |
| `submit_review` | **终止型工具**：提交 summary / verdict / findings |

---

## 6. 静态规则清单（25 条）

规则引擎基于 `ast` 模块一次遍历完成，覆盖安全、缺陷、可维护性、风格四类：

| 编号 | 检查项 | 严重程度 |
| --- | --- | --- |
| SYN001 | 语法错误，无法解析 | high |
| BUG001 | 裸 `except:` 捕获所有异常 | medium |
| BUG002 | 异常被静默吞掉（`except Exception: pass`） | high |
| BUG003 | 可变对象作为默认参数 | high |
| BUG004 | 使用 `== None` 比较 | low |
| BUG005 | `type(x) == T` 判断类型 | low |
| BUG006 | 遍历时修改容器 | medium |
| SEC001 | 疑似硬编码密钥/口令（带占位符过滤，避免误报） | high |
| SEC002 | `eval` / `exec` 执行动态代码 | high |
| SEC003 | `subprocess(..., shell=True)` | high |
| SEC004 | `os.system` 拼接命令 | high |
| SEC005 | 不安全反序列化（`pickle` / `yaml.load`） | medium |
| SEC006 | `assert` 用于输入校验 | low |
| MAINT001 | 函数过长（> 40 行） | low |
| MAINT002 | 圈复杂度过高（≥ 10） | medium |
| MAINT003 | 参数过多（> 5 个） | low |
| MAINT004 | 缺少文档字符串 | info |
| MAINT005 | 未解决的 TODO/FIXME | info |
| MAINT006 | 未使用的导入 | low |
| MAINT007 | 通配符导入 | medium |
| MAINT008 | 变量/参数遮蔽内置名 | low |
| MAINT009 | 库代码里的 `print` | low |
| STYLE001 / 002 / 003 | 行超长 / 行尾空白与制表符 / 文件过长 | info |

> 风格类规则按文件聚合成功一条，单文件问题上限 30 条且**优先保留高危项**（`LIMIT001` 会告知折叠了多少条），避免报告被上千条格式问题淹没。

---

## 7. 边界情况处理

| 场景 | 工具/系统的行为 |
| --- | --- |
| 目录为空 / 全被过滤 | 明确提示"没有找到可审查文件"，报告正常生成 |
| 代码有语法错误 | 转成 `SYN001` 高优先级问题，**不中断**整次审查 |
| 二进制文件（图片、可执行） | 识别为 binary 并跳过，说明原因 |
| 空文件（0 字节） | 单列为 empty，读取时给出明确提示 |
| 超大文件（默认 > 200KB） | 只登记并标注，不做 AST 分析；读文件时分页 + 截断说明 |
| 非 UTF-8（GBK/GB18030/BOM） | 自动探测编码并解码，报告里注明实际编码 |
| 非 Python 代码（JS/Java…） | 规则引擎不处理，交给 LLM 语义审查；`analyze_python` 会明确拒绝并说明原因 |
| 审"单个文件"时想读同目录其他文件 | 单文件模式下工具**只能访问那一个文件**，越界直接拒绝（避免"审一个文件"变成"审它所在的整个目录"） |
| 路径越界（`../../.ssh/id_rsa`） | **直接拒绝**（防提示词注入读取工作区外文件） |
| 依赖/构建目录 | 默认忽略 `.git`、`node_modules`、`__pycache__`、`dist` 等 |
| 软链接 | 默认不跟随，避免循环 |
| 文件数/总大小超限 | 截断并明确说明"结果不完整" |
| API 401/403 | 立即失败不做无意义重试，提示检查 Key |
| API 429 / 5xx / 网络抖动 | 指数退避重试（最多 3 次，尊重 `Retry-After`） |
| 服务端不支持 function calling | 自动切换 JSON 文本协议继续工作 |
| 模型返回非法 JSON | 括号配对 + 去尾逗号修复后再解析 |
| 模型原地打转（重复调用） | 护栏拦截重复调用，两次后提前收尾 |
| 达到步数上限 | 基于已有调查生成报告，并注明"部分调查" |
| LLM 完全不可用 | 降级为规则报告，`degraded_reason` 写清原因 |

---

## 8. 测试

标准库 `unittest`，**102 个用例全部通过**，不需要安装 pytest：

```bash
python -m unittest discover -s tests -t .
```

覆盖范围：

- `test_analysis.py` —— 25 条规则每条都能触发、干净代码零误报、语法错误兜底、上限优先保留高危
- `test_fsutil.py` —— 编码探测、二进制识别、路径越界拒绝、忽略规则、限额截断
- `test_llm.py` —— 重试/退避、401 不重试、429 重试、工具不支持自动降级、脏参数解析（假传输层，不联网）
- `test_tools.py` —— 参数校验与类型纠正、工具错误语义、终止型工具
- `test_agent.py` —— 正常链路、工具观察回灌、步数上限、LLM 异常降级、JSON 协议降级、重复调用护栏
- `test_report_web.py` —— 报告章节完整、**密钥不泄露**、Markdown→HTML、Web 接口（真实起服务打请求）
- `test_jsonutil_memory.py` —— 脏 JSON 提取、记忆裁剪不产生孤儿 tool 消息、会话持久化

---

## 9. 交付物与文档

| 内容 | 位置 |
| --- | --- |
| 使用说明（本文） | `README.md` |
| 设计文档（架构、时序、取舍、边界、测试策略） | `docs/Design.md` |
| 真实运行产出的示例报告 | `docs/sample_report.md`、`docs/sample_report.json` |
| 1 分钟演示视频脚本 | `docs/DEMO_SCRIPT.md` |
| 演示样例（含故意写坏的代码） | `examples/` |

---

## 10. 常见问题

**Q：一定要用 DeepSeek 吗？**
不。任何 **OpenAI 兼容**接口都可以：改 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL` 即可（通义千问、智谱、Moonshot、本地 vLLM 等）。

**Q：我想用官方 SDK 而不是内置 HTTP 客户端？**
`pip install openai` 后在 `.env` 中加 `CRA_TRANSPORT=sdk`，传输层可替换，其余逻辑不变。

**Q：为什么不用 LangChain / AutoGen？**
作业允许使用 LLM 原生 API。原生实现让 Agent 循环、工具协议、重试与降级策略完全可见、可控、可测试；本项目的 `cra/llm.py` 与 `cra/tools/base.py` 就是可替换的抽象层，换成框架只需替换这两处。

**Q：Windows 控制台中文乱码？**
程序启动时会自动把 stdout/stderr 切成 UTF-8；若仍有问题，`chcp 65001` 或设置 `PYTHONIOENCODING=utf-8`。

**Q：在 cmd 里 `cd` 到 G 盘没反应，提示符还停在 C 盘？**
cmd 的 `cd` **不换盘符**，要用 `cd /d G:\代码路径`；或者先输 `G:` 再 `cd`。也可以直接用 `start-web.cmd` / `review.cmd`，它们会自动切到脚本所在目录。

**Q：打开 `127.0.0.1` 显示"拒绝连接"？**
必须带端口号：`http://127.0.0.1:8765`。不带端口走的是 80 端口，本服务不在那里。

**Q：审查很慢 / 很贵？**
`--max-steps` 控制推理轮数，`--max-files` 控制扫描规模，`scan` 子命令完全免费。一次典型审查（10 个文件内）约 8 次模型请求、2 万 token 量级。

**Q：会不会把我代码上传到别处？**
只会把**工具读到的内容**发给你配置的 LLM 服务商；路径越界被拒绝，密钥不会进报告。
