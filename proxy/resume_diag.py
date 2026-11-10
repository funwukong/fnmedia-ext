#!/usr/bin/env python3
"""续播诊断：内存环形缓冲（最近 20 条续播事件），供只读端点用。

只记内存，不写盘；不记任何 token / 凭证原文，只记 guid 与分区键。
分区键来自 history.current_user()：中间件只在识别到已知飞牛账号时才
设置上下文，未识别时读到 ""（全局分区）——这正是要观察的：
record 与 play/info 的分区不一致，就是续播丢位置的首要嫌疑。
"""
from __future__ import annotations

import threading
import time
from collections import deque

import history as histmod

_MAX_EVENTS = 20
_events: "deque[dict]" = deque(maxlen=_MAX_EVENTS)
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def note_record(raw_guid, normalized_guid, body, outcome, parsed):
    """play/record 到达时记一条：原始/归一化 guid、解析结果、分区、ts/duration、去向。

    outcome 为 "history"（进度记入本地 history）或 "upstream"（未识别为
    在线条目，透传官方后端）；parsed 为 "title" / "episode" / "unresolved"。
    """
    try:
        event = {
            "kind": "record",
            "at": _now(),
            "raw_guid": str(raw_guid or ""),
            "guid": str(normalized_guid or ""),
            "parsed": str(parsed or "unresolved"),
            "partition": histmod.current_user(),
            "ts": _to_int((body or {}).get("ts")),
            "duration": _to_int((body or {}).get("duration")),
            "outcome": str(outcome or ""),
        }
    except Exception:
        return
    with _lock:
        _events.append(event)


def note_info(request_guid, info):
    """play/info 返回时记一条：请求 guid、实际返回的 guid、返回的 ts、分区。

    只记最终返回的 ts；info 里没有 ts 键时记 None（与 ts=0 区分）。
    """
    try:
        info = info or {}
        event = {
            "kind": "info",
            "at": _now(),
            "guid": str(request_guid or ""),
            "resolved_guid": str(info.get("guid") or ""),
            "ts": _to_int(info["ts"]) if "ts" in info else None,
            "partition": histmod.current_user(),
        }
    except Exception:
        return
    with _lock:
        _events.append(event)


def events() -> list[dict]:
    """最近事件的快照（最旧在前），返回副本，调用方改不动缓冲。"""
    with _lock:
        return [dict(e) for e in _events]


def lookup(hist, guids):
    """按 guid 查 history 各分区里的当前记录。

    只读：直接读 history._users，不触发继承/落盘；只返回分区键与
    ts / duration / updated_at，不含任何 token 或凭证原文。
    """
    out: dict[str, dict] = {}
    try:
        users = getattr(hist, "_users", None) or {}
        want = [str(g) for g in (guids or []) if g]
        for partition, bucket in users.items():
            if not isinstance(bucket, dict):
                continue
            for g in want:
                rec = bucket.get(g)
                if isinstance(rec, dict):
                    out.setdefault(str(partition), {})[g] = {
                        "ts": _to_int(rec.get("ts")),
                        "duration": _to_int(rec.get("duration")),
                        "updated_at": rec.get("updated_at"),
                    }
    except Exception:
        return {}
    return out


def clear():
    """清空缓冲（测试隔离用）。"""
    with _lock:
        _events.clear()
