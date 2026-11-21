"""Dedicated bounded thread pool for synchronous cloud-drive work.

`pan.py` / `panuc.py` / `panali.py` are synchronous (httpx.Client) and retry with
`time.sleep()` backoffs, so a single call can hold a thread for seconds. Running
them on the default executor lets a few slow transfers starve unrelated
`asyncio.to_thread()` users (config writes, disk snapshots).

This module keeps that work on its own executor with a hard worker cap and an
optional wait budget, so drive calls stay isolated and bounded.
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
import logging

_log = logging.getLogger("fnmedia.panpool")

MAX_WORKERS = 4
_EXECUTOR: ThreadPoolExecutor | None = None
_METRICS = {"submitted": 0, "completed": 0, "failed": 0, "timeouts": 0,
            "wait_ms_total": 0.0, "queued_peak": 0}
_PENDING = 0


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                       thread_name_prefix="fnmedia-pan")
    return _EXECUTOR


def _bump(key: str, amount: float = 1) -> None:
    _METRICS[key] = _METRICS.get(key, 0) + amount


async def run(fn, *args, timeout: float | None = None, **kwargs):
    """Run blocking cloud-drive call on the dedicated pool.

    `timeout` bounds how long the caller waits. The worker keeps running to
    completion in the background (cancelling a thread blocked in `time.sleep`
    is not possible), and the result is still discarded.
    """
    global _PENDING
    loop = asyncio.get_running_loop()
    _bump("submitted")
    _PENDING += 1
    if _PENDING > _METRICS["queued_peak"]:
        _METRICS["queued_peak"] = _PENDING
    started = time.monotonic()
    future = loop.run_in_executor(_executor(), lambda: fn(*args, **kwargs))
    try:
        if timeout:
            result = await asyncio.wait_for(future, timeout=timeout)
        else:
            result = await future
        _bump("completed")
        return result
    except asyncio.TimeoutError:
        _bump("timeouts")
        raise
    except Exception as exc:
        _bump("failed")
        _log.warning("cloud worker failed (%s)", type(exc).__name__)
        raise
    finally:
        _PENDING -= 1
        _bump("wait_ms_total", (time.monotonic() - started) * 1000)


def stats() -> dict:
    done = max(1, _METRICS["completed"] + _METRICS["failed"] + _METRICS["timeouts"])
    return {"max_workers": MAX_WORKERS,
            "submitted": _METRICS["submitted"],
            "completed": _METRICS["completed"],
            "failed": _METRICS["failed"],
            "timeouts": _METRICS["timeouts"],
            "pending": _PENDING,
            "queued_peak": _METRICS["queued_peak"],
            "avg_wait_ms": round(_METRICS["wait_ms_total"] / done, 1)}


def shutdown() -> None:
    global _EXECUTOR
    if _EXECUTOR is not None:
        _EXECUTOR.shutdown(wait=False)
        _EXECUTOR = None
