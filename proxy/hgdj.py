#!/usr/bin/env python3
"""红果短剧（HGDJ）伪媒体库。

HGDJ 是一个本机 companion 服务（飞牛 NAS 上需另行部署），本机监听
``http://127.0.0.1:18098``，对外提供：

- ``GET /api/recommend``            -> ``{"items":[...]}``
- ``GET /api/browse?category=<cat>&page=<n>``
                                   -> ``{"items":[...],"pagination":{"total","totalPages"}}``
- ``GET /api/series/<sid>``         -> ``{"detail":{"series_name","series_intro",
                                      "series_cover","tags","episode_cnt","vid_list":[...]}}``
- ``GET /api/play/<sid>/<ep>``      -> 解密 mp4 流（支持 Range，Content-Type video/mp4）

本模块只做 **客户端**：拉取 companion 的元数据，伪造飞牛原生 item/season/episode/stream
结构，并把播放请求代理到 companion 的 ``/api/play``。整条播放链走“云盘直连”类型
（``cloud_storage_info={cloud_storage_type:2}`` + ``isDirectLinkEnabled:1``），
以绕过飞牛原生播放器对非网盘直链报 -99985 的限制（与 aimimang 同思路）。

开关（config.py DEFAULTS）：``hgdj_enabled=False``、``hgdj_url``。默认关闭；用户需
在管理页打开，且本机已部署 HGDJ companion 服务，媒体库才会出现数据。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from config import config

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("hgdj")

# 与 app.py:60 保持一致；播放直链与代理路由都挂在这个前缀下。
ONLINE_PREFIX = "/v/media/online"
HGDJ_DEFAULT_URL = "http://127.0.0.1:18098"
HGDJ_LIB_GUID = "on" + hashlib.md5(b"fnv-hgdj-lib").hexdigest()[:30]
HGDJ_LIB_NAME = "红果短剧"
HGDJ_TTL = 1800            # 库列表/推荐缓存时长（秒）
HGDJ_DETAIL_TTL = 3600     # 单部详情解析缓存时长（秒）
HGDJ_CATS = ("real-drama", "comic-drama", "ai-drama", "comic")
_LOAD_CONCURRENCY = 6

STATE = Path(__file__).resolve().parent.parent / "state" / "hgdj" / "library.json"

# 模块级缓存（进程内，避免每个请求都打 companion）。
_config_cache: tuple[float, tuple[bool, str]] = (0.0, (False, HGDJ_DEFAULT_URL))
_cache: dict[str, Any] = {"ts": 0.0, "items": {}, "order": [], "totals": {},
                          "total_pages": {}, "all_loaded": False}
_cache_lock = threading.Lock()
_resolve_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# guid / 配置
# --------------------------------------------------------------------------- #
def hgdj_guid_of(sid: Any) -> str:
    return "on" + hashlib.md5(("fnv-hgdj-" + str(sid)).encode()).hexdigest()[:30]


def series_guid_of(sid: Any) -> str:
    return hgdj_guid_of(sid)


def season_guid_of(series_guid: str) -> str:
    return f"hgdj-season:{series_guid}"


def episode_guid_of(series_guid: str, ep: int) -> str:
    return f"hgdj-ep:{series_guid}:{ep}"


def media_guid_of(series_guid: str, ep: int) -> str:
    return f"hgdj-mg:{series_guid}:{ep}"


def parse_hgdj_mg(mg: str) -> Optional[tuple[str, int]]:
    """hgdj-mg:<series_guid>:<ep> -> (series_guid, ep)。"""
    if not mg.startswith("hgdj-mg:"):
        return None
    rest = mg[len("hgdj-mg:"):]
    try:
        sg, ep = rest.rsplit(":", 1)
        return sg, int(ep)
    except ValueError:
        return None


def is_hgdj_series(g: str) -> bool:
    return g == HGDJ_LIB_GUID or g.startswith("hgdj-season:") or g.startswith("on") and len(g) == 32 and _cache["items"].get(g, {}).get("hgdj")


def is_hgdj_episode(g: str) -> bool:
    return g.startswith("hgdj-ep:")


def is_hgdj_media_guid(mg: str) -> bool:
    return mg.startswith("hgdj-mg:")


def lib_guid() -> str:
    return HGDJ_LIB_GUID


def enabled() -> bool:
    return bool(config.get().get("hgdj_enabled", False))


def _cfg() -> tuple[bool, str]:
    global _config_cache
    now = time.time()
    if now - _config_cache[0] < 2.0:
        return _config_cache[1]
    val = (enabled(), config.get().get("hgdj_url", HGDJ_DEFAULT_URL) or HGDJ_DEFAULT_URL)
    _config_cache = (now, val)
    return val


# --------------------------------------------------------------------------- #
# companion 请求
# --------------------------------------------------------------------------- #
async def _get_json(client: httpx.AsyncClient, url: str, timeout: float = 20.0) -> Optional[dict]:
    try:
        r = await client.get(url, timeout=timeout)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        _log.debug("_get_json failed", exc_info=True)
        return None


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(20), trust_env=False, follow_redirects=True)


# --------------------------------------------------------------------------- #
# 原始条目 -> 内部 rec
# --------------------------------------------------------------------------- #
def _raw_rec(raw: dict) -> dict:
    sid = raw.get("series_id") or raw.get("seriesId")
    title = raw.get("series_name") or raw.get("name") or ""
    cover = raw.get("series_cover") or raw.get("cover") or ""
    intro = raw.get("series_intro") or raw.get("intro") or ""
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        n = int(raw.get("episode_cnt") or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, 1000))
    eps = [{"name": f"第{i}集", "hgdj_ep": i} for i in range(1, n + 1)]
    g = hgdj_guid_of(sid)
    return {
        "title": title,
        "overview": intro,
        "pic": cover,
        "pic_cms": cover,
        "tags": tags,
        "year": 2026,
        "site": HGDJ_LIB_NAME,
        "hgdj": True,
        "hgdj_sid": sid,
        "hgdj_resolved": False,
        "hgdj_last_try": 0.0,
        "tv": 0,
        "eps": eps,
        "guid": g,
    }


def _merge(rec: dict, old: dict | None) -> dict:
    if old is None:
        return rec
    # 已解析详情优先；缺字段则补旧值。
    for k, v in old.items():
        if k not in rec or rec[k] in (None, "", [], {}):
            rec[k] = v
    rec["hgdj_resolved"] = old.get("hgdj_resolved", rec.get("hgdj_resolved", False))
    return rec


# --------------------------------------------------------------------------- #
# 库加载 / 缓存
# --------------------------------------------------------------------------- #
def _save() -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def _load_disk() -> bool:
    global _cache
    try:
        j = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.debug("_load_disk failed", exc_info=True)
        return False
    if not isinstance(j, dict):
        return False
    with _cache_lock:
        _cache = j
    return True


def _stale() -> bool:
    return time.time() - _cache.get("ts", 0.0) > HGDJ_TTL


def _covers() -> list[str]:
    out: list[str] = []
    for g in _cache.get("order", []):
        rec = _cache["items"].get(g)
        if rec and rec.get("pic_cms"):
            out.append(rec["pic_cms"])
        if len(out) >= 12:
            break
    return out


def cached_posters(n: int = 6) -> list[str]:
    return _covers()[:n]


def total() -> int:
    return len(_cache.get("order", []))


def poster_of(guid: str) -> str:
    rec = _cache["items"].get(guid)
    return rec.get("pic_cms") or "" if rec else ""


def series_guids() -> list[str]:
    return list(_cache.get("order", []))


def browse_guids(page: int, size: int) -> tuple[list[str], int, int]:
    order = _cache.get("order", [])
    total_n = len(order)
    pages = max(1, (total_n + size - 1) // size) if size > 0 else 1
    page = max(1, min(page, pages))
    start = (page - 1) * size
    return order[start:start + size], total_n, pages


async def series_detail(guid: str) -> Optional[dict]:
    await ensure_resolved(guid)
    with _cache_lock:
        rec = _cache["items"].get(guid)
        if rec is None:
            return None
        rec = dict(rec)
    if not rec.get("eps"):
        n = int(rec.get("episode_cnt") or 0) or 0
        rec["eps"] = [{"name": f"第{i}集", "hgdj_ep": i} for i in range(1, n + 1)]
    return rec


def episode_num(rec: dict) -> int:
    if rec.get("hgdj_ep"):
        return int(rec["hgdj_ep"])
    if rec.get("episode_number"):
        return int(rec["episode_number"])
    eps = rec.get("eps") or []
    if eps:
        return int(eps[0].get("hgdj_ep", 1))
    return 1


# --------------------------------------------------------------------------- #
# 详情解析（companion /api/series/<sid>）
# --------------------------------------------------------------------------- #
async def ensure_resolved(guid: str) -> None:
    with _cache_lock:
        rec = _cache["items"].get(guid)
        if rec is None:
            return
        if rec.get("hgdj_resolved") and time.time() - rec.get("hgdj_last_try", 0.0) < HGDJ_DETAIL_TTL:
            return
        rec["hgdj_last_try"] = time.time()
        sid = rec.get("hgdj_sid")
    if sid is None:
        return
    _, base = _cfg()
    async with _http() as client:
        j = await _get_json(client, f"{base}/api/series/{sid}")
    if not j:
        return
    detail = j.get("detail") or {}
    vid_list = detail.get("vid_list") or []
    with _cache_lock:
        rec = _cache["items"].get(guid)
        if rec is None:
            return
        rec["title"] = detail.get("series_name") or rec.get("title") or ""
        rec["overview"] = detail.get("series_intro") or rec.get("overview") or ""
        rec["pic"] = detail.get("series_cover") or rec.get("pic") or ""
        rec["pic_cms"] = detail.get("series_cover") or rec.get("pic_cms") or ""
        tags = detail.get("tags") or rec.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        rec["tags"] = tags
        try:
            n = int(detail.get("episode_cnt") or len(vid_list) or rec.get("episode_cnt") or 0)
        except (TypeError, ValueError):
            n = 0
        eps = []
        if vid_list:
            for vid in vid_list:
                ep_no = int(vid.get("episode") or vid.get("index") or len(eps) + 1)
                eps.append({"name": vid.get("name") or f"第{ep_no}集",
                            "hgdj_ep": ep_no, "hgdj_vid": vid})
        else:
            eps = [{"name": f"第{i}集", "hgdj_ep": i} for i in range(1, n + 1)]
        rec["eps"] = eps
        rec["hgdj_resolved"] = True
        rec["hgdj_resolved_at"] = time.time()
    _save()


# --------------------------------------------------------------------------- #
# 库刷新（companion /api/recommend + 每分类 /api/browse 全部分页）
# --------------------------------------------------------------------------- #
async def fetch_library(force: bool = False) -> bool:
    on, base = _cfg()
    if not on:
        return False
    if not force and not _stale() and _cache.get("all_loaded"):
        return True
    async with _http() as client:
        recs: dict[str, dict] = {}
        order: list[str] = []

        # 推荐
        j = await _get_json(client, f"{base}/api/recommend")
        items = (j or {}).get("items") or []
        for raw in items:
            rec = _raw_rec(raw)
            recs[rec["guid"]] = _merge(rec, recs.get(rec["guid"]))
            if rec["guid"] not in order:
                order.append(rec["guid"])

        # 分类浏览：逐分类翻完全部分页
        sem = asyncio.Semaphore(_LOAD_CONCURRENCY)

        async def _browse(cat: str):
            page = 1
            while True:
                url = f"{base}/api/browse?category={cat}&page={page}"
                jp = await _get_json(client, url)
                if not jp:
                    break
                its = jp.get("items") or []
                pag = jp.get("pagination") or {}
                with _cache_lock:
                    for raw in its:
                        rec = _raw_rec(raw)
                        recs[rec["guid"]] = _merge(rec, recs.get(rec["guid"]))
                        if rec["guid"] not in order:
                            order.append(rec["guid"])
                total_pages = int(pag.get("totalPages") or 0)
                if not its or (total_pages and page >= total_pages):
                    break
                page += 1
                if page > 200:
                    break

        await asyncio.gather(*(_browse(c) for c in HGDJ_CATS))

    with _cache_lock:
        # 合并到既有缓存（保留已解析详情）
        for g, rec in recs.items():
            _cache["items"][g] = _merge(rec, _cache["items"].get(g))
        # 重新排序：既有 order 优先，新追加在末尾（不破坏既有顺序）
        new_order = list(_cache.get("order", []))
        for g in order:
            if g not in new_order:
                new_order.append(g)
        _cache["order"] = new_order
        _cache["ts"] = time.time()
        _cache["all_loaded"] = True
    _save()
    return True


# --------------------------------------------------------------------------- #
# 媒体库卡片 / 元数据
# --------------------------------------------------------------------------- #
def lib_info() -> dict:
    covers = cached_posters(12)
    n = total()
    return {
        "guid": HGDJ_LIB_GUID,
        "name": HGDJ_LIB_NAME,
        "title": HGDJ_LIB_NAME,
        "category": "TV",
        "dir_list": [],
        "lan": "zh-CN",
        "auto_scrap_subtitle": 0,
        "include_adult": False,
        "subtitle_lan": "zh-CN",
        "poster": covers[0] if covers else "",
        "poster_type": 0,
        "posters": covers,
        "view_type": 0,
        "refresh_disabled": True,
        "id": 0,
        "number_of_episodes": n,
        "number_of_seasons": 1,
        "season_number": 0,
        "episode_number": 0,
        "can_play": 1,
        "ancestor_guid": "",
        "ancestor_category": "TV",
        "media_stream": {
            "resolutions": ["1080p"],
            "audio_type": ["Stereo"],
            "color_range_type": ["SDR"],
        },
        "isDirectLinkEnabled": 1,
    }


def series_card(guid: str) -> dict:
    rec = _cache["items"].get(guid) or {}
    covers = [rec.get("pic_cms") or ""] if rec.get("pic_cms") else []
    n = len(rec.get("eps") or [])
    if n == 0:
        try:
            n = int(rec.get("episode_cnt") or 0)
        except (TypeError, ValueError):
            n = 0
    return {
        "guid": guid,
        "title": rec.get("title") or "",
        "type": "TV",
        "category": "TV",
        "poster": covers[0] if covers else "",
        "posters": covers,
        "poster_type": 0,
        "overview": rec.get("overview") or "",
        "number_of_episodes": n,
        "number_of_seasons": 1,
        "season_number": 0,
        "episode_number": 0,
        "can_play": 0,
        "ancestor_guid": HGDJ_LIB_GUID,
        "ancestor_name": HGDJ_LIB_NAME,
        "ancestor_category": "TV",
        "isDirectLinkEnabled": 1,
    }


def episode_card(guid: str, series_guid: str, ep: int, title: str) -> dict:
    return {
        "guid": guid,
        "title": title,
        "type": "Episode",
        "category": "TV",
        "poster": poster_of(series_guid),
        "posters": [poster_of(series_guid)] if poster_of(series_guid) else [],
        "poster_type": 0,
        "season_number": 1,
        "episode_number": ep,
        "number_of_seasons": 1,
        "number_of_episodes": 0,
        "can_play": 1,
        "ancestor_guid": season_guid_of(series_guid),
        "ancestor_name": "第1季",
        "ancestor_category": "TV",
        "parent_guid": season_guid_of(series_guid),
        "parent_title": "第1季",
        "isDirectLinkEnabled": 1,
    }


# --------------------------------------------------------------------------- #
# 播放合成（云盘直连类型，绕过 -99985）
# --------------------------------------------------------------------------- #
def abs_url(path: str, headers: dict[str, str]) -> str:
    """按请求头重建绝对 URL（飞牛端要把 play_link 当绝对直链访问）。"""
    host = (headers.get("x-forwarded-host")
            or headers.get("host")
            or "127.0.0.1:5666")
    proto = (headers.get("x-forwarded-proto")
             or ("https" if ":5667" in host or host.startswith("https") else "http"))
    if "://" not in host:
        default_port = "5667" if proto == "https" else "5666"
        if ":" not in host.split("/")[0]:
            host = f"{host}:{default_port}"
    if "://" in host:
        base = host
    else:
        base = f"{proto}://{host}"
    return base.rstrip("/") + path


def _direct(series_guid: str, ep: int, headers: dict[str, str]) -> str:
    return abs_url(f"{ONLINE_PREFIX}/hgdj/{series_guid}.mp4?ep={ep}", headers)


def synth_stream(guid: str, rec: dict, headers: dict[str, str]) -> dict:
    ep = episode_num(rec)
    sid = rec.get("hgdj_sid")
    direct = _direct(guid, ep, headers)
    vg = "onv" + guid[2:][:29]
    ag = "ona" + guid[2:][:29]
    mg = media_guid_of(guid, ep)
    return {
        "files": [
            {
                "media_guid": mg,
                "path": direct,
                "file_name": f"{rec.get('title', '')} 第{ep}集.mp4",
                "size": 0,
                "timestamp": 0,
                "type": 1,
                "sort_num": 0,
                "can_play": 1,
                "play_error": "",
            }
        ],
        "video_streams": [
            {
                "media_guid": mg,
                "title": "",
                "guid": vg,
                "resolution_type": "1080p",
                "color_range_type": "SDR",
                "codec_name": "hevc",
                "codec_type": "video",
                "width": 1080,
                "height": 1920,
                "bitrate": 0,
                "frame_rate": 0,
                "aspect_ratio": "9:16",
                "wrapper": "MP4",
                "is_bluray": False,
            }
        ],
        "audio_streams": [],
        "subtitle_streams": [],
        "play_link": direct,
        "qualities": [
            {
                "bitrate": 0,
                "resolution": "1080p",
                "progressive": True,
                "is_m3u8": False,
                "url": direct,
            }
        ],
        "cloud_storage_info": {"cloud_storage_type": 2},
        "isDirectLinkEnabled": 1,
        "audio_guid": ag,
        "video_guid": vg,
    }


def synth_play(guid: str, rec: dict, headers: dict[str, str]) -> dict:
    ep = episode_num(rec)
    direct = _direct(guid, ep, headers)
    vg = "onv" + guid[2:][:29]
    ag = "ona" + guid[2:][:29]
    mg = media_guid_of(guid, ep)
    return {
        "audio_guid": ag,
        "audio_index": -1,
        "hls_time": 6,
        "is_subtitle_external": 0,
        "media_guid": mg,
        "play_link": direct,
        "subtitle_guid": "",
        "subtitle_index": -1,
        "subtitle_link": "",
        "video_guid": vg,
        "video_index": 0,
        "wrapper": "MP4",
        "non_fatal_errno": None,
        "video_encoder": "",
        "not_supported_resolution": "",
        "supported_highest_resolution": "",
        "item_guid": guid,
        "title": rec.get("title", ""),
        "ts": int(time.time()),
        "cloud_storage_info": {"cloud_storage_type": 2},
        "isDirectLinkEnabled": 1,
        "qualities": [
            {
                "bitrate": 0,
                "resolution": "1080p",
                "progressive": True,
                "is_m3u8": False,
                "url": direct,
            }
        ],
    }


def media_url(series_guid: str, ep: int) -> str:
    """companion 直接播放流地址（不经过本插件代理，留给需要裸链的场景）。"""
    _, base = _cfg()
    return f"{base}/api/play/{series_guid and _sid_of(series_guid)}/{ep}"


def _sid_of(series_guid: str) -> Any:
    rec = _cache["items"].get(series_guid) or {}
    return rec.get("hgdj_sid")


# --------------------------------------------------------------------------- #
# 代理：GET {ONLINE_PREFIX}/hgdj/{guid}.mp4?ep=N -> companion /api/play/<sid>/<ep>
# --------------------------------------------------------------------------- #
async def proxy_media(guid: str, ep: int, req_headers: dict[str, str]):
    """返回 (status_code, headers_dict, byte_iterator) 供 StreamingResponse 使用。

    companion /api/play 支持 Range（用于拖拽）。这里透传 Range 头。
    """
    with _cache_lock:
        rec = _cache["items"].get(guid)
        sid = rec.get("hgdj_sid") if rec else None
    if sid is None:
        return 404, {}, None
    _, base = _cfg()
    url = f"{base}/api/play/{sid}/{ep}"
    fwd = {}
    for h in ("range", "user-agent", "referer"):
        if h in req_headers:
            fwd[h] = req_headers[h]
    client = httpx.AsyncClient(timeout=httpx.Timeout(240, sock_read=240),
                               trust_env=False, follow_redirects=True)
    try:
        r = await client.send(client.build_request("GET", url, headers=fwd), stream=True)
    except Exception:
        _log.debug("proxy_media failed: guid=%s ep=%s", guid, ep, exc_info=True)
        await client.aclose()
        return 502, {}, None

    async def _gen():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    out_headers = {}
    for k, v in r.headers.items():
        lk = k.lower()
        if lk in ("content-type", "content-length", "content-range",
                  "accept-ranges", "cache-control", "etag", "last-modified"):
            out_headers[k] = v
    out_headers.setdefault("Content-Type", "video/mp4")
    return r.status_code, out_headers, _gen()


# --------------------------------------------------------------------------- #
# 搜索并入
# --------------------------------------------------------------------------- #
def search_items(kw: str, limit: int = 20) -> list[dict]:
    """仅查本地已缓存标题（不再额外打 upstream）。返回 series_card 列表。"""
    kw = (kw or "").strip().lower()
    if not kw:
        return []
    out: list[dict] = []
    for g in _cache.get("order", []):
        rec = _cache["items"].get(g)
        if not rec:
            continue
        title = (rec.get("title") or "").lower()
        if kw in title:
            out.append(series_card(g))
            if len(out) >= limit:
                break
    return out
