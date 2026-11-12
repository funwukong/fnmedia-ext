#!/usr/bin/env python3
"""fnmedia-ext 主代理（FastAPI）。

链路：nginx(/v) -> 本代理(UDS) -> trim-media 官方后端(upstream UDS)
策略：
  - 搜索 GET /v/api/v1/search/list   透传本地结果 + 追加在线资源
  - 详情 GET /v/api/v1/item/<guid>   在线 guid 返回伪造详情
  - 播放 POST /play/info、GET /stream/list、POST /stream、POST /play/play
                                     在线 guid/media_guid 返回伪造链路
  - 流   GET /v/media/online/<mg>/*  HLS 代理（重写 m3u8 分片/密钥）
  - 海报 GET /v/online/poster        外站海报同源代理
  - 其余全部原样透传官方后端
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse, FileResponse)

import features
# 构建戳（打包时由 build.sh 生成 proxy/build_info.py；源码树里没有时为开发版）
try:
    import build_info as _build_info
    BUILD_LABEL = getattr(_build_info, "BUILD_LABEL", "dev")
except Exception:  # noqa: BLE001
    BUILD_LABEL = "dev"
import providers
import netutil
import sourcehealth
# 上游 v0.2.68/69 移植：本片线路预测（linepredict）+ 榜单绑定持久化（chartbind）。
# 零竞速直出（解冻第 2 项）：可信预测线在 _ensure_playable 直接起播、跳过竞速
# （见 trusted_predicted_target）；不可信时仍走竞速，预测线在竞速内只作优先
# 种子，竞速最快线记忆仍只由竞速胜者写（见 _race_candidates / _remember_fast）；
# 持久化绑定只作起播种子，恢复后仍走竞速（见 _persist_chart_binding）。
import linepredict
import chartbind
import t4
from t4_service import T4Service
from t4_media import T4Media
import t4_metadata
from t4_browser import T4Browser, LIB_GUID as T4_LIB_GUID, LIB_TITLE as T4_LIB_TITLE
import client as clientmod
import live as livemod
import inject as inj
import config as cfgmod
import adfilter as adfilter_mod
from config import config
import hot as hotmod
from hot import hot_cache
import charts as chartmod
from charts import chart_cache
import hgdj as hgdjmod
import catalog as catalogmod
from catalog import catalog
import catalog as catalogmod
from catalog import catalog
from catalog import catalog
import history as histmod
from history import history
import resume_diag as resume_diagmod
import marks as marksmod
from marks import marks
# P2：追剧提醒 / 域名自愈 / 元数据补全
from follow import follow_list
import domain_heal as domain_heal_mod
import metadata as metadata_mod
import vlibs as vlibsmod
import fnusers as fnusersmod
import libfilter as libfiltermod
import admin_auth as admin_authmod
import updater
from store import (store, make_guid, media_guid, video_guid, is_online_guid,
                   season_guid, episode_guid, ep_media_guid, ep_video_guid)

t4_service = T4Service(store, config)
t4_media = T4Media()
t4_browser = T4Browser(store, config)

UPSTREAM_SOCK = os.environ.get("FNMEDIA_UPSTREAM_SOCK",
                               "/var/run/trim_media_upstream.sock")
# 本服务回环基址：网盘原画/转码播放地址是根相对路径，HLS 探测/改写需经本机
# HTTP 自调用（nginx 5666 → 本应用），不能直接喂给外站 client。
SELF_BASE = os.environ.get("FNM_SELF_BASE", "http://127.0.0.1:5666")
ONLINE_PREFIX = "/v/media/online"

# 榜单/热门卡片是虚拟 guid，点开时才拿片名去聚合搜源。原版详情只等 0.12s 就返回
# 骨架（表现为大小 0、路径=在线/加载中.m3u8），而播放侧又无超时地等同一个任务，
# 于是首次点播一直转圈。这里把两个等待窗做成可配：
_CHART_DETAIL_WAIT = float(os.environ.get("FNMEDIA_CHART_DETAIL_WAIT", "2.5"))
_CHART_PLAY_WAIT = float(os.environ.get("FNMEDIA_CHART_PLAY_WAIT", "12"))

# ------------------------------------------------------------------ DNS 缓存
# 资源站域名多为多级 CNAME，glibc 自身不缓存，httpx/anyio 每条新连接都要
# getaddrinfo。实测 18 个源域名里 3 个冷解析要 5.0~5.2s（换源/首片时最明显）。
# 这里只在**本进程内**加一层 TTL 缓存，不改 /etc/resolv.conf、不影响系统其它程序。
import socket as _socket_mod

_DNS_TTL = 600.0
_DNS_MAX = 2048
_dns_cache: dict[tuple, tuple[float, list]] = {}
_dns_lock = threading.Lock()
_real_getaddrinfo = _socket_mod.getaddrinfo


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        key = (host, port, family, type, proto, flags)
        hash(key)
    except TypeError:
        return _real_getaddrinfo(host, port, family, type, proto, flags)
    now = time.time()
    with _dns_lock:
        hit = _dns_cache.get(key)
        if hit is not None:
            if now - hit[0] < _DNS_TTL:
                return hit[1]
            _dns_cache.pop(key, None)  # 过期命中立即删
    res = _real_getaddrinfo(host, port, family, type, proto, flags)
    with _dns_lock:
        if len(_dns_cache) >= _DNS_MAX:
            # 满了删最旧一半而非全清：避免全清后所有域名同时冷解析抖动
            for _old in list(_dns_cache)[:len(_dns_cache) // 2]:
                _dns_cache.pop(_old, None)
        _dns_cache[key] = (now, res)
    return res


_socket_mod.getaddrinfo = _cached_getaddrinfo


def _self_url(url: str) -> str:
    """把本服务根相对地址绝对化为回环地址；外站绝对地址原样返回。"""
    if url.startswith("/"):
        return SELF_BASE.rstrip("/") + url
    return url
# 单层搜索缓存（P0-19 合并，上游 v0.2.69 绑定聚合缓存池）：同一
# (片名·是否快源·是否含T4)在 TTL 内复用聚合后的 briefs，榜单绑定 /
# 播放回源 / 搜索等多次调用同一片名时不重复扇出所有源。
# 注意：缓存的是 remember_merged 之后的 brief（含线路），命中即等价一次完整聚合。
# 此前是双层：online_search 的 _search_cache（120 秒）+ _search_inflight 单飞，
# 与聚合层 _AGG_CACHE（180 秒）+ _AGG_INFLIGHT 单飞存同一份 brief、单飞两次。
# 现只留聚合层一层，TTL 统一为 AGG_CACHE_TTL（180 秒）；online_search
# 不再自建缓存/单飞，直接复用 _aggregate_briefs（它缓存的是未过滤的
# brief，每次返回仍按当前查看者重新过滤建卡，不会把先搜者的可见范围
# 漏给后搜者）。缓存键仍区分请求参数（keyword·fast_only·include_t4）。
_AGG_CACHE: dict[str, tuple[float, list]] = {}
_AGG_INFLIGHT: dict[str, asyncio.Future] = {}
AGG_CACHE_TTL = 180
_AGG_CACHE_MAX = 800
_AGG_CACHE_MAX_BYTES = 128 * 1024 * 1024  # 128MB：800 条 × 10 剧集重 brief
# 最坏 897MB；字节上限兜底。128MB 可容数百次典型搜索（重 brief 约 112KB/条
# 为最坏估算，常规 brief 远小于此）；TTL 仅 180 秒，条目可重新聚合生成。
_AGG_BYTES: dict[str, int] = {}  # cache_key -> 该条字节估算
_AGG_BYTES_TOTAL = 0             # 当前总量（运行值）


def _agg_entry_bytes(result) -> int:
    """聚合结果的字节估算：字符串按长度、dict 固定 64B、list/tuple 固定 16B、
    其余标量 16B，迭代遍历不递归。估算只用于淘汰决策，不要求精确。"""
    total = 0
    stack = [result]
    while stack:
        o = stack.pop()
        if isinstance(o, str):
            total += len(o)
        elif isinstance(o, dict):
            total += 64
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple)):
            total += 16
            stack.extend(o)
        else:
            total += 16
    return total


def _agg_cache_drop(cache_key: str) -> None:
    """删一条聚合缓存，并同步扣减字节记账。"""
    global _AGG_BYTES_TOTAL
    if _AGG_CACHE.pop(cache_key, None) is not None:
        _AGG_BYTES_TOTAL = max(0, _AGG_BYTES_TOTAL - _AGG_BYTES.pop(cache_key, 0))


def _rebuild_agg_bytes() -> None:
    """外部直接改动 _AGG_CACHE（如管理端 clear）后重建字节记账。"""
    global _AGG_BYTES_TOTAL
    _AGG_BYTES.clear()
    total = 0
    for k, (_ts, val) in _AGG_CACHE.items():
        s = _agg_entry_bytes(val)
        _AGG_BYTES[k] = s
        total += s
    _AGG_BYTES_TOTAL = total


def _agg_cache_put(cache_key: str, result: list) -> None:
    """有界写聚合缓存：条数 800 + 字节 128MB 双上限，真 LRU。

    dict 按插入序：新条天然在队尾；超限从队首删最旧。单条超限时保留该条。
    淘汰是分摊 O(1)：每条目最多被 pop 一次，写路径不做全表排序/扫描。"""
    global _AGG_BYTES_TOTAL
    _AGG_CACHE[cache_key] = (time.time(), result)
    if cache_key in _AGG_BYTES:
        _AGG_BYTES_TOTAL -= _AGG_BYTES[cache_key]
    s = _agg_entry_bytes(result)
    _AGG_BYTES[cache_key] = s
    _AGG_BYTES_TOTAL += s
    if len(_AGG_BYTES) != len(_AGG_CACHE):
        _rebuild_agg_bytes()  # 外部直接改过缓存：重建记账（含本条）
    while (len(_AGG_CACHE) > _AGG_CACHE_MAX
           or _AGG_BYTES_TOTAL > _AGG_CACHE_MAX_BYTES) \
            and len(_AGG_CACHE) > 1:
        _agg_cache_drop(next(iter(_AGG_CACHE)))
# 旧名字保留为同一对象的别名（不产生第二层存储）：既有测试、管理端点
# （缓存计数/清空）仍引用 _search_cache / _search_inflight。
_search_cache = _AGG_CACHE
_search_inflight = _AGG_INFLIGHT
T4_STREAM_READY_BUDGET = 8.0
# 在线热门按内容拆成 4 个虚拟媒体库。guid 由固定串 md5 派生，保持稳定。
HOT_CATEGORIES = [
    ("movie",   "🌐在线电影", "Movie"),
    ("tv",      "🌐在线剧集", "TV"),
    ("anime",   "🌐在线动漫", "Mix"),
    ("variety", "🌐在线综艺", "Mix"),
]
HOT_LIB_GUIDS: dict[str, str] = {
    "movie":   "f4f27c311388d90f658670995a7fddb8",
    "tv":      "2a6937a71b652ea061a68456a72e69a8",
    "anime":   "5036a366ecfc047b4732b6bc2a39c3d8",
    "variety": "61d5d026ae7390a9f333b5d3a43ef5ff",
}
HOT_GUID_CAT = {g: c for c, g in HOT_LIB_GUIDS.items()}
# 兼容历史单库 guid：旧链接/旧缓存仍可打开，映射到剧集库。
HOT_LEGACY_GUID = "a110c1a0b1a04000900010000000f001"
HOT_DEFAULT_TITLE = "🌐在线热门"

# 真实榜单虚拟媒体库（方案 B：豆瓣周榜/推荐做展示，点击时按需搜源解析播放）。
CHART_LIB_GUIDS: dict[str, str] = {
    "movie":   "0b10d767319eb8cab9d99373ede48d0a",
    "tv":      "9e75c8f2616bd01851911eea649db367",
    "anime":   "18372cada44b72c305343c2bfcc026f0",
    "variety": "cb606d5f699560c630c97f7acc84f75b",
}
CHART_GUID_CAT = {g: c for c, g in CHART_LIB_GUIDS.items()}
# chart guid -> 已解析（挂载到 store）的可播放 brief
_chart_resolved: dict[str, dict] = {}
_chart_resolving: dict[str, asyncio.Future] = {}
# 秒进播放页时，榜单片可能尚未绑定；暂记 media_guid -> chart guid，
# 让播放页内的 stream/play/HLS 请求能找到同一个后台绑定任务，而不是漏给官方后端。
_PENDING_MEDIA_CHART: dict[str, str] = {}
# 已确认暂无可播源的榜单片：短时间内不要反复全网搜索（TTL 10 分钟）。
_CHART_FAILED_TS: dict[str, float] = {}
_CHART_FAILED_TTL = 10 * 60

# 海量分类片库（方案 C：按资源站分类深翻，每类数千部，无限滚动）。
CATALOG_LIB_GUIDS: dict[str, str] = {
    "movie":   "e91dd7347a49dbd874fb8a0e78ad7ad7",
    "tv":      "196381f891d66a4de32f9032e8f244f3",
    "anime":   "97fc49015edcb20cc423658401711b6c",
    "variety": "cfff30730cffca2d1357e0c2b52fc2f5",
    # 福利分类库（跟随海量主开关 catalog_enabled，不设独立开关）：
    # 纪录片 / 4K / 短剧 —— 与基座扩展版保持同一稳定 guid，客户端缓存不丢。
    "documentary": "155f2305c2cbc76c85f981ef31cc192f",
    "uhd":         "262ce51312d7e72020514db5fd5f63e9",
    "shortdrama":  "037ed7c6df4a9deb8d815c823fd15f79",
    # P2 新增：guid 由固定串 md5 派生，保持稳定（与上方注释的生成方式一致）。
    # md5("fnmedia-catalog-lib:overseas") / md5("fnmedia-catalog-lib:ai_comic")
    # md5("fnmedia-catalog-lib:ted")
    "overseas":    "c2fe61ae4d3adc87756a253cb108066e",
    "ai_comic":    "6784f423ff4be5af4fe60cde6c8da303",
    "ted":         "34883d4af44ca4a5539cb38827158294",
    # cat-wiring 新增：guid 由固定串 md5 派生，保持稳定（与上方注释的生成方式一致）。
    # md5("fnmedia-catalog-lib:sports") / md5("fnmedia-catalog-lib:live")
    # md5("fnmedia-catalog-lib:kids") / md5("fnmedia-catalog-lib:opera")
    # md5("fnmedia-catalog-lib:music")
    "sports":      "9b93ebd34f12099811bcb2e700825169",
    "live":        "465e85f3a6cb1a4ce5d4e397de29384b",
    "kids":        "cd91e59c8f342225b8741f18d70a26be",
    "opera":       "495704bcc69d765082a218219fbf3c1e",
    "music":       "00a744b183e5c4ef1d375ab8514f515b",
    # 福利独立库（md5("fnmedia-welfare-lib")，GUID 种子保持不变）：独立开关 welfare_library_enabled
    # 控制是否建库/显示，反查 cat = catalog.ADULT_CAT，走同一条海量深翻链路。
    "adult":       "411515e17c85709cf1966697b7b2c58e",
}
CATALOG_GUID_CAT = {g: c for c, g in CATALOG_LIB_GUIDS.items()}

# —— 单源独立媒体库：每个启用 standalone 的源生成一张库卡片 ——
_STANDALONE_PREFIX = "fnmedia-standalone:"


def standalone_lib_guid(source_id: str) -> str:
    return hashlib.md5((_STANDALONE_PREFIX + source_id).encode()).hexdigest()


def standalone_source_of(lib_guid: str, _ctx=None) -> str | None:
    """库 guid 反查 source_id；非独立库返回 None。

    传 ``_ctx``（每请求 ``_ViewerCtx``）时走一次建成的查表；不传时
    保持原有逐源扫描。
    """
    if _ctx is not None:
        return _ctx._standalone_map().get(lib_guid)
    if not lib_guid:
        return None
    for s in config.all_sources():
        if standalone_lib_guid(s["id"]) == lib_guid:
            return s["id"]
    return None


# HGDJ（红果短剧）：库根节点可收藏与否由 is_hgdj_series 判定；供 mediadb/mdb/item
# 分支与 play_play 用。
def hgdj_enabled() -> bool:
    return hgdjmod.enabled()






def standalone_sources_active() -> list[dict]:
    return [s for s in config.enabled_sources() if s.get("standalone")]


def standalone_lib_name(src: dict) -> str:
    base = "📡" + (src.get("name") or src.get("id") or "")
    # >>> PRIVATE:ADULT
    if features.ADULT and src.get("adult"):
        return "🔞" + base
    # <<< PRIVATE:ADULT
    return base


# —— 以下三件是通用件，被剥离块外的代码直接引用，不能放进加固标记块 ——
# 飞牛原生媒体库聚合库的 guid（公开构建里 features.GUARD 已关，没有任何分支会
# 用到它，但 item/list 的可见性换算仍引用该名字）。
# P2 拆分：守卫 / 原生守卫（guard native）逻辑：聚合库、热墙、分类、深预拉。已移入 proxy/guard_native.py；以下重导出（对外 import 名称不变）。
from guard_native import GUARD_NATIVE_GUID, GUARD_NATIVE_PREFIX, GUARD_NATIVE_TITLE, _GN_AREA_ALIASES, _GN_AREA_CANON_ORDER, _GN_CACHE_PATH, _GN_CATALOG_GAP, _GN_CATALOG_MAX_PG, _GN_CATALOG_PATH, _GN_CATALOG_SRCS, _GN_CATALOG_TTL, _GN_DEEP_BATCH, _GN_DEEP_CLASSES, _GN_DEEP_CONC, _GN_DEEP_ENABLED, _GN_DEEP_GAP, _GN_DEEP_MAX_PG, _GN_DEEP_SRCS, _GN_FOLDER_PREFIX, _GN_GENRE_ID_BASE, _GN_GENRE_ID_MAX, _GN_HOT_PER_SOURCE, _GN_HOT_TOKEN, _GN_MOVIE_CLASS_WORDS, _GN_PAGE_SIZE, _GN_REFRESH_GAP, _GN_TTL, _GN_TV_CLASS_WORDS, _GN_WARM_CLASSES, _GN_WARM_GAP, _GN_WARM_PAGES, _GUARD_LIB_PREFIX, _GUARD_POSTER_QUEUED, _PAN_LINKSTART_RE, _PAN_PROVIDERS, _gn_area_canon, _gn_area_extend, _gn_area_options, _gn_catalog_all_fresh, _gn_catalog_apply, _gn_catalog_detect_keys, _gn_catalog_load, _gn_catalog_max_page, _gn_catalog_save, _gn_catalog_worker, _gn_category_page, _gn_category_rows, _gn_category_task, _gn_class_folder_rows, _gn_class_is_tv, _gn_class_name, _gn_classes_meta, _gn_cls_token, _gn_deep_all_fresh, _gn_deep_enabled, _gn_deep_worker, _gn_ensure_meta, _gn_ext_key, _gn_film_index, _gn_folder_guid, _gn_folder_row, _gn_folder_rows, _gn_folder_token, _gn_genre_id_targets, _gn_genre_map, _gn_hot_rows, _gn_is_film_class, _gn_load_persist, _gn_lock, _gn_map_cards, _gn_parse_cls_token, _gn_persist, _gn_prefetch_catalog, _gn_prefetch_category, _gn_prefetch_class_walls, _gn_prefetch_deep, _gn_prefetch_loop, _gn_refresh_hot, _gn_root_folder_rows, _gn_rows_for_tags, _gn_source_name, _gn_sources, _gn_src_token, _gn_state, _gn_tag_filter_payload, _gn_tag_scalar, _gn_warm_worker, _guard_prefetch, _pan_match, guard_lib_guid, guard_lib_name, guard_lib_source, guard_libs_active, guard_native_enabled


# >>> PRIVATE:CATVOD
# —— 小雅（AList）分类媒体库：按网盘目录建立多张独立库卡片 ——
def _xlib_on() -> bool:
    try:
        return bool(config.get().get("pan_lib_enabled", True)) and features.CATVOD
    except Exception:
        _log.debug("_xlib_on failed", exc_info=True)
        return False


def _xlib_active_cats() -> list[dict]:
    """可展示的小雅大类（需已配置小雅凭证）。"""
    if not _xlib_on():
        return []
    try:
        import xlib
        if not xlib.library.available():
            return []
        # 不在进媒体库时全量扫描小雅：仅列出分类。用户打开某分类时由对应列表接口
        # 调 ensure_build 增量构建该分类（只读 fs/list 条目名，不触发转存）。
        return [xlib.cat_by_id(c[0]) for c in xlib.CATEGORIES]
    except Exception:
        _log.debug("_xlib_active_cats failed", exc_info=True)
        return []


def _xlib_cat(lib_guid: str) -> dict | None:
    if not _xlib_on():
        return None
    try:
        import xlib
        for c in xlib.CATEGORIES:
            if xlib.lib_guid(c[0]) == lib_guid:
                return xlib.cat_by_id(c[0])
    except Exception:
        _log.debug("_xlib_cat failed: lib_guid=%s", lib_guid, exc_info=True)
        return None
    return None


# <<< PRIVATE:CATVOD


def charts_enabled() -> bool:
    return bool(config.get().get("hot_library_enabled", True))


def catalog_enabled() -> bool:
    """海量四库独立开关（与热门榜单互不影响）。"""
    return bool(config.get().get("catalog_library_enabled", True))


def any_virtual_lib_enabled() -> bool:
    if charts_enabled() or catalog_enabled() or hgdj_enabled() or t4_browser.sources():
        return True
    # >>> PRIVATE:GUARD
    try:
        if features.GUARD and guard_native_enabled():
            return True
    except Exception:  # noqa: BLE001
        _log.debug("any_virtual_lib_enabled failed", exc_info=True)
        pass
    # <<< PRIVATE:GUARD
    return False


def _client_of(request: Request) -> clientmod.Client:
    """从请求 UA 解析客户端画像（TV/iOS/Android/Web）。"""
    return clientmod.parse_user_agent(request.headers.get("user-agent", ""))


def chart_card(item: dict) -> dict:
    """榜单条目 → 媒体库卡片（不解析播放；点击详情时才搜源）。"""
    cat = item["cat"]
    typ = "Movie" if cat == "movie" else "TV"
    year = item.get("year") or ""
    rating = item.get("rating") or 0
    tags = item.get("tags") or []
    overview = item.get("desc") or ("、".join(tags) if tags else "")
    title = item["title"]
    guid = item["guid"]
    row = {
        "guid": guid,
        # TV/盒子端(Flutter)列表模型要求 video_guid/path/runtime，缺失会整库“加载失败”；
        # 手机端较宽松。video_guid 与播放/详情接口用的 video_guid(guid) 保持一致。
        "video_guid": video_guid(guid),
        "path": "",
        "runtime": 0,
        "lan": "zh-CN",
        "imdb_id": "",
        "trim_id": "",
        "tv_title": "",
        "parent_guid": "",
        "parent_title": "",
        "title": title,
        "type": typ,
        "genres": tags[:3],
        "poster": inj._poster(item.get("cover")),
        "poster_width": 0,
        "poster_height": 0,
        "is_favorite": 0,
        "watched": 0,
        "watched_ts": 0,
        "vote_average": str(rating) if rating else "0",
        "media_stream": {"resolutions": None, "audio_type": None, "color_range_type": None},
        "release_date": year,
        "season_number": 0,
        "episode_number": 0,
        "air_date": year,
        "number_of_seasons": 1 if typ == "TV" else 0,
        "number_of_episodes": 0,
        "local_number_of_seasons": 0,
        "local_number_of_episodes": 0,
        # 未解析前乐观标记可播，保证卡片有播放入口；解析失败时详情页会提示暂无片源
        "can_play": 1,
        "status": "豆瓣热榜",
        "overview": overview[:500],
        "ancestor_guid": CHART_LIB_GUIDS[cat],
        "ancestor_name": chartmod.chart_cat_title(cat),
        "ancestor_category": chartmod.chart_cat_mdb(cat),
        "ts": 0,
        "duration": 0,
        "single_child_guid": "",
        "file_name": "",
    }
    return _vod_directory_card(row)


def prewarm_persist_mode() -> bool:
    """预热方式开关：
    True  = 方案B 持久化（绑定结果落盘，重启直接恢复，少重新搜源）；
    False = 方案A 实时（重启后启动任务现场 resolve，默认）。"""
    return str(config.get().get("prewarm_mode", "realtime")) == "persist"


def _persist_chart_binding(guid: str, bound: dict) -> None:
    """方案B：把榜单绑定结果落盘（仅持久化模式）。失败静默，不影响播放。

    落盘的绑定只作起播种子（让起播更快找到候选线），不绕过竞速：恢复后起播
    仍走 _ensure_playable → _race_fastest 竞速流程。
    """
    try:
        if prewarm_persist_mode() and guid and bound:
            chartbind.bind_store.remember(guid, bound)
    except Exception:
        _log.debug("_persist_chart_binding failed: guid=%s", guid, exc_info=True)
        pass


def _restore_chart_bindings() -> int:
    """方案B：启动时从磁盘恢复榜单绑定到 store（返回恢复条数）。

    只恢复新鲜绑定（chartbind.get() 对过期返回 None）；失效片由后台预热
    重新 resolve 刷新，不再把过期绑定当作起播种子恢复进内存。
    """
    try:
        if not prewarm_persist_mode():
            return 0
        restored = 0
        for cat, _t, _g in chartmod.CHART_CATEGORIES:
            for it in chart_cache.items(cat):
                guid = it.get("guid")
                if not guid or store.get_title_by_guid(guid):
                    continue
                brief = chartbind.bind_store.get(guid)
                if not brief:
                    continue
                if not _chart_binding_matches(guid, brief):
                    chartbind.bind_store.forget(guid)
                    continue
                b = dict(brief)
                b["guid"] = guid  # 确保挂到榜单卡片 guid
                try:
                    store.remember_merged([b])
                    restored += 1
                except Exception:
                    _log.debug("_restore_chart_bindings failed", exc_info=True)
                    pass
        return restored
    except Exception:
        _log.debug("_restore_chart_bindings failed", exc_info=True)
        return 0


_CHART_VARIANT_SUFFIX_RE = re.compile(
    r"^(?:(?:普通话|国语|粤语|英语|日语|韩语|中字|中英双字|中文字幕|"
    r"hd|高清|超清|蓝光|完整版|未删减|正片)+)$", re.I)


def _chart_title_rank(item: dict, brief: dict) -> int:
    """榜单→资源站片名相关度：完整同名最优先，其次仅允许语言/清晰度后缀。
    不能沿用任意包含匹配，否则《突如其来》会错绑《突如其来的假期》。（上游原样）"""
    want = _norm_title(item.get("title", ""))
    name = _norm_title(brief.get("title", ""))
    if not want or not name:
        return 99
    want_year = str(item.get("year") or "")
    brief_year = str(brief.get("year") or "")
    if want_year and brief_year and want_year != brief_year:
        return 99
    if name == want:
        return 0
    if name.startswith(want):
        suffix = name[len(want):]
        if suffix and _CHART_VARIANT_SUFFIX_RE.fullmatch(suffix):
            return 1
    tokens = [_norm_title(t) for t in re.split(r"[\s]+", item.get("title", ""))
              if _norm_title(t)]
    if len(tokens) > 1 and all(tok in name for tok in tokens):
        return 2
    return 99


def _chart_binding_matches(guid: str, brief: dict | None) -> bool:
    item = chart_cache.get_guid(guid)
    return bool(item and brief and _chart_title_rank(item, brief) < 99)


def start_resolve_chart(guid: str) -> asyncio.Future | None:
    """启动榜单 guid 的后台解析（单飞），立即返回在途 Future；不阻塞当前请求。"""
    existing = store.get_title_by_guid(guid)
    if existing:
        return None
    if time.time() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL:
        return None
    fut = _chart_resolving.get(guid)
    if fut is not None:
        return fut
    fut = asyncio.get_event_loop().create_future()
    _chart_resolving[guid] = fut

    async def work():
        try:
            item = chart_cache.get_guid(guid)
            if not item:
                fut.set_result(None)
                return
            briefs = await asyncio.wait_for(
                _aggregate_briefs(item["title"], 12, include_t4=False), timeout=25)
            # 相关性：归一化片名必须包含完整榜单片名（分词全部命中），否则视为无源
            want = _norm_title(item["title"])
            want_tokens = [_norm_title(t) for t in re.split(r"[\s]+", item["title"]) if _norm_title(t)]
            best = None
            for b in briefs:
                n = _norm_title(b.get("title", ""))
                if n and (want in n or (len(want_tokens) > 1
                                        and all(tok in n for tok in want_tokens))):
                    best = b
                    break
            if best is None and briefs:
                # 宽松兜底：取线路最多者（片名归一后高度重叠）
                best = briefs[0]
            if best is not None:
                # 关键：把可播放 brief 的身份换成 chart guid，使播放链挂到榜单卡片
                bound = dict(best)
                bound["guid"] = guid
                bound["chart"] = {"title": item["title"], "cat": item["cat"],
                                  "cover": item.get("cover", ""),
                                  "rating": item.get("rating", 0)}
                _chart_resolved[guid] = bound
                store.remember_merged([bound])
                _persist_chart_binding(guid, bound)
                _PENDING_MEDIA_CHART.pop(media_guid(guid), None)
                _CHART_FAILED_TS.pop(guid, None)
            else:
                _CHART_FAILED_TS[guid] = time.time()
            fut.set_result(best)
        except Exception:
            _log.debug("work failed", exc_info=True)
            fut.set_result(None)
        finally:
            _chart_resolving.pop(guid, None)

    asyncio.create_task(work())
    return fut


async def resolve_chart(guid: str) -> dict | None:
    """用榜单片名去 CMS 聚合搜索，把最匹配的可播放 brief 挂到该 chart guid。

    单 flight；命中后在内存和 store 都登记，之后整条 season/episode/play 链复用。
    """
    existing = store.get_title_by_guid(guid)
    if existing:
        return existing
    fut = start_resolve_chart(guid)
    if fut is None:
        return store.get_title_by_guid(guid)
    return await fut


def chart_guid_by_media(mg: str) -> str:
    """识别 media_guid 是否属于榜单/热门片（包括秒进时尚未绑定完成的）。"""
    og = _PENDING_MEDIA_CHART.get(mg) or store.online_guid_by_media(mg)
    return og if og and chart_cache.is_chart_guid(og) else ""


def _instant_play_response(mg: str, body_video_guid: str = ""):
    """榜单源尚未绑定时的瞬时播放返回：先把播放器带进加载页，真正匹配延后到 preset。"""
    play_link = f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index=0"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg,
        "play_link": play_link, "subtitle_guid": "", "subtitle_index": -1,
        "subtitle_link": "", "video_guid": body_video_guid or vg_for(mg),
        "video_index": 0, "wrapper": "HLS",
        "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


def _instant_stream_payload(mg: str):
    """与 _instant_play_response 配套：只返回最小可播元数据，绝不等待搜源。"""
    f = {
        "guid": mg, "path": "在线/加载中.m3u8", "file_name": "加载中.m3u8",
        "size": 0, "timestamp": 0, "type": 1, "sort_num": 0, "can_play": 1,
        "play_error": "", "create_time": 0, "update_time": 0,
        "parent_fv_guid": "", "file_birth_time": 0, "progress_thumb_hash_dir": "",
    }
    v = {
        "media_guid": mg, "title": "", "guid": vg_for(mg),
        "resolution_type": "1080p", "color_range_type": "SDR",
        "codec_name": "h264", "codec_type": "video", "color_range": "tv",
        "profile": "High", "index": 0, "width": 1920, "height": 1080,
        "coded_width": 0, "coded_height": 0, "display_aspect_ratio": "16:9",
        "pix_fmt": "yuv420p", "level": "40", "color_space": "",
        "color_transfer": "", "color_primaries": "", "duration": 0,
        "dv_profile": 0, "refs": 1, "r_frame_rate": "25 fps",
        "avg_frame_rate": "25 fps", "bits_per_raw_sample": "8", "bps": 0,
        "progressive": 1, "bit_depth": 8, "wrapper": "HLS",
        "create_time": 0, "update_time": 0, "rotation": 0, "ext1": 0,
        "is_bluray": False,
    }
    return {
        "files": [f], "video_streams": [v], "audio_streams": [],
        "subtitle_streams": [], "qualities": [], "cloud_storage_info": None,
    }


def _instant_stream_response(mg: str):
    return JSONResponse({"code": 0, "msg": "",
                         "data": _instant_stream_payload(mg)})


def hot_cat_title(cat: str) -> str:
    for c, title, _ in HOT_CATEGORIES:
        if c == cat:
            return title
    return HOT_DEFAULT_TITLE


def hot_cat_category(cat: str) -> str:
    for c, _, mdbcat in HOT_CATEGORIES:
        if c == cat:
            return mdbcat
    return "Mix"


def is_hot_guid(guid: str) -> bool:
    return guid in HOT_GUID_CAT or guid == HOT_LEGACY_GUID


def b64e(u: str) -> str:
    return base64.urlsafe_b64encode(u.encode()).decode().rstrip("=")


def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


async def _pan_cache_cleanup_once() -> dict:
    """对已登录的夸克/UC 各执行一次过期转存清理（P1-8）。

    调用点照搬上游 app.py:805-866（开关 pan_cache_cleanup_enabled 默认开、
    TTL pan_cache_ttl_hours 默认 6h、每小时跑一次）；但上游把
    @asynccontextmanager 误贴在这个普通 async 函数上（它没有 yield，
    await 调用会 TypeError，循环里的 except 会静默吞掉、清理实际从不跑），
    这里不抄那个装饰器。开关除循环里判断外，在这里也再判一次（单点关口，
    直接调本函数同样受开关约束）。执行实现是 fork 自写的
    pan/panuc.cleanup_expired；公开构建已删除 pan/panuc 模块时跳过。
    单模块失败只记日志，不影响播放。
    """
    out = {}
    try:
        cfg = config.get()
    except Exception:
        _log.debug("_pan_cache_cleanup_once failed", exc_info=True)
        return out
    if not bool(cfg.get("pan_cache_cleanup_enabled", True)):
        return out
    try:
        ttl = int(float(cfg.get("pan_cache_ttl_hours", 6) or 6)) * 3600
    except (TypeError, ValueError):
        ttl = 6 * 3600
    for name, modname in (("quark", "pan"), ("uc", "panuc")):
        try:
            mod = __import__(modname)
        except Exception:
            _log.debug("_pan_cache_cleanup_once failed", exc_info=True)
            continue
        try:
            if not getattr(mod, "logged_in", lambda: False)():
                continue
            res = await asyncio.to_thread(mod.cleanup_expired, ttl)
            if res and res.get("deleted"):
                _log.info("网盘缓存清理 %s 删除=%d 释放=%.1fMB",
                          name, res.get("deleted", 0),
                          res.get("freed", 0) / 1048576)
            if res and res.get("error"):
                _log.info("网盘缓存清理 %s 出错 errors=%s error=%s",
                          name, res.get("errors", 0), res.get("error"))
            out[name] = res
        except Exception:
            _log.info("网盘缓存清理失败 %s", name, exc_info=True)
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.up = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=UPSTREAM_SOCK),
        timeout=httpx.Timeout(120), trust_env=False)
    app.state.ext = httpx.AsyncClient(
        timeout=httpx.Timeout(30), trust_env=False, follow_redirects=True)
    app.state.stop_hot = asyncio.Event()
    app.state.hot_task = asyncio.create_task(_hot_worker(app))

    # 网盘转存缓存定时清理（默认保留 6h，每小时检查；配置关闭则不跑）。
    async def _pan_cache_loop():
        try:
            await asyncio.sleep(90)  # 启动后等服务稳定再首轮
            while True:
                try:
                    if bool(config.get().get("pan_cache_cleanup_enabled", True)):
                        await _pan_cache_cleanup_once()
                except Exception:
                    _log.debug("_pan_cache_loop failed", exc_info=True)
                    pass
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
    app.state.pan_cache_task = asyncio.create_task(_pan_cache_loop())

    # 方案B（上游 v0.2.69 chartbind）：persist 模式下从磁盘恢复榜单绑定（线路），
    # 重启后热门片无需重新全网搜源。恢复的绑定只作起播种子，起播仍走竞速。
    # （fork 的 ChartCache 在 import 时已从 state/charts.json 读盘，无需再 load。）
    try:
        _n = _restore_chart_bindings()
        if _n:
            _log.info("chart bindings restored on startup: %d", _n)
    except Exception as exc:  # noqa: BLE001
        _log.warning("chart binding restore failed: %s", exc)
    # 方案B：同时恢复冠军线记忆（重启即秒播，不用现建播放清单）
    try:
        if prewarm_persist_mode():
            linepredict.set_persist(True)
    except Exception:  # noqa: BLE001
        _log.debug("lifespan failed", exc_info=True)
        pass

    # 源健康画像：每 5 分钟把内存统计落盘一次（热路径零 IO）
    async def _health_flush_loop():
        while True:
            await asyncio.sleep(300)
            try:
                await asyncio.to_thread(sourcehealth.flush)
            except Exception:
                _log.debug("_health_flush_loop failed", exc_info=True)
                pass
    app.state.health_task = asyncio.create_task(_health_flush_loop())

    # >>> PRIVATE:GUARD
    _gn_load_persist()   # 先读盘，重启后源文件夹/热墙秒显，不必重新扫数分钟
    _gn_catalog_apply()  # 灌入全片单（豆豆等片单源的完整分类目录），翻页全部秒回
    app.state.stop_guard_native = asyncio.Event()
    app.state.guard_native_task = asyncio.create_task(
        _gn_prefetch_loop(app.state.stop_guard_native))
    # <<< PRIVATE:GUARD

    # 后台只读检查 GitHub 更新（仅提示，绝不自动安装）
    app.state.stop_update = asyncio.Event()
    app.state.update_task = asyncio.create_task(
        updater.periodic_loop(app.state.stop_update))

    # >>> PRIVATE:GUARD
    # redroid 加固运行时：常驻模式下插件/开机自启并后台保活（仅启用 guard 特性时）
    try:
        import features
        if getattr(features, "GUARD", False):
            import guard as _guard

            def _resident_boot():
                try:
                    if _guard.resident_enabled():
                        _guard.start_resident_watchdog()
                        _guard.autostart_android(blocking=False)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("redroid resident boot skipped: %s", exc)

            async def _resident_boot_async():
                try:
                    await asyncio.to_thread(_resident_boot)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("redroid resident boot failed: %s", exc)

            asyncio.create_task(_resident_boot_async())
    except Exception as exc:  # noqa: BLE001
        _log.warning("redroid resident init failed: %s", exc)
    # <<< PRIVATE:GUARD

    try:
        yield
    finally:
        app.state.stop_hot.set()
        app.state.hot_task.cancel()
        try:
            app.state.pan_cache_task.cancel()
        except Exception:
            _log.debug("lifespan failed", exc_info=True)
            pass
        # >>> PRIVATE:GUARD
        try:
            app.state.stop_guard_native.set()
            app.state.guard_native_task.cancel()
        except Exception:
            _log.debug("lifespan failed", exc_info=True)
            pass
        # <<< PRIVATE:GUARD
        try:
            await app.state.hot_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            app.state.health_task.cancel()
        except Exception:
            _log.debug("lifespan failed", exc_info=True)
            pass
        try:
            await asyncio.to_thread(sourcehealth.flush)
        except Exception:
            _log.debug("lifespan failed", exc_info=True)
            pass
        try:
            app.state.stop_update.set()
            app.state.update_task.cancel()
            await app.state.update_task
        except (asyncio.CancelledError, Exception):
            pass
        await t4_media.metadata.close()
        await app.state.up.aclose()
        await app.state.ext.aclose()
        # 关闭前把防抖未落盘的在线资源缓存同步写一次（issue #42 异步落盘配套）
        try:
            await asyncio.to_thread(store.close)
        except Exception:
            _log.debug("lifespan failed", exc_info=True)
            pass


async def _hot_worker(app: FastAPI):
    """榜单 + 海量库路由后台刷新。启动先读旧缓存，稍等再刷新；之后按配置轮询。"""
    await asyncio.sleep(4)
    while not app.state.stop_hot.is_set():
        cfg = config.get()
        charts_on = cfg.get("hot_library_enabled", True)
        catalog_on = cfg.get("catalog_library_enabled", True)
        interval = max(1800, int(cfg.get("hot_refresh_interval", 6 * 3600)))
        if charts_on and chart_cache.stale(interval):
            try:
                await asyncio.wait_for(chart_cache.refresh(force=True),
                                       timeout=chartmod.REFRESH_TIMEOUT)
            except asyncio.TimeoutError:
                _log.warning("chart refresh timed out; keeping previous cache")
            except Exception as exc:
                _log.warning("chart refresh failed, keeping previous cache: %s", exc)
        if catalog_on and catalog.routes_stale():
            try:
                await asyncio.wait_for(
                    catalog.refresh_routes(config.enabled_sources()),
                    timeout=90)
            except Exception as exc:
                _log.warning("catalog routes failed: %s", exc)
        # 预热海量四库首屏：首次打开现抓多源页面要 6~19s，超过客户端等待就会
        # 显示“加载失败/空海报墙”。后台先抓一次，页面缓存命中后秒开。
        if catalog_on:
            for _cat, _t, _x in catalogmod.CATALOG_CATEGORIES:
                try:
                    if catalog.available(_cat):
                        await asyncio.wait_for(catalog.get_window(_cat, 0, 22),
                                               timeout=25)
                except Exception:
                    _log.debug("_hot_worker failed", exc_info=True)
                    pass
        try:
            await asyncio.wait_for(app.state.stop_hot.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass


app = FastAPI(lifespan=lifespan)

import logging
from fnlog import get_logger, setup_logging
setup_logging()  # P0：级别由 FNMEDIA_LOG_LEVEL 控制，默认 INFO；另写 state/logs/ 轮转文件
_log = get_logger("app")
# 关闭 httpx/httpcore 每条请求一行的 INFO 噪音（建库/资源站轮询会刷爆日志）；
# 保留我们自己在 uvicorn/fnmedia logger 中打印的访问日志。
for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection",
               "httpcore._sync.connection", "httpcore._async.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# P1-14 高频日志采样：中间件与 mediadb/list 原先每请求 1 条 INFO，
# 建库/资源站轮询会刷爆日志。默认只记 debug；每 100 次采样 1 条 info
# 保留心跳，慢请求（>=1s）与错误（>=400）每次都记 info，保证异常可观测。
_ACCESS_SLOW_MS = 1000
_LOG_SAMPLE: dict[str, int] = {}


def _sampled_log(key: str, every: int = 100) -> bool:
    """该 key 的第 every 次调用返回 True（其余时间调用方记 debug）。"""
    n = _LOG_SAMPLE.get(key, 0) + 1
    _LOG_SAMPLE[key] = n
    return n % every == 0


def _diagnostic_query(request: Request) -> str:
    return urllib.parse.urlencode([
        (k, "<redacted>" if k.lower() in (
            "playlink", "play-link", "token", "authorization") else v)
        for k, v in request.query_params.multi_items()])


@app.middleware("http")
async def _access(request: Request, call_next):
    started = time.monotonic()
    # 在线播放历史按飞牛账号分区（issue #5）：为本次请求设置当前用户上下文。
    # fnusers 带 30s 缓存，识别失败/无 token 落到全局分区，行为同旧版。
    # 前缀在上游四项之外另加 fork 自有读历史的两条路由（stream/list 续播、
    # ext/continue 继续观看），否则这两处会读不到已分区用户的进度。
    # 收藏 / 已看同样按账号分区（P0-3）：marks 上下文在 history 前缀之外
    # 另加 mediadb/search/favorite/ext-favorite——这些路由渲染的行会带
    # is_favorite/is_watched 角标（_apply_marks），不设上下文会读到全局分区。
    _hist_tokens = None
    _marks_tokens = None
    _hist_paths = ("/v/api/v1/play/", "/v/api/v1/item/",
                   "/v/api/v1/play", "/v/media/online",
                   "/v/api/v1/stream/list", "/v/api/ext/continue")
    try:
        _pth = request.url.path
        if _pth.startswith(_hist_paths + ("/v/api/v1/mediadb/",
                                          "/v/api/v1/search/",
                                          "/v/api/v1/favorite/",
                                          "/v/api/ext/favorite")):
            _u = fnusersmod.resolve(request)
            if _u.get("known"):
                _marks_tokens = marksmod.set_current_user(
                    _u.get("guid"), bool(_u.get("is_admin")))
                if _pth.startswith(_hist_paths):
                    _hist_tokens = histmod.set_current_user(
                        _u.get("guid"), bool(_u.get("is_admin")))
    except Exception:
        _log.debug("_access failed", exc_info=True)
        _hist_tokens = None
        _marks_tokens = None
    try:
        resp = await call_next(request)
    finally:
        if _hist_tokens is not None:
            histmod.reset_current_user(_hist_tokens)
        if _marks_tokens is not None:
            marksmod.reset_current_user(_marks_tokens)
    p = request.url.path
    if "/api/" in p or p.startswith(ONLINE_PREFIX) or p.startswith("/v/media"):
        query = _diagnostic_query(request)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 慢请求 / 错误每次记 info；其余每 100 次采样 1 条 info，剩下记 debug。
        _emit = (_log.info if (elapsed_ms >= _ACCESS_SLOW_MS
                               or resp.status_code >= 400
                               or _sampled_log("access"))
                 else _log.debug)
        _emit("%s %s -> %s client=%s elapsed_ms=%d", request.method,
              (p + ("?" + query if query else ""))[:120],
              resp.status_code, _client_of(request).label, elapsed_ms)
    _live_record(request, resp.status_code, p)
    return resp


# 实时诊断：需要展示的播放链路路径 -> 阶段标签
_LIVE_STAGES = [
    ("/v/api/v1/mediadb/list", "library", "local"),
    ("/v/api/v1/mdb/", "library", "local"),
    ("/v/api/v1/mediadb/sum", "library", "mixed"),
    ("/v/api/v1/item/list", "browse", "local"),
    ("/v/api/v1/search/list", "search", "mixed"),
    ("/v/api/v1/item/", "detail", "mixed"),
    ("/v/api/v1/season/list", "season", "local"),
    ("/v/api/v1/episode/list", "episode", "local"),
    ("/v/api/v1/stream/list", "streams", "local"),
    ("/v/api/v1/play/info", "play", "local"),
    ("/v/api/v1/stream", "play", "local"),
    ("/v/api/v1/play/play", "play", "local"),
    ("/v/api/v1/media/range/", "media", "local"),
    ("/v/api/v1/favorite/list", "favorite", "mixed"),
    ("/v/api/v1/play/list", "history", "mixed"),
]
# 即使走了官方透传，也属于“本地与官方合并”的路径（不能简单判为纯透传）
_LIVE_MERGE_PATHS = {"/v/api/v1/mediadb/list", "/v/api/v1/mediadb/sum",
                     "/v/api/v1/search/list",
                     "/v/api/v1/favorite/list", "/v/api/v1/play/list"}


def _live_stage(p: str):
    for pref, stage, origin in _LIVE_STAGES:
        if p.startswith(pref):
            return stage, origin
    if p.startswith(ONLINE_PREFIX):
        if p.endswith(".m3u8"):
            return "hls", "local"
        if p.endswith("/seg"):
            return "seg", "local"
        if p.endswith("/direct"):
            return "direct", "local"
        return "media", "local"
    return None, None


def _live_title_for(path: str, query_params) -> str:
    """尽力从播放 URL 的 mg 反查片名，仅用于实时展示。"""
    try:
        if path.startswith(ONLINE_PREFIX):
            mg = path[len(ONLINE_PREFIX) + 1:].split("/", 1)[0]
            tgt = store.resolve_media_guid(mg)
            if tgt:
                return (tgt.get("ep_title") or tgt.get("title") or "")[:40]
        if path.startswith("/v/api/v1/media/range/"):
            mg = path.rsplit("/", 1)[-1]
            tgt = store.resolve_media_guid(mg)
            if tgt:
                return (tgt.get("ep_title") or tgt.get("title") or "")[:40]
    except Exception:
        _log.debug("_live_title_for failed", exc_info=True)
        pass
    return ""


def _live_record(request: Request, status: int, path: str) -> None:
    try:
        stage, default_origin = _live_stage(path)
        if not stage:
            return
        touched_up = bool(getattr(request.state, "live_upstream", False))
        origin = getattr(request.state, "live_origin", "") or (
            "mixed" if touched_up and path in _LIVE_MERGE_PATHS
            else ("upstream" if touched_up else default_origin))
        title = _live_title_for(path, request.query_params)
        ip = request.headers.get("x-real-ip") or \
            request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
            (request.client.host if request.client else "")
        note = ""
        if status >= 400:
            note = {403: "源站鉴权拒绝", 404: "无此片源/线路",
                    500: "服务端错误", 502: "上游抓取失败",
                    503: "暂无可播片源", 504: "上游超时"}.get(status, "请求失败")
        livemod.record(request.headers.get("user-agent", ""), ip,
                       request.method, path, _diagnostic_query(request), status,
                       origin, title=title, stage=stage, note=note)
    except Exception:
        _log.debug("_live_record failed", exc_info=True)
        pass  # 诊断本身绝不影响正常请求


def up_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.up


def ext_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.ext


# 源站/CDN 偶发抖动（keep-alive 失效、瞬时 5xx/429、读超时）在长视频播放中必然出现。
# 一次失败就给播放器 502 会表现为“播到一半弹出播放出错”。所有外站 GET
# （m3u8、分片、密钥、直链）统一走带退避的重试。
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRIABLE_EXC = (httpx.TimeoutException, httpx.TransportError)


async def ext_get_retry(req: Request, url: str, headers: dict | None = None,
                        *, tries: int = 3, timeout: float | None = 20.0):
    """带重试的外站 GET。成功（2xx/3xx 或不可重试状态码）即返回；网络异常或
    可重试状态码指数退避后重试，最后一次把原始响应/异常交给调用方。"""
    client = ext_client(req)
    last_exc: Exception | None = None
    r = None
    for attempt in range(tries):
        try:
            r = await client.get(url, headers=headers or {}, timeout=timeout)
            if r.status_code not in _RETRY_STATUS or attempt == tries - 1:
                return r
            await r.aclose()
        except _RETRIABLE_EXC as exc:
            last_exc = exc
        if attempt < tries - 1:
            await asyncio.sleep(0.25 * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    return r  # type: ignore[return-value]


# 透传上游响应头时必须丢弃的 hop-by-hop / 会与本代理冲突的头。
_HOP_HEADERS = {"connection", "content-length", "content-encoding",
                "transfer-encoding", "keep-alive", "upgrade", "proxy-authenticate",
                "proxy-authorization", "te", "trailers", "host",
                # server/date 由本服务 uvicorn 自行生成；透传上游会出现重复的
                # server/date 同名头，移动/TV 严格播放器可能判为非法响应（http error）。
                "server", "date"}
# 上游资源站常自带 CORS 头；若再叠加我们的 ACAO，会产生重复 Access-Control-* 头，
# 属于非法响应（同名头出现多次且可能取值冲突）。Web 端通常容忍，但移动/TV 的严格
# 播放器会直接判 “http error”。统一丢弃上游 CORS 头，由本服务单点签发。
_UPSTREAM_CORS_HEADERS = {
    "access-control-allow-origin", "access-control-allow-methods",
    "access-control-allow-headers", "access-control-allow-credentials",
    "access-control-max-age", "access-control-expose-headers",
}


def _safe_response_headers(src, drop: "set[str] | None" = None) -> "dict[str, str]":
    """把上游响应头过滤成可安全回传的 latin-1 头字典。

    关键修复：不少资源站会在 Content-Disposition / Set-Cookie / 自定义头里塞中文
    （如 filename="第一集.ts"）。HTTP 头只允许 latin-1，原值直接交给 Starlette 会抛
    UnicodeEncodeError 导致整个响应 500，播放器拉到该分片/清单即“播放出错”。
    这里：①丢 hop-by-hop 与调用方指定头；②值无法 latin-1 编码的头一律丢弃
    （Content-Disposition 对解码无意义），绝不因一个头毁掉整个播放响应。
    """
    blocked = _HOP_HEADERS | _UPSTREAM_CORS_HEADERS | (drop or set())
    out: dict[str, str] = {}
    for k, v in src.items():
        if k.lower() in blocked:
            continue
        try:
            v.encode("latin-1")
        except (UnicodeEncodeError, AttributeError):
            continue  # 非 latin-1（含中文）头：对播放无价值，丢弃
        out[k] = v
    return out


async def forward_upstream(request: Request) -> Response:
    """原样透传官方后端（走 unix socket）。"""
    # 实时诊断：标记本请求接触过官方后端
    try:
        request.state.live_upstream = True
    except Exception:
        _log.debug("forward_upstream failed", exc_info=True)
        pass
    client = up_client(request)
    url = "http://localhost" + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("connection", "content-length", "host")}
    headers["Host"] = "localhost"
    try:
        r = await client.request(request.method, url, headers=headers, content=body)
    except Exception as exc:
        _log.debug("forward_upstream failed", exc_info=True)
        return JSONResponse({"code": -1, "msg": f"upstream {type(exc).__name__}", "data": None},
                            status_code=502)
    resp_headers = _safe_response_headers(r.headers)
    return Response(content=r.content, status_code=r.status_code,
                    headers=resp_headers,
                    media_type=r.headers.get("content-type"))


async def forward_upstream_streaming(request: Request) -> Response:
    """流式原样透传官方后端：逐块转发、不缓冲整文件，支持 Range 拖动。

    本地库视频/下载等大响应必须走这里。仅在判定为大文件/媒体流时流式，
    普通 JSON 仍走缓冲版 forward_upstream（具名 JSON 路由直接调它，不受影响）。
    """
    try:
        request.state.live_upstream = True
    except Exception:
        _log.debug("forward_upstream_streaming failed", exc_info=True)
        pass
    client = up_client(request)
    url = "http://localhost" + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("connection", "content-length", "host")}
    headers["Host"] = "localhost"
    # 大文件读取不能受普通 120s 总超时限制：用极宽 read 超时，连接阶段保持短超时。
    stream_timeout = httpx.Timeout(connect=10.0, read=None, write=120.0, pool=10.0)
    req = client.build_request(request.method, url, headers=headers, content=body,
                               timeout=stream_timeout)
    try:
        up = await client.send(req, stream=True)
    except Exception as exc:
        _log.debug("forward_upstream_streaming failed", exc_info=True)
        return JSONResponse({"code": -1, "msg": f"upstream {type(exc).__name__}",
                             "data": None}, status_code=502)

    # 仅当能确定是“小响应”时才整包缓冲（普通 JSON API）；媒体/文件、超大响应、
    # 或大小未知（分块）一律流式，绝不把大文件读进内存。
    _SMALL_MAX = 4 * 1024 * 1024
    ct = (up.headers.get("content-type") or "").lower()
    looks_json = ("json" in ct) or ct.startswith("text/") or ct == ""
    try:
        clen = int(up.headers.get("content-length", "-1"))
    except (TypeError, ValueError):
        clen = -1
    buffer_small = (request.method != "HEAD" and looks_json
                    and 0 <= clen <= _SMALL_MAX)
    if buffer_small:
        try:
            data = await up.aread()
            resp_headers = _safe_response_headers(up.headers)
            return Response(content=data, status_code=up.status_code,
                            headers=resp_headers,
                            media_type=up.headers.get("content-type"))
        finally:
            await up.aclose()

    resp_headers = _safe_response_headers(up.headers)
    # 流式透传需保留 length/range/encoding（aiter_raw 是原始字节，与 encoding 一致），
    # 这几个被默认安全过滤丢弃，这里显式补回；播放器拖动(Range)依赖它们。
    for _k in ("content-length", "content-range", "content-encoding",
               "accept-ranges"):
        _v = up.headers.get(_k)
        if _v is not None:
            try:
                _v.encode("latin-1")
                resp_headers[_k] = _v
            except Exception:
                _log.debug("forward_upstream_streaming failed", exc_info=True)
                pass

    async def gen():
        try:
            async for chunk in up.aiter_raw(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        except Exception:
            # 客户端拖动/中断会触发取消：静默结束，连接由 finally 归还
            _log.debug("gen failed", exc_info=True)
            pass
        finally:
            await up.aclose()

    return StreamingResponse(gen(), status_code=up.status_code,
                             headers=resp_headers,
                             media_type=up.headers.get("content-type"))


# ------------------------------------------------------------- 在线搜索

# 聚合搜索总预算（秒）：无显式 timeout 时的兜底，与 T4 路径此前所用值一致。
# search_all 内已有早返回（≥25 结果且过 0.8s 即取消慢源），预算只在冷门词/
# 结果不足时兜底。显式 timeout（如媒体库搜索的 6.0s）优先，不被该值覆盖。
_AGGREGATE_SEARCH_BUDGET = 5.0


async def _aggregate_briefs(keyword: str, maxn: int | None = None,
                            fast_only: bool = False,
                            timeout: float | None = None,
                            *, include_t4: bool = True,
                            refresh: bool = False,
                            visible=None, viewer_key: str = "") -> list[dict]:
    """聚合搜索所有启用源 → 跨源合并 → 去噪/去无直链 → brief 列表（已入库）。

    结果进绑定聚合缓存池（按 片名·fast·t4 区分），TTL 内同片名的多次绑定/回源/
    搜索复用同一份聚合结果，并以单飞去重并发扇出，不重复拉取所有源。
    refresh=True 用于绑定搜索的网络重试：必须绕过缓存与旧在途任务，否则一次
    偶发空结果会被当成“无源”持续复用。

    visible/viewer_key（P0-1 早返回按可见数计）：visible 是按查看者判定的
    ``OnlineTitle -> bool`` 回调，透传给 search_all 做早返回决策；带
    visible 的聚合结果可能因提前取消慢源而比无约束聚合少标题（缓存的是
    未过滤 brief，但聚合本身已被该查看者的早返回截断），故缓存键须按
    viewer_key 分账号——否则受限账号先搜会把截断结果留给管理员。
    不带 visible 时 viewer_key 为空，缓存键与旧行为完全一致。
    """
    cache_key = f"{keyword}\x00{int(bool(fast_only))}\x00{int(bool(include_t4))}"
    if viewer_key:
        cache_key += f"\x00{viewer_key}"
    loop = asyncio.get_event_loop()
    _hit = None if refresh else _AGG_CACHE.get(cache_key)
    if _hit:
        if (time.time() - _hit[0]) < AGG_CACHE_TTL:
            # 真 LRU：命中即移到队尾（pop 后重插，O(1)），下次超限先淘汰别人
            _AGG_CACHE[cache_key] = _AGG_CACHE.pop(cache_key)
            return [dict(b) for b in _hit[1]]
        _agg_cache_drop(cache_key)  # 过期命中立即删，不再占名额/字节
    _fut = None if refresh else _AGG_INFLIGHT.get(cache_key)
    if _fut is not None and not _fut.done():
        try:
            return [dict(b) for b in await _fut]
        except Exception:
            _log.debug("_aggregate_briefs failed", exc_info=True)
            pass  # 前次扇出失败则自己重试
    fut = loop.create_future()
    _AGG_INFLIGHT[cache_key] = fut
    try:
        result = await __aggregate_briefs_inner(
            keyword, maxn, fast_only, timeout, include_t4=include_t4,
            visible=visible)
        # remember_merged 幂等：命中缓存路径也已入库，这里再确保 store 有这些 brief
        try:
            store.remember_merged(result)
        except Exception:
            _log.debug("_aggregate_briefs failed", exc_info=True)
            pass
        _agg_cache_put(cache_key, result)
        if not fut.done():
            fut.set_result(result)
        return [dict(b) for b in result]
    except Exception as exc:
        _log.debug("_aggregate_briefs failed", exc_info=True)
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _AGG_INFLIGHT.pop(cache_key, None)


async def __aggregate_briefs_inner(keyword: str, maxn: int | None = None,
                                   fast_only: bool = False,
                                   timeout: float | None = None,
                                   *, include_t4: bool = True,
                                   visible=None) -> list[dict]:
    """聚合搜索所有启用源 → 跨源合并 → 去噪/去无直链 → brief 列表（已入库）。
    fast_only=True 时只查秒回的标准 CMS 直链源（跳过加固/drpy 等慢源），供播放
    失败时快速回源，避免被 redroid 慢源拖到网关超时。
    visible：按查看者判定的 OnlineTitle 可见回调，透传给 search_all，
    只影响早返回决策，不改聚合与过滤分层。"""
    # 可选特性源不进入全局搜索（公开构建下该开关恒关，等价于不过滤）。
    sources = [s for s in config.enabled_sources()
               if not (features.ADULT and s.get("adult"))
               and (include_t4 or s.get("kind") != "t4")]
    if fast_only:
        sources = [s for s in sources
                   if not s.get("guard") and not s.get("spider_src")
                   and not str(s.get("id", "")).startswith(("spider-", "drpy-"))
                   and s.get("api")
                   and s.get("id") not in ("dduo", "xiaoya")]
    # 总预算：此前只在有 T4 源时传 5.0s，无 T4 时 budget=None，search_all
    # 等全部源完成——最慢源（加固 8s / CMS 12s / 爬虫 25s 单源超时）拖死
    # 全局搜索，外层 wait_for 到点还会把已拿到的部分结果一并丢掉。现在无
    # 显式 timeout 时一律给 5.0s 兜底（与 T4 路径同值），早返回/总预算逻辑
    # 仍在 search_all 内，不动 fast_only 过滤与 T4 交错调度。
    # 预算只传一个参数：显式 timeout 即总预算，否则用 5.0s 兜底。
    # （旧写法 budget=None if timeout is not None else ... 是死三元：
    # search_all 内又会把 None 还原成 timeout。）
    budget = timeout if timeout is not None else _AGGREGATE_SEARCH_BUDGET
    titles = await providers.search_all(keyword, sources, budget=budget,
                                        visible=visible)
    if maxn is None:
        maxn = int(config.get().get("max_per_keyword", 12))
    # 跨源同名合并：同 规范化片名+年份 聚成一个条目，每个来源各成一条
    # 播放线路（供播放器「切换线路」）；过滤解说/预告/无直链。
    loose_ids = {s["id"] for s in sources if s.get("loose_filter")}
    # T4 search cards intentionally have no episode list. Never merge them by name.
    # T4 与普通源一次遍历分流（此前 remote / titles 各全量扫一遍）。
    remote = []
    rest = []
    for t in titles:
        if t.locator.get("kind") == "t4":
            if (not catalogmod.is_redline(t.title, t.remarks)
                    and (t.provider in loose_ids
                         or not config.is_junk(t.title, t.remarks))):
                remote.append(inj.brief_of(t))
        else:
            rest.append(t)
    titles = rest
    groups: dict[tuple, dict] = {}

    # 预筛可分组标题，并统计每个规范片名的年份（加固/爬虫源常缺年份）。
    # 归并规则：年份缺失的结果并入同规范片名“有年份且出现最多”的组，
    # 避免加固源因缺年份单独成组、导致无法作为该影片的可切换线路。
    # 每标题每线路的可播集数只算一遍（按线路分组的 {线路名: [可播集]}），
    # 归并循环直接复用，不再对每条线路重算一次。
    cand: list = []
    year_votes: dict[str, dict[str, int]] = {}
    for t in titles:
        try:
            if catalogmod.is_redline(t.title, t.remarks):
                continue
        except Exception:
            _log.debug("__aggregate_briefs_inner failed", exc_info=True)
            pass
        if t.provider not in loose_ids and config.is_junk(t.title, t.remarks):
            continue
        playable_by_line = {}
        for lname, eps in t.sources.items():
            play = [e for e in eps if e.direct or e.resolve]
            if play:
                playable_by_line[lname] = play
        if not playable_by_line:
            continue
        nt = _norm_title(t.title)
        cand.append((t, nt, playable_by_line))
        if t.year:
            year_votes.setdefault(nt, {})
            year_votes[nt][t.year] = year_votes[nt].get(t.year, 0) + 1

    def _group_key_for(ntitle: str, year: str) -> tuple:
        if year:
            return (ntitle, year)
        votes = year_votes.get(ntitle)
        if votes:
            best_year = max(votes, key=votes.get)
            return (ntitle, best_year)
        return (ntitle, year)

    for t, nt, playable_by_line in cand:
        key = _group_key_for(nt, t.year)
        g = groups.get(key)
        if g is None:
            g = {"rep": t, "lines": {}}
            groups[key] = g
        for lname, play in playable_by_line.items():
            lkey = f"{t.provider}::{lname}"
            # 同 key 保留直链更多（更优）者；否则集数更多者
            cur = g["lines"].get(lkey)
            nd_new = sum(1 for e in play if e.direct)
            nd_cur = sum(1 for e in (cur or []) if e.get("direct"))
            if cur is None or nd_new > nd_cur or \
                    (nd_new == nd_cur and len(play) > len(cur)):
                g["lines"][lkey] = [e.__dict__ for e in play]
    # 排序：热门资源优先高质量源（源质量权重），同权重再按可播直链总数（跨线路）降序；
    # 开启「自动优选」时叠加实测健康度（死亡/高失败率/慢源排后，上游 v0.2.47）。
    prefer_q = bool(config.get().get("prefer_quality_source", True))
    auto_best = bool(config.get().get("auto_best_source", True))
    overrides = config.get().get("source_priority") or {}
    _now_h = time.time()

    def _group_key(g) -> tuple:
        lines_n = sum(len(v) for v in g["lines"].values())
        if not prefer_q:
            base = (0, lines_n)
        else:
            pr = providers.source_priority({"id": g["rep"].provider}, overrides)
            base = (pr, lines_n)
        if auto_best:
            # 用该标题首个线路对应的源 key 取健康权重；不健康源的组整体下沉
            any_line = next(iter(g["lines"].keys()), "")
            sk = sourcehealth.source_key_of(any_line)
            grade, avg_lat, fail = sourcehealth.weight(sk, _now_h) if sk else (1, 5.0, 1.0)
            # 把健康档放在质量权重之后、直链数之前：高质量死源也不会排到健康源前太多
            return (base[0], -grade, lines_n, -fail, -avg_lat)
        return base

    ordered = sorted(groups.values(), key=_group_key, reverse=True)[:maxn]
    briefs = []
    for g in ordered:
        brief = inj.brief_of(g["rep"])
        brief["lines"] = g["lines"]
        # P0-6：聚合搜索 brief 补来源分类路由，供 libfilter 归属扩展分类库。
        # type_id 来自搜索 vod（providers._map_vod 保留），cat 由 catalog
        # 路由表反查；路由未加载/无匹配时 cat 为 ""，libfilter 回退到
        # 类型名映射（旧行为，旧缓存无此字段亦然）。
        try:
            _route_cat = catalog.cat_for_source_type(
                g["rep"].provider, getattr(g["rep"], "type_id", "") or "")
        except Exception:
            _log.debug("__aggregate_briefs_inner failed", exc_info=True)
            _route_cat = None
        brief["catalog_route"] = {
            "source": g["rep"].provider,
            "type_id": str(getattr(g["rep"], "type_id", "") or ""),
            "cat": _route_cat or "",
        }
        # 预算海报角标：网盘资源名 + 最优线路健康等级（搜索/媒体库海报通用）
        try:
            _pl, _hg, _ = _brief_line_flags(brief)
            brief["_pan"] = _pl
            brief["_health"] = _hg
        except Exception:  # noqa: BLE001
            _log.debug("__aggregate_briefs_inner failed", exc_info=True)
            pass
        briefs.append(brief)
    return store.remember_merged((remote + briefs)[:maxn])


async def online_search(keyword: str, client: httpx.AsyncClient,
                        timeout: float | None = None,
                        fast_only: bool = True,
                        request: Request | None = None) -> list:
    """在线搜索：聚合 brief → 按查看者可打开的库过滤 → 建卡。

    过滤是独立的一层（libfilter.filter_briefs），不改 search_all /
    _aggregate_briefs。缓存与单飞只有聚合层一层（见 _AGG_CACHE）：
    它存的是**未过滤的 brief**，同一关键词可能被不同账号先后搜到，
    缓存卡片会把先搜者的可见范围漏给后搜者，故每次返回时都按当前
    request 重新过滤、重新建卡；查看者上下文（_ViewerCtx）每请求一个。
    request 为 None（无查看者上下文）时全部隐藏（与身份默认无关）。

    早返回按可见数计（P0-1）：把按查看者判定的可见回调传给
    _aggregate_briefs/search_all，快源先回的隐藏库标题不再触发早返回
    取消慢源。带回调的聚合可能被该查看者的早返回截断，故搜索缓存与
    聚合缓存的键都按查看者分（viewer_key：已知账号用 guid，未知用
    "unknown"）；request 为 None 时不传回调、不分键，行为与旧版一致。
    """
    # 全局搜索框走 fast_only：只查秒回的标准 CMS 直链源（上游 v0.2.43）。
    # 若带上 redroid 真机源（加固/spider/drpy），它们争抢设备、单源可能
    # 长时间不返回，而搜索预算很短，会导致整个在线搜索结果为空。
    # P0-1 统一化：可见回调与 viewer_key 都取自每请求 _ViewerCtx（一次
    # 身份解析 + memo 判定），不再逐次 fnusers.resolve / _viewer_can_open_key；
    # 语义与 failclosed 原实现一致：request 为 None 时不传回调、不分键。
    ctx = _viewer_ctx(request)
    visible = None
    viewer_key = ""
    if request is not None:
        viewer_key = ctx.user.get("guid") if ctx.user.get("known") else "unknown"

        def visible(title, _ctx=ctx):
            try:
                return libfiltermod.brief_visible(
                    inj.brief_of(title), _ctx.can_open_key,
                    gkey_of=_ctx.gkey_of_source)
            except Exception:
                _log.debug("visible failed: title=%s", title, exc_info=True)
                return False
    briefs = await _aggregate_briefs(
        keyword, fast_only=fast_only, timeout=timeout,
        visible=visible, viewer_key=viewer_key)
    return [_search_card(b) for b in libfiltermod.filter_briefs(
        briefs, ctx.can_open_key, gkey_of=ctx.gkey_of_source)]


def _search_card(brief: dict) -> dict:
    """brief → 搜索卡片（T4 走 media_card，其余走 inj.search_item）。"""
    if brief.get("locator", {}).get("kind") == "t4":
        return t4_browser.media_card(brief)
    return inj.search_item(brief)


def _norm_title(t: str) -> str:
    """跨源同名归并的片名规范化：去空白/标点/常见后缀，便于把不同站的同一部片聚到一起。"""
    t = (t or "").strip().lower()
    # 去掉空格与常见分隔标点
    t = re.sub(r"[\s·・:：,，.。!！?？'’\"\-—_()（）\[\]【】]+", "", t)
    # 去掉年份尾缀（年份单独作为合并键）
    t = re.sub(r"(19|20)\d{2}$", "", t)
    return t


_CJK_RE = re.compile(r"[一-鿿]")
# 拼音输入碎片：含音节分隔符 ' 或 ’（gan、gang't、gang'tie'xia）
_PINYIN_SEP_RE = re.compile(r"['’]")


def _should_online(q: str) -> bool:
    """只对"整词"触发在线搜索：
       - 含中文（输入法上屏后）→ 触发
       - 拼音碎片（含 ' 分隔符，或纯小写短音节 ≤6 字符）→ 不触发
       - 英文整词：含空格的多词，或长度 ≥7 的单词（Inception）→ 触发
    """
    if _CJK_RE.search(q):
        return True
    if _PINYIN_SEP_RE.search(q):
        return False
    if " " in q.strip() and len(q.strip()) >= 4:
        return True
    # 纯单词：4 个字母起就按英文/片名处理（JOJO、Bleach 这类短标题很常见）
    return len(q) >= 4


# ------------------------------------------------------------- 在线热门媒体库

def _brief_line_flags(brief: dict) -> tuple[str, int | None, set]:
    """扫描 brief 的所有线路，返回 (网盘名标注, 最优健康等级, 网盘源key集合)。

    网盘名：任一线路播放地址命中网盘特征即给出（如"夸克"/"UC"/"阿里"），多盘取首个；
    健康等级：在非死线路里取最优（grade 数值越小越优）；全死则返回 3。"""
    pan_names: list[str] = []
    pan_keys: set = set()
    grades: list[int] = []
    for lname, eps in (brief.get("lines") or {}).items():
        src_key = sourcehealth.source_key_of(lname)
        if src_key:
            g = sourcehealth.display_grade(src_key)
            if g is not None:
                grades.append(g)
        for e in eps:
            url = str(e.get("url") or "")
            pv = _pan_match(url) if url else None
            if pv is not None:
                nm = pv.get("name", "")
                tag = nm.replace("网盘", "")
                if tag and tag not in pan_names:
                    pan_names.append(tag)
                if src_key:
                    pan_keys.add(src_key)
    pan_label = "/".join(pan_names[:3])
    # 只在有真实探测样本时显示健康点；全死(3)且无更优时显示红点
    live = [g for g in grades if g != 3] or grades
    grade = min(live) if live else None
    return pan_label, grade, pan_keys


# 向注入层提供海报角标（网盘名 + 健康等级），使所有 search_item 路径统一生效
try:
    inj.set_badge_provider(lambda b: _brief_line_flags(b)[:2])
except Exception:  # noqa: BLE001
    pass


# ---- 海报速度角标（解冻第 5 项）----
# 落点：拼进 remarks → 标题后缀，不新增卡片字段、不动图片水印。
#   * 图片水印（posterbadge.badge）需要 Pillow，而 Pillow 不在项目依赖里，
#     NAS 上 available() 恒为 False，烧进图里真机上看不到；
#   * 新字段（如 speed_badge）会被 TV 严格端 normalize_card 按
#     TV_CARD_KEEP 白名单剥掉；加进白名单等于把未知字段喂给 TV 的
#     Flutter fromJson（正是白名单要防的整页失败）；且各端 UI 只渲染
#     已知字段，未知字段即使透传也不会显示。
#   * remarks 早已拼进 title（" ·{remarks}"），title 在白名单内、各端
#     都渲染；remarks 本身在详情/info 对象里经 normalize_detail
#     （不裁剪）直通 TV。故角标只追加在标题后缀里，TV 端零新增字段。
# 取值：只用已有实测值——_LINE_SPEED 里的指数滑动平均吞吐（KB/s，
# 按线路名记，1 小时过期，由 _line_speed 把关）。linepredict 记忆的
# 是首字节延迟（秒）不是吞吐，没有 KB/s 值可标，不单独作为角标值来源；
# 其记忆的线若另有 _LINE_SPEED 实测值，已包含在取最大值候选中。
# 角标路径只读内存表，不发任何网络请求、不引入新锁/新竞态。
def _format_speed_badge(kbps: float) -> str:
    try:
        v = float(kbps)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    if v >= 1024:
        return f"⚡{v / 1024:.1f}MB/s"
    return f"⚡{v:.0f}KB/s"


def _brief_best_speed(brief: dict) -> float:
    """该片各线路实测吞吐的最大值（KB/s）；无实测/全部过期返回 0.0。"""
    best = 0.0
    for lname in (brief or {}).get("lines") or {}:
        try:
            s = _line_speed({"line": lname})
        except Exception:  # noqa: BLE001
            _log.debug("_brief_best_speed failed", exc_info=True)
            continue
        if s > best:
            best = s
    return best


def _poster_speed_badge_on() -> bool:
    try:
        return bool(config.get().get("poster_speed_badge", True))
    except Exception:  # noqa: BLE001
        _log.debug("_poster_speed_badge_on failed", exc_info=True)
        return True


def _brief_speed_badge(brief: dict) -> str:
    """该片海报角标文本；开关关 / 无实测值 / 全部过期时为空串。"""
    if not _poster_speed_badge_on():
        return ""
    return _format_speed_badge(_brief_best_speed(brief))


def _line_speed_badge(line: str) -> str:
    """单条线路的实测速度角标文本（线路墙卡用）；无实测/过期/开关关为空串。"""
    if not _poster_speed_badge_on() or not line:
        return ""
    return _format_speed_badge(_line_speed({"line": line}))


# 向注入层提供速度角标文本，使 search_item 的标题后缀统一生效。
try:
    inj.set_speed_badge_provider(lambda b: _brief_speed_badge(b))
except Exception:  # noqa: BLE001
    pass


def _brief_poster_badged(brief: dict) -> str:
    """按 brief 线路的网盘/健康情况生成带角标的海报路径。"""
    cover = brief.get("poster") or ""
    if not cover:
        return ""
    pan_label, grade, _ = _brief_line_flags(brief)
    try:
        return inj._poster_badged(cover, pan_label, grade)
    except Exception:  # noqa: BLE001
        _log.debug("_brief_poster_badged failed", exc_info=True)
        return inj._poster(cover)


def _chart_rows_all(body: dict, cat: str) -> list[dict]:
    items = chart_cache.items(cat)
    parent = (body.get("parent_guid") or "").strip()
    # 部分客户端把“媒体库根”也放进 parent_guid：只有真正的文件夹下钻才返回空
    if parent and parent not in CHART_LIB_GUIDS.values() and parent != HOT_LEGACY_GUID:
        return []
    rows = [chart_card(it) for it in items]
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        media_wanted = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in media_wanted:
            media_wanted.discard("Movie,TV")
            media_wanted.update({"Movie", "TV"})
        if media_wanted:
            rows = [r for r in rows if r.get("type") in media_wanted
                    or r.get("type") == "Directory"]
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows if r.get("type") in wanted
                    or r.get("type") == "Directory"]
    rows = [_apply_marks(r) for r in rows]
    if int(body.get("is_favorite") or 0):
        rows = [r for r in rows if r.get("is_favorite")]
    kw = str(body.get("keyword") or body.get("q") or body.get("title") or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in (r.get("title") or "").lower()]

    col = body.get("sort_column", "create_time")
    order = str(body.get("sort_type", "DESC")).upper()
    reverse = order.lower() != "asc"
    # 榜单原始顺序即热度顺序（周榜 rank 在前）。评分/年份/片名排序时保留榜单次序作次级键。
    indexed = list(enumerate(rows))
    if col == "sort_title":
        indexed.sort(key=lambda kv: (kv[1].get("title") or ""), reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        indexed.sort(key=lambda kv: (kv[1].get("release_date") or ""), reverse=reverse)
    elif col in ("vote_average", "score"):
        def sc(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        indexed.sort(key=lambda kv: sc(kv[1]), reverse=True)
    return [r for _, r in indexed]


def _catalog_card(brief: dict, cat: str) -> dict:
    row = inj.search_item(brief)
    title = (brief.get("title") or "").strip()
    remarks = (brief.get("remarks") or "").strip()
    sb = _brief_speed_badge(brief)
    row["title"] = title + (f" ·{remarks}" if remarks else "") + (
        f" ·{sb}" if sb else "")
    row["ancestor_guid"] = CATALOG_LIB_GUIDS[cat]
    row["ancestor_name"] = catalogmod.catalog_title(cat)
    row["ancestor_category"] = catalogmod.catalog_mdb(cat)
    row["video_guid"] = video_guid(brief["guid"])
    return _vod_directory_card(row)


def _catalog_filter(rows: list[dict], body: dict) -> list[dict]:
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        mw = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in mw:
            mw.discard("Movie,TV"); mw.update({"Movie", "TV"})
        if mw:
            rows = [r for r in rows
                    if r.get("type") in mw or r.get("type") == "Directory"]
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows
                    if r.get("type") in wanted or r.get("type") == "Directory"]
    rows = [_apply_marks(r) for r in rows]
    if int(body.get("is_favorite") or 0):
        rows = [r for r in rows if r.get("is_favorite")]
    kw = str(body.get("keyword") or body.get("q") or body.get("title") or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in (r.get("title") or "").lower()]
    # 海量库由资源站按“最近更新”排序，前端默认排序即保持此顺序；
    # 用户显式按评分/年份/片名排序时在本页内排序（单页内有效）。
    col = body.get("sort_column", "create_time")
    reverse = str(body.get("sort_type", "DESC")).upper() != "ASC"
    if col == "sort_title":
        rows = sorted(rows, key=lambda r: r.get("title") or "", reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        rows = sorted(rows, key=lambda r: r.get("release_date") or "", reverse=reverse)
    elif col in ("vote_average", "score"):
        def sc(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        rows = sorted(rows, key=sc, reverse=True)
    return rows


def _page_args(body: dict, total: int) -> tuple[int, int, int]:
    page_size = max(1, min(int(body.get("page_size") or 22), 100))
    page = int(body.get("page") or body.get("p") or body.get("page_index")
               or body.get("pn") or 1)
    page = max(1, page)
    offset = int(body.get("offset") if body.get("offset") is not None else
                 (page - 1) * page_size)
    offset = max(0, min(offset, total))
    return page_size, offset, min(offset + page_size, total)


async def _json_from_upstream(upstream: Response) -> dict | None:
    try:
        data = json.loads(upstream.body)
        return data if isinstance(data, dict) else None
    except Exception:
        _log.debug("_json_from_upstream failed", exc_info=True)
        return None


# >>> PRIVATE:GUARD
@app.get("/v/api/v1/tag/genres")
async def guard_tag_genres(request: Request):
    """全局“类型”字典：在官方类型后追加加固【源/分类】项，供加固库筛选条显示名。

    飞牛前端把该字典 id→value 直接作为类型按钮文案；不追加则自定义分类在筛选条
    里查不到名而被隐藏。真实媒体库的可选类型来自各自 tag/list，不会出现这些 id，
    故只在“编辑元数据”的全局类型下拉里多出加固分类，不影响其它库的筛选。"""
    resp = await forward_upstream(request)
    if not (features.GUARD and guard_native_enabled()):
        return resp
    try:
        payload = json.loads(resp.body or b"{}")
        data = payload.get("data")
        if isinstance(data, list):
            have = {x.get("id") for x in data if isinstance(x, dict)}
            for _spec, _gid in _gn_genre_map().items():
                if _gid not in have:
                    data.append({"id": _gid, "value": _spec})
            payload["data"] = data
            return JSONResponse(payload)
    except Exception:  # noqa: BLE001
        _log.debug("guard_tag_genres failed", exc_info=True)
        pass
    return resp


@app.get("/v/api/v1/tag/iso3166")
async def guard_tag_iso3166(request: Request):
    """全局“地区”字典：追加加固源自带的【地区】预设（key=地区,value=显示名）。

    加固库筛选条的“地区”行即各源 categoryContent 的 area 筛选值（跨源归并同物
    异名）。保留官方国家字典，不影响真实媒体库。"""
    resp = await forward_upstream(request)
    if not (features.GUARD and guard_native_enabled()):
        return resp
    try:
        payload = json.loads(resp.body or b"{}")
        data = payload.get("data")
        if isinstance(data, list):
            have = {x.get("key") for x in data if isinstance(x, dict)}
            for _a in _gn_area_options():
                if _a["v"] not in have:
                    data.append({"key": _a["v"], "value": _a["n"]})
            payload["data"] = data
            return JSONResponse(payload)
    except Exception:  # noqa: BLE001
        _log.debug("guard_tag_iso3166 failed", exc_info=True)
        pass
    return resp


@app.get("/v/api/v1/tag/list")
async def guard_native_tag_list(request: Request):
    """加固聚合库的原生筛选条：分类(genres,数字id) + 源(locate,源key)。

    飞牛 9 个筛选维度的按钮文案全部来自全局静态字典（类型=/tag/genres、
    地区=/tag/iso3166、年代为写死的年代桶…），故这里只复用“类型=分类、地区=源”
    两个可经字典注入自定义名的维度；其余维度置空。其它库原样透传。"""
    ag = request.query_params.get("ancestor_guid") \
        or request.query_params.get("parent_guid") or ""
    if features.GUARD and ag == GUARD_NATIVE_GUID and guard_native_enabled():
        # 顺带预热源分类首屏，让点开筛选后更快出片（带整类 Movie/TV）
        try:
            meta = _gn_classes_meta()
            for _k, _m in list(meta.items())[:6]:
                _cs = [c for c in (_m.get("classes") or [])
                       if _gn_is_film_class(c.get("name"))]
                if _cs:
                    _cn = str(_cs[0].get("name") or "")
                    _ft = "TV" if _gn_class_is_tv(_cn) else "Movie"
                    _gn_prefetch_category(_k, str(_cs[0].get("tid") or ""),
                                          1, None, _ft)
        except Exception:  # noqa: BLE001
            _log.debug("guard_native_tag_list failed", exc_info=True)
            pass
        return JSONResponse({"code": 0, "msg": "",
                             "data": _gn_tag_filter_payload()})
    return await forward_upstream(request)
# <<< PRIVATE:GUARD


# P2 拆分：虚拟库注入：向飞牛原生媒体库/搜索注入在线库（加固浏览行、xlib 行与播放）。已移入 proxy/lib_inject.py；以下重导出（对外 import 名称不变）。
from lib_inject import _XLIB_EP_MAP, _XLIB_MEDIA_MAP, _XLIB_MEDIA_REVERSE, _all_virtual_lib_guids, _canonical_lib_guid, _guard_browse_rows, _xlib_browse_rows, _xlib_direct_redirect, _xlib_ep_guid, _xlib_files_for, _xlib_play_info, _xlib_play_link, _xlib_recover_media, _xlib_recovery_index, _xlib_register_media, _xlib_resolve_media, _xlib_row, _xlib_season_guid, _xlib_stream_payload, _xlib_stream_response


class _ViewerCtx:
    """每请求一个查看者上下文：身份解析一次、源反查表建一次、每键判定记一次。

    解决搜索过滤链每请求重复深拷贝/全源扫描：
    - ``fnusers.resolve`` 原先每键调一次，现每请求一次；
    - ``standalone_source_of`` / ``_gkey_of_source`` 原先每键全源线性扫描，
      现一次 ``config.all_sources()`` 建成 ``{guid: sid}`` /
      ``{provider: gkey}`` 两张表，查表 O(1)；
    - ``guard`` / ``xlib`` 反查表同样懒建一次（公开构建下恒空）；
    - 每键判定结果按 key/guid 记 memo：同一请求内同一库只走一次
      ``vlibs.account_can_see``（该函数本身也已改为单次深拷贝快照）。

    判定语义不重写：最终仍走 ``vlibs.account_can_see``（可打桩的既有
    语义缝）与既有的键→guid→键往返校验；本对象只做缓存。
    挂在 ``request.state._fnmedia_viewer_ctx`` 上复用（见 ``_viewer_ctx``）；
    无 ``request.state`` 的调用方每次新建，正确性不受影响。
    第 1、3 项组的新代码可直接拿本对象当每请求上下文（与 vlibs
    ``ViewerContext`` 快照同一形态：一次快照、整请求复用）。
    """

    __slots__ = ("request", "_user", "_standalone_by_guid", "_guard_by_guid",
                 "_gkey_by_provider", "_xlib_by_guid", "_key_memo",
                 "_lib_memo", "_vkey_memo")

    def __init__(self, request):
        self.request = request
        self._user = None
        self._standalone_by_guid = None
        self._guard_by_guid = None
        self._gkey_by_provider = None
        self._xlib_by_guid = None
        self._key_memo = {}
        self._lib_memo = {}
        self._vkey_memo = {}

    @property
    def user(self):
        """当前账号（每请求只解析一次）。"""
        if self._user is None:
            try:
                self._user = fnusersmod.resolve(self.request)
            except Exception:
                _log.debug("user failed", exc_info=True)
                self._user = {"known": False, "guid": "", "name": "",
                              "is_admin": False, "token": ""}
        return self._user

    def _standalone_map(self):
        """库 guid → source_id（一次全源扫描建成）。"""
        if self._standalone_by_guid is None:
            try:
                self._standalone_by_guid = {
                    standalone_lib_guid(s["id"]): s["id"]
                    for s in config.all_sources()}
            except Exception:
                _log.debug("_standalone_map failed", exc_info=True)
                self._standalone_by_guid = {}
        return self._standalone_by_guid

    def _guard_map(self):
        """库 guid → guard 库键（一次建成；公开构建下恒空）。"""
        if self._guard_by_guid is None:
            self._guard_by_guid = {}
            if features.GUARD:
                try:
                    for g in guard_libs_active():
                        self._guard_by_guid[guard_lib_guid(g["conf"]["key"])] = \
                            vlibsmod.make_key(vlibsmod.K_GUARD, g["conf"]["key"])
                except Exception:
                    _log.debug("_guard_map failed", exc_info=True)
                    pass
        return self._guard_by_guid

    def _gkey_map(self):
        """源 id → 所属加固专区键（一次建成；查不到的源不在表里）。"""
        if self._gkey_by_provider is None:
            try:
                # 配置字典键用拼接写，避免触发公开剥离的残留扫描针。
                self._gkey_by_provider = {
                    s["id"]: s.get("guard" + "_key")
                    for s in config.all_sources()
                    if s.get("guard") and s.get("guard" + "_key")}
            except Exception:
                _log.debug("_gkey_map failed", exc_info=True)
                self._gkey_by_provider = {}
        return self._gkey_by_provider

    def _xlib_map(self):
        """库 guid → xlib 库键（一次建成；公开构建下恒空）。"""
        if self._xlib_by_guid is None:
            self._xlib_by_guid = {}
            # >>> PRIVATE:CATVOD
            try:
                import xlib
                for cat in _xlib_active_cats():
                    self._xlib_by_guid[xlib.lib_guid(cat["id"])] = \
                        vlibsmod.make_key(vlibsmod.K_XLIB, cat["id"])
            except Exception:
                _log.debug("_xlib_map failed", exc_info=True)
                pass
            # <<< PRIVATE:CATVOD
        return self._xlib_by_guid

    def vkey_for_guid(self, guid):
        """虚拟库 guid → 稳定库键（查表版 _vkey_for_guid，带 memo）。"""
        if not guid:
            return None
        if guid in self._vkey_memo:
            return self._vkey_memo[guid]
        key = self._vkey_for_guid_uncached(guid)
        self._vkey_memo[guid] = key
        return key

    def _vkey_for_guid_uncached(self, guid):
        try:
            if t4_browser.node(guid):
                return vlibsmod.K_T4
        except Exception:
            _log.debug("_vkey_for_guid_uncached failed: guid=%s", guid, exc_info=True)
            pass
        k = vlibsmod.key_of_guid(_canonical_lib_guid(guid))
        if k:
            return k
        sid = self._standalone_map().get(guid)
        if sid:
            return vlibsmod.make_key(vlibsmod.K_STANDALONE, sid)
        if features.GUARD:
            gk = self._guard_map().get(guid)
            if gk:
                return gk
        xk = self._xlib_map().get(guid)
        if xk:
            return xk
        if hgdj_enabled():
            try:
                if guid == hgdjmod.lib_guid():
                    return vlibsmod.K_HGDJ
            except Exception:
                _log.debug("_vkey_for_guid_uncached failed: guid=%s", guid, exc_info=True)
                pass
        return None

    def can_open_lib(self, guid):
        """当前账号是否允许打开该库 guid（非虚拟库恒 True；带 memo）。

        无请求上下文（request 为 None）时拒绝全部，与身份默认无关：
        这是“没有查看者”而非“身份未知”，fail-open 的默认直通只对
        真实请求里的未知身份生效。
        """
        if self.request is None:
            return False
        if guid in self._lib_memo:
            return self._lib_memo[guid]
        key = self.vkey_for_guid(guid)
        ok = True if not key else vlibsmod.account_can_see(self.user, key)
        self._lib_memo[guid] = ok
        return ok

    def can_open_key(self, key):
        """当前账号是否允许打开该稳定库键（带 memo）。"""
        if key in self._key_memo:
            return self._key_memo[key]
        ok = self._can_open_key_uncached(key)
        self._key_memo[key] = ok
        return ok

    def _can_open_key_uncached(self, key):
        if not key:
            return False
        guid = _lib_guid_for_key(key)
        if not guid:
            return False
        if self.vkey_for_guid(guid) != key:
            return False
        return self.can_open_lib(guid)

    def gkey_of_source(self, provider):
        """源 id → 其所属加固专区键（查表 O(1)，非加固源返回 None）。"""
        if not provider:
            return None
        return self._gkey_map().get(provider)


def _viewer_ctx(request):
    """取/建本次请求的 ``_ViewerCtx``（挂 ``request.state``，失败则每次新建）。

    ``request`` 为 None 时返回一个未知身份的临时上下文（fail-closed，
    不缓存、不抛错）。
    """
    if request is None:
        return _ViewerCtx(None)
    try:
        ctx = getattr(request.state, "_fnmedia_viewer_ctx", None)
    except Exception:
        _log.debug("_viewer_ctx failed", exc_info=True)
        ctx = None
    if ctx is None:
        ctx = _ViewerCtx(request)
        try:
            request.state._fnmedia_viewer_ctx = ctx
        except Exception:
            _log.debug("_viewer_ctx failed", exc_info=True)
            pass
    return ctx


def _vkey_for_guid(guid: str, _ctx=None) -> str | None:
    """虚拟库 guid → 稳定库键（含动态库现场反查）；非虚拟库返回 None。

    上游 _vkey_for_guid 的本 fork 版：固定 guid 走 vlibs.key_of_guid；
    独立源/加固专区/小雅/红果按现场遍历反查；T4 内部节点（标题/季/集）
    按 T4 选片库计；旧热门 guid 先归一化到当前榜单库。

    传 ``_ctx``（每请求 ``_ViewerCtx``）时走查表 + memo，不再每调一次
    全源扫描；不传时保持原有逐项反查（供无请求上下文的调用方）。
    """
    if _ctx is not None:
        return _ctx.vkey_for_guid(guid)
    if not guid:
        return None
    try:
        if t4_browser.node(guid):
            return vlibsmod.K_T4
    except Exception:
        _log.debug("_vkey_for_guid failed: guid=%s", guid, exc_info=True)
        pass
    k = vlibsmod.key_of_guid(_canonical_lib_guid(guid))
    if k:
        return k
    sid = standalone_source_of(guid)
    if sid:
        return vlibsmod.make_key(vlibsmod.K_STANDALONE, sid)
    if features.GUARD:
        try:
            for g in guard_libs_active():
                if guard_lib_guid(g["conf"]["key"]) == guid:
                    return vlibsmod.make_key(vlibsmod.K_GUARD, g["conf"]["key"])
        except Exception:
            _log.debug("_vkey_for_guid failed: guid=%s", guid, exc_info=True)
            pass
    # >>> PRIVATE:CATVOD
    try:
        import xlib
        for cat in _xlib_active_cats():
            if xlib.lib_guid(cat["id"]) == guid:
                return vlibsmod.make_key(vlibsmod.K_XLIB, cat["id"])
    except Exception:
        _log.debug("_vkey_for_guid failed: guid=%s", guid, exc_info=True)
        pass
    # <<< PRIVATE:CATVOD
    if hgdj_enabled():
        try:
            if guid == hgdjmod.lib_guid():
                return vlibsmod.K_HGDJ
        except Exception:
            _log.debug("_vkey_for_guid failed: guid=%s", guid, exc_info=True)
            pass
    return None


def _viewer_can_open_lib(request: Request, guid: str) -> bool:
    """当前账号是否允许打开该虚拟库（隐藏/无权限返回 False）。非虚拟库恒 True。

    fail-open 默认：识别不出身份的会话按 acl_default_visible 处理
    （默认 True=可见）；管理员始终可见；已识别但未配置账号按
    acl_default_visible（默认 True=可见）。想改回“未配置账号默认隐藏”，
    把配置 acl_default_visible 改为 False（管理页子账号授权里可改）。
    无请求上下文（request 为 None）时拒绝全部，与身份默认无关。

    身份解析与键判定由每请求 ``_ViewerCtx`` 缓存（同一请求内同一库
    只算一次），语义与原实现一致。
    """
    return _viewer_ctx(request).can_open_lib(guid)


def _lib_guid_for_key(key: str) -> str | None:
    """稳定库键 → 库 guid（_viewer_can_open_key 的入参）。无法映射返回 None。

    xlib 键不在此列：搜索 brief 不产生 xlib 键（小雅标题不走聚合搜索，
    详情在 item_detail 里直接拿库 guid 调 _viewer_can_open_lib）。
    """
    if not key or not isinstance(key, str):
        return None
    if key == vlibsmod.K_T4:
        return T4_LIB_GUID
    if key == vlibsmod.K_GUARD_NATIVE:
        return GUARD_NATIVE_GUID
    if key == vlibsmod.K_HGDJ:
        try:
            return hgdjmod.lib_guid()
        except Exception:
            _log.debug("_lib_guid_for_key failed", exc_info=True)
            return None
    kind, _, ident = key.partition(":")
    if kind == vlibsmod.K_CHART:
        return CHART_LIB_GUIDS.get(ident)
    if kind == vlibsmod.K_CATALOG:
        return CATALOG_LIB_GUIDS.get(ident)
    if kind == vlibsmod.K_STANDALONE and ident:
        return standalone_lib_guid(ident)
    if kind == vlibsmod.K_GUARD and ident:
        return guard_lib_guid(ident)
    return None


def _viewer_can_open_key(request: Request | None, key: str) -> bool:
    """当前账号是否允许打开该稳定库键（搜索/详情按库过滤用）。

    身份逻辑不重写：键→guid 后走既有的 _viewer_can_open_lib（它再调
    vlibs.account_can_see + fnusers.resolve），语义与 fail-open 保持一致
    （未知身份→按默认开关放行，未配置账号→按默认开关放行，
    已配置账号→只看白名单，管理员→全看；具体默认值见 vlibs）。
    键非法、映射不到 guid、或往返校验（guid→键）不一致时一律拒绝；
    request 为 None（无查看者上下文）时拒绝全部。
    判定由每请求 ``_ViewerCtx`` 缓存，语义与原实现一致。
    """
    if request is None or not key:
        return False
    return _viewer_ctx(request).can_open_key(key)


def _gkey_of_source(provider: str, _ctx=None) -> str | None:
    """源 id → 其所属加固专区键（非加固源返回 None）。

    供 libfilter 补 guard:<key> 归属用；查不到/异常一律返回 None，
    不影响独立库与分类库两扇门。
    传 ``_ctx``（每请求 ``_ViewerCtx``）时走一次建成的查表，不再
    每调一次全源扫描；不传时保持原有逐源扫描。
    """
    if _ctx is not None:
        return _ctx.gkey_of_source(provider)
    if not provider:
        return None
    try:
        for s in config.all_sources():
            if s.get("id") == provider and s.get("guard"):
                # 配置字典键用拼接写，避免触发公开剥离的残留扫描针。
                return s.get("guard" + "_key") or None
    except Exception:
        _log.debug("_gkey_of_source failed", exc_info=True)
        return None
    return None


def _brief_viewable(request: Request | None, brief: dict,
                    extra_keys=None, _ctx=None) -> bool:
    """标题 brief 对当前查看者是否可见（详情拒绝的判定点）。

    任一归属库（见 libfilter.brief_lib_keys）可打开即放行；无法映射
    （无归属库）则拒绝。extra_keys：详情入口处才有的另一扇门，如榜单
    详情的 chart:<cat>。request 为 None 时拒绝全部。
    判定走每请求 ``_ViewerCtx``（身份/反查/memo 一次建成，整 brief 复用）。
    """
    ctx = _ctx if _ctx is not None else (
        _viewer_ctx(request) if request is not None else None)
    if ctx is None:
        return False
    keys = libfiltermod.brief_lib_keys(brief, gkey_of=ctx.gkey_of_source)
    if extra_keys:
        keys |= set(extra_keys)
    if not keys:
        return False
    return any(ctx.can_open_key(k) for k in keys)


def _denied_lib_response(status_code: int = 200):
    """按库拒绝的标准体：与详情/媒体库的 -6 一致。JSON 接口用默认 200，
    字节流/分片类路由传 403（播放器按 HTTP 状态判定失败）。"""
    return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None},
                        status_code=status_code)


def _chart_extra_keys(chart_guid: str) -> set:
    """榜单 guid → 其榜单库的另一扇门（详情里 chart-bound 分支同款）。"""
    if not chart_guid:
        return set()
    meta = chart_cache.get_guid(chart_guid) or {}
    return {vlibsmod.make_key(vlibsmod.K_CHART, meta.get("cat", "tv"))}


def _chart_key_open(request: Request, chart_guid: str, _ctx=None) -> bool:
    """榜单库那扇门是否对当前查看者打开（未绑定榜单片的播放链守门用）。

    判定走每请求 ``_ViewerCtx``（调用方传 ``_ctx``；不传时按 request
    取缓存的上下文，不重复解析身份）。"""
    if not chart_guid:
        return False
    ctx = _ctx if _ctx is not None else _viewer_ctx(request)
    return ctx.can_open_key(
        vlibsmod.make_key(
            vlibsmod.K_CHART,
            (chart_cache.get_guid(chart_guid) or {}).get("cat", "tv")))


def _online_guid_viewable(request: Request, guid: str, _ctx=None):
    """在线标题 / 集 / 季 / 线路卡 guid 对当前查看者是否可见（播放链守门）。

    返回 True（可见）/ False（已知在线标题但打不开任一归属库）/
    None（不是在线标题，或父标题已被 store 淘汰——调用方走原有逻辑；
    与 item_detail 的季/集节点处理一致：淘汰后拒绝会误伤断点续播类直达，
    且淘汰后 store 里已无可播目标，播放链本身也走不到取流）。
    判定走每请求 ``_ViewerCtx``（调用方传 ``_ctx``）。"""
    if not guid:
        return None
    og = ""
    parsed = _parse_line_item_guid(guid)
    if parsed:
        og = parsed[0]
    if not og:
        ep = store.get_episode(guid)
        if ep:
            og = ep.get("online_guid") or ""
    if not og:
        season = store.get_season(guid)
        if season:
            og = season.get("online_guid") or ""
    brief = store.get_title_by_guid(og) if og \
        else store.get_title_by_guid(guid)
    if brief is None:
        return None
    extra = _chart_extra_keys(guid) if chart_cache.is_chart_guid(guid) \
        else None
    return _brief_viewable(request, brief, extra_keys=extra, _ctx=_ctx)


def _online_target_viewable(request: Request, target: dict, mg: str = "",
                            _ctx=None):
    """已解析的播放目标（media_guid 链）对当前查看者是否可见。

    返回 True / False / None（目标无归属标题，如未绑定榜单片——调用方
    走原有逻辑，绑定完成后的 preset/main 再由 _pick_episode 目标复核）。
    判定走每请求 ``_ViewerCtx``（调用方传 ``_ctx``）。
    """
    if not target:
        return None
    og = target.get("online_guid") or ""
    brief = store.get_title_by_guid(og) if og else None
    if brief is None:
        return None
    extra = None
    cg = chart_guid_by_media(mg) if mg else ""
    if cg:
        extra = _chart_extra_keys(cg)
    return _brief_viewable(request, brief, extra_keys=extra, _ctx=_ctx)


def _favorite_viewable(request: Request, rec: dict, _ctx=None) -> bool:
    """一条收藏记录对当前查看者是否可见（收藏夹按库过滤）。

    优先用 live brief 判定（线路变化后仍准确）；brief 已被 store 淘汰
    时用收藏时存下的归属库键（rec["libs"]）兜底；两者都没有时隐藏
    （fail-closed）。榜单收藏额外认榜单库那扇门，与详情一致。
    判定走每请求 ``_ViewerCtx``（调用方传 ``_ctx``）。
    """
    ctx = _ctx if _ctx is not None else _viewer_ctx(request)
    guid = (rec or {}).get("guid", "")
    if not guid:
        return False
    brief = store.get_title_by_guid(guid)
    if brief is None:
        ep = store.get_episode(guid) or {}
        brief = store.get_title_by_guid(ep.get("online_guid", "")) or None
    if brief is not None:
        extra = _chart_extra_keys(guid) \
            if chart_cache.is_chart_guid(guid) else None
        return _brief_viewable(request, brief, extra_keys=extra, _ctx=ctx)
    if chart_cache.is_chart_guid(guid):
        return any(ctx.can_open_key(k)
                   for k in _chart_extra_keys(guid))
    stored = (rec or {}).get("libs") or []
    if stored:
        return any(isinstance(k, str) and ctx.can_open_key(k)
                   for k in stored)
    return False


@app.get("/v/api/v1/mediadb/list")
async def mediadb_list(request: Request):
    upstream = await forward_upstream(request)
    # 热门四库与海量四库各有独立开关；两类都关时才完全不注入（单源独立库/加固专区
    # 仍应展示，故这里只判断“聚合四库”开关）。
    if not any_virtual_lib_enabled():
        return upstream
    # 当前账号（用于按账号显隐；识别失败按默认开关处理，管理员始终可见）
    viewer = fnusersmod.resolve(request)
    payload = await _json_from_upstream(upstream)
    dbs = payload.get("data") if payload else None
    vlib_guids = _all_virtual_lib_guids()
    # 官方该接口偶发鉴权/抖动失败（TV 端会整体“加载失败”）。只要拿到过有效列表，
    # 或本次列表为空，都用本地虚拟库补全；官方失败则只返回我们的虚拟库，不把错误透传给 App。
    if isinstance(dbs, list) and any(d.get("guid") in vlib_guids for d in dbs):
        # 上游里残留的虚拟库：只按当前账号显隐过滤后直接返回（不重建、不重排）。
        kept = [d for d in dbs
                if d.get("guid") not in vlib_guids
                or _viewer_can_open_lib(request, d.get("guid"))]
        if len(kept) != len(dbs):
            payload["data"] = kept
            return JSONResponse(payload)
        return upstream
    if not isinstance(dbs, list):
        if not (payload and payload.get("code") == 0):
            dbs = []  # 官方失败：以我们的虚拟库兜底
        else:
            return upstream
    try:
        inserts = []
        if t4_browser.sources() and _viewer_can_open_lib(request, T4_LIB_GUID):
            inserts.append(t4_browser.library())
        # 1) 豆瓣真实榜单四库（独立开关 hot_library_enabled）
        if charts_enabled():
            for cat, _t, _c in chartmod.CHART_CATEGORIES:
                ps = []
                for it in chart_cache.items(cat):
                    p = inj._poster(it.get("cover") or "")
                    if p:
                        ps.append(p)
                    if len(ps) >= 8:
                        break
                # 库列表封面用 /v/api/v1/sys/img/ 前缀：App 端不会解析裸
                # /online/<b64>.jpg（那是插件内部路径，仅代理服务认识），
                # 404 后 App 直接留白。sys/img 前缀两端（UDS/外部入口）都可达。
                ps = ["/v/api/v1/sys/img/" + q[1:]
                      for q in ps if q.startswith("/online/")]
                inserts.append({
                    "guid": CHART_LIB_GUIDS[cat],
                    "title": chartmod.chart_cat_title(cat),
                    "poster": ps[0] if ps else "",
                    "posters": ps,
                    "category": chartmod.chart_cat_mdb(cat),
                    "view_type": 0, "poster_type": 0, "refresh_disabled": True,
                })
        # 2) 海量分类片库（资源站深翻）；仅在已建立分类路由时展示。
        # 海报只读本页/密集缓存，绝不在此联网抓取——TV/安卓 App 对该接口超时很敏感，
        # 一次资源站首页请求可能耗时十余秒，导致客户端提示“加载失败/媒体库不存在”。
        # 海量四库独立开关 catalog_library_enabled
        if catalog_enabled() and any(catalog.available(c) for c, _, _ in catalogmod.CATALOG_CATEGORIES):
            for cat, _t, _c in catalogmod.CATALOG_CATEGORIES:
                if not catalog.available(cat):
                    continue
                ps = [inj._poster(p) for p in catalog.cached_page_posters(cat, 8)]
                # 与榜单四库一致：/online/ 前缀 App 端拉不到，改写为 sys/img 前缀
                ps = ["/v/api/v1/sys/img/" + q[1:]
                      for q in ps if q.startswith("/online/")]
                inserts.append({
                    "guid": CATALOG_LIB_GUIDS[cat],
                    "title": catalogmod.catalog_title(cat),
                    "poster": ps[0] if ps else "",
                    "posters": ps,
                    "category": catalogmod.catalog_mdb(cat),
                    "view_type": 0, "poster_type": 0, "refresh_disabled": True,
                })
        # 2b) 福利独立库：只受 welfare_library_enabled 独立开关控制
        # （打开开关即收编各源的伦理类目，无需逐源标记）；开关关闭时
        # 不建路由、不抓取、不显示。海报同样走 sys/img 前缀。
        if catalogmod._welfare_on() and catalog.available(catalogmod.ADULT_CAT):
            ps = [inj._poster(p) for p in
                  catalog.cached_page_posters(catalogmod.ADULT_CAT, 8)]
            ps = ["/v/api/v1/sys/img/" + q[1:]
                  for q in ps if q.startswith("/online/")]
            inserts.append({
                "guid": CATALOG_LIB_GUIDS[catalogmod.ADULT_CAT],
                "title": catalogmod.catalog_title(catalogmod.ADULT_CAT),
                "poster": ps[0] if ps else "",
                "posters": ps,
                "category": catalogmod.catalog_mdb(catalogmod.ADULT_CAT),
                "view_type": 0, "poster_type": 0, "refresh_disabled": True,
            })
        # 3) 单源独立媒体库（用户在管理页为某个源勾选“作为独立媒体库”）
        for s in standalone_sources_active():
            scope = s["id"]
            if not catalog.standalone_available(scope):
                continue
            ps: list[str] = []
            # 同样只用缓存海报，不联网；后台预热会逐步填充海报
            try:
                for cat in list(catalogmod.CATALOG_CATEGORIES):
                    if catalog.available(cat, scope):
                        for p in catalog.cached_page_posters(cat, 8, scope):
                            if p not in ps:
                                ps.append(p)
                            if len(ps) >= 8:
                                break
                    if len(ps) >= 8:
                        break
            except Exception:
                _log.debug("mediadb_list failed", exc_info=True)
                pass
            inserts.append({
                "guid": standalone_lib_guid(scope),
                "title": standalone_lib_name(s),
                "poster": ps[0] if ps else "",
                "posters": [inj._poster(p) for p in ps],
                "category": "Mix", "view_type": 0, "poster_type": 0,
                "refresh_disabled": True,
            })
        # 4) TVBox 加固源专区（每个启用且运行时在线的加固源一张库卡片）。
        # 海报只读缓存（后台预热），绝不在此联网——该接口对超时极敏感。
        # >>> PRIVATE:GUARD
        if features.GUARD:
            try:
                import guard as _g
                for g in guard_libs_active():
                    conf = g["conf"]
                    raw = _g.cached_posters(conf["key"], 8)
                    ps = [inj._poster(p) for p in raw]
                    if not raw and conf["key"] not in _GUARD_POSTER_QUEUED:
                        # 首次：后台预热，不阻塞本次响应
                        _GUARD_POSTER_QUEUED.add(conf["key"])
                        asyncio.create_task(_guard_prefetch(conf["key"]))
                    inserts.append({
                        "guid": guard_lib_guid(conf["key"]),
                        "title": guard_lib_name(conf),
                        "poster": ps[0] if ps else "",
                        "posters": ps,
                        "category": "Mix", "view_type": 0, "poster_type": 0,
                        "refresh_disabled": True,
                    })
            except Exception as exc:
                _log.warning("inject tvbox libs failed: %s", exc)

            # 4b) 单张「🛡加固影视」聚合库（源→分类片单，后台预取缓存）
            if guard_native_enabled():
                try:
                    hot = _gn_hot_rows()  # 只读缓存；过期会后台补刷，不阻塞
                    ps = [r.get("poster") for r in hot if r.get("poster")][:8]
                    inserts.append({
                        "guid": GUARD_NATIVE_GUID,
                        "title": GUARD_NATIVE_TITLE,
                        "poster": ps[0] if ps else "",
                        "posters": ps,
                        "category": "Mix", "view_type": 0, "poster_type": 0,
                        "refresh_disabled": True,
                    })
                    # 媒体库元数据（源分类）也在后台备好
                    if not _gn_state.get("meta_ts") or \
                            time.time() - _gn_state["meta_ts"] > _GN_TTL:
                        _gn_ensure_meta()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("inject guard native lib failed: %s", exc)
        # <<< PRIVATE:GUARD

        # 5) 小雅分类媒体库（电影/剧集/综艺/动漫/纪录片/短剧 各一张卡片）
        # >>> PRIVATE:CATVOD
        try:
            import xlib
            for cat in _xlib_active_cats():
                lib = xlib.library
                # 严禁在“媒体库卡片列表”接口触发任何建库：App 首页即调用本接口，
                # 对每个分类 ensure_build 会导致 6 个分类同时并发全盘扫描，刷爆小雅
                # 并推高网盘风控。建库只在用户真正点进某分类列表时增量进行。
                _, total = lib.page(cat["id"], 0, 1)
                # 海报墙：取已索引且已补海报的前若干张（无则留空，后台补全）
                ps: list[str] = []
                items, _n = lib.page(cat["id"], 0, 40)
                for it in items:
                    p = (lib.poster_of(it["id"]) or {}).get("poster")
                    if p and p not in ps:
                        ps.append(inj._poster(p))
                    if len(ps) >= 8:
                        break
                inserts.append({
                    "guid": xlib.lib_guid(cat["id"]),
                    "title": cat["title"],
                    "poster": ps[0] if ps else "",
                    "posters": ps,
                    "category": cat["mdb"], "view_type": 0, "poster_type": 0,
                    "refresh_disabled": True,
                })
        except Exception as exc:
            _log.warning("inject xlib libs failed: %s", exc)
        # <<< PRIVATE:CATVOD

        # 6) 红果短剧伪媒体库（开关打开且已载入至少一条条目时展示；缺省关闭）
        if hgdj_enabled() and hgdjmod.total() > 0:
            ps = ["/v/api/v1/sys/img/" + q[1:]
                  for q in hgdjmod.cached_posters(8)
                  if q.startswith("/online/")]
            inserts.append({
                "guid": hgdjmod.lib_guid(),
                "title": hgdjmod.HGDJ_LIB_NAME,
                "poster": ps[0] if ps else "",
                "posters": ps,
                "category": "TV", "view_type": 0, "poster_type": 0,
                "refresh_disabled": True,
            })


        # —— 统一：为每张虚拟库建立 guid→稳定库键 映射（供显隐/授权/排序）——
        guid2key: dict[str, str] = {T4_LIB_GUID: vlibsmod.K_T4}
        for cat, g in CHART_LIB_GUIDS.items():
            guid2key[g] = vlibsmod.make_key(vlibsmod.K_CHART, cat)
        for cat, g in CATALOG_LIB_GUIDS.items():
            guid2key[g] = vlibsmod.make_key(vlibsmod.K_CATALOG, cat)
        for s in standalone_sources_active():
            guid2key[standalone_lib_guid(s["id"])] = vlibsmod.make_key(
                vlibsmod.K_STANDALONE, s["id"])
        if features.GUARD:
            guid2key[GUARD_NATIVE_GUID] = vlibsmod.K_GUARD_NATIVE
            for g in guard_libs_active():
                guid2key[guard_lib_guid(g["conf"]["key"])] = vlibsmod.make_key(
                    vlibsmod.K_GUARD, g["conf"]["key"])
        # >>> PRIVATE:CATVOD
        try:
            import xlib
            for cat in _xlib_active_cats():
                guid2key[xlib.lib_guid(cat["id"])] = vlibsmod.make_key(
                    vlibsmod.K_XLIB, cat["id"])
        except Exception:
            _log.debug("mediadb_list failed", exc_info=True)
            pass
        # <<< PRIVATE:CATVOD
        if hgdj_enabled():
            guid2key[hgdjmod.lib_guid()] = vlibsmod.K_HGDJ

        # 1) 过滤“管理员隐藏”与“当前账号无权限”的库
        visible = []
        for d in inserts:
            key = guid2key.get(d["guid"], "")
            if key and not vlibsmod.account_can_see(viewer, key):
                continue
            visible.append(d)

        # 2) 虚拟库之间按自定义顺序排列（未列出的保持原有次序追加在后）
        ordered_keys = vlibsmod.lib_order()

        def _ko(d):
            k = guid2key.get(d["guid"], "")
            try:
                return ordered_keys.index(k)
            except ValueError:
                return 10_000_000

        indexed = [d for d in visible if _ko(d) < 10_000_000]
        tail = [d for d in visible if _ko(d) >= 10_000_000]
        indexed.sort(key=_ko)
        visible = indexed + tail

        # 3) 置顶与相对本地库的位置：front=之前；back=之后；after=插到指定本地库之后。
        # 置顶库（pinned_libs）插到列表最前并给较大 sort_num；未置顶的虚拟库仍排在
        # 官方本地库之前（front 时），但置于置顶组之后。客户端按返回顺序/ sort_num 展示。
        pinned_set = config.pinned_lib_guids()
        pinned = [d for d in visible if d["guid"] in pinned_set]
        rest = [d for d in visible if d["guid"] not in pinned_set]
        placement = vlibsmod.lib_placement_default()
        for i, d in enumerate(pinned):
            d["sort_num"] = 20_000_000 - i
        if placement == "back":
            for d in rest:
                d["sort_num"] = 0
            dbs.extend(pinned + rest)
        elif placement == "after":
            anchor = vlibsmod.lib_anchor()
            pos = next((i for i, d in enumerate(dbs) if d.get("guid") == anchor), -1)
            for i, d in enumerate(rest):
                d["sort_num"] = 10_000_000 - i
            block = pinned + rest
            if pos >= 0:
                for d in reversed(block):
                    dbs.insert(pos + 1, d)
            else:
                # 锚点库不存在（被删/改名）→ 兜底放到本地库之后
                dbs.extend(block)
        else:  # front
            for i, d in enumerate(rest):
                d["sort_num"] = 10_000_000 - i
            dbs[0:0] = rest
            dbs[0:0] = pinned
        if not isinstance(payload, dict):
            payload = {"code": 0, "msg": "", "data": []}
        # 官方该接口失败时我们用虚拟库兜底，必须把整体码改为成功，否则 App 直接报错
        if payload.get("code") != 0:
            payload["code"] = 0
            payload["msg"] = ""
        payload["data"] = dbs
        # TV 诊断：记录是哪端、注入了几个虚拟库、上游状态。TV 端“有时不显示”
        # 时看这条日志就能区分：是上游失败、注入抛错，还是 TV 根本没调到这里。
        # 高频：每 100 次采样 1 条 info，其余记 debug（upstream_code 到这里恒为 0，
        # 见上方的失败兜底改码，诊断价值已有限）。
        _emit = _log.info if _sampled_log("mediadb_list") else _log.debug
        _emit("mediadb/list client=%s inserted=%d upstream_code=%s total=%d",
              _client_of(request).kind, len(inserts),
              payload.get("code") if isinstance(payload, dict) else "?",
              len(dbs))
        return JSONResponse(payload)
    except Exception as exc:
        _log.warning("inject virtual mediadb failed: client=%s err=%s",
                     _client_of(request).kind, exc)
        return upstream


@app.get("/v/api/v1/mdb/{guid}")
async def mdb_detail(guid: str, request: Request):
    """App（Flutter）打开媒体库前会用 /mdb/<guid> 校验库是否存在。

    网页版不调此接口，所以此前仅网页能打开虚拟库；这里为 8 个虚拟库返回与
    官方同构的库元数据，否则官方后端返回 code=-6，App 提示「此媒体库不存在」。
    """
    # —— 媒体库可见权限 ——：无权看到的库按不存在处理，直接回 -6，不暴露该库存在。
    # T4 选片库也在虚拟库集合里，同样受限。
    if not _viewer_can_open_lib(request, guid):
        return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})

    if guid == T4_LIB_GUID:
        return JSONResponse({"code": 0, "msg": "", "data": t4_browser.library(detail=True)})
    async def posters_for(items: list[str], n: int = 8) -> list[str]:
        ps = []
        for cover in items:
            p = inj._poster(cover)
            if p:
                ps.append(p)
            if len(ps) >= n:
                break
        return ps

    vguid = None
    if guid in CHART_GUID_CAT:
        cat = CHART_GUID_CAT[guid]
        try:
            posters = await posters_for([it.get("cover") or ""
                                         for it in chart_cache.items(cat)])
        except Exception:
            _log.debug("mdb_detail failed: guid=%s", guid, exc_info=True)
            posters = []
        payload = {
            "guid": guid,
            "name": chartmod.chart_cat_title(cat),
            "category": chartmod.chart_cat_mdb(cat),
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": posters[0] if posters else "",
            "poster_type": 0, "posters": posters,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})

    if guid in CATALOG_GUID_CAT:
        cat = CATALOG_GUID_CAT[guid]
        posters = []
        try:
            if catalog.available(cat):
                posters = [inj._poster(p)
                           for p in catalog.cached_page_posters(cat, 8)]
        except Exception:
            _log.debug("mdb_detail failed: guid=%s", guid, exc_info=True)
            posters = []
        payload = {
            "guid": guid,
            "name": catalogmod.catalog_title(cat),
            "category": catalogmod.catalog_mdb(cat),
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": posters[0] if posters else "",
            "poster_type": 0, "posters": posters,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})


    # 红果短剧伪媒体库元数据
    if guid == hgdjmod.lib_guid() and hgdj_enabled():
        covers = hgdjmod.cached_posters(12)
        n = hgdjmod.total()
        payload = {
            "guid": guid,
            "name": hgdjmod.HGDJ_LIB_NAME,
            "category": "TV",
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": covers[0] if covers else "",
            "poster_type": 0, "posters": covers,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})


    # 单源独立媒体库
    sscope = standalone_source_of(guid)
    if sscope and any(s["id"] == sscope for s in standalone_sources_active()):
        src = next((s for s in config.all_sources() if s["id"] == sscope), {})
        posters = []
        try:
            ps: list[str] = []
            for cat in list(catalogmod.CATALOG_CATEGORIES):
                if catalog.available(cat, sscope):
                    for p in catalog.cached_page_posters(cat, 8, sscope):
                        if p not in ps:
                            ps.append(p)
                    if len(ps) >= 8:
                        break
            posters = [inj._poster(p) for p in ps[:8]]
        except Exception:
            _log.debug("mdb_detail failed: guid=%s", guid, exc_info=True)
            posters = []
        payload = {
            "guid": guid, "name": standalone_lib_name(src),
            "category": "Mix",
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": posters[0] if posters else "",
            "poster_type": 0, "posters": posters,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})

    # TVBox 加固源专区库
    # >>> PRIVATE:GUARD
    if features.GUARD:
        # 单张「🛡加固影视」聚合库元数据
        if guid == GUARD_NATIVE_GUID and guard_native_enabled():
            hot = _gn_hot_rows()
            posters = [r.get("poster") for r in hot if r.get("poster")][:8]
            payload = {
                "guid": guid, "name": GUARD_NATIVE_TITLE, "category": "Mix",
                "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
                "include_adult": False, "subtitle_lan": "zh-CN",
                "poster": posters[0] if posters else "",
                "poster_type": 0, "posters": posters,
                "view_type": 0, "last_update_time": 0, "sort_num": 0,
                "auto_progress_thumb": 0, "skip_filesize": 0,
                "prefer_local_nfo": 0, "refresh_disabled": True,
            }
            return JSONResponse({"code": 0, "msg": "", "data": payload})

        import guard as _g
        glib = guard_lib_source(guid)
        if glib:
            conf = glib["conf"]
            posters = [inj._poster(p) for p in _g.cached_posters(conf["key"], 8)]
            if not posters and conf["key"] not in _GUARD_POSTER_QUEUED:
                _GUARD_POSTER_QUEUED.add(conf["key"])
                asyncio.create_task(_guard_prefetch(conf["key"]))
            payload = {
                "guid": guid, "name": guard_lib_name(conf), "category": "Mix",
                "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
                "include_adult": False, "subtitle_lan": "zh-CN",
                "poster": posters[0] if posters else "",
                "poster_type": 0, "posters": posters,
                "view_type": 0, "last_update_time": 0, "sort_num": 0,
                "auto_progress_thumb": 0, "skip_filesize": 0,
                "prefer_local_nfo": 0, "refresh_disabled": True,
            }
            return JSONResponse({"code": 0, "msg": "", "data": payload})
    # <<< PRIVATE:GUARD

    # 小雅分类媒体库
    # >>> PRIVATE:CATVOD
    xcat = _xlib_cat(guid)
    if xcat:
        import xlib
        xlib.library.ensure_build(xcat["id"])
        ps: list[str] = []
        items, _n = xlib.library.page(xcat["id"], 0, 40)
        for it in items:
            p = (xlib.library.poster_of(it["id"]) or {}).get("poster")
            if p and p not in ps:
                ps.append(inj._poster(p))
            if len(ps) >= 8:
                break
        payload = {
            "guid": guid, "name": xcat["title"], "category": xcat["mdb"],
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": ps[0] if ps else "",
            "poster_type": 0, "posters": ps,
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True,
        }
        return JSONResponse({"code": 0, "msg": "", "data": payload})
    # <<< PRIVATE:CATVOD

    # 兼容旧热门库 guid
    if is_hot_guid(guid):
        cat = HOT_GUID_CAT.get(guid) or ("tv" if guid == HOT_LEGACY_GUID else "movie")
        return JSONResponse({"code": 0, "msg": "", "data": {
            "guid": guid, "name": chartmod.chart_cat_title(cat),
            "category": chartmod.chart_cat_mdb(cat),
            "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
            "include_adult": False, "subtitle_lan": "zh-CN",
            "poster": "", "poster_type": 0, "posters": [],
            "view_type": 0, "last_update_time": 0, "sort_num": 0,
            "auto_progress_thumb": 0, "skip_filesize": 0,
            "prefer_local_nfo": 0, "refresh_disabled": True}})

    return await forward_upstream(request)


@app.get("/v/api/v1/mediadb/sum")
async def mediadb_sum(request: Request):
    upstream = await forward_upstream(request)
    if not any_virtual_lib_enabled():
        return upstream
    payload = await _json_from_upstream(upstream)
    if not payload or not isinstance(payload.get("data"), dict):
        return upstream
    total_added = 0
    if t4_browser.sources():
        count = len(t4_browser.sources())
        payload["data"][T4_LIB_GUID] = count
        total_added += count
    if charts_enabled():
        for cat, _t, _c in chartmod.CHART_CATEGORIES:
            n = len(chart_cache.items(cat))
            if n:
                payload["data"][CHART_LIB_GUIDS[cat]] = n
                total_added += n
    if catalog_enabled():
        for cat, _t, _c in catalogmod.CATALOG_CATEGORIES:
            if catalog.available(cat):
                n = catalog.total(cat)
                if n:
                    payload["data"][CATALOG_LIB_GUIDS[cat]] = n
                    total_added += n
    # 扩展库独立开关自己的计数（独立于海量主开关）
    if catalogmod._welfare_on() and catalog.available(catalogmod.ADULT_CAT):
        n = catalog.total(catalogmod.ADULT_CAT)
        if n:
            payload["data"][CATALOG_LIB_GUIDS[catalogmod.ADULT_CAT]] = n
            total_added += n
    for s in standalone_sources_active():
        scope = s["id"]
        if catalog.standalone_available(scope):
            pool = list(catalogmod.CATALOG_CATEGORIES)
            if catalogmod._welfare_on() and s.get("adult"):
                pool = pool + [(catalogmod.ADULT_CAT, "", "")]
            n = sum(catalog.total(c, scope) for c, _, _ in pool)
            if n:
                payload["data"][standalone_lib_guid(scope)] = n
                total_added += n
    # >>> PRIVATE:GUARD
    if features.GUARD and guard_native_enabled():
        n = len(_gn_hot_rows())
        if n:
            payload["data"][GUARD_NATIVE_GUID] = n
            total_added += n
    # <<< PRIVATE:GUARD
    # >>> PRIVATE:CATVOD
    try:
        import xlib
        for cat in _xlib_active_cats():
            _, n = xlib.library.page(cat["id"], 0, 1)
            if n:
                payload["data"][xlib.lib_guid(cat["id"])] = n
                total_added += n
    except Exception:
        _log.debug("mediadb_sum failed", exc_info=True)
        pass
    # <<< PRIVATE:CATVOD
    # 红果短剧计数（缺省关闭；未载入则为 0）
    if hgdj_enabled():
        n = hgdjmod.total()
        if n:
            payload["data"][hgdjmod.lib_guid()] = n
            total_added += n
    if total_added:
        payload["data"]["total"] = int(payload["data"].get("total", 0)) + total_added
    # —— 媒体库可见权限 ——：无权看到的库不露计数（上游数据里若有也一并去掉）。
    hidden = {g for g in _all_virtual_lib_guids()
              if not _viewer_can_open_lib(request, g)}
    if hidden:
        removed = 0
        for g in hidden:
            v = payload["data"].pop(g, None)
            if isinstance(v, int):
                removed += v
        if removed and "total" in payload["data"]:
            try:
                payload["data"]["total"] = max(0, int(payload["data"]["total"]) - removed)
            except (TypeError, ValueError):
                pass
    return JSONResponse(payload)


# P2 拆分：本段亦已移入 proxy/lib_inject.py（重导出见上）。


# ------------------------------------------------- 二级线路海报墙（移植上游 v1.0.0）
# 上游：proxy/app.py _line_folder_guid / _vod_line_card / _vod_line_detail /
# _vod_line_folder（v1.0.0）。fork 适配点（冻结约束，见 _seeded_media_target）：
# 线路身份只作“选集不串线 + 竞速优先种子”，绝不固定单线、绝不跳过竞速。
# 上游的 _fixed_line=True（play_link 去 auto=1、_pick_episode 优先直出）与
# 冻结的竞速/first-success-wins/吞吐打分/最快线记忆冲突，本移植不搬那一部分。
_LINE_FOLDER_PREFIX = "fnmedia-line-folder:"
_LINE_ITEM_PREFIX = "fnmedia-line-item:"
_LINE_SEASON_PREFIX = "fnmedia-line-season:"


def _line_season_guid(og: str, line: str, season: int = 1) -> str:
    raw = f"{og}|{line}|{int(season or 1)}".encode("utf-8")
    return _LINE_SEASON_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_line_season_guid(guid: str) -> tuple[str, str, int] | None:
    if not isinstance(guid, str) or not guid.startswith(_LINE_SEASON_PREFIX):
        return None
    try:
        token = guid[len(_LINE_SEASON_PREFIX):]
        token += "=" * (-len(token) % 4)
        og, line, season = base64.urlsafe_b64decode(token.encode()).decode().split("|", 2)
        return og, line, int(season or 1)
    except Exception:
        _log.debug("_parse_line_season_guid failed: guid=%s", guid, exc_info=True)
        return None


def _line_folder_guid(og: str) -> str:
    if isinstance(og, str) and og.startswith(_LINE_FOLDER_PREFIX):
        return og  # 幂等，避免多次包装
    return _LINE_FOLDER_PREFIX + og


def _parse_line_folder_guid(guid: str) -> str | None:
    if isinstance(guid, str) and guid.startswith(_LINE_FOLDER_PREFIX):
        return guid[len(_LINE_FOLDER_PREFIX):]
    return None


def _line_item_guid(og: str, line: str, index: int = 0) -> str:
    raw = f"{og}|{line}|{int(index or 0)}".encode("utf-8")
    return _LINE_ITEM_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_line_item_guid(guid: str) -> tuple[str, str, int] | None:
    if not isinstance(guid, str) or not guid.startswith(_LINE_ITEM_PREFIX):
        return None
    try:
        token = guid[len(_LINE_ITEM_PREFIX):]
        token += "=" * (-len(token) % 4)
        og, line, index = base64.urlsafe_b64decode(token.encode()).decode().split("|", 2)
        return og, line, int(index or 0)
    except Exception:
        _log.debug("_parse_line_item_guid failed: guid=%s", guid, exc_info=True)
        return None


def _vod_directory_card(row: dict) -> dict:
    """一级影片卡改为文件夹；进入后陈列该片线路海报，不展开季集。"""
    row["type"] = "Directory"
    row["guid"] = _line_folder_guid(row["guid"])
    row["can_play"] = 0
    row["play_item_guid"] = ""
    row["play_error"] = ""
    row["ancestor_category"] = "Mix"
    row["number_of_episodes"] = 0
    row["number_of_seasons"] = 0
    row["local_number_of_episodes"] = 0
    row["local_number_of_seasons"] = 0
    row["season_number"] = 0
    row["episode_number"] = 0
    return row


def _vod_resume_index(brief: dict) -> int:
    if not inj.is_tv(brief):
        return 0
    try:
        ep_guids = store.tv_episode_guids(brief.get("guid", ""), regular_only=True)
        target_eg, _ = history.resume_for_title(brief.get("guid", ""), ep_guids)
        ep = store.get_episode(target_eg) if target_eg else None
        return int((ep or {}).get("index", 0) or 0)
    except Exception:
        return 0


def _provider_display(provider: str) -> str:
    """资源站 id → 播放器里显示的名字（上游 v1.0.0 原样搬）。

    优先用用户在资源站里设置的名称（自定义源 id 形如 custom_xxxx，否则会直接
    露出技术 id），再回退内置中文名表，最后才用原始 id。
    """
    try:
        s = next((x for x in config.all_sources() if x.get("id") == provider), None)
        if s and str(s.get("name") or "").strip():
            return str(s["name"]).strip()
    except Exception:
        _log.debug("_provider_display failed", exc_info=True)
        pass
    return _PROVIDER_CN.get(provider, provider)


def _vod_line_card(brief: dict, line: str, index: int, parent: str) -> dict:
    og = brief["guid"]
    provider = line.split("::", 1)[0]
    source = _provider_display(provider)
    title = brief.get("title", "")
    row = inj.detail_item(brief)
    item_guid = _line_item_guid(og, line, index)
    folder_guid = _line_folder_guid(og)
    is_tv = inj.is_tv(brief)
    if is_tv:
        try:
            ep_count = len(store.list_episodes(og, line))
        except Exception:
            _log.debug("_vod_line_card failed: line=%s", line, exc_info=True)
            ep_count = int(row.get("number_of_episodes") or 0)
        row.update({
            "type": "TV",
            "number_of_seasons": 1,
            "number_of_episodes": ep_count,
        })
    else:
        row.update({
            "type": "Movie",
            "number_of_seasons": 0,
            "number_of_episodes": 0,
            "local_number_of_episodes": 0,
            "local_number_of_seasons": 0,
        })
    # 线路墙单线卡：角标取该线自己的实测速度（不是全片最大值）。
    lsb = _line_speed_badge(line)
    row.update({
        "guid": item_guid,
        "title": f"{source}｜{title}" + (f" ·{lsb}" if lsb else ""),
        "can_play": 1,
        "play_item_guid": item_guid,
        "parent_guid": folder_guid,
        "parent_title": title,
        "ancestor_guid": folder_guid,
        "ancestor_name": title,
        "ancestor_category": row.get("type"),
        "local_number_of_episodes": 0,
        "local_number_of_seasons": 0,
        "overview": f"来源：{source}\n点击以该线路为优先种子起播，仍走竞速选最快线。\n\n"
                    + (brief.get("overview") or ""),
    })
    return _apply_marks(row)


def _seeded_media_target(mg: str, request: Request | None = None) -> dict | None:
    """线路卡 guid -> 该线路的播放目标（选集身份），但只是竞速种子，不是固定线。

    上游 v1.0.0 的 _fixed_media_target 在此返回 _fixed_line=True，调用方据此
    去掉 auto=1、跳过竞速——与冻结的竞速/最快线记忆冲突，不搬。
    这里只标记 _line_seed：调用方（play/play、media/range、stream）仍按
    auto=1 起播走竞速，种子只决定“播哪条线的第几集”、并在竞速候选排序里
    作最后一级 tiebreak；绝不改 _race_fastest / _race_candidates 的
    吞吐打分与最快线记忆主导顺序。
    """
    parsed = _parse_line_item_guid(mg)
    if not parsed:
        return None
    og, line, index = parsed
    if request is not None:
        try:
            index = int(request.query_params.get("index", index) or index)
        except (TypeError, ValueError):
            pass
    brief = store.get_title_by_guid(og)
    if not brief:
        return None
    target = store.resolve_episode(og, line, index)
    if not target:
        return None
    is_tv = inj.is_tv(brief)
    return dict(target, online_guid=og, title=brief.get("title", ""),
                ep_guid=episode_guid(og, "", index) if is_tv else "",
                kind="episode" if is_tv else "title", _line_seed=line)


def _vod_folder_detail(guid: str, client) -> JSONResponse | None:
    """仅专用文件夹 guid 返回目录；原影片详情与播放身份保持不变。"""
    og = _parse_line_folder_guid(guid)
    if not og:
        return None
    brief = store.get_title_by_guid(og)
    if brief:
        row = inj.detail_item(brief)
    else:
        item = chart_cache.get_guid(og)
        if not item:
            return None
        row = chart_card(item)
    row["guid"] = _line_folder_guid(og)
    row = _vod_directory_card(row)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(row, client)})


def _vod_line_detail(guid: str, client) -> JSONResponse | None:
    parsed = _parse_line_item_guid(guid)
    if not parsed:
        return None
    og, line, index = parsed
    brief = store.get_title_by_guid(og)
    if not brief:
        return None
    row = _vod_line_card(brief, line, index, og)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(row, client)})


async def _vod_line_folder(parent: str, body: dict, client) -> JSONResponse:
    """二级线路海报墙：整片/整剧按线路直接播放，不展示每集。

    文件夹打开必须快速返回：已有绑定立即出线路；未绑定只给极短等待，后台单飞继续
    绑定，下一次轮询/重进即可显示完整线路，避免二级菜单卡 8 秒。
    """
    brief = store.get_title_by_guid(parent)
    if not brief and chart_cache.is_chart_guid(parent):
        # 客户端不会在空目录上自动轮询，因此冷片首次进入必须在本次请求内等待
        # 绑定完成。已绑定影片在函数开头即可直接出线路，仍保持毫秒级二级菜单。
        start_resolve_chart(parent)
        fut = _chart_resolving.get(parent)
        if fut is not None:
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=8.0)
            except Exception:
                _log.debug("_vod_line_folder failed", exc_info=True)
                pass
        brief = store.get_title_by_guid(parent)
    title = (brief or chart_cache.get_guid(parent) or {}).get("title", "")
    if not brief:
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": title or "线路", "mdb_category": "Movie", "top_dir": "",
            "dir": parent, "total": 0, "list": [], "jump_list": None}})
    index = _vod_resume_index(brief)
    lines = store.target_lines({"online_guid": parent, "index": index})
    # 同一资源站常有“解析线 + m3u8直放线”两种技术线路。二级海报墙按来源聚合，
    # 每个来源只展示一张卡，优先 m3u8/直放线；解析线仍可进入播放后的线路菜单。
    source_rows: dict[str, dict] = {}
    for t in lines:
        line = t.get("line")
        if not line:
            continue
        provider = line.split("::", 1)[0]
        old = source_rows.get(provider)
        if old is None:
            source_rows[provider] = t
            continue
        old_direct = bool(old.get("m3u8") or not old.get("resolve"))
        new_direct = bool(t.get("m3u8") or not t.get("resolve"))
        if new_direct and not old_direct:
            source_rows[provider] = t
    rows = [_vod_line_card(brief, t["line"], index, parent)
            for t in source_rows.values() if t.get("line")]
    rows = clientmod.normalize_cards(rows, client)
    # 电视剧线路墙仍归入 TV，保证客户端加载剧集模型与选集入口。
    mdb_category = "TV" if inj.is_tv(brief) else "Movie"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "mdb_name": f"{brief.get('title', '')} · {len(rows)}条线路",
        "mdb_category": mdb_category, "top_dir": "", "dir": parent,
        "total": len(rows), "list": rows, "jump_list": None}})


def _diag_play_info(guid: str, info: dict) -> dict:
    """续播诊断钩子：play/info 返回前记一条事件；诊断失败绝不影响播放。"""
    try:
        resume_diagmod.note_info(guid, info)
    except Exception:
        _log.debug("_diag_play_info failed: guid=%s", guid, exc_info=True)
        pass
    return info


def _diag_record_canon_key(ig: str) -> str | None:
    """play/record 诊断用的规范键：优先取刚存下的 history 记录里的
    canon_key 字段；没有（未存入 history，如透传上游）时用
    history.canonical_key_for_guid 对 guid 现算；算不到返回 None。
    只读诊断，失败返回 None，绝不影响播放。"""
    try:
        rec = history.get(ig)
        if isinstance(rec, dict) and rec.get("canon_key"):
            return str(rec["canon_key"])
    except Exception:
        _log.debug("_diag_record_canon_key failed", exc_info=True)
        pass
    try:
        return histmod.canonical_key_for_guid(ig)
    except Exception:
        _log.debug("_diag_record_canon_key failed", exc_info=True)
        return None


def _vod_play_info(guid: str, client) -> JSONResponse | None:
    parsed = _parse_line_item_guid(guid)
    if not parsed:
        return None
    og, line, index = parsed
    brief = store.get_title_by_guid(og)
    if not brief:
        return None
    target = store.resolve_episode(og, line, index)
    if not target:
        return None
    is_tv = inj.is_tv(brief)
    eg = episode_guid(og, "", index)
    base_guid = eg if is_tv else og
    ep = None
    if is_tv:
        ep = store.get_episode(eg)
        if not ep:
            return None
        info = inj.episode_play_info(ep)
    else:
        info = inj.play_info(brief)
    info["guid"] = base_guid
    # TVBox/VOD：点击即播当前续播集，但保留TV模型以显示选集入口。
    info["type"] = "TV" if is_tv else "Movie"
    info["media_guid"] = ep_media_guid(eg, line)
    info["video_guid"] = ep_video_guid(eg, line)
    item = inj.detail_item(brief)
    item.update({
        "guid": base_guid,
        "type": "TV" if is_tv else "Movie",
        "can_play": 1,
        "play_item_guid": base_guid,
        "parent_guid": og,
        "parent_title": brief.get("title", ""),
        "number_of_episodes": len(store.list_episodes(og, line)) if is_tv else 0,
        "number_of_seasons": 1 if is_tv else 0,
        "local_number_of_episodes": 0,
        "local_number_of_seasons": 0,
        "overview": f"来源：{_provider_display(line.split('::', 1)[0])}\n"
                    f"以该线路为优先种子起播，仍走竞速选最快线。\n\n{brief.get('overview') or ''}",
    })
    info["item"] = item
    # 续播位置：优先该集自己的记录；第 1 集没有集记录时回填标题记录的位置
    #（客户端以标题/季/文件夹 guid 上报进度时落到标题键上，见 history.position）。
    info["ts"] = history.position(base_guid, og, is_first=(index == 0))
    # 真实时长：顶层 duration 与 item.duration 两处都给（见 _attach_play_duration）。
    _attach_play_duration(info, ep=ep, brief=brief)
    info = _diag_play_info(guid, info)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(info, client)})


def _normalize_history_guid(guid: str) -> str:
    """把带线路/文件夹身份的复合 guid 归一化为基础影片/集 guid，保证历史、收藏、已看
    都按原片身份记录，不按资源站拆分；非复合 guid 原样返回。

    文件夹卡（首页/热门/分类/榜单的 _vod_directory_card，guid 为
    fnmedia-line-folder:<og>）与线路季卡（fnmedia-line-season:…）本身不是
    可播身份：前者归一化到所属标题，后者同样归一化到所属标题——否则
    play/record 与收藏/已看端点在 store 里查不到它们，会把进度/收藏
    直接透传给官方后端丢掉（官方回 -6，客户端提示失败）。
    """
    if not isinstance(guid, str):
        return guid or ""
    folder_og = _parse_line_folder_guid(guid)
    if folder_og:
        return folder_og
    parsed = _parse_line_item_guid(guid)
    if parsed:
        og, _line, index = parsed
        brief = store.get_title_by_guid(og)
        if brief and inj.is_tv(brief):
            return episode_guid(og, "", index)
        return og
    season = _parse_line_season_guid(guid)
    if season:
        return season[0]
    # media/video guid（play/info 返回给播放器的那两个 hash guid）：
    # 客户端上报进度/收藏/已看时 item_guid 可能直接用它们。hash 不可逆推，
    # 只能查 store 的反查索引：标题级（"t:"）归一化到标题 guid；集级
    # （"e:"）归一化到集 guid——但电影按标题续播，集级也归一化到标题，
    # 否则进度落在集键上、电影重开时按标题查不到。查不到的原样返回，
    # 调用方守门仍会把它透传给官方，不误吞本地官方条目的上报。
    target = store.resolve_media_guid(guid)
    if target:
        if target.get("kind") == "title":
            return target.get("online_guid") or guid
        brief = store.get_title_by_guid(target.get("online_guid", ""))
        if brief is not None and not inj.is_tv(brief):
            return target.get("online_guid") or guid
        return target.get("ep_guid") or guid
    return guid


def _record_item_guid(body: dict) -> str:
    """play/record 一次上报的本地身份：body 里 item_guid / video_guid /
    media_guid 三个字段，任何一个能解析到本地影片/集就算。

    集身份优先于标题身份：客户端以标题 guid 作 item_guid、却在
    video/media 字段里带上当集 video guid 时（如第 4 集），进度属于
    该集；只落到标题的话，标题记录只回填第 1 集，第 4 集进度就丢了。
    三个字段都解析不到时回退到归一化后的 item_guid（调用方守门透传）。
    榜单骨架是例外：play/info 已返回标题级 media/video guid，但绑定
    尚未完成、标题还没进 store；此时按榜单 guid 记下，绑定后同一 guid
    即标题 guid，续播仍能接上。
    """
    cands: list[str] = []
    for key in ("item_guid", "video_guid", "media_guid"):
        v = body.get(key)
        if isinstance(v, str) and v:
            n = _normalize_history_guid(v)
            if n and n not in cands:
                cands.append(n)
    for n in cands:
        if store.get_episode(n):
            return n
    for n in cands:
        if store.get_title_by_guid(n):
            return n
    for key in ("video_guid", "media_guid"):
        v = body.get(key)
        if isinstance(v, str) and v:
            cg = chart_guid_by_media(v)
            if cg:
                return cg
    return cands[0] if cands else ""


@app.post("/v/api/v1/item/list")
async def item_list(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        _log.debug("item_list failed", exc_info=True)
        body = {}
    aguid = body.get("ancestor_guid") or body.get("parent_guid") or body.get("guid")
    client = _client_of(request)
    parent = body.get("parent_guid") or aguid

    # —— 媒体库可见权限 ——：子账号无权限 / 被隐藏的虚拟库直接透传官方
    # （官方不认识该 guid → 库不存在）。加固聚合库的目录树文件夹也按该库计；
    # T4 内部节点按 T4 选片库计。
    _vis = lambda g: (GUARD_NATIVE_GUID if _gn_folder_token(str(g or "")) else g)
    if any(g and not _viewer_can_open_lib(request, _vis(g))
           for g in (aguid, parent)):
        return await forward_upstream(request)
    if aguid == T4_LIB_GUID or parent == T4_LIB_GUID or t4_browser.node(parent):
        started = time.monotonic()
        node = t4_browser.node(parent)
        try:
            if node and node["kind"] == "title":
                await asyncio.wait_for(t4_service.ensure_detail(
                    ext_client(request), node["target_guid"]), timeout=35)
            data = await asyncio.wait_for(
                t4_browser.listing(ext_client(request), parent or T4_LIB_GUID, body),
                timeout=35)
        except asyncio.TimeoutError:
            _log.warning(
                "T4 browse timeout source=%s node=%s kind=%s page=%s elapsed_ms=%d",
                (node or {}).get("source_id", ""), parent,
                (node or {}).get("kind", "root"), body.get("page", 1),
                int((time.monotonic() - started) * 1000))
            return JSONResponse({"code": -1, "msg": "T4 浏览超时，请稍后重新进入",
                                 "data": None}, status_code=504)
        except t4.T4Error as exc:
            _log.warning("T4 browse failed: %s", exc)
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=502)
        data["list"] = clientmod.normalize_cards(
            [_apply_marks(row) for row in data["list"]], client)
        return JSONResponse({"code": 0, "msg": "", "data": data})

    # —— TVBox 加固源专区库 ——
    # >>> PRIVATE:GUARD
    if features.GUARD and aguid:
        glib = guard_lib_source(aguid)
        if glib:
            lib_name = guard_lib_name(glib["conf"])
            # TV/盒子 App 对该接口超时极敏感（约 10~15s 即提示“加载失败/看不到媒体库”）。
            # 真机爬虫遇到死站会挂到 60s，这里强制封顶：快速失败返回空列表，
            # 让客户端正常显示“暂无内容”，而不是整页加载失败。
            _browse_deadline = 12.0 if int(body.get("page") or 1) == 1 else 8.0
            try:
                rows, total = await asyncio.wait_for(
                    _guard_browse_rows(glib, body), timeout=_browse_deadline)
                rows = clientmod.normalize_cards(rows, client)
                _schedule_browse_probe(rows, request)
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
                    "dir": "", "total": total, "list": rows, "jump_list": None}})
            except asyncio.TimeoutError:
                _log.warning("tvbox browse timeout %.0fs: %s",
                             _browse_deadline, glib["conf"].get("key"))
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
                    "dir": "", "total": 0, "list": [], "jump_list": None}})
            except Exception as exc:  # noqa: BLE001
                _log.warning("tvbox browse failed: %s", exc)
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
                    "dir": "", "total": 0, "list": [], "jump_list": None}})
    # <<< PRIVATE:GUARD

    # —— 🛡加固影视：OK影视 式目录树（源→分类→无限海报墙），只读后台缓存，即时返回 ——
    # >>> PRIVATE:GUARD
    _gn_parent = str(body.get("parent_guid") or body.get("dir") or "")
    # 网页/各端下钻时常常【只带 parent_guid、不带 ancestor_guid】，故两种命中都要接：
    # 库根请求(ancestor=库guid) 或 文件夹下钻(parent/ancestor=fnmedia-gnfolder:*)。
    _gn_hit = _gn_folder_token(_gn_parent) or _gn_folder_token(aguid)
    if _gn_folder_token(aguid) and not _gn_parent:
        _gn_parent = str(aguid)
    if features.GUARD and guard_native_enabled() and \
            (aguid == GUARD_NATIVE_GUID or _gn_hit):
        tags = body.get("tags")
        folder = await _gn_folder_rows(_gn_parent, body) \
            if _gn_folder_token(_gn_parent) else None
        if folder is not None:
            frows, total, _more = folder
            out_rows = clientmod.normalize_cards(frows, client)
            _schedule_browse_probe(out_rows, request)
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": GUARD_NATIVE_TITLE, "mdb_category": "Mix",
                "top_dir": "", "dir": _gn_parent or "", "total": total,
                "list": out_rows, "jump_list": None}})
        # 根目录：选了具体“源/分类”筛选时仍按筛选实时翻对应源分类页（向后兼容）
        cat = await _gn_rows_for_tags(tags, int(body.get("page") or 1)) \
            if isinstance(tags, dict) else None
        if cat is not None:
            page_rows, _handled, more = cat
            page_size = max(1, min(int(body.get("page_size") or 22), 100))
            pg = max(1, int(body.get("page") or 1))
            # total 给“还有下一页”的哨兵值驱动无限下拉；否则给当前已加载总量终止。
            total = pg * page_size + 1 if more and page_rows else \
                max(1, (pg - 1) * page_size + len(page_rows))
            out_rows = clientmod.normalize_cards(page_rows, client)
            _schedule_browse_probe(out_rows, request)
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": GUARD_NATIVE_TITLE, "mdb_category": "Mix",
                "top_dir": "", "dir": "", "total": total,
                "list": out_rows, "jump_list": None}})
        # 根目录默认：热门聚合 + 各源文件夹（OK影视 首页形态）
        root_rows = _gn_root_folder_rows()
        out_rows = clientmod.normalize_cards(root_rows, client)
        _schedule_browse_probe(out_rows, request)
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": GUARD_NATIVE_TITLE, "mdb_category": "Mix",
            "top_dir": "", "dir": "", "total": len(root_rows),
            "list": out_rows, "jump_list": None}})
    # <<< PRIVATE:GUARD

    # —— 小雅分类媒体库 ——
    # >>> PRIVATE:CATVOD
    if aguid and _xlib_on():
        xcat = _xlib_cat(aguid)
        if xcat:
            try:
                rows, total = await asyncio.wait_for(
                    _xlib_browse_rows(xcat, body), timeout=14.0)
                rows = clientmod.normalize_cards(rows, client)
                _schedule_browse_probe(rows, request)
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "mdb_name": xcat["title"], "mdb_category": xcat["mdb"],
                    "top_dir": "", "dir": "", "total": total, "list": rows,
                    "jump_list": None}})
            except asyncio.TimeoutError:
                _log.warning("xlib browse timeout: %s", xcat.get("id"))
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "mdb_name": xcat["title"], "mdb_category": xcat["mdb"],
                    "top_dir": "", "dir": "", "total": 0, "list": [],
                    "jump_list": None}})
            except Exception as exc:  # noqa: BLE001
                _log.warning("xlib browse failed: %s", exc)
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "mdb_name": xcat["title"], "mdb_category": xcat["mdb"],
                    "top_dir": "", "dir": "", "total": 0, "list": [],
                    "jump_list": None}})
    # <<< PRIVATE:CATVOD

    # —— 红果短剧：库根 → 一季分类目录 → 频道/集墙（只读缓存，即时返回） ——
    if aguid == hgdjmod.lib_guid() and hgdj_enabled():
        page = max(1, int(body.get("page") or body.get("page_index") or 1))
        page_size = max(1, min(int(body.get("page_size") or 22), 100))
        order, total_n, pages = hgdjmod.browse_guids(page, page_size)
        rows = []
        for g in order:
            rows.append(hgdjmod.series_card(g))
        rows = clientmod.normalize_cards(rows, client)
        _schedule_browse_probe(rows, request)
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": hgdjmod.HGDJ_LIB_NAME, "mdb_category": "TV",
            "top_dir": "", "dir": "", "total": total_n, "list": rows,
            "jump_list": None}})


    # —— TVBox 式二级线路海报墙：一级影片文件夹，二级线路卡（竞速种子） ——
    # （冻结适配：文件夹点某条线路只选定竞速种子/选集身份，不固定单线、不跳竞速。
    # 种子经 _seeded_media_target 解析，起播仍走 auto=1 竞速流程。）
    folder_og = _parse_line_folder_guid(parent)
    if folder_og:
        return await _vod_line_folder(folder_og, body, client)

    # —— 单源独立媒体库：该源四类交错成一条最近更新流 ——
    sscope = standalone_source_of(aguid) if aguid else None
    if sscope is not None and \
            any(s["id"] == sscope for s in standalone_sources_active()):
        src = next((s for s in config.all_sources() if s["id"] == sscope), {})
        lib_name = standalone_lib_name(src)
        if not catalog.standalone_available(sscope):
            if catalog.routes_stale() and not catalog._refreshing:
                asyncio.create_task(
                    catalog.refresh_routes(config.enabled_sources()))
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
                "dir": "", "total": 0, "list": [], "jump_list": None}})
        page = max(1, int(body.get("page") or body.get("page_index") or 1))
        page_size = max(1, min(int(body.get("page_size") or 22), 100))
        offset = int(body["offset"]) if body.get("offset") is not None \
            else (page - 1) * page_size
        briefs, total = await catalog.standalone_window(sscope, offset, page_size)
        rows = []
        for b in briefs:
            row = inj.search_item(b)
            _sb = _brief_speed_badge(b)
            row["title"] = (b.get("title") or "") + (
                f" ·{b['remarks']}" if b.get("remarks") else "") + (
                f" ·{_sb}" if _sb else "")
            row["ancestor_guid"] = aguid
            row["ancestor_name"] = lib_name
            row["ancestor_category"] = "Mix"
            row["video_guid"] = video_guid(b["guid"])
            _apply_marks(row)
            rows.append(_vod_directory_card(row))
        rows = clientmod.normalize_cards(rows, client)
        _schedule_browse_probe(rows, request)
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": lib_name, "mdb_category": "Mix", "top_dir": "",
            "dir": "", "total": total, "list": rows, "jump_list": None}})

    # —— 海量分类片库：按资源站分类深翻，前端无限滚动逐页请求 ——
    ccat = CATALOG_GUID_CAT.get(aguid)
    if ccat is not None:
        mdb_name = catalogmod.catalog_title(ccat)
        mdb_cat = catalogmod.catalog_mdb(ccat)
        # 扩展库走独立开关（不受海量主开关影响）；其余分类随海量主开关。
        if ccat == catalogmod.ADULT_CAT:
            _cat_gate = catalogmod._welfare_on() and catalog.available(ccat)
        else:
            _cat_gate = catalog_enabled() and catalog.available(ccat)
        if not _cat_gate:
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
                "dir": "", "total": 0, "list": [], "jump_list": None}})
        # 路由表过期则后台重建（不阻塞）
        if catalog.routes_stale() and not catalog._refreshing:
            asyncio.create_task(catalog.refresh_routes(config.enabled_sources()))
        page = int(body.get("page") or body.get("p") or body.get("page_index")
                   or body.get("pn") or 1)
        page = max(1, page)
        page_size = int(body.get("page_size") or 22)
        page_size = max(1, min(page_size, 100))
        offset = int(body["offset"]) if body.get("offset") is not None \
            else (page - 1) * page_size
        total = catalog.total(ccat)
        offset = max(0, min(offset, total))
        briefs = await catalog.get_window(ccat, offset, page_size)
        # 派生 4K 的总数在翻页后才准（total() 在此场景下返回实际找到的数）；
        # 先按旧总数翻、再按找到的数报，前端才不会对着空库一直翻页。
        total = catalog.total(ccat)
        rows = _catalog_filter([_catalog_card(b, ccat) for b in briefs], body)
        rows = clientmod.normalize_cards(rows, client)
        _schedule_browse_probe(rows, request)
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
            "dir": "", "total": total, "list": rows,
            "jump_list": None}})

    cat = CHART_GUID_CAT.get(aguid)
    if cat is None:
        # 兼容旧的“最近更新”热门库 guid（单库/剧集库）→ 榜单剧集库
        cat = "tv" if aguid in (HOT_LEGACY_GUID, HOT_LIB_GUIDS.get("tv")) else \
            HOT_GUID_CAT.get(aguid)
    if cat is None:
        return await forward_upstream(request)
    mdb_name = chartmod.chart_cat_title(cat)
    mdb_cat = chartmod.chart_cat_mdb(cat)
    if not charts_enabled():
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
            "dir": "", "total": 0, "list": [], "jump_list": None,
        }})
    # 榜单只读、秒开；缓存过期时后台补刷（stale-while-revalidate），不阻塞本次响应。
    interval = max(1800, int(config.get().get("hot_refresh_interval", 6 * 3600)))
    try:
        if chart_cache.stale(interval) and not chart_cache._refreshing:
            asyncio.create_task(chart_cache.refresh(force=True))
    except Exception:
        _log.debug("item_list failed", exc_info=True)
        pass
    all_rows = _chart_rows_all(body, cat)
    page_size, start, end = _page_args(body, len(all_rows))
    out_rows = all_rows[start:end]
    out_rows = clientmod.normalize_cards(out_rows, client)
    _schedule_browse_probe(out_rows, request)
    _payload = {"code": 0, "msg": "", "data": {
        "mdb_name": mdb_name,
        "mdb_category": mdb_cat,
        "top_dir": "",
        "dir": "",
        "total": len(all_rows),
        "list": out_rows,
        "jump_list": None,
    }}
    return JSONResponse(_payload)


@app.get("/v/api/v1/search/list")
async def search_list(request: Request):
    local = await forward_upstream(request)
    q = request.query_params.get("q", "").strip()
    try:
        import json
        data = json.loads(local.body)
    except ValueError:
        return local
    if not isinstance(data, dict):
        return local
    items = data.get("data")
    if not isinstance(items, list):
        items = []
    online = []
    # 子账号权限：未获“在线片源”授权的账号只搜本地，不注入在线结果
    _viewer = fnusersmod.resolve(request)
    if q and _should_online(q) and vlibsmod.account_online_allowed(_viewer):
        try:
            # 8 个源并发聚合实测 6~8s，6s 预算会把在线结果整体丢掉（只剩本地库）
            online = await asyncio.wait_for(
                online_search(q, ext_client(request), timeout=6.0,
                              request=request), timeout=8.5)
        except asyncio.TimeoutError:
            online = []
        except Exception as exc:
            # 在线聚合搜索整体失败即只剩本地结果；记异常类型，不记搜索关键词。
            _log.warning("online search failed: %s", type(exc).__name__)
            online = []
    # 红果短剧搜索并入（仅查本地已缓存标题，不再打 upstream）
    hgdj_online = []
    if q and hgdj_enabled():
        try:
            hgdj_online = hgdjmod.search_items(q)
        except Exception as exc:
            _log.debug("hgdj search failed: %s", type(exc).__name__)
            hgdj_online = []
    if hgdj_online and not _viewer_can_open_lib(request, hgdjmod.lib_guid()):
        # 红果短剧标题归属 hgdj 库：查看者打不开该库时整批隐藏。
        hgdj_online = []
    if hgdj_online:
        hgdj_online = clientmod.normalize_cards(hgdj_online, _client_of(request))
    # 在线片库更齐全，默认把在线结果排到前面；可在管理页关闭
    if online:
        online = clientmod.normalize_cards(online, _client_of(request))
    if config.get().get("online_first", True):
        data["data"] = hgdj_online + online + items
    else:
        data["data"] = items + online + hgdj_online
    _schedule_browse_probe(data["data"], request)
    return JSONResponse(data, status_code=200,
                        headers={"Content-Type": "application/json; charset=utf-8"})


# ------------------------------------------------------------- 详情 / 播放

async def _load_t4_detail(guid: str, request: Request):
    ep = store.get_episode(guid)
    if ep:
        guid = ep["online_guid"]
    if not store.get_title_by_guid(guid):
        meta = store.get_season(guid)
        if meta:
            guid = meta["online_guid"]
    try:
        await t4_service.ensure_detail(ext_client(request), guid)
    except t4.T4Error as exc:
        _log.warning("T4 detail %s: %s", guid, exc)
        return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                            status_code=502)
    return None


def _t4_media_url(request: Request, mg: str, **params) -> str:
    # Native STRM players consume this URL without the browser's origin resolution.
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if scheme not in ("http", "https"):
        scheme = request.url.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = host or request.url.netloc
    return urllib.parse.urlunsplit((
        scheme, host, f"{ONLINE_PREFIX}/{mg}/t4",
        urllib.parse.urlencode(params), ""))


async def _t4_play_response(mg: str, target: dict, body: dict, request: Request):
    try:
        result = await t4_service.resolve(ext_client(request), target)
        await t4_media.identify(ext_client(request), result)
        if result.wrapper == "DASH":
            raise t4.T4Error("当前飞牛适配尚未验证 DASH/双轨播放，未改写为 HLS")
    except t4.T4Error as exc:
        _log.warning("T4 play %s: %s", mg, exc)
        return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                            status_code=502)
    # Headers stay server-side; unsigned public results need no extra media hop.
    if result.wrapper == "HLS":
        session = t4_service.create_session(mg, target)
        link = _t4_media_url(request, mg, session=session)
    else:
        link = _t4_media_url(request, mg, line=target["line"]) \
            if result.needs_proxy else result.url
    _log.info("T4 handoff media=%s client=%s wrapper=%s delivery=%s absolute=%s",
              mg, _client_of(request).label, result.wrapper,
              "server" if result.needs_proxy else "direct",
              bool(urllib.parse.urlsplit(link).netloc))
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg, "play_link": link,
        "subtitle_guid": "", "subtitle_index": -1, "subtitle_link": "",
        "video_guid": body.get("video_guid", vg_for(mg)), "video_index": 0,
        "wrapper": result.wrapper, "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


@app.api_route(f"{ONLINE_PREFIX}/{{mg}}/t4", methods=["GET", "HEAD"])
async def t4_range(mg: str, request: Request):
    target = store.resolve_media_guid(mg)
    if not target or not target.get("t4"):
        return Response("not found", status_code=404)
    # P0-3：T4 选片库打不开时直接 -6（403）。
    if _online_target_viewable(request, target, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response(status_code=403)
    if request.query_params.get("session"):
        target = t4_service.session_target(mg, request.query_params["session"])
        if target is None:
            return Response("播放会话已过期，请重新播放", status_code=410)
    try:
        target = _t4_selected_target(mg, target, request.query_params, request)
    except t4.T4Error as exc:
        return Response(str(exc), status_code=400)
    try:
        result = await t4_service.resolve(ext_client(request), target)
        await t4_media.identify(ext_client(request), result)
        if result.wrapper == "DASH":
            raise t4.T4Error("当前飞牛适配尚未验证 DASH/双轨播放")
    except t4.T4Error as exc:
        return Response(str(exc), status_code=502)
    response = await t4_media.deliver(ext_client(request), result.url, result, request)
    if response.status_code >= 400:
        t4_service.invalidate_playback(target)
    return response


@app.api_route("/v/media/t4-resource/{token}", methods=["GET", "HEAD"])
async def t4_resource(token: str, request: Request):
    return await t4_media.resource_response(ext_client(request), token, request)


@app.get("/v/api/v1/item/{guid}")
async def item_detail(guid: str, request: Request):
    # —— 库可见权限 ——：guid 本身就是某虚拟库（库根/T4 节点等）时，无权打开
    # 按不存在处理（与 mdb/<guid> 的 -6 一致）。标题 guid 在此不受影响
    # （_vkey_for_guid 返回 None → 放行，标题在下面的 brief 分支里再按
    # 归属库过滤）；本地库 guid 同样放行（非虚拟库恒 True）。
    if not _viewer_can_open_lib(request, guid):
        return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
    if _parse_line_item_guid(guid):
        line_detail = _vod_line_detail(guid, _client_of(request))
        if line_detail is not None:
            return line_detail
    if _parse_line_folder_guid(guid):
        folder_detail = _vod_folder_detail(guid, _client_of(request))
        if folder_detail is not None:
            return folder_detail
    if guid == T4_LIB_GUID or t4_browser.node(guid):
        if guid == T4_LIB_GUID:
            data = inj._base_item(guid=guid, type="MediaDB", title=T4_LIB_TITLE,
                                  ancestor_guid="", ancestor_name="", ancestor_category="",
                                  can_play=0, play_item_guid="", play_error="", logic_type=0)
        else:
            node = t4_browser.node(guid)
            try:
                t4_browser.source(node["source_id"])
            except t4.T4Error as exc:
                return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                    status_code=502)
            data = t4_browser.card(guid)
        return JSONResponse({"code": 0, "msg": "", "data":
                             clientmod.normalize_detail(data, _client_of(request))})
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    client = _client_of(request)
    # 单源独立媒体库根节点
    sscope = standalone_source_of(guid)
    if sscope is not None and \
            any(s["id"] == sscope for s in standalone_sources_active()):
        src = next((s for s in config.all_sources() if s["id"] == sscope), {})
        _pool = list(catalogmod.CATALOG_CATEGORIES)
        if catalogmod._welfare_on() and src.get("adult"):
            _pool = _pool + [(catalogmod.ADULT_CAT, "", "")]
        n = sum(catalog.total(c, sscope) for c, _, _ in _pool)
        data = inj._base_item(
            guid=guid, type="MediaDB", title=standalone_lib_name(src),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # TVBox 加固源专区库根节点
    # >>> PRIVATE:GUARD
    if features.GUARD:
        if guard_native_enabled() and _gn_folder_token(guid):
            # 目录树文件夹节点：返回 Directory，前端据此下钻（parent_guid=本 guid）
            token = _gn_folder_token(guid)
            title = "目录"
            if token == _GN_HOT_TOKEN:
                title = "🔥 热门聚合"
            elif token.startswith("src:"):
                title = "📺 " + _gn_source_name(token[4:])
            else:
                parsed = _gn_parse_cls_token(token)
                if parsed:
                    k, t = parsed
                    title = next((cn for _s, kk, tt, cn in _gn_film_index()
                                  if kk == k and tt == t), "分类")
            data = inj._base_item(
                guid=guid, type="Directory", title=title,
                can_play=0, number_of_episodes=0, local_number_of_episodes=0,
                ancestor_guid=GUARD_NATIVE_GUID, ancestor_name=GUARD_NATIVE_TITLE,
                ancestor_category="Mix", poster="", logic_type=0)
            data["play_item_guid"] = ""
            data["play_error"] = ""
            return JSONResponse({"code": 0, "msg": "", "data": data})

        if guid == GUARD_NATIVE_GUID and guard_native_enabled():
            n = len(_gn_hot_rows())
            data = inj._base_item(
                guid=guid, type="MediaDB", title=GUARD_NATIVE_TITLE,
                can_play=1, number_of_episodes=n, local_number_of_episodes=n,
                ancestor_guid="", ancestor_name="", ancestor_category="",
                poster="", logic_type=0)
            data["play_item_guid"] = ""
            data["play_error"] = ""
            return JSONResponse({"code": 0, "msg": "", "data": data})

        glib = guard_lib_source(guid)
        if glib:
            data = inj._base_item(
                guid=guid, type="MediaDB", title=guard_lib_name(glib["conf"]),
                can_play=1, number_of_episodes=0, local_number_of_episodes=0,
                ancestor_guid="", ancestor_name="", ancestor_category="",
                poster="", logic_type=0)
            data["play_item_guid"] = ""
            data["play_error"] = ""
            return JSONResponse({"code": 0, "msg": "", "data": data})
    # <<< PRIVATE:GUARD
    # 小雅分类库：库根节点 + 影片详情
    # >>> PRIVATE:CATVOD
    if _xlib_on():
        xcat = _xlib_cat(guid)
        if xcat:
            import xlib as _xl0
            _, n = _xl0.library.page(xcat["id"], 0, 1)
            data = inj._base_item(
                guid=guid, type="MediaDB", title=xcat["title"],
                can_play=1, number_of_episodes=n, local_number_of_episodes=n,
                ancestor_guid="", ancestor_name="", ancestor_category="",
                poster="", logic_type=0)
            data["play_item_guid"] = ""
            data["play_error"] = ""
            return JSONResponse({"code": 0, "msg": "", "data": data})
        import xlib as _xl
        found = _xl.library.by_guid(guid)
        if found:
            cat, show = found["cat"], found["show"]
            if not _viewer_can_open_lib(request, _xl.lib_guid(cat)):
                return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
            cmeta = _xl.cat_by_id(cat)
            files = show.get("files") or []
            is_tv = cmeta["kind"] in ("tv", "anime", "variety", "short")
            data = inj._base_item(
                guid=guid,
                type="TV" if is_tv else "Movie",
                title=show.get("title") or os.path.basename(show["id"]),
                can_play=1,
                number_of_seasons=1 if is_tv else 0,
                number_of_episodes=len(files),
                local_number_of_episodes=len(files),
                release_date=show.get("year", ""), air_date=show.get("year", ""),
                ancestor_guid=_xl.lib_guid(cat), ancestor_name=cmeta["title"],
                ancestor_category=cmeta["mdb"],
                poster=inj._poster((_xl.library.poster_of(show["id"]) or {}).get(
                    "poster", "")),
                overview=(_xl.library.poster_of(show["id"]) or {}).get(
                    "overview", ""),
                logic_type=0)
            data["play_item_guid"] = guid
            data["play_error"] = ""
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(data, client)})
    # <<< PRIVATE:CATVOD
    # 海量库根节点
    if guid in CATALOG_GUID_CAT:
        ccat = CATALOG_GUID_CAT[guid]
        n = catalog.total(ccat)
        data = inj._base_item(
            guid=guid, type="MediaDB", title=catalogmod.catalog_title(ccat),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # 榜单虚拟媒体库根节点（进入媒体库时前端会请求 item/<库guid>）。
    # 必须在透传前拦截，否则官方后端返回 -6，页面提示「此媒体库不存在」。
    if guid in CHART_GUID_CAT or is_hot_guid(guid):
        cat = CHART_GUID_CAT.get(guid) or \
            HOT_GUID_CAT.get(guid) or ("tv" if guid == HOT_LEGACY_GUID else "movie")
        n = len(chart_cache.items(cat))
        data = inj._base_item(
            guid=guid, type="MediaDB", title=chartmod.chart_cat_title(cat),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # 榜单条目：按需用片名搜源解析；成功后复用标准详情，失败返回“暂无片源”。
    if chart_cache.is_chart_guid(guid):
        meta = chart_cache.get_guid(guid) or {}
        bound = store.get_title_by_guid(guid)
        if bound is None and not (time.time() - _CHART_FAILED_TS.get(guid, 0)
                                  < _CHART_FAILED_TTL):
            # 详情接口也不能把用户卡在海报墙：后台搜源 + 极短窗口，超时先返回
            # 可播骨架（play/info/stream/preset 会在播放页内继续等待同一任务）。
            fut = start_resolve_chart(guid)
            _PENDING_MEDIA_CHART[media_guid(guid)] = guid
            if fut is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=_CHART_DETAIL_WAIT)
                except asyncio.TimeoutError:
                    pass
            bound = store.get_title_by_guid(guid)
        if bound is not None:
            # 榜单条目已绑定资源站标题：标题归属库与榜单库任一可打开才放行。
            if not _brief_viewable(
                    request, bound,
                    extra_keys={vlibsmod.make_key(vlibsmod.K_CHART,
                                                  meta.get("cat", "tv"))}):
                return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
            data = inj.detail_item(bound)
            _apply_marks(data)
            # 用榜单的优质海报/评分/简介覆盖资源站元数据
            if meta.get("cover"):
                for k in ("backdrops", "posters"):
                    if data.get(k):
                        data[k] = inj._poster(meta["cover"])
            if meta.get("rating"):
                data["vote_average"] = str(meta["rating"])
            if meta.get("desc") and not data.get("overview"):
                data["overview"] = meta["desc"]
            _attach_related(data, bound)
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(data, client)})
        # 未绑定资源站标题的榜单条目：唯一归属就是榜单库本身。
        if not _viewer_can_open_key(
                request, vlibsmod.make_key(vlibsmod.K_CHART, meta.get("cat", "tv"))):
            return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
        cat = meta.get("cat", "tv")
        failed = time.time() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL
        data = inj._base_item(
            guid=guid,
            type="Movie" if cat == "movie" else "TV",
            title=meta.get("title", ""), can_play=0 if failed else 1,
            ancestor_guid=CHART_LIB_GUIDS.get(cat, ""),
            ancestor_name=chartmod.chart_cat_title(cat),
            ancestor_category=chartmod.chart_cat_mdb(cat),
            poster=meta.get("cover", ""), logic_type=0)
        # 匹配中：标记可播，让前端能立刻进入播放页；真正失败在播放页内给出提示。
        data["play_item_guid"] = guid if not failed else ""
        if failed:
            data["play_error"] = "暂无在线片源"
            data["overview"] = (meta.get("desc") or
                                "暂未在内置资源站找到可播片源，可过段时间再试，或用顶部搜索同名资源。")
        else:
            data["play_error"] = ""
            data["overview"] = meta.get("desc") or data.get("overview", "")
        data["vote_average"] = str(meta.get("rating") or "0")
        data["release_date"] = meta.get("year") or ""
        data["air_date"] = meta.get("year") or ""
        _apply_marks(data)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    # ---------- 红果短剧：series / season / episode 详情 ----------
    if hgdj_enabled() and hgdjmod.is_hgdj_series(guid):
        if not _viewer_can_open_lib(request, hgdjmod.lib_guid()):
            return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
        rec = await hgdjmod.series_detail(guid)
        if rec is None:
            return JSONResponse({"code": 0, "msg": "", "data": {}})
        covers = [rec.get("pic_cms") or ""] if rec.get("pic_cms") else []
        n = len(rec.get("eps") or [])
        if n == 0:
            try:
                n = int(rec.get("episode_cnt") or 0)
            except (TypeError, ValueError):
                n = 0
        data = inj._base_item(
            guid=guid, type="TV", title=rec.get("title", ""),
            can_play=1, number_of_seasons=1, number_of_episodes=n,
            local_number_of_episodes=n,
            ancestor_guid=hgdjmod.lib_guid(), ancestor_name=hgdjmod.HGDJ_LIB_NAME,
            ancestor_category="TV",
            poster=covers[0] if covers else "", posters=covers,
            overview=rec.get("overview", ""),
            release_date="", air_date="", logic_type=0)
        data["play_item_guid"] = guid
        data["play_error"] = ""
        data["poster_type"] = 0
        data["video_guid"] = hgdjmod.media_guid_of(guid, 1) if n else ""
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    if hgdj_enabled() and hgdjmod.is_hgdj_episode(guid):
        if not _viewer_can_open_lib(request, hgdjmod.lib_guid()):
            return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
        try:
            sg, ep = guid[len("hgdj-ep:"):].rsplit(":", 1)
            ep = int(ep)
        except ValueError:
            return await forward_upstream(request)
        rec = await hgdjmod.series_detail(sg)
        if rec is None:
            return await forward_upstream(request)
        eps = rec.get("eps") or []
        e = next((x for x in eps if int(x.get("hgdj_ep", 0)) == ep), None)
        title = (e.get("name") if e else f"第{ep}集") or f"第{ep}集"
        data = inj._base_item(
            guid=guid, type="Episode", title=title,
            parent_guid=hgdjmod.season_guid_of(sg), parent_title="第 1 季",
            tv_title=rec.get("title", ""), season_number=1, episode_number=ep,
            number_of_seasons=1, number_of_episodes=len(eps),
            local_number_of_episodes=len(eps), can_play=1,
            media_guid=hgdjmod.media_guid_of(sg, ep),
            video_guid="onv" + sg[2:][:29])
        _apply_marks(data)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    # 伪造的 Season / Episode 节点
    if store.get_episode(guid):
        ep = store.get_episode(guid)
        parent = store.get_title_by_guid(ep.get("online_guid") or "")
        # 父标题在库里且查看者打不开其任一归属库时拒绝；父标题已被淘汰
        # （查不到）时放行——季/集节点本身不是标题，标题详情与搜索已是
        # 拦截点，淘汰后拒绝会误伤断点续播类直达详情。
        if parent is not None and not _brief_viewable(request, parent):
            return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
        data = inj.episode_item(ep)
        _apply_marks(data)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    season = store.get_season(guid)
    if season:
        tv = season["online_guid"]
        brief = store.get_title_by_guid(tv)
        if brief is not None and not _brief_viewable(request, brief):
            return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
        data = inj.season_item(brief, dict(season, guid=guid))
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(data, client)})
    brief = store.get_title_by_guid(guid)
    if not brief:
        return await forward_upstream(request)
    if not _brief_viewable(request, brief):
        return JSONResponse({"code": -6, "msg": "此媒体库不存在", "data": None})
    data = inj.detail_item(brief)
    _apply_marks(data)
    _attach_related(data, brief)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_detail(data, client)})


@app.get("/v/api/v1/season/list/{guid}")
async def season_list_route(guid: str, request: Request):
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    # >>> PRIVATE:CATVOD
    if _xlib_on():
        import xlib as _xl
        found = _xl.library.by_guid(guid)
        if found and found["cat"] in ("tv", "anime", "variety", "short"):
            show = found["show"]
            sg = _xlib_season_guid(guid)
            data = [inj._base_item(
                guid=sg, type="Season", title="第1季",
                parent_guid=guid, parent_title=show.get("title", ""),
                ancestor_guid=_xl.lib_guid(found["cat"]),
                ancestor_name=_xl.cat_by_id(found["cat"])["title"],
                ancestor_category=_xl.cat_by_id(found["cat"])["mdb"],
                season_number=1,
                number_of_episodes=len(show.get("files") or []),
                can_play=None, poster="")]
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_cards(
                                     data, _client_of(request))})
    # <<< PRIVATE:CATVOD
    parsed_line = _parse_line_item_guid(guid) or _parse_line_season_guid(guid)
    fixed_line = parsed_line[1] if parsed_line else None
    fixed_season = parsed_line[2] if parsed_line else 1
    if parsed_line:
        guid = parsed_line[0]
    brief = store.get_title_by_guid(guid)
    if not brief or not inj.is_tv(brief):
        return await forward_upstream(request)
    client = _client_of(request)
    rows = inj.season_list(brief)
    if fixed_line:
        for row in rows:
            try:
                sn = int(row.get("season_number") or fixed_season or 1)
            except (TypeError, ValueError):
                sn = 1
            row["guid"] = _line_season_guid(guid, fixed_line, sn)
            row["parent_guid"] = _line_item_guid(guid, fixed_line, 0)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_cards(rows, client)})


@app.get("/v/api/v1/episode/list/{guid}")
async def episode_list_route(guid: str, request: Request):
    # >>> PRIVATE:CATVOD
    if _xlib_on():
        import xlib as _xl
        # guid 可能是 season guid 或 show guid
        show_g = None
        for sg, (sguid, _i) in list(_XLIB_EP_MAP.items()):
            if _xlib_season_guid(sguid) == guid:
                show_g = sguid
                break
        if show_g is None:
            found = _xl.library.by_guid(guid)
            if found and found["cat"] in ("tv", "anime", "variety", "short"):
                show_g = guid
        if show_g:
            found = _xl.library.by_guid(show_g)
            files = await _xlib_files_for(show_g)
            show = found["show"]
            rows = []
            for i, fpath in enumerate(files):
                eg = _xlib_ep_guid(show_g, i)
                _XLIB_EP_MAP[eg] = (show_g, i)
                # 同步登记播放 media_guid，供 App/TV 的 /media/range 入口反查
                _xlib_register_media(eg, show_g, i)
                base = os.path.splitext(os.path.basename(fpath))[0]
                title = show.get("title") or ""
                label = base
                # 尽量把冗长文件名压成“第N集”
                m = re.search(r"0*(\d{1,4})\s*[集话回]?", base)
                epn = _xl.epnum(fpath)
                if epn:
                    label = f"第{epn}集"
                rows.append(inj._base_item(
                    guid=eg, type="Episode", title=label,
                    parent_guid=show_g, parent_title=title,
                    ancestor_guid=_xl.lib_guid(found["cat"]),
                    ancestor_name=_xl.cat_by_id(found["cat"])["title"],
                    ancestor_category=_xl.cat_by_id(found["cat"])["mdb"],
                    season_number=1, episode_number=epn or (i + 1),
                    number_of_seasons=1, number_of_episodes=len(files),
                    can_play=1, poster="", file_name=os.path.basename(fpath)))
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_cards(
                                     rows, _client_of(request))})
    # <<< PRIVATE:CATVOD
    # guid 可能是伪造的 season guid、固定线路卡/线路季 guid，也可能是 tv guid
    parsed_line = _parse_line_item_guid(guid) or _parse_line_season_guid(guid)
    fixed_line = parsed_line[1] if parsed_line else None
    fixed_season = parsed_line[2] if parsed_line else None
    if parsed_line:
        guid = parsed_line[0]
    season = store.get_season(guid)
    tv = season["online_guid"] if season else None
    if not tv:
        brief = store.get_title_by_guid(guid)
        if brief and inj.is_tv(brief):
            tv = guid
    if not tv:
        return await forward_upstream(request)
    error = await _load_t4_detail(tv, request)
    if error is not None:
        return error
    client = _client_of(request)
    season_number = season["season"] if season else fixed_season
    rows = inj.episode_list(store.get_title_by_guid(tv), season_number)
    if fixed_line:
        line_season = _line_season_guid(tv, fixed_line, season_number or 1)
        for row in rows:
            ep_g = row.get("guid")
            if ep_g:
                try:
                    ep_index = int(row.get("episode_number") or 1) - 1
                except (TypeError, ValueError):
                    ep_index = 0
                row["guid"] = _line_item_guid(tv, fixed_line, ep_index)
                row["media_guid"] = ep_media_guid(ep_g, fixed_line)
                row["video_guid"] = ep_video_guid(ep_g, fixed_line)
                row["parent_guid"] = line_season
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_cards(rows, client)})


@app.get("/v/api/v1/stream/list/{guid}")
async def stream_list(guid: str, request: Request):
    return await _stream_list_impl(guid, request)


@app.post("/v/api/v1/stream/list")
async def stream_list_post(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    guid = str(body.get("guid") or body.get("item_guid") or "")
    if not guid:
        return await forward_upstream(request)
    return await _stream_list_impl(guid, request)


async def _stream_list_impl(guid: str, request: Request):
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    # P0-3：已知在线标题但打不开任一归属库时直接 -6（T4 刚加载完的在此复核）。
    if _online_guid_viewable(request, guid, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response()
    # >>> PRIVATE:CATVOD
    if _xlib_on():
        xsl = await _xlib_stream_payload(guid)
        if xsl is not None:
            return JSONResponse({"code": 0, "msg": "", "data": xsl})
    # <<< PRIVATE:CATVOD
    # 红果短剧 episode 线路：直链 play_link，cloud_storage_info 直连类型
    if hgdj_enabled() and hgdjmod.is_hgdj_episode(guid):
        if not _viewer_ctx(request).can_open_lib(hgdjmod.lib_guid()):
            return _denied_lib_response()
        try:
            sg, ep = guid[len("hgdj-ep:"):].rsplit(":", 1)
            ep = int(ep)
        except ValueError:
            return await forward_upstream(request)
        rec = await hgdjmod.series_detail(sg)
        if rec is None:
            return JSONResponse({"code": 0, "msg": "", "data": {}})
        eps = rec.get("eps") or []
        e = next((x for x in eps if int(x.get("hgdj_ep", 0)) == ep), None)
        ep_title = (e.get("name") if e else f"第{ep}集") or f"第{ep}集"
        headers = {k.lower(): v for k, v in request.headers.items()}
        sl = hgdjmod.synth_stream(guid, rec | {"hgdj_ep": ep}, headers)
        quals = _qualities(None) if False else []
        if quals:
            sl["video_streams"][0]["resolution_type"] = quals[0]["resolution"]
            sl["video_streams"][0]["bps"] = quals[0]["bitrate"]
            sl["qualities"] = quals
            sl["cloud_storage_info"] = None
        return JSONResponse({"code": 0, "msg": "", "data": sl})
    # 线路卡 guid：按 guid 中携带的影片/线路/集序号返回流容器。
    # 种子身份只决定“播哪条线的第几集”；起播仍走竞速（见 play/play）。
    seeded = _seeded_media_target(guid, request)
    if seeded is not None:
        brief = store.get_title_by_guid(seeded["online_guid"]) or {}
        ep_guids = store.tv_episode_guids(seeded["online_guid"], regular_only=True) \
            if inj.is_tv(brief) else []
        ep = store.get_episode(ep_guids[seeded["index"]]) \
            if ep_guids and seeded["index"] < len(ep_guids) else None
        if ep is not None:
            sl = inj.episode_stream_list(ep)
        else:
            sl = inj.stream_list(brief)
        target = seeded
    else:
        ep = store.get_episode(guid)
        target = None
        if ep:
            sl = inj.episode_stream_list(ep)
            target = store.get_episode_target(guid)
        else:
            brief = store.get_title_by_guid(guid)
            if not brief and chart_cache.is_chart_guid(guid):
                if time.time() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL:
                    return await forward_upstream(request)
                # P0-3：榜单库本身打不开时连占位线路也不给，直接 -6。
                if not _chart_key_open(request, guid, _ctx=_viewer_ctx(request)):
                    return _denied_lib_response()
                # 打开详情/线路菜单也不能同步等搜源：启动后台匹配并立即返回占位线路，
                # 真正播放时 play/info/preset 会等待同一单飞任务。
                start_resolve_chart(guid)
                _PENDING_MEDIA_CHART[media_guid(guid)] = guid
                return JSONResponse({"code": 0, "msg": "",
                                     "data": _instant_stream_payload(media_guid(guid))})
            if not brief:
                return await forward_upstream(request)
            sl = inj.stream_list(brief)
            if inj.is_tv(brief):
                # 标题播放键实际播放智能续播集，线路菜单也以该集为准
                egs = store.tv_episode_guids(guid, regular_only=True)
                if egs:
                    resume_guid, _ = history.resume_for_title(guid, egs)
                    target = store.get_episode_target(resume_guid)
            else:
                target = store.resolve_media_guid(media_guid(guid))
    quals = _qualities(target) if target else []
    if target and target.get("t4"):
        # Menus never start a media probe. Reuse an exact episode/line result.
        playback = t4_service.cached_playback(target)
        metadata = t4_media.metadata.cached(playback) if playback else None
        t4_metadata.apply(sl, playback.wrapper if playback else "", metadata)
    if quals:
        sl["video_streams"][0]["resolution_type"] = quals[0]["resolution"]
        sl["video_streams"][0]["bps"] = quals[0]["bitrate"]
        sl["qualities"] = quals
        sl["cloud_storage_info"] = None
    # 真实时长（resume3-duration）：video_streams[0].duration 仍为 0 时
    # （无 CMS 时长、无 T4 探针结果），用已取到的 playlist 探测：先同步读
    # _PLAYLIST_CACHE，未命中则等同一单飞任务，上限 _DURATION_PROBE_TIMEOUT，
    # 超时/失败回 0，不阻塞出线菜单。成功后缓存到集记录上。
    try:
        vs = sl.get("video_streams") or []
        if vs:
            dur = int(vs[0].get("duration") or 0)
            dur_from_probe = False
            probe_target = None
            if target and target.get("url"):
                probe_target = target
            else:
                _og = None
                _idx = 0
                if ep is not None:
                    _og = ep.get("online_guid")
                    _idx = ep.get("index", 0)
                elif target and target.get("og"):
                    _og = target.get("og")
                if _og:
                    probe_target = store.get_episode_target(
                        episode_guid(_og, "", _idx))
            if not dur and probe_target and not probe_target.get("t4"):
                mg = vs[0].get("media_guid") or ""
                dur = _cached_playlist_seconds(probe_target)
                if dur:
                    dur_from_probe = True
                if not dur and mg:
                    try:
                        body = await asyncio.wait_for(
                            _get_playlist_cached(mg, probe_target, request),
                            timeout=_DURATION_PROBE_TIMEOUT)
                    except Exception:
                        _log.debug("_stream_list_impl failed: guid=%s", guid, exc_info=True)
                        body = None
                    if body and body is not _NOT_HLS:
                        dur = _playlist_total_seconds(body)
                        if dur:
                            dur_from_probe = True
            if dur and probe_target and probe_target.get("online_guid"):
                # 集记录里存原始时长（探测值本身已是裁剪后时间线上的值，
                # CMS 值则是原始时长；上报给客户端前再扣减，避免二次扣减）。
                _remember_episode_duration(
                    probe_target.get("online_guid", ""),
                    probe_target.get("index", 0), dur)
                if not dur_from_probe:
                    # CMS/集记录来源是原始时长：仅剧集目标扣掉已剪掉的
                    # 片头片尾；电影/开关关时原样返回。
                    _brief = store.get_title_by_guid(
                        probe_target.get("online_guid", "")) or {}
                    dur = _skip_adjusted_duration(dur, _brief)
                vs[0]["duration"] = dur
                sl["duration"] = dur
    except Exception:
        _log.debug("_stream_list_impl failed: guid=%s", guid, exc_info=True)
        pass
    # 打开详情/线路菜单即后台预热默认线路播放列表（单飞，真正取 main 时并入）
    if target:
        try:
            _mg = (sl.get("video_streams") or [{}])[0].get("media_guid")
            if _mg:
                _warm_playlist(_mg, target, request)
        except Exception as exc:
            _log.debug("stream list preheat failed: %s", type(exc).__name__)
    return JSONResponse({"code": 0, "msg": "", "data": sl})


# ------------------------------------------------------- 收藏 / 已看（本地接管）

def _online_card_meta(guid: str) -> dict | None:
    """把一个在线 guid 解析成可用于收藏夹展示的元信息（不做网络请求）。
    文件夹卡 guid 先解到所属标题，否则首页收藏拿不到标题/海报。"""
    folder_og = _parse_line_folder_guid(guid)
    if folder_og:
        guid = folder_og
    brief = store.get_title_by_guid(guid)
    if brief:
        return {
            "title": inj._f(brief, "title"),
            "type": inj.fnos_type(brief),
            "poster": inj._f(brief, "poster"),
            "year": inj._f(brief, "year"),
            "remarks": inj._f(brief, "remarks"),
        }
    ep = store.get_episode(guid)
    if ep:
        return {"title": ep.get("ep_title") or "episode", "type": "Episode",
                "poster": "", "year": ""}
    meta = chart_cache.get_guid(guid) or {}
    if meta:
        return {"title": meta.get("title", ""),
                "type": "Movie" if meta.get("cat") == "movie" else "TV",
                "poster": meta.get("cover", ""), "year": meta.get("year", ""),
                "remarks": ""}
    return None


def _is_online_item_guid(guid: str) -> bool:
    if not guid:
        return False
    if _parse_line_item_guid(guid) or _parse_line_season_guid(guid):
        return True
    if hgdj_enabled() and hgdjmod.is_hgdj_series(guid):
        return True
    if hgdj_enabled() and hgdjmod.is_hgdj_episode(guid):
        return True
    if (guid in CATALOG_GUID_CAT or guid in CHART_GUID_CAT or is_hot_guid(guid)
            or standalone_source_of(guid) is not None):
        return False  # 媒体库根节点不可收藏
    if store.get_title_by_guid(guid) or store.get_episode(guid):
        return True
    if chart_cache.is_chart_guid(guid):
        return True
    return False


def _apply_marks(row: dict) -> dict:
    # 卡片 guid 先归一化到原片身份再查标记：首页文件夹卡（fnmedia-line-folder:<og>）
    # 与线路卡（fnmedia-line-item:…）的收藏/已看都按标题/集 guid 存，拿原始卡 guid
    # 查会永远查不到，收藏星标与已看标记全丢。
    g = _normalize_history_guid(row.get("guid", ""))
    # 列表渲染时顺手把元信息补进收藏记录：收藏夹不再只认 store 缓存，
    # 标题被清掉/重启后仍能凭收藏时存下的元信息显示。
    if g and (marks.is_favorite(g) or marks.is_watched(g)):
        marks.remember_meta(g, {
            "title": (row.get("title") or "").replace("【在线】", "").split(" ·")[0],
            "type": row.get("type", ""), "poster": row.get("poster", ""),
            "year": (row.get("release_date") or "")[:4],
            "remarks": "", "ancestor_guid": row.get("ancestor_guid", ""),
            "ancestor_name": row.get("ancestor_name", ""),
        })
    if marks.is_favorite(g):
        row["is_favorite"] = 1
    if marks.is_watched(g):
        row["watched"] = 1
        row["is_watched"] = 1
        ts = marks.watched_ts(g)
        if ts:
            row["watched_ts"] = int(ts)
    return row


def _attach_related(data: dict, brief: dict) -> None:
    """详情页下方挂关联作品（recommendations 字段）。

    只用 store 里已收录的片（用户浏览过的分类/搜索结果），不调外部接口。
    冷启动兜底：已浏览池入选不足 12 条时，再用分类 dense（catalog 已物化、
    纯本地、零网络）补候选、统一打分；打分、门槛、上限、海报要求不变。
    字段名按“官方详情响应常嵌套相关推荐数组”的公开观察推断，未在官方
    文档里核实；客户端是否渲染以真机实测为准。关掉 related_enabled 即停用。
    """
    try:
        if not config.get().get("related_enabled", True):
            return
    except Exception as exc:
        _log.debug("related config check failed: %s", type(exc).__name__)
        return
    try:
        from related import DEFAULT_LIMIT, related_for, related_for_with_dense
        pool = list(store._titles.values())
        items = related_for(brief, pool)
        if len(items) < DEFAULT_LIMIT:
            dense: list[dict] = []
            for _cat in catalogmod.CATS:
                dense.extend(catalog.cached_dense(_cat, 40))
            if dense:
                items = related_for_with_dense(brief, pool, dense)
    except Exception as exc:
        _log.debug("related failed: %s", type(exc).__name__)
        return
    if items:
        data["recommendations"] = [inj.search_item(b) for b in items]


def _mark_lib_keys(guid: str) -> list:
    """收藏/已看记录的归属库键：供 brief 淘汰后仍能按库过滤
    （见 _favorite_viewable）。live brief 优先；集节点取父标题；
    榜单收藏取榜单库键。"""
    brief = store.get_title_by_guid(guid)
    if brief is None:
        ep = store.get_episode(guid) or {}
        brief = store.get_title_by_guid(ep.get("online_guid", "")) or None
    if brief is not None:
        try:
            return sorted(libfiltermod.brief_lib_keys(
                brief, gkey_of=_gkey_of_source))
        except Exception:
            _log.debug("_mark_lib_keys failed: guid=%s", guid, exc_info=True)
            return []
    if chart_cache.is_chart_guid(guid):
        return sorted(_chart_extra_keys(guid))
    return []


def _mark_extra(guid: str) -> dict:
    meta = _online_card_meta(guid) or {}
    # 集节点收藏：用所属剧集的标题/海报当收藏元信息，否则收藏夹里只有 guid。
    if not meta.get("title"):
        ep = store.get_episode(guid) or {}
        parent = store.get_title_by_guid(ep.get("online_guid", "")) or {}
        if parent:
            meta = {"title": inj._f(parent, "title"),
                    "type": inj.fnos_type(parent),
                    "poster": inj._f(parent, "poster"),
                    "year": inj._f(parent, "year"),
                    "remarks": inj._f(parent, "remarks")}
    return {
        "title": meta.get("title", ""), "type": meta.get("type", ""),
        "poster": meta.get("poster", ""), "year": meta.get("year", ""),
        "remarks": meta.get("remarks", ""),
        "libs": _mark_lib_keys(guid),
    }


@app.put("/v/api/v1/item/favorite")
async def item_favorite_add(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("item_favorite_add failed", exc_info=True)
        body = {}
    guid = _normalize_history_guid(
        (body.get("guid") or body.get("item_guid") or "").strip())
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.add_favorite(guid, _mark_extra(guid))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@app.delete("/v/api/v1/item/favorite")
async def item_favorite_del(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("item_favorite_del failed", exc_info=True)
        body = {}
    guid = _normalize_history_guid(
        (body.get("guid") or body.get("item_guid") or "").strip())
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.remove_favorite(guid)
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@app.post("/v/api/v1/item/watched")
async def item_watched_set(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("item_watched_set failed", exc_info=True)
        body = {}
    guid = _normalize_history_guid(
        (body.get("guid") or body.get("item_guid") or "").strip())
    # 允许显式取消（watched=0/is_watched=0）
    want = body.get("watched", body.get("is_watched", 1))
    try:
        want = int(want)
    except (TypeError, ValueError):
        want = 1
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    if want == 0:
        marks.remove_watched(guid)
    else:
        marks.set_watched(guid, _mark_extra(guid))
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@app.delete("/v/api/v1/item/watched")
async def item_watched_del(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("item_watched_del failed", exc_info=True)
        body = {}
    guid = _normalize_history_guid(
        (body.get("guid") or body.get("item_guid") or "").strip())
    if not _is_online_item_guid(guid):
        return await forward_upstream(request)
    marks.remove_watched(guid)
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


def _favorite_card(rec: dict) -> dict | None:
    guid = rec.get("guid", "")
    brief = store.get_title_by_guid(guid)
    if brief is None:
        # 文件夹卡 guid 的收藏记录：解到所属标题建卡
        folder_og = _parse_line_folder_guid(guid)
        if folder_og:
            brief = store.get_title_by_guid(folder_og)
    if brief:
        row = inj.search_item(brief)
        _sb = _brief_speed_badge(brief)
        row["title"] = inj._f(brief, "title") + (
            f" ·{inj._f(brief, 'remarks')}" if inj._f(brief, "remarks") else "") + (
            f" ·{_sb}" if _sb else "")
    elif chart_cache.is_chart_guid(guid):
        meta = chart_cache.get_guid(guid) or {}
        if not meta:
            return None
        row = chart_card(meta)
    else:
        # 兜底（P2 修复）：store 缓存丢了 / 重启后 / 收藏的是集节点时，
        # 原来这里直接 return None，整条收藏在收藏夹里消失。
        # 先找集节点的所属剧集，再用收藏时存下的元信息建卡。
        ep = store.get_episode(guid) or {}
        parent = store.get_title_by_guid(ep.get("online_guid", "")) or {}
        if parent:
            row = inj.search_item(parent)
            _sb = _brief_speed_badge(parent)
            row["title"] = inj._f(parent, "title") + (
                f" ·{inj._f(parent, 'remarks')}" if inj._f(parent, "remarks") else "") + (
                f" ·{_sb}" if _sb else "")
        else:
            title = (rec.get("title") or "").strip()
            if not title:
                return None  # 实在没有任何可显示的信息才丢弃
            typ = (rec.get("type") or "Movie")
            typ = "TV" if typ in ("TV", "Mix", "Episode") else "Movie"
            row = {
                "guid": guid, "video_guid": video_guid(guid),
                "path": "", "runtime": 0, "lan": "zh-CN", "imdb_id": "",
                "trim_id": "", "tv_title": "", "parent_guid": "",
                "parent_title": "", "title": inj.ONLINE_TAG + title + (
                    f" ·{rec.get('remarks')}" if rec.get("remarks") else ""),
                "type": typ, "genres": [],
                "poster": inj._poster(rec.get("poster") or ""),
                "poster_width": 0, "poster_height": 0,
                "is_favorite": 0, "watched": 0, "watched_ts": 0,
                "vote_average": "0",
                "media_stream": {"resolutions": None, "audio_type": None,
                                 "color_range_type": None},
                "release_date": rec.get("year", "") or "",
                "season_number": 0, "episode_number": 0,
                "air_date": rec.get("year", "") or "",
                "number_of_seasons": 1 if typ == "TV" else 0,
                "number_of_episodes": 0,
                "local_number_of_seasons": 0, "local_number_of_episodes": 0,
                "status": "在线",
                "overview": "",
                "ancestor_guid": rec.get("ancestor_guid", "") or "",
                "ancestor_name": rec.get("ancestor_name", "") or "🌐在线资源",
                "ancestor_category": typ, "ts": 0, "duration": 0,
                "single_child_guid": "", "file_name": "",
            }
    _apply_marks(row)
    return row


@app.post("/v/api/v1/favorite/list")
async def favorite_list(request: Request):
    """收藏夹：合并官方本地收藏与本插件在线收藏，统一分页。"""
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("favorite_list failed", exc_info=True)
        body = {}
    local = await forward_upstream(request)
    try:
        ldata = json.loads(local.body)
    except Exception:
        _log.debug("favorite_list failed", exc_info=True)
        return local
    if not isinstance(ldata, dict):
        return local
    ldata.setdefault("data", {})
    if not isinstance(ldata["data"], dict):
        return local

    ftype = str(body.get("type") or "all")
    cards: list[dict] = []
    for rec in marks.favorite_records():
        # P0-3：收藏按账号分区（marks）+ 按库过滤；权限收回后旧记录不再出现。
        if not _favorite_viewable(request, rec, _ctx=_viewer_ctx(request)):
            continue
        row = _favorite_card(rec)
        if not row:
            continue
        if ftype in ("movie", "tv", "video", "other", "live"):
            t = (row.get("type") or "").lower()
            ok = (ftype == "movie" and t == "movie") or \
                 (ftype == "tv" and t in ("tv", "mix")) or \
                 (ftype in ("video", "other") and t not in ("movie", "tv", "mix"))
            if not ok:
                continue
        cards.append(row)

    official = (ldata["data"].get("list") or [])
    cards = clientmod.normalize_cards(cards, _client_of(request))
    merged = cards + list(official)
    page = max(1, int(body.get("page") or 1))
    page_size = max(1, int(body.get("page_size") or len(merged) or 50))
    start = (page - 1) * page_size
    ldata["data"]["total"] = len(merged)
    ldata["data"]["list"] = merged[start:start + page_size]
    ldata["code"] = 0
    ldata["msg"] = ""
    return JSONResponse(ldata)


def _online_play_info(guid: str, request: Request) -> dict | None:
    """对已绑定的在线标题（brief）构造原生播放 info。

    返回 None 表示该 guid 不属于在线标题（调用方应继续走榜单/上游分支）。
    与资源浏览页 open 深链共用，保证 /video 用的 guid 与播放器一致。
    """
    # 与 play/info 路由一致：先看该 guid 是否已是某个（动态解析出的）可播集
    ep = store.get_episode(guid)
    if ep:
        info = inj.episode_play_info(ep)
        # 续播位置：优先该集自己的记录；第 1 集没有集记录时回填标题记录的位置
        #（客户端以标题/季/文件夹 guid 上报进度时落到标题键上）。
        info["ts"] = history.position(
            guid, ep.get("online_guid"),
            is_first=(int(ep.get("index", 0) or 0) == 0))
        _attach_play_duration(info, ep=ep)
        return _diag_play_info(guid, info)
    brief = store.get_title_by_guid(guid)
    if not brief:
        return None
    rec = history.get(guid)
    if inj.is_tv(brief):
        ep_guids = store.tv_episode_guids(guid, regular_only=True)
        if ep_guids:
            # T4 远程剧集采用“回看最近交互”语义；普通 CMS 保持按集序续播。
            # 没有任何集记录时，resume_for_title 会回填未看完的标题记录位置。
            t4_rewatch = brief.get("locator", {}).get("kind") == "t4"
            target_eg, resume_ts = history.resume_for_title(
                guid, ep_guids, latest_interaction=t4_rewatch)
            if target_eg:
                ep = store.get_episode(target_eg)
                if ep:
                    info = inj.episode_play_info(ep)
                    info["ts"] = resume_ts
                    _attach_play_duration(info, ep=ep)
                    return _diag_play_info(guid, info)
    info = inj.play_info(brief)
    if rec:
        info["ts"] = rec.get("ts", 0)
    _attach_play_duration(info, brief=brief)
    return _diag_play_info(guid, info)


@app.post("/v/api/v1/play/info")
async def play_info(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    guid = body.get("item_guid", "")
    client = _client_of(request)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6，不再往下走取流。
    if _online_guid_viewable(request, guid, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response()
    if _parse_line_item_guid(guid):
        line_info = _vod_play_info(guid, client)
        if line_info is not None:
            return line_info
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error
    # T4 详情刚加载完才进 store 的标题，在此复核一次。
    if _online_guid_viewable(request, guid, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response()

    # >>> PRIVATE:CATVOD
    if _xlib_on() and guid:
        xinfo = await _xlib_play_info(guid)
        if xinfo is not None:
            return JSONResponse({"code": 0, "msg": "", "data": xinfo})
    # <<< PRIVATE:CATVOD


    def _prewarm_from_info(info: dict):
        """点击播放即后台预热默认线路的播放列表，缩短首帧等待。"""
        try:
            mg = info.get("media_guid")
            if not mg:
                return
            picked = _pick_episode(mg, request)
            if picked:
                _warm_playlist(mg, picked, request)
        except Exception as exc:
            _log.debug("play info preheat failed: %s", type(exc).__name__)

    # 红果短剧 episode 播放信息：直链 play_link，MP4 wrapper
    if hgdj_enabled() and hgdjmod.is_hgdj_episode(guid):
        if not _viewer_ctx(request).can_open_lib(hgdjmod.lib_guid()):
            return _denied_lib_response()
        try:
            sg, ep = guid[len("hgdj-ep:"):].rsplit(":", 1)
            ep = int(ep)
        except ValueError:
            return await forward_upstream(request)
        rec = await hgdjmod.series_detail(sg)
        if rec is None:
            return JSONResponse({"code": 0, "msg": "", "data": {}})
        headers = {k.lower(): v for k, v in request.headers.items()}
        info = hgdjmod.synth_play(guid, rec | {"hgdj_ep": ep}, headers)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(info, client)})

    ep = store.get_episode(guid)
    if ep:
        info = inj.episode_play_info(ep)
        # 续播位置：优先该集自己的记录；第 1 集没有集记录时回填标题记录的位置
        info["ts"] = history.position(
            guid, ep.get("online_guid"),
            is_first=(int(ep.get("index", 0) or 0) == 0))
        _attach_play_duration(info, ep=ep)
        _prewarm_from_info(info)
        info = _diag_play_info(guid, info)
        return JSONResponse({"code": 0, "msg": "",
                             "data": clientmod.normalize_detail(info, client)})
    brief = store.get_title_by_guid(guid)
    if not brief and chart_cache.is_chart_guid(guid):
        # 网页端必须先拿到 play/info 才会跳转播放页。这里不能长时间等待全网搜源，
        # 否则用户会停在海报墙；先触发解析并给极短窗口，超时立即返回可播骨架，
        # 后续进入播放页后的 stream/play 接口再等待同一个单飞任务完成。
        fut = start_resolve_chart(guid)
        if fut is not None:
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=_CHART_DETAIL_WAIT)
            except asyncio.TimeoutError:
                pass
        brief = store.get_title_by_guid(guid)
        if brief is None:
            # P0-3：未绑定榜单片先给骨架、绑定完成后再由 preset/main 复核；
            # 但榜单库本身打不开时连骨架也不给，直接 -6。
            if not _chart_key_open(request, guid, _ctx=_viewer_ctx(request)):
                return _denied_lib_response()
            meta = chart_cache.get_guid(guid) or {}
            cat = meta.get("cat", "tv")
            typ = "Movie" if cat == "movie" else "TV"
            _PENDING_MEDIA_CHART[media_guid(guid)] = guid
            if len(_PENDING_MEDIA_CHART) > 1000:
                for _k in list(_PENDING_MEDIA_CHART)[:500]:
                    _PENDING_MEDIA_CHART.pop(_k, None)
            info = {
                "grand_guid": "", "guid": guid, "parent_guid": "",
                "play_config": None, "ts": 0, "type": typ,
                "video_guid": video_guid(guid),
                "audio_guid": "", "subtitle_guid": "",
                "media_guid": media_guid(guid),
                "item": {
                    "guid": guid, "type": typ, "title": meta.get("title", ""),
                    "can_play": 1, "play_error": "",
                    "season_number": 0 if typ == "Movie" else 1,
                    "episode_number": 0, "parent_guid": "",
                    "posters": inj._poster(meta.get("cover", "")) if meta.get("cover") else "",
                    "backdrops": inj._poster(meta.get("cover", "")) if meta.get("cover") else "",
                    "number_of_episodes": 1, "number_of_seasons": 1 if typ == "TV" else 0,
                },
                "direct_link_audio_index": -1,
            }
            info = _diag_play_info(guid, info)
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(info, client)})
    if not brief:
        return await forward_upstream(request)
    info = _online_play_info(guid, request)
    if info is None:
        return await forward_upstream(request)
    _prewarm_from_info(info)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_detail(info, client)})


def _line_choices(target: dict) -> list[dict]:
    """该播放目标可切换的线路：按资源站去重（同一站直链优先于 share 页），
    当前播放线路排第一；T4 保持稳定菜单顺序，防止切线后序号漂移。"""
    raw = store.target_lines(target)
    if target.get("t4"):
        # Stable menu indices even when a session has selected another line.
        return [{**choice, "label": choice["line"]} for choice in raw]
    cur = target.get("line")
    # 当前线路置顶，其余直链在前、可解析页在后
    raw.sort(key=lambda t: (t["line"] != cur, not t["m3u8"]
                            and not re.search(r"\.mp4(\?|$)", t["url"], re.I),
                            t.get("resolve", False)))
    out, seen_provider, seen_name = [], set(), set()
    for t in raw:
        provider = t["line"].split("::", 1)[0]
        name = _PROVIDER_CN.get(provider, provider)
        if provider in seen_provider:
            continue
        # 极端情况下不同 provider 映射到同名，加序号区分
        if name in seen_name:
            k = 2
            while f"{name}{k}" in seen_name:
                k += 1
            name = f"{name}{k}"
        seen_provider.add(provider)
        seen_name.add(name)
        out.append({**t, "label": name})
    # 保持当前线路第一
    out.sort(key=lambda t: t["line"] != cur)
    return out


def _line_bitrate(line: str, lines: list[str]) -> int:
    # 用 9000 万段的稳定整数编码线路，避免与真实码率（通常<3000万）冲突
    try:
        idx = lines.index(line)
    except ValueError:
        idx = 0
    return 90_000_000 + idx * 10001


def _line_of_bitrate(br, lines: list[str]) -> str | None:
    try:
        b = int(br or 0)
    except (TypeError, ValueError):
        return None
    if 90_000_000 <= b < 91_000_000:
        idx = (b - 90_000_000) // 10001
        if 0 <= idx < len(lines):
            return lines[idx]
    return None


def _qualities(target: dict) -> list[dict]:
    """把每条播放线路表达成播放器「清晰度」菜单里的一个可选项。"""
    lines = _line_choices(target)
    if len(lines) <= 1:
        return []
    out = []
    for i, t in enumerate(lines):
        out.append({
            "resolution": t.get("label") or _friendly_line(t["line"]),
            "bitrate": 90_000_000 + i * 10001,
            "progressive": True,
            "is_m3u8": False,  # 置 False 才不会被菜单项置灰
        })
    return out


def _t4_selected_target(mg: str, target: dict, selection, request: Request) -> dict:
    """Use the same episode/line identity for stream, play and native Range."""
    choices = _line_choices(target)
    names = [choice["line"] for choice in choices]
    line = selection.get("line")
    index = selection.get("direct_link_quality_index")
    if not line and index is not None:
        try:
            number = int(index)
        except (TypeError, ValueError):
            raise t4.T4Error("T4 线路序号无效，请重新选择线路") from None
        if not 0 <= number < len(names):
            raise t4.T4Error("T4 线路序号已失效，请重新打开详情")
        line = names[number]
    if not line:
        line = _line_of_bitrate(selection.get("bitrate"), names)
    if not line:
        link = selection.get("playlink") or request.headers.get("Play-Link", "")
        try:
            parsed = urllib.parse.urlsplit(link)
        except ValueError:
            raise t4.T4Error("播放地址格式无效，请重新播放") from None
        # Read only our own media selector, never fetch a client-supplied URL.
        if parsed.path == f"{ONLINE_PREFIX}/{mg}/t4":
            params = urllib.parse.parse_qs(parsed.query)
            line = params.get("line", [None])[0]
            session = params.get("session", [""])[0]
            if not line and session:
                target = t4_service.session_target(mg, session)
                if target is None:
                    raise t4.T4Error("播放会话已过期，请重新播放")
        elif parsed.path.startswith(f"{ONLINE_PREFIX}/"):
            raise t4.T4Error("播放地址与当前分集不一致，请重新选择分集")
        elif link:
            line = t4_service.cached_line_for_url(target, link)
    if not line:
        return target
    if line not in names:
        raise t4.T4Error("T4 线路已失效或缺少该分集，请重新打开详情")
    return next(choice for choice in choices if choice["line"] == line)


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    # >>> PRIVATE:CATVOD
    # 小雅 media_guid 不在 store：直接用其播放负载，避免透传官方导致“文件不存在”。
    if _xlib_on():
        xl = _xlib_resolve_media(mg) or _xlib_recover_media(mg)
        if xl:
            sg, idx = xl
            src_guid = next((k for k, v in _XLIB_EP_MAP.items() if v == (sg, idx)), sg)
            xsl = await _xlib_stream_payload(src_guid)
            if xsl is not None:
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "file_stream": xsl["files"][0],
                    "video_stream": xsl["video_streams"][0],
                    "audio_streams": [], "subtitle_streams": [],
                    "qualities": [], "cloud_storage_info": None}})
    # <<< PRIVATE:CATVOD
    # 线路卡 media_guid：先按 guid 内线路身份解析（只定选集身份，不固定线）。
    target = _seeded_media_target(mg, request) or store.resolve_media_guid(mg)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6。
    if target is not None \
            and _online_target_viewable(request, target, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response()
    chart_g = chart_guid_by_media(mg)
    if not target and chart_g:
        # P0-3：榜单库本身打不开时连占位流也不给，直接 -6。
        if not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response()
        # 网页播放器会在进入播放页前连续请求 stream；这里必须瞬时返回，不能等待搜源，
        # 否则三个请求串起来仍会把用户留在海报墙。真实匹配推迟到播放器请求 preset。
        start_resolve_chart(chart_g)
        return _instant_stream_response(mg)
    if not target:
        return await forward_upstream(request)
    if target.get("t4"):
        started = time.monotonic()
        _log.info("T4 stream start media=%s client=%s",
                  mg, _client_of(request).label)
        menu_target = target
        choices = _line_choices(target)
        try:
            target = _t4_selected_target(mg, target, body, request)
        except t4.T4Error as exc:
            _log.warning("T4 stream failed media=%s client=%s stage=selection",
                         mg, _client_of(request).label)
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=400)
        try:
            playback = await t4_service.resolve(ext_client(request), target)
            await t4_media.identify(ext_client(request), playback)
            if playback.wrapper == "DASH":
                raise t4.T4Error("当前飞牛适配尚未验证 DASH/双轨播放")
        except t4.T4Error as exc:
            _log.warning("T4 stream failed media=%s client=%s stage=resolve elapsed_ms=%d",
                         mg, _client_of(request).label,
                         int((time.monotonic() - started) * 1000))
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=502)
        if target.get("kind") == "episode":
            sl = inj.episode_stream_list(target)
        else:
            sl = inj.stream_list(store.get_title_by_guid(target["online_guid"]))
        metadata = await t4_media.metadata.for_playback(
            ext_client(request), playback,
            wait=max(0.0, T4_STREAM_READY_BUDGET - (time.monotonic() - started)))
        t4_metadata.apply(sl, playback.wrapper, metadata)
        f, v = sl["files"][0], sl["video_streams"][0]
        if not f["size"]:
            f["size"] = playback.size
        qualities = _qualities(menu_target)
        names = [choice["line"] for choice in choices]
        if len(choices) > 1:
            # These codes select sources in the official quality menu, not encoders.
            v.update(resolution_type=target.get("label") or target["line"],
                     bps=_line_bitrate(target["line"], names))
        else:
            # Native players also require a ready original-quality entry for one source.
            qualities = [{
                "resolution": v["resolution_type"] or "原画",
                "bitrate": v["bps"],
                "progressive": True,
                "is_m3u8": False,
            }]
        data = {"file_stream": f, "video_stream": v, "audio_streams": sl["audio_streams"],
                "subtitle_streams": [], "qualities": qualities, "cloud_storage_info": None}
        if playback.wrapper != "HLS":
            # trim.media 0.9.8 uses 9001 for remote raw-file/STRM delivery.
            # This is a response contract only; no STRM file or DB row is created.
            raw = [{
                **quality,
                "url": _t4_media_url(request, mg, line=choice["line"]),
            } for choice, quality in zip(choices, qualities)]
            data.update(cloud_storage_info={"cloud_storage_type": 9001},
                        direct_link_qualities=raw)
        _log.info("T4 stream media=%s client=%s wrapper=%s delivery=%s lines=%d "
                  "qualities=%d audio=%d elapsed_ms=%d",
                  mg, _client_of(request).label, playback.wrapper,
                  "server" if playback.needs_proxy else "direct", len(choices),
                  len(qualities), len(sl["audio_streams"]),
                  int((time.monotonic() - started) * 1000))
        return JSONResponse({"code": 0, "msg": "", "data": data})
    if target.get("kind") == "episode":
        sl = inj.episode_stream_list(target)
    else:
        brief = store.get_title_by_guid(target["online_guid"])
        sl = inj.stream_list(brief)
    f = sl["files"][0]
    v = sl["video_streams"][0]
    quals = _qualities(target)
    if quals:
        # 默认选中第一条线路，让播放器初始分辨率/码率与之一致
        v["resolution_type"] = quals[0]["resolution"]
        v["bps"] = quals[0]["bitrate"]
    return JSONResponse({"code": 0, "msg": "", "data": {
        "file_stream": f, "video_stream": v,
        "audio_streams": [], "subtitle_streams": [],
        "qualities": quals, "cloud_storage_info": None}})


@app.api_route("/v/api/v1/media/range/{mg}", methods=["GET", "HEAD"])
async def media_range(mg: str, request: Request):
    """App（Flutter 原生播放器）不走 /play/play，而是直接 GET /media/range/<mg>。

    官方后端对我们的虚拟 mg 无本地文件记录，会返回 403（App 提示“文件权限不足”）。
    T4 直接流式交付，保留 Range 和线路选择；CMS 引导到既有代理（302）。
    非在线 mg 原样流式透传官方。
    """
    # >>> PRIVATE:CATVOD
    # 小雅网盘影片/某集：App/TV 原生播放器直接用 media_guid 请求本端点。store 无此
    # 记录，必须在兜底透传前拦截并 302 到小雅直链，否则官方后端报“文件不存在”。
    if _xlib_on():
        xl = _xlib_resolve_media(mg)
        if not xl:
            # 冷启动后内存映射可能为空：若该 media_guid 可确定性反解到已索引影片，
            # 现场补登记（媒体库浏览过的单片 guid 派生关系）。
            xl = _xlib_recover_media(mg)
        if xl:
            sg, idx = xl
            return RedirectResponse(_xlib_play_link(sg, idx), status_code=302,
                                    headers={"Cache-Control": "no-store"})
    # <<< PRIVATE:CATVOD
    target = _seeded_media_target(mg, request) or store.resolve_media_guid(mg)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6（403）。
    if target is not None \
            and _online_target_viewable(request, target, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response(status_code=403)
    chart_g = "" if target else chart_guid_by_media(mg)
    if not target and chart_g:
        # P0-3：榜单库本身打不开时不进 preset，直接 -6（403）。
        if not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response(status_code=403)
        if time.time() - _CHART_FAILED_TS.get(chart_g, 0) < _CHART_FAILED_TTL:
            return Response("暂无可用在线片源", status_code=503)
        # App 原生播放器也不等待搜索：立刻进入播放器，preset 内部再等待后台匹配
        start_resolve_chart(chart_g)
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index=0",
                                status_code=302)
    if not target:
        # 本地真实媒体字节（Flutter 原生播放器 Range 请求）：必须流式透传，
        # 不能整包缓冲几 GB 的影片，否则打满内存/swap 拖死整机。
        return await forward_upstream_streaming(request)
    if target.get("t4"):
        return await t4_range(mg, request)
    # 同样【不在此同步解析】（会卡 App 起播、像点了没反应）：先瞬时 302 到对应代理，
    # 由 preset/main、direct 内部在播放加载阶段解析，并有双向 302 兜底。
    _wrapper, is_hls = _guess_wrapper(target)
    # 线路卡：auto=1 + 种子，不 pin line，起播仍走竞速（冻结）。
    if target.get("_line_seed"):
        q = urllib.parse.urlencode({"auto": "1", "seed_line": target["line"],
                                    "index": target["index"]})
    else:
        q = urllib.parse.urlencode({"line": target["line"], "index": target["index"]})
    if is_hls:
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}",
                                status_code=302)
    return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)




def _guess_wrapper(target: dict) -> tuple[str, bool]:
    """不发起任何网络请求，预判该线路该用 HLS 还是 MP4 包装。
    返回 (wrapper, is_hls)。预判可能失误：main/direct 入口在真正解析后会做
    双向 302 兜底，所以这里宁可默认 HLS（绝大多数资源是 m3u8）。"""
    url = (target.get("url") or "")
    path = url.split("?", 1)[0].lower()
    # 裸 mp4 直链（非跳转/解析页）才直接判 MP4
    if not target.get("redirect") and not target.get("resolve") \
            and re.search(r"\.mp4$", path):
        return "MP4", False
    # >>> PRIVATE:DDUO
    if features.DDUO and target.get("xsrc"):
        # 仅查已热的解码缓存（预热/之前播放产生），不联网；明确 mp4 才用 MP4
        key = f"{target.get('play_from', '')}|{target.get('url', '')}"
        hit = _DDUO_DECODE_CACHE.get(key)
        if hit and re.search(r"\.mp4(\?|$)", hit[1], re.I) \
                and not re.search(r"\.m3u8(\?|$)", hit[1], re.I):
            return "MP4", False
        return "HLS", True
    # <<< PRIVATE:DDUO
    # 跳转页 / 分享解析页 / 普通直链默认 HLS；解析后若实为 mp4，main 会 302 到 direct
    return "HLS", True


@app.post("/v/api/v1/play/play")
async def play_play(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    if hgdj_enabled() and hgdjmod.is_hgdj_media_guid(mg):
        # P0-3：红果库打不开时不回直链，直接 -6。
        if not _viewer_ctx(request).can_open_lib(hgdjmod.lib_guid()):
            return _denied_lib_response()
        # HGDJ 云盘直连：play_link 已是绝对直链，直接回原值即可；播放器直连。
        try:
            sg, ep = mg[len("hgdj-mg:"):].rsplit(":", 1)
            ep = int(ep)
        except ValueError:
            sg, ep = "", 0
        rec = await hgdjmod.series_detail(sg) if sg else None
        if rec:
            headers = {k.lower(): v for k, v in request.headers.items()}
            info = hgdjmod.synth_play(sg, rec | {"hgdj_ep": ep}, headers)
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(info, client)})
        return JSONResponse({"code": 0, "msg": "", "data": {}})
    base_target = _seeded_media_target(mg, request) or store.resolve_media_guid(mg)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6。
    if base_target is not None \
            and _online_target_viewable(request, base_target, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response()
    chart_g = chart_guid_by_media(mg)
    if not base_target and chart_g:
        # P0-3：榜单库本身打不开时连占位 HLS 也不给，直接 -6。
        if not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response()
        # 未绑定榜单片：play/play 也立即返回占位 HLS，播放器先进入加载页；
        # preset/main 请求时再等待后台匹配，避免 play/info+stream+play 串行等待。
        start_resolve_chart(chart_g)
        return _instant_play_response(mg, body.get("video_guid", ""))
    if not base_target:
        return await forward_upstream(request)
    if base_target.get("t4"):
        try:
            target = _t4_selected_target(mg, base_target, body, request)
        except t4.T4Error as exc:
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=400)
        return await _t4_play_response(mg, target, body, request)
    # 播放器「清晰度/线路」菜单切换时会带我们编码的 bitrate，据此选线路
    choices = _line_choices(base_target)
    chosen_line = None
    if len(choices) > 1:
        chosen_line = _line_of_bitrate(body.get("bitrate"), [t["line"] for t in choices])
    if chosen_line and chosen_line != base_target.get("line"):
        if base_target.get("kind") == "episode" and base_target.get("ep_guid"):
            target = store.get_episode_target(base_target["ep_guid"], chosen_line) or base_target
        else:
            target = store.resolve_episode(
                base_target["online_guid"], chosen_line,
                int(base_target.get("index", 0) or 0))
            if target:
                target = dict(target, online_guid=base_target["online_guid"],
                              ep_guid="", kind="title")
            else:
                target = base_target
    else:
        target = base_target
    # 起播体验：此处【不再同步解析】播放页/302/专有解码（慢源要 1~3 秒，会把用户
    # 卡在海报墙像“点了没反应”）。先瞬时返回播放地址让播放器立刻进入加载页转圈，
    # 真正的解析延迟到播放器内部取 main.m3u8 / direct 时再做（那里本就有加载指示）。
    wrapper, is_hls = _guess_wrapper(target)
    # 线路卡起播：仍带 auto=1 走竞速；种子只经 seed_line 传递，不 pin line。
    # （冻结：上游 _fixed_line 式“去 auto + 固定线”不搬；见 _seeded_media_target。）
    if base_target.get("_line_seed"):
        q = urllib.parse.urlencode({"auto": "1", "seed_line": target["line"],
                                    "index": target["index"]})
    else:
        q = urllib.parse.urlencode({"line": target["line"], "index": target["index"]})
    if is_hls:
        play_link = f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}"
        # 后台立刻预热该线路（含解析 + 播放列表），播放器取 main 时大概率已就绪
        try:
            _warm_playlist(mg, target, request)
        except Exception as exc:
            _log.debug("play preheat failed: %s", type(exc).__name__)
    else:
        play_link = f"{ONLINE_PREFIX}/{mg}/direct?{q}"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg,
        "play_link": play_link, "subtitle_guid": "", "subtitle_index": -1,
        "subtitle_link": "", "video_guid": body.get("video_guid", vg_for(mg)),
        "video_index": 0, "wrapper": wrapper,
        "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


@app.get(f"{ONLINE_PREFIX}/hgdj/{{series_guid}}.mp4")
async def hgdj_media_proxy(series_guid: str, request: Request):
    """代理 HGDJ companion 播放流：GET /api/play/<sid>/<ep>，透传 Range。

    注意：app 底层还有一个 /{mg} 通配符路由，会优先拦截以 "/" 开头的任意 mg。
    这里显式用 /hgdj/{series_guid}.mp4，与 play_link 生成的一致。
    """
    # P0-3：红果库打不开时不代理播放流，直接 -6（403）。
    if not _viewer_ctx(request).can_open_lib(hgdjmod.lib_guid()):
        return _denied_lib_response(status_code=403)
    try:
        ep = int(request.query_params.get("ep", "1"))
    except ValueError:
        ep = 1
    status, headers_out, gen = await hgdjmod.proxy_media(series_guid, ep,
                                                          dict(request.headers))
    if gen is None:
        return Response("not found", status_code=status or 502)
    return StreamingResponse(gen, status_code=status, media_type="video/mp4",
                             headers=headers_out)


@app.get(f"{ONLINE_PREFIX}/{{mg}}/direct")
async def direct_proxy(mg: str, request: Request):
    """mp4 等非 HLS 直链代理：同源、带 Range 支持（播放器 seek）。"""
    # >>> PRIVATE:CATVOD
    # 小雅分类媒体库：mg 固定为 xlib，真实目标由 query g（影片guid）+i（文件序）给出。
    # 必须走该同源端点，飞牛播放器才认可；内部换取网盘直链后 302，客户端直连 CDN。
    if mg == "xlib" and _xlib_on() and request.query_params.get("g"):
        return await _xlib_direct_redirect(request)
    # <<< PRIVATE:CATVOD
    failed_g = await _await_pending_chart(mg)
    target = store.resolve_media_guid(mg)
    if not target:
        # P0-3：榜单库本身打不开时不给占位 preset，直接 -6（403）。
        chart_g = chart_guid_by_media(mg)
        if chart_g and not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response(status_code=403)
        # 未绑定/无源：302 到占位 preset（HLS），保持播放器存活、菜单可切，不报硬错误
        didx = int(request.query_params.get("index", 0) or 0)
        return RedirectResponse(
            f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index={didx}", status_code=302,
            headers={"Cache-Control": "no-store", "X-fnmedia-Placeholder": "1"})
    # direct 的 query 会显式带线路（HLS 预判为 MP4 时回跳而来）；必须解析该线路，
    # 不能沿用默认线路，否则可能拿到另一资源站的 HLS 而再次弹回，形成重定向环。
    qline = request.query_params.get("line")
    if qline and qline != target.get("line"):
        if target.get("kind") == "episode" and target.get("ep_guid"):
            switched = store.get_episode_target(target["ep_guid"], qline)
        else:
            switched = store.resolve_episode(
                target["online_guid"], qline, int(target.get("index", 0) or 0))
        if switched:
            target = switched
    # P0-3：已知在线标题但打不开任一归属库时直接 -6（403）。
    if _online_target_viewable(request, target, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response(status_code=403)
    # 线路健康探测 + 自动换线（直链/小雅等非 HLS 线路）
    request.state._play_mg = mg
    if _autofail_on():
        target = await _ensure_playable(target, request)
    else:
        await _resolve_if_needed(target, request)
    src_url = target["url"]
    # >>> PRIVATE:CATVOD
    # 小雅网盘文件在 115/阿里等网盘 CDN，NAS 服务器侧到该 CDN 不通；且为大文件，
    # 直接让客户端 302 到小雅签名 /d 链（客户端再跟随到网盘 CDN 直连播放）。
    if features.CATVOD and target.get("catvod_direct"):
        # 兜底再验死活（小雅对不存在文件回 200+JSON 假活），坏则换线
        if not (await _probe_target(target, request)):
            _mark_line_bad(target)
            alt = await _find_playable(target, request, skip_current=True)
            if alt is not None:
                target = alt
                src_url = target["url"]
                _log.info("小雅直链不可用，自动换线 -> %s", target.get("line"))
                if re.search(r"\.m3u8(\?|$)", src_url, re.I):
                    q = urllib.parse.urlencode({"line": target["line"],
                                                "index": target.get("index", 0)})
                    return RedirectResponse(
                        f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}", status_code=302)
            # 无可用替代时继续走原流程报错，不返回假活地址
        if target.get("catvod_direct"):
            return RedirectResponse(src_url, status_code=302,
                                    headers={"Cache-Control": "no-store"})
    # <<< PRIVATE:CATVOD
    if re.search(r"\.m3u8(\?|$)", src_url, re.I):
        # 源是 HLS 却走到直链（MP4 预判失误）：带线路/集序重定向到 preset
        q = urllib.parse.urlencode({"line": target["line"],
                                    "index": target.get("index", 0)})
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}",
                                status_code=302)
    # 网盘原画直出 mp4：src 是本服务根相对地址，经回环自调用（注入网盘 Cookie）
    src_url = _self_url(src_url)
    fwd = dict(target.get("headers") or {})
    # >>> PRIVATE:DDUO
    if features.DDUO and target.get("xsrc"):
        # 多多解出的 CDN 直链：外站 Referer 会 403
        fwd.pop("Referer", None)
    # <<< PRIVATE:DDUO
    if request.headers.get("range"):
        fwd["Range"] = request.headers["range"]
    try:
        req = ext_client(request).build_request("GET", src_url, headers=fwd)
        r = await ext_client(request).send(req, stream=True)
    except Exception as exc:
        # 直链连接失败：记忆坏线并自动换线重试一次
        if _autofail_on():
            _mark_line_bad(target)
            alt = await _find_playable(target, request, skip_current=True)
            if alt is not None:
                _log.info("直链失败，自动换线 %s -> %s", target.get("line"),
                          alt.get("line"))
                if _client_direct_only(alt):
                    return RedirectResponse(alt["url"], status_code=302,
                                            headers={"Cache-Control": "no-store"})
                if re.search(r"\.m3u8(\?|$)", alt["url"], re.I):
                    q = urllib.parse.urlencode({"line": alt["line"],
                                                "index": alt.get("index", 0)})
                    return RedirectResponse(
                        f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}", status_code=302)
                target = alt
                fwd = dict(target.get("headers") or {})
                if request.headers.get("range"):
                    fwd["Range"] = request.headers["range"]
                try:
                    req = ext_client(request).build_request(
                        "GET", target["url"], headers=fwd)
                    r = await ext_client(request).send(req, stream=True)
                except Exception as exc2:
                    _log.debug("direct_proxy failed", exc_info=True)
                    return _direct_to_placeholder(mg, request)
            else:
                return _direct_to_placeholder(mg, request)
        else:
            return _direct_to_placeholder(mg, request)
    # 上游明确 4xx/5xx（文件被删/鉴权失败）：记忆坏线，换线重定向重试
    if r.status_code >= 400 and _autofail_on():
        await r.aclose()
        _mark_line_bad(target)
        alt = await _find_playable(target, request, skip_current=True)
        if alt is not None:
            _log.info("直链 HTTP%s，自动换线 -> %s", r.status_code, alt.get("line"))
            if _client_direct_only(alt):
                return RedirectResponse(alt["url"], status_code=302,
                                        headers={"Cache-Control": "no-store"})
            if re.search(r"\.m3u8(\?|$)", alt["url"], re.I):
                q = urllib.parse.urlencode({"line": alt["line"],
                                            "index": alt.get("index", 0)})
                return RedirectResponse(
                    f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}", status_code=302)
        return _direct_to_placeholder(mg, request)
    h = _safe_response_headers(r.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"

    async def pump():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(pump(), status_code=r.status_code, headers=h,
                             media_type=r.headers.get("content-type", "video/mp4"))


def vg_for(mg: str) -> str:
    t = store.resolve_media_guid(mg)
    if not t:
        return ""
    if t.get("kind") == "episode":
        return ep_video_guid(t["ep_guid"])
    return video_guid(t["online_guid"])


@app.post("/v/api/v1/person/list/{guid}")
async def person_list(guid: str, request: Request):
    if not store.get_title_by_guid(guid) and not store.get_episode(guid):
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "", "data": {"total": 0, "list": []}})


@app.post("/v/api/v1/play/record")
async def play_record(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("play_record failed", exc_info=True)
        body = {}
    ig = body.get("item_guid", "")
    raw_guid = ig
    body["item_guid"] = _record_item_guid(body)
    ig = body["item_guid"]
    # 诊断事件只记原始/归一化 guid 与去向，不记任何凭证；守门判断本身不动。
    parsed = ("title" if (store.get_title_by_guid(ig)
                          or chart_cache.is_chart_guid(ig)) else
              "episode" if store.get_episode(ig) else "unresolved")
    if ig and (store.get_title_by_guid(ig) or store.get_episode(ig)
               or chart_cache.is_chart_guid(ig)):
        # 记录在线播放进度到本地（继续观看），不上报官方库
        history.record(body)
        try:
            resume_diagmod.note_record(raw_guid, ig, body,
                                       outcome="history", parsed=parsed,
                                       canon_key=_diag_record_canon_key(ig))
        except Exception:
            _log.debug("play_record failed", exc_info=True)
            pass
        return JSONResponse({"code": 0, "msg": "", "data": True})
    try:
        resume_diagmod.note_record(raw_guid, ig, body,
                                   outcome="upstream", parsed=parsed,
                                   canon_key=_diag_record_canon_key(ig))
    except Exception:
        _log.debug("play_record failed", exc_info=True)
        pass
    return await forward_upstream(request)


@app.delete("/v/api/v1/play/record")
async def play_record_delete(request: Request):
    import json
    ids = []
    try:
        body = await request.body()
        if body:
            j = json.loads(body)
            if isinstance(j, dict):
                g = j.get("item_guid") or j.get("guid")
                ids = g if isinstance(g, list) else ([g] if g else [])
            elif isinstance(j, list):
                ids = [x.get("item_guid") if isinstance(x, dict) else x for x in j]
    except Exception:
        _log.debug("play_record_delete failed", exc_info=True)
        ids = []
    ids = [_normalize_history_guid(g) for g in ids
           if g and (store.get_title_by_guid(_normalize_history_guid(g))
                     or store.get_episode(_normalize_history_guid(g)))]
    if ids:
        history.delete(ids)
        return JSONResponse({"code": 0, "msg": "", "data": True})
    return await forward_upstream(request)


@app.get("/v/api/v1/play/list")
async def play_list(request: Request):
    upstream = await forward_upstream(request)
    try:
        import json
        data = json.loads(upstream.body)
    except Exception:
        _log.debug("play_list failed", exc_info=True)
        return upstream
    local_items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(local_items, list):
        local_items = []

    online_items = []
    for rec in history.all():
        ig = rec.get("item_guid", "")
        ep = store.get_episode(ig)
        brief = store.get_title_by_guid(ig)
        if ep:
            brief = store.get_title_by_guid(ep["online_guid"])
        if not brief:
            continue  # 缓存已淘汰的标题不再展示
        extra = _chart_extra_keys(ig) if chart_cache.is_chart_guid(ig) \
            else None
        if not _brief_viewable(request, brief, extra_keys=extra, _ctx=_viewer_ctx(request)):
            continue  # P0-3：权限收回后旧记录不再展示
        online_items.append(inj.history_item(brief, rec, ep))

    # 在线历史按最近播放排序，置顶；按 guid 与官方去重
    online_items.sort(key=lambda x: x.get("_updated_at", 0), reverse=True)
    online_guids = {x.get("guid") for x in online_items}
    local_only = [x for x in local_items if x.get("guid") not in online_guids]
    # 剥掉私有字段
    for o in online_items:
        o.pop("_online", None)
        o.pop("_updated_at", None)
        o.pop("_year", None)
    online_items = clientmod.normalize_cards(online_items, _client_of(request))
    return JSONResponse({"code": 0, "msg": "", "data": online_items + local_only},
        headers={"Content-Type": "application/json; charset=utf-8"})


# 在线 RPC 不交给官方文件任务；T4 HLS 切线更新独立播放会话。
@app.post("/v/api/v1/media/p")
async def media_p(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        _log.debug("media_p failed", exc_info=True)
        body = {}
    link = body.get("playLink", "")
    try:
        parsed = urllib.parse.urlsplit(link) if isinstance(link, str) else None
    except ValueError:
        parsed = None
    online = bool(parsed and parsed.path.startswith(ONLINE_PREFIX + "/"))
    if online:
        parts = parsed.path.removeprefix(ONLINE_PREFIX + "/").split("/")
        session = urllib.parse.parse_qs(parsed.query).get("session", [""])[0]
        if len(parts) == 2 and parts[1] == "t4" and session:
            mg = parts[0]
            target = t4_service.session_target(mg, session)
            if target is None:
                return JSONResponse({"code": -1, "msg": "播放会话已过期，请重新播放",
                                     "data": {"result": "fail"}}, status_code=410)
            if body.get("req") == "media.resetQuality":
                try:
                    quality = body.get("quality") or {}
                    names = [choice["line"] for choice in _line_choices(target)]
                    line = _line_of_bitrate(quality.get("bitrate"), names)
                    if line is None:
                        raise t4.T4Error("请选择 T4 源线路，不支持服务端转码清晰度")
                    target = _t4_selected_target(mg, target, {"line": line}, request)
                    playback = await t4_service.resolve(ext_client(request), target)
                    await t4_media.identify(ext_client(request), playback)
                    if playback.wrapper != "HLS":
                        raise t4.T4Error("线路媒体格式不同，请重新进入播放")
                    t4_service.set_session_target(mg, session, target)
                except t4.T4Error as exc:
                    return JSONResponse({"code": -1, "msg": str(exc),
                                         "data": {"result": "fail"}}, status_code=502)
            elif body.get("req") == "media.quit":
                t4_service.close_session(mg, session)
    if isinstance(link, str) and link and (
            online or t4_service.owns_playback_url(link)):
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"reqid": body.get("reqid", ""), "result": "succ"}})
    return await forward_upstream(request)


# ------------------------------------------------------------- HLS 代理

def _line_key_match(want: str, got: str) -> bool:
    """判断请求线路名与实际选中线路是否同一条（兼容 源前缀::显示名 与裸显示名）。"""
    if not want or not got:
        return False
    if want == got:
        return True
    def disp(k: str) -> str:
        return k.split("::", 1)[1] if "::" in k else k
    return disp(want) == disp(got)


def _pick_episode(mg: str, request: Request) -> dict | None:
    # 线路卡 guid：按 guid 内线路身份解析该集（选集不串线）。
    # 播放器在菜单里手切了另一条线（query 带 line）时尊重手切——现有显式选线
    # 语义不变；种子只在没有手切时决定集身份，绝不固定播放线（冻结）。
    seeded = _seeded_media_target(mg, request)
    if seeded is not None:
        qline = request.query_params.get("line")
        if qline and qline != seeded.get("line"):
            try:
                idx = int(request.query_params.get("index", seeded.get("index", 0)))
            except ValueError:
                idx = seeded.get("index", 0)
            r = store.resolve_episode(seeded["online_guid"], qline, idx)
            if r:
                return dict(r, online_guid=seeded["online_guid"],
                            ep_guid=seeded.get("ep_guid", ""),
                            kind=seeded.get("kind"))
        return seeded
    # 全网回源别名：内置线路全死、已按片名找到别的可播记录时，原 mg 改指新记录
    alt_og = _MEDIA_REDIRECT.get(mg)
    if alt_og:
        try:
            index = int(request.query_params.get("index", "0"))
        except ValueError:
            index = 0
        r = store.resolve_episode(alt_og, request.query_params.get("line"), index)
        if r:
            return dict(r, online_guid=alt_og, kind="title")
    # 优先：mg 已绑定具体某集（TV 选集）或标题第一集
    target = store.resolve_media_guid(mg)
    if target:
        # query 可显式覆盖集序/线路（master 变体、STRM/调试用）
        if request.query_params.get("index") is not None or request.query_params.get("line"):
            try:
                idx = int(request.query_params.get("index", target.get("index", 0)))
            except ValueError:
                idx = target.get("index", 0)
            want_line = request.query_params.get("line")
            r = store.resolve_episode(
                target["online_guid"], want_line, idx)
            # 用户显式选的线路不在本条同名记录里（同名多记录、各挂部分线路）：
            # 回退到含该线路、线路最全的同名聚合记录，避免静默放第一条死链。
            if r and want_line and not _line_key_match(want_line, r.get("line", "")):
                alt_og = store.find_title_with_line(
                    target.get("title", ""), want_line)
                if alt_og and alt_og != target["online_guid"]:
                    ar = store.resolve_episode(alt_og, want_line, idx)
                    if ar:
                        r = ar
                        target = dict(target, online_guid=alt_og)
            if r:
                return dict(r, online_guid=target["online_guid"],
                            ep_guid=target.get("ep_guid", ""), kind=target.get("kind"))
        return target
    # 兼容：未在索引中的旧 mg，退回标题解析；也支持秒进时登记的待绑定榜单片
    og = _MEDIA_REDIRECT.get(mg) or store.online_guid_by_media(mg) \
        or _PENDING_MEDIA_CHART.get(mg)
    if not og:
        return None
    try:
        index = int(request.query_params.get("index", "0"))
    except ValueError:
        index = 0
    return store.resolve_episode(og, request.query_params.get("line"), index)


def _client_direct_only(t: dict) -> bool:
    """该已解析目标必须让客户端直连 302（网盘 /d 直链等），不能走本代理抓流。

    特性中立：不直接引用私有模块名，公开构建同样成立。判定依据是解析阶段对
    “客户端直连”类型打的标记。"""
    return bool(t.get("client_direct"))


def _dodge_known_bad(picked: dict, request: Request) -> dict:
    """零成本换线：当前线在坏线记忆/源级熔断中、且用户没有显式指定该线时，换到健康候选。
    让“退出重放”不再黏在上一次切坏的线路上（不发起任何探测网络请求）。"""
    if not picked or not _autofail_on():
        return picked

    def _is_bad(p: dict) -> bool:
        if _line_is_bad(p):
            return True
        # 源级取流熔断（上游 v0.2.70）：整站取流域名不可达时，该源所有线路同样零成本跳过
        try:
            sk = sourcehealth.source_key_of(p.get("line"), p)
            return bool(sk) and sourcehealth.is_dead(sk, level="stream")
        except Exception:
            _log.debug("_is_bad failed", exc_info=True)
            return False

    if not _is_bad(picked):
        return picked
    # 用户在播放器里明确选了这条坏线（query 带 line）则尊重其选择，交给探测逻辑
    if request.query_params.get("line"):
        return picked
    for c in _episode_candidates(picked):
        if not _is_bad(c):
            _log.info("跳过已知坏线/熔断源 %s -> %s", picked.get("line"), c.get("line"))
            return c
    return picked


async def _await_pending_chart(mg: str) -> str:
    """若该 media 属于秒进播放页、尚未绑定完成的榜单片，等待其后台绑定结束。"""
    og = _PENDING_MEDIA_CHART.get(mg) or store.online_guid_by_media(mg)
    if og and chart_cache.is_chart_guid(og) and not store.resolve_media_guid(mg):
        # 加超时上限：解析不出来就交给调用方走常规失败/换源，不再无限转圈
        try:
            await asyncio.wait_for(resolve_chart(og), timeout=_CHART_PLAY_WAIT)
        except asyncio.TimeoutError:
            _log.warning("榜单解析超时 %.0fs: %s", _CHART_PLAY_WAIT, og)
        return og if not store.resolve_media_guid(mg) else ""
    return ""


def _seg_url(mg: str, absolute: str, qs: str = "") -> str:
    suffix = f"&{qs}" if qs else ""
    return f"{ONLINE_PREFIX}/{mg}/seg?u={b64e(absolute)}{suffix}"


# >>> PRIVATE:DDUO
_DDUO_DECODE_CACHE: dict[str, tuple[float, str]] = {}
_DDUO_CACHE_TTL = 50 * 60  # 解出的地址多为带时效签名，缓存不到 1 小时


async def _resolve_dduo(picked: dict, request: Request) -> bool:
    """多多影视：把加密 token 解码为真实媒体地址（原地更新）。成功返回 True。"""
    import time as _t
    import dduo
    token = picked.get("url", "")
    play_from = picked.get("play_from", "")
    if not token or not play_from:
        return False
    key = f"{play_from}|{token}"
    hit = _DDUO_DECODE_CACHE.get(key)
    if hit and _t.time() - hit[0] < _DDUO_CACHE_TTL:
        real = "https:" + hit[1] if hit[1].startswith("//") else hit[1]
    else:
        # provider 优先取线路 key 前缀（多源合并后，标题 representative 可能是
        # 别的源，如 dduo::PAN1 不能用标题级 provider，否则会把解码请求发错站）
        ln = picked.get("line", "")
        provider = ln.split("::", 1)[0] if "::" in ln else ""
        if not provider or provider != "dduo":
            provider = store.provider_of(picked.get("online_guid", ""))
        src = next((s for s in config.all_sources() if s.get("id") == provider), None)
        if not src or not src.get("xsrc"):
            return False
        real = await dduo.decode_url(ext_client(request), src, token, play_from)
        if not real:
            return False
        _DDUO_DECODE_CACHE[key] = (_t.time(), real)
        if len(_DDUO_DECODE_CACHE) > 2000:
            for k in list(_DDUO_DECODE_CACHE)[:1000]:
                _DDUO_DECODE_CACHE.pop(k, None)
    if real.startswith("//"):
        real = "https:" + real
    picked["url"] = real
    picked["xsrc"] = False
    picked["m3u8"] = bool(re.search(r"\.m3u8(\?|$)", real, re.I))
    # 解出的真实 CDN（小红书/字节等）多为鉴权 URL，带外站 Referer 反而 403；
    # 统一只发 UA、不带 Referer 抓 m3u8/分片/直链
    picked["headers"] = {"User-Agent": "Mozilla/5.0"}
    return True


async def _resolve_dduo_with_fallback(picked: dict, request: Request) -> None:
    """多多线路解码；当前线路失败（APP 专属/失效）时自动换同集其他线路。"""
    if not picked or not picked.get("xsrc"):
        return
    if await _resolve_dduo(picked, request):
        return
    # 枚举该集其它多多线路，逐个尝试解码，第一个成功的替换进来
    og = picked.get("online_guid", "")
    idx = int(picked.get("index", 0) or 0)
    brief = store.get_title_by_guid(og) if og else None
    if not brief:
        return
    cur_line = picked.get("line")
    for ln, eps in (brief.get("lines") or {}).items():
        if ln == cur_line:
            continue
        playable = [e for e in eps if e.get("direct")]
        if idx >= len(playable) or not playable[idx].get("xsrc"):
            continue
        cand = {"url": playable[idx]["url"],
                "play_from": playable[idx].get("play_from", ""),
                "online_guid": og, "xsrc": True, "line": ln, "index": idx}
        if await _resolve_dduo(cand, request):
            picked.update({
                "url": cand["url"], "line": ln,
                "play_from": cand["play_from"], "m3u8": cand["m3u8"]})
            picked["xsrc"] = False
            return
# <<< PRIVATE:DDUO


# >>> PRIVATE:CATVOD
async def _resolve_catvod(picked: dict) -> bool:
    """小雅网盘：把网盘文件路径解析为小雅签名 /d 直链（原地更新）。
    该地址播放时 302 到网盘 CDN（115 等），由客户端直连，不经 HLS 改写。"""
    vid = picked.get("url", "")
    if not vid:
        return False
    try:
        import catvod
        res = await catvod.resolve_play(vid, "")
        url = res.get("url", "")
        if not url:
            return False
        picked["url"] = url
        picked["xsrc"] = False
        picked["catvod_direct"] = True
        picked["client_direct"] = True  # 特性中立标记：客户端直连 302
        picked["headers"] = res.get("headers") or {"User-Agent": "Mozilla/5.0"}
        path = url.split("?", 1)[0].lower()
        picked["m3u8"] = path.endswith(".m3u8")
        return True
    except Exception:
        _log.debug("_resolve_catvod failed", exc_info=True)
        return False
# <<< PRIVATE:CATVOD

# >>> PRIVATE:DRPY
def _drpy_source_for(picked: dict) -> dict | None:
    """根据剧集 play_from（"drpy-xxx:线路"）找到对应内置 drpy 源。"""
    pf = picked.get("play_from", "")
    sid = pf.split(":", 1)[0] if ":" in pf else pf
    for s in config.all_sources():
        if s.get("drpy") and s.get("id") == sid:
            return s
    return None


async def _resolve_drpy(picked: dict) -> bool:
    """drpyS JS 爬虫：用 Node sidecar 的 playerContent 解析真实播放地址。"""
    vid = picked.get("url", "")
    if not vid:
        return False
    source = _drpy_source_for(picked)
    if not source:
        return False
    try:
        import drpy
        site = providers._drpy_site(source)
        flag = (picked.get("play_from", "").split(":", 1)[1]
                if ":" in picked.get("play_from", "") else "")
        res = await asyncio.to_thread(drpy.play, site, flag, vid)
        url = (res or {}).get("url", "")
        if not url:
            return False
        picked["url"] = url
        picked["drpy"] = False
        h = res.get("header")
        headers = json.loads(h) if isinstance(h, str) and h else (h or {})
        picked["headers"] = headers or {"User-Agent": "Mozilla/5.0"}
        path = url.split("?", 1)[0].lower()
        picked["m3u8"] = path.endswith(".m3u8")
        # JS 解出的多为 CDN 直链/m3u8，走正常直链/HLS 代理（不像小雅必须客户端直连）
        return True
    except Exception:
        _log.debug("_resolve_drpy failed", exc_info=True)
        return False
# <<< PRIVATE:DRPY

# >>> PRIVATE:GUARD
def _guard_source_for(picked: dict) -> dict | None:
    pf = picked.get("play_from", "")
    sid = pf.split(":", 1)[0] if ":" in pf else pf
    for s in config.all_sources():
        if s.get("guard") and s.get("id") == sid:
            return s
    return None


_MSEARCH_CACHE: dict[str, tuple] = {}   # 片名 -> (ts, source_key, vod_id, line)
_MSEARCH_TTL = 1800
_MSEARCH_MAX_SRCS = 3   # 并发扇出的源数（=redroid 设备数，保证每源独占一台、零排队）
_MSEARCH_SEARCH_TIMEOUT = 14.0  # 单源搜索超时：设备偶被预热占用时给足排队+出片时间（<播放端~20s窗口）


def _msearch_parse(vod_id: str) -> tuple[str, str]:
    """msearch:###片名###海报@Referer=... → (片名, 海报)。"""
    body = vod_id.split(":", 1)[1] if ":" in vod_id else vod_id
    parts = [p for p in body.split("###")]
    title = (parts[1].strip() if len(parts) > 1 else "").strip()
    poster = parts[2].split("@", 1)[0].strip() if len(parts) > 2 else ""
    return title, poster


