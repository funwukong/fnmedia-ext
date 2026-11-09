#!/usr/bin/env python3
"""追剧提醒（P2）。

与已有的 History（继续观看进度）与 Marks（收藏）互补，不重复造轮子：
- History 管“看到哪一集、断点续播”（resume_episode 已有）；
- Marks 管“收藏了没有”；
- 本模块管“追的剧有没有更新”：记下收藏时看到的集数，
  下次抓到更多集时给出提醒。

数据：state/follow.json，{guid: {title, last_seen_episodes, updated_at}}。
损坏 / 不存在时退回空，不抛错。调用方（app.py 的管理接口 / 后台刷新）
只需传“当前集数”，本模块算差值。

提醒规则：
- current > last_seen → 有更新，new_episodes = current - last_seen；
- current <= last_seen → 无更新；
- mark_seen() 后把 last_seen 更新为当前集数，提醒清零。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "follow.json"
_LOCK = threading.RLock()
_MAX = 500


class FollowList:
    def __init__(self, state_path: Path | None = None):
        self._path = state_path or _STATE
        self._items: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            j = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = j.get("items", {}) if isinstance(j, dict) else {}
        except Exception:
            self._items = {}

    def _persist_locked(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            items = self._items
            if len(items) > _MAX:
                keep = sorted(items.values(), key=lambda r: r.get("updated_at", 0),
                              reverse=True)[:_MAX]
                items = {r["guid"]: r for r in keep}
                self._items = items
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": items}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def follow(self, guid: str, title: str = "", episodes: int = 0) -> bool:
        if not guid:
            return False
        with _LOCK:
            prev = self._items.get(guid, {})
            self._items[guid] = {
                "guid": guid,
                "title": title or prev.get("title", ""),
                "last_seen_episodes": int(episodes or prev.get("last_seen_episodes", 0)),
                "updated_at": time.time(),
            }
            self._persist_locked()
            return True

    def unfollow(self, guid: str) -> bool:
        with _LOCK:
            existed = self._items.pop(guid, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def is_following(self, guid: str) -> bool:
        return bool(guid) and guid in self._items

    def list(self) -> list[dict]:
        with _LOCK:
            return [dict(r) for r in sorted(
                self._items.values(), key=lambda r: r.get("updated_at", 0),
                reverse=True)]

    def mark_seen(self, guid: str, episodes: int) -> bool:
        """用户已看到 current 集数，提醒清零。"""
        with _LOCK:
            if guid not in self._items:
                return False
            self._items[guid]["last_seen_episodes"] = int(episodes)
            self._items[guid]["updated_at"] = time.time()
            self._persist_locked()
            return True

    def check_updates(self, current_counts: dict[str, int]) -> list[dict]:
        """current_counts: guid -> 当前总集数。返回有更新的条目。"""
        out = []
        with _LOCK:
            for guid, rec in self._items.items():
                cur = int(current_counts.get(guid, 0) or 0)
                last = int(rec.get("last_seen_episodes", 0) or 0)
                if cur > last:
                    out.append({
                        "guid": guid,
                        "title": rec.get("title", ""),
                        "last_seen_episodes": last,
                        "current_episodes": cur,
                        "new_episodes": cur - last,
                    })
        return sorted(out, key=lambda r: r["new_episodes"], reverse=True)


follow_list = FollowList()
