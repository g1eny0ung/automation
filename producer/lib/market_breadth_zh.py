"""A 股大盘广度：沪深300 成分股站上均线占比。

统计范围为沪深300 成分股，计算方法与美股端点的标普500 均线广度相同。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import BytesIO
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd
    import requests

from lib.breadth_trace import RefreshTrace, observe
from lib.constituent_cache import ConstituentCache
from lib.market_snapshot_cache import (
    MarketSnapshotCache,
    MarketSnapshotCacheConfig,
)
from lib.utils.trading_calendar import MARKET_CN

logger = logging.getLogger(__name__)

REFRESH_HOUR = 18
SOURCE = "akshare_csi300_daily_hist"
CALL_INTERVAL_SECONDS = 0.1
# 覆盖 200 日均线所需的交易日窗口，按自然日回看。
HIST_LOOKBACK_DAYS = 380
CSI300_INDEX_CODE = "000300"
CSI300_MIN_VALID = 240
HIST_FETCH_RETRIES = 2
# 最多同时拉取 6 只股票的历史行情。
FETCH_WORKERS = 6

REDIS_KEY_PREFIX = "market:zh-breadth:v2"
REDIS_LOCK_TTL_SECONDS = 1200

_constituent_cache = ConstituentCache("csi300", min_symbols=CSI300_MIN_VALID)

_cache = MarketSnapshotCache(
    MarketSnapshotCacheConfig(
        cache_key="market_zh_breadth",
        redis_key_prefix=REDIS_KEY_PREFIX,
        source=SOURCE,
        refresh_hour=REFRESH_HOUR,
        market=MARKET_CN,
        redis_lock_ttl_seconds=REDIS_LOCK_TTL_SECONDS,
        l1_max_age_seconds=300,
        publish_requires_lock=True,
        # 目标日没有缓存时，最多查找此前 3 个交易日的缓存，长假期间也按交易日回退。
        read_fallback_trading_days=3,
        payload_data_error="Cached China breadth payload missing data object",
        warming_message="China market breadth cache is not ready",
        refresh_failure_message="Failed to refresh China market breadth cache",
        release_lock_failure_message=(
            "Failed to release China market breadth refresh lock"
        ),
    ),
    logger,
)


def _csi300_symbols(*, trace: RefreshTrace | None = None) -> list[str]:
    """从中证指数官网读取沪深300 成分股代码列表。"""
    import pandas as pd
    import requests

    url = (
        "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/"
        f"autofile/cons/{CSI300_INDEX_CODE}cons.xls"
    )
    for attempt in range(2):
        try:
            with observe(
                trace,
                "source",
                "constituents",
                source="csindex",
                unit="http_request",
                attempt=attempt + 1,
            ):
                response = requests.get(url, timeout=(5, 20))
                response.raise_for_status()
                df = pd.read_excel(BytesIO(response.content))
                code_column = "成份券代码Constituent Code"
                if df.empty or code_column not in df.columns:
                    raise ValueError(
                        "CSI300 constituents missing constituent code field"
                    )
                symbols = sorted(
                    {
                        str(code).split(".")[0].zfill(6)
                        for code in df[code_column].tolist()
                    }
                )
                if len(symbols) < CSI300_MIN_VALID:
                    raise ValueError(
                        f"CSI300 constituents fewer than {CSI300_MIN_VALID}: {len(symbols)}"
                    )
                return symbols
        except requests.Timeout, requests.ConnectionError:
            if attempt == 1:
                raise
        except requests.HTTPError:
            if attempt == 1 or response.status_code not in (
                408,
                429,
                500,
                502,
                503,
                504,
            ):
                raise
        time.sleep(0.5)


def _hist_start_date(refresh_date: date) -> str:
    return (refresh_date - timedelta(days=HIST_LOOKBACK_DAYS)).strftime("%Y%m%d")


def _hist_end_date(refresh_date: date) -> str:
    return refresh_date.strftime("%Y%m%d")


def _tencent_symbol(symbol: str) -> str:
    """给 6 位股票代码添加腾讯要求的交易所前缀，不支持的代码抛出 ValueError。"""
    if symbol.startswith("6"):
        return f"sh{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz{symbol}"
    if symbol.startswith(("4", "8", "9")):
        return f"bj{symbol}"
    raise ValueError(f"Unsupported market for symbol: {symbol}")


def _fetch_symbol_closes_eastmoney(
    symbol: str,
    start_date: str,
    end_date: str,
    session: requests.Session,
    *,
    trace: RefreshTrace | None = None,
) -> pd.Series:
    import pandas as pd

    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101",
        "fqt": "1",
        "secid": f"{1 if symbol.startswith('6') else 0}.{symbol}",
        "beg": start_date,
        "end": end_date,
    }
    last_error: Exception | None = None
    for attempt in range(HIST_FETCH_RETRIES):
        try:
            with observe(
                trace,
                "source",
                "history",
                source="eastmoney",
                unit="http_request",
                symbol=symbol,
                attempt=attempt + 1,
            ):
                response = session.get(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    params=params,
                    timeout=(5, 20),
                )
                data = response.json()["data"]
                if not data or not data["klines"]:
                    raise ValueError(
                        f"AKShare history missing rows/fields for {symbol}"
                    )
                df = pd.DataFrame([row.split(",") for row in data["klines"]])
                df.columns = [
                    "日期",
                    "开盘",
                    "收盘",
                    "最高",
                    "最低",
                    "成交量",
                    "成交额",
                    "振幅",
                    "涨跌幅",
                    "涨跌额",
                    "换手率",
                ]
                df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.date
                df["收盘"] = pd.to_numeric(df["收盘"], errors="coerce")
                return pd.Series(
                    df["收盘"].tolist(), index=pd.to_datetime(df["日期"]), name=symbol
                )
        except Exception as exc:  # noqa: BLE001 - 单只失败不应中断整体刷新
            last_error = exc
            # 重试间隔逐次翻倍（0.1s、0.2s……），避免被限流后仍密集重试、加剧限流。
            time.sleep(CALL_INTERVAL_SECONDS * (2**attempt))
    raise RuntimeError(f"eastmoney failed for {symbol}: {last_error}")


def _fetch_symbol_closes_tencent(
    symbol: str,
    start_date: str,
    end_date: str,
    session: requests.Session,
    *,
    trace: RefreshTrace | None = None,
) -> pd.Series:
    import pandas as pd
    from akshare.utils import demjson

    tx_symbol = _tencent_symbol(symbol)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    count = (end - start).days + 1
    params = {
        "_var": "kline_dayqfq",
        "param": f"{tx_symbol},day,,{end.isoformat()},{count},qfq",
        "r": "0.8205512681390605",
    }
    with observe(
        trace,
        "source",
        "history",
        source="tencent",
        unit="http_request",
        symbol=symbol,
        attempt=1,
    ):
        response = session.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            params=params,
            timeout=(5, 20),
        )
        response.raise_for_status()
        data = demjson.decode(response.text[response.text.find("={") + 1 :])["data"][
            tx_symbol
        ]
        rows = data.get("day", data.get("hfqday", data.get("qfqday")))
        if rows is None:
            raise ValueError(f"Tencent history missing fields for {symbol}")
        if not rows:
            raise ValueError(f"Tencent history missing rows for {symbol}")
        frame = pd.DataFrame(rows).iloc[:, [0, 1, 2, 3, 4, 5, 7, 8]]
    # 与固定 AKShare 版本在参与去重的八列上保持一致，再提取日期和收盘价。
    frame.columns = [
        "date",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "turnover",
        "amount",
    ]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in frame.columns[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.drop_duplicates()
    frame.index = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.sort_index().loc[start_date:end_date]
    if frame.empty:
        raise ValueError(f"Tencent history missing rows for {symbol}")
    return pd.Series(frame["close"].tolist(), index=frame.index, name=symbol)


def _fetch_symbol_closes(
    symbol: str,
    start_date: str,
    end_date: str,
    session: requests.Session,
    *,
    trace: RefreshTrace | None = None,
) -> pd.Series:
    """单只个股日收盘序列：东财为主源，腾讯为备源，两者都失败才跳过该成分。"""
    errors: list[str] = []
    for fetcher in (_fetch_symbol_closes_eastmoney, _fetch_symbol_closes_tencent):
        try:
            if fetcher is _fetch_symbol_closes_eastmoney:
                return fetcher(symbol, start_date, end_date, session, trace=trace)
            if trace:
                trace.emit("fallback", source="tencent", symbol=symbol)
            return fetcher(symbol, start_date, end_date, session, trace=trace)
        except Exception as exc:  # noqa: BLE001 - 当前数据源失败时尝试下一个
            errors.append(f"{fetcher.__name__}: {exc}")
            time.sleep(CALL_INTERVAL_SECONDS)
    raise RuntimeError(f"Failed to fetch history for {symbol}: {'; '.join(errors)}")


def _fetch_close_matrix(
    symbols: list[str], refresh_date: date, *, trace: RefreshTrace | None = None
) -> pd.DataFrame:
    """通过线程池拉取成分股收盘价，返回以交易日为行、股票代码为列的收盘价表。"""
    import pandas as pd
    import requests

    start_date = _hist_start_date(refresh_date)
    end_date = _hist_end_date(refresh_date)
    worker_state = threading.local()
    sessions: list[requests.Session] = []
    sessions_lock = threading.Lock()

    def initialize_worker() -> None:
        session = requests.Session()
        worker_state.session = session
        with sessions_lock:
            sessions.append(session)

    def fetch(symbol: str) -> tuple[str, pd.Series | None]:
        try:
            with observe(trace, "symbol", "history", symbol=symbol):
                return symbol, _fetch_symbol_closes(
                    symbol, start_date, end_date, worker_state.session, trace=trace
                )
        except Exception as exc:  # noqa: BLE001 - 容忍少量成分拉取失败
            if trace:
                trace.emit("failed_symbol", symbol=symbol)
            logger.warning("Skipping CSI300 breadth symbol %s: %s", symbol, exc)
            return symbol, None

    series_by_symbol: dict[str, pd.Series] = {}
    try:
        with ThreadPoolExecutor(
            max_workers=FETCH_WORKERS, initializer=initialize_worker
        ) as executor:
            for symbol, series in executor.map(fetch, symbols):
                if series is not None:
                    series_by_symbol[symbol] = series
    finally:
        for session in sessions:
            session.close()

    valid_count = len(series_by_symbol)
    if valid_count < CSI300_MIN_VALID:
        raise ValueError(
            f"CSI300 breadth valid symbols fewer than {CSI300_MIN_VALID}: {valid_count}"
        )
    return pd.concat(series_by_symbol, axis=1)


def _fetch_zh_breadth(
    refresh_date: date, *, trace: RefreshTrace | None = None
) -> dict[str, Any]:
    from lib.utils.market_metrics_utils import sma_breadth

    logger.debug("Fetching China market breadth for %s", refresh_date)
    symbols = _constituent_cache.get(lambda: _csi300_symbols(trace=trace), trace=trace)
    close_matrix = _fetch_close_matrix(symbols, refresh_date, trace=trace)
    with observe(trace, "stage", "calculation"):
        breadth = sma_breadth(close_matrix)
    breadth["universe"] = "沪深300"
    # 拉取失败的成分股不参与计算，并将 partial 设为 True，表示统计范围不完整。
    breadth["partial"] = close_matrix.shape[1] < len(symbols)

    # 实际数据日期与目标交易日不一致时，只记录警告，仍保存本次快照。
    data_date = close_matrix.index.max().date() if len(close_matrix.index) else None
    if data_date != refresh_date:
        logger.warning(
            "Breadth close data as-of %s differs from refresh_date %s",
            data_date,
            refresh_date,
        )
    return breadth


def fetch_zh_market_breadth() -> dict[str, Any]:
    """只读取缓存，目标日没有缓存时最多查找此前 3 个交易日，不触发上游抓取。

    缓存由 Actions 定时任务负责填充。
    """
    return _cache.read(include_metadata=True)


def refresh_zh_market_breadth(*, trace: RefreshTrace | None = None) -> dict[str, Any]:
    """供定时任务调用，重新计算广度并覆盖当前 refresh_date 的缓存，可重复调用。"""
    return _cache.refresh(
        lambda day: _fetch_zh_breadth(day, trace=trace),
        include_metadata=True,
        trace=trace,
    )