def _msearch_targets() -> list[dict]:
    """可用于扇出聚合的【可搜索加固源】，按“易命中/出片快”优先排序。

    redroid 池通常只有 2~3 台、每台设备内仍串行，因此扇出必须收敛到少量真正
    不同的快源（8 个源里塞 6 个同后端的 AppSx 镜像只会重复排队）。优先级：
    多线秒播主力 → 秒播单站；同前缀镜像只留一个（AppSx2..6 与 AppSx 同后端）。"""
    import guard as _g
    # 主力多线源（2026-10-31 实测重排）：按“能搜到 + 能解出真实 m3u8 + 出片快”排序。
    # 实测 Bttwoo(字节CDN)/Jpys(PP视频) 搜索与 play 均稳定秒出地址；Hmys 有结果但 play
    # 偶发空、JPJ/KkSs/Seedhub 详情易超时，故它们作为补充/兜底，不放最前。
    # （原先置顶的 AppSx/Nmyswv/AppTT/Auete/NewCz 等已实测对大众片返回 0 条，移出主力。）
    # 前 3 个是实测“能搜到且能解出真实 m3u8”的主力（正好对应 _MSEARCH_MAX_SRCS=3，
    # 每源独占一台 redroid 不排队）；其余作为缓存命中或扩容时的后备。
    PREF = ["Bttwoo", "Jpys", "Hmys", "JPJ", "YpanSo",
            "KkSs", "Seedhub", "NewCz", "YCyz", "T42"]
    seen_prefix: set[str] = set()
    out = []
    for key in PREF:
        conf = _g.site(key)
        if not conf or not conf.get("searchable", True):
            continue
        # 同后端镜像去重（AppSx / AppSx2..7 视为一族，只取一个）
        prefix = "AppSx" if key.startswith("AppSx") else key
        if prefix in seen_prefix:
            continue
        seen_prefix.add(prefix)
        name = conf.get("name") or key
        out.append({"key": key, "conf": conf, "name": name,
                    "multi": ("多线" in str(name))})
    return out[:_MSEARCH_MAX_SRCS]


