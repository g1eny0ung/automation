"""市场表现与市场广度指标计算工具，供美股与 A 股模块共用。"""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd

from lib.utils.json_utils import json_value

SMA_WINDOWS = (20, 50, 200)
LONG_WINDOW_SESSIONS = 252
TREND_SESSIONS = 30
WEEK_SESSIONS = 5
MONTH_SESSIONS = 21


def _pct_change(latest: float, baseline: float | None) -> float | None:
    if baseline is None or pd.isna(baseline) or baseline == 0:
        return None
    return (latest - baseline) / baseline * 100


def normalize_close_series(closes: pd.Series) -> pd.Series:
    """整理收盘序列：去空值、按日期升序并去重（保留最后一条）。"""
    series = closes.dropna()
    series = series[~series.index.duplicated(keep="last")]
    return series.sort_index()


def normalize_close_matrix(matrix: pd.DataFrame) -> pd.DataFrame:
    """整理收盘矩阵：按交易日升序并去重（保留最后一条），不丢弃含空值的行。"""
    out = matrix[~matrix.index.duplicated(keep="last")]
    return out.sort_index()


def performance_metrics(
    closes: pd.Series,
    *,
    allow_single_row: bool = False,
    period_basis: Literal["sessions", "calendar"] = "sessions",
) -> dict[str, Any]:
    """从按日期升序的日收盘序列计算多周期表现指标。

    - change_1w/1m/1y 以 5/21/252 个交易日前收盘为基准
    - change_qtd/ytd 以最近一个不属于当前季度/年度的交易日收盘为基准
    - pct_from_52w_high 基于近 252 个交易日收盘最高价
    - calendar 模式按 1 天/7 天/1 个月/1 年前或更早的最近收盘计算，最高价取近 52 周
    - above_sma* 为最新收盘是否站上对应简单均线，历史不足时为 None
    """
    series = normalize_close_series(closes)
    if series.empty or (len(series) < 2 and not allow_single_row):
        raise ValueError("not enough valid close rows for performance metrics")

    latest = float(series.iloc[-1])
    latest_ts = pd.Timestamp(series.index[-1])

    def close_n_sessions_ago(n: int) -> float | None:
        return float(series.iloc[-1 - n]) if len(series) > n else None

    def close_before_offset(offset: pd.DateOffset) -> float | None:
        history = series[series.index <= latest_ts - offset]
        return float(history.iloc[-1]) if not history.empty else None

    if period_basis == "calendar":
        baselines = {
            "change_1d": close_before_offset(pd.DateOffset(days=1)),
            "change_1w": close_before_offset(pd.DateOffset(days=7)),
            "change_1m": close_before_offset(pd.DateOffset(months=1)),
            "change_1y": close_before_offset(pd.DateOffset(years=1)),
        }
        long_window = series[series.index > latest_ts - pd.Timedelta(weeks=52)]
    else:
        baselines = {
            "change_1d": close_n_sessions_ago(1),
            "change_1w": close_n_sessions_ago(WEEK_SESSIONS),
            "change_1m": close_n_sessions_ago(MONTH_SESSIONS),
            "change_1y": close_n_sessions_ago(LONG_WINDOW_SESSIONS),
        }
        long_window = series.iloc[-LONG_WINDOW_SESSIONS:]

    quarter_start = pd.Timestamp(
        year=latest_ts.year, month=(latest_ts.quarter - 1) * 3 + 1, day=1,
        tz=latest_ts.tz,
    )
    year_start = pd.Timestamp(year=latest_ts.year, month=1, day=1, tz=latest_ts.tz)
    before_quarter = series[series.index < quarter_start]
    before_year = series[series.index < year_start]
    qtd_baseline = float(before_quarter.iloc[-1]) if not before_quarter.empty else None
    ytd_baseline = float(before_year.iloc[-1]) if not before_year.empty else None

    high_52w = float(long_window.max()) if not long_window.empty else None
    pct_from_52w_high = (
        (latest / high_52w - 1) * 100 if high_52w not in (None, 0) else None
    )

    metrics: dict[str, Any] = {
        "date": json_value(series.index[-1]),
        "price": json_value(latest),
        "change_1d": json_value(_pct_change(latest, baselines["change_1d"])),
        "change_1w": json_value(_pct_change(latest, baselines["change_1w"])),
        "change_1m": json_value(_pct_change(latest, baselines["change_1m"])),
        "change_qtd": json_value(_pct_change(latest, qtd_baseline)),
        "change_ytd": json_value(_pct_change(latest, ytd_baseline)),
        "change_1y": json_value(_pct_change(latest, baselines["change_1y"])),
        "pct_from_52w_high": json_value(pct_from_52w_high),
        "trend_30d": [
            json_value(value) for value in series.iloc[-TREND_SESSIONS:].tolist()
        ],
    }
    for window_size in SMA_WINDOWS:
        key = f"above_sma{window_size}"
        if len(series) >= window_size:
            sma = float(series.rolling(window_size).mean().iloc[-1])
            metrics[key] = bool(latest > sma)
        else:
            metrics[key] = None
    return metrics


