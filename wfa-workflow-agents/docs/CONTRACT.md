# wfa 模块接口契约（冻结版 v1）

> 本文件是 Homework2 工程的**唯一接口真相**。所有模块按此实现；**任何签名变更必须先通知 Lead**。
> 依赖方向严格单向，禁止逆向 import：
>
> ```
> util, errors, model
>    ↑
>  bus, state
>    ↑
>  graph
>    ↑
>  agents/*        workflows/*
>    ↑
>  engine
>    ↑
>  cli, web, tests
> ```

包名：`wfa`（Workflow Agents）。全部使用相对 import（`from ..errors import ...`）。

---

## 1. `wfa/errors.py`

```python
class WorkflowError(Exception):                # 本工程异常基类
class ConfigError(WorkflowError)
class NodeError(WorkflowError)                 # 节点执行失败基类
class AgentError(NodeError)                    # Agent 自身失败
class ToolError(WorkflowError)                 # 工具/文件操作失败
class PathSecurityError(ToolError)             # 路径越界（工作区之外）
class LLMError(WorkflowError):
    retryable: bool = False
    status: int | None = None
    def __init__(self, message: str, *, status: int | None = None) -> None
class LLMAuthError(LLMError)                   # 401/403，不可重试
class LLMBadRequestError(LLMError)             # 400/404/422，不可重试
class LLMRateLimitError(LLMError)              # retryable = True
class LLMServerError(LLMError)                 # retryable = True
class LLMNetworkError(LLMError)                # retryable = True
class LLMResponseError(LLMError)               # 响应不可解析
class WorkflowAborted(WorkflowError)           # 被取消 / 达到 max_turns
```

## 2. `wfa/util.py`

```python
def extract_json_object(text: str) -> dict | None          # 括号配对+字符串感知，容忍 ```json 包裹与尾逗号
def extract_json_array(text: str) -> list | None
def now_iso() -> str                                       # "2026-10-08T12:00:00"
def new_id(prefix: str = "id") -> str                      # f"{prefix}-{uuid4().hex[:8]}"
def truncate(text: str, limit: int = 400, suffix: str = "…") -> str
def safe_relpath(root: str, path: str) -> str              # 统一正斜杠的相对路径
def safe_join(root: str, rel: str) -> str                  # 拒绝越界，抛 PathSecurityError
def sha1_text(text: str) -> str                            # 内容指纹（10 位）
def read_text(path: str, *, max_bytes: int = 200_000) -> str
def write_text(path: str, text: str) -> None               # 自动建父目录，UTF-8 无 BOM
class RetryPolicy:
    def __init__(self, max_retries: int = 3, base_delay: float = 0.4,
                 max_delay: float = 8.0, sleep: Callable[[float], None] = time.sleep) -> None
    def run(self, fn: Callable[[], T], *, on_retry: Callable[[int, Exception], None] | None = None) -> T
        # 只对 LLMError.retryable=True 重试；指数退避；其它异常立即抛出
    @property
    def attempts(self) -> int
    @property
    def retries(self) -> int
```

`wfa/model.py`（LLM 接口层，零第三方依赖）：

```python
@dataclass
class Usage: prompt_tokens=0; completion_tokens=0; total_tokens=0
    def add(self, other: "Usage") -> None
@dataclass
class ToolCall: id=""; name=""; arguments: dict = {}; raw_arguments: str = ""; parse_error: str = ""
    def to_message_part(self) -> dict
@dataclass
class LLMResponse: content: str = ""; tool_calls: list[ToolCall] = []; usage: Usage = Usage(); finish_reason: str = ""
@dataclass
class LLMStats: calls=0; retries=0; failures=0; prompt_tokens=0; completion_tokens=0; total_latency=0.0
    def as_dict(self) -> dict
class ChatModel(Protocol):
    supports_tools: bool | None
    stats: LLMStats
    def chat(self, messages: Sequence[dict], *, tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int | None = None) -> LLMResponse