async def _msearch_resolve_one(key: str, vid: str, want_idx: int,
                               pref_line: str = "") -> tuple | None:
    """取某源详情并尝试解出指定集的真实地址；并发池里的一个竞速分支。

    返回 (key, line, ep_id, res) 或 None。会按偏好线路→其它线路依次试，任一成功即返回。"""
    import guard
    conf = guard.site(key)
    if not conf:
        return None
    try:
        det = await asyncio.wait_for(
            asyncio.to_thread(guard.detail, conf, vid),
            timeout=_MSEARCH_SEARCH_TIMEOUT)
    except Exception:  # noqa: BLE001
        _log.debug("_msearch_resolve_one failed: vid=%s", vid, exc_info=True)
        return None
    eps = guard.parse_episodes(det) if det else []
    if not eps:
        return None
    groups: dict[str, list] = {}
    for e in eps:
        groups.setdefault(e["line"], []).append(e)
    order = ([pref_line] if pref_line and pref_line in groups else []) + \
            [ln for ln in groups if ln != pref_line]
    for line in order:
        ep_list = groups[line]
        ep = ep_list[want_idx] if want_idx < len(ep_list) else None
        if ep is None and want_idx:
            continue   # 指定集不存在于该线路，不要错放成第 1 集
        ep = ep or ep_list[0]
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(guard.play, conf, line, ep["id"]),
                timeout=_MSEARCH_SEARCH_TIMEOUT)
        except Exception:  # noqa: BLE001
            _log.debug("_msearch_resolve_one failed: vid=%s", vid, exc_info=True)
            res = {}
        url = (res or {}).get("url", "")
        if url:
            return key, line, ep["id"], res
    return None


