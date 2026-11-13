#!/usr/bin/env python3
"""在线条目的本地收藏 / 已看标记。state/marks.json。

官方后端不认识虚拟在线 guid：PUT /item/favorite 返回 -6、POST /item/watched
返回 -1。我们在本地接管这两类标记（卸载即清、零数据库污染），并在
item/list、item/{guid}、favorite/list 中回填，使收藏夹和「已看」筛选可用。

多用户（P0-3 fail-closed）：收藏 / 已看按飞牛账号分区，沿用 history.py
的模式——当前账号由 ``set_current_user``（每个请求由中间件按 token 解析
后设置）经 contextvar 传递；未识别身份落到全局分区 ""，行为与旧版一致；
升级时旧的全局记录只由管理员在其首次访问时「继承」一次（复制，不清空
全局分区；管理员是原来的主要使用者），子账号从空白开始、不再互相串收藏。
继承标记持久化在文件顶层 adopted 列表里，避免每次重启重复继承。

分区说明：
- 存储格式 {"users": {guid: {"favorites": {...}, "watched": {...}}},
  "adopted": [...]}；旧版扁平 {"favorites": ..., "watched": ...} 读到时
  整体归入全局分区 ""。
- 每个分区的收藏 / 已看各有 _MAX 条上限（与旧版全局上限同值）。
- ``_fav`` / ``_watched`` 保留为属性：读写当前账号分区的两个映射，
  兼容旧测试直接读写它们的做法（无请求上下文时即全局分区）。
- 收藏 / 已看记录可带 "libs"：收藏时由调用方算好的归属库键列表
  （见 app._mark_extra），供收藏夹在标题 brief 已被 store 淘汰后仍能
  按库过滤；有 live brief 时以 live 判定为准，"libs" 只作兜底。
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "marks.json"
_LOCK = threading.RLock()
_MAX = 5000

# 每个请求设置的当前飞牛账号 guid；"" = 未识别，使用全局分区
_current_user: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "fnmedia_marks_user", default="")
_current_admin: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "fnmedia_marks_admin", default=False)


def set_current_user(guid: str | None, is_admin: bool = False):
    """设置当前请求的账号分区，返回 (user_token, admin_token) 以便 reset。"""
    return (_current_user.set(str(guid or "")),
            _current_admin.set(bool(is_admin)))


def reset_current_user(tokens) -> None:
    try:
        _current_admin.reset(tokens[1])
        _current_user.reset(tokens[0])
    except (ValueError, LookupError, IndexError, TypeError):
        pass


def current_user() -> str:
    return _current_user.get() or ""


_META_KEYS = ("title", "type", "poster", "year", "remarks",
              "ancestor_guid", "ancestor_name")


class Marks:
    def __init__(self):
        # {user_guid: {"favorites": {...}, "watched": {...}}}；
        # "" 是全局分区（未识别身份 / 旧数据）
        self._users: dict[str, dict[str, dict]] = {}
        # 已从旧全局分区继承过收藏的账号（持久化，避免每次重启重复继承）
        self._adopted: set[str] = set()
        self._load()

    # ---- 兼容：_fav / _watched 是当前账号分区的两个映射 ----
    @property
    def _fav(self) -> dict:
        return self._bucket()["favorites"]

    @_fav.setter
    def _fav(self, value) -> None:
        self._bucket()["favorites"] = dict(value or {})

    @property
    def _watched(self) -> dict:
        return self._bucket()["watched"]

    @_watched.setter
    def _watched(self, value) -> None:
        self._bucket()["watched"] = dict(value or {})

    def _load(self):
        try:
            j = json.loads(_STATE.read_text())
        except Exception:
            j = {}
        if "users" in j and isinstance(j.get("users"), dict):
            users = {}
            for u, v in j["users"].items():
                if not isinstance(v, dict):
                    continue
                users[str(u)] = {
                    "favorites": dict(v.get("favorites") or {}),
                    "watched": dict(v.get("watched") or {}),
                }
            self._users = users
            self._adopted = set(j.get("adopted", []) or [])
        else:
            # 旧版扁平结构 {"favorites": ..., "watched": ...} → 全局分区
            self._users = {"": {
                "favorites": dict(j.get("favorites") or {}),
                "watched": dict(j.get("watched") or {}),
            }}
            self._adopted = set()

    def _bucket(self, user: str | None = None) -> dict[str, dict]:
        u = current_user() if user is None else str(user or "")
        bucket = self._users.setdefault(
            u, {"favorites": {}, "watched": {}})
        self._adopt_global_if_needed(u, bucket)
        return bucket

    def _adopt_global_if_needed(self, u: str, bucket: dict):
        """升级迁移：旧版收藏是全家共享的全局记录。按账号隔离后，只让
        管理员在其首次访问时把旧全局记录继承一次（复制，不清空全局分区；
        管理员是原来的主要使用者），子账号从空白开始、不再互相串收藏。
        与 history.py 的 _adopt_global_if_needed 同规则。"""
        if u and u not in self._adopted \
                and _current_admin.get() \
                and not bucket["favorites"] and not bucket["watched"]:
            g = self._users.get("", {})
            if g.get("favorites") or g.get("watched"):
                bucket["favorites"].update(
                    json.loads(json.dumps(g.get("favorites") or {})))
                bucket["watched"].update(
                    json.loads(json.dumps(g.get("watched") or {})))
                self._adopted.add(u)
                self._persist_locked()

    def _persist_locked(self):
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            users: dict[str, dict] = {}
            for u, bucket in self._users.items():
                entry: dict[str, dict] = {}
                for kind in ("favorites", "watched"):
                    mapping = bucket.get(kind) or {}
                    if len(mapping) > _MAX:
                        keep = sorted(mapping.values(),
                                      key=lambda r: r.get("ts", 0),
                                      reverse=True)[:_MAX]
                        mapping = {r["guid"]: r for r in keep}
                        bucket[kind] = mapping
                    if mapping:
                        entry[kind] = mapping
                if entry:
                    users[u] = entry
            tmp = _STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"users": users, "adopted": sorted(self._adopted)},
                ensure_ascii=False))
            tmp.replace(_STATE)
        except Exception:
            pass

    # ---- 收藏 ----
    def add_favorite(self, guid: str, extra: dict | None = None,
                     user: str | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            bucket = self._bucket(user)
            fav = bucket["favorites"]
            if guid in fav:
                return True
            rec = {"guid": guid, "ts": time.time()}
            if extra:
                for k in _META_KEYS:
                    if extra.get(k) not in (None, ""):
                        rec[k] = extra[k]
                libs = extra.get("libs")
                if isinstance(libs, (list, tuple)) and libs:
                    rec["libs"] = [str(k) for k in libs if k]
            fav[guid] = rec
            self._persist_locked()
            return True

    def remove_favorite(self, guid: str, user: str | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            fav = self._bucket(user)["favorites"]
            existed = fav.pop(guid, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def is_favorite(self, guid: str, user: str | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            return guid in self._bucket(user)["favorites"]

    def favorite_guids(self, user: str | None = None) -> set[str]:
        with _LOCK:
            return set(self._bucket(user)["favorites"].keys())

    def favorite_records(self, user: str | None = None) -> list[dict]:
        with _LOCK:
            return [dict(r) for r in sorted(
                self._bucket(user)["favorites"].values(),
                key=lambda r: r.get("ts", 0), reverse=True)]

    # ---- 已看 ----
    def set_watched(self, guid: str, extra: dict | None = None,
                    user: str | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            wat = self._bucket(user)["watched"]
            rec = wat.get(guid, {"guid": guid})
            rec["ts"] = time.time()
            if extra:
                for k in _META_KEYS:
                    if extra.get(k) not in (None, ""):
                        rec[k] = extra[k]
                libs = extra.get("libs")
                if isinstance(libs, (list, tuple)) and libs:
                    rec["libs"] = [str(k) for k in libs if k]
            wat[guid] = rec
            self._persist_locked()
            return True

    def remove_watched(self, guid: str, user: str | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            wat = self._bucket(user)["watched"]
            existed = wat.pop(guid, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def is_watched(self, guid: str, user: str | None = None) -> bool:
        if not guid:
            return False
        with _LOCK:
            return guid in self._bucket(user)["watched"]

    def watched_ts(self, guid: str, user: str | None = None) -> int:
        with _LOCK:
            r = self._bucket(user)["watched"].get(guid)
            return int(r.get("ts", 0)) if r else 0

    def watched_guids(self, user: str | None = None) -> set[str]:
        with _LOCK:
            return set(self._bucket(user)["watched"].keys())

    def remember_meta(self, guid: str, meta: dict, user: str | None = None):
        """列表渲染时顺手补充元信息，供收藏夹在条目未被 store 记住时兜底。"""
        if not guid or not meta:
            return
        with _LOCK:
            bucket = self._bucket(user)
            changed = False
            for store_map in (bucket["favorites"], bucket["watched"]):
                rec = store_map.get(guid)
                if not rec:
                    continue
                for k in _META_KEYS:
                    v = meta.get(k)
                    if v not in (None, "") and not rec.get(k):
                        rec[k] = v
                        changed = True
            if changed:
                self._persist_locked()


marks = Marks()