class HttpChatModel:      # OpenAI 兼容，标准库 urllib；重试/退避/401 分类/不支持 tools 自动降级
    def __init__(self, cfg, *, transport: Transport | None = None, sleep=None) -> None
    # cfg 需有: base_url, model, timeout, max_retries, retry_base_delay, temperature, max_tokens, resolve_api_key()
class ScriptedChatModel:  # 离线/测试：按顺序返回预置 LLMResponse；可传 responder: Callable[[list[dict]], LLMResponse]
    def __init__(self, script: list[LLMResponse] | None = None, *, responder=None) -> None
    def chat(...) -> LLMResponse          # script 用尽时复用最后一条
class RuleChatModel:      # 离线确定性"伪 LLM"：根据提示词关键词返回内置模板（demo 用）
    def __init__(self, persona: str = "default") -> None
```

## 3. `wfa/bus.py` — Agent 间通信

```python
PERF_ID = "workflow"        # 广播/系统消息的固定 sender

@dataclass
class Message:
    kind: str                    # task | result | question | answer | review | feedback | system | error
    sender: str                  # 节点 id 或 PERF_ID
    recipient: str               # 节点 id 或 BROADCAST
    payload: dict = {}
    id: str = ""                 # 自动 new_id("msg")
    created_at: str = ""         # now_iso()
    turn: int = 0
    correlation_id: str = ""     # 请求-应答配对
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, data: dict) -> "Message"
BROADCAST = "*"

class Subscription:          # 迭代取消息
    def drain(self, timeout: float = 0.0) -> list[Message]   # timeout=0 立即返回；阻塞至多 timeout 秒
    def close(self) -> None
    @property
    def closed(self) -> bool

class MessageBus:
    def __init__(self, *, echo: bool = False) -> None
    #: 发布：写入日志、投递给 recipient 的收件箱；BROADCAST 投递给所有订阅者（含 workflow）
    def publish(self, message: Message) -> Message
    def send(self, kind: str, sender: str, recipient: str, payload: dict | None = None, *,
             correlation_id: str = "", turn: int = 0) -> Message
    def ask(self, sender: str, recipient: str, payload: dict, *, turn: int = 0) -> Message   # 发 question
    def reply(self, request: Message, sender: str, payload: dict) -> Message                 # 发 answer，自动带 correlation_id
    def subscribe(self, recipient: str) -> Subscription
    def unsubscribe(self, recipient: str) -> None
    def inbox(self, recipient: str) -> list[Message]        # 全部已投递给某方的消息（含 BROADCAST）
    def history(self, *, kind: str | None = None, recipient: str | None = None,
                sender: str | None = None, limit: int = 0) -> list[Message]
    def wait_for(self, recipient: str, *, kind: str | None = None,
                 correlation_id: str | None = None, timeout: float = 0.0) -> Message | None
    def to_dict(self) -> dict      # {"messages": [...], "counts": {"kind": n}}
    @classmethod
    def from_dict(cls, data: dict) -> "MessageBus"
    def log_lines(self) -> list[str]   # 人类可读的一行一条，用于 --verbose 与 docs/run_log.md
    @property
    def messages(self) -> list[Message]
```

## 4. `wfa/state.py` — 共享状态（Blackboard）

```python
@dataclass
class NodeRecord:
    node_id: str; agent: str; turn: int; status: str      # ok | failed | skipped
    started_at: str; duration: float = 0.0
    summary: str = ""; kind: str = ""                      # kind = AgentResult.kind
    decision: str = ""                                     # AgentResult.decision
    result: dict = {}                                      # AgentResult.to_dict()
    messages: list[str] = []                               # 本节点产生的消息 id
    error: str = ""
    attempt: int = 1
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, data: dict) -> "NodeRecord"

