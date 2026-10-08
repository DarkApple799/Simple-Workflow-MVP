"""wfa 便捷入口：`python webui.py` 启动零依赖 Web 界面。

界面能力（详见 wfa/web/server.py）：
    * 展示两个预置工作流的拓扑（Mermaid / 节点表格）与参与 Agent 清单；
    * 选择工作流 + 填任务 + 离线/在线，点击运行；
    * 实时查看回合事件流与消息总线日志；
    * 运行结束后直接阅读 Markdown 报告并下载 JSON。
"""

from __future__ import annotations

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from wfa.web.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
