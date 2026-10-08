"""统一异常定义（Homework2 · wfa）。

设计原则（沿用 Homework1 的分层经验，但类名以本工程 `docs/CONTRACT.md` 为准）：

* **分层清晰**：`WorkflowError` 之下分三条线——配置 / 节点（Agent）/ 工具（文件），
  外加 LLM 专用一条线。CLI 据此决定退出码（2 = 鉴权失败、4 = 运行失败……），
  `WorkflowEngine` 据此决定 `on_error` 语义，测试据此精确断言。
* **重试由数据决定**：`LLMError.retryable` 是"是否值得重试"的唯一判据，
  `util.RetryPolicy` 只读这个属性，不做 `isinstance` 的具体类型判断——
  将来新增错误类型不必改重试逻辑。
* **安全异常不可吞**：`PathSecurityError`（路径越界）属于安全防线，
  上层不得用宽泛 `except` 把它降级成"工具暂时不可用"。
"""

from __future__ import annotations


class WorkflowError(Exception):
    """本工程所有自定义异常的基类（`except WorkflowError` 可兜住全部业务异常）。"""


# --------------------------------------------------------------------------- #
# 配置类
# --------------------------------------------------------------------------- #
class ConfigError(WorkflowError):
    """配置缺失或非法（例如在线模式没有 API Key、工作流名字不存在）。"""


# --------------------------------------------------------------------------- #
# 节点 / Agent 类
# --------------------------------------------------------------------------- #
class NodeError(WorkflowError):
    """节点执行失败基类（找不到 Agent、节点返回非法结果、黑板缺键等）。"""


class AgentError(NodeError):
    """Agent 自身失败（模型输出无法使用、必要输入缺失等）。"""


# --------------------------------------------------------------------------- #
# 工具 / 文件类
# --------------------------------------------------------------------------- #
class ToolError(WorkflowError):
    """工具或文件操作失败（读写失败、编码错误、工具参数非法等）。"""


class PathSecurityError(ToolError):
    """路径越界：试图访问工作区之外的位置，直接拒绝（安全防护）。"""


# --------------------------------------------------------------------------- #
# LLM 类
# --------------------------------------------------------------------------- #
class LLMError(WorkflowError):
    """LLM 调用相关错误的基类。

    Attributes:
        retryable: 该错误是否值得重试（网络抖动、限流、5xx 为 True）。
        status: HTTP 状态码（如果可以拿到，否则为 None）。
    """

    #: 子类通过覆盖该属性声明"是否可重试"。
    retryable: bool = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - 仅影响日志可读性
        text = super().__str__()
        if self.status is not None:
            return f"{text}（HTTP {self.status}）"
        return text


class LLMAuthError(LLMError):
    """鉴权失败（401/403）：Key 无效或余额不足，重试无意义。"""


class LLMBadRequestError(LLMError):
    """请求非法（400/404/422）：模型名或参数不支持，重试无意义。"""


class LLMRateLimitError(LLMError):
    """触发限流（429）。"""

    retryable = True


class LLMServerError(LLMError):
    """服务端错误（5xx）。"""

    retryable = True


class LLMNetworkError(LLMError):
    """网络层错误：DNS 失败、连接被拒、读超时等。"""

    retryable = True


class LLMResponseError(LLMError):
    """响应结构不符合预期（缺少 choices、JSON 不可解析等）。"""


# --------------------------------------------------------------------------- #
# 流程控制类
# --------------------------------------------------------------------------- #
class WorkflowAborted(WorkflowError):
    """流程被取消（Ctrl+C / abort()）或达到 max_turns / max_iterations。"""


__all__ = [
    "WorkflowError",
    "ConfigError",
    "NodeError",
    "AgentError",
    "ToolError",
    "PathSecurityError",
    "LLMError",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMNetworkError",
    "LLMResponseError",
    "WorkflowAborted",
]