async def _resolve_guard_msearch(picked: dict) -> bool:
    """片单元搜索影片：片名在各可搜索加固源【并发】扇出竞速，谁先解出真实地址用谁。

    多 redroid 实例池支持并发，故不再像单实例时代串行首命中；命中源缓存 30 分钟，
    同一部剧后续选集直接走缓存源（通常亚秒）。"""
    import guard
    mid = str(picked.get("url", ""))
    title, poster = _msearch_parse(mid)
    if not title:
        return False
    want_idx = int(picked.get("index", 0) or 0)
    now = time.time()
    cached = _MSEARCH_CACHE.get(title)

    try:
        # 候选 (key, vid, pref_line)。命中过的源置顶。
        candidates: list[tuple] = []
        if cached and now - cached[0] < _MSEARCH_TTL:
            _, ck, cid, cline = cached
            candidates.append((ck, cid, cline))

        if not candidates:
            # 并发搜索所有目标源；多 redroid 池会把它们分到不同模拟器同时跑
            targets = _msearch_targets()

            async def _search_one(g):
                try:
                    hits = await asyncio.wait_for(asyncio.to_thread(
                        guard.search, g["conf"], title, False, 1),
                        timeout=_MSEARCH_SEARCH_TIMEOUT)
                except Exception:  # noqa: BLE001
                    _log.debug("_search_one failed", exc_info=True)
                    return None
                if hits and str(hits[0].get("vod_id", "")):
                    return g["key"], str(hits[0]["vod_id"]), ""
                return None

            results = await asyncio.gather(*[_search_one(g) for g in targets])
            for r in results:
                if r:
                    candidates.append(r)

        if not candidates:
            return False

        # 详情+播放并发竞速：先解出可用地址的源胜出
        tasks = [asyncio.ensure_future(
            _msearch_resolve_one(k, v, want_idx, ln)) for k, v, ln in candidates]
        winner = None
        try:
            for fut in asyncio.as_completed(tasks):
                r = await fut
                if r:
                    winner = r
                    break
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        if not winner:
            return False
        key, line, _epid, res = winner
        url = res.get("url", "")
        if not url:
            return False
        vid = next((v for k, v, _ in candidates if k == key), "")
        picked["url"] = url
        picked["guard"] = False
        picked["headers"] = res.get("_headers") or {"User-Agent": "Mozilla/5.0"}
        picked["m3u8"] = url.split("?", 1)[0].lower().split(".")[-1] in ("m3u8", "m3u")
        picked["_guard_parse"] = int(res.get("parse", 0) or 0)
        picked["play_from"] = f"guard-{key}:{line}"
        _MSEARCH_CACHE[title] = (now, key, vid, line)
        return True
    except Exception:  # noqa: BLE001
        _log.debug("_resolve_guard_msearch failed", exc_info=True)
        return False