@dataclass
class RunState:
    run_id: str; workflow: str; status: str = "pending"    # pending|running|success|partial|failed|aborted
    turn: int = 0; iterations: dict[str, int] = {}
    started_at: str = ""; finished_at: str = ""; duration: float = 0.0
    records: list[NodeRecord] = []
    final: dict = {}                                       # 终态摘要（报告用）
    error: str = ""
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, data: dict) -> "RunState"

class Blackboard:
    """节点间共享状态：命名空间键值 + 产物登记 + 工作区文件 + 运行状态。"""
    def __init__(self, *, workspace: str = "", bus: MessageBus | None = None,
                 state: RunState | None = None) -> None
    # ---- 键值 ----
    def set(self, key: str, value: Any) -> None
    def get(self, key: str, default: Any = None) -> Any
    def require(self, key: str) -> Any                     # 缺失抛 NodeError
    def update(self, mapping: dict) -> None
    def keys(self) -> list[str]
    def snapshot(self, prefix: str = "") -> dict            # prefix 过滤的前缀视图
    def ns_set(self, ns: str, key: str, value: Any) -> None     # 等价 set(f"{ns}.{key}")
    def ns_get(self, ns: str, key: str, default: Any = None) -> Any
    def ns_all(self, ns: str) -> dict
    def ns_merge(self, ns: str, mapping: dict) -> None
    # ---- 产物（artifact）----
    def put_artifact(self, name: str, content: Any, *, kind: str = "doc", producer: str = "") -> dict
    def artifact(self, name: str) -> dict | None           # {"name","kind","producer","created_at","content"}
    def artifacts(self) -> dict[str, dict]
    # ---- 工作区文件 ----
    def write_file(self, rel: str, content: str) -> str     # 返回绝对路径；路径越界抛 PathSecurityError
    def read_file(self, rel: str, default: str | None = None) -> str | None
    def file_hash(self, rel: str) -> str
    def file_exists(self, rel: str) -> bool
    def list_files(self, *, pattern: str = "*") -> list[str]   # 相对路径，正斜杠，排序
    # ---- 运行状态 ----
    @property
    def bus(self) -> MessageBus
    def record(self, record: NodeRecord) -> None
    def bump_iteration(self, loop_id: str) -> int
    def iteration(self, loop_id: str) -> int
    def summary(self) -> dict       # 指标卡：轮次、节点数、消息数、产物数、耗时、状态
    # ---- 持久化 ----
    def to_dict(self) -> dict       # {"kv": {...}, "artifacts": {...}, "state": {...}, "metrics": {...}}
    def save(self, path: str) -> str
    @classmethod
    def load(cls, path: str, *, workspace: str = "", bus: MessageBus | None = None) -> "Blackboard"
```

`RunState.records` 只增不减；`Blackboard.record()` 追加并更新 `state.turn`（取最大值）。

## 5. `wfa/graph.py` — 节点与流程定义

```python
# ---- 结果 ----
@dataclass
class AgentResult:
    kind: str = "result"                   # result | review | fix | doc | requirement
    summary: str = ""                      # 一句话摘要（给人看）
    decision: str = ""                     # "" | continue | pass | retry | fail | done
    data: dict = {}                        # 结构化载荷（契约见下）
    artifacts: dict[str, str] = {}         # 产物名 -> 内容
    metrics: dict = {}                     # 任意指标
    notes: list[str] = []

@dataclass
class NodeOutcome:
    next_node: str | None = None           # None 表示用 graph 默认出边
    status: str = "ok"                     # ok | failed | skipped
    error: str = ""
    payload: dict = {}                     # 额外回写黑板的键值
    def to_dict(self) -> dict
    def __bool__(self) -> bool             # status == "ok"

# execute 签名（所有节点统一）
ExecuteFn = Callable[[Node, "NodeContext"], AgentResult]

