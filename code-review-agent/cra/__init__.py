"""Code Review Agent —— 一个基于 LLM 的代码审查 Agent。

模块划分：
    config      配置与环境变量
    llm         LLM 客户端（原生 HTTP，含重试/退避/降级）
    fsutil      文件遍历、编码探测、路径安全
    analysis    AST 静态分析引擎（确定性规则）
    scanner     工作区预扫描
    tools       供 Agent 调用的工具集
    agent       Agent 主循环（推理 → 工具调用 → 观察 → 汇总）
    memory      上下文记忆
    report      报告渲染（Markdown / JSON）
    cli         命令行入口
    web         轻量 Web 界面
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
