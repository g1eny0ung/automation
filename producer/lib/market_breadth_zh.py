"""A 股大盘广度：沪深300 成分股站上均线占比。

统计范围为沪深300 成分股，计算方法与美股端点的标普500 均线广度相同。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from io import BytesIO
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd
    import requests

from lib.breadth_trace import RefreshTrace, observe
from lib.constituent_cache import ConstituentCache
from lib.market_snapshot_cache import (
    BEIJING_TZ,
    MarketSnapshotCache,
    MarketSnapshotCacheConfig,
    SnapshotRefreshResult,
)
from lib.utils.trading_calendar import MARKET_CN, next_trading_day

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
FULL_REFRESH_DAYS = 14
QUOTE_BATCH_SIZE = 50

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
        refresh_state_key="market:zh-breadth:history:v1",
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


@dataclass(frozen=True)
class _HistoryState:
    constituents: list[str]
    last_full_refresh_date: date
    asof: date
    matrix: pd.DataFrame

    @classmethod
    def from_json(cls, value: Any) -> _HistoryState:
        import pandas as pd

        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("Invalid breadth history version")
        constituents = value["constituents"]
        columns = value["columns"]
        for symbols in (constituents, columns):
            if (
                not isinstance(symbols, list)
                or not symbols
                or any(
                    not isinstance(symbol, str)
                    or not re.fullmatch(r"[0-9]{6}", symbol)
                    for symbol in symbols
                )
                or len(set(symbols)) != len(symbols)
            ):
                raise ValueError("Invalid breadth history symbols")
        if len(constituents) < CSI300_MIN_VALID or not set(columns) <= set(constituents):
            raise ValueError("Invalid breadth history constituents")
        asof = date.fromisoformat(value["asof"])
        last_full = date.fromisoformat(value["last_full_refresh_date"])
        dates = [date.fromisoformat(day) for day in value["dates"]]
        if (
            not dates
            or len(dates) > HIST_LOOKBACK_DAYS + 1
            or dates != sorted(set(dates))
            or dates[-1] != asof
            or dates[0] < asof - timedelta(days=HIST_LOOKBACK_DAYS)
            or last_full > asof
        ):
            raise ValueError("Invalid breadth history dates")
        values = value["values"]
        if not isinstance(values, list) or len(values) != len(dates):
            raise ValueError("Invalid breadth history rows")
        for row in values:
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError("Invalid breadth history row width")
            if any(
                value is not None
                and (type(value) not in (int, float) or not math.isfinite(value))
                for value in row
            ):
                raise ValueError("Invalid breadth history close")
        matrix = pd.DataFrame(
            values, columns=columns, index=pd.to_datetime(dates), dtype=float
        )
        if int((matrix.iloc[-1] > 0).sum()) < CSI300_MIN_VALID:
            raise ValueError("Insufficient breadth history target closes")
        return cls(constituents, last_full, asof, matrix)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "constituents": self.constituents,
            "last_full_refresh_date": self.last_full_refresh_date.isoformat(),
            "asof": self.asof.isoformat(),
            "dates": [day.date().isoformat() for day in self.matrix.index],
            "columns": list(self.matrix.columns),
            "values": (
                self.matrix.astype(object).where(self.matrix.notna(), None).values.tolist()
            ),
        }


def _fetch_latest_closes(
    symbols: list[str], refresh_date: date, *, trace: RefreshTrace | None = None
) -> dict[str, float]:
    import requests

    closes: dict[str, float] = {}
    current = _cache.now().astimezone(BEIJING_TZ).replace(tzinfo=None)
    with requests.Session() as session:
        for offset in range(0, len(symbols), QUOTE_BATCH_SIZE):
            batch = {
                _tencent_symbol(symbol): symbol
                for symbol in symbols[offset : offset + QUOTE_BATCH_SIZE]
            }
            for attempt in range(2):
                try:
                    with observe(
                        trace,
                        "source",
                        "quotes",
                        source="tencent_quotes",
                        unit="http_request",
                        batch_id=offset // QUOTE_BATCH_SIZE + 1,
                        attempt=attempt + 1,
                    ) as observation:
                        observation["requested_count"] = len(batch)
                        observation["valid_count"] = 0
                        response = session.get(
                            "https://qt.gtimg.cn/q=" + ",".join(batch), timeout=(5, 20)
                        )
                        response.raise_for_status()
                        seen: dict[str, str] = {}
                        conflicts: set[str] = set()
                        for key, record in re.findall(
                            r'v_([a-z]{2}[0-9]{6})="([^"\r\n]*)";', response.text
                        ):
                            if key not in batch:
                                continue
                            if key in seen and seen[key] != record:
                                conflicts.add(key)
                            seen[key] = record
                            if key in conflicts:
                                closes.pop(batch[key], None)
                                continue
                            fields = record.split("~")
                            if (
                                len(fields) <= 30
                                or fields[2] != batch[key]
                                or not re.fullmatch(r"[0-9]{14}", fields[30])
                            ):
                                continue
                            try:
                                price = float(fields[3])
                                timestamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
                            except ValueError:
                                continue
                            if (
                                math.isfinite(price)
                                and price > 0
                                and timestamp.date() == refresh_date
                                and timestamp.time() >= datetime_time(15)
                                and timestamp <= current
                            ):
                                closes[batch[key]] = price
                        valid_count = sum(symbol in closes for symbol in batch.values())
                        observation["valid_count"] = valid_count
                        observation["missing_count"] = len(batch) - valid_count
                        if not valid_count:
                            raise ValueError(
                                "Tencent quote batch has no valid target-day closes"
                            )
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 1:
                        logger.warning(
                            "Tencent quote batch %s failed", offset // QUOTE_BATCH_SIZE + 1
                        )
                    else:
                        time.sleep(CALL_INTERVAL_SECONDS)
    if trace:
        trace.emit("snapshot", quote_valid_count=len(closes))
    if len(closes) < CSI300_MIN_VALID:
        raise ValueError(
            f"CSI300 target-day valid closes fewer than {CSI300_MIN_VALID}: {len(closes)}"
        )
    return closes


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
) -> SnapshotRefreshResult:
    import pandas as pd

    from lib.utils.market_metrics_utils import normalize_close_matrix, sma_breadth

    logger.debug("Fetching China market breadth for %s", refresh_date)
    symbols = _constituent_cache.get(lambda: _csi300_symbols(trace=trace), trace=trace)
    raw_state = _cache.read_refresh_state()
    state = None
    reason = "cold"
    if raw_state is not None:
        try:
            state = _HistoryState.from_json(raw_state)
        except (ValueError, TypeError, KeyError, OverflowError):
            reason = "corrupt"
            logger.warning("Invalid CSI300 history state; fetching full history")
    mode = "full"
    if state is not None:
        if set(state.constituents) != set(symbols):
            reason = "constituents_changed"
        elif refresh_date < state.asof:
            reason = "older_target"
        elif refresh_date == state.asof:
            mode, reason = "reuse", "same_target"
        elif (refresh_date - state.last_full_refresh_date).days >= FULL_REFRESH_DAYS:
            reason = "periodic"
        elif next_trading_day(state.asof, MARKET_CN) != refresh_date:
            reason = "missing_trading_days"
        else:
            mode, reason = "incremental", "next_trading_day"
    if trace:
        trace.emit(
            "snapshot",
            mode=mode,
            reason=reason,
            last_full_refresh_date=(
                state.last_full_refresh_date.isoformat() if state else None
            ),
        )
    if mode == "full":
        close_matrix = _fetch_close_matrix(symbols, refresh_date, trace=trace)
        last_full = refresh_date
    elif mode == "incremental":
        closes = _fetch_latest_closes(symbols, refresh_date, trace=trace)
        close_matrix = pd.concat(
            [
                state.matrix,
                pd.DataFrame([closes], index=pd.to_datetime([refresh_date])).reindex(
                    columns=state.matrix.columns
                ),
            ]
        )
        last_full = state.last_full_refresh_date
    else:
        close_matrix = state.matrix
        last_full = state.last_full_refresh_date

    close_matrix = normalize_close_matrix(close_matrix)
    close_matrix = close_matrix.loc[
        (
            close_matrix.index
            >= pd.Timestamp(refresh_date - timedelta(days=HIST_LOOKBACK_DAYS))
        )
        & (close_matrix.index <= pd.Timestamp(refresh_date))
    ]
    close_matrix = close_matrix.replace([float("inf"), float("-inf")], float("nan"))
    if pd.Timestamp(refresh_date) in close_matrix.index:
        target_row = close_matrix.loc[pd.Timestamp(refresh_date)]
        close_matrix.loc[pd.Timestamp(refresh_date)] = target_row.where(target_row > 0)
    target_count = (
        int((close_matrix.loc[pd.Timestamp(refresh_date)] > 0).sum())
        if pd.Timestamp(refresh_date) in close_matrix.index
        else 0
    )
    if trace:
        trace.emit("snapshot", target_count=target_count, history_rows=len(close_matrix))
    if target_count < CSI300_MIN_VALID:
        raise ValueError(
            f"CSI300 target-day valid closes fewer than {CSI300_MIN_VALID}: {target_count}"
        )
    with observe(trace, "stage", "calculation"):
        breadth = sma_breadth(close_matrix)
    breadth["universe"] = "沪深300"
    breadth["partial"] = target_count < len(symbols)
    new_state = _HistoryState(symbols, last_full, refresh_date, close_matrix).to_json()
    if trace:
        trace.emit(
            "snapshot",
            last_full_refresh_date=last_full.isoformat(),
            state_bytes=len(
                json.dumps(new_state, ensure_ascii=False, allow_nan=False).encode()
            ),
        )
    return SnapshotRefreshResult(
        breadth, new_state if state is None or refresh_date >= state.asof else None
    )


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
