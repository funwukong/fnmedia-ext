#!/usr/bin/env python3
"""播放链路性能指标：环形窗口 + 管理页快照（纯内存，重启清空）。

与 runtime_metrics（RSS/缓存字节/后台任务数）互补：本模块只记录
"用户感知得到"的起播指标——竞速胜出延迟、清单 TTFB、分片首字节、
吞吐、缓存命中率、零竞速直出率。所有 record() 均为 O(1) 且有界，
热路径零 IO、零网络。
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque

_WINDOW = 500
_LOCK = threading.Lock()
_METRICS: dict[str, deque] = {
    "race_latency": deque(maxlen=_WINDOW),      # 竞速胜出延迟（秒）
    "race_candidates": deque(maxlen=_WINDOW),   # 参与竞速线路数
    "playlist_ttfb": deque(maxlen=_WINDOW),     # main.m3u8 请求到响应首字节（秒）
    "seg_ttfb": deque(maxlen=_WINDOW),          # 分片首字节（秒）
    "seg_throughput_kbps": deque(maxlen=_WINDOW),
    "cache_hit": deque(maxlen=_WINDOW),         # 1=命中 0=miss
    "predict_direct": deque(maxlen=_WINDOW),    # 1=零竞速直出
}

_STARTED = time.time()


def record(metric: str, value: float) -> None:
    """记录一个采样值。未知 metric 静默忽略（热路径零异常）。"""
    q = _METRICS.get(metric)
    if q is None:
        return
    with _LOCK:
        q.append(float(value))


def snapshot() -> dict:
    """分位数统计：count / p50 / p95 / mean + 命中率。"""
    out: dict = {"uptime_seconds": round(time.time() - _STARTED, 1)}
    with _LOCK:
        for name, q in _METRICS.items():
            if not q:
                continue
            vals = sorted(q)
            n = len(vals)
            out[name] = {
                "count": n,
                "p50": round(vals[n // 2], 3),
                "p95": round(vals[int(n * 0.95)] if n >= 20 else vals[-1], 3),
                "mean": round(statistics.fmean(vals), 3),
                "max": round(vals[-1], 3),
            }
        hits = _METRICS["cache_hit"]
        if hits:
            out["cache_hit_rate"] = round(sum(hits) / len(hits), 3)
        direct = _METRICS["predict_direct"]
        if direct:
            out["predict_direct_rate"] = round(sum(direct) / len(direct), 3)
    return out


def reset() -> None:
    """管理页重置按钮用。"""
    with _LOCK:
        for q in _METRICS.values():
            q.clear()
