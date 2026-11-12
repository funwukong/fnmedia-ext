#!/usr/bin/env python3
"""在线资源会话存储（持久化）。

注入的 item guid 为 md5("on_<provider>_<vodid>")。标题/线路等播放所需数据
持久化到 state/titles.json，服务重启后历史搜索卡片仍可点击播放。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import providers
import t4

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("store")

_GUID_PREFIX = "on"
_TTL = 24 * 3600
_MAX_TITLES = 4000
_STATE = Path(__file__).resolve().parent.parent / "state" / "titles.json"
# 超过该字节数的 titles.json 走流式载入（逐条解析、边读边裁剪），
# 避免 100MB 级文件全量进内存；小文件仍用 json.loads 快捷路径。
_STREAM_LOAD_MIN_BYTES = 1024 * 1024
# 单集布尔字段：落盘时省略 False 值，读盘时补默认 False（见 _compact_brief）。
_EP_BOOL_FIELDS = ("direct", "resolve", "redirect", "xsrc", "t4",
                   "catvod", "drpy", "guard")


def _compact_brief(brief: dict) -> dict:
    """落盘瘦身：单集 headers 提为线路级一份、省略恒 False 的布尔字段。

    只用于落盘快照与字节测算；内存里的 _titles 始终保持完整形态
    （逐集 headers、布尔字段齐全），各读路径不用改。
    同线路各集 headers 全等且非空时提为 {"headers": ..., "eps": [...]}；
    各集 headers 不同（或为空）时保持列表形态、逐集保留非空 headers。
    """
    lines = brief.get("lines")
    if not isinstance(lines, dict):
        return dict(brief)
    new_lines: dict = {}
    for name, eps in lines.items():
        if not isinstance(eps, list):
            new_lines[name] = eps  # 已是紧凑形态或异常形态：原样保留
            continue
        compact_eps: list = []
        header_jsons: set = set()
        all_have_headers = bool(eps)
        for e in eps:
            if not isinstance(e, dict):
                compact_eps.append(e)
                all_have_headers = False
                continue
            ce = {k: v for k, v in e.items()
                  if not (k in _EP_BOOL_FIELDS and v is False)}
            h = ce.pop("headers", None)
            if h:
                header_jsons.add(json.dumps(h, ensure_ascii=False,
                                            sort_keys=True))
            else:
                all_have_headers = False
            compact_eps.append(ce)
        if all_have_headers and len(header_jsons) == 1:
            new_lines[name] = {
                "headers": json.loads(next(iter(header_jsons))),
                "eps": compact_eps}
        else:
            for ce, e in zip(compact_eps, eps):
                if isinstance(e, dict) and e.get("headers"):
                    ce["headers"] = e["headers"]
            new_lines[name] = compact_eps
    out = dict(brief)
    out["lines"] = new_lines
    return out


def _expand_brief(brief: dict) -> dict:
    """读盘展开：紧凑形态 -> 内存完整形态（逐集 headers、布尔字段补 False）。

    旧文件（逐集 headers 的列表形态）原样通过，仅补缺失的默认值
    （缺 headers 补 {}、缺布尔字段补 False），新旧两种形状都能读。
    """
    lines = brief.get("lines")
    if not isinstance(lines, dict):
        return brief
    new_lines: dict = {}
    changed = False
    for name, val in lines.items():
        if isinstance(val, dict) and "eps" in val:
            changed = True
            line_headers = val.get("headers") or {}
            eps = []
            for e in val.get("eps") or []:
                if not isinstance(e, dict):
                    eps.append(e)
                    continue
                ne = dict(e)
                ne["headers"] = e.get("headers") or line_headers
                for k in _EP_BOOL_FIELDS:
                    ne.setdefault(k, False)
                eps.append(ne)
            new_lines[name] = eps
        elif isinstance(val, list):
            # 旧形态：只有缺默认值时才重建，完整旧文件零拷贝通过。
            need = any(
                isinstance(e, dict)
                and ("headers" not in e
                     or any(k not in e for k in _EP_BOOL_FIELDS))
                for e in val)
            if not need:
                new_lines[name] = val
                continue
            eps = []
            for e in val:
                if not isinstance(e, dict):
                    eps.append(e)
                    continue
                ne = dict(e)
                if "headers" not in ne:
                    ne["headers"] = {}
                    changed = True
                for k in _EP_BOOL_FIELDS:
                    if k not in ne:
                        ne[k] = False
                        changed = True
                eps.append(ne)
            new_lines[name] = eps
        else:
            new_lines[name] = val
    if not changed:
        return brief
    out = dict(brief)
    out["lines"] = new_lines
    return out


def _select_capped(g_items: list, o_items: list) -> tuple[dict, dict]:
    """按「条数上限 + 字节上限」裁剪（与落盘、载入共用同一规则）。

    入参为插入顺序的 [(key, brief)] / [(key, orig)]；从最旧丢起，留最新。
    字节按落盘紧凑形态的 UTF-8 长度算（中文 3 字节/字），留 2% 余量。
    返回 (titles, orig)：titles 为紧凑形态（供落盘），调用方读盘时需
    再 _expand_brief 回完整形态。
    """
    max_n, max_bytes, _ = _cache_cfg()
    if len(g_items) > max_n:
        g_items = g_items[-max_n:]
        keep = {k for k, _ in g_items}
        o_items = [(k, v) for k, v in o_items if k in keep]
    compacted = [(k, _compact_brief(b)) for k, b in g_items]
    if max_bytes > 0:
        o_map = dict(o_items)
        sizes = [
            len(json.dumps(cb, ensure_ascii=False).encode("utf-8"))
            + len(json.dumps(o_map.get(k, ""), ensure_ascii=False)
                    .encode("utf-8"))
            for k, cb in compacted]
        budget = max_bytes * 0.92
        total = sum(sizes) + 256
        drop = 0
        while total > budget and drop < len(compacted):
            total -= sizes[drop]
            drop += 1
        if drop:
            compacted = compacted[drop:]
            keep = {k for k, _ in compacted}
            o_items = [(k, v) for k, v in o_items if k in keep]
    return dict(compacted), dict(o_items)


def _cache_cfg() -> tuple[int, int, float]:
    """读取 titles.json 缓存裁剪/落盘策略（条数上限、字节上限、防抖秒）。
    config 在函数内导入，避免与 config->providers 形成模块加载环。"""
    try:
        import config
        c = config.get()
        max_n = int(c.get("titles_cache_max_entries", _MAX_TITLES))
        max_mb = float(c.get("titles_cache_max_mb", 4))
        debounce = float(c.get("titles_flush_debounce", 5))
    except Exception:
        _log.debug("_cache_cfg failed", exc_info=True)
        max_n, max_mb, debounce = _MAX_TITLES, 4.0, 5.0
    return (max(100, max_n),
            max(0.0, max_mb) * 1024 * 1024,
            max(0.0, debounce))


def _h(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def make_guid(t: providers.OnlineTitle) -> str:
    return _h(f"{_GUID_PREFIX}_{t.provider}_{t.vod_id}")


def media_guid(online_guid: str) -> str:
    return _h("media:" + online_guid)


def video_guid(online_guid: str) -> str:
    return _h("video:" + online_guid)


def season_guid(online_guid: str, season: int = 1) -> str:
    return _h(f"season{season}:{online_guid}")


def episode_guid(online_guid: str, line: str = "", index: int = 0) -> str:
    """集 guid 不绑定线路（按集序号），以便多线路切换时同一集保持同一身份。"""
    return _h(f"ep:{online_guid}:{index}")


def ep_media_guid(ep_g: str, line: str = "") -> str:
    # 线路并入 media guid：同一集切换线路会得到不同 play 目标
    return _h("media:" + ep_g + (f":{line}" if line else ""))


def ep_video_guid(ep_g: str, line: str = "") -> str:
    return _h("video:" + ep_g + (f":{line}" if line else ""))


def is_online_guid(guid: str) -> bool:
    return store.get_title_by_guid(guid) is not None


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._titles: dict[str, dict] = {}
        self._orig: dict[str, str] = {}
        self._play: dict[str, dict] = {}
        self._detail_cache: dict[str, providers.OnlineTitle] = {}
        self._dirty = False
        # P1-18：写盘代际。每次真实内容变化 +1；_flush_now 只在快照
        # 期间无人再改时才清脏，避免“快照已取、落盘中途又变脏”时把新
        # 数据的脏标记误清、导致新数据迟迟不落盘。
        self._dirty_seq = 0
        # —— 后台防抖落盘（issue #42：机械盘同步全量写 20+MB 会让搜索卡 10s）——
        self._dirty_at = 0.0
        self._writer_started = False
        self._closing = False
        self._state: Path = _STATE    # 缓存文件路径（实例级，便于测试/端点引用）
        self._ep_index: dict[str, dict] = {}   # episode guid -> 定位信息（含多线路）
        self._media_index: dict[str, str] = {} # media/video guid -> ("t:"|"e:") + JSON
        self._load()

    def _ensure_writer(self):
        """惰性启动后台写线程（daemon，仅一个）。"""
        if self._writer_started:
            return
        self._writer_started = True
        t = threading.Thread(target=self._flush_loop, name="titles-flush",
                             daemon=True)
        t.start()

    def _flush_loop(self):
        """防抖写：变脏后静默满 debounce 秒（期间新脏数据顺延、合并为一次写）
        再后台落盘；debounce=0 时尽快写。"""
        while not self._closing:
            time.sleep(0.25)
            try:
                with self._lock:
                    if not self._dirty:
                        continue
                    since = time.time() - self._dirty_at
                    _, _, debounce = _cache_cfg()
                if since < debounce:
                    continue
                self._flush_now()
            except Exception:
                _log.debug("_flush_loop failed", exc_info=True)
                pass

    def _schedule_flush(self):
        """标记脏并请求后台合并写；搜索热路径不做任何磁盘 IO。

        P1-18：调用方只在内容真实变化时调这里（见 remember_* 的
        changed 判断）；无变化的重复写入不再记脏、不再排队写盘。
        调用时须已持 _lock（各 mutator 内调用，RLock 可重入）。"""
        now = time.time()
        with self._lock:
            if not self._dirty:
                self._dirty_at = now
            self._dirty = True
            self._dirty_seq += 1
        self._ensure_writer()

    def _load(self):
        """载入 titles.json：形状校验 + 与落盘相同的截断。

        旧实现只包读盘/解析：超限文件全量进内存（8000 条/116MB），且
        titles 为 list、条目为数字等形状错误会让随后的 _rebuild_index
        抛 AttributeError，Store() 构造失败（模块级 store = Store()，
        即启动即崩溃）。现在：非 dict 的 titles/orig、非 dict 的条目、
        非 str 的 orig 值逐一丢弃并记日志；超限内容按落盘规则裁剪后
        才进内存与索引；大文件走流式解析。
        """
        try:
            try:
                size = self._state.stat().st_size
            except OSError:
                size = 0
            if size > _STREAM_LOAD_MIN_BYTES:
                titles, orig = self._load_streaming()
            else:
                j = json.loads(self._state.read_text(encoding="utf-8"))
                if not isinstance(j, dict):
                    raise ValueError("titles.json top level is not an object")
                titles, orig = self._validate_loaded(j.get("titles"),
                                                      j.get("orig"), size)
            self._titles, self._orig = titles, orig
        except Exception:
            _log.debug("_load failed", exc_info=True)
            self._titles, self._orig = {}, {}
        self._rebuild_index()

    @staticmethod
    def _validate_loaded(titles, orig, file_size: int = 0) -> tuple[dict, dict]:
        """小文件路径的形状校验 + 裁剪：返回内存完整形态的 (titles, orig)。

        titles 非 dict → 整库丢弃（条目形状无法信任）；orig 非 dict →
        只丢 orig（标题可再生，orig 下次写入时补）；条目非 dict、
        orig 值非 str 逐条丢弃。裁剪复用落盘同一规则。
        快捷路径：条数、文件字节都已在上限内时不做逐条算字节
        （落盘紧凑形态只会更小，磁盘文件尺寸已是上界）。
        """
        if not isinstance(titles, dict):
            _log.warning("_load: 'titles' is not an object (%r), "
                         "starting with an empty store", type(titles).__name__)
            return {}, {}
        if not isinstance(orig, dict):
            _log.warning("_load: 'orig' is not an object (%r), discarding it",
                         type(orig).__name__)
            orig = {}
        clean_t: dict = {}
        for k, b in titles.items():
            if not isinstance(b, dict):
                _log.warning("_load: dropping non-object title entry %r", k)
                continue
            clean_t[k] = _expand_brief(b)
        clean_o = {k: v for k, v in orig.items()
                   if k in clean_t and isinstance(v, str)}
        max_n, max_bytes, _ = _cache_cfg()
        if (len(clean_t) <= max_n
                and (max_bytes <= 0 or file_size + 256 <= max_bytes * 0.92)):
            return clean_t, clean_o
        capped, capped_o = _select_capped(list(clean_t.items()),
                                          list(clean_o.items()))
        # _select_capped 返回紧凑形态；内存要完整形态，展开回去。
        return {k: _expand_brief(v) for k, v in capped.items()}, capped_o

    def _load_streaming(self) -> tuple[dict, dict]:
        """大文件流式载入：逐条解析 titles/orig，边读边按条数上限裁剪，
        读完再按字节上限裁剪；任意时刻内存里最多只有上限内的条目，
        不再把整个超限文件全量解析进内存。"""
        from collections import deque

        text = self._state.read_text(encoding="utf-8")
        dec = json.JSONDecoder()

        def skip_ws(i: int) -> int:
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            return i

        def parse_titles(i: int, keep: deque, max_n: int) -> int:
            if text[i] != "{":
                raise ValueError("'titles' is not an object")
            i += 1
            while True:
                i = skip_ws(i)
                if i >= len(text):
                    raise ValueError("unterminated 'titles' object")
                if text[i] == "}":
                    return i + 1
                key, i = dec.raw_decode(text, i)
                i = skip_ws(i)
                if text[i] != ":":
                    raise ValueError("expected ':' in 'titles'")
                i = skip_ws(i + 1)
                brief, i = dec.raw_decode(text, i)
                if isinstance(brief, dict):
                    full = _expand_brief(brief)
                    size = len(json.dumps(
                        _compact_brief(full),
                        ensure_ascii=False).encode("utf-8"))
                    keep.append((key, full, size))
                    while len(keep) > max_n:
                        keep.popleft()
                else:
                    _log.warning("_load: dropping non-object title entry %r",
                                 key)
                i = skip_ws(i)
                if text[i] == ",":
                    i += 1
                    continue
                if text[i] == "}":
                    return i + 1
                raise ValueError("expected ',' or '}' in 'titles'")

        def parse_orig(i: int) -> tuple[int, dict]:
            if text[i] != "{":
                raise ValueError("'orig' is not an object")
            i += 1
            out: dict = {}
            while True:
                i = skip_ws(i)
                if i >= len(text):
                    raise ValueError("unterminated 'orig' object")
                if text[i] == "}":
                    return i + 1, out
                key, i = dec.raw_decode(text, i)
                i = skip_ws(i)
                if text[i] != ":":
                    raise ValueError("expected ':' in 'orig'")
                i = skip_ws(i + 1)
                val, i = dec.raw_decode(text, i)
                if isinstance(val, str):
                    out[key] = val
                i = skip_ws(i)
                if text[i] == ",":
                    i += 1
                    continue
                if text[i] == "}":
                    return i + 1, out
                raise ValueError("expected ',' or '}' in 'orig'")

        max_n, max_bytes, _ = _cache_cfg()
        keep: deque = deque()
        orig: dict = {}
        i = skip_ws(0)
        if i >= len(text) or text[i] != "{":
            raise ValueError("titles.json top level is not an object")
        i += 1
        while True:
            i = skip_ws(i)
            if i >= len(text):
                raise ValueError("unterminated top-level object")
            if text[i] == "}":
                break
            key, i = dec.raw_decode(text, i)
            i = skip_ws(i)
            if text[i] != ":":
                raise ValueError("expected ':' at top level")
            i = skip_ws(i + 1)
            if key == "titles":
                i = parse_titles(i, keep, max_n)
            elif key == "orig":
                i, orig = parse_orig(i)
            else:
                _, i = dec.raw_decode(text, i)
            i = skip_ws(i)
            if text[i] == ",":
                i += 1
                continue
            if text[i] == "}":
                break
            raise ValueError("expected ',' or '}' at top level")
        # 字节上限：条数上限已在解析时增量执行，这里只做字节裁剪。
        items = list(keep)
        if max_bytes > 0:
            sizes = [sz + len(json.dumps(orig.get(k, ""),
                                         ensure_ascii=False).encode("utf-8"))
                     for k, _, sz in items]
            total = sum(sizes) + 256
            budget = max_bytes * 0.92
            drop = 0
            while total > budget and drop < len(items):
                total -= sizes[drop]
                drop += 1
            if drop:
                items = items[drop:]
        titles = {k: b for k, b, _ in items}
        keep_keys = set(titles)
        return titles, {k: v for k, v in orig.items() if k in keep_keys}

    def _direct_eps(self, brief: dict, line: str) -> list[dict]:
        # 可直接播放，或可按需解析的播放页（/share/<id>）
        return [e for e in brief.get("lines", {}).get(line, [])
                if e.get("direct") or e.get("resolve") or e.get("t4")]

    def _t4_rows(self, brief: dict) -> list[dict]:
        rows = {}
        default = self.best_line(brief["guid"])
        lines = ([default] if default else []) + [
            ln for ln in brief.get("lines", {}) if ln != default]
        for line in lines:
            for key, ep in t4.playlist_entries(self._direct_eps(brief, line), line):
                row = rows.setdefault(key, {
                    "key": key, "title": t4.episode_menu_title(ep["title"], key), "per": {}})
                row["per"][line] = ep
        def order(row):
            key = row["key"]
            number = t4.episode_number(key)
            season = t4.episode_season(key)
            return (0 if number and season else 1, season, number, key)
        return sorted(rows.values(), key=order)

    def episode_guid_at(self, og: str, index: int) -> str:
        brief = self.get_title_by_guid(og) or {}
        if brief.get("locator", {}).get("kind") == "t4":
            guids = self._t4_episode_guids.get(og, [])
            if not 0 <= index < len(guids):
                return ""
            return guids[index]
        return episode_guid(og, "", index)

    @staticmethod
    def t4_episode_guid(og: str, key: str) -> str:
        return _h(f"t4ep:{og}:{key}")

    def _index_t4(self, g: str, brief: dict):
        rows = self._t4_rows(brief)
        self._t4_episode_guids[g] = []
        numbered = any(t4.episode_number(row["key"]) for row in rows)
        positions = {}
        for index, row in enumerate(rows):
            eg = _h(f"t4ep:{g}:{row['key']}")
            self._t4_episode_guids[g].append(eg)
            per = row["per"]
            default = next(iter(per))
            season = t4.episode_season(row["key"]) if (
                t4.episode_number(row["key"]) or not numbered) else 0
            positions[season] = positions.get(season, 0) + 1
            self._ep_index[eg] = {
                "ep_guid": eg, "online_guid": g, "index": index, "season": season,
                "episode_number": t4.episode_number(row["key"]) or positions[season],
                "season_guid": season_guid(g, season), "title": brief["title"],
                "default_line": default, "lines": list(per), "_per": {
                    ln: dict(e, ep_title=e["title"]) for ln, e in per.items()},
                "ep_title": row["title"], "line": default, "t4": True,
                "episode_key": row["key"],
            }
            for line in [""] + list(per):
                meta = "e:" + json.dumps({"ep": eg, "line": line or default})
                self._media_index[ep_media_guid(eg, line)] = meta
                self._media_index[ep_video_guid(eg, line)] = meta

    def _drop_t4(self, g: str) -> None:
        """摘除单个 T4 标题的集索引（remember_merged 增量更新用）。"""
        for eg in self._t4_episode_guids.get(g, []):
            ep = self._ep_index.pop(eg, None)
            lines = [""] + list(ep.get("lines", [])) if ep else [""]
            for ln in lines:
                self._media_index.pop(ep_media_guid(eg, ln), None)
                self._media_index.pop(ep_video_guid(eg, ln), None)
        self._t4_episode_guids.pop(g, None)
        self._t4_seasons.pop(g, None)
        for sg in [k for k, v in self._season_index.items()
                   if v.get("online_guid") == g]:
            self._season_index.pop(sg, None)

    def _drop_title_index_locked(self, g: str) -> bool:
        """摘除单个标题的 ep/media 索引（增量更新用）；返回是否碰过 T4。
        调用时须已持 _lock，且 _titles[g] 仍在（_index_brief 的 drop
        路径要读旧 brief 定默认线路）。"""
        old = self._titles.get(g)
        if old is None:
            return False
        if old.get("locator", {}).get("kind") == "t4":
            self._drop_t4(g)
            return True
        self._index_brief(g, old, drop=True)
        return False

    def _index_title_locked(self, b: dict) -> bool:
        """写入单个标题的 ep/media 索引；返回是否碰过 T4。调用时须已持 _lock。"""
        g = b["guid"]
        if b.get("locator", {}).get("kind") == "t4":
            self._media_index[media_guid(g)] = "t:" + json.dumps(
                {"og": g, "line": ""})
            self._media_index[video_guid(g)] = "t:" + json.dumps(
                {"og": g, "line": ""})
            self._index_t4(g, b)
            return True
        self._index_brief(g, b)
        return False

    def _prune_detail_cache_locked(self):
        """_detail_cache 与标题淘汰同步（调用时须已持 _lock）。

        _detail_cache 以前没有任何淘汰路径，随写入 1:1 增长；现在只留
        仍在库的 online_id，超出条数上限再截最旧。get_detail 在代码库
        内暂无调用方，淘汰不影响任何读路径。
        """
        live = set(self._orig.values())
        for oid in [k for k in self._detail_cache if k not in live]:
            self._detail_cache.pop(oid, None)
        # 与标题内存上限同步：1.5× 条数上限才截，截到条数上限。
        max_n, _, _ = _cache_cfg()
        ceiling = int(max_n * 1.5)
        if len(self._detail_cache) > ceiling:
            for oid in list(self._detail_cache)[:len(self._detail_cache) - max_n]:
                self._detail_cache.pop(oid, None)

    def _rebuild_season_index(self) -> None:
        self._season_index = {}
        self._t4_seasons = {}
        for g, guids in self._t4_episode_guids.items():
            for eg in guids:
                ep = self._ep_index[eg]
                sg = ep["season_guid"]
                if sg not in self._season_index:
                    self._t4_seasons.setdefault(g, []).append(sg)
                self._season_index.setdefault(sg, {
                    "online_guid": g, "season": ep["season"], "episode_guids": [],
                })["episode_guids"].append(eg)

    def _index_brief(self, g: str, brief: dict, drop: bool = False) -> None:
        """单个标题写入（drop=True 时从）ep/media 反查索引。"""
        def put(d: dict, k: str, v: str) -> None:
            if drop:
                d.pop(k, None)
            else:
                d[k] = v

        put(self._media_index, media_guid(g), "t:" + json.dumps({"og": g, "line": ""}))
        put(self._media_index, video_guid(g), "t:" + json.dumps({"og": g, "line": ""}))
        default = self.best_line(g)
        if not default:
            return
        line_names = list(brief.get("lines", {}).keys())
        # 每条线路的可播集只算一次（原先在集循环里重复算，长剧集是 O(集数²)）
        eps_by_line = {ln: self._direct_eps(brief, ln) for ln in line_names}
        base = eps_by_line.get(default) or []
        for i, base_e in enumerate(base):
            eg = episode_guid(g, "", i)
            per_line = {}
            for ln in line_names:
                eps = eps_by_line[ln]
                if i < len(eps):
                    e = eps[i]
                    per_line[ln] = {"url": e["url"],
                                    "headers": e.get("headers", {}),
                                    "ep_title": e.get("title", ""),
                                    "resolve": bool(e.get("resolve")),
                                    "redirect": bool(e.get("redirect")),
                                    "xsrc": bool(e.get("xsrc") or e.get("dduo")),
                                    "play_from": e.get("play_from", "")}
            if drop:
                self._ep_index.pop(eg, None)
            else:
                self._ep_index[eg] = {
                    "ep_guid": eg, "online_guid": g, "index": i, "season": 1,
                    "season_guid": season_guid(g), "title": brief.get("title", ""),
                    "default_line": default, "lines": list(per_line.keys()),
                    "_per": per_line,
                    "ep_title": base_e.get("title", ""), "line": default,
                }
            for ln in per_line:
                put(self._media_index, ep_media_guid(eg, ln),
                    "e:" + json.dumps({"ep": eg, "line": ln}))
                put(self._media_index, ep_video_guid(eg, ln),
                    "e:" + json.dumps({"ep": eg, "line": ln}))
            put(self._media_index, ep_media_guid(eg, ""),
                "e:" + json.dumps({"ep": eg, "line": default}))
            put(self._media_index, ep_video_guid(eg, ""),
                "e:" + json.dumps({"ep": eg, "line": default}))

    def _rebuild_index(self):
        """重建 ep / media 反查索引（集身份与线路解耦，media guid 携带线路）。"""
        self._ep_index = {}
        self._media_index = {}
        self._t4_episode_guids = {}
        self._season_index = {}
        self._t4_seasons = {}
        for g, brief in self._titles.items():
            if brief.get("locator", {}).get("kind") == "t4":
                self._media_index[media_guid(g)] = "t:" + json.dumps({"og": g, "line": ""})
                self._media_index[video_guid(g)] = "t:" + json.dumps({"og": g, "line": ""})
                self._index_t4(g, brief)
                continue
            self._index_brief(g, brief)
        self._rebuild_season_index()

    def resolve_media_guid(self, mg: str) -> Optional[dict]:
        """media/video guid -> 可播放目标（含 url/line/index）。"""
        ref = self._media_index.get(mg)
        if not ref:
            return None
        kind, payload = ref[:1], ref[2:]
        try:
            meta = json.loads(payload)
        except ValueError:
            _log.debug("resolve_media_guid failed", exc_info=True)
            return None
        if kind == "e":
            return self._ep_resolve(meta["ep"], meta.get("line"))
        r = self.resolve_episode(meta["og"], meta.get("line") or None)
        if not r:
            return None
        return dict(r, online_guid=meta["og"], ep_guid="", kind="title")

    def get_episode_target(self, ep_g: str, line: str | None = None) -> Optional[dict]:
        """供播放层按指定线路解析某一集（line 非法时回退默认线路）。"""
        return self._ep_resolve(ep_g, line)

    def _ep_resolve(self, ep_g: str, line: str | None = None) -> Optional[dict]:
        ep = self._ep_index.get(ep_g)
        if not ep:
            return None
        ln = line if line in ep["_per"] else ep["default_line"]
        if ep.get("t4") and line and line not in ep["_per"]:
            return None
        one = ep["_per"][ln]
        return {
            "ep_guid": ep_g, "online_guid": ep["online_guid"], "line": ln,
            "index": ep["index"], "url": one["url"], "headers": one.get("headers", {}),
            "ep_title": one["ep_title"] or ep["ep_title"], "title": ep["title"],
            "resolve": one.get("resolve", False),
            "redirect": one.get("redirect", False),
            "xsrc": one.get("xsrc", False),
            # >>> PRIVATE:CATVOD
            "catvod": bool(one.get("catvod")),
            # <<< PRIVATE:CATVOD
            # >>> PRIVATE:DRPY
            "drpy": bool(one.get("drpy")),
            # <<< PRIVATE:DRPY
            # >>> PRIVATE:GUARD
            "guard": bool(one.get("guard")),
            # <<< PRIVATE:GUARD
            "play_from": one.get("play_from", ""),
            "t4": bool(one.get("t4")),
            "episode_key": one.get("episode_key", ""),
            "total": self.episode_count(ep["online_guid"]) if ep.get("t4") else
            len(self._direct_eps(self._titles[ep["online_guid"]],
                                 ep["default_line"])),
            "season_guid": ep["season_guid"], "season": ep.get("season", 1),
            "episode_number": ep.get("episode_number", ep["index"] + 1), "kind": "episode",
        }

    def get_episode(self, ep_g: str) -> Optional[dict]:
        ep = self._ep_index.get(ep_g)
        if not ep:
            return None
        d = dict(ep)
        d.pop("_per", None)
        return d

    def tv_episode_guids(self, og: str, *, regular_only: bool = False) -> list[str]:
        """某 TV（默认线路）按顺序的全部集 guid（与 episode/list 一致）。"""
        line = self.best_line(og)
        if not line:
            return []
        if self._titles[og].get("locator", {}).get("kind") == "t4":
            guids = list(self._t4_episode_guids.get(og, []))
            regular = [eg for eg in guids if self._ep_index[eg]["season"] > 0]
            return regular if regular_only and regular else guids
        n = len(self._direct_eps(self._titles[og], line))
        return [episode_guid(og, "", i) for i in range(n)]

    def seasons(self, og: str) -> list[dict]:
        brief = self.get_title_by_guid(og) or {}
        if brief.get("locator", {}).get("kind") != "t4":
            return [{"guid": season_guid(og), "season": 1,
                     "episode_guids": self.tv_episode_guids(og)}]
        rows = [dict(self._season_index[sg], guid=sg)
                for sg in self._t4_seasons.get(og, [])]
        return sorted(rows, key=lambda row: (row["season"] == 0, row["season"]))

    def get_season(self, sg: str) -> Optional[dict]:
        meta = self._season_index.get(sg)
        if meta is not None:
            return dict(meta)
        for g, brief in self._titles.items():
            if brief.get("locator", {}).get("kind") != "t4" and season_guid(g) == sg:
                return {"online_guid": g, "season": 1,
                        "episode_guids": self.tv_episode_guids(g)}
        return None

    def episode_lines(self, ep_g: str) -> list[str]:
        ep = self._ep_index.get(ep_g)
        return list(ep["_per"].keys()) if ep else []

    def target_lines(self, target: dict) -> list[dict]:
        """给定 resolve 出的播放目标，枚举该集（或电影正片）在各线路下的地址。
        返回 [{line,url,m3u8}]，默认线路排最前。"""
        og = target.get("online_guid")
        brief = self._titles.get(og)
        if not brief:
            return []
        if brief.get("locator", {}).get("kind") == "t4":
            eg = target.get("ep_guid") or self.episode_guid_at(
                og, int(target.get("index") or 0))
            return [dict(self._ep_resolve(eg, line), m3u8=False)
                    for line in self.episode_lines(eg)]
        idx = int(target.get("index", 0) or 0)
        default = self.best_line(og)
        out, seen_url = [], set()
        ordered = ([default] if default else []) + \
                  [ln for ln in brief.get("lines", {}) if ln != default]
        for ln in ordered:
            eps = self._direct_eps(brief, ln)
            if idx >= len(eps):
                continue
            url = eps[idx]["url"]
            if url in seen_url:
                continue
            seen_url.add(url)
            out.append({"line": ln, "url": url,
                        "m3u8": bool(re.search(r"\.m3u8(\?|$)", url, re.I)),
                        "redirect": bool(eps[idx].get("redirect")),
                        "resolve": bool(eps[idx].get("resolve")),
                        "xsrc": bool(eps[idx].get("xsrc") or eps[idx].get("dduo")),
                        # >>> PRIVATE:CATVOD
                        "catvod": bool(eps[idx].get("catvod")),
                        # <<< PRIVATE:CATVOD
                        # >>> PRIVATE:DRPY
                        "drpy": bool(eps[idx].get("drpy")),
                        # <<< PRIVATE:DRPY
                        # >>> PRIVATE:GUARD
                        "guard": bool(eps[idx].get("guard")),
                        # <<< PRIVATE:GUARD
                        "play_from": eps[idx].get("play_from", ""),
                        "online_guid": og, "index": idx,
                        "headers": eps[idx].get("headers", {})})
        return out

    def title_lines(self, og: str) -> list[str]:
        """该标题有直链的线路名（按直链集数降序）。"""
        brief = self._titles.get(og)
        if not brief:
            return []
        out = [(ln, len(self._direct_eps(brief, ln)))
               for ln in brief.get("lines", {})]
        return [ln for ln, n in sorted(out, key=lambda x: -x[1]) if n > 0]

    def _prune_memory_locked(self, keep_guards: set):
        """内存软上限：条数远超配置上限时，丢最旧的非保护条目。
        磁盘裁剪只影响落盘快照；这个防止长跑进程内存与索引无界增长（issue #42）。

        淘汰时同步摘除该标题的 ep/media 索引与 _detail_cache 条目：
        旧实现只清 _titles/_orig，索引里残留的集/media guid 会让
        resolve 走到已淘汰标题上（甚至抛 KeyError）。"""
        max_n, _, _ = _cache_cfg()
        ceiling = int(max_n * 1.5)
        pruned = False
        if len(self._titles) > ceiling:
            keys = [k for k in self._titles.keys() if k not in keep_guards]
            drop_n = len(self._titles) - max_n
            t4_touched = False
            for k in keys[:max(0, drop_n)]:
                # 先摘索引（需旧 brief 仍在 _titles 里定默认线路），再删数据。
                t4_touched = self._drop_title_index_locked(k) or t4_touched
                self._titles.pop(k, None)
                self._orig.pop(k, None)
            if t4_touched:
                self._rebuild_season_index()
            pruned = True
        self._prune_detail_cache_locked()
        return pruned

    def _snapshot_capped(self) -> tuple[dict, dict]:
        """在锁内只拷贝条目列表，条数/字节裁剪与逐条 json.dumps 算字节
        都在锁外做（旧实现持锁算字节，4000 条约 0.3 秒阻塞搜索/读）。
        返回落盘用快照：titles 为紧凑形态（见 _compact_brief）。
        只裁剪落盘内容，不动内存；按插入顺序丢最旧（dict 保序，最近搜索在后）。"""
        with self._lock:
            g_items = list(self._titles.items())
            o_items = list(self._orig.items())
        return _select_capped(g_items, o_items)

    def _flush_now(self) -> bool:
        """实际落盘：锁内取裁剪快照，锁外序列化+原子替换（写盘不阻塞搜索/读）。

        P1-18：快照时记下 _dirty_seq；落盘后只有代际未变才清脏——
        写盘期间若又有新变化，脏标记保留，后台防抖线程会再写一次，
        不丢数据。无变化时直接返回 False，不碰磁盘。

        代际在锁内取，快照（_snapshot_capped，含逐条算字节）在锁外做，
        避免落盘裁剪阻塞搜索/读；快照与写盘之间进来的新变化会计入
        新一代，落盘后脏标记保留、由防抖线程补写。"""
        if not self._dirty:
            return False
        with self._lock:
            if not self._dirty:
                return False
            # 代际必须在快照之前取：快照期间的变脏要计入新一代，
            # 落盘后才不被误清（先快照后取代际会让保护失效）。
            seq = self._dirty_seq
        titles, orig = self._snapshot_capped()
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"titles": titles, "orig": orig},
                              ensure_ascii=False)
            tmp = self._state.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self._state)
            with self._lock:
                if self._dirty_seq == seq:
                    self._dirty = False
            return True
        except Exception:
            _log.debug("_flush_now failed", exc_info=True)
            return False

    def flush(self, wait: bool = True) -> bool:
        """对外的立即落盘（关闭进程时调用）。wait=True 时同步写完。

        P1-18：写盘期间若又有新变化落进来，补写一次（最多 3 轮），
        避免刚落盘的快照丢新数据；无变化时直接返回 True，不碰磁盘。"""
        with self._lock:
            if not self._dirty:
                return True
        if wait:
            for _ in range(3):
                self._flush_now()
                with self._lock:
                    if not self._dirty:
                        return True
            return False
        self._ensure_writer()
        return True

    def close(self):
        """进程关闭：停掉防抖线程并尽力把最后一批脏数据写盘。

        P1-18：写盘期间若又有新变化，补写（最多 3 轮）；无变化时不碰磁盘。"""
        self._closing = True
        try:
            for _ in range(3):
                if not self._dirty:
                    break
                self._flush_now()
        except Exception:
            _log.debug("close failed", exc_info=True)
            pass

    def remember_titles(self, titles: list[providers.OnlineTitle]) -> list[dict]:
        import inject
        now = time.time()
        briefs = []
        with self._lock:
            # P1-18：只有标题/来源真实变化才记脏；内容完全相同的重复
            # 写入（如同一搜索结果反复入库）不再触发写盘。_detail_cache
            # 与 _play 只在内存，不计入脏判断。
            # 增量索引（与 remember_merged 同款）：只摘除/写入本批标题
            # 的索引，不再每次全量 _rebuild_index（4000 条下 10 秒级，
            # 且全程持锁阻塞搜索）。
            changed = False
            t4_touched = False
            for t in titles:
                g = make_guid(t)
                b = inject.brief_of(t)
                if self._titles.get(g) != b or self._orig.get(g) != t.online_id:
                    changed = True
                if self._titles.get(g) is not None:
                    t4_touched = self._drop_title_index_locked(g) or t4_touched
                self._titles[g] = b
                self._orig[g] = t.online_id
                self._detail_cache[t.online_id] = t
                briefs.append(b)
            self._play = {k: v for k, v in self._play.items() if now - v["ts"] < _TTL}
            self._prune_memory_locked({make_guid(t) for t in titles})
            if changed:
                self._schedule_flush()
            # 同一批里同一 guid 出现多次时只索引最后一份，避免旧份残留。
            index_targets: dict = {}
            for b in briefs:
                index_targets[b["guid"]] = b
            for b in index_targets.values():
                t4_touched = self._index_title_locked(b) or t4_touched
            if t4_touched:
                self._rebuild_season_index()
        return briefs

    def remember_merged(self, briefs: list[dict]) -> list[dict]:
        """保存跨源合并后的 brief（guid 已在 brief 内确定）。

        只增量更新本批 brief 的索引：全量重建在几千条缓存下要 5 秒以上，
        会拖慢每次搜索。替换前先用旧 brief 摘除索引项，避免线路/集数变化后
        残留指向旧线路的死链。
        """
        with self._lock:
            t4_touched = False
            # P1-18：只有标题/来源真实变化才记脏；T4 分支先按旧线路归一化
            # 再比较，避免“线路沿用旧值”这种无实质变化也触发写盘。
            changed = False
            for b in briefs:
                old = self._titles.get(b["guid"])
                if b.get("locator", {}).get("kind") == "t4" and old and old.get("lines"):
                    # A search card must not erase loaded detail or historical episode IDs.
                    locator = dict(b["locator"], loaded_at=0)
                    b = dict(b, lines=old["lines"], locator=locator)
                if b["guid"] not in self._orig or old is None or old != b:
                    changed = True
                if old is not None:
                    if old.get("locator", {}).get("kind") == "t4":
                        self._drop_t4(b["guid"])
                        t4_touched = True
                    else:
                        self._index_brief(b["guid"], old, drop=True)
                self._titles[b["guid"]] = b
                self._orig.setdefault(b["guid"], b.get("online_id", ""))
            self._prune_memory_locked({b["guid"] for b in briefs})
            if changed:
                self._schedule_flush()
            for b in briefs:
                if b.get("locator", {}).get("kind") == "t4":
                    self._media_index[media_guid(b["guid"])] = "t:" + json.dumps(
                        {"og": b["guid"], "line": ""})
                    self._media_index[video_guid(b["guid"])] = "t:" + json.dumps(
                        {"og": b["guid"], "line": ""})
                    self._index_t4(b["guid"], b)
                    t4_touched = True
                else:
                    self._index_brief(b["guid"], b)
            if t4_touched:
                self._rebuild_season_index()
        return briefs

    def replace_t4_detail(self, guid: str, title: providers.OnlineTitle) -> dict:
        import inject
        with self._lock:
            brief = dict(inject.brief_of(title), guid=guid)
            # P1-18：内容无变化不记脏
            changed = (self._titles.get(guid) != brief
                       or self._orig.get(guid) != title.online_id)
            t4_touched = False
            if self._titles.get(guid) is not None:
                t4_touched = self._drop_title_index_locked(guid)
            self._titles[guid] = brief
            self._orig[guid] = title.online_id
            self._prune_memory_locked({guid})
            if changed:
                self._schedule_flush()
            t4_touched = self._index_title_locked(brief) or t4_touched
            if t4_touched:
                self._rebuild_season_index()
            return brief

    def get_title_by_guid(self, guid: str) -> Optional[dict]:
        with self._lock:
            return self._titles.get(guid)

    def find_title_with_line(self, title: str, line: str) -> Optional[str]:
        """同名标题常因不同搜索/聚合路径各存一条、且只挂部分线路。当播放页记录里
        没有用户所选线路时，按片名找到「包含该线路、线路最多」的另一条记录，返回其
        guid，避免静默退回第一条死链。"""
        if not title or not line:
            return None
        best, best_n = None, -1
        disp = line.split("::", 1)[1] if "::" in line else line
        with self._lock:
            for g, b in self._titles.items():
                if b.get("title") != title:
                    continue
                lines = b.get("lines") or {}
                hit = line in lines or any(
                    k == line or ("::" in k and k.split("::", 1)[1] == disp)
                    for k in lines)
                if hit and len(lines) > best_n:
                    best, best_n = g, len(lines)
        return best

    def provider_of(self, online_guid: str) -> str:
        """guid 对应标题所属资源源 id（来自 online_id "<provider>:<vod_id>"）。"""
        with self._lock:
            return (self._orig.get(online_guid, "") or "").split(":", 1)[0]

    def remember_detail(self, t: providers.OnlineTitle) -> str:
        import inject
        with self._lock:
            g = make_guid(t)
            brief = inject.brief_of(t)
            # P1-18：内容无变化不记脏
            changed = (self._titles.get(g) != brief
                       or self._orig.get(g) != t.online_id)
            t4_touched = False
            if self._titles.get(g) is not None:
                t4_touched = self._drop_title_index_locked(g)
            self._titles[g] = brief
            self._orig[g] = t.online_id
            self._detail_cache[t.online_id] = t
            self._prune_memory_locked({g})
            if changed:
                self._schedule_flush()
            t4_touched = self._index_title_locked(brief) or t4_touched
            if t4_touched:
                self._rebuild_season_index()
        return g

    def get_detail(self, online_id: str) -> Optional[providers.OnlineTitle]:
        with self._lock:
            return self._detail_cache.get(online_id)

    def online_guid_by_media(self, mg: str) -> Optional[str]:
        with self._lock:
            for g in self._titles:
                if media_guid(g) == mg or video_guid(g) == mg:
                    return g
        return None

    # ---- 季 / 集（原生 TV 选集）----
    @staticmethod
    def _playable(eps: list[dict]) -> list[dict]:
        return [e for e in eps if e.get("direct") or e.get("resolve") or e.get("t4")]

    def best_line(self, guid: str) -> str | None:
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        # 直链线路优先于仅可解析的线路
        best, bestn, best_direct = None, -1, False
        for ln, eps in brief.get("lines", {}).items():
            play = self._playable(eps)
            nd = sum(1 for e in play if e.get("direct"))
            if not play:
                continue
            direct = nd > 0
            if (direct != best_direct and direct) or \
               (direct == best_direct and len(play) > bestn):
                best, bestn, best_direct = ln, len(play), direct
        return best

    def episode_count(self, guid: str, line: str | None = None) -> int:
        brief = self.get_title_by_guid(guid)
        if not brief:
            return 0
        if brief.get("locator", {}).get("kind") == "t4" and not line:
            return len(self._t4_episode_guids.get(guid, []))
        ln = line if line in brief.get("lines", {}) else self.best_line(guid)
        if not ln:
            return 0
        return len(self._playable(brief["lines"][ln]))

    def season_episode(self, season_g: str, index: int) -> Optional[dict]:
        """由伪造的 season guid 与集序号返回在线标题 guid / 线路 / 集。"""
        meta = self.get_season(season_g)
        if meta:
            guids = meta["episode_guids"]
            return self.get_episode_target(guids[index]) if 0 <= index < len(guids) else None
        for g in self._titles:
            if season_guid(g) == season_g:
                ln = self.best_line(g)
                return self.resolve_episode(g, ln, index)
        return None

    def resolve_episode_guid(self, ep_g: str) -> Optional[dict]:
        """由伪造的 episode guid 反查真实播放地址。"""
        for g, brief in self._titles.items():
            for ln, eps in brief.get("lines", {}).items():
                dirs = [e for e in eps if e.get("direct")]
                for i, e in enumerate(dirs):
                    if episode_guid(g, ln, i) == ep_g:
                        return {"online_guid": g, "url": e["url"],
                                "headers": e.get("headers", {}), "line": ln,
                                "index": i, "total": len(dirs),
                                "ep_title": e["title"], "title": brief.get("title", "")}
        return None

    def list_episodes(self, guid: str, line: str | None = None) -> list[dict]:
        """返回某线路（或第一条可播线路）下所有可直放剧集。"""
        brief = self.get_title_by_guid(guid)
        if not brief:
            return []
        lines = brief.get("lines", {})
        name = line if line in lines else next(iter(lines), None)
        if name is None:
            return []
        return [dict(e, _line=name) for e in lines[name] if e.get("direct")]

    def resolve_episode(self, guid: str, line: str | None = None,
                        ep_index: int = 0) -> Optional[dict]:
        """解析出某一集的真实地址。返回 {url,headers,line,index,total,ep_title}。"""
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        if brief.get("locator", {}).get("kind") == "t4":
            eg = self.episode_guid_at(guid, int(ep_index or 0))
            return self._ep_resolve(eg, line) if eg else None
        lines = brief.get("lines", {})
        # 选线路：指定 > 第一条可播（直链优先，其次可解析页）
        name = None
        if line and line in lines and self._playable(lines[line]):
            name = line
        else:
            # 直链优先
            for ln, eps in lines.items():
                if any(e.get("direct") for e in eps):
                    name = ln
                    break
            if name is None:
                for ln, eps in lines.items():
                    if self._playable(eps):
                        name = ln
                        break
        if name is None:
            return None
        eps = self._playable(lines[name])
        if not eps:
            return None
        ep_index = max(0, min(int(ep_index or 0), len(eps) - 1))
        ep = eps[ep_index]
        return {"url": ep["url"], "headers": ep.get("headers", {}),
                "line": name, "index": ep_index, "total": len(eps),
                "resolve": bool(ep.get("resolve")),
                "redirect": bool(ep.get("redirect")),
                "xsrc": bool(ep.get("xsrc") or ep.get("dduo")),
                # >>> PRIVATE:CATVOD
                "catvod": bool(ep.get("catvod")),
                # <<< PRIVATE:CATVOD
                # >>> PRIVATE:DRPY
                "drpy": bool(ep.get("drpy")),
                # <<< PRIVATE:DRPY
                # >>> PRIVATE:GUARD
                "guard": bool(ep.get("guard")),
                # <<< PRIVATE:GUARD
                "play_from": ep.get("play_from", ""),
                "online_guid": guid,
                "ep_title": ep["title"], "title": brief.get("title", "")}

    def first_playable(self, guid: str) -> Optional[dict]:
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        for line, eps in brief["lines"].items():
            for ep in eps:
                if ep.get("direct"):
                    return {"line": line, "ep": ep}
        return None

    def create_play(self, guid: str, line: str | None = None, ep_index: int = 0):
        brief = self.get_title_by_guid(guid)
        if not brief:
            return None
        lines = brief["lines"]
        name = line if line in lines else next(iter(lines), None)
        if name is None:
            return None
        eps = [e for e in lines[name] if e.get("direct")]
        if not eps:
            return None
        ep_index = max(0, min(ep_index, len(eps) - 1))
        ep = eps[ep_index]
        psid = uuid.uuid4().hex
        with self._lock:
            self._play[psid] = {
                "url": ep["url"], "headers": ep.get("headers", {}),
                "title": brief["title"], "ep": ep["title"], "ts": time.time()}
        return {"psid": psid, "line": name, "ep": ep, "index": ep_index,
                "total": len(eps)}

    def get_play(self, psid: str) -> Optional[dict]:
        with self._lock:
            return self._play.get(psid)


store = Store()
