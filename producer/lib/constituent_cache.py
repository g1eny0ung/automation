from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Any

from lib.breadth_trace import RefreshTrace, observe
from lib.json_cache import JsonCache, JsonCacheConfig

logger = getLogger(__name__)


@dataclass(frozen=True)
class _ConstituentRecord:
    symbols: tuple[str, ...]
    refreshed_at: datetime


class ConstituentCache:
    def __init__(self, index: str, *, min_symbols: int) -> None:
        self._key = f"market:constituents:{index}:v1"
        self._min_symbols = min_symbols
        self._cache = JsonCache(
            JsonCacheConfig(
                cache_key=self._key,
                redis_key_prefix=self._key,
                redis_ttl_seconds=None,
            )
        )

    def get(
        self, fetcher: Callable[[], list[str]], *, trace: RefreshTrace | None = None
    ) -> list[str]:
        with observe(trace, "stage", "constituent_cache_read"):
            payload = self._cache.redis_get_json(self._key)
        now = datetime.now(timezone.utc)
        record = None
        if payload is not None:
            try:
                record = self._parse_record(payload, now)
            except ValueError as exc:
                logger.warning(
                    "Discarding invalid constituents for %s: %s", self._key, exc
                )
                self._cache.redis_delete(self._key)
        if record is not None and now - record.refreshed_at < timedelta(days=7):
            if trace:
                trace.emit(
                    "cache",
                    key=self._key,
                    status="hit",
                    refreshed_at=record.refreshed_at.isoformat(),
                )
            return list(record.symbols)

        if trace:
            trace.emit("cache", key=self._key, status="expired" if record else "miss")
        try:
            symbols = self._validate_symbols(fetcher())
        except Exception:
            if record is None:
                raise
            logger.warning(
                "Failed to refresh %s; using constituents last refreshed at %s",
                self._key,
                record.refreshed_at.isoformat(),
                exc_info=True,
            )
            if trace:
                trace.emit(
                    "cache",
                    key=self._key,
                    status="stale_fallback",
                    refreshed_at=record.refreshed_at.isoformat(),
                )
            return list(record.symbols)

        refreshed_at = datetime.now(timezone.utc)
        self._cache.redis_set_json(
            self._key,
            {
                "symbols": list(symbols),
                "refreshed_at": refreshed_at.isoformat(),
            },
        )
        if trace:
            trace.emit(
                "cache",
                key=self._key,
                status="refreshed",
                refreshed_at=refreshed_at.isoformat(),
            )
        return list(symbols)

    def _parse_record(
        self, payload: dict[str, Any], now: datetime
    ) -> _ConstituentRecord:
        symbols = self._validate_symbols(payload.get("symbols"))
        timestamp = payload.get("refreshed_at")
        if not isinstance(timestamp, str):
            raise ValueError("Constituent refresh timestamp must be a string")
        refreshed_at = datetime.fromisoformat(timestamp)
        if refreshed_at.tzinfo is None or refreshed_at.utcoffset() is None:
            raise ValueError("Constituent refresh timestamp must include a timezone")
        if refreshed_at > now:
            raise ValueError("Constituent refresh timestamp is in the future")
        return _ConstituentRecord(symbols, refreshed_at)

    def _validate_symbols(self, value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) < self._min_symbols:
            raise ValueError(
                f"Constituents must contain at least {self._min_symbols} symbols"
            )
        if any(
            not isinstance(symbol, str)
            or re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]*", symbol) is None
            for symbol in value
        ):
            raise ValueError("Constituents contain invalid symbols")
        if len(set(value)) != len(value):
            raise ValueError("Constituents contain duplicate symbols")
        return tuple(value)
