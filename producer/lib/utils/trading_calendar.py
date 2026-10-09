"""交易日历：基于 holidays 库判断某日是否为交易日。

仅周一至周五且不在对应市场假日表中的日期算作交易日。
调休补班的周末也按休市处理。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from functools import lru_cache

import holidays as holidays_lib

MARKET_CN = "CN"
MARKET_US = "US"

# HolidayBase 非线程安全：首次查询会按年填充假日数据，Flask 多线程下需串行化。
_CALENDAR_LOCK = threading.Lock()


@lru_cache(maxsize=None)
def _holiday_calendar(market: str) -> holidays_lib.HolidayBase:
    if market == MARKET_CN:
        return holidays_lib.country_holidays("CN")
    if market == MARKET_US:
        return holidays_lib.NYSE()
    raise ValueError(f"Unsupported market calendar: {market}")


def is_trading_day(day: date, market: str) -> bool:
    if day.weekday() >= 5:
        return False
    with _CALENDAR_LOCK:
        return day not in _holiday_calendar(market)


def previous_trading_day(day: date, market: str) -> date:
    """严格早于 day 的最近交易日。"""
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate, market):
        candidate -= timedelta(days=1)
    return candidate


def next_trading_day(day: date, market: str) -> date:
    """严格晚于 day 的最近交易日。"""
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate, market):
        candidate += timedelta(days=1)
    return candidate