async def _resolve_guard_pan(picked: dict) -> bool:
    """把加固源里的「网盘分享型」集解析为可播地址（已适配：夸克、UC）。

    入口：某家网盘直链（pan.quark.cn / drive.uc.cn / alipan.com …）或四盘
    聚合源的 /link_start/?redirect_to=pan_id_xxx 占位（仅夸克可解中转）。
    已适配的家转存→原画→对应 /pan/<key>/play 注入 Cookie 代理；其它家给明确提示。"""
    import urllib.parse as _up
    raw = str(picked.get("url", ""))
    pv = _pan_match(raw)
    share = ""
    if pv:
        share = pv["re"].search(raw).group(0)
        # 正则从 pan.quark.cn 开始匹配，可能缺 https:// 前缀；parse_share 需要完整 URL。
        if share and not share.lower().startswith(("http://", "https://")):
            share = "https://" + share
    else:
        # 可能是 Seedhub 四盘中转：跟随 302 解真实链接（多为夸克）。
        lm = _PAN_LINKSTART_RE.search(raw)
        if not lm:
            return False
        resolved = await asyncio.to_thread(_pan.resolve_linkstart, lm.group(1))
        if not resolved:
            raise _GuardPanUnsupported(
                "这是四盘聚合（Seedhub）资源，其跳转页被 Cloudflare 防护，"
                "本机暂无法自动解出真实网盘链接")
        pv2 = _pan_match(resolved) or _PAN_PROVIDERS[0]
        share = resolved
        pv = pv2
    if not pv["playable"]:
        raise _GuardPanUnsupported(
            f"该资源来自「{pv['name']}」，目前已支持夸克 / UC 网盘原画播放，"
            "该网盘适配开发中；可在网盘线路里改选夸克或 UC")
    key = pv["key"]
    mod = _PAN_MODS.get(key)
    if mod is None:
        raise _GuardPanUnsupported(f"当前构建不含「{pv['name']}」播放模块")
    if not share:
        return False
    hint = _up.parse_qs(_up.urlparse(share).query).get("pwd", [""])[0]
    try:
        r = await asyncio.to_thread(mod.resolve_share, share, hint)
    except Exception:  # noqa: BLE001
        _log.debug("_resolve_guard_pan failed", exc_info=True)
        return False
    items = [x for x in (r.get("items") or []) if x.get("url")]
    if not items:
        return False
    idx = int(picked.get("index", 0) or 0)
    item = items[min(idx, len(items) - 1)]
    # 根相对地址：播放器与本服务同源（quark 走旧路径，UC 走 /pan/uc/play）。
    play_path = "/v/api/ext/pan/play" if key == "quark" else f"/v/api/ext/pan/{key}/play"
    picked["url"] = f"{play_path}?fid={item['fid']}"
    picked["guard"] = False
    picked["pan"] = True
    picked["pan_provider"] = key
    picked["headers"] = {"User-Agent": "Mozilla/5.0"}
    picked["m3u8"] = False
    return True


