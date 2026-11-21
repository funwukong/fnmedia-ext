"""Lightweight runtime trend sampling (RSS / active tasks / cache bytes).

Ring buffer only: nothing is persisted, nothing is exposed beyond aggregate
numbers on the admin status endpoint.
"""
from __future__ import annotations

import threading
import time
from collections import deque

_SAMPLE_LIMIT = 120
_SAMPLE_INTERVAL = 60.0
_LOCK = threading.Lock()
_SAMPLES: deque = deque(maxlen=_SAMPLE_LIMIT)
_LAST_SAMPLE = 0.0


def _rss_bytes() -> int:
    """Resident set size from /proc; 0 when unavailable (non-Linux/denied)."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            parts = fh.read().split()
        if len(parts) < 2:
            return 0
        pages = int(parts[1])
        return pages * 4096
    except (OSError, ValueError):
        return 0


def sample(*, cache_bytes: int = 0, active_tasks: int = 0,
           force: bool = False) -> dict | None:
    """Record one sample at most every _SAMPLE_INTERVAL unless forced."""
    global _LAST_SAMPLE
    now = time.time()
    with _LOCK:
        if not force and _SAMPLES and now - _LAST_SAMPLE < _SAMPLE_INTERVAL:
            return None
        _LAST_SAMPLE = now
        row = {"at": int(now), "rss_mb": round(_rss_bytes() / 1048576, 1),
               "cache_mb": round(cache_bytes / 1048576, 2),
               "tasks": int(active_tasks)}
        _SAMPLES.append(row)
        return dict(row)


def snapshot() -> dict:
    with _LOCK:
        rows = [dict(r) for r in _SAMPLES]
    if not rows:
        return {"samples": 0, "rss_mb": 0.0, "rss_peak_mb": 0.0,
                "cache_mb": 0.0, "tasks": 0, "recent": []}
    rss = [r["rss_mb"] for r in rows]
    return {"samples": len(rows),
            "rss_mb": rss[-1],
            "rss_peak_mb": max(rss),
            "cache_mb": rows[-1]["cache_mb"],
            "tasks": rows[-1]["tasks"],
            "recent": rows[-20:]}


def reset() -> None:
    global _LAST_SAMPLE
    with _LOCK:
        _SAMPLES.clear()
        _LAST_SAMPLE = 0.0
