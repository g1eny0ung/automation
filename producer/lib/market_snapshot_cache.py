from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from logging import Logger
from math import ceil
from typing import Any, Callable
from zoneinfo import ZoneInfo

from lib.breadth_trace import RefreshTrace, observe
from lib.json_cache import JsonCache, JsonCacheConfig
from lib.utils.trading_calendar import (
    MARKET_CN,
    MARKET_US,
    is_trading_day,
    next_trading_day,
    previous_trading_day,
)
from server_error import CacheWarmingError

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class MarketSnapshotCacheConfig:
    cache_key: str
    redis_key_prefix: str
    source: str
    refresh_hour: int
    payload_data_error: str
    warming_message: str
    refresh_failure_message: str
    release_lock_failure_message: str
    # 交易日历口径：CN 公休假日（A 股）/ US NYSE 日历（美股）。
    market: str = MARKET_CN
    redis_ttl_seconds: int = 60 * 60 * 24 * 7
    redis_lock_ttl_seconds: int = 180
    # read() 找不到目标日缓存时，最多查找此前多少个交易日；0 表示只查目标日。
    read_fallback_trading_days: int = 0
    l1_max_age_seconds: int | None = None
    publish_requires_lock: bool = False
    refresh_state_key: str | None = None


@dataclass(frozen=True)
class SnapshotRefreshResult:
    data: dict[str, Any]
    state: dict[str, Any] | None = None


