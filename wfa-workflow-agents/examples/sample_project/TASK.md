# 示例工作区 · 任务卡

> 这是 `--workspace examples/sample_project` 时的输入材料：Workflow 会读取它作为任务上下文，
> 然后把生成的代码、测试与文档写进同一个目录（`--offline` 演示默认写到
> `examples/workflow_workspace/`，避免污染示例输入）。

## 任务

实现一个**订单折扣结算模块** `app/orders.py`，满足：

1. `calculate_total(items, discount_code=None)`：按条目金额求和后应用折扣；
2. 折扣规则：`SAVE10` 打九折、`SAVE20` 打八折、未知折扣码按原价并给出告警；
3. 输入校验：条目必须是含 `price` 的字典，金额为负数时抛 `ValueError`；
4. 不允许出现硬编码密钥、裸 `except:`、静默吞异常；
5. 附带 `tests/test_orders.py` 单元测试，覆盖正常路径、边界路径与异常路径。

## 约束

- 只使用 Python 标准库；
- Python 3.9+；
- 单文件不超过 200 行，函数不超过 40 行（便于静态规则检查通过）。

## 验收标准

- `python -m unittest discover -s tests -t .` 全绿；
- 静态规则检查无 critical / high 命中；
- 提供 README 说明接口与使用方式。
