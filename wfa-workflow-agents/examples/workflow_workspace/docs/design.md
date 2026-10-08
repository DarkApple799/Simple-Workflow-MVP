# 设计说明

- 任务：按需求实现一个小而完整的 Python 模块，并完成审查与修复
- 目标模块：`app/service.py`

## 接口设计

- `normalize_records(records)` → list
- `summarize(records)` → dict

## 文件清单

- app/service.py
- tests/test_service.py

## 说明

接口保持纯粹函数式，便于单测与静态检查。
