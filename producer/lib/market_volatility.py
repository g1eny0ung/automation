from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import TypedDict

from lib.breadth_trace import RefreshTrace, observe

logger = logging.getLogger(__name__)


class VolatilityReading(TypedDict):
    value: float | None
    date: str | None


class VolatilitySnapshot(TypedDict):
    VIX: VolatilityReading
    DSPX: VolatilityReading
    COR1M: VolatilityReading
    MOVE: VolatilityReading


def fetch_us_volatility(
    refresh_date: date, *, trace: RefreshTrace | None = None
) -> VolatilitySnapshot:
    history = None
    try:
        import pandas as pd
        import yfinance as yf

        with observe(
            trace,
            "source",
            "volatility_batch",
            source="yfinance",
            unit="library_call",
            attempt=1,
        ):
            history = yf.download(
                ["^VIX", "^DSPX", "^COR1M", "^MOVE"],
                start=(refresh_date - timedelta(days=35)).isoformat(),
                end=(refresh_date + timedelta(days=1)).isoformat(),
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
                timeout=12,
            )
    except Exception as exc:
        logger.warning("Volatility download unavailable: %s", exc)

    def reading(symbol: str) -> VolatilityReading:
        if history is None:
            return {"value": None, "date": None}
        try:
            closes = pd.to_numeric(history[symbol]["Close"], errors="coerce")
            dates = pd.to_datetime(closes.index, errors="coerce")
            eligible = [
                (timestamp.date(), float(value))
                for timestamp, value in zip(dates, closes)
                if not pd.isna(timestamp)
                and timestamp.date() <= refresh_date
                and pd.notna(value)
                and math.isfinite(value)
            ]
            if not eligible:
                raise ValueError(f"No eligible close for {symbol}")
            observed, value = max(eligible, key=lambda item: item[0])
            return {"value": value, "date": observed.isoformat()}
        except Exception as exc:
            logger.warning("Volatility reading unavailable for %s: %s", symbol, exc)
            return {"value": None, "date": None}

    result = {
        "VIX": reading("^VIX"),
        "DSPX": reading("^DSPX"),
        "COR1M": reading("^COR1M"),
        "MOVE": reading("^MOVE"),
    }

    if trace:
        for symbol, value in result.items():
            trace.emit(
                "reading",
                source="yfinance",
                symbol=symbol,
                available=value["value"] is not None,
                date=value["date"],
            )
    return result
