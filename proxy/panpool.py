"""Bounded executor for synchronous cloud-drive work."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_log = logging.getLogger("fnmedia.panpool")

MAX_WORKERS = 4
MAX_QUEUE = 8
_CAPACITY = threading.BoundedSemaphore(MAX_WORKERS + MAX_QUEUE)
_EXECUTOR: ThreadPoolExecutor | None = None
_LOCK = threading.Lock()
_METRICS = {
    "submitted": 0, "completed": 0, "failed": 0, "timeouts": 0,
    "rejected": 0, "wait_ms_total": 0.0, "queued_peak": 0,
    "running": 0, "queued": 0, "pending": 0,
}


class PoolBusyError(RuntimeError):
    """The dedicated pool has reached its running-plus-queued limit."""


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="fnmedia-pan")
    return _EXECUTOR


def _bump(key: str, amount: float = 1) -> None:
    with _LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + amount


def _snapshot() -> dict:
    with _LOCK:
        return dict(_METRICS)


def _worker(fn, args, kwargs):
    with _LOCK:
        _METRICS["queued"] -= 1
        _METRICS["running"] += 1
    try:
        result = fn(*args, **kwargs)
    except BaseException as exc:
        _bump("failed")
        _log.warning("cloud worker failed (%s)", type(exc).__name__)
        raise
    else:
        _bump("completed")
        return result
    finally:
        with _LOCK:
            _METRICS["running"] -= 1
            _METRICS["pending"] -= 1
        _CAPACITY.release()


async def run(fn, *args, timeout: float | None = None, **kwargs):
    """Run a blocking cloud-drive call on the bounded dedicated pool.

    ``timeout`` only bounds the caller wait. A thread already submitted keeps
    running, remains in ``pending``/``running`` and releases capacity only when
    its blocking function really returns.
    """
    started = time.monotonic()
    if not _CAPACITY.acquire(blocking=False):
        _bump("rejected")
        raise PoolBusyError("cloud worker pool is full")

    with _LOCK:
        _METRICS["submitted"] += 1
        _METRICS["pending"] += 1
        _METRICS["queued"] += 1
        _METRICS["queued_peak"] = max(
            _METRICS["queued_peak"], _METRICS["queued"])

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor(), _worker, fn, args, kwargs)
    try:
        # shield keeps a timeout from cancelling a queued concurrent future;
        # the worker must always release the capacity token in its finally.
        result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout) \
            if timeout else await future
        return result
    except asyncio.TimeoutError:
        _bump("timeouts")
        raise
    finally:
        _bump("wait_ms_total", (time.monotonic() - started) * 1000)


def stats() -> dict:
    data = _snapshot()
    done = max(1, data["completed"] + data["failed"])
    return {
        "max_workers": MAX_WORKERS,
        "max_queue": MAX_QUEUE,
        "capacity": MAX_WORKERS + MAX_QUEUE,
        "submitted": data["submitted"],
        "completed": data["completed"],
        "failed": data["failed"],
        "timeouts": data["timeouts"],
        "rejected": data["rejected"],
        "pending": data["pending"],
        "running": data["running"],
        "queued": data["queued"],
        "queued_peak": data["queued_peak"],
        "avg_wait_ms": round(data["wait_ms_total"] / done, 1),
    }


def shutdown() -> None:
    global _EXECUTOR
    if _EXECUTOR is not None:
        _EXECUTOR.shutdown(wait=False)
        _EXECUTOR = None
