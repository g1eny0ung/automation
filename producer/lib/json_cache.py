from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from cachetools import TTLCache

logger = getLogger(__name__)


@dataclass(frozen=True)
class JsonCacheConfig:
    cache_key: str
    redis_key_prefix: str
    redis_ttl_seconds: int | None = 60 * 60 * 48


class JsonCache:
    def __init__(self, config: JsonCacheConfig) -> None:
        self.config = config
        # cachetools 不支持并发读写，Flask 多线程运行时需加锁保护。
        self._l1_lock = threading.Lock()
        self._l1_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=1, ttl=1)
        self._redis_client: Any | None = None

    def reset_l1_cache(self) -> None:
        with self._l1_lock:
            self._l1_cache = TTLCache(maxsize=1, ttl=1)

    def get_redis_client(self) -> Any:
        if self._redis_client:
            return self._redis_client

        url = os.environ.get("UPSTASH_REDIS_REST_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            raise ValueError(
                "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be set"
            )

        from upstash_redis import Redis

        self._redis_client = Redis(url=url, token=token)
        return self._redis_client

    def redis_key(self, key_suffix: str) -> str:
        return f"{self.config.redis_key_prefix}:{key_suffix}"

    def get_l1_payload(
        self, payload_key: str, payload_value: str
    ) -> dict[str, Any] | None:
        with self._l1_lock:
            payload = self._l1_cache.get(self.config.cache_key)
        if payload and payload.get(payload_key) == payload_value:
            return payload
        return None

    def set_l1_payload(self, payload: dict[str, Any], ttl_seconds: int) -> None:
        with self._l1_lock:
            self._l1_cache = TTLCache(maxsize=1, ttl=max(ttl_seconds, 1))
            self._l1_cache[self.config.cache_key] = payload

    def redis_get_json(self, key: str) -> dict[str, Any] | None:
        raw = self.get_redis_client().get(key)
        if raw is None:
            return None
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if isinstance(raw, str):
                value = json.loads(raw)
            elif isinstance(raw, dict):
                value = raw
            else:
                raise ValueError(
                    f"Unsupported cached Redis value type: {type(raw).__name__}"
                )
            if not isinstance(value, dict):
                raise ValueError("Cached Redis value is not a JSON object")
        except ValueError as exc:
            # 损坏的缓存值视为未命中：删除该键，让下次请求重新生成缓存，而不是一直报错。
            logger.warning("Discarding corrupted Redis cache value for %s: %s", key, exc)
            self.redis_delete(key)
            return None
        return value

    def redis_set_json(
        self,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        self.get_redis_client().set(
            key,
            json.dumps(value, ensure_ascii=False),
            ex=self.config.redis_ttl_seconds if ttl_seconds is None else ttl_seconds,
        )

    def redis_delete(self, key: str) -> None:
        try:
            self.get_redis_client().delete(key)
        except Exception:
            logger.exception("Failed to delete Redis key: %s", key)
