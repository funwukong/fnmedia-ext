#!/usr/bin/env python3
"""在线播放进度（继续观看）。state/history.json。

记录由前端高频 POST /play/record 驱动，内存更新 + 限频落盘；
GET /play/list 时与官方本地历史合并，让在线条目出现在「继续观看」。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "history.json"
_LOCK = threading.RLock()
_FLUSH_INTERVAL = 3.0
_MAX = 500


class History:
    def __init__(self):
        self._rec: dict[str, dict] = {}
        self._last_flush = 0.0
        self._dirty = False
        self._load()

    def _load(self):
        try:
            j = json.loads(_STATE.read_text())
            self._rec = j.get("records", {})
        except Exception:
            self._rec = {}

    def _flush_locked(self, force: bool = False):
        now = time.time()
        if not self._dirty:
            return
        if not force and now - self._last_flush < _FLUSH_INTERVAL:
            return
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            recs = self._rec
            if len(recs) > _MAX:
                keep = sorted(recs.values(),
                              key=lambda r: r.get("updated_at", 0),
                              reverse=True)[:_MAX]
                recs = {r["item_guid"]: r for r in keep}
                self._rec = recs
            tmp = _STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"records": recs}, ensure_ascii=False))
            tmp.replace(_STATE)
            self._dirty = False
            self._last_flush = now
        except Exception:
            pass

    def flush(self):
        with _LOCK:
            self._flush_locked(force=True)

    def record(self, body: dict) -> bool:
        """处理一次 play/record 上报，返回是否为我们的在线条目。"""
        ig = body.get("item_guid", "")
        if not ig:
            return False
        with _LOCK:
            prev = self._rec.get(ig, {})
            ts = int(float(body.get("ts", 0) or 0))
            duration = int(float(body.get("duration", 0) or 0))
            rec = {
                "item_guid": ig,
                "media_guid": body.get("media_guid", prev.get("media_guid", "")),
                "video_guid": body.get("video_guid", prev.get("video_guid", "")),
                "audio_guid": body.get("audio_guid", prev.get("audio_guid", "")),
                "subtitle_guid": body.get("subtitle_guid",
                                          prev.get("subtitle_guid", "")),
                "resolution": body.get("resolution", prev.get("resolution", "")),
                "ts": max(ts, int(prev.get("ts", 0))),
                "duration": duration or int(prev.get("duration", 0)),
                "updated_at": time.time(),
            }
            # 只有上报进度回退（重新播放）才允许变小：以更大 ts 为准，除非显式重置
            if ts < int(prev.get("ts", 0)) and body.get("reset"):
                rec["ts"] = ts
            self._rec[ig] = rec
            self._dirty = True
            self._flush_locked()
        return True

    def set_position(self, item_guid: str, ts: int, duration: int = 0):
        with _LOCK:
            prev = self._rec.get(item_guid, {})
            prev.update({"item_guid": item_guid, "ts": int(ts),
                         "updated_at": time.time()})
            if duration:
                prev["duration"] = int(duration)
            self._rec[item_guid] = prev
            self._dirty = True
            self._flush_locked(force=True)

    def get(self, item_guid: str) -> dict | None:
        with _LOCK:
            return dict(self._rec.get(item_guid) or {}) or None

    def all(self) -> list[dict]:
        with _LOCK:
            self._flush_locked(force=True)
            return [dict(r) for r in self._rec.values()]

    def resume_episode(self, ep_guids: list[str]) -> tuple[str | None, int]:
        """给定一部剧按顺序的全部集 guid，决定"该播哪一集"。

        - 从没看过 → 第一集（ts=0）
        - 有未看完的集（进度<95%）→ 最近在看的那一集，带回 ts 续播
        - 看过的都看完了 → 下一集（ts=0）；全看完 → 最后一集
        返回 (ep_guid 或 None 表示无记录, ts)。
        """
        with _LOCK:
            # 有序地只保留有记录的集
            watched = [(i, self._rec[g]) for i, g in enumerate(ep_guids)
                       if g in self._rec]
            if not watched:
                return (ep_guids[0] if ep_guids else None), 0
            # 找未看完的，取集序最大（最近在追）的一集
            unfinished = [(i, r) for i, r in watched
                          if not self._finished(r)]
            if unfinished:
                i, r = max(unfinished, key=lambda x: x[0])
                return ep_guids[i], int(r.get("ts", 0) or 0)
            # 都看完了 → 下一集
            last_i = max(i for i, _ in watched)
            nxt = min(last_i + 1, len(ep_guids) - 1)
            return ep_guids[nxt], 0

    @staticmethod
    def _finished(r: dict) -> bool:
        ts = int(r.get("ts", 0) or 0)
        dur = int(r.get("duration", 0) or 0)
        return bool(dur) and ts >= dur * 0.95

    def delete(self, item_guids) -> int:
        ids = item_guids if isinstance(item_guids, (list, tuple)) else [item_guids]
        n = 0
        with _LOCK:
            for g in ids:
                if g in self._rec:
                    del self._rec[g]
                    n += 1
            if n:
                self._dirty = True
                self._flush_locked(force=True)
        return n


history = History()