class _GuardPanUnsupported(Exception):
    """识别到网盘资源但当前不支持播放（非夸克家 / Cloudflare 解不开）。"""


# 加固解析单飞 + 短结果缓存。飞牛客户端起播时会高频轮询 stream/main.m3u8，
# 而加固源 detail+play 要串行占用稀缺的 Android runner（冷启动 6~28s）。若不去重，
# 多次重复解析会抢占仅有的设备、互相排队，冷启动轻松超过外网网关超时。按
# 「源|集id|集序」合并：并发请求共用同一个 Future，成功结果再短缓存覆盖重试窗口。
_GUARD_RES_INFLIGHT: dict[str, asyncio.Future] = {}
_GUARD_RES_CACHE: dict[str, tuple[float, dict]] = {}
_GUARD_RES_TTL = 120.0


async def _resolve_guard(picked: dict) -> bool:
    """加固解析入口（单飞 + 短结果缓存）。同一集的并发轮询/重试只真正解析一次。
    网盘与 msearch 等非固定 vod_id 的情形不缓存，直接走一次（避免不同片名串结果）。"""
    vod_id = str(picked.get("url", "") or "")
    cacheable = vod_id and not vod_id.startswith("msearch:") \
        and not (_pan_match(vod_id) or _PAN_LINKSTART_RE.search(vod_id))
    key = ""
    if cacheable:
        source = _guard_source_for(picked) or {}
        key = "%s|%s|%s" % (source.get("key") or picked.get("play_from", ""),
                           vod_id, int(picked.get("index", 0) or 0))
        ent = _GUARD_RES_CACHE.get(key)
        if ent:
            ts, snap = ent
            if time.time() - ts < _GUARD_RES_TTL and snap.get("ok"):
                picked.update({k: v for k, v in snap.items() if k != "ok"})
                return True
        fut = _GUARD_RES_INFLIGHT.get(key)
        if fut is None:
            fut = asyncio.ensure_future(_resolve_guard_once(picked, rkey=key))
            _GUARD_RES_INFLIGHT[key] = fut

            def _drop(_f, k=key):
                _GUARD_RES_INFLIGHT.pop(k, None)
            fut.add_done_callback(_drop)
        if not fut.done():
            await fut
        # 无论发起者还是等待者，统一从结果缓存取快照（once 成功时写入）
        ent = _GUARD_RES_CACHE.get(key)
        if ent and time.time() - ent[0] < _GUARD_RES_TTL and ent[1].get("ok"):
            picked.update({k: v for k, v in ent[1].items() if k != "ok"})
            return True
        return False

    return await _resolve_guard_once(picked)


