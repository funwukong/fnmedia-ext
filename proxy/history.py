#!/usr/bin/env python3
"""在线播放进度（继续观看）。state/history.json。

记录由前端高频 POST /play/record 驱动，内存更新 + 限频落盘；
GET /play/list 时与官方本地历史合并，让在线条目出现在「继续观看」。

多用户（issue #5）：在线历史按飞牛账号分区，避免一人看片全家共享进度。
当前账号通过 ``set_current_user``（每个请求由中间件按 token 解析后设置）传递；
未识别身份（本地/无 token/解析失败）落到全局分区 ""，行为与旧版一致。
升级时旧的全局记录会在某个已识别账号首次访问时被其「继承」一次，之后各账号独立。

跨线身份（resume3）：标题 guid（og）= md5("on_<provider>_<vod_id>")，
分源不同 og 不同，集 guid = md5("ep:<og>:<index>") 也不同——同一部剧
同一集在 ffzy / dytt 两条线下是两个身份，进度互不相通。本模块改用
规范键做历史/续播身份：

- 剧集：``canon:tv:<归一化标题>:s<季号>:<集身份>``，集身份优先用 T4 的
  episode_key（已跨线路归一：episode:N / season:S:episode:N / label:…；
  variant 行去掉线路 hash 落到同一 label 键），否则用集号 ``e<N>``；
- 电影：``canon:movie:<归一化标题>``；标题级记录（文件夹/季卡上报的进度，
  只能回填第 1 集）：``canon:title:<归一化标题>``。
- 标题归一化复用 metadata.normalize_title（去括号备注与集数/画质后缀），
  再去空白/标点/大小写；不含年份——同名不同年翻拍会并到同一键下，
  是已知残留（见 canonical_* 的注释），换源不断播优先于翻拍隔离。

回退链（旧记录不丢、不破坏性重写 history.json）：
- 查：先旧键（分源 guid），再规范键；单条读入口（get/position/resume_*）
  先整桶扫一次，把可解析的旧键记录迁移到规范键下（两边都有时合并，
  ts 取大、updated_at 取大），再按旧键→规范键查；
- 写：新记录一律按规范键存，bucket 键 = 规范键；但记录里的
  ``item_guid`` 仍保留原始分源 guid——play/list 与 /v/api/ext/continue
  靠它回查 store（get_episode/get_title_by_guid），改成规范键会被它们
  当成“缓存已淘汰”直接丢弃；另附 ``canon_key`` 字段便于诊断。
- 读列表（all/continue_watching）时按有效键（规范键或由 item_guid
  现算的规范键）合并去重，过渡期的旧键记录不重复出现。
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from pathlib import Path

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("history")

_STATE = Path(__file__).resolve().parent.parent / "state" / "history.json"
_LOCK = threading.RLock()
_FLUSH_INTERVAL = 3.0
_MAX = 500

# 每个请求设置的当前飞牛账号 guid；"" = 未识别，使用全局分区
_current_user: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "fnmedia_history_user", default="")
_current_admin: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "fnmedia_history_admin", default=False)


# ---------------------------------------------------------------- 规范键
_CANON_PREFIX = "canon:"
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_T4_EP_NUM_RE = re.compile(r"^(?:season:\d+:)?episode:(\d+)$")
_T4_SEASON_RE = re.compile(r"^season:(\d+):")
_MOVIE_TYPE_WORDS = ("电影", "影片", "剧情片", "动作片", "喜剧片", "爱情片",
                     "科幻片", "恐怖片", "纪录片")


def key_normalize_title(title: str) -> str:
    """做键用的标题归一化：复用 metadata.normalize_title 去后缀/括号备注
    （“第X集 / 预告 / HD / 4K / （2024）”等），再去全部空白、标点与
    大小写差异。只去噪，不做语义改写；同名翻拍仍会撞键（见模块注释）。"""
    try:
        from metadata import normalize_title
        t = normalize_title(title)
    except Exception:
        _log.debug("key_normalize_title failed: title=%s", title, exc_info=True)
        t = (title or "").strip()
    return _NON_WORD_RE.sub("", (t or "").casefold())


def _canon_episode_part(episode_key: str | None,
                        episode_number: int) -> str | None:
    """T4 episode_key → 规范键里的集身份。

    episode_key 本身已跨线路归一（store._t4_rows 按 key 去重合并各线），
    优先用它：episode:N / season:S:episode:N → eN；label:… → label:…；
    variant 行（同一集两个编码，key 里带线路 hash）去掉 hash 落到同一
    label 键；related 行（相关视频，本就不是同一集）保留全 key，不跨源合。
    未知形态回退集号；集号也没有（label 行为 0）时返回 None。"""
    key = episode_key or ""
    if key:
        m = _T4_EP_NUM_RE.match(key)
        if m:
            return "e%s" % int(m.group(1))
        label = ""
        if ":variant:" in key:
            rest = key.split(":variant:", 1)[1]
            label = rest.split(":", 1)[1] if ":" in rest else ""
        elif ":label:" in key:
            label = key.split(":label:", 1)[1]
        elif key.startswith("label:"):
            label = key[len("label:"):]
        if label:
            return "label:%s" % label
        if "related:" in key:
            return key
    if episode_number:
        return "e%s" % int(episode_number)
    return None


def canonical_episode_key(title: str, season: int = 1,
                          episode_number: int = 0,
                          episode_key: str | None = None) -> str | None:
    """剧集规范键。季号优先以 episode_key 里的 season: 前缀为准
    （与 store._index_t4 的 season 来源一致）；标题空或集身份
    无法确定时返回 None（调用方只走旧键）。"""
    ntitle = key_normalize_title(title)
    if not ntitle:
        return None
    part = _canon_episode_part(episode_key, episode_number)
    if not part:
        return None
    season_n = 1
    try:
        season_n = int(season or 1)
    except (TypeError, ValueError):
        season_n = 1
    if episode_key:
        m = _T4_SEASON_RE.match(episode_key)
        if m:
            season_n = int(m.group(1))
    return "%stv:%s:s%d:%s" % (_CANON_PREFIX, ntitle, season_n, part)


def canonical_movie_key(title: str) -> str | None:
    ntitle = key_normalize_title(title)
    return "%smovie:%s" % (_CANON_PREFIX, ntitle) if ntitle else None


def canonical_title_key(title: str) -> str | None:
    ntitle = key_normalize_title(title)
    return "%stitle:%s" % (_CANON_PREFIX, ntitle) if ntitle else None


def _is_tv_brief(brief: dict | None) -> bool:
    """标题是不是剧集（决定标题级记录走 canon:title 还是 canon:movie）。
    优先用 inject.is_tv（与播放层同一口径）；引不到时用类型词 + 集数
    做等价判断。"""
    brief = brief or {}
    try:
        import inject as _inj
        return bool(_inj.is_tv(brief))
    except Exception:
        _log.debug("_is_tv_brief failed", exc_info=True)
        pass
    typ = (brief.get("type_name") or "").strip()
    if any(w in typ for w in _MOVIE_TYPE_WORDS):
        return False
    n = 0
    for eps in (brief.get("lines") or {}).values():
        n = max(n, len([e for e in eps
                        if e.get("direct") or e.get("resolve")
                        or e.get("t4")]))
    return n > 1


def canonical_key_for_guid(guid: str) -> str | None:
    """分源 guid → 规范键；解析不到（不在 store 里、标题空）返回 None。

    集 guid → 剧集规范键；标题 guid → 电影/标题规范键；resume2 的
    video/media guid 经 store 反查索引先落到集/标题身份再转规范键
    （电影的集级 video guid 按标题走，不落到集键，与
    _normalize_history_guid 的电影口径一致）。规范键本身原样返回。
    store 不可用时返回 None，只走旧键，不抛错。"""
    if not isinstance(guid, str) or not guid:
        return None
    if guid.startswith(_CANON_PREFIX):
        return guid
    try:
        import store as _store_mod
        st = getattr(_store_mod, "store", None)
        if st is None:
            return None
        ep = st.get_episode(guid)
        if ep:
            return canonical_episode_key(
                ep.get("title", ""), ep.get("season", 1),
                ep.get("episode_number", ep.get("index", 0) + 1),
                ep.get("episode_key"))
        brief = st.get_title_by_guid(guid)
        if brief:
            if _is_tv_brief(brief):
                return canonical_title_key(brief.get("title", ""))
            return canonical_movie_key(brief.get("title", ""))
        target = st.resolve_media_guid(guid)
        if target:
            og = target.get("online_guid") or ""
            tbrief = st.get_title_by_guid(og) if og else None
            if tbrief is not None and not _is_tv_brief(tbrief):
                return canonical_key_for_guid(og)
            eg = target.get("ep_guid") or og
            if eg and eg != guid:
                return canonical_key_for_guid(eg)
    except Exception:
        _log.debug("canonical_key_for_guid failed: guid=%s", guid, exc_info=True)
        return None
    return None


def _merge_records(a: dict, b: dict) -> dict:
    """两条同身份记录合并：ts 取大，updated_at 取大，duration 与
    media/video 等身份字段取较新一条的（空则取另一条的），
    item_guid 取较新一条的（最近一次上报的分源 guid）。"""
    if not a:
        return dict(b) if b else {}
    if not b:
        return dict(a)
    a_up = float(a.get("updated_at", 0) or 0)
    b_up = float(b.get("updated_at", 0) or 0)
    newer, older = (a, b) if a_up >= b_up else (b, a)
    out = dict(older)
    for k, v in newer.items():
        if v not in (None, "") or k not in older:
            out[k] = v
    out["ts"] = max(int(a.get("ts", 0) or 0), int(b.get("ts", 0) or 0))
    out["updated_at"] = max(a_up, b_up)
    out["duration"] = int(newer.get("duration", 0) or 0) or \
        int(older.get("duration", 0) or 0)
    out["item_guid"] = newer.get("item_guid") or older.get("item_guid")
    return out


class _Bucket(dict):
    """按账号分区的历史桶：键是规范键（新记录）或旧分源 guid（遗留记录）。

    只覆写 get：精确命中走原键；miss 时按规范键回退（旧分源 guid
    → 规范键）。直接读 _users 的调用方（resume_diag.lookup）不用改
    就能按旧 guid 查到规范键下的记录。__contains__ / __delitem__ /
    迭代保持字面语义，不覆写——删除与迁移仍按字面键操作，避免
    “查得到却删不掉”的歧义。"""

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            return dict.get(self, key, default)
        if isinstance(key, str) and key:
            ck = canonical_key_for_guid(key)
            if ck and dict.__contains__(self, ck):
                return dict.get(self, ck)
        return default


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


class History:
    def __init__(self):
        # {user_guid: {item_guid: rec}}；"" 是全局分区（未识别身份 / 旧数据）
        self._users: dict[str, dict[str, dict]] = {}
        # 已从旧全局分区继承过历史的账号（持久化，避免每次重启重复继承）
        self._adopted: set[str] = set()
        self._last_flush = 0.0
        self._dirty = False
        self._load()

    def _load(self):
        try:
            j = json.loads(_STATE.read_text())
        except (OSError, ValueError):
            _log.debug("_load failed", exc_info=True)
            j = {}
        if "users" in j and isinstance(j.get("users"), dict):
            self._users = {u: _Bucket(v) for u, v in j["users"].items()
                           if isinstance(v, dict) and not u.startswith("__adopted_")}
            # 已继承标记单独恢复（持久化在顶层 adopted 列表）
            self._adopted = set(j.get("adopted", []) or [])
        else:
            # 旧版扁平结构 {"records": {...}} → 归入全局分区
            recs = j.get("records", {})
            self._users = {"": _Bucket(recs) if isinstance(recs, dict) else _Bucket()}
            self._adopted = set()

    def _bucket(self, user: str | None = None) -> dict[str, dict]:
        u = current_user() if user is None else str(user or "")
        return self._users.setdefault(u, _Bucket())

    def _flush_locked(self, force: bool = False):
        now = time.time()
        if not self._dirty:
            return
        if not force and now - self._last_flush < _FLUSH_INTERVAL:
            return
        try:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            users: dict[str, dict] = {}
            for u, recs in self._users.items():
                if not recs:
                    continue
                if len(recs) > _MAX:
                    # 按 bucket 键裁剪（键可能是规范键，不再从 item_guid
                    # 反推键——同一 item_guid 可能对应多个规范键）。
                    keep = sorted(recs.items(),
                                  key=lambda kv: kv[1].get("updated_at", 0),
                                  reverse=True)[:_MAX]
                    recs = _Bucket(keep)
                    self._users[u] = recs
                users[u] = recs
            tmp = _STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"users": users, "adopted": sorted(self._adopted)},
                ensure_ascii=False))
            tmp.replace(_STATE)
            self._dirty = False
            self._last_flush = now
        except Exception:
            _log.debug("_flush_locked failed", exc_info=True)
            pass

    def flush(self):
        with _LOCK:
            self._flush_locked(force=True)

    def _adopt_global_if_needed(self, u: str, bucket: dict):
        """升级迁移：旧版历史是全家共享的全局记录。v0.2.48 起按账号隔离，
        只让管理员在其首次访问时把旧全局记录继承一次（管理员是原来的主要使用者），
        子账号从空白开始、不再互相串进度。"""
        if u and not bucket and _current_admin.get() and u not in self._adopted:
            g = self._users.get("", {})
            if g:
                bucket.update(json.loads(json.dumps(g)))
                self._adopted.add(u)
                self._dirty = True

    def _migrate_legacy_locked(self, bucket: dict):
        """读时迁移（调用时须已持 _LOCK）：把仍以旧分源 guid 为键的记录
        搬到规范键下（可解析时）；两边都有时合并，ts 取大。
        必须在单条读入口先整桶扫一次：旧记录可能以 A 源 guid 存着，
        却被 B 源 guid 查到——只查 B 的规范键找不到它。桶 ≤500 条。"""
        changed = False
        for key in list(bucket.keys()):
            if key.startswith(_CANON_PREFIX):
                continue
            rec = bucket.get(key)
            if not rec:
                continue
            ck = canonical_key_for_guid(rec.get("item_guid") or key)
            if not ck or ck == key:
                continue
            existing = bucket.get(ck)
            merged = _merge_records(existing, rec) if existing \
                else dict(rec)
            merged["canon_key"] = ck
            bucket.pop(key, None)
            bucket[ck] = merged
            changed = True
        if changed:
            self._dirty = True

    def _lookup_locked(self, bucket: dict, guid: str) -> dict | None:
        """桶内查一条记录（调用时须已持 _LOCK）：先旧键，再规范键。
        旧键独有且可解析时迁移到规范键下；两边都有时合并。不触发整桶
        扫描——调用方在入口扫过一次。"""
        if not guid:
            return None
        rec = bucket.get(guid)
        ck = canonical_key_for_guid(guid)
        crec = bucket.get(ck) if ck else None
        if rec is not None and crec is not None and ck != guid:
            merged = _merge_records(crec, rec)
            merged["canon_key"] = ck
            bucket.pop(guid, None)
            bucket[ck] = merged
            self._dirty = True
            return dict(merged)
        if rec is not None and ck and ck != guid and crec is None:
            moved = dict(rec)
            moved["canon_key"] = ck
            bucket.pop(guid, None)
            bucket[ck] = moved
            self._dirty = True
            return dict(moved)
        if crec is not None:
            return dict(crec)
        if rec is not None:
            return dict(rec)
        return None

    def _dedup_locked(self, bucket: dict) -> list[dict]:
        """列表读去重（调用时须已持 _LOCK）：按有效键合并——规范键
        直接用桶键，旧键记录由 item_guid 现算规范键；过渡期两源各存
        一条旧记录时，继续观看 / play/list 只出现一次。"""
        merged: dict[str, dict] = {}
        order: list[str] = []
        for key, rec in bucket.items():
            if key.startswith(_CANON_PREFIX):
                eff = key
            else:
                eff = rec.get("canon_key") or canonical_key_for_guid(
                    rec.get("item_guid") or key) or key
            if eff in merged:
                merged[eff] = _merge_records(merged[eff], rec)
            else:
                merged[eff] = dict(rec)
                order.append(eff)
        return [merged[k] for k in order]

    def record(self, body: dict, user: str | None = None) -> bool:
        """处理一次 play/record 上报，返回是否为我们的在线条目。

        新记录一律按规范键存（可解析时），同一标题同一集换源上报落到
        同一条记录上（ts 取大）；item_guid 仍保留原始分源 guid，
        供 play/list 与 continue_watching 回查 store。"""
        ig = body.get("item_guid", "")
        if not ig:
            return False
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            bucket = self._bucket(u)
            self._adopt_global_if_needed(u, bucket)
            ck = canonical_key_for_guid(ig)
            key = ck or ig
            prev = bucket.get(key, {})
            if ck and ck != ig:
                legacy = bucket.pop(ig, None)
                if legacy is not None:
                    prev = _merge_records(prev, legacy) if prev \
                        else dict(legacy)
            ts = int(float(body.get("ts", 0) or 0))
            duration = int(float(body.get("duration", 0) or 0))
            rec = {
                "item_guid": ig,
                "canon_key": ck or "",
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
            bucket[key] = rec
            self._dirty = True
            self._flush_locked()
        return True

    def set_position(self, item_guid: str, ts: int, duration: int = 0,
                     user: str | None = None):
        with _LOCK:
            bucket = self._bucket(user)
            ck = canonical_key_for_guid(item_guid)
            key = ck or item_guid
            prev = bucket.get(key, {})
            if ck and ck != item_guid:
                legacy = bucket.pop(item_guid, None)
                if legacy is not None:
                    prev = _merge_records(prev, legacy) if prev \
                        else dict(legacy)
            prev.update({"item_guid": item_guid, "canon_key": ck or "",
                         "ts": int(ts), "updated_at": time.time()})
            if duration:
                prev["duration"] = int(duration)
            bucket[key] = prev
            self._dirty = True
            self._flush_locked(force=True)

    def get(self, item_guid: str, user: str | None = None) -> dict | None:
        """先旧键、再规范键查；读时把可解析的旧键记录迁移到规范键下。"""
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            bucket = self._users.get(u, {})
            if u:
                self._adopt_global_if_needed(u, self._bucket(u))
                bucket = self._users.get(u, {})
            self._migrate_legacy_locked(bucket)
            return self._lookup_locked(bucket, item_guid)

    def all(self, user: str | None = None) -> list[dict]:
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            if u:
                bucket = self._bucket(u)
                self._adopt_global_if_needed(u, bucket)
            else:
                bucket = self._users.get("", {})
            self._flush_locked(force=True)
            return self._dedup_locked(bucket)

    def continue_watching(self, limit: int = 20,
                          user: str | None = None) -> list[dict]:
        """P2 继续观看列表：未看完（进度 <95%）的记录，按最近观看倒序。

        每条带 progress（0~1）、ts、duration，供飞牛“继续观看”库卡直接用；
        已看完的（>=95%）不进列表，由 resume_episode 负责“下一集”。
        （fork 自有方法，上游无此方法；按当前账号分区读取。）
        跨线同一集的两条旧记录在此合并为一条（按有效键去重）。
        """
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            if u:
                bucket = self._bucket(u)
                self._adopt_global_if_needed(u, bucket)
            else:
                bucket = self._users.get("", {})
            out = []
            for r in self._dedup_locked(bucket):
                if self._finished(r):
                    continue
                dur = int(r.get("duration", 0) or 0)
                ts = int(r.get("ts", 0) or 0)
                out.append({
                    **dict(r),
                    "progress": round(ts / dur, 3) if dur else 0.0,
                })
            out.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
            return out[:max(1, int(limit))]

    def position(self, ep_guid: str, title_guid: str | None = None,
                 user: str | None = None, *, is_first: bool = False) -> int:
        """某一集的续播位置（秒）。

        优先用该集自己的记录；没有时，若它是该剧第 1 集、且存在一条未看完
        的标题记录（客户端以标题/季/文件夹 guid 上报进度时落到标题键上），
        用标题记录的位置续播第 1 集——标题记录本身带不出集身份，第 1 集是
        唯一可安全回填的位置。已看完的标题记录不回填（不倒回）。
        集 guid 与标题 guid 都先旧键、再规范键查（见 get），换源同一集
        能读到另一源的进度。
        """
        rec = self.get(ep_guid, user=user)
        if rec:
            return int(rec.get("ts", 0) or 0)
        if is_first and title_guid:
            trec = self.get(title_guid, user=user)
            if trec and not self._finished(trec):
                return int(trec.get("ts", 0) or 0)
        return 0

    def resume_for_title(self, og: str, ep_guids: list[str],
                         user: str | None = None, *,
                         latest_interaction: bool = False) -> tuple[str | None, int]:
        """给定一部剧的标题 guid 与按顺序的全部集 guid，决定"该播哪一集"。

        有集记录时与 resume_episode 完全一致（CMS 按集序续播靠后的未看完集、
        T4 按最近一次交互）；只有当没有任何集记录、却有一条未看完的标题
        记录时，才回填第 1 集的标题位置——对应客户端以标题/季/文件夹 guid
        上报进度的情况。无任何记录时与旧行为一致：第 1 集 0 秒。
        集记录按规范键查：另一源报过的进度也能续上，返回的仍是本源的集 guid。
        """
        if not ep_guids:
            return None, 0
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            if u:
                bucket = self._bucket(u)
                self._adopt_global_if_needed(u, bucket)
            else:
                bucket = self._users.get("", {})
            self._migrate_legacy_locked(bucket)
            has_ep = any(self._lookup_locked(bucket, g) is not None
                         for g in ep_guids)
        if has_ep:
            return self.resume_episode(
                ep_guids, user=user, latest_interaction=latest_interaction)
        return ep_guids[0], self.position(
            ep_guids[0], og, user=user, is_first=True)

    def resume_episode(self, ep_guids: list[str],
                       user: str | None = None, *,
                       latest_interaction: bool = False) -> tuple[str | None, int]:
        """给定一部剧按顺序的全部集 guid，决定"该播哪一集"。

        - 从没看过 → 第一集（ts=0）
        - 有未看完的集（进度<95%）→ 默认按【集序】续播靠后的未看完集（CMS 原语义）；
          latest_interaction=True 时改为按【最近一次交互】选（T4 回看语义）。
        - 看过的都看完了 → 下一集（ts=0）；全看完 → 最后一集
        返回 (ep_guid 或 None 表示无记录, ts)。
        集记录按规范键查（先旧键、再规范键），换源同一集的进度也算看过。
        """
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            if u:
                bucket = self._bucket(u)
                self._adopt_global_if_needed(u, bucket)
            else:
                bucket = self._users.get("", {})
            self._migrate_legacy_locked(bucket)
            # 有序地只保留有记录的集
            watched = [(i, r) for i, g in enumerate(ep_guids)
                       if (r := self._lookup_locked(bucket, g)) is not None]
            if not watched:
                return (ep_guids[0] if ep_guids else None), 0
            # 回看较早一集时，也要恢复到最近一次交互的进度
            unfinished = [(i, r) for i, r in watched
                          if not self._finished(r)]
            if unfinished:
                # CMS 默认按集序续播；仅 T4 回看(latest_interaction)按最近交互时间。
                i, r = max(unfinished, key=lambda x: (
                    x[1].get("updated_at", 0) if latest_interaction else x[0]))
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

    def delete(self, item_guids, user: str | None = None) -> int:
        """按分源 guid 删进度：同身份的规范键记录一起删，否则删完
        又从规范键里复活。返回删掉的输入 id 数。"""
        ids = item_guids if isinstance(item_guids, (list, tuple)) else [item_guids]
        n = 0
        with _LOCK:
            u = current_user() if user is None else str(user or "")
            bucket = self._bucket(u)
            for g in ids:
                removed = False
                if g in bucket:
                    del bucket[g]
                    removed = True
                ck = canonical_key_for_guid(g)
                if ck and ck != g and ck in bucket:
                    del bucket[ck]
                    removed = True
                if removed:
                    n += 1
            if n:
                self._dirty = True
                self._flush_locked(force=True)
        return n


history = History()