@dataclass
class Node:
    id: str
    agent: str = ""                        # Agent.name
    execute: ExecuteFn | None = None
    type: str = "agent"                    # agent | gate | route | parallel | start | end
    next: list[str] = []                   # 出边（默认按顺序尝试第一个可运行的）
    when: Callable[["NodeContext"], bool] | None = None   # 入边条件（False 则跳过该节点）
    retry: int = 0                         # 失败重试次数（不含首次）
    retry_delay: float = 0.2
    on_error: str = "fail"                 # fail | continue | fallback:<node_id>
    fallback: str | None = None
    loop_of: str = ""                      # 标记该节点属于哪个循环（用于 iterations 计数）
    max_iterations: int = 0                # 该循环最多跑几次（0 = 用 global max_iterations）
    children: list["Node"] = []            # type == parallel 时使用
    options: dict = {}                     # 其它扩展参数
    description: str = ""
    def to_dict(self) -> dict
    @dataclass
class Edge:
    src: str; dst: str; when: str = ""; label: str = ""
    def to_dict(self) -> dict

class Workflow:
    def __init__(self, name: str, *, description: str = "", entry: str = "",
                 nodes: list[Node] | None = None, edges: list[Edge] | None = None,
                 max_turns: int = 50, max_iterations: int = 3) -> None
    @property
    def name(self) -> str
    @property
    def entry(self) -> str
    def node(self, node_id: str) -> Node | None
    def nodes(self) -> list[Node]
    def edges(self) -> list[Edge]
    def add_node(self, node: Node) -> Node
    def add_edge(self, src: str, dst: str, *, when: str = "", label: str = "") -> Edge
    def entry_node(self) -> Node                                  # 无 entry 时取第一个节点
    def successors(self, node_id: str) -> list[Node]              # 按声明顺序
    def to_dict(self) -> dict
    def to_dot(self) -> str                                       # Graphviz DOT，含回边标注
    def to_mermaid(self) -> str                                   # 文档用
    def validate(self) -> list[str]                               # 返回问题列表；空 = 合法

class WorkflowBuilder:      # 流式声明 API（文档与测试都用它）
    def __init__(self, name: str, *, description: str = "") -> None
    def agent(self, node_id: str, agent: str, *, execute: ExecuteFn | None = None,
              description: str = "", retry: int = 0, on_error: str = "fail",
              loop_of: str = "", max_iterations: int = 0, when=None) -> "WorkflowBuilder"
    def gate(self, node_id: str, *, execute: ExecuteFn, description: str = "") -> "WorkflowBuilder"
    def route(self, node_id: str, *, execute: ExecuteFn, description: str = "") -> "WorkflowBuilder"
    def parallel(self, node_id: str, children: list[Node], *, execute: ExecuteFn | None = None,
                 description: str = "") -> "WorkflowBuilder"
    def start(self, node_id: str = "start") -> "WorkflowBuilder"
    def end(self, node_id: str = "end") -> "WorkflowBuilder"
    def edge(self, src: str, dst: str, *, when: str = "", label: str = "") -> "WorkflowBuilder"
    def build(self, *, entry: str | None = None, max_turns: int = 50,
              max_iterations: int = 3) -> Workflow       # 自动校验，问题非空抛 NodeError

@dataclass
class NodeContext:
    """节点执行上下文（由 engine 构造）。"""
    node: Node
    state: Blackboard
    bus: MessageBus
    inputs: dict          # {上游节点 id: AgentResult} —— 仅直接前驱
    iteration: int        # 当前节点所属循环已执行的次数
    turn: int
    workspace: str
    config: Any           # RunConfig（engine 模块定义，鸭子类型使用）
    def input_result(self, node_id: str, default: AgentResult | None = None) -> AgentResult | None
    def payload(self, node_id: str, key: str, default: Any = None) -> Any    # 上游 result.data[key]
    def first_payload(self, key: str, default: Any = None) -> Any            # 按 inputs 声明顺序找