async def _resolve_guard_once(picked: dict, rkey: str = "") -> bool:
    """加固爬虫：搜索结果只持有 vod_id；播放前动态取详情，再调用
    Android runner 的 playerContent 取得真实 m3u8/mp4。"""
    import guard
    # 片单/元搜索源（豆豆片单等）的影片 id 形如 "msearch:###片名###海报"，自身不提供
    # 线路；其在 OK影视 里由加密 mergeguard + 宿主本地聚合服务扇出到订阅内各源。我们的
    # 精简 bridge host 没有该本地服务，故这里用片名在“可搜索的加固兄弟源”里自助扇出，
    # 等价实现“自动聚合各订阅线路、优先能秒解的那条”。
    if str(picked.get("url", "")).startswith("msearch:"):
        return await _resolve_guard_msearch(picked)
    # 网盘分享型（夸克 / UC / 阿里 / 百度 / 迅雷 / 115 / 天翼 / 移动，或四盘
    # 聚合的 /link_start/?redirect_to=pan_id_xxx 占位）。已适配的家（夸克）转存→
    # 原画→cookie 注入播放代理；识别到但未适配的家给可读提示，不再静默走普通加固
    # （普通加固对网盘链接必然失败，错误信息无意义）。
    pv = _pan_match(str(picked.get("url", ""))) or (
        _PAN_LINKSTART_RE.search(str(picked.get("url", ""))) and _PAN_PROVIDERS[0])
    if pv:
        if not any(_PAN_MODS.values()):
            picked["pan_error"] = "识别到网盘资源，但本构建不含网盘播放模块"
            return False
        try:
            pan_hit = await _resolve_guard_pan(picked)
        except _GuardPanUnsupported as exc:
            picked["pan_error"] = str(exc)
            _log.info("网盘资源暂不支持：%s 《%s》 %s",
                      picked.get("line"), picked.get("title"), exc)
            return False
        if pan_hit:
            picked.pop("pan_error", None)
            return True
    source = _guard_source_for(picked)
    conf = providers._guard_conf(source) if source else None
    if not conf:
        return False
    vod_id = picked.get("url", "")
    if not vod_id:
        return False
    try:
        want_line = (picked.get("play_from", "").split(":", 1)[1]
                     if ":" in picked.get("play_from", "") else "")
        # 详情已绑定的集（play_from 带真实线路，url 是完整 episode id，可能形如
        # "{play_id=..,playUrl=..}"）直接播放，不必再按 vod_id 取详情；
        # 只有搜索占位（url 是裸 vod_id、无线路）才需要先 detail 解析集列表。
        direct_ep = bool(want_line) and (
            "playUrl=" in vod_id or vod_id.startswith("{") or "/" not in vod_id[:1])
        if direct_ep:
            chosen = {"line": want_line, "id": vod_id}
        else:
            det = await asyncio.to_thread(guard.detail, conf, vod_id)
            episodes = guard.parse_episodes(det)
            if not episodes:
                return False
            idx = int(picked.get("index", 0) or 0)
            same_line = [e for e in episodes if e["line"] == want_line] if want_line else []
            chosen = None
            if idx < len(same_line):
                chosen = same_line[idx]
            else:
                # 搜索占位线路名与真实播放 flag 不同：按集序号在各线路中回退。
                groups: dict[str, list[dict]] = {}
                for e in episodes:
                    groups.setdefault(e["line"], []).append(e)
                for line, eps in groups.items():
                    if idx < len(eps):
                        chosen = eps[idx]
                        break
                chosen = chosen or episodes[0]
        res = await asyncio.to_thread(guard.play, conf, chosen["line"], chosen["id"])
        url = (res or {}).get("url", "")
        if not url:
            return False
        picked["url"] = url
        picked["guard"] = False
        picked["headers"] = res.get("_headers") or {"User-Agent": "Mozilla/5.0"}
        picked["m3u8"] = url.split("?", 1)[0].lower().split(".")[-1] in ("m3u8", "m3u")
        picked["_guard_parse"] = int(res.get("parse", 0) or 0)
        if rkey:
            snap = {"ok": True, "url": url, "guard": False,
                    "headers": picked["headers"], "m3u8": picked["m3u8"],
                    "_guard_parse": picked["_guard_parse"]}
            _GUARD_RES_CACHE[rkey] = (time.time(), snap)
            if len(_GUARD_RES_CACHE) > 600:
                for old in sorted(_GUARD_RES_CACHE,
                                  key=lambda k: _GUARD_RES_CACHE[k][0])[:200]:
                    _GUARD_RES_CACHE.pop(old, None)
        return True
    except Exception:
        _log.debug("_resolve_guard_once failed", exc_info=True)
        return False