def _breadth_reading(
    matrix: pd.DataFrame, sma: pd.DataFrame, row_position: int
) -> tuple[float | None, int]:
    close_row = matrix.iloc[row_position]
    sma_row = sma.iloc[row_position]
    # 当日收盘价或对应均线为空的成分股不参与统计。
    valid = close_row.notna() & sma_row.notna()
    valid_count = int(valid.sum())
    if valid_count == 0:
        return None, 0
    advanced = (close_row > sma_row)[valid]
    return float(advanced.mean() * 100), valid_count


def sma_breadth(close_matrix: pd.DataFrame) -> dict[str, Any]:
    """计算收盘矩阵中站上各均线成分占比（%）及相对上一交易日的变化（百分点）。

    close_matrix 每行是一个交易日，每列是一只成分股，单元格为收盘价。
    按日期升序计算，收盘价或该周期均线为空的成分股不计入当日占比的分子和分母。
    """
    matrix = normalize_close_matrix(close_matrix)
    if matrix.empty:
        raise ValueError("empty close matrix for market breadth")

    readings: dict[str, Any] = {}
    for window_size in SMA_WINDOWS:
        sma = matrix.rolling(window_size).mean()
        value, valid_count = _breadth_reading(matrix, sma, -1)
        previous_value, _ = (
            _breadth_reading(matrix, sma, -2) if len(matrix) >= 2 else (None, 0)
        )
        change = (
            value - previous_value
            if value is not None and previous_value is not None
            else None
        )
        readings[f"above_sma{window_size}"] = {
            "value": json_value(value),
            "change": json_value(change),
            "valid_count": valid_count,
        }
    return {
        "date": json_value(matrix.index[-1]),
        "universe_size": int(matrix.shape[1]),
        **readings,
    }


def advancers_share(close_matrix: pd.DataFrame) -> dict[str, Any]:
    """计算最近一个交易日上涨成分占比（%）及相对上一交易日的变化（百分点）。"""
    matrix = normalize_close_matrix(close_matrix)
    if matrix.empty or len(matrix) < 2:
        raise ValueError("close matrix needs at least 2 sessions")

    def _reading(row_position: int) -> tuple[float | None, int]:
        latest_row = matrix.iloc[row_position]
        baseline_row = matrix.iloc[row_position - 1]
        valid = latest_row.notna() & baseline_row.notna()
        valid_count = int(valid.sum())
        if valid_count == 0:
            return None, 0
        advanced = (latest_row > baseline_row)[valid]
        return float(advanced.mean() * 100), valid_count

    value, valid_count = _reading(-1)
    # 两天的收盘价足以计算当日上涨占比；计算该占比较前一日的变化，还需要再前一天的收盘价。
    previous_value, _ = _reading(-2) if len(matrix) >= 3 else (None, 0)
    change = (
        value - previous_value
        if value is not None and previous_value is not None
        else None
    )
    return {
        "value": json_value(value),
        "change": json_value(change),
        "valid_count": valid_count,
    }
