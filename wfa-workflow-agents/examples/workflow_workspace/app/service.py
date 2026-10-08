"""示例业务模块（Workflow 修复后的版本）。"""

import json
import logging
import os

logger = logging.getLogger("app.service")

#: 密钥一律从环境变量读取，绝不硬编码在源码里。
API_KEY_ENV = "SAMPLE_API_KEY"


def normalize_records(records):
    """把字符串记录解析成字典列表；无法解析的记录被显式跳过并记录日志。"""
    result = []
    for index, record in enumerate(records):
        try:
            item = json.loads(record)
        except (TypeError, ValueError) as exc:
            logger.warning("第 %d 条记录解析失败，已跳过：%s", index, exc)
            continue
        result.append(item)
    return result


def summarize(records, *, key="value"):
    """统计记录条数与指定字段的合计值。"""
    total = 0
    for record in records:
        if isinstance(record, dict):
            total += record.get(key, 0)
    return {"count": len(records), "total": total}


def load_config(path):
    """读取 JSON 配置；缺失或损坏时返回空配置并记录原因。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("配置读取失败，使用空配置：%s", exc)
        return {}
