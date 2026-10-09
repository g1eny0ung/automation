"""JSON 序列化工具函数。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd


def json_value(value: Any) -> Any:
    # 容器类型先递归转换：pd.isna 对 list/ndarray 会返回数组，
    # 直接作为布尔判断会抛出歧义异常。
    if isinstance(value, np.ndarray):
        return [json_value(item) for item in value.tolist()]
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # json.dumps 不支持 Decimal；NaN 已在上面被拦截为 None。
        return float(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value
