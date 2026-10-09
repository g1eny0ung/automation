from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def redact(message: str) -> str:
    for name, value in os.environ.items():
        if value and any(word in name for word in ("TOKEN", "SECRET", "PASSWORD")):
            message = message.replace(value, "[redacted]")
    message = re.sub(r"https?://\S+", "[url]", message)
    return message


def error_details(exc: BaseException) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": redact(str(exc))[:500]}


class RedactedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RefreshTrace:
    """刷新事件逐行落盘，进程被终止后仍可读取已完成的记录。"""

    def __init__(self, path: str | Path, market: str, revision: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._sequence = 0
        self.emit(
            "run_start", market=market, revision=revision, run_id=uuid.uuid4().hex
        )

    def emit(self, event: str, **fields: Any) -> None:
        with self._lock:
            self._file.write(
                json.dumps(
                    {
                        "event": event,
                        "wall_seconds": round(time.monotonic() - self._start, 6),
                        **fields,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._file.flush()

    @contextmanager
    def scope(
        self, kind: str, operation: str, **fields: Any
    ) -> Iterator[dict[str, Any]]:
        with self._lock:
            self._sequence += 1
            identity = self._sequence
        details = {"kind": kind, "operation": operation, **fields, "id": identity}
        self.emit("start", **details)
        started = time.monotonic()
        result: dict[str, Any] = {}
        try:
            yield result
        except BaseException as exc:
            result.update(status="failure", **error_details(exc))
            evidence = f"{type(exc).__name__} {exc}".lower()
            result["timeout"] = any(
                word in evidence for word in ("timeout", "timed out", "time out")
            )
            raise
        finally:
            result.setdefault("status", "success")
            self.emit(
                "end",
                **details,
                elapsed_seconds=round(time.monotonic() - started, 6),
                **result,
            )

    def close(self) -> None:
        self._file.close()


@contextmanager
def observe(
    trace: RefreshTrace | None, kind: str, operation: str, **fields: Any
) -> Iterator[dict[str, Any]]:
    if trace is None:
        yield {}
    else:
        with trace.scope(kind, operation, **fields) as result:
            yield result


def summarize(path: str | Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "missing_log",
        "sources": {},
        "failed_symbols": [],
        "fallbacks": [],
        "cache": [],
        "readings": [],
        "stages": [],
        "unfinished": [],
    }
    if not Path(path).exists():
        return summary
    active: dict[int, dict[str, Any]] = {}
    slow: list[dict[str, Any]] = []
    logical_calls: dict[str, set[tuple[Any, ...]]] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            summary["truncated_log"] = True
            continue
        summary["observed_wall_seconds"] = event["wall_seconds"]
        kind = event["event"]
        if kind == "run_start":
            summary.update({k: v for k, v in event.items() if k != "event"})
            summary["status"] = "interrupted"
        elif kind == "run_end":
            summary.update({k: v for k, v in event.items() if k != "event"})
        elif kind in ("cache", "fallback", "reading"):
            summary[
                {"cache": "cache", "fallback": "fallbacks", "reading": "readings"}[kind]
            ].append(event)
        elif kind == "failed_symbol":
            summary["failed_symbols"].append(event["symbol"])
        elif kind == "snapshot":
            summary.update(
                {k: v for k, v in event.items() if k not in ("event", "wall_seconds")}
            )
        elif kind in ("start", "end"):
            if kind == "start":
                active[event["id"]] = event
            else:
                active.pop(event["id"], None)
                if event["kind"] == "stage":
                    summary["stages"].append(event)
                    if event["operation"] == "redis_publication":
                        summary["publication"] = event.get("publication", "unknown")
                if event["kind"] == "symbol":
                    slow.append(event)
            if event["kind"] == "source":
                logical_calls.setdefault(event["source"], set()).add(
                    (event["operation"], event.get("symbol"))
                )
                source = summary["sources"].setdefault(
                    event["source"],
                    {
                        "unit": event["unit"],
                        "attempts_started": 0,
                        "attempts_completed": 0,
                        "successes": 0,
                        "failures": 0,
                        "timeouts": 0,
                        "elapsed_sum_seconds": 0.0,
                        "elapsed_max_seconds": 0.0,
                    },
                )
                if kind == "start":
                    source["attempts_started"] += 1
                else:
                    source["attempts_completed"] += 1
                    source[
                        "successes" if event["status"] == "success" else "failures"
                    ] += 1
                    source["timeouts"] += int(event.get("timeout", False))
                    source["elapsed_sum_seconds"] += event["elapsed_seconds"]
                    source["elapsed_max_seconds"] = max(
                        source["elapsed_max_seconds"], event["elapsed_seconds"]
                    )
    for source, calls in logical_calls.items():
        summary["sources"][source]["logical_calls"] = len(calls)
    summary["unfinished"] = list(active.values())
    if any(event["operation"] == "redis_publication" for event in active.values()):
        summary["publication"] = "unknown"
    summary["failed_symbols_count"] = len(summary["failed_symbols"])
    summary["slow_symbols"] = sorted(
        slow, key=lambda item: item["elapsed_seconds"], reverse=True
    )[:10]
    return summary
