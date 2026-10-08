"""wfa —— Workflow Agents：多 Agent 协作的流程编排框架（Homework2）。

一个 Workflow 由若干 **节点（Node）** 组成，每个节点绑定一个 **Agent**；
节点之间通过 **消息总线（MessageBus）** 通信、通过 **共享状态（Blackboard）** 交换产物，
由 **执行器（WorkflowEngine）** 按图拓扑驱动，支持顺序、条件分支、循环与并行。

模块划分（依赖方向严格单向）：

    config      配置与环境变量
    errors      分层异常
    util        通用工具（JSON 提取、重试策略、路径安全）
    model       LLM 接口层（HTTP / 脚本化 / 规则化）
    bus         Agent 间消息总线
    state       共享状态（Blackboard）与运行记录
    graph       节点、边与流程定义（含 WorkflowBuilder）
    analysis    确定性 Python 规则引擎（复用 Homework1 思路）
    agents      Agent 实现（编码 / 审查 / 修复 / 文档 / Homework1 适配器）
    workflows   预置流程（code_review / docs）
    engine      编排执行器
    report      报告渲染（运行报告 / 流程文档）
    cli         命令行入口
    web         零依赖 Web 界面
"""

__version__ = "1.0.0"

#: 严重程度由高到低（报告排序与门禁判断共用）。
SEVERITY_ORDER = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}

SEVERITY_LABEL_ZH = {
    "critical": "致命",
    "high": "严重",
    "medium": "中等",
    "low": "轻微",
    "info": "提示",
}

CATEGORY_LABEL_ZH = {
    "bug": "缺陷",
    "security": "安全",
    "performance": "性能",
    "style": "风格",
    "maintainability": "可维护性",
    "testing": "测试",
    "documentation": "文档",
    "other": "其他",
}

#: 阻断级严重程度：命中即要求走修复分支（门禁默认口径）。
BLOCKING_SEVERITIES = ("critical", "high")

__all__ = [
    "__version__",
    "SEVERITY_ORDER",
    "SEVERITY_LABEL_ZH",
    "CATEGORY_LABEL_ZH",
    "BLOCKING_SEVERITIES",
]