```

**AgentResult.data 约定**（所有 Agent 必须遵守，engine 的 gate/route/report 依赖它）：

| producer | key | 类型 | 含义 |
| --- | --- | --- | --- |
| coding agent | `files` | `dict[str,str]` | 相对路径 → 文件内容 |
| coding agent | `tests` | `dict[str,str]` | 测试文件 |
| coding agent | `plan` | `list[str]` | 实现步骤 |
| coding agent | `self_test` | `dict` | `{"command": str, "returncode": int, "stdout": str}` |
| review agent | `issues` | `list[dict]` | 见下 `Issue` |
| review agent | `counts` | `dict` | `{"critical":n,"high":n,"medium":n,"low":n,"info":n}` |
| review agent | `verdict` | `str` | `pass|pass_with_comments|request_changes|block` |
| review agent | `blocking` | `list[dict]` | critical/high 子集 |
| fix agent | `patches` | `list[dict]` | `{"file":str,"issue_id":str,"summary":str,"before":str,"after":str}` |
| fix agent | `fixed` | `list[str]` | 已修 issue id |
| fix agent | `remaining` | `list[dict]` | 无法修复的 issue（含原因） |
| doc agent | `documents` | `dict[str,str]` | 文件名 → Markdown 内容 |
| doc agent | `outline` | `list[str]` | 章节标题 |

`Issue` dict 字段：`id, title, file, line, severity, category, detail, suggestion, source, rule_id`，
`severity ∈ {critical, high, medium, low, info}`。`SEVERITY_ORDER` 常量由 `wfa/__init__.py` 暴露。

## 6. `wfa/agents/base.py` — Agent 抽象

```python
class Agent(ABC):
    name: str            # 唯一标识，如 "coder"
    role: str            # 人类可读角色名，如 "编码 Agent"
    @abstractmethod
    def run(self, ctx: NodeContext) -> AgentResult: ...
    def describe(self) -> str                 # 自我介绍（报告/日志用）
class AgentRegistry:
    def register(self, agent: Agent) -> Agent
    def get(self, name: str) -> Agent         # 缺失抛 ConfigError
    def names(self) -> list[str]
    def describe(self) -> list[dict]          # [{"name","role","describe"}]
