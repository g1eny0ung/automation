"""美股大盘广度：标普500 成分股站上均线占比。

数据源为 Hugging Face 公共数据集 defeatbeta/yahoo-finance-data 的
stock_prices.parquet 文件，通过 DuckDB 的 httpfs 扩展读取。一次查询取出全部成分股的收盘价。
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import date, timedelta
from math import isfinite
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from lib.breadth_trace import RefreshTrace, observe
from lib.constituent_cache import ConstituentCache
from lib.market_snapshot_cache import (
    MarketSnapshotCache,
    MarketSnapshotCacheConfig,
)
from lib.utils.trading_calendar import MARKET_US

if TYPE_CHECKING:
    import duckdb
    import pandas as pd

logger = logging.getLogger(__name__)

REFRESH_HOUR = 8
SOURCE = "hf_defeatbeta_duckdb_sp500"
EASTERN_TZ = ZoneInfo("America/New_York")

# 用 DuckDB 读取 Hugging Face 数据集中的美股日线文件。
HF_PRICES_URL = (
    "https://huggingface.co/datasets/defeatbeta/yahoo-finance-data/"
    "resolve/main/data/US/stock_prices.parquet"
)
# 覆盖 200 日均线所需窗口，按自然日回看。
DUCKDB_LOOKBACK_DAYS = 380
# 为周末和节假日留出余量，数据日期落后美东当天超过 4 天时，才按日期过旧报错。
MAX_DATASET_LAG_DAYS = 4
DUCKDB_HTTP_TIMEOUT_SECONDS = 90

UNIVERSE_LABEL = "S&P 500"
MIN_CONSTITUENTS = 400
MIN_VALID_RATIO = 0.6
REQUEST_TIMEOUT_SECONDS = (5, 20)
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
GITHUB_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

REDIS_KEY_PREFIX = "market:en-breadth:v4"
REDIS_LOCK_TTL_SECONDS = 1200

_constituent_cache = ConstituentCache("sp500", min_symbols=MIN_CONSTITUENTS)

_cache = MarketSnapshotCache(
    MarketSnapshotCacheConfig(
        cache_key="market_en_breadth",
        redis_key_prefix=REDIS_KEY_PREFIX,
        source=SOURCE,
        refresh_hour=REFRESH_HOUR,
        market=MARKET_US,
        read_fallback_trading_days=3,
        redis_lock_ttl_seconds=REDIS_LOCK_TTL_SECONDS,
        l1_max_age_seconds=300,
        publish_requires_lock=True,
        payload_data_error="Cached US breadth payload missing data object",
        warming_message="US market breadth cache is warming",
        refresh_failure_message="Failed to refresh US market breadth cache",
        release_lock_failure_message=(
            "Failed to release US market breadth refresh lock"
        ),
    ),
    logger,
)


def _yahoo_symbol(symbol: str) -> str:
    # Wikipedia 使用 BRK.B 形式，Yahoo 代码为 BRK-B。
    return symbol.strip().upper().replace(".", "-")


def _parse_cik(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        cik = int(value.strip())
    elif isinstance(value, Integral) and not isinstance(value, bool):
        cik = int(value)
    elif (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and isfinite(value)
        and value == int(value)
    ):
        cik = int(value)
    else:
        raise ValueError(f"Invalid constituent CIK: {value!r}")
    if cik <= 0:
        raise ValueError(f"Invalid constituent CIK: {value!r}")
    return cik


def _parse_constituents(frame: pd.DataFrame) -> list[str]:
    import pandas as pd

    if frame.empty or not {"Symbol", "CIK"}.issubset(frame.columns):
        raise ValueError("Constituent table missing Symbol or CIK column")
    companies: dict[int, str] = {}
    symbol_ciks: dict[str, int] = {}
    for raw_symbol, raw_cik in frame[["Symbol", "CIK"]].itertuples(
        index=False, name=None
    ):
        if pd.isna(raw_symbol) or not str(raw_symbol).strip():
            continue
        symbol = _yahoo_symbol(str(raw_symbol))
        cik = _parse_cik(raw_cik)
        if symbol in symbol_ciks and symbol_ciks[symbol] != cik:
            raise ValueError(f"Conflicting CIKs for constituent symbol: {symbol}")
        symbol_ciks[symbol] = cik
        companies[cik] = min(symbol, companies.get(cik, symbol))
    symbols = sorted(companies.values())
    if len(symbols) < MIN_CONSTITUENTS:
        raise ValueError(
            f"Constituent companies fewer than {MIN_CONSTITUENTS}: {len(symbols)}"
        )
    return symbols


def _fetch_constituents(*, trace: RefreshTrace | None = None) -> list[str]:
    """读取标普500 成分股列表，GitHub 数据集失败时改用 Wikipedia。"""
    errors: list[str] = []
    for source, url, reader in (
        ("github", GITHUB_URL, _read_github_symbols),
        ("wikipedia", WIKIPEDIA_URL, _read_wikipedia_symbols),
    ):
        try:
            if trace and source == "wikipedia":
                trace.emit("fallback", source=source)
            with observe(
                trace,
                "source",
                "constituents",
                source=source,
                unit="http_request",
                attempt=1,
            ):
                return reader(url)
        except Exception as exc:  # noqa: BLE001 - 当前数据源失败时尝试下一个
            errors.append(f"{url}: {exc}")
            logger.warning("Constituent source failed: %s", errors[-1])
    raise RuntimeError(f"All constituent sources failed: {'; '.join(errors)}")


def _read_wikipedia_symbols(url: str) -> list[str]:
    import pandas as pd
    import requests

    response = requests.get(
        url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    return _parse_constituents(tables[0])


def _read_github_symbols(url: str) -> list[str]:
    import pandas as pd
    import requests

    response = requests.get(
        url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return _parse_constituents(pd.read_csv(io.StringIO(response.text)))


def _duckdb_connection() -> duckdb.DuckDBPyConnection:
    import duckdb

    con = duckdb.connect()
    # Vercel 没有可用的 HOME，httpfs 初始化仍需要显式设置可写主目录。
    con.execute("SET home_directory = '/tmp'")
    # serverless 环境 HOME 通常不可写，扩展目录显式指向 /tmp（可用环境变量覆盖）。
    extension_dir = os.environ.get("DUCKDB_EXTENSION_DIR", "/tmp/duckdb-extensions")
    os.makedirs(extension_dir, exist_ok=True)
    con.execute(f"SET extension_directory = '{extension_dir}'")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET GLOBAL http_timeout = {DUCKDB_HTTP_TIMEOUT_SECONDS}")
    # 用多个线程下载远程 Parquet 文件的不同数据段。
    con.execute(f"SET GLOBAL threads = {os.environ.get('DUCKDB_THREADS', '8')}")
    return con


def _latest_dataset_date(con: duckdb.DuckDBPyConnection) -> date | None:
    import pandas as pd

    # 只读 parquet 元数据即可得到 max(report_date)，不用扫描实际数据。
    row = con.execute(
        f"SELECT max(report_date) FROM read_parquet('{HF_PRICES_URL}')"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return pd.Timestamp(row[0]).date()


def _dataset_is_stale(latest: date | None, eastern_today: date) -> bool:
    if latest is None:
        return True
    return (eastern_today - latest).days > MAX_DATASET_LAG_DAYS


def _fetch_close_matrix_via_duckdb(
    symbols: list[str], *, trace: RefreshTrace | None = None
) -> pd.DataFrame:
    """用 DuckDB 查询 Hugging Face 的 Parquet 文件，一次取出全部成分股的历史收盘价。"""
    import pandas as pd

    eastern_today = pd.Timestamp.now(tz=EASTERN_TZ).date()
    # 把股票代码直接写入 SQL 的 IN 列表，让 DuckDB 能利用 Parquet 的最小值和最大值统计，
    # 跳过不含这些代码的数据块。文件按 symbol 排序，便于跳过无关数据。
    for symbol in symbols:
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]*", symbol):
            raise ValueError(f"Unexpected symbol format: {symbol}")
    symbol_list = ", ".join(f"'{symbol}'" for symbol in symbols)

    with observe(trace, "stage", "duckdb_httpfs_setup"):
        con = _duckdb_connection()
    try:
        with observe(
            trace,
            "source",
            "latest_date",
            source="huggingface",
            unit="query",
            attempt=1,
        ):
            latest = _latest_dataset_date(con)
            if _dataset_is_stale(latest, eastern_today):
                raise ValueError(
                    f"HF dataset stale: latest={latest}, eastern_today={eastern_today}"
                )

        start = (eastern_today - timedelta(days=DUCKDB_LOOKBACK_DAYS)).isoformat()
        with observe(
            trace,
            "source",
            "close_matrix",
            source="huggingface",
            unit="query",
            attempt=1,
        ):
            df = con.execute(
                f"""
                SELECT symbol, report_date, close
                FROM read_parquet('{HF_PRICES_URL}')
                WHERE symbol IN ({symbol_list})
                  AND report_date >= '{start}'
                """
            ).fetchdf()
            if df.empty:
                raise ValueError("HF dataset returned no rows for constituents")

            matrix = df.pivot(index="report_date", columns="symbol", values="close")
            matrix.index = pd.to_datetime(matrix.index)
            matrix.columns.name = None

            if trace:
                for symbol in sorted(set(symbols) - set(matrix.columns)):
                    trace.emit("failed_symbol", symbol=symbol)
            valid_count = matrix.shape[1]
            if (
                valid_count < MIN_CONSTITUENTS
                or valid_count < len(symbols) * MIN_VALID_RATIO
            ):
                raise ValueError(
                    f"DuckDB breadth close matrix too sparse: {valid_count}/{len(symbols)}"
                )
            logger.info(
                "Breadth close matrix loaded via DuckDB: %s symbols", valid_count
            )
            return matrix
    finally:
        con.close()


def _fetch_en_breadth(
    refresh_date: date, *, trace: RefreshTrace | None = None
) -> dict[str, Any]:
    from lib.market_volatility import fetch_us_volatility
    from lib.utils.market_metrics_utils import advancers_share, sma_breadth

    logger.debug("Fetching US market breadth for %s", refresh_date)
    symbols = _constituent_cache.get(
        lambda: _fetch_constituents(trace=trace), trace=trace
    )
    close_matrix = _fetch_close_matrix_via_duckdb(symbols, trace=trace)
    # 只保留目标交易日及以前的数据，避免首次生成快照时混入尚未收盘的日线。
    close_matrix = close_matrix[close_matrix.index.date <= refresh_date]

    with observe(trace, "stage", "calculation"):
        breadth = sma_breadth(close_matrix)
        breadth["universe"] = UNIVERSE_LABEL
        # 取到的成分股数量不足时，将 partial 设为 True，表示广度只统计了部分成分股。
        breadth["partial"] = close_matrix.shape[1] < len(symbols)
        breadth["advancers_pct"] = advancers_share(close_matrix)
    breadth["volatility"] = fetch_us_volatility(refresh_date, trace=trace)
    return breadth


def fetch_en_market_breadth() -> dict[str, Any]:
    """只读缓存，最多回退 3 个美股交易日；缓存由 Actions 定时填充。"""
    return _cache.read(include_metadata=True)


def refresh_en_market_breadth(*, trace: RefreshTrace | None = None) -> dict[str, Any]:
    """供定时任务调用，重新计算广度并覆盖当前 refresh_date 的缓存，可重复调用。"""
    return _cache.refresh(
        lambda day: _fetch_en_breadth(day, trace=trace),
        include_metadata=True,
        trace=trace,
    )