class MarketSnapshotCache:
    def __init__(self, config: MarketSnapshotCacheConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self._cache = JsonCache(
            JsonCacheConfig(
                cache_key=config.cache_key,
                redis_key_prefix=config.redis_key_prefix,
                redis_ttl_seconds=config.redis_ttl_seconds,
            )
        )

    def read_refresh_state(self) -> Any:
        key = self.config.refresh_state_key
        if key is None:
            return None
        raw = self._cache.get_redis_client().get(key)
        if raw is None or isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (ValueError, TypeError, UnicodeDecodeError):
            self.logger.warning("Invalid refresh state at %s", key)
            return raw

    def reset_l1_cache(self) -> None:
        self._cache.reset_l1_cache()

    def now(self) -> datetime:
        return datetime.now(BEIJING_TZ)

    def refresh_date(self, current: datetime) -> date:
        beijing_now = current.astimezone(BEIJING_TZ)
        if beijing_now.time() < datetime_time(hour=self.config.refresh_hour):
            candidate = beijing_now.date() - timedelta(days=1)
        else:
            candidate = beijing_now.date()
        # 美股在北京时间次日早上刷新；08:00 前仍使用上一次刷新对应的交易日。
        if self.config.market == MARKET_US:
            candidate -= timedelta(days=1)
        # refresh_date 是缓存目标交易日，实际数据仍可能因上游滞后而更早。
        while not is_trading_day(candidate, self.config.market):
            candidate -= timedelta(days=1)
        return candidate

    def next_refresh_boundary(self, current: datetime) -> datetime:
        """下一目标交易日的刷新时刻；美股在交易日次日北京时间刷新。"""
        candidate = next_trading_day(self.refresh_date(current), self.config.market)
        if self.config.market == MARKET_US:
            candidate += timedelta(days=1)
        return datetime.combine(
            candidate,
            datetime_time(hour=self.config.refresh_hour),
            tzinfo=BEIJING_TZ,
        )

    def seconds_until_next_refresh(self, current: datetime) -> int:
        beijing_now = current.astimezone(BEIJING_TZ)
        seconds = int(
            (self.next_refresh_boundary(beijing_now) - beijing_now).total_seconds()
        )
        return max(seconds, 1)

    def redis_key(self, refresh_date: date) -> str:
        return self._cache.redis_key(refresh_date.isoformat())

    def redis_ttl_seconds(self, refresh_date: date, current: datetime) -> int:
        ttl = self.config.redis_ttl_seconds
        if self.config.read_fallback_trading_days:
            # 长假可能超过默认 TTL，保留到该快照不再属于回退窗口为止。
            expires_date = refresh_date
            for _ in range(self.config.read_fallback_trading_days + 1):
                expires_date = next_trading_day(expires_date, self.config.market)
            if self.config.market == MARKET_US:
                expires_date += timedelta(days=1)
            expires_at = datetime.combine(
                expires_date,
                datetime_time(hour=self.config.refresh_hour),
                tzinfo=BEIJING_TZ,
            )
            ttl = max(ttl, ceil((expires_at - current).total_seconds()))
        return ttl

    def build_payload(
        self, records: dict[str, Any], refresh_date: date, current: datetime
    ) -> dict[str, Any]:
        return {
            "refresh_date": refresh_date.isoformat(),
            "refreshed_at": current.astimezone(BEIJING_TZ).isoformat(),
            "source": self.config.source,
            "data": records,
        }

    def payload_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(self.config.payload_data_error)
        return data

    def get_l1_payload(self, refresh_date: date) -> dict[str, Any] | None:
        return self._cache.get_l1_payload("refresh_date", refresh_date.isoformat())

    def set_l1_payload(self, payload: dict[str, Any], current: datetime) -> None:
        ttl = self.seconds_until_next_refresh(current)
        if self.config.l1_max_age_seconds is not None:
            ttl = min(ttl, self.config.l1_max_age_seconds)
        self._cache.set_l1_payload(payload, ttl)

    def _acquire_refresh_lock(self) -> str | None:
        token = uuid.uuid4().hex
        redis = self._cache.get_redis_client()
        acquired = redis.set(
            f"{self.config.redis_key_prefix}:refresh-lock",
            token,
            ex=self.config.redis_lock_ttl_seconds,
            nx=True,
        )
        if acquired:
            return token
        return None

    def _release_refresh_lock(self, token: str) -> None:
        try:
            key = f"{self.config.redis_key_prefix}:refresh-lock"
            redis = self._cache.get_redis_client()
            # 比较与删除必须原子执行，避免旧持锁者误删过期后重新取得的锁。
            redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                keys=[key],
                args=[token],
            )
        except Exception:
            self.logger.exception(self.config.release_lock_failure_message)

    def fetch(
        self,
        fetch_records: Callable[[date], dict[str, Any]],
        current: datetime | None = None,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        """先读取缓存，找不到时在本次请求中抓取数据并写入缓存，供宏观和大盘表现接口使用。"""
        current = current or self.now()
        refresh_date = self.refresh_date(current)

        payload = self.get_l1_payload(refresh_date)
        if not payload:
            payload = self._cache.redis_get_json(self.redis_key(refresh_date))
            if payload:
                self.set_l1_payload(payload, current)

        if not payload:
            # 冷启动或 Redis 空缓存时，无论是否到达刷新时间，都要主动补缓存。
            lock_token = self._acquire_refresh_lock()
            if not lock_token:
                raise CacheWarmingError(self.config.warming_message)

            try:
                records = fetch_records(refresh_date)
                # refreshed_at 与 L1 TTL 以完成时刻计算。
                completed_at = self.now()
                payload = self.build_payload(records, refresh_date, completed_at)
                self._cache.redis_set_json(
                    self.redis_key(refresh_date),
                    payload,
                    ttl_seconds=self.redis_ttl_seconds(refresh_date, completed_at),
                )
                self.set_l1_payload(payload, completed_at)
            except Exception:
                self.logger.exception(self.config.refresh_failure_message)
                raise
            finally:
                self._release_refresh_lock(lock_token)

        return payload if include_metadata else self.payload_data(payload)

    def refresh(
        self,
        fetch_records: Callable[[date], dict[str, Any] | SnapshotRefreshResult],
        current: datetime | None = None,
        include_metadata: bool = False,
        trace: RefreshTrace | None = None,
    ) -> dict[str, Any]:
        """强制刷新当前 refresh_date 的缓存；幂等，可安全重试。

        供定时任务主动调用。include_metadata=True 时返回包含日期和来源的完整快照，
        否则只返回 data。其他进程正在刷新时抛出 CacheWarmingError。
        """
        current = current or self.now()
        refresh_date = self.refresh_date(current)

        if trace:
            trace.emit("snapshot", refresh_date=refresh_date.isoformat())
        with observe(trace, "stage", "redis_lock"):
            lock_token = self._acquire_refresh_lock()
        if not lock_token:
            raise CacheWarmingError(self.config.warming_message)

        try:
            with observe(trace, "stage", "fetch_and_calculate"):
                result = fetch_records(refresh_date)
                if not isinstance(result, SnapshotRefreshResult):
                    result = SnapshotRefreshResult(result)
                records = result.data
            state_json = None
            if result.state is not None:
                if (
                    not self.config.refresh_state_key
                    or not self.config.publish_requires_lock
                ):
                    raise ValueError("Refresh state requires a key and locked publication")
                state_json = json.dumps(result.state, ensure_ascii=False, allow_nan=False)
            # refreshed_at 与 L1 TTL 以完成时刻计算。
            completed_at = self.now()
            payload = self.build_payload(records, refresh_date, completed_at)
            if trace:
                trace.emit(
                    "snapshot",
                    refreshed_at=payload["refreshed_at"],
                    payload_sha256=hashlib.sha256(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                    data_date=records.get("date"),
                    universe_size=records.get("universe_size"),
                    partial=records.get("partial"),
                )
            with observe(trace, "stage", "redis_publication") as publication:
                try:
                    if self.config.publish_requires_lock:
                        keys = [
                            f"{self.config.redis_key_prefix}:refresh-lock",
                            self.redis_key(refresh_date),
                        ]
                        args = [
                            lock_token,
                            json.dumps(payload, ensure_ascii=False, allow_nan=False),
                            str(self.redis_ttl_seconds(refresh_date, completed_at)),
                        ]
                        if state_json is not None:
                            keys.append(self.config.refresh_state_key)
                            args.append(state_json)
                        published = self._cache.get_redis_client().eval(
                            "if redis.call('get', KEYS[1]) == ARGV[1] then "
                            "redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3]); "
                            "if #KEYS == 3 then redis.call('set', KEYS[3], ARGV[4]); end; "
                            "return 1 else return 0 end",
                            keys=keys,
                            args=args,
                        )
                    else:
                        self._cache.redis_set_json(
                            self.redis_key(refresh_date),
                            payload,
                            ttl_seconds=self.redis_ttl_seconds(
                                refresh_date, completed_at
                            ),
                        )
                        published = True
                except Exception:
                    publication["publication"] = "unknown"
                    raise
                if not published:
                    publication["publication"] = "rejected_lock_expired"
                    raise RuntimeError("Refresh lock expired before publication")
                publication["publication"] = "confirmed"
            self.set_l1_payload(payload, completed_at)
        except Exception:
            self.logger.exception(self.config.refresh_failure_message)
            raise
        finally:
            self._release_refresh_lock(lock_token)

        return payload if include_metadata else self.payload_data(payload)

    def read(
        self,
        current: datetime | None = None,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        """只读取缓存，不触发上游抓取；找不到目标日缓存时，按交易日向前查找。

        最多回退 read_fallback_trading_days 个交易日，仍找不到时抛出
        CacheWarmingError。找到的旧快照写入 L1 时保留原有 refresh_date，
        后续请求不会将它当成目标日的缓存，仍会先查找目标日的数据。
        """
        current = current or self.now()
        expected = self.refresh_date(current)
        candidate = expected

        payload = self._read_payload(candidate, current)
        for _ in range(self.config.read_fallback_trading_days):
            if payload:
                break
            candidate = previous_trading_day(candidate, self.config.market)
            payload = self._read_payload(candidate, current)

        if not payload:
            raise CacheWarmingError(self.config.warming_message)
        return payload if include_metadata else self.payload_data(payload)

    def _read_payload(
        self, refresh_date: date, current: datetime
    ) -> dict[str, Any] | None:
        payload = self.get_l1_payload(refresh_date)
        if not payload:
            payload = self._cache.redis_get_json(self.redis_key(refresh_date))
            if payload:
                self.set_l1_payload(payload, current)
        return payload
