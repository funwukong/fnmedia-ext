#!/usr/bin/env python3
"""在线条目的本地收藏 / 已看标记。state/marks.json。

官方后端不认识虚拟在线 guid：PUT /item/favorite 返回 -6、POST /item/watched
返回 -1。我们在本地接管这两类标记（卸载即清、零数据库污染），并在
item/list、item/{guid}、favorite/list 中回填，使收藏夹和「已看」筛选可用。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "marks.json"
_LOCK = threading.RLock()
_MAX = 5000


class Marks:
    def __init__(self):
        self._fav: dict[str, dict] = {}
        self._watched: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            j = json.loads(_STATE.read_text())
            self._fav = j.get("favorites", {}) or {}
            self._watched = j.get("watched", {}) or {}
        except Exception:
            self._fav, self._watched = {}, {}

    def _persist_locked(self):
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            fav = self._fav
            wat = self._watched
            if len(fav) > _MAX:
                keep = sorted(fav.values(), key=lambda r: r.get("ts", 0),
                              reverse=True)[:_MAX]
                fav = {r["guid"]: r for r in keep}
                self._fav = fav
            if len(wat) > _MAX:
                keep = sorted(wat.values(), key=lambda r: r.get("ts", 0),
                              reverse=True)[:_MAX]
                wat = {r["guid"]: r for r in keep}
                self._watched = wat
            tmp = _STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"favorites": fav, "watched": wat}, ensure_ascii=False))
            tmp.replace(_STATE)
        except Exception:
            pass

    # ---- 收藏 ----
    def add_favorite(self, guid: str, extra: dict | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            if guid in self._fav:
                return True
            rec = {"guid": guid, "ts": time.time()}
            if extra:
                for k in ("title", "type", "poster", "year", "remarks",
                          "ancestor_guid", "ancestor_name"):
                    if extra.get(k) not in (None, ""):
                        rec[k] = extra[k]
            self._fav[guid] = rec
            self._persist_locked()
            return True

    def remove_favorite(self, guid: str) -> bool:
        if not guid:
            return False
        with _LOCK:
            existed = self._fav.pop(guid, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def is_favorite(self, guid: str) -> bool:
        return bool(guid) and guid in self._fav

    def favorite_guids(self) -> set[str]:
        with _LOCK:
            return set(self._fav.keys())

    def favorite_records(self) -> list[dict]:
        with _LOCK:
            return [dict(r) for r in sorted(
                self._fav.values(), key=lambda r: r.get("ts", 0), reverse=True)]

    # ---- 已看 ----
    def set_watched(self, guid: str, extra: dict | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            rec = self._watched.get(guid, {"guid": guid})
            rec["ts"] = time.time()
            if extra:
                for k in ("title", "type", "poster", "year", "remarks",
                          "ancestor_guid", "ancestor_name"):
                    if extra.get(k) not in (None, ""):
                        rec[k] = extra[k]
            self._watched[guid] = rec
            self._persist_locked()
            return True

    def remove_watched(self, guid: str) -> bool:
        if not guid:
            return False
        with _LOCK:
            existed = self._watched.pop(guid, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def is_watched(self, guid: str) -> bool:
        return bool(guid) and guid in self._watched

    def watched_ts(self, guid: str) -> int:
        r = self._watched.get(guid)
        return int(r.get("ts", 0)) if r else 0

    def watched_guids(self) -> set[str]:
        with _LOCK:
            return set(self._watched.keys())

    def remember_meta(self, guid: str, meta: dict):
        """列表渲染时顺手补充元信息，供收藏夹在条目未被 store 记住时兜底。"""
        if not guid or not meta:
            return
        with _LOCK:
            changed = False
            for store_map in (self._fav, self._watched):
                rec = store_map.get(guid)
                if not rec:
                    continue
                for k in ("title", "type", "poster", "year", "remarks",
                          "ancestor_guid", "ancestor_name"):
                    v = meta.get(k)
                    if v not in (None, "") and not rec.get(k):
                        rec[k] = v
                        changed = True
            if changed:
                self._persist_locked()


marks = Marks()