def default_registry(model, *, config=None, bus=None) -> AgentRegistry   # 注册全部内置 Agent
```

## 7. `wfa/agents/*.py`

| 文件 | 类 | `name` | 备注 |
| --- | --- | --- | --- |
| `coding.py` | `CodingAgent(model, *, config=None)` | `coder` | 读需求/设计 → 生成代码与测试 → 写入工作区 → 本地自测 |
| `review.py` | `ReviewAgent(model, *, config=None)` | `reviewer` | 见下（三级审查：工具核实 + 规则 + LLM） |
| `fix.py` | `FixAgent(model, *, config=None)` | `fixer` | 按 issue 列表修复；生成 patch + 修复报告 |
| `doc.py` | `DocAgent(model, *, config=None)` | `writer` / `proofreader` | 由一个类参数化出两个角色 |
| `code_review.py` | `LegacyReviewAgent` | `legacy_reviewer` | **复用 Homework1 的 `CodeReviewAgent`**（见第 9 节） |

### 7.1 ReviewAgent（核心，评分点"Agent 协作 + 复用项目1"）

```
run(ctx):
 ① 阶段一 确定性事实：使用内置的 `wfa/analysis.py` 规则引擎扫描工作区（无 LLM）
 ② 阶段二 工具核实：调用 LLM（若有 tools 能力）发起工具调用（list_files / read_file / analyze_python /
    search_code），最多 review_max_steps 步，收集 tool_calls 轨迹
 ③ 阶段三 判定：把 ①② 汇成 issues（规则命中 + LLM findings 去重合并），
    产出 verdict / counts / blocking
 ④ 若注入了 LegacyReviewAgent（Homework1 适配器），额外调用一次并合并其 findings
```

**强制要求（否则无法离线跑通）**：`model` 为 `RuleChatModel` 或 `ScriptedChatModel` 时必须走确定性路径，
规则引擎结果必须仍然完整（`--offline` 演示不能为空）。

### 7.2 `wfa/analysis.py` — 规则引擎（复用 Homework1 思路的精简实现）

```python
@dataclass
class RuleHit: rule_id, title, file, line, severity, category, detail, suggestion, evidence
    def to_issue(self) -> dict
RULES: list[dict]        # 每条 {"id","title","severity","category","detector": Callable[[ast.Module,str,str], list[RuleHit]]}
def analyze_python_file(path: str, *, rel: str | None = None) -> list[RuleHit]
def analyze_source(source: str, rel: str, *, max_hits: int = 30) -> list[RuleHit]
def scan_workspace(root: str, *, ignore_dirs=..., max_files: int = 200, max_file_bytes: int = 200_000) -> "ScanReport"
@dataclass
class ScanReport: root, files: list[dict], hits: list[RuleHit], skipped: list[dict], notes: list[str], duration: float
    def counts(self) -> dict
    def to_dict(self) -> dict
```

规则至少覆盖：`SYN001` 语法错误、`BUG001` 裸 except、`BUG002` 静默吞异常、`BUG003` 可变默认参数、
`BUG004` `== None`、`BUG006` 遍历时修改容器、`SEC001` 硬编码密钥、`SEC002` `eval/exec`、
`SEC003` `subprocess(shell=True)`、`SEC004` `os.system`、`SEC005` 不安全反序列化、
`SEC006` `assert` 做校验、`MAINT001` 函数过长、`MAINT002` 圈复杂度过高、`MAINT003` 参数过多、
`MAINT004` 缺少 docstring、`MAINT005` TODO/FIXME、`MAINT006` 未使用导入、`MAINT007` 通配符导入、
`MAINT008` 遮蔽内置名、`MAINT009` 裸 print、`STYLE001` 行过长。

## 8. `wfa/engine.py` — 编排执行器

```python
@dataclass
class RunConfig:
    task: str = ""
    workspace: str = ""
    mode: str = "offline"            # offline | online
    model: str = ""
    max_turns: int = 50
    max_iterations: int = 3
    review_max_steps: int = 4
    stop_on_blocking: bool = True
    verbose: bool = False
    parallel_workers: int = 3
    extra: dict = {}
    def to_dict(self) -> dict        # 不含密钥

@dataclass
class RunReport:
    run_id: str; workflow: str
    status: str = "pending"          # success | partial | failed | aborted
    turns: int = 0; duration: float = 0.0
    node_runs: list[NodeRecord] = []
    issues: list[dict] = []          # 最终 issue 列表
    counts: dict = {}
    artifacts: dict[str, dict] = {}
    metrics: dict = {}
    messages: int = 0
    error: str = ""
    notes: list[str] = []
    def to_dict(self) -> dict
    def to_markdown(self) -> str      # 人读的运行报告

class StopReason: ...                # 常量：COMPLETED / MAX_TURNS / MAX_ITERATIONS / ABORTED / ERROR / STOP

class WorkflowEngine:
    def __init__(self, workflow: Workflow, agents: AgentRegistry, *,
                 config: RunConfig | None = None, bus: MessageBus | None = None,
                 state: Blackboard | None = None, on_event: Callable[[str, dict], None] | None = None,
                 stop_check: Callable[[], bool] | None = None) -> None
    def run(self, *, resume_from: str | None = None) -> RunReport
    def abort(self) -> None
    @property
    def bus(self) -> MessageBus
    @property
    def state(self) -> Blackboard
    def checkpoint(self, path: str) -> str
    @classmethod
    def resume(cls, path: str, agents: AgentRegistry, **kwargs) -> tuple["WorkflowEngine", RunReport]
```

**执行语义（必须精确实现，测试会验）**：

1. `before_turn`：`turn += 1`；若 `turn > config.max_turns` → `StopReason.MAX_TURNS`，`status="partial"`；若 `stop_check()` → `StopReason.ABORTED`，`status="aborted"`。
2. 取当前节点：若 `when` 返回 False → 记录 `skipped`，跳到下一个后继（不执行）。
3. 找 Agent：`agents.get(node.agent)`；找不到 → `on_error="fail"` 语义。
4. 执行：`agent.run(NodeContext(...))`；支持 `retry` 次重试（`RetryPolicy`，退避 `retry_delay`）。
5. 失败处理：`fail`（有 fallback 则走 fallback）| `continue`（记 failed，继续后继）。
6. 成功：`AgentResult` 写入 `state.set(f"{node.id}", result.to_dict())`，并 `state.ns_merge("results", {node.id: result.to_dict()})`；`kind=="review"` 时把 `issues/counts/verdict/blocking` 写到顶层键 `issues`、`issue_counts`、`verdict`、`blocking`；`kind=="fix"` 时 `state.set("fixes", data["patches"])`。
7. `loop_of` 非空的节点：每次执行前 `state.bump_iteration(loop_of)`，若 `> max_iterations(node) or config.max_iterations` → `StopReason.MAX_ITERATIONS`，`status="partial"`。
8. 出边选择：`AgentResult.decision` 优先——
   - `"pass"|"done"` → 选第一个 `dst` 为 `end` 的出边，否则最后一个出边；
   - `"retry"|"continue"` → 选标注 `when="retry"` / `label="retry"` 的出边，否则第一个出边；
   - `"fail"` → 选 `when="fail"` 的出边，否则 `on_error` 语义；
   - `decision == ""` → 按声明顺序取第一个 `when` 为空的出边。
   - 若无出边 → 结束（`status="success"`）。
9. `type=="parallel"`：用 `ThreadPoolExecutor(max_workers=config.parallel_workers)` 并发执行 `children`，
   `AgentResult.data` 合并到 `{"children": {child_id: result.to_dict()}}`；若提供了 `execute` 则把合并结果交给它再产出最终 `AgentResult`。
   子节点结果同时写入 `state.ns_merge(node.id, {...})`。
10. 每回合结束 `state.record(NodeRecord)`，并向 bus 发 `system` 消息；`on_event` 回调收到 `turn_start|turn_end|node_start|node_end|node_failed|stop|parallel_start|parallel_end`。
11. 终态：`status` ∈ `success|partial|failed|aborted`；汇总 `issues`（`state.get("issues")`）、
    `counts`、`artifacts`、`metrics`（`state.summary()` + 模型统计）写进 `RunReport` 与 `state.state.final`。
12. `checkpoint(path)`：`{workflow, config, blackboard, bus}` JSON；`resume` 恢复后从 `resume_from` 或
    最后一条记录的 `next_node` 继续（`RunReport` 里带 `notes=["已从检查点恢复"]`）。

## 9. 复用 Homework1（`cra`）的方式

`wfa/agents/code_review.py` 中实现 `LegacyReviewAgent`：

```python
class LegacyReviewAgent(Agent):
    name = "legacy_reviewer"
    def __init__(self, *, cra_root: str | None = None, offline: bool = True, max_steps: int = 4) -> None
    # cra_root 记录 Homework1 工程路径；通过加进 sys.path 后 `from cra.agent import CodeReviewAgent`
    # 复用其 25 条 AST 规则 + 扫描器 + 工具循环，把 AgentOutcome.findings 转成 Issue dict
```

- 复用成功时 `AgentResult.metrics["legacy"] = True`，`notes` 里说明"复用了 Homework1 的审查能力"；
- 找不到 `cra` 时**不得崩溃**：回退到 `wfa/analysis.py`，`metrics["legacy"] = False`，`notes` 说明原因。

## 10. `wfa/workflows/*.py`

```python
# code_review.py
def build_code_review_workflow(*, agents=("coder", "reviewer", "fixer", "documenter"),
                               parallel_review: bool = False, max_iterations: int = 3) -> Workflow
# docs.py
def build_docs_workflow(*, agents=("planner", "writer", "proofreader"), max_iterations: int = 2) -> Workflow
# registry.py
WORKFLOWS: dict[str, Callable[..., Workflow]] = {"code_review": ..., "docs": ...}
def build_workflow(name: str, **kwargs) -> Workflow      # 未知名字抛 ConfigError
def list_workflows() -> list[dict]                       # [{"name","description","agents"}]
```

**code_review 流程（必须与此拓扑一致）**：

```
start → requirements(需求 Agent/ coder 前置: 解析任务)
      → design(设计 Agent: 产出接口与文件清单)
      → code(coder, loop_of="impl")
      → parallel_review{security_review, maintainability_review}(reviewer, 可选)
      → review(reviewer)  [parallel_review 关闭时直接 review]
      → gate_quality(gate: 有 blocking 且 iteration < max → retry)
      ├─ retry → fix(fixer, loop_of="impl") → review
      └─ pass  → docs(documenter) → report → end
```

（实现时节点名与上表一致；`parallel_review` 打开时 `review` 聚合子节点结果。）

## 11. `wfa/cli.py` + 入口脚本

```
python run.py run --workflow code_review --task "..." --workspace <dir> [--online|--offline]
                  [--max-iterations 3] [--max-turns 50] [--parallel-review] [--verbose]
                  [--out docs/run_report.md] [--json-out docs/run_report.json]
                  [--checkpoint .wfa/checkpoint.json] [--resume .wfa/checkpoint.json]
                  [--no-write-workspace]
python run.py list            # 列出 Agent 与 Workflow
python run.py graph --workflow code_review [--format mermaid|dot] [--out docs/flow.md]
python run.py report --json docs/run_report.json [--out report.md]
python webui.py [--port 8765] [--no-open] [--no-write-workspace]
```

退出码：`0` 成功、`1` 参数/配置错误、`2` 鉴权失败、`3` 存在阻断级 issue、`4` 运行失败/中断。

CLI 必须保证：Windows 控制台自动切 UTF-8；`--offline` 不需要 API Key；所有输出路径自动建目录。

## 12. `wfa/web/server.py`

零依赖 `http.server`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 单页 UI（`static/index.html`），显示 Workflow 拓扑 + 运行按钮 + 实时日志 |
| GET | `/api/workflows` | `{"workflows":[{"name","description","agents","mermaid"}]}` |
| GET | `/api/agents` | `{"agents":[{"name","role","describe"}]}` |
| POST | `/api/run` | `{"workflow","task","workspace","offline":true,...}` → 启动后台线程，返回 `{"run_id"}` |
| GET | `/api/runs/<id>` | 运行状态 + 事件流 + 报告（进行中时增量返回） |
| GET | `/api/runs/<id>/report` | Markdown 报告（`text/markdown`） |

## 13. 测试与文档（其他 teammate 负责，勿改实现文件）

- `tests/` 使用标准库 `unittest`，`python -m unittest discover -s tests -t .` 必须全绿；
- 不允许测试依赖网络或 API Key；
- `docs/Design.md`、`README.md`、`docs/FLOW.md`（流程图）、`docs/DEMO_SCRIPT.md`。

## 14. 代码风格硬要求

- Python 3.9+ 兼容语法（可用 `from __future__ import annotations`，**不要**用 3.10+ 的 `X | Y` 运行时求值场景）；
- 只依赖标准库；`python_requires >= 3.9`；
- 每个模块有中文 docstring，公共函数有简短 docstring；
- 不使用 `print` 做库内输出（CLI/入口除外），统一 `logging.getLogger("wfa.*")`；
- 中文注释、中文文档（与 Homework1 风格一致）。