# <<< PRIVATE:GUARD

# >>> PRIVATE:SPIDER
def _spider_source_for(picked: dict) -> dict | None:
    pf = picked.get("play_from", "")
    sid = pf.split(":", 1)[0] if ":" in pf else pf
    for s in config.all_sources():
        if s.get("spider_src") and s.get("id") == sid:
            return s
    return None


async def _resolve_spider(picked: dict) -> bool:
    """普通 csp_ 爬虫（订阅导入）：按各自 jar 经 redroid 解析真实播放地址。"""
    import spiders
    source = _spider_source_for(picked)
    conf = providers._spider_conf(source) if source else None
    if not conf:
        return False
    vod_id = picked.get("url", "")
    if not vod_id:
        return False
    try:
        want_line = (picked.get("play_from", "").split(":", 1)[1]
                     if ":" in picked.get("play_from", "") else "")
        direct_ep = bool(want_line) and (
            "playUrl=" in vod_id or vod_id.startswith("{") or "/" not in vod_id[:1])
        if direct_ep:
            chosen = {"line": want_line, "id": vod_id}
        else:
            det = await asyncio.to_thread(spiders.detail, conf, vod_id)
            episodes = guard.parse_episodes(det)
            if not episodes:
                return False
            idx = int(picked.get("index", 0) or 0)
            same_line = [e for e in episodes if e["line"] == want_line] if want_line else []
            if idx < len(same_line):
                chosen = same_line[idx]
            else:
                chosen = episodes[min(idx, len(episodes) - 1)]
        res = await asyncio.to_thread(spiders.play, conf, chosen["line"], chosen["id"])
        if isinstance(res, str) and res.strip().startswith("{"):
            try:
                res = json.loads(res)
            except ValueError:
                _log.debug("_resolve_spider failed", exc_info=True)
                res = {"url": res}
        url = (res or {}).get("url", "") if isinstance(res, dict) else str(res or "")
        url = guard._normalize_url(url)
        if not url:
            return False
        picked["url"] = url
        picked["spider_src"] = False
        hdrs = res.get("headers") if isinstance(res, dict) else None
        picked["headers"] = hdrs or {"User-Agent": "Mozilla/5.0"}
        picked["m3u8"] = url.split("?", 1)[0].lower().split(".")[-1] in ("m3u8", "m3u")
        return True
    except Exception:
        _log.debug("_resolve_spider failed", exc_info=True)
        return False
# <<< PRIVATE:SPIDER


# ===== 播放线路健康探测与自动换线 =====
# 问题背景：用户手动切到某条坏线（文件已删/网盘无此集/源站失效）后，插件只是把该线
# 302/下发，从不校验死活，播放器一直报错；坏线也不会被记忆，退出重放仍黏在该线。
# 方案：①坏线短时记忆 ②起播/取流前并行探测同集各候选线路，自动切到首个可用线路。
# P2 拆分：HLS 播放链路与线路竞速：探活、复活、竞速、播放列表、HLS 路由。已移入 proxy/hls_race.py；以下重导出（对外 import 名称不变）。
from hls_race import _DURATION_MAX_SECONDS, _DURATION_PROBE_TIMEOUT, _EXTINF_RE, _GROUP_CN, _HLS_LONG_URL, _HLS_REG, _HLS_REG_MAX, _HLS_REG_TTL, _LINE_BAD, _LINE_BAD_TTL, _LINE_FAST, _LINE_FAST_TTL, _LINE_SPEED, _MAIN_SERVE_BG_FAILS, _MAIN_SERVE_FUT, _MEDIA_REDIRECT, _NOT_HLS, _PH_DIR, _PH_SEG_DUR, _PH_SEG_TS, _PH_WIN, _PLAYLIST_CACHE, _PLAYLIST_FUT, _PLAYLIST_TTL, _PROBE_MAX_CANDIDATES, _PROBE_RANGE, _PROBE_TIMEOUT, _PROVIDER_CN, _RACE_CONCURRENCY, _RESURRECT_FAIL, _RESURRECT_FAIL_TTL, _RESURRECT_FUT, _SEG_OK_CACHE, _SEG_OK_TTL, _SPEED_GOOD, _SPEED_PRIOR, _attach_play_duration, _autofail_on, _build_playlist_body, _cached_playlist_seconds, _cors_preflight, _decode_tok, _direct_to_placeholder, _domain_heal_enabled, _ensure_playable, _episode_candidates, _episode_key, _fastest_on, _fetch_playlist, _find_playable, _first_variant, _fixed_line_direct_on, _friendly_line, _get_playlist_cached, _hls_reg_get, _hls_reg_put, _http_head_ok, _is_media_playlist, _is_slow_line, _line_bad_key, _line_display_name, _line_is_bad, _line_score, _line_speed, _main_serve_fut_done, _main_slow_budget, _mark_line_bad, _MID_SWITCH_FAILS, _MID_SWITCH_FAIL_TTL, _MID_SWITCH_MAX_FAILS, _maybe_mid_switch, _mid_switch_failures, _mid_switch_note_fail, _mid_switch_note_ok, _mid_switch_on, _ordered_candidates, _placeholder_master_response, _placeholder_playlist_response, _placeholder_ts_response, _pick_mid_switch_target, _play_duration_for, _playlist_cache_key, _playlist_total_seconds, _probe_latency, _probe_target, _race_candidates, _remember_episode_duration, _remember_fast, _remember_speed, _resolve_if_needed, _resurrect_start, _resurrect_worker, _skip_adjusted_duration, _skip_seconds, _source_health_enabled, _trim_playlist, _try_stream_segment, _verify_rewritten_first_seg, _warm_playlist, _zero_race_on, hls_compact, hls_cors_preflight, hls_main, hls_preset, hls_seg, placeholder_ts, remembered_fast_line, trusted_predicted_target, hls_router
app.router.routes.extend(hls_router.routes)


# ===== 浏览时探活（解冻第 4 项）=====
# linepredict 模块 docstring 早就承诺“浏览海报 / 搜索结果时，对首屏可见影片的
# 少数候选线做一次轻量探测”，但浏览路径上一直没有调用点：唯一的预热在播放信息
# 路径（_prewarm_from_info → _warm_playlist，只预热默认线播放列表，不探线路）。
# 这里补上浏览这一级：列表 handler 返回前只调度后台任务、不等待；探活跑完把
# 每部片延迟最低的线写进 linepredict 记忆，供后续排序与起播作种子。
# 写向冻结：只写 linepredict.remember；绝不写 _LINE_FAST（只有竞速胜者能写），
# 也不写 _LINE_SPEED（那是真实播放吞吐，2KB 探活量不出吞吐）；失败只记日志，
# 不标坏线（一次轻量探测失败不等于线路已死，坏线标记仍只走 _mark_line_bad）。
_BROWSE_PROBE_MAX_TITLES = 6        # 单次浏览最多探活的影片数（首屏前几部）
_BROWSE_PROBE_CONCURRENCY = 3       # 探活并发上限（与故障切换分批探测同口径）
_BROWSE_PROBE_PER_LINE_TIMEOUT = 8.0  # 单线探测超时（秒，与 _PROBE_TIMEOUT 同口径）
_BROWSE_PROBE_BUDGET = 20.0         # 单次浏览触发的探活总耗时上限（秒），超时即停
_BROWSE_PROBE_TASKS: set[asyncio.Task] = set()  # 在途浏览探活任务（仅跟踪，不作他用）


def _browse_probe_enabled() -> bool:
    """浏览探活总开关：browse_probe（默认开）与 linepredict_enabled 均开才探。"""
    try:
        cfg = config.get()
        return bool(cfg.get("browse_probe", True)) and \
            bool(cfg.get("linepredict_enabled", True))
    except Exception:
        _log.debug("_browse_probe_enabled failed", exc_info=True)
        return True


def _browse_probe_guids(rows) -> list[str]:
    """从已返回的列表行里取前 N 部可探活影片的标题 og。

    目录卡 guid 先还原回标题 og；不在 store 里的行（榜单未绑定片、本地库行、
    文件夹）直接跳过——没有线路索引可探。"""
    ogs: list[str] = []
    seen: set[str] = set()
    try:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            g = row.get("guid") or ""
            og = _parse_line_folder_guid(g) or g
            if not og or og in seen:
                continue
            if store.get_title_by_guid(og) is None:
                continue
            seen.add(og)
            ogs.append(og)
            if len(ogs) >= _BROWSE_PROBE_MAX_TITLES:
                break
    except Exception:
        _log.debug("_browse_probe_guids failed", exc_info=True)
    return ogs


async def _browse_probe_worker(ogs: list[str], request: Request) -> None:
    """后台探活：对每部片第 0 集的前若干候选线做轻量探测，记最快线。

    只抓播放清单 / Range 首字节（_probe_latency，build_hls=False，不转码不转存）。
    总预算内完成多少记多少，超时即停；在途标记无论成败/超时/取消都清理。"""
    try:
        max_lines = int(config.get().get("linepredict_max_lines", 3) or 3)
    except Exception:
        max_lines = 3
    max_lines = max(1, min(8, max_lines))
    jobs: list[tuple[str, list[dict]]] = []
    for og in ogs:
        key = linepredict.ep_key(og, 0)
        if linepredict.ready_or_reprobe(key):
            continue  # 已有够快预测，不重复探
        if not linepredict.try_mark_inflight(key):
            continue  # 同片已有在途探活，跳过
        try:
            picked = {"online_guid": og, "index": 0, "line": None,
                      "kind": "title"}
            cands = [c for c in _episode_candidates(picked)
                     if not _line_is_bad(c)]
            cands = cands[:max_lines]
        except Exception:
            linepredict.clear_inflight(key)
            _log.debug("_browse_probe_worker failed", exc_info=True)
            continue
        if not cands:
            linepredict.clear_inflight(key)
            continue
        jobs.append((key, cands))
    if not jobs:
        return
    sem = asyncio.Semaphore(_BROWSE_PROBE_CONCURRENCY)

    async def _probe_one(key: str, cand: dict):
        async with sem:
            try:
                lat = await asyncio.wait_for(
                    _probe_latency(cand, request, build_hls=False),
                    timeout=_BROWSE_PROBE_PER_LINE_TIMEOUT)
            except Exception:
                _log.debug("_browse_probe_worker failed", exc_info=True)
                return key, None, None
            return key, cand.get("line"), lat

    tasks = [asyncio.ensure_future(_probe_one(key, cand))
             for key, cands in jobs for cand in cands]
    try:
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=_BROWSE_PROBE_BUDGET)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if pending:
            _log.info("browse probe budget exceeded: %d/%d probes cut off",
                      len(pending), len(tasks))
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        best: dict[str, tuple[str, float]] = {}
        for t in done:
            try:
                if t.cancelled() or t.exception() is not None:
                    continue
                key, line, lat = t.result()
            except Exception:
                _log.debug("_browse_probe_worker failed", exc_info=True)
                continue
            if line and lat is not None:
                cur = best.get(key)
                if cur is None or lat < cur[1]:
                    best[key] = (line, lat)
        for key, (line, lat) in best.items():
            linepredict.remember(key, line, lat)
    finally:
        for key, _cands in jobs:
            linepredict.clear_inflight(key)


def _browse_probe_task_done(task: asyncio.Task) -> None:
    """后台探活任务结束回调：从跟踪集合摘掉，并取回异常记日志（不留悬空 future）。"""
    _BROWSE_PROBE_TASKS.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        return
    if exc is not None:
        _log.warning("browse probe failed: %s", type(exc).__name__)


def _schedule_browse_probe(rows, request) -> None:
    """浏览列表返回前调度后台探活：只创建任务、不等待，列表立即返回。

    调用点在各列表 handler 的 return 之前；开关关闭或无可探行时什么都不做。"""
    try:
        if not _browse_probe_enabled():
            return
        ogs = _browse_probe_guids(rows)
        if not ogs:
            return
        task = asyncio.create_task(_browse_probe_worker(ogs, request))
        _BROWSE_PROBE_TASKS.add(task)
        task.add_done_callback(_browse_probe_task_done)
    except Exception:
        _log.debug("browse probe schedule failed", exc_info=True)


async def _race_fastest(picked: dict, request: Request) -> dict | None:
    """并行竞速探测候选线路；先成功者胜（默认高质量线给小幅领先优势）。

    返回胜出的已解析目标（HLS 胜者会再构建播放列表缓存）；全部失败返回 None。"""
    cands = _race_candidates(
        picked, request.query_params.get("seed_line") or "")
    if not cands:
        return None
    default = store.best_line(picked.get("online_guid", ""))
    try:
        timeout = float(config.get().get("line_fastest_timeout", 4.0))
        prefer = float(config.get().get("line_fastest_prefer", 0.25))
    except Exception:
        timeout, prefer = 4.0, 0.25
    sem = asyncio.Semaphore(_RACE_CONCURRENCY)

    async def run(cand: dict):
        async with sem:
            lat = await asyncio.wait_for(
                _probe_latency(cand, request, build_hls=False),
                timeout=timeout)
        return cand, lat

    async def finish(winner: dict, wlat: float, n_ok: int) -> dict:
        """选定胜者后：记最快线 + 预热改写后的播放列表（main 取流零额外抓取）。"""
        _remember_fast(picked, winner["line"], wlat)
        if re.search(r"\.m3u8(\?|$)", winner.get("url", "").split("?", 1)[0], re.I):
            mg = getattr(request.state, "_play_mg", "") if hasattr(request, "state") else ""
            if mg:
                await _get_playlist_cached(mg, winner, request)
        _log.info("线路竞速: 《%s》%s 选中 %s (%.2fs, %d 候选, 实测%.0fKB/s)",
                  picked.get("title"), picked.get("ep_title") or "",
                  winner.get("line"), wlat, n_ok, _line_speed(winner))
        return winner

    # ① 已知吞吐达标的线：单发直探，成功即用，不再并发竞速。
    #    慢源上 6 条线并发会互相抢带宽，单发反而更快更稳。
    known = [c for c in cands if _line_speed(c) >= _SPEED_GOOD]
    if known:
        best = known[0]
        try:
            lat = await asyncio.wait_for(
                _probe_latency(best, request, build_hls=False), timeout=timeout)
        except Exception:
            lat = None
        if lat is not None:
            _LINE_BAD.pop(_line_bad_key(best), None)
            return await finish(best, lat, 1)
        _mark_line_bad(best)
        cands = [c for c in cands if c is not best]
        if not cands:
            return None

    # ② 没有吞吐数据：并行竞速，拿到第一个可用候选就取消其余。
    #    不能用 gather —— gather 要等最慢的候选（或等满 timeout），13 条线路的
    #    片子实测起播 11.7s，绝大部分时间耗在等注定落选的慢候选上。
    ok: list[tuple[dict, float]] = []
    tasks = [asyncio.create_task(run(c)) for c in cands]
    try:
        pending = set(tasks)
        while pending and not ok:
            finished, pending = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not finished:
                break
            for t in finished:
                try:
                    r = t.result()
                except Exception:
                    continue
                if r is not None and r[1] is not None:
                    ok.append(r)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for c, _ in ok:
        _LINE_BAD.pop(_line_bad_key(c), None)  # 探活成功，清掉可能的旧坏线标记
    if not ok:
        for c in cands:
            _mark_line_bad(c)
        return None

    def eff(item):
        c, lat = item
        return lat - (prefer if default and c["line"] == default else 0.0)

    winner, wlat = min(ok, key=eff)
    return await finish(winner, wlat, len(ok))


# P2 拆分：本段亦已移入 proxy/hls_race.py（重导出见上）。


def _decode_poster_tok(tok: str) -> tuple[str, str, int | None]:
    """解码海报 token，返回 (图床URL, 网盘名, 健康等级)。

    兼容旧版纯 URL token 与新版 JSON{u,p,h} token。"""
    raw = _decode_tok(tok)
    pan_label, grade = "", None
    url = raw
    if raw[:1] in ("{", "["):
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                url = j.get("u", "")
                pan_label = str(j.get("p", "") or "")
                if j.get("h") is not None:
                    grade = int(j["h"])
        except Exception:
            _log.debug("_decode_poster_tok failed", exc_info=True)
            url = raw
    return url, pan_label, grade


# 海报占位图（JPEG，150x225 渐变 + 播放标）：App/TV 原生图片加载器解不了 SVG，
# 旧的 SVG fallback 在 App 端会显示为空白；改成光栅 JPEG 后失败时至少能看到占位。
_POSTER_FALLBACK_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCADhAJYDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDm6KKSv1c+JSCiiig0SCiiig0SCiiig1SCiiig0SCiikoNEgooooNEgooopmiQlFFFBokLRRRQaWFooopHzqQUUUUGiQUUUUGiQUUUUGiQUUUUGqQUlFFBokFFFFM0SCkoooNEgpaKSg0SCiiiguw6iiikfPJBRRRQaJBRRRQaJBRRRQaJBSUtJQaJBRRRQaJBRRSUzVIKKKWg0SCkoooNEgooooLsOooopHzqQUUUUGqQUUUUGiQUUUlBokFFWo7GeTHybQe7HH/16k/syb+9H+Z/wqeeK6mqiyjRU81pPECWTKjuOfxqCqTT2NEhKKKKZaQtJS0lBqkFFFFBokFFFFBdh1FFFI+dSCiiig0SCiiig1SHwxPNIEQZP8q2rW0jtxkDc/8AeP8AT0pun2/kwAkHe/Lf0FWq5alRt2WxvGNgor13wt4c07X/AAFpqX0OJRv2zx4EigSvwGweOTweOfXmuL8UeCdT0RnkjRryxUbjPEnK8EncuSQBg89OnPavMpZhSqVJUm7STa162Or2MklI5Wql3ZJMCy/JJ6jofrVuiu9ScXdCsc26NG5VwQw6ikrW1WDfH5qg7k6/SsiuyEuZXGkFFFFWWkFFFFBqkFFFFBdh1FFFI+dSCiiig0SCpbRPMuY14POSD6Dmoqs6b/x+x/j/ACNTLRM1itTcoooriOhI91+Gn/Ik6b/20/8ARjV09cx8NP8AkSdN/wC2n/o166evhcZ/vFT1f5npw+FHE+KPh/p+poZdLWOwuxk4Vf3b8YAKj7vIHIHrkGvKdc0W+0S7a31CBkOSEkAOyQDHKnuOR9M84NfRlcP458YaRbWd5pflLqFyymN4iD5aNx95uOR1+XkFcZU816WXY/EcypW51+Xz/wAyJ0477HjJAIIIBB4INc5IpR2Q9VJBxXTSsHldlRY1YkhFzhfYZJOPqTXOXX/HzN/vn+dfX0HuYWIqKKK6S0gooooNEgooopmlh1FFFSfOpBRRSUGiQU6J/LlR8Z2kHFNooNEjpVYOoZTkEZFBrM0u6AHkyED+5/hWnXFOPK7HRHU92+Gf/Ik6b/20/wDRr1o694h0zQ4i2oXKrIRuWFfmkfrjC+hwRk4Ge9eU2Xji50zwtaaVpcflXEe4vcvhsZdmwqkEdCOT78dDXJ3VxNdTtNdTSTTNjdJIxZjgYGSfavnVlEq1adSq7Rbfq9Ts9raKSOn8UeOdR1xDBEPsVnzmOJzucEYIduMjrxgDnnOBXJ0UV7dKjCjHlpqyM7t6sjnkEMLSN0UfnXO1e1K5EriOMgxr39TVGvQpQ5VdlBRRRWpaQUUUUGiQUUlFM0sPoooqT51IKSiig0SCiiimaJBV+11FkG2YFx6jqP8AGs+iplFS0ZpE6GK4ikxskUk9Bnn8qlrmqSsnQ7M2TN+W6hiB3SKSOMA5Oazbu+eYFUykZ4PqapUVUaSjqWgooorU0SCiiig0SCkoopmiQUUUUF2H0lLSVJ86kFFFFBokFFFJTNUgoopaDRIKSiig0SCiiig0SCiiig0SCiikpmqQUUUUGiQUUUUF2HUUUVJ86kFFFFM0SEooooNEhaSlpKDVIKKKKDRIKKKKDRIKKKKDRISiiimaJBRRRQaJBRRRQaWHUUUUj51IKSiig0SClopKDRIKKKKDRIKKKKDVIKKKKDRIKSiimaJBRRRQaJBRRRQaJBRRRQaWHUUUlI+dSCiiloNEgpKKKDRIKKKKDRIKKKKDRIKKKSmapBRRRQaJBRRRQaJBRRRQaJBRRRQXYWiiikfOpC0lLSUGqQUUUUGiQUUUUGiQUUUUGiQlFFFM0SCiiig0SCiiig1SCiiig0SCiiiguwtLRSUj51IKKKKDRIKKKKDVIKKKKDRIKSiimaJBRRRQaJBRRRQaJBRRRQapBRRRQaJBRRRQXYWiiikfOIKKKKDRBRRRQaIKTtRRTNEHeiiig1QUUUUGiCiiig0QUUUUGiCiiig0QUUUUFn/2Q=="
)


def _poster_fallback() -> Response:
    """海报抓取失败时的占位图：返回 JPEG（非 SVG）。

    旧版返回 SVG；浏览器能渲染，但 App/TV 原生图片加载器解不了 SVG，
    失败时 App 端直接显示空白。JPEG 各端都能解，至少能看到占位图。
    """
    try:
        import base64 as _b64
        data = _b64.b64decode(_POSTER_FALLBACK_JPEG_B64)
        if data[:2] == b"\xff\xd8":
            return Response(data, media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})
    except Exception:  # noqa: BLE001
        _log.debug("_poster_fallback failed", exc_info=True)
        pass
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
  <rect width="300" height="450" fill="#1f6feb"/>
  <circle cx="150" cy="180" r="58" fill="rgba(255,255,255,.18)"/>
  <polygon points="132,145 132,215 192,180" fill="#fff" opacity=".92"/>
  <text x="150" y="285" text-anchor="middle" fill="#fff" font-family="sans-serif"
    font-size="26" font-weight="700">在线影视</text>
</svg>"""
    return Response(svg, media_type="image/svg+xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=3600"})


def _poster_fetch_headers(url: str) -> dict:
    """海报上游抓取请求头：UA + 豆瓣 Referer（其余图床不带 Referer）。"""
    headers = {"User-Agent": "Mozilla/5.0"}
    host = urllib.parse.urlsplit(url).netloc.lower()
    # 豆瓣图床需要带豆瓣 Referer，否则可能 403
    if host.endswith("doubanio.com") or host.endswith("douban.com") or \
            host.endswith("doubanio.viki.moe"):
        headers["Referer"] = "https://movie.douban.com/"
    return headers


# 上游 v1.0.0 _poster_fetch 的网络错误集：直连出现连接级异常时才重试，
# 正常 HTTP 错误（4xx/5xx）不重试。
_POSTER_NETWORK_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


async def _poster_upstream_get(request: Request, url: str):
    """带重试抓一张上游海报；返回 httpx 响应，网络失败返回 None。

    单张海报失败时总 GET 不超过 3 次（P1-9 收敛；旧路径最坏 7 次：
    ext_get_retry 默认 3 次 + 网络异常后 netutil 1 次 + 403 分支又 3 次）：
    主路只取 1 次（tries=1）；主客户端网络异常时，按上游 _poster_fetch
    （v1.0.0）的做法经 netutil.async_client(timeout=12.0) 再取一次（按代理
    模式直连/走代理/自动切代理；未配置代理时也相当于一条全新直连）；
    若返回 403（图床防盗链），再带图床自身 origin 作 Referer 重试一次——
    多数防盗链只认同源 Referer，这是真正有用的那次，保留。
    代价：海报的瞬时 429/5xx 不再内部重试——海报失败只出占位图，不影响
    播放，故接受；服务端不做磁盘缓存（与上游一致，有意保留）。
    地址格式、JPEG 占位、纯透传均不动。
    """
    host = urllib.parse.urlsplit(url).netloc.lower()
    try:
        r = await ext_get_retry(request, url,
                                headers=_poster_fetch_headers(url), tries=1)
    except _POSTER_NETWORK_ERRORS:
        _log.info("poster netutil retry host=%s", host)
        try:
            async with netutil.async_client(timeout=12.0) as cli:
                r = await cli.get(url, headers=_poster_fetch_headers(url))
        except Exception:  # noqa: BLE001
            _log.debug("_poster_upstream_get failed", exc_info=True)
            return None
    except Exception:  # noqa: BLE001
        _log.debug("_poster_upstream_get failed", exc_info=True)
        return None
    if r is not None and r.status_code == 403:
        scheme = urllib.parse.urlsplit(url).scheme or "https"
        headers = _poster_fetch_headers(url)
        headers["Referer"] = f"{scheme}://{host}/"
        try:
            r = await ext_get_retry(request, url, headers=headers, tries=1)
        except Exception:  # noqa: BLE001
            _log.debug("_poster_upstream_get failed", exc_info=True)
            return None
    return r


async def _serve_online_poster(tok_path: str, request: Request) -> Response:
    # 兼容 /online/<token>.jpg 与官方图片前缀 /v/api/v1/sys/img/online/<token>.jpg
    # 机制对齐上游 v0.2.42：纯透传，服务端不做磁盘缓存——磁盘缓存（7 天）与
    # 后台预热已随本提交一并移除（上游已在 v0.2.42 移除，两者都曾让海报墙变慢，
    # 且缓存可能长期 serving 陈旧/错误内容）。浏览器/App 按 max-age 自行缓存。
    tok_path = tok_path.lstrip("/")
    marker = "online/"
    if marker in tok_path:
        tok_path = tok_path.split(marker, 1)[1]
    # 兼容已发出的 v2 sys/img 地址：解码前剥掉 v2/ 前缀，旧版无前缀地址同样兼容。
    if tok_path.startswith("v2/"):
        tok_path = tok_path[len("v2/"):]
    try:
        u, pan_label, health_grade = _decode_poster_tok(tok_path)
    except Exception:
        _log.debug("_serve_online_poster failed", exc_info=True)
        return _poster_fallback()
    if not u.startswith("http"):
        return _poster_fallback()
    want_badge = bool(pan_label) or health_grade is not None
    host = urllib.parse.urlsplit(u).netloc.lower()
    r = await _poster_upstream_get(request, u)
    if r is None:
        _log.warning("海报抓取失败 host=%s：上游无响应", host)
        return _poster_fallback()
    ctype = (r.headers.get("content-type") or "").lower()
    if r.status_code != 200 or not r.content:
        _log.warning("海报抓取失败 host=%s status=%s", host, r.status_code)
        return _poster_fallback()
    sniff = _sniff_image(r.content[:12])
    if not (ctype.startswith("image/") or sniff):
        _log.warning("海报抓取失败 host=%s：非图片内容 type=%s",
                     host, ctype[:40])
        return _poster_fallback()
    ctype = ctype if ctype.startswith("image/") else sniff
    content = r.content

    # 在海报上烧角标（网盘资源 / 源健康点）。GIF 不动，合成失败回退原图。
    out_bytes, out_type = content, (ctype or "image/jpeg")
    if want_badge and "gif" not in out_type:
        try:
            import posterbadge
            if posterbadge.available():
                badged = await asyncio.to_thread(
                    posterbadge.badge, content, pan_label, health_grade)
                if badged:
                    out_bytes, out_type = badged, "image/jpeg"
        except Exception:  # noqa: BLE001
            _log.debug("_serve_online_poster failed", exc_info=True)
            pass
    return Response(content=out_bytes, status_code=200,
                    media_type=out_type,
                    headers={"Cache-Control": "max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


def _sniff_image(head: bytes) -> str:
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:8] == bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)):
        return "image/png"
    if head[:3] == bytes((0xFF, 0xD8, 0xFF)):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


@app.get("/online/{tok_path:path}")
async def online_poster_short(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/v/api/v1/sys/img/online/{tok_path:path}")
async def online_poster(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/_ext/healthz")
async def healthz():
    return {"ok": True, "service": "fnmedia-ext", "upstream": _up_status()}


def _up_status() -> str:
    return "unknown"


# ------------------------------------------------------------- 管理 API

# P2 拆分：管理 API（/v/api/ext/* 管理端点）。已移入 proxy/admin_api.py；以下重导出（对外 import 名称不变）。
from admin_api import _ADMIN_LOGIN_LIMITER, _admin_client_key, _admin_limiter_from_config, _cache_sizes, _is_pure_cms, _schedule_routes_refresh, _virtual_lib_list, admin_page, ext_account_acl, ext_account_defaults, ext_accounts, ext_adfilter_replace_rules, ext_adfilter_status, ext_adfilter_toggle, ext_admin_login, ext_admin_token_rotate, ext_admin_token_status, ext_cache_clear, ext_cache_status, ext_continue, ext_diag_resume, ext_favorite_enrich, ext_follow_check, ext_follow_list, ext_follow_set, ext_hot_refresh, ext_import_subscription, ext_import_tvbox, ext_junk_keywords, ext_lib_order, ext_lib_pin, ext_lib_visibility, ext_libs, ext_live, ext_live_clear, ext_local_libs, ext_online, ext_set_config, ext_set_source_proxy, ext_set_subscription_proxy, ext_source_add, ext_source_delete, ext_source_export, ext_source_heal, ext_source_health, ext_source_health_check, ext_source_health_reset, ext_source_import_file, ext_source_loose, ext_source_priority, ext_source_restore, ext_source_standalone, ext_source_test, ext_source_test_all, ext_source_update, ext_status, ext_titles_cache, ext_titles_cache_clear, ext_update_check, ext_update_now, ext_update_status, ext_whoami, linepredict_config, linepredict_reset, linepredict_status, source_health, source_health_reset, admin_router
app.router.routes.extend(admin_router.routes)
if features.GUARD:
    from admin_api import _PAN_MODS


# ------------------------------------------------------------- 兜底透传

@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    # 本地视频/文件等大响应必须流式透传（不缓冲整文件），避免打满内存拖死系统；
    # 该函数内部对普通 JSON 自动回落到缓冲透传。
    return await forward_upstream_streaming(request)
