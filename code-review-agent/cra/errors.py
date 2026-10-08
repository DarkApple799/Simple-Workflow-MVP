"""统一异常定义。

设计原则：异常分层清晰，便于 CLI 决定退出码、Agent 决定是否重试、测试决定断言。
"""

from __future__ import annotations


class CodeReviewAgentError(Exception):
    """本项目所有自定义异常的基类。"""


# --------------------------------------------------------------------------- #
# 配置类
# --------------------------------------------------------------------------- #
class ConfigError(CodeReviewAgentError):
    """配置缺失或非法（例如没有 API Key）。"""


# --------------------------------------------------------------------------- #
# LLM 类
# --------------------------------------------------------------------------- #
class LLMError(CodeReviewAgentError):
    """LLM 调用相关错误基类。

    Attributes:
        retryable: 该错误是否值得重试（网络抖动、限流、5xx 为 True）。
        status: HTTP 状态码（如果有）。
    """

    retryable = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMAuthError(LLMError):
    """鉴权失败（401/403）：Key 无效或余额不足，重试无意义。"""


class LLMBadRequestError(LLMError):
    """请求非法（400/404/422）：通常是模型名或参数不支持，重试无意义。"""


class LLMRateLimitError(LLMError):
    """触发限流（429）。"""

    retryable = True


class LLMServerError(LLMError):
    """服务端错误（5xx）。"""

    retryable = True


class LLMNetworkError(LLMError):
    """网络层错误：DNS、连接被拒、超时等。"""

    retryable = True


class LLMResponseError(LLMError):
    """响应结构不符合预期（缺少 choices、JSON 解析失败等）。"""


# --------------------------------------------------------------------------- #
# 工具 / 文件类
# --------------------------------------------------------------------------- #
class ToolError(CodeReviewAgentError):
    """工具执行失败。注意：工具失败会被 Agent 当作"观察结果"回灌给模型，而不是中断流程。"""


class ToolArgumentError(ToolError):
    """工具参数缺失或类型错误。"""


class PathSecurityError(ToolError):
    """路径越界（试图读取工作区之外的文件），属于安全防护。"""


class FileBoundaryError(ToolError):
    """文件层面的边界情况：二进制、超大、编码不可读等。"""
