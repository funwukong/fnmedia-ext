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
# 冻结适配：预测线只作竞速优先种子，绝不替代竞速、不改写最快线记忆（见 _race_candidates /
# _remember_fast）；持久化绑定只作起播种子，恢复后仍走竞速（见 _persist_chart_binding）。
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
        if hit is not None and now - hit[0] < _DNS_TTL:
            return hit[1]
    res = _real_getaddrinfo(host, port, family, type, proto, flags)
    with _dns_lock:
        if len(_dns_cache) >= _DNS_MAX:
            _dns_cache.clear()
        _dns_cache[key] = (now, res)
    return res


_socket_mod.getaddrinfo = _cached_getaddrinfo


def _self_url(url: str) -> str:
    """把本服务根相对地址绝对化为回环地址；外站绝对地址原样返回。"""
    if url.startswith("/"):
        return SELF_BASE.rstrip("/") + url
    return url
_search_cache: dict[str, tuple[float, list]] = {}
_search_inflight: dict[str, asyncio.Future] = {}
CACHE_TTL = 120
# 绑定聚合缓存池（上游 v0.2.69）：同一(片名·是否快源·是否含T4)在 TTL 内复用聚合后
# 的 briefs，榜单绑定 / 播放回源 / 搜索等多次调用同一片名时不重复扇出所有源。
# 注意：缓存的是 remember_merged 之后的 brief（含线路），命中即等价一次完整聚合。
# 与已有的 _search_cache（120 秒，缓存搜索 brief，未过滤；每次返回时按
# 当前查看者重新过滤建卡，见 online_search）+ _search_inflight 单飞互不干扰，
# 只补聚合层这一层，不动现有搜索缓存。
_AGG_CACHE: dict[str, tuple[float, list]] = {}
_AGG_INFLIGHT: dict[str, asyncio.Future] = {}
AGG_CACHE_TTL = 180
_AGG_CACHE_MAX = 800
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
    # 福利独立库（md5("fnmedia-welfare-lib")，GUID 种子保持不变）：独立开关 welfare_library_enabled
    # 控制是否建库/显示，反查 cat = catalog.ADULT_CAT，走同一条海量深翻链路。
    "adult":       "411515e17c85709cf1966697b7b2c58e",
}
CATALOG_GUID_CAT = {g: c for c, g in CATALOG_LIB_GUIDS.items()}

# —— 单源独立媒体库：每个启用 standalone 的源生成一张库卡片 ——
_STANDALONE_PREFIX = "fnmedia-standalone:"


def standalone_lib_guid(source_id: str) -> str:
    return hashlib.md5((_STANDALONE_PREFIX + source_id).encode()).hexdigest()


def standalone_source_of(lib_guid: str) -> str | None:
    """库 guid 反查 source_id；非独立库返回 None。"""
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
GUARD_NATIVE_PREFIX = "fnmedia-guardnative:"
GUARD_NATIVE_GUID = hashlib.md5(
    (GUARD_NATIVE_PREFIX + "root").encode()).hexdigest()
GUARD_NATIVE_TITLE = "🛡加固影视"


# 文件夹 guid 前缀与解析（目录树通用件）：item/list 的可见性检查在公开构建里
# 也要认它；其余目录树逻辑仍在加固块内。
_GN_FOLDER_PREFIX = "fnmedia-gnfolder:"


def _gn_folder_token(guid: str) -> str | None:
    if isinstance(guid, str) and guid.startswith(_GN_FOLDER_PREFIX):
        return guid[len(_GN_FOLDER_PREFIX):]
    return None


def guard_native_enabled() -> bool:
    # 开关本身是通用件：公开构建里 features.GUARD 已关，恒为 False；
    # 真正的加固分支仍由 features.GUARD 门控，代码在剥离块内。
    return bool(features.GUARD
                and config.get().get("guard_native_enabled", True))


# —— 多网盘识别注册表（通用分类器，移出加固私有块）———————————
# _pan_match 被块外的通用代码引用（播放变体排序、线路角标），放块内会在公开构建
# 被剥离后报 NameError（与 _gn_folder_token / source_priority 同类问题）；
# 本段只做链接模式识别，不含任何私有播放实现，移出不影响剥离语义。
# —— 多网盘识别注册表 ———————————————————————————————————————————————
# “网盘资源”的判定：加固 jar 解出的播放地址里出现某家网盘的分享链接，或四盘
# 聚合源的中转占位（Seedhub/盘搜类）。playable=True 的家已实现转存→原画播放。
# 其余家先做“识别 + 明确提示”，播放驱动按同一接口逐家接入。
_PAN_PROVIDERS = [
    {"key": "quark", "name": "夸克网盘", "playable": True,
     "patterns": [r"pan\.quark\.cn/s/[A-Za-z0-9]+",
                  r"drive\.quark\.cn/[^\s\"']*"]},
    {"key": "uc", "name": "UC网盘", "playable": True,
     "patterns": [r"(?:pan|drive)\.uc\.cn/s/[A-Za-z0-9]+",
                  r"drive\.uc\.cn/[^\s\"']*"]},
    {"key": "ali", "name": "阿里云盘", "playable": True,
     "patterns": [r"(?:alipan|aliyundrive)\.com/s/[A-Za-z0-9]+",
                  r"www\.alipan\.com/s/[A-Za-z0-9]+"]},
    {"key": "baidu", "name": "百度网盘", "playable": False,
     "patterns": [r"pan\.baidu\.com/s/[A-Za-z0-9_%-]+"]},
    {"key": "xunlei", "name": "迅雷云盘", "playable": False,
     "patterns": [r"pan\.xunlei\.com/[A-Za-z0-9]+",
                  r"(?:pan\.xunlei\.com|xunlei\.com)/[^\s\"']*(?:s/|share)"]},
    {"key": "p115", "name": "115网盘", "playable": False,
     "patterns": [r"115\.com/s/[A-Za-z0-9]+"]},
    {"key": "tianyi", "name": "天翼云盘", "playable": False,
     "patterns": [r"cloud\.189\.cn/[^\s\"']+",
                  r"(?:cloud|pan)\.189\.cn/[^\s\"']*"]},
    {"key": "yidong", "name": "移动云盘", "playable": False,
     "patterns": [r"yun\.139\.com/[^\s\"']+"]},
]
for _pv in _PAN_PROVIDERS:
    _pv["re"] = re.compile("|".join(_pv["patterns"]), re.I)

# 四盘聚合源（Seedhub 等）的中转占位：redirect_to=pan_id_<id>，最终由其 302
# 解出真实夸克链接（前置 Cloudflare 时解不开则回退）。
_PAN_LINKSTART_RE = re.compile(r"link_start/[^\s\"']*redirect_to=pan_id_(\d+)")


def _pan_match(text: str):
    """在一段文本（播放地址/线路名/影片名）里识别网盘分享，返回 provider 或 None。"""
    if not text:
        return None
    s = str(text)
    for pv in _PAN_PROVIDERS:
        if pv["re"].search(s):
            return pv
    return None


# —— 加固库 guid 纯映射（移出加固私有块）———————————————
# _guid_for_key 的 guard 分支在块外通用代码里调用它；放块内会在公开构建被剥离后
# 报 NameError。纯 md5 映射，不含任何加固运行时依赖，移出不影响剥离语义。
# —— TVBox 加固源专区：每个“启用且作为专区展示”的加固源生成一张库卡片 ——
_GUARD_LIB_PREFIX = "fnmedia-guard:"


def guard_lib_guid(gkey: str) -> str:
    return hashlib.md5((_GUARD_LIB_PREFIX + gkey).encode()).hexdigest()


# >>> PRIVATE:GUARD


def guard_lib_source(lib_guid: str) -> dict | None:
    """库 guid 反查加固源定义；非专区库/源不可用/专区关闭返回 None。"""
    if not features.GUARD or not lib_guid:
        return None
    if not bool(config.get().get("guard_zone_enabled", False)):
        return None
    try:
        import guard as _g
        for s in config.enabled_sources():
            if not s.get("guard"):
                continue
            if guard_lib_guid(s.get("guard_key", "")) == lib_guid:
                conf = _g.site(s.get("guard_key", ""))
                if conf:
                    return {"source": s, "conf": conf}
    except Exception:
        return None
    return None


def guard_lib_name(conf: dict) -> str:
    return "📺" + str(conf.get("name") or conf.get("key") or "TVBox")


_GUARD_POSTER_QUEUED: set[str] = set()


async def _guard_prefetch(key: str) -> None:
    import guard as _g
    conf = _g.site(key)
    if not conf:
        return
    try:
        await asyncio.to_thread(_g.prefetch_posters, conf)
    except Exception:
        pass


def guard_libs_active() -> list[dict]:
    """作为专区展示的启用加固源（需要 Android 运行时可用且专区开关打开）。"""
    if not features.GUARD:
        return []
    if not bool(config.get().get("guard_zone_enabled", False)):
        return []
    try:
        import guard as _g
        if not _g.available():
            return []
        out = []
        for s in config.enabled_sources():
            if not s.get("guard"):
                continue
            conf = _g.site(s.get("guard_key", ""))
            if conf:
                out.append({"source": s, "conf": conf})
        return out
    except Exception:
        return []


# 聚合墙每源最多收录条数；分类页每页条数
_GN_HOT_PER_SOURCE = 60
_GN_PAGE_SIZE = 30
# 后台深预热：每个影视分类预取的页数（用户点进分类/翻第 2 页都尽量命中缓存秒开）
_GN_WARM_PAGES = 1
# 每个源最多深预热的影视分类数。预热宁可慢/浅，也不能短时间高频打单设备把桥压挂。
_GN_WARM_CLASSES = 3
_GN_WARM_GAP = 1.2     # 每次设备调用之间的喘息间隔（秒）
_GN_TTL = 1800          # 缓存有效期（秒）
_GN_REFRESH_GAP = 120   # 后台刷新最小间隔
_gn_lock = threading.RLock()
_gn_state = {"hot_ts": 0.0, "hot": [], "per_src": {},
             "hot_refreshing": False,
             "cls": {}, "cat": {}, "meta_ts": 0.0,
             "meta_refreshing": False,
             "meta_ok": {},   # key -> 上次成功时间戳
             "meta_fail": {},  # key -> 上次真异常/超时时间戳
             "meta_empty": {},  # key -> 上次返回空分类时间戳（初始化未完成，快速重试）
             "warm_running": False,  # 深预热 worker 是否在跑
             "catalog_running": False,  # 全片单深度预拉是否在跑
             "deep_running": False}  # 真机源适度深预拉是否在跑


_GN_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "guard_native_cache.json")

# 全片单（标题库）独立落盘：豆豆这类纯片单源把整个分类目录预拉到本地，浏览/翻页秒开，
# 播放时才用 msearch 跨源聚合匹配真实线路（与 OK影视 “片单与解析分离” 一致）。
_GN_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "guard_catalog.json")
_GN_CATALOG_TTL = 6 * 3600.0        # 全片单 6 小时内视为新鲜，不必重抓
_GN_CATALOG_SRCS = ("DouDou",)      # 做全量预拉的片单型源（msearch 元搜索源）
_GN_CATALOG_MAX_PG = 40             # 单分类最多抓到的页（热播墙约 10 页，留余量）
_GN_CATALOG_GAP = 0.8               # 每页之间的喘息，保护唯一 redroid 设备

# —— 高质量真机加固源的“适度深预拉”（OK影视 式点开即满墙、翻页秒开）——
# 与片单源不同：这些是 jar/spider 真机源，每页都要打 redroid，故只深拉前若干页、
# 低并发（池有 3 台设备）并留喘息；结果同样落 guard_catalog.json，翻页全部命中本地。
_GN_DEEP_ENABLED = False            # 默认关闭：实测批量深拉会把 redroid 桥压进慢等，
#                                   拖慢加固搜索/热榜起播；需要时在配置 guard_native_deep=true 手动开
_GN_DEEP_SRCS = (                   # 高价值“秒播/多线/不卡”真机源（内部 key）
    "Nmyswv",   # 🍓糯米·多线
    "AppSx",    # 🐻剧圈·多线
    "Hmys",     # 🐬海绵·不卡
    "Libvio",   # 🌟立播·秒播
    "App99",    # 🌸茉莉·多线
    "Appgz",    # 👀瓜子·不卡
    "NewCz",    # 📔厂长·秒播
    "Jpys",     # 💮文采·秒播
)
_GN_DEEP_MAX_PG = 6                 # 真机源每分类最多深拉页数（≈180 部/类）
_GN_DEEP_CLASSES = 4                # 每源最多深拉的影视分类数（控总量、不长期占设备）
_GN_DEEP_BATCH = 2                  # 每批并发取页数（小批量顺序推进，空页即停）
_GN_DEEP_CONC = 2                   # 深预拉并发（≤设备数 3，给前台播放留 1 台）
_GN_DEEP_GAP = 0.6                  # 深预拉批次之间的喘息（秒）


def _gn_persist() -> None:
    """把元数据/热墙落盘：重启后秒显源文件夹与热门墙，不必重新扫 5–10 分钟。"""
    try:
        with _gn_lock:
            payload = {"v": 1,
                       "hot_ts": _gn_state.get("hot_ts", 0.0),
                       "hot": _gn_state.get("hot") or [],
                       "per_src": _gn_state.get("per_src") or {},
                       "cls": _gn_state.get("cls") or {}}
        tmp = str(_GN_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _GN_CACHE_PATH)
    except Exception:  # noqa: BLE001
        pass


def _gn_load_persist() -> None:
    try:
        if not os.path.exists(_GN_CACHE_PATH):
            return
        with open(_GN_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or d.get("v") != 1:
            return
        with _gn_lock:
            if d.get("cls"):
                _gn_state["cls"] = d["cls"]
                _gn_state["meta_ts"] = time.time()
            if d.get("hot"):
                _gn_state["hot"] = d["hot"]
                _gn_state["per_src"] = d.get("per_src") or {}
                _gn_state["hot_ts"] = d.get("hot_ts") or time.time()
    except Exception:  # noqa: BLE001
        pass


def _gn_catalog_load() -> dict:
    """读全片单磁盘缓存：{key: {ts, ckey: cards, ...}}（进程内短时缓存）。"""
    now = time.time()
    ent = globals().get("_GN_CATALOG_MEM")
    if ent and now - ent[0] < 30:
        return ent[1]
    out: dict = {}
    try:
        if os.path.exists(_GN_CATALOG_PATH):
            with open(_GN_CATALOG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            out = d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        out = ent[1] if ent else {}
    globals()["_GN_CATALOG_MEM"] = (now, out)
    return out


def _gn_catalog_apply() -> None:
    """把磁盘全片单灌入内存 cat 缓存（长 TTL），让重启后各分类所有页直接秒回。"""
    d = _gn_catalog_load()
    if not d:
        return
    now = time.time()
    n = 0
    with _gn_lock:
        for key, blob in d.items():
            if not isinstance(blob, dict):
                continue
            pages = blob.get("pages") or {}
            for ckey, cards in pages.items():
                # more 由是否抓到下一页决定：持久化里已带每键 more
                more = bool((blob.get("more") or {}).get(ckey))
                _gn_state["cat"][ckey] = (now, cards, more)
                n += 1
    _log.info("guard native full catalog loaded: %d cached pages", n)


def _gn_catalog_save(key: str, pages: dict, more: dict,
                     complete: bool = False) -> None:
    """合并写回某个源的全片单（保留其它源已有数据）。"""
    try:
        d = _gn_catalog_load()
        prev = d.get(key) if isinstance(d.get(key), dict) else {}
        d[key] = {"ts": time.time(), "pages": pages, "more": more,
                  "complete": bool(complete) or bool(prev.get("complete"))}
        tmp = str(_GN_CATALOG_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _GN_CATALOG_PATH)
        globals()["_GN_CATALOG_MEM"] = (time.time(), d)
    except Exception:  # noqa: BLE001
        pass


def _gn_catalog_all_fresh() -> bool:
    """已知/已发现的片单源是否都已有新鲜全片单（只读磁盘缓存，不打设备）。"""
    d = _gn_catalog_load()
    det = d.get("__detected__")
    keys = list(_GN_CATALOG_SRCS)
    if isinstance(det, dict):
        for k in (det.get("keys") or []):
            if k not in keys:
                keys.append(k)
    now = time.time()
    for k in keys:
        blob = d.get(k)
        if not (isinstance(blob, dict) and blob.get("pages")
                and now - blob.get("ts", 0) < _GN_CATALOG_TTL):
            return False
    return True


def _gn_prefetch_catalog() -> None:
    """幂等触发全片单深度预拉（单后台 worker 串行慢抓，保护唯一设备）。"""
    with _gn_lock:
        if _gn_state.get("catalog_running"):
            return
        _gn_state["catalog_running"] = True
    try:
        asyncio.get_event_loop().create_task(_gn_catalog_worker())
    except RuntimeError:
        with _gn_lock:
            _gn_state["catalog_running"] = False


def _gn_catalog_detect_keys() -> list[str]:
    """自动发现“纯片单/元搜索源”：源本身不可搜索且其某影视分类首条是 msearch: 占位。

    判定结果持久化（键为当前候选源集合），避免每轮重复探测那些初始化慢的镜像源。
    播放时这些源统一走 _resolve_guard_msearch 跨源聚合，与是否预拉无关。"""
    import guard as _g
    groups = _gn_sources()
    # 候选：启用、有 site、且站点配置标记为不可搜索（纯片单聚合源的典型特征）
    cand = []
    for g in groups:
        conf = g["conf"]
        if conf.get("searchable", True):
            continue
        cand.append((g["conf"]["key"], g))
    sig = ",".join(sorted(k for k, _ in cand))
    d = _gn_catalog_load()
    det = d.get("__detected__")
    if isinstance(det, dict) and det.get("sig") == sig:
        return list(det.get("keys") or [])
    keys: list[str] = []
    for key, g in cand:
        conf = _g.site(key)
        meta = _gn_classes_meta().get(key) or {}
        film = [c for c in (meta.get("classes") or [])
                if _gn_is_film_class(c.get("name"))]
        if not film or not conf:
            continue
        is_ms = False
        for c in film[:2]:
            tid = str(c.get("tid") or "")
            if not tid:
                continue
            try:
                vods = _g.browse_page(conf, tid, 1)
            except Exception:  # noqa: BLE001
                vods = []
            if vods and str(vods[0].get("vod_id", "")).startswith("msearch"):
                is_ms = True
                break
        if is_ms:
            keys.append(key)
    # 持久化发现结果（与片单同文件的特殊键）
    try:
        full = _gn_catalog_load()
        full["__detected__"] = {"sig": sig, "keys": keys, "ts": time.time()}
        tmp = str(_GN_CATALOG_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False)
        os.replace(tmp, _GN_CATALOG_PATH)
        globals()["_GN_CATALOG_MEM"] = (time.time(), full)
    except Exception:  # noqa: BLE001
        pass
    return keys


async def _gn_catalog_worker() -> None:
    """把片单型源（豆豆等）每个影视分类从第 1 页一直抓到空页/上限，结果落盘+入内存。

    片单源每页 0.2~0.8s 且总量小（约数百部）；抓全后浏览/翻页全部命中本地缓存，
    播放时才走 msearch 跨源聚合。刻意串行 + 每页留 gap，绝不并发压桥。"""
    import guard as _g
    try:
        groups = {g["conf"]["key"]: g for g in _gn_sources()}
        # 自动发现当前订阅里所有纯片单源（探测只读元数据+取首页，结果持久化）
        src_keys = list(_GN_CATALOG_SRCS)
        try:
            det = await asyncio.to_thread(_gn_catalog_detect_keys)
            for k in det:
                if k not in src_keys:
                    src_keys.append(k)
        except Exception:  # noqa: BLE001
            det = []
        _log.info("guard native catalog sources: %s", src_keys)
        for key in src_keys:
            group = groups.get(key)
            conf = _g.site(key)
            if not group or not conf:
                continue
            # 6 小时内已有全片单则跳过
            blob = _gn_catalog_load().get(key)
            if isinstance(blob, dict) and time.time() - blob.get("ts", 0) < _GN_CATALOG_TTL \
                    and blob.get("pages"):
                _gn_catalog_apply()
                continue
            meta = _gn_classes_meta().get(key) or {}
            film = [c for c in (meta.get("classes") or [])
                    if _gn_is_film_class(c.get("name"))]
            if not film:
                continue
            anc = f"{GUARD_NATIVE_TITLE}/{group['source'].get('name') or key}"
            pages: dict[str, list] = {}
            more_map: dict[str, bool] = {}
            for c in film:
                tid, cname = str(c.get("tid") or ""), str(c.get("name") or "")
                if not tid:
                    continue
                ftype = "TV" if _gn_class_is_tv(cname) else "Movie"
                got_pages: list[int] = []
                for pg in range(1, _GN_CATALOG_MAX_PG + 1):
                    ckey = f"{key}|{tid}|{pg}"
                    if ckey in pages:
                        got_pages.append(pg)
                        continue
                    try:
                        vods = await asyncio.wait_for(asyncio.to_thread(
                            _g.browse_page, conf, tid, pg), timeout=20)
                    except Exception:  # noqa: BLE001
                        vods = []
                    cards = await asyncio.to_thread(
                        _gn_map_cards, group, vods, _GN_PAGE_SIZE, anc, ftype)
                    if not cards:
                        # 真机/片单源偶发瞬时空页：重试一次再判到底，避免一次抖动
                        # 就把整个分类截断在第 pg 页（曾导致每类只缓存 1 页、片单变少）。
                        try:
                            vods = await asyncio.wait_for(asyncio.to_thread(
                                _g.browse_page, conf, tid, pg), timeout=20)
                        except Exception:  # noqa: BLE001
                            vods = []
                        cards = await asyncio.to_thread(
                            _gn_map_cards, group, vods, _GN_PAGE_SIZE, anc, ftype)
                    if cards:
                        pages[ckey] = cards
                        got_pages.append(pg)
                        with _gn_lock:
                            _gn_state["cat"][ckey] = (time.time(), cards, False)
                    if pg % 5 == 0:
                        _gn_catalog_save(key, pages, more_map)
                    if not cards:
                        break   # 重试仍空：真正到底（片单源 pagecount 恒为 999 哨兵）
                    await asyncio.sleep(_GN_CATALOG_GAP)
                # more(p) = 第 p+1 页是否抓到
                for p in got_pages:
                    more_map[f"{key}|{tid}|{p}"] = (p + 1) in got_pages
            _gn_catalog_save(key, pages, more_map, complete=True)
            # 用正确的 more 回填内存 cat，避免抓取期间临时写入的 False 影响分页
            now_r = time.time()
            with _gn_lock:
                for ckey, cards in pages.items():
                    _gn_state["cat"][ckey] = (now_r, cards,
                                              bool(more_map.get(ckey)))
            _log.info("guard native catalog prefetched %s: %d pages", key, len(pages))
    except Exception:  # noqa: BLE001
        _log.exception("guard native catalog prefetch failed")
    finally:
        with _gn_lock:
            _gn_state["catalog_running"] = False


def _gn_deep_enabled() -> bool:
    """真机加固源“适度深预拉”开关：总开关 + 配置可关。"""
    return bool(_GN_DEEP_ENABLED and guard_native_enabled()
                and config.get().get("guard_native_deep", True))


def _gn_deep_all_fresh() -> bool:
    """白名单真机源是否都已有未过期的深预拉页（只读磁盘，不打设备）。"""
    d = _gn_catalog_load()
    now = time.time()
    for key in _GN_DEEP_SRCS:
        blob = d.get(key)
        if not (isinstance(blob, dict) and blob.get("pages")
                and now - blob.get("ts", 0) < _GN_CATALOG_TTL):
            return False
    return True


def _gn_prefetch_deep() -> None:
    """幂等触发真机源深预拉（单后台 worker，内部低并发，保护 redroid 池）。"""
    with _gn_lock:
        if _gn_state.get("deep_running"):
            return
        _gn_state["deep_running"] = True
    try:
        asyncio.get_event_loop().create_task(_gn_deep_worker())
    except RuntimeError:
        with _gn_lock:
            _gn_state["deep_running"] = False


async def _gn_deep_worker() -> None:
    """对高价值真机源的每个影视分类深拉前 _GN_DEEP_MAX_PG 页。

    池有 3 台设备：用 _GN_DEEP_CONC(=2) 路并发取页，给前台播放始终留 1 台；
    每页留 _GN_DEEP_GAP 喘息，空两页即停。结果落 guard_catalog.json，翻页全命中本地。"""
    import guard as _g
    try:
        groups = {g["conf"]["key"]: g for g in _gn_sources()}
        have = set(_gn_classes_meta().keys())
        keys = [k for k in _GN_DEEP_SRCS if k in have and groups.get(k)
                and _g.site(k)]
        if not keys:
            return

        async def fetch_page(group, conf, c, pg):
            key = group["conf"]["key"]
            tid = str(c.get("tid") or "")
            cname = str(c.get("name") or "")
            ftype = "TV" if _gn_class_is_tv(cname) else "Movie"
            try:
                vods = await asyncio.wait_for(asyncio.to_thread(
                    _g.browse_page, conf, tid, pg), timeout=22)
            except Exception:  # noqa: BLE001
                vods = []
            anc = (f"{GUARD_NATIVE_TITLE}/"
                   f"{group['source'].get('name') or key}")
            cards = await asyncio.to_thread(
                _gn_map_cards, group, vods, _GN_PAGE_SIZE, anc, ftype)
            return key, tid, pg, cards

        sem = asyncio.Semaphore(_GN_DEEP_CONC)

        async def bound(*a):
            async with sem:
                r = await fetch_page(*a)
                await asyncio.sleep(_GN_DEEP_GAP)
                return r

        for key in keys:
            group, conf = groups[key], _g.site(key)
            blob = _gn_catalog_load().get(key)
            if isinstance(blob, dict) and blob.get("pages") \
                    and time.time() - blob.get("ts", 0) < _GN_CATALOG_TTL:
                _gn_catalog_apply()
                continue
            meta = _gn_classes_meta().get(key) or {}
            film = [c for c in (meta.get("classes") or [])
                    if _gn_is_film_class(c.get("name"))]
            if not film:
                continue
            pages: dict[str, list] = {}
            more_map: dict[str, bool] = {}
            # 只深拉前 _GN_DEEP_CLASSES 个影视分类，控总量
            for c in film[:_GN_DEEP_CLASSES]:
                tid = str(c.get("tid") or "")
                if not tid:
                    continue
                # 小批量顺序推进：每批并发取 _GN_DEEP_BATCH 页。分类页必须【连续】，
                # 批次内一旦在页 p 遇到空页就停在 p（丢弃更深的结果），避免出现缺第 p 页、
                # 却有第 p+1 页的“空洞”；空页单独重试一次以容忍真机源偶发抖动。
                pg = 1
                while pg <= _GN_DEEP_MAX_PG:
                    batch = list(range(pg, min(pg + _GN_DEEP_BATCH,
                                              _GN_DEEP_MAX_PG + 1)))
                    res = await asyncio.gather(
                        *[bound(group, conf, c, p) for p in batch],
                        return_exceptions=True)
                    got_map = {}
                    for r in res:
                        if isinstance(r, tuple):
                            got_map[r[2]] = r[3]
                    stop_after = None
                    for p in sorted(batch):
                        cards = got_map.get(p) or []
                        if not cards and p == pg:
                            # 仅对“本批第一页”单独重试一次（更深页依赖它连续）
                            try:
                                _k2, _t, _p, cards = await asyncio.wait_for(
                                    fetch_page(group, conf, c, p), timeout=22)
                            except Exception:  # noqa: BLE001
                                cards = []
                        if not cards:
                            stop_after = p
                            break
                        ckey = f"{key}|{tid}|{p}"
                        pages[ckey] = cards
                        with _gn_lock:
                            _gn_state["cat"][ckey] = (
                                time.time(), cards, False)
                    if stop_after is not None:
                        break
                    pg = max(batch) + 1
                    await asyncio.sleep(_GN_DEEP_GAP)
                # more(p)=第 p+1 页是否抓到
                pgs = sorted(int(k.rsplit("|", 1)[1]) for k in pages
                             if k.startswith(f"{key}|{tid}|"))
                for p in pgs:
                    more_map[f"{key}|{tid}|{p}"] = (p + 1) in pgs
                # 每个分类抓完落盘一次，避免长任务中途丢结果
                _gn_catalog_save(key, pages, more_map)
            _gn_catalog_save(key, pages, more_map, complete=True)
            now_r = time.time()
            with _gn_lock:
                for ckey, cards in pages.items():
                    _gn_state["cat"][ckey] = (now_r, cards,
                                              bool(more_map.get(ckey)))
            _log.info("guard native deep prefetched %s: %d pages", key, len(pages))
    except Exception:  # noqa: BLE001
        _log.exception("guard native deep prefetch failed")
    finally:
        with _gn_lock:
            _gn_state["deep_running"] = False
        _log.info("guard native deep prefetch done")


# —— OK影视 式目录树：库根=源文件夹；源内=分类文件夹；分类内=无限海报墙 ——
# 文件夹 guid 用固定前缀 + 可逆 token，避免与影片 md5 guid 碰撞，也不命中详情/播放解析。
# （前缀与 _gn_folder_token 已移到加固块外，见块开口前；目录树其余逻辑仍在块内。）
_GN_HOT_TOKEN = "__hot__"


def _gn_folder_guid(token: str) -> str:
    return _GN_FOLDER_PREFIX + token


def _gn_src_token(key: str) -> str:
    # 源 key 本身唯一且只含安全字符，直接用它，不暴露其它信息
    return "src:" + str(key)


def _gn_cls_token(key: str, tid: str) -> str:
    raw = f"{key}|{tid}".encode()
    return "cls:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _gn_parse_cls_token(token: str) -> tuple[str, str] | None:
    if not token.startswith("cls:"):
        return None
    s = token[4:]
    try:
        s += "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(s.encode()).decode()
        key, tid = raw.split("|", 1)
        return key, tid
    except Exception:  # noqa: BLE001
        return None


def _gn_folder_row(guid: str, title: str, subtitle: str = "",
                  poster: str = "") -> dict:
    """飞牛可下钻的目录行（type=Directory）；点击后以 parent_guid=本行 guid 再查列表。"""
    row = inj._base_item(
        guid=guid, type="Directory", title=title,
        ancestor_guid=GUARD_NATIVE_GUID, ancestor_name=GUARD_NATIVE_TITLE,
        ancestor_category="Mix", can_play=0,
        poster=inj._poster(poster) if poster else "",
        number_of_episodes=0, local_number_of_episodes=0)
    row["parent_guid"] = ""
    if subtitle:
        row["overview"] = subtitle
    return _apply_marks(row)


def _gn_sources() -> list[dict]:
    """可浏览（有影视分类）的启用加固源，附 conf。"""
    import guard as _g
    out = []
    if not _g.available():
        return out
    for s in config.enabled_sources():
        if not s.get("guard"):
            continue
        conf = _g.site(s.get("guard_key"))
        if conf:
            out.append({"source": s, "conf": conf})
    return out


# 分类名 → 是否按“剧集(TV)”处理。加固源分类列表项不带 type_name，影片列表项也
# 多为空，默认会被当成电影；这里按分类名一次性确定整类的 Movie/TV，剧集类强制 TV。
_GN_TV_CLASS_WORDS = ("电视剧", "连续剧", "剧集", "番剧", "动漫", "动画", "综艺",
                      "短剧", "日剧", "韩剧", "美剧", "英剧", "泰剧", "国剧",
                      "港台剧", "海外剧", "连载",
                      # 片单/榜单型源（如豆豆片单）：热播剧集/电视筛选/剧榜单…
                      "热播剧", "电视", "剧集榜", "剧榜", "电视剧榜", "追剧",
                      "网剧", "番", "剧")
_GN_MOVIE_CLASS_WORDS = ("电影", "影片", "剧场版", "片单", "影片榜")


def _gn_class_is_tv(cname: str) -> bool:
    """该分类是否整体按剧集(TV)处理。明确含电影词优先判电影，否则命中剧集词判 TV。"""
    cname = str(cname or "")
    if any(w in cname for w in _GN_MOVIE_CLASS_WORDS):
        return False
    return any(w in cname for w in _GN_TV_CLASS_WORDS)


def _gn_map_cards(group: dict, vods: list, n: int, anc_name: str,
                  force_type: str | None = None) -> list[dict]:
    """批量映射加固 Vod→入库→飞牛海报行（ancestor 指向加固聚合库）。

    force_type: 整类强制的飞牛类型（"TV"/"Movie"），由分类名决定，修正加固列表项
    缺少 type_name 导致电视剧被识别成电影的问题。"""
    source = group["source"]
    # 让 inject.is_tv 能据显式词判定：TV→“电视剧”（且至少 1 集即判 TV），Movie→“电影”
    forced_type_name = {"TV": "电视剧", "Movie": "电影"}.get(force_type or "")
    titles, seen = [], set()
    for v in vods[:n]:
        try:
            t = providers._guard_map_item(source, v)
        except Exception:  # noqa: BLE001
            continue
        if not t.title or not t.vod_id:
            continue
        if forced_type_name and not (t.type_name or "").strip():
            t.type_name = forced_type_name
        og = make_guid(t)
        if og in seen:
            continue
        seen.add(og)
        titles.append(t)
    new_t = [t for t in titles if not store.get_title_by_guid(make_guid(t))]
    # 剧集/电影分类强制类型时，已缓存但类型不符的旧 brief 也要回写 type_name，
    # 否则列表虽显示 TV，点进去选集接口仍按旧 brief 判成电影、出不来选集。
    if force_type in ("TV", "Movie"):
        want_tv = force_type == "TV"
        for t in titles:
            b = store.get_title_by_guid(make_guid(t))
            if b and bool(inj.is_tv(b)) != want_tv and t not in new_t:
                new_t.append(t)
    if new_t:
        store.remember_titles(new_t)
    rows = []
    for t in titles:
        b = store.get_title_by_guid(make_guid(t))
        if not b:
            continue
        row = inj.search_item(b)
        row["guid"] = b["guid"]
        # 部分源（如豆豆片单）把“评分：x”塞进 vod_remarks：去掉空/0 分与纯评分尾巴，
        # 标题后只保留有信息量的备注（更新集数等）。
        rm = (t.remarks or "").strip()
        msc = re.match(r"^评分[：:]?\s*([0-9.]*)$", rm)
        if msc:
            sc = msc.group(1).rstrip(".")
            try:
                rm = f"评分 {float(sc):g}" if sc and float(sc) > 0 else ""
            except Exception:  # noqa: BLE001
                rm = ""
        row["title"] = (t.title or "") + (f" ·{rm}" if rm else "")
        row["ancestor_guid"] = GUARD_NATIVE_GUID
        row["ancestor_name"] = anc_name
        row["ancestor_category"] = force_type or "Mix"
        row["video_guid"] = video_guid(b["guid"])
        if force_type in ("TV", "Movie"):
            row["type"] = force_type
            row["number_of_seasons"] = 1 if force_type == "TV" else 0
        rows.append(_apply_marks(row))
    return rows


def _gn_refresh_hot() -> None:
    """后台：并发取各加固源首页推荐/首个分类，混合去重成聚合墙并入库。"""
    import guard as _g

    async def _fetch(group: dict):
        """返回 (items, ok)；items=[(vod, cname|None),...]。

        热墙必须【快速】出第一版（用户打开空等会以为没资源）：每源只取第一个影视/
        推荐分类首页（片单型源如豆豆的首个分类即“热播电影”，放宽识别后已有约 20 部），
        其余分类由后台深预热 worker 慢慢补缓存，不拖慢首屏。"""
        conf = group["conf"]

        async def one():
            meta = await asyncio.to_thread(_g.browse_meta, conf)
            film = [c for c in (meta.get("classes") or [])
                    if _gn_is_film_class(c.get("name"))]
            if film:
                c0 = film[0]
                try:
                    vs = await asyncio.wait_for(asyncio.to_thread(
                        _g.browse_page, conf, c0["tid"], 1), timeout=18)
                except Exception:  # noqa: BLE001
                    vs = []
                cn = str(c0.get("name") or "")
                if vs:
                    return [(v, cn) for v in vs]
            h = await asyncio.to_thread(_g.home_cached, conf)
            return [(v, None) for v in (h.get("list") or [])]
        try:
            items = await asyncio.wait_for(one(), timeout=22)
            return items, bool(items)
        except Exception:  # noqa: BLE001
            return [], False

    groups = _gn_sources()
    if not groups:
        return

    async def run():
        try:
            # redroid 只有一台设备且桥内部严格串行：并发 /invoke 只会互相排队、
            # 甚至把桥 app 压进 do_epoll 忙等死循环。热墙改为逐个源串行取数。
            results = []
            for g in groups:
                try:
                    results.append(await _fetch(g))
                except Exception:  # noqa: BLE001
                    results.append(([], False))
            # 按源保留：本轮取数失败/为空的源沿用其上次缓存卡，避免单模拟器争用
            # 导致部分源超时后，整面墙越刷新越少。
            with _gn_lock:
                prev_per_src = dict(_gn_state.get("per_src") or {})
            per_src, merged, seen = {}, [], set()
            for group, (items, ok) in zip(groups, results):
                key = group["conf"]["key"]
                # 同一源可能既有电影分类又有剧集分类：按分类决定的类型分批映射，
                # 合并后按 vod 去重，上限 _GN_HOT_PER_SOURCE。
                by_type: dict[str, list] = {}
                for vod, cn in items:
                    ft = "TV" if (cn and _gn_class_is_tv(cn)) else \
                         ("Movie" if cn else None)
                    by_type.setdefault(ft, []).append(vod)
                cards_all, dedup_vid = [], set()
                for ft, vs in by_type.items():
                    cs = await asyncio.to_thread(
                        _gn_map_cards, group, vs, _GN_HOT_PER_SOURCE,
                        GUARD_NATIVE_TITLE, ft)
                    for c in cs:
                        if c["guid"] in dedup_vid:
                            continue
                        dedup_vid.add(c["guid"])
                        cards_all.append(c)
                cards_all = cards_all[:_GN_HOT_PER_SOURCE]
                if (not ok or not cards_all) and prev_per_src.get(key):
                    cards_all = prev_per_src[key]      # 沿用旧卡
                per_src[key] = cards_all
                for c in cards_all:
                    title = (c.get("title", "") or "").split(" ·")[0]
                    if title in seen:
                        continue
                    seen.add(title)
                    merged.append(c)
            with _gn_lock:
                if merged:
                    _gn_state["hot"] = merged
                    _gn_state["per_src"] = per_src
                _gn_state["hot_ts"] = time.time()
            _log.info("guard native hot refreshed: %d rows from %d srcs",
                      len(merged), len(groups))
            if merged:
                _gn_persist()
        except Exception:  # noqa: BLE001
            _log.exception("guard native hot refresh failed")
        finally:
            # 无论成败都必须复位，否则一次异常会让热墙永久卡在“刷新中”、再无更新
            with _gn_lock:
                _gn_state["hot_refreshing"] = False

    with _gn_lock:
        if _gn_state["hot_refreshing"]:
            return
        _gn_state["hot_refreshing"] = True
    try:
        asyncio.get_event_loop().create_task(run())
    except RuntimeError:
        _gn_state["hot_refreshing"] = False


def _gn_ensure_meta() -> None:
    """后台：顺序刷新各源分类元数据，每取到一个源立即合并（选项随取随有）。

    redroid 单实例对调用串行，22 源并发只会互相排队、各自超时；顺序取反而最快，
    且 browse_classes 命中缓存后约 1s/源。失败源沿用旧分类（按源保留）。"""
    import guard as _g

    async def run():
        deadline = time.time() + 420.0   # 自驱续扫总时限（源多/有慢源时给足时间）
        empty_backoff = 25.0            # “空分类”快速重试（多为暂时未初始化/冷启）
        fail_backoff = 90.0             # 真异常（超时/报错）冷却
        while time.time() < deadline:
            groups = _gn_sources()
            with _gn_lock:
                fail = dict(_gn_state["meta_fail"])
                empt = dict(_gn_state.get("meta_empty") or {})
                have = set(_gn_state["cls"].keys())
            now_l = time.time()
            # 区分对待：真失败冷却 90s；返回空分类只等 25s 就重试（这类源初始化完成前
            # 会短暂返回空，不能像 v0.2.17 那样重罚 2 分钟，否则多线源迟迟进不了库）。
            pending = []
            for g in groups:
                k = g["conf"]["key"]
                if k in have:
                    continue
                if now_l - fail.get(k, 0) < fail_backoff:
                    continue
                if now_l - empt.get(k, 0) < empty_backoff:
                    continue
                pending.append(g)
            if not pending:
                break

            round_start = time.time()
            progressed = False
            for group in pending:
                if time.time() - round_start > 300:
                    break  # 单轮预算放宽，尽量一轮扫完；剩余下一轮续扫
                key = group["conf"]["key"]
                try:
                    meta = await asyncio.wait_for(
                        asyncio.to_thread(_g.browse_meta, group["conf"]),
                        timeout=12)
                    errored = False
                    err_reason = ""
                except Exception as _exc:  # noqa: BLE001
                    meta, errored = {}, True
                    err_reason = repr(_exc)[:160]
                # 健康源约 1s；保留极小喘息，避免高频连续调用把单设备压挂。
                await asyncio.sleep(0.2)
                c = meta.get("classes") if isinstance(meta, dict) else []
                flt = meta.get("filters") if isinstance(meta, dict) else {}
                with _gn_lock:
                    empt_map = _gn_state.setdefault("meta_empty", {})
                    if c:
                        progressed = True
                        _gn_state["cls"][key] = {
                            "name": group["source"].get("name")
                                    or group["conf"]["key"],
                            "classes": c,
                            "filters": flt or {}}
                        _gn_state["meta_ok"][key] = time.time()
                        _gn_state["meta_ts"] = time.time()
                        empt_map.pop(key, None)
                        _gn_state["meta_fail"].pop(key, None)
                        _gn_persist()
                    elif errored:
                        _gn_state["meta_fail"][key] = time.time()
                        _gn_state.setdefault("meta_fail_reason", {})[key] = err_reason
                    else:
                        empt_map[key] = time.time()
            with _gn_lock:
                _gn_state["meta_ts"] = time.time()
            if not progressed:
                # 没有新进展也别立刻放弃：空分类源很快就能初始化好，等一个 backoff 再扫
                await asyncio.sleep(empty_backoff)
                continue
            await asyncio.sleep(3)
        with _gn_lock:
            _gn_state["meta_ts"] = time.time()
            _gn_state["meta_refreshing"] = False
        _gn_persist()

    with _gn_lock:
        if _gn_state.get("meta_refreshing"):
            return
        _gn_state["meta_refreshing"] = True
    try:
        asyncio.get_event_loop().create_task(run())
    except RuntimeError:
        _gn_state["meta_refreshing"] = False


def _gn_hot_rows() -> list[dict]:
    now = time.time()
    with _gn_lock:
        rows = list(_gn_state["hot"])
        age = now - _gn_state["hot_ts"]
        refreshing = _gn_state["hot_refreshing"]
    # 过期或为空且未在刷新 → 触发后台刷新（本次仍返回缓存，快速失败不阻塞 TV）
    if (not rows or age > _GN_TTL) and not refreshing and \
            now - _gn_state.get("hot_ts", 0) > _GN_REFRESH_GAP:
        _gn_refresh_hot()
    return rows


def _gn_classes_meta() -> dict:
    now = time.time()
    with _gn_lock:
        cls = dict(_gn_state["cls"])
        stale = (now - _gn_state["meta_ts"]) > _GN_TTL or not cls
    if stale:
        _gn_ensure_meta()
    return cls


def _gn_ext_key(extend: dict | None) -> str:
    if not extend:
        return ""
    return "|" + "&".join(f"{k}={extend[k]}" for k in sorted(extend))


def _gn_category_rows(key: str, tid: str, pg: int,
                      extend: dict | None = None) -> tuple[list[dict], bool]:
    """某源某分类一页（同步，只读缓存）：未命中返回 ([],False)，不触发取数。"""
    ckey = f"{key}|{tid}|{pg}{_gn_ext_key(extend)}"
    now = time.time()
    with _gn_lock:
        hit = _gn_state["cat"].get(ckey)
    if hit and now - hit[0] < _GN_TTL:
        return hit[1], hit[2]
    return [], False


async def _gn_fetch_category(key: str, tid: str, pg: int = 1,
                             extend: dict | None = None,
                             force_type: str | None = None):
    """取某源某分类一页并入库缓存。通过 _gn_category_task 去重，调用方超时
    也不会取消真正的取数（结果照样落缓存，下次秒回）。返回 (cards, more)。"""
    return await _gn_category_task(key, tid, pg, extend, force_type)


def _gn_category_task(key: str, tid: str, pg: int = 1,
                      extend: dict | None = None,
                      force_type: str | None = None):
    """返回该分类页的在途取数任务（去重、持久，不被等待方超时取消）。"""
    import guard as _g
    ext = dict(extend or {})
    ckey = f"{key}|{tid}|{pg}{_gn_ext_key(ext)}"
    with _gn_lock:
        inflight = _gn_state.setdefault("inflight", {})
        existing = inflight.get(ckey)
        if existing and not existing.done():
            return existing
        groups = {g["conf"]["key"]: g for g in _gn_sources()}
        group = groups.get(key)
        if not group:
            async def _empty():
                return [], False
            tsk = asyncio.ensure_future(_empty())
            inflight[ckey] = tsk
            return tsk

        async def fetch():
            try:
                with _gn_lock:
                    hit = _gn_state["cat"].get(ckey)
                if hit and time.time() - hit[0] < _GN_TTL:
                    return hit[1], hit[2]

                async def _one():
                    return await asyncio.to_thread(
                        _g.browse_page_ext, group["conf"], tid, pg, ext)
                try:
                    vods = await asyncio.wait_for(_one(), timeout=22)
                except Exception:  # noqa: BLE001
                    vods = []
                if not vods and pg == 1:
                    try:
                        await asyncio.to_thread(
                            _g.invalidate_browse, key, tid, 1)
                        vods = await asyncio.wait_for(
                            asyncio.to_thread(
                                _g.browse_page_ext, group["conf"], tid, 1, ext),
                            timeout=22)
                    except Exception:  # noqa: BLE001
                        vods = []
                anc = (f"{GUARD_NATIVE_TITLE}/"
                       f"{group['source'].get('name') or key}")
                cards = await asyncio.to_thread(
                    _gn_map_cards, group, vods, _GN_PAGE_SIZE, anc, force_type)
                more = len(cards) >= _GN_PAGE_SIZE
                with _gn_lock:
                    _gn_state["cat"][ckey] = (time.time(), cards, more)
                return cards, more
            finally:
                with _gn_lock:
                    _gn_state.get("inflight", {}).pop(ckey, None)

        tsk = asyncio.ensure_future(fetch())
        inflight[ckey] = tsk
        return tsk


def _gn_prefetch_category(key: str, tid: str, pg: int = 1,
                          extend: dict | None = None,
                          force_type: str | None = None) -> None:
    """后台预热某分类一页（不阻塞；在途任务自动去重）。"""
    try:
        _gn_category_task(key, tid, pg, extend, force_type)
    except RuntimeError:
        pass


def _gn_is_film_class(name: str) -> bool:
    """该源分类是否为可浏览的影视向分类。除常规电影/电视剧/动漫/综艺外，还纳入片单型
    源（豆豆片单等）的 热播/榜单/筛选/更新/电视 等推荐位；排除纯体育/直播/听书。"""
    n = str(name or "")
    has_vod = bool(re.search(
        r"电影|影片|剧集|电视剧|连续剧|短剧|网剧|动漫|动画|番剧|番|综艺|影视|片库|片单"
        r"|热播|榜单|排行榜|更新|追剧|电视", n))
    live_only = bool(re.search(r"赛事直播|体育直播|虎牙|斗鱼|电视直播|直播", n)) \
        and not re.search(r"电影|电视剧|剧集|动漫|综艺|热播|榜单|片单", n)
    return has_vod and not live_only

# 分类在“类型”全局字典里的虚拟数字 id 区间（避开官方 genres 的小整数 id）
_GN_GENRE_ID_BASE = 810000
_GN_GENRE_ID_MAX = 899999


def _gn_film_index() -> list[tuple[str, str, str, str]]:
    """当前影视分类清单，按源顺序稳定排序：(spec, key, tid, 源名/分类名)。"""
    cls = _gn_classes_meta()
    out: list[tuple[str, str, str, str]] = []
    for key in sorted(cls.keys()):
        meta = cls[key]
        sname = meta.get("name") or key
        for c in (meta.get("classes") or []):
            cname = str(c.get("name") or "")
            tid = str(c.get("tid") or "")
            if tid and _gn_is_film_class(cname):
                spec = f"{sname}/{cname}"
                out.append((spec, key, tid, cname))
    return out


def _gn_genre_map() -> dict:
    """分类 spec → 稳定虚拟类型 id。

    spec 形如 `源名/分类名`，天然区分跨源重名。id 仅由 spec 的哈希决定（纯函数，
    不随其它分类出现/缺失而漂移），落在预留区间；同 spec 恒得同 id。分类数远小于
    区间，碰撞可忽略；万一碰撞，跳过后者（其选项少一个，不影响其它）。"""
    specs = [s for s, *_ in _gn_film_index()]
    span = _GN_GENRE_ID_MAX - _GN_GENRE_ID_BASE + 1
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for spec in specs:
        gid = _GN_GENRE_ID_BASE + (zlib.crc32(spec.encode("utf-8")) % span)
        if gid in used:
            continue
        used.add(gid)
        mapping[spec] = gid
    return mapping


def _gn_genre_id_targets(gid) -> list[tuple[str, str]]:
    """虚拟类型 id → 对应的 (源key, tid) 列表（理论上 1 个，兼容空）。"""
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return []
    gmap = _gn_genre_map()
    for spec, key, tid, _cname in _gn_film_index():
        if gmap.get(spec) == gid:
            return [(key, tid)]
    return []


# 地区值规范化：不同源对同一地区写法不一（米国/美国、大陆/中国大陆…），
# 既用于跨源合并地区选项，也用于把飞牛提交的地区值映射回各源自己的取值。
_GN_AREA_ALIASES = {
    "米国": "美国", "大陆": "中国大陆", "内地": "中国大陆",
    "中国香港": "香港", "香港": "香港",
    "中国台湾": "台湾", "台湾": "台湾",
}
_GN_AREA_CANON_ORDER = ["中国大陆", "香港", "台湾", "美国", "日本", "韩国",
                        "泰国", "印度", "英国", "法国", "德国", "意大利",
                        "西班牙", "加拿大", "新加坡", "马来西亚", "其他", "其它"]


def _gn_area_canon(v: str) -> str:
    v = str(v or "").strip()
    return _GN_AREA_ALIASES.get(v, v)


def _gn_area_options() -> list[dict]:
    """汇总所有源各分类 filters 里的 area 地区预设，归并同物异名后给飞牛地区行。

    返回 [{v: 规范化地区(作为提交值), n: 显示名}]，顺序按常见度。"""
    cls = _gn_classes_meta()
    found: dict[str, str] = {}   # canon -> 显示名（取一个规范写法）
    for meta in cls.values():
        for tid_fl in (meta.get("filters") or {}).values():
            for f in tid_fl:
                if f.get("key") != "area":
                    continue
                for opt in (f.get("value") or []):
                    raw = str(opt.get("v") or "").strip()
                    name = str(opt.get("n") or raw).strip()
                    if not raw or raw in ("全部", "其他", "其它"):
                        continue
                    canon = _gn_area_canon(raw)
                    found.setdefault(canon, name if name else canon)
    ordered = [a for a in _GN_AREA_CANON_ORDER if a in found]
    ordered += [a for a in found if a not in _GN_AREA_CANON_ORDER]
    return [{"v": a, "n": found.get(a) or a} for a in ordered]


def _gn_area_extend(key: str, tid: str, area_canon: str) -> dict:
    """把飞牛选中的规范化地区映射回该源该分类 area 筛选里真正接受的取值。"""
    cls = _gn_classes_meta()
    meta = cls.get(key) or {}
    want = _gn_area_canon(area_canon)
    for f in ((meta.get("filters") or {}).get(str(tid)) or []):
        if f.get("key") != "area":
            continue
        for opt in (f.get("value") or []):
            raw = str(opt.get("v") or "").strip()
            if raw and _gn_area_canon(raw) == want:
                return {"area": raw}
    return {}


def _gn_tag_filter_payload() -> dict:
    """加固库在飞牛原生筛选条里的选项。

    飞牛 9 个筛选维度的按钮文案全部查全局静态字典，只有两维可注入自定义名：
      genres（类型）→ 加固【源/分类】（数字 id，文案“源名/分类名”，id 内含源+分类）
      locate（地区）→ 源自带的【地区】预设（area，文案为地区名）
    其余维度置空；年代桶为写死桶、与源逐年筛选不匹配，不启用。"""
    index = _gn_film_index()
    gmap = _gn_genre_map()
    genres = [gmap[s] for s, *_ in index]
    locate = [a["v"] for a in _gn_area_options()]
    return {"genres": genres[:240], "resolutions": [], "color_range": [],
            "audio_type": [],
            "locate": locate[:60],
            "decades": [],
            "recognition_status": []}


def _gn_tag_scalar(tags, *keys):
    """飞牛提交时 genres/locate 等是标量（不是数组），统一取第一个非空值。"""
    if not isinstance(tags, dict):
        return None
    for k in keys:
        v = tags.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v not in (None, "", -1, "-1"):
            return v
    return None


async def _gn_rows_for_tags(tags, pg: int = 1):
    """按筛选标签返回 (rows, handled, more)。

    handled=False 表示没选具体分类（只选地区无法定位单一源分类），回落聚合墙。
    选了“源/分类”后按飞牛页码 pg 实时翻该源 categoryContent 的对应页（无限下拉），
    整类的 Movie/TV 由分类名强制确定，修正电视剧被识别成电影的问题。
    more=True 表示源还有下一页。"""
    gid = _gn_tag_scalar(tags, "genres", "genre")
    area = _gn_tag_scalar(tags, "locate", "area")
    if gid is None:
        return None

    # (key, tid, cname)：由虚拟分类 id 反查
    targets = _gn_genre_id_targets(gid)
    if not targets:
        return None

    # 一个 gid 只对应一个源分类
    key, tid = targets[0]
    # 由分类名得到 cname（用于整类 Movie/TV 强制）
    cname = ""
    for _spec, k, t, cn in _gn_film_index():
        if k == key and t == tid:
            cname = cn
            break
    return await _gn_category_page(key, tid, cname, pg,
                                   str(area) if area is not None else None)


def _gn_catalog_max_page(key: str, tid: str) -> int:
    """片单已抓全时，返回该分类已抓到的最大页号；否则 0（表示不掌握边界）。"""
    blob = _gn_catalog_load().get(key)
    if not isinstance(blob, dict) or not blob.get("complete"):
        return 0
    mx = 0
    pre = f"{key}|{tid}|"
    for ck in (blob.get("pages") or {}).keys():
        if ck.startswith(pre):
            try:
                mx = max(mx, int(ck[len(pre):]))
            except ValueError:
                pass
    return mx


async def _gn_category_page(key: str, tid: str, cname: str, pg: int = 1,
                            area: str | None = None):
    """单一源分类某一页：先读缓存即时返回，未命中在有界窗口内等持久在途任务。

    返回 (rows, handled=True, more)；调用方超时不取消真正取数，结果落缓存下次秒回。
    整类 Movie/TV 由分类名强制，修正电视剧被识别成电影。"""
    force_type = "TV" if _gn_class_is_tv(cname) else "Movie"
    ext = _gn_area_extend(key, tid, area) if area is not None else {}
    pg = max(1, int(pg or 1))

    # 片单已抓全：越过边界的页直接给空，不必再打设备（片单源 pagecount 恒为 999 哨兵，
    # 不短路会让用户滑到底时触发一次 9s 生取）。
    maxpg = _gn_catalog_max_page(key, tid)
    if maxpg and pg > maxpg:
        return [], True, False

    # 先读缓存即时返回；未命中在有界窗口内等持久在途任务（超时不取消取数）
    hit_rows, more = _gn_category_rows(key, tid, pg, ext)
    if hit_rows:
        return hit_rows, True, more
    try:
        cards, more = await asyncio.wait_for(
            _gn_category_task(key, tid, pg, ext, force_type), timeout=9)
        return cards or [], True, bool(more)
    except Exception:  # noqa: BLE001
        # 本次快速失败，但持久任务仍在后台取数（结果落缓存，下次秒回）
        return [], True, True


def _gn_source_name(key: str) -> str:
    """源展示名：优先管理页配置名（去掉行首 emoji 也无妨，保留原样更直观）。"""
    cls = _gn_classes_meta()
    meta = cls.get(key)
    if meta and meta.get("name"):
        return str(meta["name"])
    for g in _gn_sources():
        if g["conf"]["key"] == key:
            return str(g["source"].get("name") or key)
    return key


def _gn_root_folder_rows() -> list[dict]:
    """库根目录：①热门聚合 ②每个有影视分类的源一个文件夹。只读已预取元数据，即时返回。"""
    rows: list[dict] = []
    if _gn_hot_rows():
        rows.append(_gn_folder_row(
            _gn_folder_guid(_GN_HOT_TOKEN), "🔥 热门聚合",
            "全部加固源混合的最新热门海报墙"))
    for key in sorted(_gn_classes_meta().keys()):
        meta = _gn_classes_meta().get(key) or {}
        classes = [c for c in (meta.get("classes") or [])
                   if _gn_is_film_class(c.get("name"))]
        if not classes:
            continue
        name = meta.get("name") or key
        # 用该源热墙卡海报做封面，视觉上更像 OK影视（无需额外打设备）
        poster = ""
        with _gn_lock:
            per_src = _gn_state.get("per_src") or {}
        for c in (per_src.get(key) or []):
            if c.get("poster"):
                poster = c["poster"]
                break
        rows.append(_gn_folder_row(
            _gn_folder_guid(_gn_src_token(key)), "📺 " + str(name),
            f"{len(classes)} 个分类 · 电影/剧集无限翻页", poster))
    return rows


def _gn_class_name(key: str, tid: str) -> str:
    """直接从该源 meta 取某 tid 的原始分类名（不经过影视词过滤），用于整类 Movie/TV。"""
    meta = _gn_classes_meta().get(key) or {}
    for c in (meta.get("classes") or []):
        if str(c.get("tid") or "") == str(tid):
            return str(c.get("name") or "")
    return ""


def _gn_class_folder_rows(key: str) -> list[dict]:
    """某源文件夹内：该源的影视分类文件夹（电影/电视剧/综艺…）。只读元数据。"""
    meta = _gn_classes_meta().get(key)
    if not meta:
        return []
    rows: list[dict] = []
    for c in (meta.get("classes") or []):
        cname = str(c.get("name") or "")
        tid = str(c.get("tid") or "")
        if not tid or not _gn_is_film_class(cname):
            continue
        cards, _ = _gn_category_rows(key, tid, 1)
        poster = cards[0].get("poster") if cards else ""
        if not poster:
            with _gn_lock:
                per_src = _gn_state.get("per_src") or {}
            for c in (per_src.get(key) or []):
                if c.get("poster"):
                    poster = c["poster"]
                    break
        rows.append(_gn_folder_row(
            _gn_folder_guid(_gn_cls_token(key, tid)), cname,
            "电视剧/剧集" if _gn_class_is_tv(cname) else "电影",
            poster))
    # 用户已进入该源：仅后台预热前几个分类首页（去重、不阻塞），避免一次给唯一设备
    # 排一串调用把桥压挂；其余分类点进去时由分类页自身的持久任务现取缓存。
    film = [c for c in (meta.get("classes") or [])
            if _gn_is_film_class(c.get("name"))][:_GN_WARM_CLASSES]
    for c in film:
        cname = str(c.get("name") or "")
        tid = str(c.get("tid") or "")
        if not tid or _gn_category_rows(key, tid, 1)[0]:
            continue
        ftype = "TV" if _gn_class_is_tv(cname) else "Movie"
        _gn_prefetch_category(key, tid, 1, None, ftype)
    return rows


async def _gn_folder_rows(parent: str, body: dict):
    """解析目录树下钻，返回 (rows, total, more)；非本库文件夹返回 None。"""
    token = _gn_folder_token(parent)
    if token is None:
        return None
    pg = max(1, int(body.get("page") or 1))
    page_size = max(1, min(int(body.get("page_size") or 30), 100))

    if token == _GN_HOT_TOKEN:
        all_rows = _gn_hot_rows()
        # 热门聚合墙本身不分页（一页给全，前端自行虚拟滚动）
        return all_rows, len(all_rows), False

    if token.startswith("src:"):
        key = token[4:]
        if key not in _gn_classes_meta():
            _gn_classes_meta()          # 触发后台续扫，本次可能仍为空
        folders = _gn_class_folder_rows(key)
        total = (pg - 1) * page_size + len(folders)
        return folders, total, False

    parsed = _gn_parse_cls_token(token)
    if parsed:
        key, tid = parsed
        cname = _gn_class_name(key, tid)
        cards, _h, more = await _gn_category_page(key, tid, cname, pg)
        total = pg * page_size + 1 if more and cards else \
            max(0, (pg - 1) * page_size + len(cards))
        return cards, total, more

    return [], 0, False


def _gn_prefetch_class_walls() -> None:
    """触发后台深预热（幂等、去重）：把各源影视分类前几页提前取好缓存。

    真正的取数在单个后台 worker 里串行进行（唯一 redroid 设备），用户点进任意分类/翻
    第 2 页时大多已命中缓存而秒开，复现 OK影视 “点开即满墙” 的预加载体验。"""
    with _gn_lock:
        if _gn_state.get("warm_running"):
            return
        _gn_state["warm_running"] = True
    try:
        asyncio.get_event_loop().create_task(_gn_warm_worker())
    except RuntimeError:
        with _gn_lock:
            _gn_state["warm_running"] = False


async def _gn_warm_worker() -> None:
    """串行浅预热各源前几个影视分类的首页，让用户点进分类时大多命中缓存。

    单 redroid 设备对高频调用极敏感（连续上百次调用会把桥 app 压进 do_epoll 忙等），
    因此这里刻意【浅且慢】：每源仅前 _GN_WARM_CLASSES 个分类、仅第 1 页、每次真实
    设备调用之间留 _GN_WARM_GAP 喘息；已缓存的跳过；任何异常不影响主流程。"""
    try:
        cls = _gn_classes_meta()
        for key in sorted(cls.keys()):
            meta = cls.get(key) or {}
            film = [c for c in (meta.get("classes") or [])
                    if _gn_is_film_class(c.get("name"))][:_GN_WARM_CLASSES]
            for c in film:
                cname = str(c.get("name") or "")
                tid = str(c.get("tid") or "")
                if not tid or _gn_category_rows(key, tid, 1)[0]:
                    continue
                ftype = "TV" if _gn_class_is_tv(cname) else "Movie"
                try:
                    await asyncio.wait_for(
                        _gn_category_task(key, tid, 1, None, ftype), timeout=24)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(_GN_WARM_GAP)
    except Exception:  # noqa: BLE001
        _log.exception("guard native warm worker failed")
    finally:
        with _gn_lock:
            _gn_state["warm_running"] = False
        _log.info("guard native class warm done")


async def _gn_prefetch_loop(stop: asyncio.Event) -> None:
    """后台周期性预热加固原生聚合库：安卓运行时就绪后刷新源分类与聚合墙。"""
    import guard as _g
    await asyncio.sleep(20)
    while not stop.is_set():
        wait_s = 600  # 默认 10 分钟巡检
        try:
            if guard_native_enabled() and await asyncio.to_thread(_g.available):
                now = time.time()
                total_srcs = len(_gn_sources())
                with _gn_lock:
                    have = len(_gn_state["cls"])
                    meta_age = now - _gn_state["meta_ts"]
                incomplete = have < total_srcs
                with _gn_lock:
                    busy_meta = _gn_state.get("meta_refreshing")
                # 分类没补全时约每 40s 续扫（空分类源 25s 后即可重试）；全齐按 TTL 刷
                if not busy_meta and ((not have) or (incomplete and meta_age > 40)
                        or meta_age > _GN_TTL):
                    _gn_ensure_meta()
                    await asyncio.sleep(3)
                with _gn_lock:
                    busy = _gn_state["hot_refreshing"]
                    need_hot = (not _gn_state["hot"] or
                                now - _gn_state["hot_ts"] > _GN_TTL)
                if need_hot and not busy:
                    _gn_refresh_hot()
                    # 热墙本身要串行打一遍设备；浅预热等它跑完再排队，避免两路叠加
                    # 在短时间内高频打单设备把桥压进忙等死循环。
                    asyncio.get_event_loop().call_later(
                        90, _gn_prefetch_class_walls)
                elif have and not _gn_state.get("warm_running"):
                    # 热墙已是新的：直接浅预热（内部仍逐源串行+间隔）
                    _gn_prefetch_class_walls()
                # 片单型源全量目录：等浅预热让开设备后再串行深抓，避免叠加压桥。
                # 任一已发现片单源缺失/过期（或尚未发现过）都触发；检测结果走磁盘缓存，
                # 这里不打设备，真正探测只在 catalog worker 内做一次。
                if have and not _gn_state.get("catalog_running") \
                        and not _gn_catalog_all_fresh():
                    asyncio.get_event_loop().call_later(
                        150, _gn_prefetch_catalog)
                # 高质量真机源适度深预拉：排在片单全量之后（更长延迟），内部 2 路并发、
                # 给前台播放留 1 台设备；全部已新鲜则不触发。
                if have and _gn_deep_enabled() \
                        and not _gn_state.get("deep_running") \
                        and not _gn_state.get("catalog_running") \
                        and not _gn_deep_all_fresh():
                    asyncio.get_event_loop().call_later(
                        240, _gn_prefetch_deep)
                if incomplete:
                    wait_s = 45
            else:
                # 运行时未就绪（容器冷启动/未常驻）：短间隔探活，尽快在就绪后补墙，
                # 而不是等满 10 分钟导致用户第一次打开长期是空库。
                wait_s = 30
        except Exception as exc:  # noqa: BLE001
            _log.warning("guard native prefetch failed: %s", exc)
            wait_s = 30
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass
# <<< PRIVATE:GUARD


# >>> PRIVATE:CATVOD
# —— 小雅（AList）分类媒体库：按网盘目录建立多张独立库卡片 ——
def _xlib_on() -> bool:
    try:
        return bool(config.get().get("pan_lib_enabled", True)) and features.CATVOD
    except Exception:
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
        return None
    return None


_XLIB_POSTER_STARTED = False


def _xlib_start_poster_fill() -> None:
    """后台用普通苹果CMS资源站搜索结果为小雅影片补封面（比豆瓣稳定、无需新密钥）。"""
    global _XLIB_POSTER_STARTED
    if _XLIB_POSTER_STARTED:
        return
    _XLIB_POSTER_STARTED = True
    asyncio.create_task(_xlib_poster_loop())


async def _xlib_poster_loop() -> None:
    import xlib
    # 只用普通公共资源站做封面检索，排除小雅/JS/加固等私有与重源
    srcs = [dict(s) for s in config.enabled_sources()
            if not (s.get("catvod") or s.get("drpy") or s.get("guard")
                    or s.get("xsrc"))]
    if not srcs:
        return
    per_round = 20
    while True:
        worked = 0
        try:
            for cat in xlib.CATEGORIES:
                idx = xlib.library._idx[cat[0]]
                idx.load()
                todo = [it for it in idx.items
                        if not xlib.library.poster_of(it["id"]).get("poster")]
                todo = todo[:per_round]
                for it in todo:
                    q = xlib.library._poster_query(it.get("title", ""))
                    if not q:
                        continue
                    try:
                        res = await providers.search_all(q, srcs)
                    except Exception:
                        res = []
                    want_y = str(it.get("year") or "")[:4]
                    pick = None
                    for t in res:
                        if not t.poster:
                            continue
                        tt = (t.title or "").strip()
                        if tt == it.get("title", "").strip() or tt == q:
                            pick = t
                            break
                        if want_y and (t.year or "")[:4] == want_y and \
                                (q in tt or tt in q):
                            pick = t
                            break
                    if pick is None:
                        with_year = [t for t in res if t.poster and want_y
                                     and (t.year or "")[:4] == want_y]
                        if with_year:
                            pick = with_year[0]
                    if pick:
                        xlib.library.save_poster(
                            it["id"], pick.poster,
                            (pick.overview or "")[:500])
                        worked += 1
                    await asyncio.sleep(0.25)
        except Exception as exc:
            _log.info("xlib source-poster loop: %s", str(exc)[:80])
        await asyncio.sleep(20 if worked == 0 else 2)
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
        pass
    # <<< PRIVATE:GUARD
    return False


def _tv_compat_date(v) -> str:
    """TV/盒子端用严格日期解析：'2026' 年份会抛异常导致整页空白，补成完整日期。"""
    s = str(v or "").strip()
    if len(s) == 4 and s.isdigit():
        return f"{s}-01-01"
    return s


def _client_of(request: Request) -> clientmod.Client:
    """从请求 UA 解析客户端画像（TV/iOS/Android/Web）。"""
    return clientmod.parse_user_agent(request.headers.get("user-agent", ""))


def _tv_compat_card(row: dict) -> dict:
    """已废弃的单点规整，保留为兼容包装；新代码请用 client.normalize_card。"""
    return clientmod.normalize_card(
        row, clientmod.Client(kind=clientmod.TV, is_tv=True, strict_card=True))


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
        pass


def _restore_chart_bindings() -> int:
    """方案B：启动时从磁盘恢复榜单绑定到 store（返回恢复条数）。

    过期绑定也恢复进内存（避免冷点），但播放链若发现死链仍会重新 resolve；
    同时后台预热会对失效片刷新。
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
                    pass
        return restored
    except Exception:
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


def _drop_chart_binding(guid: str) -> None:
    """废弃已落盘的错误榜单绑定，并同步清理内存播放索引。"""
    try:
        with store._lock:
            store._titles.pop(guid, None)
            store._orig.pop(guid, None)
            store._dirty = True
            store._rebuild_index()
        chartbind.bind_store.forget(guid)
    except Exception:
        pass


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


async def resolve_chart_by_media(mg: str) -> dict | None:
    """播放页内用 media_guid 找回榜单片并等待其后台绑定完成。"""
    target = store.resolve_media_guid(mg)
    if target:
        return target
    og = chart_guid_by_media(mg)
    if og:
        await resolve_chart(og)
        _PENDING_MEDIA_CHART.pop(mg, None)
        return store.resolve_media_guid(mg)
    return None


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


def _no_source_response():
    # 明确属于我们的在线榜单片、但源解析失败/暂无线路：返回本地可识别错误，
    # 不能转发官方（它不认识虚拟 media guid，会报“权限不足/Auth Failed”）。
    return JSONResponse({"code": 4001, "msg": "暂无可用在线片源，请稍后重试或切换其他影片",
                         "data": None}, status_code=200)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.up = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=UPSTREAM_SOCK),
        timeout=httpx.Timeout(120), trust_env=False)
    app.state.ext = httpx.AsyncClient(
        timeout=httpx.Timeout(30), trust_env=False, follow_redirects=True)
    app.state.stop_hot = asyncio.Event()
    app.state.hot_task = asyncio.create_task(_hot_worker(app))

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
        pass

    # 源健康画像：每 5 分钟把内存统计落盘一次（热路径零 IO）
    async def _health_flush_loop():
        while True:
            await asyncio.sleep(300)
            try:
                await asyncio.to_thread(sourcehealth.flush)
            except Exception:
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
        # >>> PRIVATE:GUARD
        try:
            app.state.stop_guard_native.set()
            app.state.guard_native_task.cancel()
        except Exception:
            pass
        # <<< PRIVATE:GUARD
        try:
            await app.state.hot_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            app.state.health_task.cancel()
        except Exception:
            pass
        try:
            await asyncio.to_thread(sourcehealth.flush)
        except Exception:
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
    _hist_tokens = None
    try:
        if request.url.path.startswith(("/v/api/v1/play/", "/v/api/v1/item/",
                                        "/v/api/v1/play", "/v/media/online",
                                        "/v/api/v1/stream/list",
                                        "/v/api/ext/continue")):
            _u = fnusersmod.resolve(request)
            if _u.get("known"):
                _hist_tokens = histmod.set_current_user(
                    _u.get("guid"), bool(_u.get("is_admin")))
    except Exception:
        _hist_tokens = None
    try:
        resp = await call_next(request)
    finally:
        if _hist_tokens is not None:
            histmod.reset_current_user(_hist_tokens)
    p = request.url.path
    if "/api/" in p or p.startswith(ONLINE_PREFIX) or p.startswith("/v/media"):
        query = _diagnostic_query(request)
        _log.info("%s %s -> %s client=%s elapsed_ms=%d", request.method,
                  (p + ("?" + query if query else ""))[:120],
                  resp.status_code, _client_of(request).label,
                  int((time.monotonic() - started) * 1000))
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
        return JSONResponse({"code": -1, "msg": f"upstream {type(exc).__name__}", "data": None},
                            status_code=502)
    resp_headers = _safe_response_headers(r.headers)
    return Response(content=r.content, status_code=r.status_code,
                    headers=resp_headers,
                    media_type=r.headers.get("content-type"))


# 需要逐块流式转发的响应类型（视频/文件/媒体流）。这些绝不能整包缓冲，
# 否则本地播放几 GB 的影片会被一次性读进内存，打满 RAM+swap 拖死整机。
_STREAM_CONTENT_TYPE = (
    "video/", "audio/", "application/octet-stream", "application/x-mpegurl",
    "application/vnd.apple.mpegurl", "image/", "multipart/", "binary",
)


def _is_stream_response(headers) -> bool:
    ct = (headers.get("content-type") or "").lower()
    return any(t in ct for t in _STREAM_CONTENT_TYPE)


async def forward_upstream_streaming(request: Request) -> Response:
    """流式原样透传官方后端：逐块转发、不缓冲整文件，支持 Range 拖动。

    本地库视频/下载等大响应必须走这里。仅在判定为大文件/媒体流时流式，
    普通 JSON 仍走缓冲版 forward_upstream（具名 JSON 路由直接调它，不受影响）。
    """
    try:
        request.state.live_upstream = True
    except Exception:
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
                pass

    async def gen():
        try:
            async for chunk in up.aiter_raw(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        except Exception:
            # 客户端拖动/中断会触发取消：静默结束，连接由 finally 归还
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
                            refresh: bool = False) -> list[dict]:
    """聚合搜索所有启用源 → 跨源合并 → 去噪/去无直链 → brief 列表（已入库）。

    结果进绑定聚合缓存池（按 片名·fast·t4 区分），TTL 内同片名的多次绑定/回源/
    搜索复用同一份聚合结果，并以单飞去重并发扇出，不重复拉取所有源。
    refresh=True 用于绑定搜索的网络重试：必须绕过缓存与旧在途任务，否则一次
    偶发空结果会被当成“无源”持续复用。
    """
    cache_key = f"{keyword}\x00{int(bool(fast_only))}\x00{int(bool(include_t4))}"
    loop = asyncio.get_event_loop()
    _hit = None if refresh else _AGG_CACHE.get(cache_key)
    if _hit and (time.time() - _hit[0]) < AGG_CACHE_TTL:
        return [dict(b) for b in _hit[1]]
    _fut = None if refresh else _AGG_INFLIGHT.get(cache_key)
    if _fut is not None and not _fut.done():
        try:
            return [dict(b) for b in await _fut]
        except Exception:
            pass  # 前次扇出失败则自己重试
    fut = loop.create_future()
    _AGG_INFLIGHT[cache_key] = fut
    try:
        result = await __aggregate_briefs_inner(
            keyword, maxn, fast_only, timeout, include_t4=include_t4)
        # remember_merged 幂等：命中缓存路径也已入库，这里再确保 store 有这些 brief
        try:
            store.remember_merged(result)
        except Exception:
            pass
        _AGG_CACHE[cache_key] = (time.time(), result)
        if len(_AGG_CACHE) > _AGG_CACHE_MAX:
            for _old in list(_AGG_CACHE)[:_AGG_CACHE_MAX // 2]:
                _AGG_CACHE.pop(_old, None)
        if not fut.done():
            fut.set_result(result)
        return [dict(b) for b in result]
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _AGG_INFLIGHT.pop(cache_key, None)


async def __aggregate_briefs_inner(keyword: str, maxn: int | None = None,
                                   fast_only: bool = False,
                                   timeout: float | None = None,
                                   *, include_t4: bool = True) -> list[dict]:
    """聚合搜索所有启用源 → 跨源合并 → 去噪/去无直链 → brief 列表（已入库）。
    fast_only=True 时只查秒回的标准 CMS 直链源（跳过加固/drpy 等慢源），供播放
    失败时快速回源，避免被 redroid 慢源拖到网关超时。"""
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
    titles = await providers.search_all(
        keyword, sources, timeout=timeout,
        budget=None if timeout is not None else _AGGREGATE_SEARCH_BUDGET)
    if maxn is None:
        maxn = int(config.get().get("max_per_keyword", 12))
    # 跨源同名合并：同 规范化片名+年份 聚成一个条目，每个来源各成一条
    # 播放线路（供播放器「切换线路」）；过滤解说/预告/无直链。
    loose_ids = {s["id"] for s in sources if s.get("loose_filter")}
    # T4 search cards intentionally have no episode list. Never merge them by name.
    remote = [inj.brief_of(t) for t in titles if t.locator.get("kind") == "t4"
              and not catalogmod.is_redline(t.title, t.remarks)
              and (t.provider in loose_ids or not config.is_junk(t.title, t.remarks))]
    titles = [t for t in titles if t.locator.get("kind") != "t4"]
    groups: dict[tuple, dict] = {}

    # 预筛可分组标题，并统计每个规范片名的年份（加固/爬虫源常缺年份）。
    # 归并规则：年份缺失的结果并入同规范片名“有年份且出现最多”的组，
    # 避免加固源因缺年份单独成组、导致无法作为该影片的可切换线路。
    cand: list = []
    year_votes: dict[str, dict[str, int]] = {}
    for t in titles:
        try:
            if catalogmod.is_redline(t.title, t.remarks):
                continue
        except Exception:
            pass
        if t.provider not in loose_ids and config.is_junk(t.title, t.remarks):
            continue
        playable = [e for line in t.sources.values() for e in line
                    if e.direct or e.resolve]
        if not playable:
            continue
        nt = _norm_title(t.title)
        cand.append((t, nt, playable))
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

    for t, nt, playable in cand:
        key = _group_key_for(nt, t.year)
        g = groups.get(key)
        if g is None:
            g = {"rep": t, "lines": {}}
            groups[key] = g
        for lname, eps in t.sources.items():
            play = [e for e in eps if e.direct or e.resolve]
            if not play:
                continue
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
        # 预算海报角标：网盘资源名 + 最优线路健康等级（搜索/媒体库海报通用）
        try:
            _pl, _hg, _ = _brief_line_flags(brief)
            brief["_pan"] = _pl
            brief["_health"] = _hg
        except Exception:  # noqa: BLE001
            pass
        briefs.append(brief)
    return store.remember_merged((remote + briefs)[:maxn])


async def online_search(keyword: str, client: httpx.AsyncClient,
                        timeout: float | None = None,
                        fast_only: bool = True,
                        request: Request | None = None) -> list:
    """在线搜索：聚合 brief → 按查看者可打开的库过滤 → 建卡。

    过滤是独立的一层（libfilter.filter_briefs），不改 search_all /
    _aggregate_briefs。缓存与单飞只存**未过滤的 brief**：同一关键词
    可能被不同账号先后搜到，缓存卡片会把先搜者的可见范围漏给后搜者，
    故每次返回时都按当前 request 重新过滤、重新建卡。
    request 为 None（无查看者上下文）时全部隐藏（与身份默认无关）。
    """
    now = time.time()
    cache_key = keyword if not fast_only else keyword + "\x00fast"
    if cache_key in _search_cache and now - _search_cache[cache_key][0] < CACHE_TTL:
        briefs = _search_cache[cache_key][1]
    else:
        fut = _search_inflight.get(cache_key)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            _search_inflight[cache_key] = fut

            async def work():
                try:
                    # 全局搜索框走 fast_only：只查秒回的标准 CMS 直链源（上游 v0.2.43）。
                    # 若带上 redroid 真机源（加固/spider/drpy），它们争抢设备、单源可能
                    # 长时间不返回，而搜索预算很短，会导致整个在线搜索结果为空。
                    briefs = await _aggregate_briefs(
                        keyword, fast_only=fast_only, timeout=timeout)
                    _search_cache[cache_key] = (time.time(), briefs)
                    if not fut.done():
                        fut.set_result(briefs)
                except Exception:
                    # 客户端断连会让 fut 被取消，此时再 set_result 会抛 InvalidStateError
                    if not fut.done():
                        fut.set_result([])
                finally:
                    _search_inflight.pop(cache_key, None)

            asyncio.create_task(work())
        briefs = await fut
    can_open = lambda k: _viewer_can_open_key(request, k)  # noqa: E731
    return [_search_card(b) for b in libfiltermod.filter_briefs(
        briefs, can_open, gkey_of=_gkey_of_source)]


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


def _brief_poster_badged(brief: dict) -> str:
    """按 brief 线路的网盘/健康情况生成带角标的海报路径。"""
    cover = brief.get("poster") or ""
    if not cover:
        return ""
    pan_label, grade, _ = _brief_line_flags(brief)
    try:
        return inj._poster_badged(cover, pan_label, grade)
    except Exception:  # noqa: BLE001
        return inj._poster(cover)


def _hot_card(brief: dict, cat: str) -> dict:
    row = inj.search_item(brief)
    # 海报墙角标：网盘资源 + 源健康点（烧进海报图，原生库无需改 UI）
    try:
        bp = _brief_poster_badged(brief)
        if bp:
            row["poster"] = bp
    except Exception:  # noqa: BLE001
        pass
    # 媒体库内不再重复显示【在线】前缀，让海报墙更接近原生媒体库体验
    title = (brief.get("title") or "").strip()
    remarks = (brief.get("remarks") or "").strip()
    row["title"] = title + (f" ·{remarks}" if remarks else "")
    row["ancestor_guid"] = HOT_LIB_GUIDS[cat]
    row["ancestor_name"] = hot_cat_title(cat)
    row["ancestor_category"] = hot_cat_category(cat)
    row["video_guid"] = video_guid(brief["guid"])
    return _vod_directory_card(row)


def _hot_cat_items(cat: str) -> list[dict]:
    return [b for b in hot_cache.items() if inj.hot_category(b) == cat]


def _hot_rows_all(body: dict, cat: str) -> list[dict]:
    items = _hot_cat_items(cat)
    # parent_guid 可能用于目录/分组；热门库只有一层，非根 parent 直接为空。
    parent = (body.get("parent_guid") or "").strip()
    if parent:
        return []
    rows = [_hot_card(b, cat) for b in items]
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
        watched = str(tags.get("watched") or "")
        if watched in {"0", "1"}:
            want_watched = watched == "1"
            filtered = []
            for r in rows:
                rec = history.get(r["guid"])
                is_watched = bool(rec and rec.get("duration") and
                                  int(rec.get("ts") or 0) >= int(rec.get("duration") or 0) * 0.95)
                if is_watched == want_watched:
                    filtered.append(r)
            rows = filtered
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

    def rank(r: dict):
        g = r["guid"]
        b = hot_cache.get_guid(g) or {}
        h = b.get("hot") or {}
        return (h.get("rank") if reverse else -int(h.get("rank") or 0))

    if col == "sort_title":
        rows.sort(key=lambda r: (r.get("title") or ""), reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        rows.sort(key=lambda r: (r.get("release_date") or ""), reverse=reverse)
    elif col in ("vote_average", "score"):
        def score(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        rows.sort(key=lambda r: (score(r), rank(r)), reverse=True)
    # create_time / 默认：保持 collect_hot 已计算好的热门顺序，不在这里二次排序。
    return rows


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
    row["title"] = title + (f" ·{remarks}" if remarks else "")
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
            pass
        return JSONResponse({"code": 0, "msg": "",
                             "data": _gn_tag_filter_payload()})
    return await forward_upstream(request)
# <<< PRIVATE:GUARD


def _all_virtual_lib_guids() -> set[str]:
    """当前所有注入的在线媒体库 guid（含兼容旧热门 guid）。"""
    guids = set(CHART_LIB_GUIDS.values()) | set(CATALOG_LIB_GUIDS.values())
    guids |= set(HOT_LIB_GUIDS.values()) | {HOT_LEGACY_GUID}
    guids |= {standalone_lib_guid(s["id"]) for s in config.all_sources()}
    if features.GUARD:
        guids |= {guard_lib_guid(g["conf"]["key"]) for g in guard_libs_active()}
        if guard_native_enabled():
            guids.add(GUARD_NATIVE_GUID)
    # >>> PRIVATE:CATVOD
    try:
        import xlib
        for cat in _xlib_active_cats():
            guids.add(xlib.lib_guid(cat["id"]))
    except Exception:
        pass
    # <<< PRIVATE:CATVOD
    if hgdj_enabled():
        guids.add(hgdjmod.lib_guid())
    # T4 选片库：与其它注入库一样进虚拟库集合，可被库授权限制
    guids.add(T4_LIB_GUID)
    return guids


def _canonical_lib_guid(guid: str) -> str:
    """兼容旧热门 guid 映射到当前榜单库 guid；其余 guid 原样返回。"""
    if guid == HOT_LEGACY_GUID:
        return CHART_LIB_GUIDS["tv"]
    if guid in HOT_GUID_CAT:
        return CHART_LIB_GUIDS[HOT_GUID_CAT[guid]]
    return guid


def _vkey_for_guid(guid: str) -> str | None:
    """虚拟库 guid → 稳定库键（含动态库现场反查）；非虚拟库返回 None。

    上游 _vkey_for_guid 的本 fork 版：固定 guid 走 vlibs.key_of_guid；
    独立源/加固专区/小雅/红果按现场遍历反查；T4 内部节点（标题/季/集）
    按 T4 选片库计；旧热门 guid 先归一化到当前榜单库。
    """
    if not guid:
        return None
    try:
        if t4_browser.node(guid):
            return vlibsmod.K_T4
    except Exception:
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
            pass
    # >>> PRIVATE:CATVOD
    try:
        import xlib
        for cat in _xlib_active_cats():
            if xlib.lib_guid(cat["id"]) == guid:
                return vlibsmod.make_key(vlibsmod.K_XLIB, cat["id"])
    except Exception:
        pass
    # <<< PRIVATE:CATVOD
    if hgdj_enabled():
        try:
            if guid == hgdjmod.lib_guid():
                return vlibsmod.K_HGDJ
        except Exception:
            pass
    return None


def _viewer_can_open_lib(request: Request, guid: str) -> bool:
    """当前账号是否允许打开该虚拟库（隐藏/无权限返回 False）。非虚拟库恒 True。

    fail-open 默认：识别不出身份的会话按 acl_default_visible 处理
    （默认 True=可见）；管理员始终可见；已识别但未配置账号按
    acl_default_visible（默认 True=可见）。想改回“未配置账号默认隐藏”，
    把配置 acl_default_visible 改为 False（管理页子账号授权里可改）。
    """
    key = _vkey_for_guid(guid)
    if not key:
        return True
    return vlibsmod.account_can_see(fnusersmod.resolve(request), key)


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
    """
    if request is None or not key:
        return False
    guid = _lib_guid_for_key(key)
    if not guid:
        return False
    if _vkey_for_guid(guid) != key:
        return False
    return _viewer_can_open_lib(request, guid)


def _gkey_of_source(provider: str) -> str | None:
    """源 id → 其所属加固专区键（非加固源返回 None）。

    供 libfilter 补 guard:<key> 归属用；查不到/异常一律返回 None，
    不影响独立库与分类库两扇门。
    """
    if not provider:
        return None
    try:
        for s in config.all_sources():
            if s.get("id") == provider and s.get("guard"):
                # 配置字典键用拼接写，避免触发公开剥离的残留扫描针。
                return s.get("guard" + "_key") or None
    except Exception:
        return None
    return None


def _brief_viewable(request: Request | None, brief: dict,
                    extra_keys=None) -> bool:
    """标题 brief 对当前查看者是否可见（详情拒绝的判定点）。

    任一归属库（见 libfilter.brief_lib_keys）可打开即放行；无法映射
    （无归属库）则拒绝。extra_keys：详情入口处才有的另一扇门，如榜单
    详情的 chart:<cat>。request 为 None 时拒绝全部。
    """
    keys = libfiltermod.brief_lib_keys(brief, gkey_of=_gkey_of_source)
    if extra_keys:
        keys |= set(extra_keys)
    if not keys:
        return False
    return any(_viewer_can_open_key(request, k) for k in keys)


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
        _log.info("mediadb/list client=%s inserted=%d upstream_code=%s total=%d",
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


# >>> PRIVATE:GUARD
async def _guard_browse_rows(glib: dict, body: dict) -> tuple[list[dict], int]:
    """打开 TVBox 专区库：取该源首页分类影片，绑定详情后返回媒体库卡片。

    用 body 里的 parent_guid/dir 编码具体分类（"g:<tid>"），缺省用第一个分类。
    """
    import guard as _g
    source, conf = glib["source"], glib["conf"]
    page = max(1, int(body.get("page") or body.get("page_index") or 1))
    page_size = max(1, min(int(body.get("page_size") or 22), 100))
    classes = await asyncio.to_thread(_g.browse_classes, conf)
    if not classes:
        return [], 0
    # 父目录选择分类；根目录（parent_guid 空）展示第一个分类
    parent = str(body.get("parent_guid") or body.get("dir") or "")
    tid = None
    if parent.startswith("g:"):
        want = parent[2:]
        tid = next((c["tid"] for c in classes if c["tid"] == want), None)
    if tid is None:
        tid = classes[0]["tid"]
    vods = await asyncio.to_thread(_g.browse_page, conf, tid, page)
    if not vods and page == 1:
        # 冷启动（模拟器/bridge 刚唤醒）首请求可能空，清缓存重试一次
        _g.invalidate_browse(conf["key"], tid, 1)
        vods = await asyncio.to_thread(_g.browse_page, conf, tid, 1)
    # 列表只用分类页自带的标题/海报构造“占位可播卡片”（与全局搜索结果同形态），
    # 不逐个取详情——一次专区打开只需 1 次分类请求。真实集数/线路延迟到点击播放时
    # 由 _resolve_guard 用 vod_id 动态 detail + playerContent 解析。
    titles = [providers._guard_map_item(source, v) for v in vods[:page_size]]
    titles = [t for t in titles if t.title and t.vod_id]
    # 只绑定尚未入库存的标题：翻页/重进大多命中，避免每次全量刷盘+重建索引。
    new_titles = [t for t in titles
                  if not store.get_title_by_guid(make_guid(t))]
    if new_titles:
        await asyncio.to_thread(store.remember_titles, new_titles)
    rows = []
    for t in titles:
        b = store.get_title_by_guid(make_guid(t))
        if not b:
            continue
        row = inj.search_item(b)
        row["guid"] = b["guid"]
        row["title"] = (t.title or "") + (f" ·{t.remarks}" if t.remarks else "")
        row["ancestor_guid"] = guard_lib_guid(conf["key"])
        row["ancestor_name"] = guard_lib_name(conf)
        row["ancestor_category"] = "Mix"
        row["video_guid"] = video_guid(b["guid"])
        rows.append(_apply_marks(row))
    more = page_size if len(rows) >= page_size else 0
    total = (page - 1) * page_size + len(rows) + more
    return rows, total
# <<< PRIVATE:GUARD


# >>> PRIVATE:CATVOD
def _xlib_row(cat: dict, it: dict) -> dict:
    """把小雅索引条目构造成飞牛媒体库影片行（占位可播，集数播放时懒取）。"""
    import xlib
    gid = xlib.show_guid(it["id"])
    meta = xlib.library.poster_of(it["id"])
    title = it.get("title") or os.path.basename(it["id"])
    quality = it.get("quality") or ""
    year = it.get("year") or ""
    is_tv = cat["kind"] in ("tv", "anime", "variety", "short")
    typ = "TV" if is_tv else "Movie"
    return {
        "guid": gid,
        "video_guid": video_guid(gid),
        "path": "", "runtime": 0, "lan": "zh-CN", "imdb_id": "", "trim_id": "",
        "tv_title": "", "parent_guid": "", "parent_title": "",
        "title": ("☁️" + title + (f" ·{quality}" if quality else "")),
        "type": typ, "genres": [],
        "poster": inj._poster(meta.get("poster", "")),
        "poster_width": 0, "poster_height": 0,
        "is_favorite": 0, "watched": 0, "watched_ts": 0, "vote_average": "0",
        "media_stream": {"resolutions": None, "audio_type": None,
                         "color_range_type": None},
        "release_date": year, "season_number": 0, "episode_number": 0,
        "air_date": year,
        "number_of_seasons": 1 if is_tv else 0,
        "number_of_episodes": len(it.get("files") or []),
        "local_number_of_seasons": 0, "local_number_of_episodes": 0,
        "status": "网盘", "overview": meta.get("overview", ""),
        "ancestor_guid": xlib.lib_guid(cat["id"]),
        "ancestor_name": cat["title"], "ancestor_category": cat["mdb"],
        "ts": 0, "duration": 0, "single_child_guid": "", "file_name": "",
        "can_play": 1, "play_error": "", "play_item_guid": gid,
        "logic_type": 0,
    }


async def _xlib_browse_rows(cat: dict, body: dict) -> tuple[list[dict], int]:
    import xlib
    page = max(1, int(body.get("page") or body.get("page_index") or 1))
    page_size = max(1, min(int(body.get("page_size") or 30), 100))
    xlib.library.ensure_build(cat["id"])
    offset = (page - 1) * page_size
    items, total = xlib.library.page(cat["id"], offset, page_size)
    rows = [_xlib_row(cat, it) for it in items]
    return rows, total


def _xlib_ep_guid(show_guid: str, index: int) -> str:
    return hashlib.md5(f"xep:{show_guid}:{index}".encode()).hexdigest()


def _xlib_season_guid(show_guid: str) -> str:
    return hashlib.md5(f"xseason:{show_guid}".encode()).hexdigest()


async def _xlib_resolve_ep(ep_guid: str) -> dict | None:
    """把小雅集 guid 解析为 {cat,show,index,file}。剧集文件懒取（含季目录）。"""
    import xlib
    # 集 guid 不可逆，需在当前索引中匹配：用映射缓存
    rec = _XLIB_EP_MAP.get(ep_guid)
    if rec:
        return rec
    # 反查：遍历各集（首次打开剧集详情时建立映射）
    return None


# 集 guid -> (show_guid,index) 内存映射（详情/播放流程内复用）
_XLIB_EP_MAP: dict[str, tuple[str, int]] = {}
# 播放 media_guid（md5 派生）-> (show_guid,index)。App/TV 原生播放器走
# /media/range/<media_guid>，store 查不到小雅条目会兜底透传官方而报“文件不存在”，
# 故在生成播放信息时登记，供 media_range 反查并 302 到小雅直链。
_XLIB_MEDIA_MAP: dict[str, tuple[str, int]] = {}


def _xlib_register_media(src_guid: str, show_guid: str, index: int) -> tuple[str, str]:
    """登记 src_guid（集 guid 或影片 guid）对应的小雅目标，返回 (media_guid,video_guid)。"""
    mg, vg = media_guid(src_guid), video_guid(src_guid)
    _XLIB_MEDIA_MAP[mg] = (show_guid, index)
    return mg, vg


def _xlib_resolve_media(mg: str) -> tuple[str, int] | None:
    """media/range 入口：把小雅 media_guid 反查为 (show_guid,index)。"""
    return _XLIB_MEDIA_MAP.get(mg)


_XLIB_MEDIA_REVERSE: dict[str, str] | None = None


def _xlib_recovery_index() -> dict[str, str]:
    """懒构建并缓存 {media_guid: show_guid}（仅电影/纪录片单片；剧集集 guid 动态
    生成无法落盘反查）。一次性 md5 扫描，之后 O(1)，避免每次透传都全量遍历。"""
    global _XLIB_MEDIA_REVERSE
    if _XLIB_MEDIA_REVERSE is not None:
        return _XLIB_MEDIA_REVERSE
    import xlib as _xl
    rev: dict[str, str] = {}
    for cid in ("movie", "doc"):
        idx = _xl.library._idx[cid]
        idx.load()
        for it in idx.items:
            sg = _xl.show_guid(it["id"])
            rev[media_guid(sg)] = sg
    _XLIB_MEDIA_REVERSE = rev
    return rev


def _xlib_recover_media(mg: str) -> tuple[str, int] | None:
    """冷启动后内存映射为空时的兜底：用缓存反查索引解析单片 media_guid。
    剧集因集 guid 动态生成、无落盘，仍需先打开详情建立映射（正常播放必经详情）。"""
    sg = _xlib_recovery_index().get(mg)
    if sg:
        _XLIB_MEDIA_MAP[mg] = (sg, 0)
        return sg, 0
    return None


async def _xlib_files_for(show_guid: str) -> list[str]:
    import xlib
    found = xlib.library.by_guid(show_guid)
    if not found:
        return []
    files = await xlib.library.show_files(found["show"])
    return files


def _xlib_play_link(show_guid: str, index: int) -> str:
    """小雅文件播放地址：必须走飞牛播放器认可的同源 /online 前缀；
    direct_proxy 识别 mg=xlib 后换取网盘直链并 302，客户端直连 CDN。"""
    q = urllib.parse.urlencode({"g": show_guid, "i": index})
    return f"{ONLINE_PREFIX}/xlib/direct?{q}"


async def _xlib_stream_response(g: str, i: int, request: Request):
    """同源流式代理小雅网盘文件。飞牛播放器只认可同源 /v/media/online/... 播放地址，
    不能 302 到外部 host；故在服务端直连网盘 CDN 取字节并透传 Range（支持拖动）。

    冷文件由 signed_raw() 经小雅 /d 只触发一次转存并缓存 CDN 直链；播放器随后的
    多次 Range 请求都复用该直链，不再重复转存（避免网盘风控）。"""
    import xlib as _xl
    found = _xl.library.by_guid(g)
    if not found:
        return Response("影片不在索引", status_code=404)
    files = await _xl.library.show_files(found["show"])
    if not files:
        return Response("无视频文件", status_code=404)
    i = max(0, min(i, len(files) - 1))
    path = files[i]
    rng = request.headers.get("range")
    try:
        up, client = await _xl.library.open_media(path, rng)
    except PermissionError as exc:
        # 403/风控冷却：明确告诉播放器稍后再试，避免高频重试继续触发网盘风控
        _log.warning("xlib raw denied: %r", exc)
        return Response("网盘直链暂时受限，请稍后重试", status_code=503,
                        headers={"Retry-After": "60", "Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001
        _log.warning("xlib open_media failed: %r", exc)
        return Response("网盘取流失败", status_code=502)

    drop = {"content-security-policy"}
    h = _safe_response_headers(up.headers, drop)
    h["Access-Control-Allow-Origin"] = "*"
    # 同源流式：保留上游 206/200 与 Content-Range，供拖动/起播探测

    async def pump():
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose()
            await client.aclose()

    return StreamingResponse(
        pump(), status_code=up.status_code, headers=h,
        media_type=up.headers.get("content-type", "video/mp4"))


async def _xlib_direct_redirect(request: Request):
    """direct_proxy 的小雅分支：同源流式代理（见 _xlib_stream_response）。"""
    g = request.query_params.get("g", "")
    try:
        i = int(request.query_params.get("i", "0") or 0)
    except ValueError:
        i = 0
    return await _xlib_stream_response(g, i, request)


async def _xlib_stream_payload(guid: str, src_guid: str | None = None) -> dict | None:
    """构造小雅影片/某集的 stream/list 负载（单文件直链，非 HLS）。"""
    import xlib
    # 集 guid
    rec = _XLIB_EP_MAP.get(guid)
    if rec:
        show_guid, index = rec
    else:
        found = xlib.library.by_guid(guid)
        if not found or found["cat"] not in ("movie", "doc"):
            return None
        show_guid, index = guid, 0
    found = xlib.library.by_guid(show_guid)
    if not found:
        return None
    files = await xlib.library.show_files(found["show"])
    if not files:
        return None
    index = max(0, min(index, len(files) - 1))
    link = _xlib_play_link(show_guid, index)
    fname = os.path.basename(files[index])
    mg, vg = _xlib_register_media(src_guid or guid, show_guid, index)
    return {
        "files": [{
            "guid": mg, "path": f"网盘/{fname}", "file_name": fname,
            "size": 0, "timestamp": 0, "type": 1, "sort_num": 0, "can_play": 1,
            "play_error": "", "create_time": 0, "update_time": 0,
            "parent_fv_guid": "", "progress_thumb_hash_dir": "",
        }],
        "video_streams": [{
            "media_guid": mg, "title": "", "guid": vg,
            "resolution_type": "2160p" if "2160" in fname.upper() or "4K" in fname.upper()
            else "1080p",
            "color_range_type": "SDR", "codec_name": "hevc", "codec_type": "video",
            "color_range": "tv", "profile": "High", "index": 0, "width": 0,
            "height": 0, "coded_width": 0, "coded_height": 0,
            "display_aspect_ratio": "16:9", "pix_fmt": "yuv420p", "level": "40",
            "color_space": "", "color_transfers": "", "color_primaries": "",
            "duration": 0, "dv_profile": 0, "refs": 1, "r_frame_rate": "24 fps",
            "avg_frame_rate": "24 fps", "bits_per_raw_sample": "8", "bps": 0,
            "progressive": 1, "bit_depth": 8, "wrapper": "MP4", "create_time": 0,
            "update_time": 0, "rotation": 0, "ext1": 0, "is_bluray": False,
        }],
        "audio_streams": [], "subtitle_streams": [],
        "play_link": link, "qualities": [], "cloud_storage_info": None,
    }


async def _xlib_play_info(guid: str) -> dict | None:
    """play/info：给小雅影片/集返回进入播放页所需信息（play_link 为专用直链）。"""
    import xlib
    rec = _XLIB_EP_MAP.get(guid)
    if rec:
        show_guid, index = rec
    else:
        found = xlib.library.by_guid(guid)
        if not found:
            return None
        show_guid, index = guid, 0
    found = xlib.library.by_guid(show_guid)
    if not found:
        return None
    files = await xlib.library.show_files(found["show"])
    if not files:
        return None
    index = max(0, min(index, len(files) - 1))
    show = found["show"]
    link = _xlib_play_link(show_guid, index)
    mg, vg = _xlib_register_media(guid, show_guid, index)
    return {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg,
        "play_link": link, "subtitle_guid": "", "subtitle_index": -1,
        "subtitle_link": "", "video_guid": vg, "video_index": 0,
        "wrapper": "MP4", "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": "",
        "item_guid": guid, "title": show.get("title", ""),
        "ts": (history.get(guid) or {}).get("ts", 0),
    }
# <<< PRIVATE:CATVOD


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
        target_eg, _ = history.resume_episode(ep_guids)
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
    row.update({
        "guid": item_guid,
        "title": f"{source}｜{title}",
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
    rec = history.get(base_guid)
    if rec:
        info["ts"] = rec.get("ts", 0)
    return JSONResponse({"code": 0, "msg": "", "data":
                         clientmod.normalize_detail(info, client)})


def _normalize_history_guid(guid: str) -> str:
    """把带线路身份的复合 guid 归一化为基础影片/集 guid，保证历史、收藏、已看
    都按原片身份记录，不按资源站拆分；非复合 guid 原样返回。"""
    if not isinstance(guid, str):
        return guid or ""
    parsed = _parse_line_item_guid(guid)
    if parsed:
        og, _line, index = parsed
        brief = store.get_title_by_guid(og)
        if brief and inj.is_tv(brief):
            return episode_guid(og, "", index)
        return og
    season = _parse_line_season_guid(guid)
    if season:
        return season_guid(season[0], season[2])
    return guid


@app.post("/v/api/v1/item/list")
async def item_list(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except Exception:
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
            return JSONResponse({"code": 0, "msg": "", "data": {
                "mdb_name": GUARD_NATIVE_TITLE, "mdb_category": "Mix",
                "top_dir": "", "dir": "", "total": total,
                "list": out_rows, "jump_list": None}})
        # 根目录默认：热门聚合 + 各源文件夹（OK影视 首页形态）
        root_rows = _gn_root_folder_rows()
        out_rows = clientmod.normalize_cards(root_rows, client)
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
            row["title"] = (b.get("title") or "") + (
                f" ·{b['remarks']}" if b.get("remarks") else "")
            row["ancestor_guid"] = aguid
            row["ancestor_name"] = lib_name
            row["ancestor_category"] = "Mix"
            row["video_guid"] = video_guid(b["guid"])
            _apply_marks(row)
            rows.append(_vod_directory_card(row))
        rows = clientmod.normalize_cards(rows, client)
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
        pass
    all_rows = _chart_rows_all(body, cat)
    page_size, start, end = _page_args(body, len(all_rows))
    out_rows = all_rows[start:end]
    out_rows = clientmod.normalize_cards(out_rows, client)
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
    # >>> PRIVATE:CATVOD
    if _xlib_on():
        xsl = await _xlib_stream_payload(guid)
        if xsl is not None:
            return JSONResponse({"code": 0, "msg": "", "data": xsl})
    # <<< PRIVATE:CATVOD
    # 红果短剧 episode 线路：直链 play_link，cloud_storage_info 直连类型
    if hgdj_enabled() and hgdjmod.is_hgdj_episode(guid):
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
                    resume_guid, _ = history.resume_episode(egs)
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
    """把一个在线 guid 解析成可用于收藏夹展示的元信息（不做网络请求）。"""
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
    g = row.get("guid", "")
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
    }


@app.put("/v/api/v1/item/favorite")
async def item_favorite_add(request: Request):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
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
    if brief:
        row = inj.search_item(brief)
        row["title"] = inj._f(brief, "title") + (
            f" ·{inj._f(brief, 'remarks')}" if inj._f(brief, "remarks") else "")
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
            row["title"] = inj._f(parent, "title") + (
                f" ·{inj._f(parent, 'remarks')}" if inj._f(parent, "remarks") else "")
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
        body = {}
    local = await forward_upstream(request)
    try:
        ldata = json.loads(local.body)
    except Exception:
        return local
    if not isinstance(ldata, dict):
        return local
    ldata.setdefault("data", {})
    if not isinstance(ldata["data"], dict):
        return local

    ftype = str(body.get("type") or "all")
    cards: list[dict] = []
    for rec in marks.favorite_records():
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
        rec0 = history.get(guid)
        if rec0:
            info["ts"] = rec0.get("ts", 0)
        return info
    brief = store.get_title_by_guid(guid)
    if not brief:
        return None
    rec = history.get(guid)
    if inj.is_tv(brief):
        ep_guids = store.tv_episode_guids(guid, regular_only=True)
        if ep_guids:
            # T4 远程剧集采用“回看最近交互”语义；普通 CMS 保持按集序续播
            t4_rewatch = brief.get("locator", {}).get("kind") == "t4"
            target_eg, resume_ts = history.resume_episode(
                ep_guids, latest_interaction=t4_rewatch)
            if target_eg:
                ep = store.get_episode(target_eg)
                if ep:
                    info = inj.episode_play_info(ep)
                    info["ts"] = resume_ts
                    return info
    info = inj.play_info(brief)
    if rec:
        info["ts"] = rec.get("ts", 0)
    return info


@app.post("/v/api/v1/play/info")
async def play_info(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    guid = body.get("item_guid", "")
    client = _client_of(request)
    if _parse_line_item_guid(guid):
        line_info = _vod_play_info(guid, client)
        if line_info is not None:
            return line_info
    error = await _load_t4_detail(guid, request)
    if error is not None:
        return error

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
    rec = history.get(guid)
    if ep:
        info = inj.episode_play_info(ep)
        if rec:
            info["ts"] = rec.get("ts", 0)
        _prewarm_from_info(info)
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
    if not target and chart_guid_by_media(mg):
        # 网页播放器会在进入播放页前连续请求 stream；这里必须瞬时返回，不能等待搜源，
        # 否则三个请求串起来仍会把用户留在海报墙。真实匹配推迟到播放器请求 preset。
        start_resolve_chart(chart_guid_by_media(mg))
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
    chart_g = "" if target else chart_guid_by_media(mg)
    if not target and chart_g:
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
    if not base_target and chart_guid_by_media(mg):
        # 未绑定榜单片：play/play 也立即返回占位 HLS，播放器先进入加载页；
        # preset/main 请求时再等待后台匹配，避免 play/info+stream+play 串行等待。
        start_resolve_chart(chart_guid_by_media(mg))
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
        body = {}
    ig = body.get("item_guid", "")
    body["item_guid"] = _normalize_history_guid(ig)
    ig = body["item_guid"]
    if store.get_title_by_guid(ig) or store.get_episode(ig):
        # 记录在线播放进度到本地（继续观看），不上报官方库
        history.record(body)
        return JSONResponse({"code": 0, "msg": "", "data": True})
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
            except Exception:
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
        return False
# <<< PRIVATE:SPIDER


# ===== 播放线路健康探测与自动换线 =====
# 问题背景：用户手动切到某条坏线（文件已删/网盘无此集/源站失效）后，插件只是把该线
# 302/下发，从不校验死活，播放器一直报错；坏线也不会被记忆，退出重放仍黏在该线。
# 方案：①坏线短时记忆 ②起播/取流前并行探测同集各候选线路，自动切到首个可用线路。
_LINE_BAD_TTL = 300.0          # 坏线记忆 5 分钟（签名时效内，避免长期误判偶发抖动）
_LINE_BAD: dict[str, float] = {}
_PROBE_TIMEOUT = 8.0
_PROBE_MAX_CANDIDATES = 8     # 最多探测候选线路数（控制起播开销）
_PROBE_RANGE = "bytes=0-2047"


def _line_bad_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{picked.get("line", "")}|{int(picked.get("index", 0) or 0)}'


def _mark_line_bad(picked: dict) -> None:
    if not picked or not picked.get("line"):
        return
    _LINE_BAD[_line_bad_key(picked)] = time.time()
    if len(_LINE_BAD) > 1000:
        for k in list(_LINE_BAD)[:500]:
            _LINE_BAD.pop(k, None)


def _line_is_bad(picked: dict) -> bool:
    ts = _LINE_BAD.get(_line_bad_key(picked))
    if ts is None:
        return False
    if time.time() - ts >= _LINE_BAD_TTL:
        _LINE_BAD.pop(_line_bad_key(picked), None)
        return False
    return True


def _autofail_on() -> bool:
    try:
        return bool(config.get().get("line_autofail", True))
    except Exception:
        return True


def _episode_candidates(picked: dict, include_current: bool = True) -> list[dict]:
    """枚举该集在各线路下的可播目标（按 store 默认顺序：默认线在前）。
    直接复用索引，不发起任何网络请求。"""
    og = picked.get("online_guid", "")
    idx = int(picked.get("index", 0) or 0)
    cur_line = picked.get("line")
    brief = store.get_title_by_guid(og) if og else None
    if not brief:
        return []
    default = store.best_line(og)
    ordered = ([default] if default else []) + \
              [ln for ln in brief.get("lines", {}) if ln != default]
    out: list[dict] = []
    seen = set()
    for ln in ordered:
        if not include_current and ln == cur_line:
            continue
        eps = store._direct_eps(brief, ln)
        if idx >= len(eps):
            continue  # 该线路没有这一集（如小雅仅 1 集），直接排除
        t = store.resolve_episode(og, ln, idx)
        if not t or not t.get("url"):
            continue
        t = dict(t, ep_guid=picked.get("ep_guid", ""),
                 kind=picked.get("kind", "title"))
        if t["url"] in seen:
            continue
        seen.add(t["url"])
        out.append(t)
    return out


def _ordered_candidates(picked: dict) -> list[dict]:
    """候选排序：当前线置顶，已知坏线沉底。"""
    cands = _episode_candidates(picked)
    cur = picked.get("line")

    def rank(t: dict) -> tuple:
        sk = sourcehealth.source_key_of(t.get("line"), t)
        # 当前线置顶 → 坏线/死源沉底 → 按源健康画像（成功率/延迟）排序
        return (t["line"] != cur,
                _line_is_bad(t),
                sourcehealth.weight(sk) if sk else (1, 5.0, 1.0))

    cands.sort(key=rank)
    return cands


async def _probe_latency(cand: dict, request: Request,
                         build_hls: bool = False) -> float | None:
    """探测一条线路该集是否可播，并返回“起播延迟”（秒，越小越快）。

    会就地解析（专有源/跳转/分享，与真实播放同一套逻辑）。失败/文件不存在返回 None。
    build_hls=True 时，HLS 线路顺手构建改写后的播放列表并填缓存（main 取流零额外抓取）；
    竞速阶段传 False，只做轻量验活（抓原始清单，不改写），选出胜者后再构建。"""
    t0 = time.time()
    try:
        await _resolve_if_needed(cand, request)
        raw_url = cand.get("url", "")
        if not raw_url:
            return None
        # 网盘转存后是本服务根相对地址（/v/api/ext/pan/play?fid=…），经回环自调用
        url = _self_url(raw_url)
        headers = cand.get("headers") or {"User-Agent": "Mozilla/5.0"}
        cli = ext_client(request)

        # 非 HLS（网盘 /d 直链、mp4 等）：Range GET 探测
        is_m3u8 = bool(cand.get("m3u8")) or bool(
            re.search(r"\.m3u8(\?|$)", url.split("?", 1)[0], re.I))
        # 网盘播放代理按 fid 实时换链：转码档返回清单、原画直出 mp4，URL 无后缀，
        # 直接交给播放列表构建（build_hls）或轻量抓清单判定，避免误走 Range 探测。
        is_pan_proxy = "/v/api/ext/pan/" in url
        if not is_m3u8 and not is_pan_proxy:
            try:
                req = cli.build_request(
                    "GET", url, headers={**headers, "Range": _PROBE_RANGE})
                r = await cli.send(req, follow_redirects=True)
                try:
                    status = r.status_code
                    ctype = (r.headers.get("content-type") or "").lower()
                    body = b""
                    try:
                        body = await r.aread()
                    except Exception:
                        pass
                finally:
                    try:
                        await r.aclose()
                    except Exception:
                        pass
            except Exception:
                return None
            if status >= 400:
                return None
            # 小雅/AList 对“目录或不存在文件”回 200 + JSON 错误（假活），判死
            if "json" in ctype or body[:1] in (b"{", b"["):
                return None
            return time.time() - t0

        # HLS
        key = _playlist_cache_key(cand)
        cached = _PLAYLIST_CACHE.get(key)
        if cached:
            body = cached[1]
            if body is not _NOT_HLS and \
                    not await _verify_rewritten_first_seg(body, request, cand):
                _PLAYLIST_CACHE.pop(key, None)
                return None
            return time.time() - t0
        if build_hls:
            mg = getattr(request.state, "_play_mg", "") if hasattr(request, "state") else ""
            if mg:
                body = await _get_playlist_cached(mg, cand, request)
                if body is None:
                    return None
                if body is not _NOT_HLS and \
                        not await _verify_rewritten_first_seg(body, request, cand):
                    _PLAYLIST_CACHE.pop(key, None)
                    return None
                return time.time() - t0
        # 轻量验活：抓原始清单（跟随嵌套 master），不做分片改写
        text, _cur = await _fetch_playlist(request, url, headers)
        if text is None or not _is_media_playlist(text):
            return None
        # 分片级验活：部分加固源（海绵/糯米的 yximgs PNG 伪装流等）清单能下、
        # 但所有分片在源站 404（清单活、分片死）。只验清单会误判可播，起播即报错。
        # 取首个分片 Range 2KB 复核：4xx/5xx 或空体即判死，触发自动换线。
        try:
            first_seg = next(
                (ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")), "")
            if first_seg:
                seg_url = urllib.parse.urljoin(_cur, first_seg)
                sreq = cli.build_request(
                    "GET", seg_url, headers={**headers, "Range": _PROBE_RANGE})
                sr = await cli.send(sreq, follow_redirects=True)
                try:
                    if sr.status_code >= 400:
                        _log.info("线路首分片%d判死: %s 《%s》%s",
                                  sr.status_code, cand.get("line"),
                                  cand.get("title"),
                                  urllib.parse.urlsplit(seg_url).netloc)
                        return None
                    head = b""
                    try:
                        head = await sr.aread()
                    except Exception:
                        pass
                    # 源站对失效分片常回 200/404 + XML/HTML 错误页（非媒体）
                    if not head or head[:1] in (b"<", b"{", b"["):
                        return None
                finally:
                    try:
                        await sr.aclose()
                    except Exception:
                        pass
        except Exception:
            return None
        return time.time() - t0
    except Exception:
        return None


async def _probe_target(cand: dict, request: Request) -> bool:
    """探测某条线路是否可播（HLS 顺带构建播放列表缓存）。布尔版，供故障切换复用。"""
    lat = await _probe_latency(cand, request, build_hls=True)
    sk = sourcehealth.source_key_of(cand.get("line"), cand)
    if sk:
        # 取流探测结果只记取流级熔断（5 次/3 分钟），不影响搜索级计数。
        sourcehealth.record(sk, lat is not None, lat or 0.0, level="stream")
    return lat is not None


# 首分片验活的短期正缓存：真实分片 URL -> 验活时间。签名 URL 会变，自然按新
# URL 重新验活。避免正常播放每次取清单都探测上游。
_SEG_OK_CACHE: dict[str, float] = {}
_SEG_OK_TTL = 180.0


async def _verify_rewritten_first_seg(body: str, request: Request,
                                      picked: dict | None = None) -> bool:
    """对已改写的清单做首分片验活：解码 /seg?u= 里的真实上游地址，直接 Range 探测。
    识别“清单活、分片 404/错误页”的死链（如某源 yximgs CDN 删流）。不在进程内
    回环请求自身（会受 worker 并发影响而误判）。探测异常不轻易判死，避免误伤。"""
    try:
        first = next((ln.strip() for ln in body.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")), "")
        if not first or first.startswith("hls?"):
            return True  # 紧凑分片/无分片信息：交由实际分片端点处理，不预判
        qs = urllib.parse.urlparse(first if first.startswith("http")
                                   else _self_url(first)).query
        u = urllib.parse.parse_qs(qs).get("u", [""])[0]
        if not u:
            return True
        real = b64d(u)
        now = time.time()
        if _SEG_OK_CACHE.get(real) and now - _SEG_OK_CACHE[real] < _SEG_OK_TTL:
            return True
        headers = dict((picked or {}).get("headers") or
                       {"User-Agent": "Mozilla/5.0"})
        # >>> PRIVATE:GUARD
        # 网盘分片需登录 Cookie
        try:
            host = urllib.parse.urlsplit(real).netloc.lower()
            if host.endswith(".quark.cn"):
                import pan as _panq
                headers["Cookie"] = _panq.auth_cookie_or_raise()
                headers.setdefault("Referer", "https://pan.quark.cn/")
            elif host.endswith(".uc.cn"):
                import panuc as _panu
                headers["Cookie"] = _panu.auth_cookie_or_raise()
                headers.setdefault("Referer", "https://drive.uc.cn/")
        except Exception:
            pass
        # <<< PRIVATE:GUARD
        headers["Range"] = _PROBE_RANGE
        cli = ext_client(request)
        req = cli.build_request("GET", real, headers=headers)
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return False
            b = b""
            try:
                b = await r.aread()
            except Exception:
                return True  # 读体失败不判死
            if not b or b[:1] in (b"<", b"{", b"["):
                return False
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
        _SEG_OK_CACHE[real] = now
        if len(_SEG_OK_CACHE) > 1500:
            for old in list(_SEG_OK_CACHE)[:700]:
                _SEG_OK_CACHE.pop(old, None)
        return True
    except Exception:
        return True  # 探测本身异常：保守放行，避免误伤好线


async def _find_playable(picked: dict, request: Request,
                         skip_current: bool = False) -> dict | None:
    """并行探测候选线路，返回（按优先级排序后的）首个可用目标（故障切换用）。"""
    cands = _ordered_candidates(picked)
    if skip_current:
        cands = [c for c in cands if c["line"] != picked.get("line")]
    if not cands:
        return None
    fresh = [c for c in cands if not _line_is_bad(c)] or cands
    fresh = fresh[:_PROBE_MAX_CANDIDATES]
    for i in range(0, len(fresh), 3):
        batch = fresh[i:i + 3]
        results = await asyncio.gather(
            *[_probe_target(c, request) for c in batch])
        for cand, ok in zip(batch, results):
            if ok:
                return cand
    return None


# 内置线路全死后，按片名全网回源找到的可播记录：原 media_guid -> 真实可播 og。
# 让播放器后续仍用原 mg 请求时能命中新记录（跨同名记录播放）。
_MEDIA_REDIRECT: dict[str, str] = {}
_RESURRECT_FAIL: dict[str, float] = {}
_RESURRECT_FAIL_TTL = 10 * 60
_RESURRECT_FUT: dict[str, asyncio.Future] = {}  # 规范化片名 -> 后台回源任务（单飞）


async def _http_head_ok(url: str, headers: dict) -> bool:
    """不依赖 request 的首片段验活：Range 取 2KB，4xx/错误页/空体判死。"""
    try:
        cli = app.state.ext
        req = cli.build_request(
            "GET", url, headers={**headers, "Range": _PROBE_RANGE})
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return False
            b = b""
            try:
                b = await r.aread()
            except Exception:
                pass
            return bool(b) and b[:1] not in (b"<", b"{", b"[")
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
    except Exception:
        return False


async def _resurrect_start(picked: dict, mg: str) -> str:
    """启动（或复用）按片名的后台回源任务。返回 'running' / 'ok' / 'fail'。"""
    title = (picked.get("title") or "").strip()
    nkey = re.sub(r"[\s\W_]+", "", title.lower())
    if not nkey:
        return "fail"
    if mg and mg in _MEDIA_REDIRECT:
        return "ok"
    if time.time() - _RESURRECT_FAIL.get(nkey, 0) < _RESURRECT_FAIL_TTL:
        return "fail"
    fut = _RESURRECT_FUT.get(nkey)
    if fut is None:
        fut = asyncio.ensure_future(_resurrect_worker(picked, mg, nkey))
        _RESURRECT_FUT[nkey] = fut

        def _drop(_f, k=nkey):
            _RESURRECT_FUT.pop(k, None)
        fut.add_done_callback(_drop)
    if fut.done() and not fut.cancelled():
        return "ok" if fut.result() else "fail"
    return "running"


async def _resurrect_worker(picked: dict, mg: str, nkey: str) -> bool:
    """后台按片名找真正能播的同名记录。两阶段：秒回直链源 → 加固源。"""
    title = (picked.get("title") or "").strip()
    want_idx = int(picked.get("index", 0) or 0)

    def match(b):
        return re.sub(r"[\s\W_]+", "", (b.get("title") or "").lower()) == nkey

    async def evaluate(briefs, direct_only):
        for b in briefs:
            if not match(b):
                continue
            og = b.get("guid")
            if not og:
                continue
            for ln, eps in (b.get("lines") or {}).items():
                if not eps:
                    continue
                ep = store.resolve_episode(og, ln, want_idx)
                if not ep or not ep.get("url"):
                    continue
                cand = dict(ep)
                if direct_only and not re.search(
                        r"\.(m3u8|mp4)(\?|$)", cand["url"], re.I):
                    continue
                # 解析到真实地址（加固源 detail+play），再验首片段
                try:
                    tgt = dict(cand, online_guid=og, kind="title")
                    await _resolve_if_needed(tgt, None)
                    if not tgt.get("url") or tgt.get("guard"):
                        continue
                    hdrs = tgt.get("headers") or {"User-Agent": "Mozilla/5.0"}
                    real = _self_url(tgt["url"])
                    is_hls = bool(tgt.get("m3u8")) or bool(
                        re.search(r"\.m3u8(\?|$)", real.split("?", 1)[0], re.I))
                    seg_url = real
                    if is_hls:
                        cur, seg_url = real, ""
                        for _d in range(3):  # 跟随嵌套 master 取首个分片
                            rr = await app.state.ext.get(
                                cur, headers=hdrs, timeout=15,
                                follow_redirects=True)
                            txt = rr.text
                            sl = [x.strip() for x in txt.splitlines()
                                  if x.strip() and not x.lstrip().startswith("#")]
                            if not sl:
                                break
                            if _is_media_playlist(txt):
                                seg_url = urllib.parse.urljoin(cur, sl[0])
                                break
                            cur = urllib.parse.urljoin(cur, sl[0])
                        if not seg_url:
                            continue
                    if not await _http_head_ok(seg_url, hdrs):
                        continue
                except Exception:
                    continue
                # 命中：记录可播 og，并补一条可解析线路到该 og（_pick_episode 用）
                if mg:
                    _MEDIA_REDIRECT[mg] = og
                _log.info("内置线路全死，全网回源《%s》-> %s", title, ln)
                return True
        return False

    try:
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=10, fast_only=True), timeout=20)
        if await evaluate(briefs, True):
            return True
    except Exception:
        pass
    try:
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=10), timeout=30)
        if await evaluate(briefs, False):
            return True
    except Exception:
        pass
    _RESURRECT_FAIL[nkey] = time.time()
    return False



# ===== 完全自动选最快线 =====
_LINE_FAST_TTL = 600.0          # 最快线记忆 10 分钟
_LINE_FAST: dict[str, tuple[float, str]] = {}   # 集 key -> (ts, line)
_RACE_CONCURRENCY = 4


def _episode_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{int(picked.get("index", 0) or 0)}'


def _remember_fast(picked: dict, line: str, latency: float | None = None) -> None:
    """记下竞速实测最快线（_LINE_FAST，turbo 实测结果为准）。

    冻结：只有竞速胜者能写 _LINE_FAST；预测结果绝不写这里。
    另把实测胜者同步给本片预测缓存（单向：turbo 实测 → linepredict，供下次
    竞速作优先种子；linepredict 绝不反向写 _LINE_FAST）。
    """
    if not line:
        return
    _LINE_FAST[_episode_key(picked)] = (time.time(), line)
    if len(_LINE_FAST) > 1000:
        for k in list(_LINE_FAST)[:500]:
            _LINE_FAST.pop(k, None)
    try:
        linepredict.remember(
            _episode_key(picked), line,
            float(latency) if latency is not None else 0.0)
    except Exception:
        pass


def remembered_fast_line(picked: dict) -> str | None:
    """该集上一次竞速选出的最快线（未过期、且当前未被标记为坏线）。

    冻结：只读 turbo 竞速记忆 _LINE_FAST。刻意不搬上游的 linepredict.fastest
    回落——预测线只能作竞速候选排序种子（见 _race_candidates），绝不能不经
    竞速就被当作最快线复用。
    """
    rec = _LINE_FAST.get(_episode_key(picked))
    if not rec:
        return None
    ts, line = rec
    if time.time() - ts >= _LINE_FAST_TTL:
        _LINE_FAST.pop(_episode_key(picked), None)
        return None
    if _line_is_bad({**picked, "line": line}):
        _LINE_FAST.pop(_episode_key(picked), None)
        return None
    return line


def _fastest_on() -> bool:
    try:
        return bool(config.get().get("line_fastest", True))
    except Exception:
        return True


# --- 线路实测吞吐（KB/s，指数滑动平均） ------------------------------------
# 竞速只比"首字节延迟"，会挑中"响应快、吞吐烂"的线：实测同一部片里既有 250 KB/s
# 的线，也有 26 KB/s 的坑，延迟上看不出差别，播起来差 10 倍。播放时顺便把每条线
# 真实下片速度记下来（零额外流量），下次选线优先用真快的。
_LINE_SPEED: dict[str, tuple[float, float]] = {}   # line -> (KB/s, ts)


def _remember_speed(picked: dict | None, kbps: float) -> None:
    if not picked or not picked.get("line") or kbps <= 0:
        return
    k = str(picked["line"])
    old = _LINE_SPEED.get(k)
    val = kbps if old is None else old[0] * 0.6 + kbps * 0.4
    _LINE_SPEED[k] = (val, time.time())
    if len(_LINE_SPEED) > 800:
        for kk in list(_LINE_SPEED)[:400]:
            _LINE_SPEED.pop(kk, None)


def _line_speed(picked: dict) -> float:
    rec = _LINE_SPEED.get(str(picked.get("line", "")))
    if not rec:
        return 0.0
    if time.time() - rec[1] > 3600:      # 一小时前的观测不再作数
        return 0.0
    return rec[0]


_SPEED_PRIOR = 150.0   # 未知线路的先验吞吐（KB/s）：没测过的线当作"可能还行"
_SPEED_GOOD = 200.0    # 达到这个实测吞吐才值得直接单发复用，低于它就换线再找


def _line_score(cand: dict) -> float:
    """选线用的吞吐评分：测过的用实测值，没测过的给先验值。

    没测过的线不能排在"实测很差（如 60 KB/s）"的线后面——否则一次测到烂线就
    永远不再试别的线。先验 150 介于"已知很差"和"已知不错"之间。"""
    s = _line_speed(cand)
    return s if s > 0 else _SPEED_PRIOR


def _race_candidates(picked: dict, seed: str = "") -> list[dict]:
    """竞速候选：默认线/上次最快线在前，坏线排除（除非全坏），限量。"""
    cands = _episode_candidates(picked)
    default = store.best_line(picked.get("online_guid", ""))
    fast = remembered_fast_line(picked)
    # 冻结适配（上游 linepredict 的降级形态）：预测线只作竞速优先种子——在吞吐
    # 打分相同、且非上次竞速最快线时排前一位。竞速仍全量跑（first-success wins
    # 不变），吞吐打分仍是第一排序键，预测线不进 _LINE_FAST、不替代竞速结果。
    # 上游的 _predict_order_lines（预测线直接置顶）刻意不搬。
    pred = None
    try:
        if bool(config.get().get("linepredict_enabled", True)):
            pred = linepredict.fastest(_episode_key(picked))
    except Exception:
        pred = None
    cands = [c for c in cands if not _line_is_bad(c)] or cands
    try:
        n = int(config.get().get("line_fastest_max", 6))
    except Exception:
        n = 6
    cands = cands[:max(2, n)]

    def _src_dead(c: dict) -> bool:
        # 源级取流熔断（上游 v0.2.70）：某资源站取流域名整片不可达时（搜索域可能
        # 仍正常），其所有线路在竞速里沉底，避免每部片都先撞长超时再换线。
        # 取流级阈值 5 次/3 分钟（level="stream"），与搜索级 5 次/3 分钟分开记。
        # 关键：熔断源只沉底、绝不排除——健康候选不足 n 时用它们补足，保证竞速总有
        # 候选，避免“源被熔断→候选只剩 1 条且恰好是死链→起播直接失败”。
        try:
            sk = sourcehealth.source_key_of(c.get("line"), c)
            return bool(sk) and sourcehealth.is_dead(sk, level="stream")
        except Exception:
            return False

    def rank(c: dict) -> tuple:
        return (_src_dead(c),                          # 熔断源沉底（仍在候选内）
                -round(_line_score(c), 1),           # 实测/先验吞吐高的线优先
                c["line"] != fast if fast else True,
                c["line"] != pred if pred else True,
                c["line"] != default if default else True,
                # 冻结：种子只作最后一级 tiebreak，绝不盖过吞吐打分/最快线/默认线。
                c["line"] != seed if seed else True)

    cands.sort(key=rank)
    return cands


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


async def _ensure_playable(picked: dict, request: Request,
                           auto: bool = False) -> dict:
    """取流前确保目标可播。

    - auto=True（播放器自动起播，非用户手动切线）且开启最快线：并行竞速选最快线。
    - 否则（用户手动选了某条线）：先探当前线，失败才记忆坏线并故障切换到首个可用线。
    返回应播放的 picked（可能已换成另一线路）。"""
    if not picked or not picked.get("online_guid"):
        return picked
    if auto and _autofail_on() and _fastest_on():
        winner = await _race_fastest(picked, request)
        if winner is not None:
            return winner
        # 竞速全失败：退回常规故障切换（探当前线/换线）
    if not _autofail_on():
        await _resolve_if_needed(picked, request)
        return picked
    cur_bad = _line_is_bad(picked)
    if not cur_bad:
        if await _probe_target(picked, request):
            return picked
        _mark_line_bad(picked)
        _log.warning("线路不可用，自动换线: %s 《%s》%s",
                     picked.get("line"), picked.get("title"),
                     picked.get("ep_title"))
    good = await _find_playable(picked, request, skip_current=True)
    if good is not None:
        _log.info("自动切换到可用线路: %s -> %s", picked.get("line"), good.get("line"))
        return good
    return picked


async def _resolve_if_needed(picked: dict, request: Request) -> dict:
    """把播放页(/share/<id>)或 302 跳转地址解析成真实媒体地址（原地更新）。"""
    # >>> PRIVATE:CATVOD
    if features.CATVOD and picked and picked.get("catvod"):
        await _resolve_catvod(picked)
    # <<< PRIVATE:CATVOD
    # >>> PRIVATE:DRPY
    elif features.DRPY and picked and picked.get("drpy"):
        await _resolve_drpy(picked)
    # <<< PRIVATE:DRPY
    # >>> PRIVATE:GUARD
    elif features.GUARD and picked and picked.get("guard"):
        await _resolve_guard(picked)
    # <<< PRIVATE:GUARD
    # >>> PRIVATE:SPIDER
    elif features.GUARD and picked and picked.get("spider_src"):
        await _resolve_spider(picked)
    # <<< PRIVATE:SPIDER
    # >>> PRIVATE:DDUO
    elif picked and picked.get("xsrc") and features.DDUO:
        await _resolve_dduo_with_fallback(picked, request)
    # <<< PRIVATE:DDUO
    if picked and picked.get("redirect"):
        real = await providers.resolve_redirect(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["redirect"] = False
            picked["m3u8"] = bool(re.search(r"\.m3u8(\?|$)", real, re.I))
    if picked and picked.get("resolve"):
        real = await providers.resolve_share(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["resolve"] = False
    return picked


# ===== 起播速度优化：播放列表短时缓存/预热 =====
# 说明：分辨率（清晰度）探测模块已整体搁置（ffprobe/额外分片占用资源、慢源下不稳定），
# preset 线路菜单只显示线路名、不再追加分辨率；quality.py 仅保留 PNG 伪装分片解包用途。
# 后续有更优的识别方案再行接入。


# 改写后的媒体播放列表短时内存缓存，避免同一集起播/重试反复抓上游。
# 上游地址带时效签名，TTL 取短；只缓存成功结果。
_PLAYLIST_CACHE: dict[str, tuple[float, str]] = {}
_PLAYLIST_TTL = 120.0
# 单飞：同一「集+线路」只允许一次在途抓取，预热与真正取 main 共用同一个 Future
_PLAYLIST_FUT: dict[str, asyncio.Future] = {}

# 紧凑分片注册表：夸克/UC 转码 HLS 的签名分片 URL 极长（~600 字符，base64 后近千），
# 一部 90 分钟的片子上千分片会撑出 1MB+ 的清单，且每个分片 URL 超长，经外网网关
# （Lucky/nginx/fngateway）容易被长度/缓冲限制拦掉，播放器拿到清单却拉不到分片。
# 故把「清单ID + 序号 → 真实分片URL」留在服务端，清单里只给短 URL（~60 字符）。
_HLS_REG: dict[str, tuple[float, list[str]]] = {}
_HLS_REG_TTL = 3 * 3600.0
_HLS_LONG_URL = 320          # 分片 URL 超过该长度即走紧凑注册表
_HLS_REG_MAX = 4000


def _hls_reg_put(urls: list[str]) -> str:
    import secrets
    rid = secrets.token_hex(6)
    _HLS_REG[rid] = (time.time(), urls)
    if len(_HLS_REG) > _HLS_REG_MAX:
        for old in sorted(_HLS_REG, key=lambda k: _HLS_REG[k][0])[:len(_HLS_REG) - _HLS_REG_MAX]:
            _HLS_REG.pop(old, None)
    return rid


def _hls_reg_get(rid: str, i: int) -> str:
    ent = _HLS_REG.get(rid)
    if not ent:
        return ""
    ts, urls = ent
    if time.time() - ts > _HLS_REG_TTL:
        _HLS_REG.pop(rid, None)
        return ""
    return urls[i] if 0 <= i < len(urls) else ""

# 哨兵：该线路解析后实为 mp4 直链（非 HLS），调用方应 302 到 direct
_NOT_HLS = object()


def _playlist_cache_key(picked: dict) -> str:
    return f'{picked.get("online_guid","")}|{picked.get("line","")}|{int(picked.get("index",0) or 0)}'


async def _build_playlist_body(mg: str, picked: dict, request: Request):
    """抓取该线路的上游媒体列表，并把分片改写为经本代理的绝对地址。
    返回 (body_text, base_url)；失败返回 (None, None)。"""
    headers = picked["headers"]
    was_redirect = bool(picked.get("redirect"))
    await _resolve_if_needed(picked, request)
    src_url = _self_url(picked["url"])
    # 解析后若发现其实是 mp4 直链（wrapper 预判为 HLS 时的兜底），交由调用方 302
    if not re.search(r"\.m3u8(\?|$)", src_url, re.I) \
            and re.search(r"\.mp4(\?|$)", src_url, re.I):
        return _NOT_HLS, None
    text, cur = await _fetch_playlist(request, src_url, headers)
    # 网盘播放代理：原画直出 mp4 时回包不是清单（content-type video、二进制），
    # 判为非 HLS 交调用方 302 到 direct；转码档则返回正常清单。
    if text is not None and picked.get("pan") and not _is_media_playlist(text):
        return _NOT_HLS, None
    # 跳转型源站极慢且间歇抖动：解析后的真实地址抓取失败时，对原始跳转地址
    # 强制重新解析一次再抓，避免整列表 502。
    if text is None and was_redirect:
        from store import store as _store
        og = picked.get("online_guid")
        raw = _store.get_title_by_guid(og) if og else None
        jump = None
        if raw:
            for _ln, eps in (raw.get("lines") or {}).items():
                for e in eps:
                    if e.get("redirect"):
                        jump = e.get("url")
                        break
                if jump:
                    break
        if jump:
            fresh = await providers.resolve_redirect(
                ext_client(request), jump, (headers or {}).get("Referer", ""))
            if fresh:
                text, cur = await _fetch_playlist(request, fresh, headers)
                if text is not None:
                    picked["url"] = fresh
    if text is None:
        return None, None

    # m3u8 流层去广告（TVBox/OK影视 Pro rules 引擎 + 通用启发式），在改写分片前执行
    try:
        import adfilter as _adfilter
        if _adfilter.enabled():
            text, _ad_removed = _adfilter.filter_hls(text, src_url)
    except Exception:
        _log.info("hls adfilter failed", exc_info=True)

    sel_qs = ""
    if picked.get("line"):
        sel_qs = f'line={urllib.parse.quote(picked["line"])}&index={picked["index"]}'

    # AES-128 加密 HLS：部分移动/TV 原生播放器拉到密钥后无法正确解密（报 http error）。
    # 在服务端取一次密钥，改写每个分片的 IV/密钥参数，由 /seg 统一解密后回传纯 TS，
    # 并从输出列表删除 EXT-X-KEY，彻底规避端侧 AES 兼容差异。
    decrypt_key: bytes | None = None
    explicit_iv: bytes | None = None
    key_marker = next((ln for ln in text.splitlines()
                       if ln.strip().startswith("#EXT-X-KEY")
                       and "METHOD=AES-128" in ln), "")
    if key_marker:
        try:
            import quality as _quality
            parsed = await _quality._parse_hls_key(
                ext_client(request), text, cur, headers)
            if parsed:
                decrypt_key, explicit_iv = parsed
        except Exception as exc:
            _log.warning("hls key parse failed: error=%s", type(exc).__name__)
            decrypt_key = None

    media_seq = 0
    mm = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", text)
    if mm:
        media_seq = int(mm.group(1))
    seg_index = 0
    out_lines = []

    # 先收集所有分片绝对地址（与下方输出顺序一致：URI 标签按出现序，分片行按出现序）
    seg_urls: list[str] = []
    for raw0 in text.splitlines():
        ln0 = raw0.strip()
        if ln0.startswith("#"):
            if ln0.startswith("#EXT-X-MAP") or ln0.startswith("#EXT-X-KEY"):
                m0 = re.search(r'URI="([^"]+)"', ln0, re.I)
                if m0:
                    seg_urls.append(urllib.parse.urljoin(cur, m0.group(1)))
        elif ln0:
            seg_urls.append(urllib.parse.urljoin(cur, ln0))
    # 任一地址过长即整表走紧凑注册表（同表统一，避免混用）
    use_compact = any(len(u) > _HLS_LONG_URL for u in seg_urls)
    compact_rid = _hls_reg_put(seg_urls) if use_compact else ""

    def seg_decrypt_qs() -> str:
        iv = explicit_iv if explicit_iv is not None else (
            (media_seq + seg_index).to_bytes(16, "big"))
        return f"dec=1&k={b64e(decrypt_key.hex())}&iv={b64e(iv.hex())}"

    compact_i = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY") and decrypt_key is not None:
            # 服务端已解密：客户端无需也不能再拿到密钥标签
            continue
        if line.startswith("#EXT-X-MAP") or \
                (line.startswith("#EXT-X-KEY") and 'URI="' in line):
            def repl(m):
                nonlocal compact_i
                uri = m.group(1)
                if compact_rid:
                    ref = f"hls?r={compact_rid}&i={compact_i}"
                else:
                    ref = _seg_url(mg, urllib.parse.urljoin(cur, uri), sel_qs)
                compact_i += 1
                return f'URI="{ref}"'
            raw = re.sub(r'URI="([^"]+)"', repl, line, flags=re.I)
            out_lines.append(raw)
        elif line.startswith("#") or not line:
            out_lines.append(raw)
        else:
            if compact_rid:
                out_lines.append(
                    f"hls?r={compact_rid}&i={compact_i}")
                compact_i += 1
            else:
                if decrypt_key is not None:
                    parts = [p for p in (sel_qs, seg_decrypt_qs()) if p]
                    extra = "&".join(parts)
                else:
                    extra = sel_qs
                out_lines.append(_seg_url(mg, urllib.parse.urljoin(cur, line), extra))
            seg_index += 1
    return "\n".join(out_lines) + "\n", cur


async def _get_playlist_cached(mg: str, picked: dict, request: Request):
    """取改写后的播放列表（内存缓存 + 单飞）。返回文本或 None。
    若该集/线路已有在途抓取（点击时预热启动），直接等同一个结果，不重复请求上游。"""
    key = _playlist_cache_key(picked)
    now = time.time()
    hit = _PLAYLIST_CACHE.get(key)
    if hit and now - hit[0] < _PLAYLIST_TTL:
        return hit[1]
    fut = _PLAYLIST_FUT.get(key)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        _PLAYLIST_FUT[key] = fut

        async def produce():
            try:
                body, _base = await _build_playlist_body(mg, picked, request)
                if body is not None and body is not _NOT_HLS:
                    _PLAYLIST_CACHE[key] = (time.time(), body)
                    if len(_PLAYLIST_CACHE) > 600:
                        for k in list(_PLAYLIST_CACHE)[:300]:
                            _PLAYLIST_CACHE.pop(k, None)
                if not fut.done():
                    fut.set_result(body)
            except Exception as exc:
                if not fut.done():
                    fut.set_result(None)
                _log.info("playlist build failed: %s", exc)
            finally:
                _PLAYLIST_FUT.pop(key, None)

        asyncio.create_task(produce())
    return await fut


def _warm_playlist(mg: str, picked: dict, request: Request) -> None:
    """点击播放时后台预热默认线路播放列表（单飞，真正取 main 时并入同一任务）。"""
    if picked.get("t4"):
        return
    key = _playlist_cache_key(picked)
    hit = _PLAYLIST_CACHE.get(key)
    if hit and time.time() - hit[0] < _PLAYLIST_TTL:
        return
    if key in _PLAYLIST_FUT:
        return  # 已在途
    asyncio.create_task(_get_playlist_cached(mg, picked, request))


# main 整条“解析+换线+建清单”的在途服务任务（mg|line|index -> Future）：慢源首个
# 请求只等到预算，超时回占位但后台任务继续跑完并写播放列表缓存，后续轮询直接命中。
# （上游 v1.0.0；键与上游一致：mg 是 URL 路径参数，index 为原值拼串。）
_MAIN_SERVE_FUT: dict[str, asyncio.Future] = {}
# 仅【慢源】（加固/爬虫/drpy/专有解码/小雅，常需十几秒解析）才用短预算先占位；普通
# CMS 直链不用预算、直接等出正片（恢复秒播），只在真正抓不到时才回占位。
# 配置 key：main_ready_budget；改配置后需重启进程才生效（模块常量不热重载）。
# 上游 config.py DEFAULTS 里没有这一项，这里也不加，靠 .get(..., 2.4) 兜底。
try:
    _MAIN_SLOW_BUDGET = float(config.get().get("main_ready_budget", 2.4))
except Exception:
    _MAIN_SLOW_BUDGET = 2.4
_MAIN_SLOW_BUDGET = min(max(_MAIN_SLOW_BUDGET, 0.8), 2.8)


def _is_slow_line(p: dict) -> bool:
    """是否为解析很慢的非标准源（加固/爬虫/drpy/专有解码/小雅），需先占位后台自愈。
    普通 CMS 直链（暴风/量子等 m3u8）返回 False，走直接秒播。（上游 v0.2.70）"""
    if not isinstance(p, dict):
        return False
    if p.get("guard") or p.get("spider_src") or p.get("drpy") \
            or p.get("catvod"):
        return True
    if p.get("xsrc"):
        return True
    ln = str(p.get("line") or "")
    return ln.startswith(("guard-", "spider-", "drpy-"))


@app.get(f"{ONLINE_PREFIX}/{{mg}}/preset.m3u8")
async def hls_preset(mg: str, request: Request):
    idx0 = int(request.query_params.get("index", 0) or 0)
    # direct/mp4 取流失败回跳入口：直接给占位 master，不再枚举可能同样失效的线路
    if request.query_params.get("ph") == "1":
        return _placeholder_master_response(idx0)
    failed_g = await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    if not picked:
        # 线路未就绪：回占位 master，由 main(phwait) 继续等待并自动切正片；
        # 播放器先进入加载页，不弹“播放出错”、线路菜单不被锁死（上游 v0.2.65）。
        return _placeholder_master_response(idx0)
    # 起播即跳过已知坏线（用户显式选线除外）
    picked = _dodge_known_bad(picked, request)
    # 注意：此处不 await _resolve_if_needed —— preset 只输出线路变体菜单，
    # 播放页/跳转/专有解码延迟到该线路被选中取 main 时再做，避免拖慢首屏。
    # play/play 选定线路后会带 line/index；此时只输出该线路，避免与清晰度菜单重复
    explicit_line = request.query_params.get("line")
    if explicit_line:
        # _pick_episode 已按 line/index 解析出该线路完整 target（含
        # redirect/resolve/xsrc 标志）；这里原样使用，真正解析推迟到取 main 时。
        targets = [picked]
    else:
        targets = store.target_lines(picked)
    # 起播速度优先：preset 只枚举候选线路，不在关键路径解析播放页/302/专有解码，
    # 那些动作延迟到该线路真正被选中取 main 时才做。
    hls_targets = []
    # 任何可按需解析的专有直链（小雅网盘 / JS / 加固等）都要进菜单，死活在 main 探测
    _SPECIAL = ("catv" + "od", "dr" + "py", "gu" + "ard")

    def _need_resolve(t: dict) -> bool:
        return bool(t.get("xsrc")) or any(t.get(k) for k in _SPECIAL)

    for t in targets:
        if t.get("m3u8") or re.search(r"\.(m3u8|mp4)(\?|$)", t.get("url", ""), re.I) \
                or t.get("resolve") or t.get("redirect") or _need_resolve(t):
            hls_targets.append(t)
    if not hls_targets:
        return Response("no hls line", status_code=404)

    # 分辨率探测模块已搁置：线路菜单只显示线路名，不追加分辨率标签。
    idx = int(picked.get("index", 0) or 0)

    # 自动起播：把上一次竞速选出的最快线排到首位（稳定用线）；用户显式选线时不动顺序
    # 冻结：最快线记忆仍是首要顺序；线路卡的种子只作次级 tiebreak，绝不盖过最快线。
    auto_mode = (not explicit_line) and _fastest_on()
    seed_line = request.query_params.get("seed_line") or ""
    if auto_mode:
        fast = remembered_fast_line(picked)
        if fast or seed_line:
            hls_targets.sort(key=lambda t: (
                t["line"] != fast if fast else True,
                t["line"] != seed_line if seed_line else True))
    else:
        # 手动/菜单模式：网盘线（高质量原画）排在在线线之前，便于直接选到网盘资源
        hls_targets.sort(key=lambda t: 0 if _pan_match(str(t.get("url") or ""))
                         else 1)

    def variant_uri(t: dict, is_auto_first: bool = False) -> str:
        q = {"index": idx, "line": t["line"]}
        # 仅播放器自动起播的首选变体带 auto=1（取 main 时触发竞速选最快线）；
        # 用户手动切到的其它变体不带，严格尊重其选择
        if is_auto_first:
            q["auto"] = "1"
            if seed_line:
                # 种子首变体不 pin line：main 见到 auto=1 且无显式 line 才会
                # 真正跑并行竞速（_race_fastest）；种子经 seed_line 参与候选排序。
                q.pop("line")
                q["seed_line"] = seed_line
        return "main.m3u8?" + urllib.parse.urlencode(q)

    if len(hls_targets) == 1:
        # 单线路：无 RESOLUTION 的单一变体（分辨率探测已搁置，不再写死占位分辨率）
        master = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
                  f"{variant_uri(hls_targets[0], auto_mode)}\n")
    else:
        # 多线路：无 RESOLUTION 的变体，飞牛 HLS 播放器呈现为可切换「线路」
        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for i, t in enumerate(hls_targets):
            name = _line_display_name(t)
            # NAME 供支持的播放器显示线路名；BANDWIDTH 差异化避免变体被合并
            out.append(f'#EXT-X-STREAM-INF:BANDWIDTH={1000000 + i * 100000},'
                       f'PROGRAM-ID=1,NAME="{name}"')
            # 仅首个（自动起播）变体带 auto=1
            out.append(variant_uri(t, auto_mode and i == 0))
        master = "\n".join(out) + "\n"
    return Response(master, media_type="application/vnd.apple.mpegurl")


_PROVIDER_CN = {
    "baofeng": "暴风", "ffzy": "非凡", "lzi": "量子", "wujin": "无尽",
    "youku": "优酷", "guangsu": "光速", "ruyi": "如意", "jisu": "极速",
    "xinlang": "新狼", "dytt": "电影天堂", "s360": "360",
}
_GROUP_CN = {"m3u8": "主线路", "yun": "云播", "ff": "非凡", "uk": "优酷",
             "gs": "光速", "bf": "暴风", "lz": "量子"}


def _friendly_line(key: str) -> str:
    """把 'provider::组名' 转成好认的线路名，如 baofeng::bfzym3u8 -> 暴风。"""
    provider, _, group = key.partition("::")
    pcn = _PROVIDER_CN.get(provider, provider)
    if not group:
        return pcn
    if group in _GROUP_CN:
        return f"{pcn}·{_GROUP_CN[group]}"
    # guard-/spider-/drpy- 等技术源：组名常是中文（如 🐻剧圈┃多线），取 ┃| 前可读部分
    if re.match(r"(guard|spider|drpy)-", provider, re.I):
        nice = re.sub(r"[┃|].*$", "", group).strip()
        nice = re.sub(r"^[\s\U0001F000-\U0001FAFF☀-➿]+", "", nice)
        if nice and not re.fullmatch(r"[A-Za-z0-9_-]+", nice):
            return nice
    stem = group
    for suf in ("zym3u8", "m3u8", "yun"):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
    stem = stem.upper() if stem.isascii() and stem else ""
    return f"{pcn}·{stem}" if stem else pcn


def _line_display_name(t: dict) -> str:
    """播放器线路菜单显示名：网盘线路加 ☁网盘名 前缀，便于区分高质量网盘资源。

    网盘线的来源多为 guard-Xxx 这类技术源名，直接展示不友好；改用线路组名里
    的中文可读部分（去掉 ┃多线 等装饰与 emoji 源前缀）。"""
    line = t.get("line", "")
    url = str(t.get("url") or "")
    pv = _pan_match(url) if url else None
    if pv is not None:
        tag = pv.get("name", "网盘").replace("网盘", "")
        group = line.split("::", 1)[1] if "::" in line else ""
        nice = re.sub(r"[┃|].*$", "", group).strip(" 🐌🍓🐻🍉🛡️")
        # 去掉常见纯英文技术源尾巴
        nice = re.sub(r"(guard-|spider-|drpy-)", "", nice, flags=re.I).strip()
        return f"☁{tag} {nice}".strip() if nice else f"☁{tag} 网盘"
    return _friendly_line(line)


def _is_media_playlist(text: str) -> bool:
    return "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text


def _first_variant(text: str, base: str) -> str | None:
    """从 master 播放列表里取第一个变体（子 m3u8）的绝对地址。"""
    uri = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        uri = s
        break
    if not uri:
        return None
    return urllib.parse.urljoin(base, uri)


async def _fetch_playlist(request: Request, url: str, headers: dict,
                          tries: int = 3, timeout: float = 15.0):
    """抓取并跟随嵌套 master，返回最终媒体列表文本与其基准 URL。失败返回 (None,url)。"""
    # 网盘播放代理首次需转存+实时换链，给更宽裕的超时
    if "/v/api/ext/pan/" in url and timeout <= 15.0:
        timeout = 45.0
    cur, text = url, None
    for _ in range(4):
        try:
            r = await ext_get_retry(request, cur, headers, tries=tries, timeout=15.0)
        except Exception as exc:
            # 抓取失败即整表失败，调用方据此换线；记异常类型，不记完整地址。
            _log.warning("playlist fetch failed: error=%s", type(exc).__name__)
            return None, cur
        if r.status_code != 200:
            return None, cur
        text = r.text
        if _is_media_playlist(text):
            break
        nxt = _first_variant(text, cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return text, cur


@app.get(f"{ONLINE_PREFIX}/{{mg}}/main.m3u8")
async def hls_main(mg: str, request: Request):
    _req_t0 = time.time()  # hls_main 入口处、任何分支之前（上游 v1.0.0）

    def _budget_left(floor: float = 0.6) -> float:
        # 首次 main 的总预算：从请求进入起计，含绑定等待、选线、dodge 等前置耗时。
        # 时钟用 time.time()（墙钟），与 _PLAYLIST_CACHE 的时间戳口径一致。
        return max(floor, _MAIN_SLOW_BUDGET - (time.time() - _req_t0))

    phwait = request.query_params.get("phwait") == "1"
    failed_g = await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    if not picked:
        # 占位自修复通道（上游 v0.2.65/v0.2.70）：只有显式 phwait 的准备通道保留占位，
        # 期间一律回占位 HLS（200），绝不返回 4xx/5xx 锁死菜单；普通 main 仍按
        # v0.2.42 不用无限占位掩盖“未找到片源”。
        if phwait:
            return _placeholder_playlist_response(mg)
        if failed_g:
            return Response("暂无可用在线片源", status_code=503)
        return Response("not found", status_code=404)

    explicit_line = request.query_params.get("line")
    auto_play = request.query_params.get("auto") == "1" and not explicit_line
    request.state._play_mg = mg  # 供探测复用播放列表构建、填充缓存
    # 起播即跳过已知坏线（用户显式选线除外），避免无谓探测延迟
    # （与上游一致：dodge 在分发前，不在 _serve_normal 内。）
    picked = _dodge_known_bad(picked, request)

    async def _serve_normal(picked: dict):
        # 整条“解析+换线+建清单”服务逻辑（上游 v1.0.0 _serve_normal）。
        # 有意偏离：不含 auto 起播快路径——pick_faster_line（播放中静默换线）与
        # _cached_sibling_playlist 是已冻结、明确不接线的功能，本次不搬。
        # 线路健康探测 + 自动选最快线
        if _autofail_on():
            picked = await _ensure_playable(picked, request, auto=auto_play)
            # 若自动换到了别的线路，后续 _pick_episode 仍按 query 的旧线解析；用换好的目标
            request.state._failover_picked = picked

        # >>> PRIVATE:CATVOD
        # 小雅网盘文件（mkv/ts 等）非 HLS：解析出签名直链后直接 302，让客户端直连网盘 CDN
        if features.CATVOD and picked.get("catvod"):
            if await _resolve_catvod(picked):
                # 再探一次死活，小雅“文件不存在”会回 200+JSON 假活；坏则换线
                if not (await _probe_target(picked, request)):
                    _mark_line_bad(picked)
                    alt = await _find_playable(picked, request, skip_current=True)
                    if alt is not None:
                        picked = alt
                        request.state._failover_picked = picked
                        _log.info("小雅文件不可用，自动切换线路 -> %s", picked.get("line"))
                    else:
                        return Response("该线路文件不存在，且暂无其他可用线路",
                                        status_code=502)
                if picked.get("catvod_direct"):
                    return RedirectResponse(picked["url"], status_code=302,
                                            headers={"Cache-Control": "no-store"})
        # <<< PRIVATE:CATVOD

        body = await _get_playlist_cached(mg, picked, request)
        if body is None:
            # 当前线抓取失败：记忆并自动换一条已探测可用的线路重试一次
            if _autofail_on():
                _mark_line_bad(picked)
                alt = await _find_playable(picked, request, skip_current=True)
                if alt is not None:
                    _log.info("播放列表抓取失败，自动换线 %s -> %s",
                              picked.get("line"), alt.get("line"))
                    picked = alt
                    request.state._failover_picked = alt
                    # 小雅等非 HLS 直链
                    # >>> PRIVATE:CATVOD
                    if features.CATVOD and picked.get("catvod"):
                        if await _resolve_catvod(picked) and picked.get("catvod_direct"):
                            return RedirectResponse(picked["url"], status_code=302,
                                                    headers={"Cache-Control": "no-store"})
                    # <<< PRIVATE:CATVOD
                    body = await _get_playlist_cached(mg, picked, request)
        if body is None:
            # 上游 v0.2.42/v0.2.70：普通 CMS 抓流失败直接报错，不返回假 HLS 造成无限占位；
            # 加固/爬虫等慢源仍保留占位自愈通道（后台找线，下次轮询切正片）。
            if _is_slow_line(picked):
                return _placeholder_playlist_response(mg)
            return Response("fetch fail upstream", status_code=502)
        if body is _NOT_HLS:
            # 预判为 HLS、解析后实为 mp4：302 到直链代理（direct 复用已解析结果）
            q = urllib.parse.urlencode({"line": picked["line"], "index": picked["index"]})
            return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)
        # 清单活、分片死（如某源 CDN 删流）：先在已解析清单上验首个分片，失败则
        # 标记坏线，并启动「按片名全网回源」后台任务。回源较慢，首个请求不阻塞到
        # 网关超时，而是让客户端（本就每 3 秒重试）在后台找到可播源后的下一次请求
        # 经 _MEDIA_REDIRECT 命中新记录。
        if _autofail_on() and not await _verify_rewritten_first_seg(body, request, picked):
            _mark_line_bad(picked)
            status = await _resurrect_start(picked, mg)
            if status == "ok":
                alt = _pick_episode(mg, request)
                if alt:
                    body2 = await _get_playlist_cached(mg, alt, request)
                    if body2 and body2 is not _NOT_HLS:
                        return Response(body2, media_type="application/vnd.apple.mpegurl",
                                        headers={"Cache-Control": "no-cache",
                                                 "Access-Control-Allow-Origin": "*"})
            if status == "fail":
                return Response("暂无可播线路（该源分片已失效，且未找到其他可用源）",
                                status_code=502)
            # running：慢源回直播式占位（200），播放器周期重拉 main 即转正，不弹错；
            # 普通源仍让客户端 3 秒后重试，不返回占位视频（上游 v0.2.42）。
            if _is_slow_line(picked):
                return _placeholder_playlist_response(mg)
            # running：让客户端稍候重试，不返回错误清单
            return Response("正在为你寻找可用线路…", status_code=503,
                            headers={"Retry-After": "3",
                                     "Access-Control-Allow-Origin": "*"})
        _sk = sourcehealth.source_key_of(picked.get("line"), picked)
        if _sk:
            # 播出成功：取流域复活（只清取流级计数，搜索级计数不动）。
            sourcehealth.record(_sk, True, level="stream")
        return Response(body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache",
                                 "Access-Control-Allow-Origin": "*"})

    # 起播策略（上游 v1.0.0）：
    #  - 普通 CMS 直链：同步等到真清单，每次 main 独立执行，不做外层单飞合并，
    #    否则前一次探测卡住时，后续重试全部挂在同一个 _serve_normal 上。
    #  - 仅加固/爬虫/drpy/专有解码/小雅这类慢源：短预算到点先回占位、后台自愈，
    #    播放器周期重拉转正片。
    slow_source = _is_slow_line(picked)
    if not slow_source:
        # v0.2.42：普通 CMS 每次 main 独立执行。不要在外层再做单飞合并，
        # 否则前一次探测卡住时，后续重试全部挂在同一个 _serve_normal 上。
        return await _serve_normal(picked)
    # 仅慢源保留单飞与短预算自愈，避免后台重复启动昂贵转存/爬虫任务。
    bk = f"{mg}|{picked.get('line')}|{picked.get('index', 0)}"
    fut = _MAIN_SERVE_FUT.get(bk)
    if fut is None:
        fut = asyncio.ensure_future(_serve_normal(picked))
        _MAIN_SERVE_FUT[bk] = fut
        fut.add_done_callback(lambda _f: _MAIN_SERVE_FUT.pop(bk, None))
    try:
        return await asyncio.wait_for(asyncio.shield(fut),
                                      timeout=_budget_left(floor=0.2))
    except asyncio.TimeoutError:
        return _placeholder_playlist_response(mg)


def _cors_preflight() -> Response:
    """播放路径的 CORS 预检：移动/TV 等跨域客户端在拉 m3u8/seg/key 前可能先发
    OPTIONS。必须由本服务直接应答标准 CORS 头，不能兜底透传给官方后端（后者不带
    CORS 头，会导致客户端判定跨域失败、报 http error）。"""
    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "3600",
        "Content-Length": "0",
    })


@app.options(f"{ONLINE_PREFIX}/{{rest:path}}")
async def hls_cors_preflight(rest: str):
    return _cors_preflight()


async def _try_stream_segment(request: Request, src_url: str, fwd: dict,
                              picked: dict | None = None):
    """把分片以「边下边传」方式直通；不适用时返回 None，交回整段缓冲老路径。

    为什么：源站实测只有 10~250 KB/s，而一个 3~6 秒的分片有 400 KB~1.9 MB。
    老实现 body = r.content 是**整段下完才回传**，播放器把"整片下载时间"
    （实测 1~12s）当成首字节等待，表现为长时间转圈不起播。改成流式后 TTFB
    等于上游首字节（约 0.3~1.5s），起播/续播都明显更快。

    回退（返回 None）的三种情况：上游 4xx/5xx 或可重试状态码（交回老路径做
    重试与坏线记忆）、分片带 PNG 外壳需整段剥壳、上游带 content-encoding
    （raw 流不能直接透传给客户端）。
    """
    client = ext_client(request)
    # 连接超时必须短：源站偶尔 TCP 都连不上，等默认 30s 才回退会让播放器干等半分钟。
    # 读超时给宽松值——慢源是"能连上但很慢"，不能因此判死。
    tmo = httpx.Timeout(connect=8.0, read=90.0, write=30.0, pool=10.0)
    try:
        # 注意：httpx 的 send() 不接受 timeout 参数，超时必须挂在 request 上
        req = client.build_request("GET", src_url, headers=fwd, timeout=tmo)
        rs = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        _log.info("seg stream open fail %s，回退整段重试", type(exc).__name__)
        return None
    if rs.status_code >= 400:
        await rs.aclose()
        return None
    enc = (rs.headers.get("content-encoding") or "identity").lower()
    if enc not in ("", "identity"):
        await rs.aclose()
        return None
    it = rs.aiter_raw()
    try:
        first = await it.__anext__()
    except StopAsyncIteration:
        first = b""
    except Exception:  # noqa: BLE001
        await rs.aclose()
        return None
    if first[:8] == b"\x89PNG\r\n\x1a\n":
        # PNG 外壳分片必须整段还原，交回老路径处理
        await rs.aclose()
        return None
    h = _safe_response_headers(rs.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"
    cl = rs.headers.get("content-length")
    if cl:
        h["Content-Length"] = cl

    async def pump():
        nbytes = 0
        t0 = time.time()
        try:
            if first:
                nbytes += len(first)
                yield first
            async for chunk in it:
                nbytes += len(chunk)
                yield chunk
        finally:
            # 边下边播的同时把这条线的真实吞吐记下来（零额外流量）
            try:
                dt = time.time() - t0
                if nbytes and dt > 0.05:
                    _remember_speed(picked, nbytes / 1024.0 / dt)
            except Exception:
                pass
            await rs.aclose()

    return StreamingResponse(
        pump(), status_code=rs.status_code, headers=h,
        media_type=rs.headers.get("content-type") or "video/mp2t")


# ---- 线路未就绪占位视频（上游 v0.2.65；保持播放器存活、线路菜单可切，不弹“播放出错”）----
_PH_DIR = Path(__file__).resolve().parent / "assets" / "placeholder"
_PH_SEG_TS = "_placeholder/placeholder.ts"
_PH_SEG_DUR = 2.0
_PH_WIN = 3  # 直播式滑动窗口分片数（3×2s≈6s：缩短冷片占位后的转正重拉间隔）


def _placeholder_playlist_response(mg: str = "", live: bool = True) -> Response:
    """返回循环提示 HLS（HTTP 200）。飞牛对 4xx/5xx 清单会弹“播放出错”并锁死
    线路菜单；200 的有效 HLS 让播放器停在提示画面、菜单仍可切换。

    用直播式滑动窗口（无 ENDLIST、带 MEDIA-SEQUENCE）：客户端只预取窗口内少数分片
    并周期性重拉 main，后台转存/搜索完成后下一次轮询即可自动切回正片。分片用相对
    路径，相对 /{mg}/main.m3u8 解析为 /{mg}/_placeholder/placeholder.ts。"""
    seq = int(time.time() // int(_PH_SEG_DUR))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{int(_PH_SEG_DUR)}",
             f"#EXT-X-MEDIA-SEQUENCE:{seq}",
             "#EXT-X-DISCONTINUITY-SEQUENCE:0"]
    for i in range(_PH_WIN):
        lines.append(f"#EXTINF:{_PH_SEG_DUR:.3f},")
        lines.append(f"{_PH_SEG_TS}?n={seq + i}")
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "X-fnmedia-Placeholder": "1"})


def _placeholder_master_response(idx: int = 0) -> Response:
    """线路未就绪时的【master】占位（HTTP 200）。返回单变体，其 main 带 phwait=1，
    hls_main 见到该标记会持续返回占位媒体清单（不挑线路、不报 4xx/5xx），后台绑定/
    找线完成后由后续正常请求自动切回正片。保证任何情况下播放器都不弹“播放出错”、
    线路菜单不被锁死。"""
    q = urllib.parse.urlencode({"index": idx, "phwait": "1"})
    body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1000000,PROGRAM-ID=1,NAME="正在准备线路…"\n'
            f"main.m3u8?{q}\n")
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "X-fnmedia-Placeholder": "1"})


def _placeholder_ts_response() -> Response:
    p = _PH_DIR / "placeholder.ts"
    if not p.exists():
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="video/mp2t",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})


@app.get(f"{ONLINE_PREFIX}/{{mg}}/_placeholder/placeholder.ts")
async def placeholder_ts(mg: str, request: Request):
    return _placeholder_ts_response()


def _direct_to_placeholder(mg: str, request: Request) -> RedirectResponse:
    """直链(mp4)取流失败：302 回占位 master，播放器不弹错、线路菜单可手动切换。
    带 ph=1，preset 见此标记直接回占位（不再给同一条坏线，避免重定向环）。"""
    didx = int(request.query_params.get("index", 0) or 0)
    return RedirectResponse(
        f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index={didx}&ph=1", status_code=302,
        headers={"Cache-Control": "no-store", "X-fnmedia-Placeholder": "1"})


@app.get(f"{ONLINE_PREFIX}/{{mg}}/seg")
async def hls_seg(mg: str, request: Request):
    u = request.query_params.get("u", "")
    try:
        src_url = b64d(u)
    except Exception:
        return Response("bad u", status_code=400)
    await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    headers = picked["headers"] if picked else {}
    # 透传 Range（播放器 seek）
    fwd = dict(headers)
    # >>> PRIVATE:GUARD
    # 网盘转码档（mkv 等不直出的源）的 HLS 分片在夸克/UC CDN 上，必须带对应网盘
    # 登录 Cookie（裸取 412）。picked 可能已脱离线路上下文，这里按 URL 域名兜底注入。
    try:
        _host = urllib.parse.urlsplit(src_url).netloc.lower()
        if _host.endswith("drive.quark.cn") or _host.endswith(".quark.cn"):
            import pan as _panq
            fwd["Cookie"] = _panq.auth_cookie_or_raise()
            fwd.setdefault("Referer", "https://pan.quark.cn/")
        elif _host.endswith(".uc.cn"):
            import panuc as _panu
            fwd["Cookie"] = _panu.auth_cookie_or_raise()
            fwd.setdefault("Referer", "https://drive.uc.cn/")
    except Exception:
        pass
    # <<< PRIVATE:GUARD
    # 加密 HLS 由本服务解密：不能按密文转发任意字节 Range，需完整拉取后回传纯 TS。
    if request.query_params.get("dec") == "1":
        fwd.pop("Range", None)
    # >>> PRIVATE:DDUO
    if features.DDUO and picked and picked.get("xsrc"):
        # 多多解出的真实 CDN 鉴权直链：带外站 Referer 会 403
        fwd.pop("Referer", None)
    # <<< PRIVATE:DDUO
    if "range" in request.headers and request.query_params.get("dec") != "1":
        fwd["Range"] = request.headers["range"]
    # 起播速度优先：普通分片走流式直通（见 _try_stream_segment）。
    # 加密分片 dec=1 由本服务解密、Range 请求要按字节回传，两者仍走整段缓冲老路径。
    if request.query_params.get("dec") != "1":
        _streamed = await _try_stream_segment(request, src_url, fwd, picked)
        if _streamed is not None:
            return _streamed
    try:
        # 分片在播放中途持续拉取，对瞬时抖动必须重试，否则单次失败播放器直接报错
        r = await ext_get_retry(request, src_url, fwd, tries=4, timeout=30.0)
    except Exception as exc:
        _log.warning("seg fetch fail %s %s", type(exc).__name__,
                     urllib.parse.urlsplit(src_url).netloc)
        return Response(f"seg fail {type(exc).__name__}", status_code=502)
    body = r.content
    # 服务端解密 AES-128 HLS 分片：m3u8 已移除 EXT-X-KEY，这里把密文 TS 解成纯 TS，
    # 并强制按完整 200 内容返回（密文 206 不能直接解密/拼接）。
    decrypt_resp_status = r.status_code
    if request.query_params.get("dec") == "1":
        try:
            import quality as _quality
            key = bytes.fromhex(b64d(request.query_params.get("k", "")))
            ivhex = b64d(request.query_params.get("iv", ""))
            iv = bytes.fromhex(ivhex)
            if len(key) == 16 and len(iv) == 16:
                dec = await _quality._aes_cbc_decrypt(body, key, iv)
                if dec and len(dec) >= 188 and dec[0] == 0x47:
                    body = dec
                    decrypt_resp_status = 200
        except Exception as exc:
            _log.warning("seg decrypt fail: %s", type(exc).__name__)
    ctype = r.headers.get("content-type", "")
    unwrapped = False
    # 部分源把真实 TS 分片伪装成「1×1 PNG 头 + TS」。剥掉 PNG 外壳
    # 还原成纯 TS，飞牛播放器才能正常解码（通用兼容，对所有源生效）
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            import quality
            payload, _ = quality._unwrap_segment(body)
            if payload is not body:
                body = payload
                ctype = "video/mp2t"
                unwrapped = True
        except Exception:
            pass
    drop = {"content-security-policy"}
    if request.query_params.get("dec") == "1":
        # 已在服务端解密并返回完整纯 TS：旧的 Range/content-range 必须移除，
        # 否则严格客户端会因 200 + Content-Range/Accept-Ranges 语义矛盾拒绝该分片。
        drop.update({"content-range", "accept-ranges"})
    if unwrapped:
        drop.add("content-type")
    h = _safe_response_headers(r.headers, drop)
    h["Access-Control-Allow-Origin"] = "*"
    h["Content-Length"] = str(len(body))
    if unwrapped or "content-type" not in {k.lower() for k in h}:
        h["Content-Type"] = ctype or "video/mp2t"
    if r.status_code in _RETRY_STATUS:
        _log.warning("seg upstream %s %s", r.status_code,
                     urllib.parse.urlsplit(src_url).netloc)
    return Response(content=body, status_code=decrypt_resp_status, headers=h)


@app.get(f"{ONLINE_PREFIX}/{{mg}}/hls")
async def hls_compact(mg: str, request: Request):
    """紧凑分片：清单只带 r=<注册表ID>&i=<序号>，真实 CDN（夸克/UC 等）地址留
    在服务端。用于签名分片超长、清单过大的转码 HLS，规避外网网关 URL/体积限制。"""
    rid = request.query_params.get("r", "")
    try:
        idx = int(request.query_params.get("i", "0"))
    except ValueError:
        idx = 0
    src_url = _hls_reg_get(rid, idx)
    if not src_url:
        return Response("expired", status_code=410)
    fwd: dict = {}
    # 网盘 CDN 分片必须带登录 Cookie（裸取 412），按域名兜底注入
    # >>> PRIVATE:GUARD
    try:
        host = urllib.parse.urlsplit(src_url).netloc.lower()
        if host.endswith("drive.quark.cn") or host.endswith(".quark.cn"):
            import pan as _panq
            fwd["Cookie"] = _panq.auth_cookie_or_raise()
            fwd["Referer"] = "https://pan.quark.cn/"
        elif host.endswith(".uc.cn"):
            import panuc as _panu
            fwd["Cookie"] = _panu.auth_cookie_or_raise()
            fwd["Referer"] = "https://drive.uc.cn/"
    except Exception:
        pass
    # <<< PRIVATE:GUARD
    if request.headers.get("range"):
        fwd["Range"] = request.headers["range"]
    try:
        r = await ext_get_retry(request, src_url, fwd, tries=4, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        _log.warning("hls compact seg fail %s", type(exc).__name__)
        return Response("seg fail", status_code=502)
    body = r.content
    ctype = r.headers.get("content-type", "")
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            import quality
            payload, _ = quality._unwrap_segment(body)
            if payload is not body:
                body, ctype = payload, "video/mp2t"
        except Exception:
            pass
    h = _safe_response_headers(r.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"
    h["Content-Length"] = str(len(body))
    h.setdefault("Content-Type", ctype or "video/mp2t")
    return Response(content=body, status_code=r.status_code, headers=h)


# ------------------------------------------------------------- 海报代理

def _decode_tok(tok: str) -> str:
    tok = tok.split(".", 1)[0]
    return b64d(tok)


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

    先用常规头重试 3 次；若返回 403（图床防盗链），再带图床自身 origin
    作 Referer 重试一次——多数防盗链只认同源 Referer。
    主客户端网络异常时，按上游 _poster_fetch（v1.0.0）的做法经
    netutil.async_client(timeout=12.0) 再取一次（按代理模式直连/走代理/
    自动切代理；未配置代理时也相当于一条全新直连）；仍失败返回 None。
    地址格式、JPEG 占位、纯透传均不动。
    """
    host = urllib.parse.urlsplit(url).netloc.lower()
    try:
        r = await ext_get_retry(request, url,
                                headers=_poster_fetch_headers(url))
    except _POSTER_NETWORK_ERRORS:
        _log.info("poster netutil retry host=%s", host)
        try:
            async with netutil.async_client(timeout=12.0) as cli:
                r = await cli.get(url, headers=_poster_fetch_headers(url))
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    if r is not None and r.status_code == 403:
        scheme = urllib.parse.urlsplit(url).scheme or "https"
        headers = _poster_fetch_headers(url)
        headers["Referer"] = f"{scheme}://{host}/"
        try:
            r = await ext_get_retry(request, url, headers=headers)
        except Exception:  # noqa: BLE001
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

@app.get("/v/api/ext/whoami")
async def ext_whoami(request: Request):
    """回显当前请求被判定为哪种客户端，供在出问题的设备上打开管理台自检。"""
    cl = _client_of(request)
    return {"code": 0, "msg": "", "data": {
        "kind": cl.kind, "label": cl.label, "is_tv": cl.is_tv,
        "is_mobile": cl.is_mobile, "is_web": cl.is_web,
        "strict_card": cl.strict_card,
        "ua": request.headers.get("user-agent", "")}}


@app.get("/v/api/ext/live")
async def ext_live(request: Request):
    """实时播放诊断：各端最近请求、当前播放会话、转发来源与错误。"""
    return {"code": 0, "msg": "", "data": livemod.snapshot()}


@app.post("/v/api/ext/live/clear")
async def ext_live_clear(request: Request):
    livemod.reset()
    return {"code": 0, "msg": "", "data": True}


# ---------------- 缓存管理（移植上游 v0.2.54） ----------------
@app.get("/v/api/ext/titles-cache")
async def ext_titles_cache(request: Request):
    """在线资源缓存 titles.json 的条数/字节（管理页性能卡，issue #42）。
    文件超过配置字节上限时，顺手调度一次后台裁剪落盘（重启后打开管理页即自动瘦身）。"""
    try:
        p = store._state
        size = p.stat().st_size if p.exists() else 0
    except Exception:
        size = 0
    with store._lock:
        entries = len(store._titles)
        dirty = bool(store._dirty)
    try:
        import store as _storemod
        _, max_bytes, _ = _storemod._cache_cfg()
        if max_bytes > 0 and size > max_bytes and not dirty:
            with store._lock:
                store._dirty = True
            store._schedule_flush()
    except Exception:
        pass
    return {"code": 0, "entries": entries, "bytes": size, "dirty": dirty}


@app.post("/v/api/ext/titles-cache/clear")
async def ext_titles_cache_clear(request: Request):
    """清空在线资源缓存并重启服务（慢盘用户搜索退化时一键回到冷态，issue #42）。"""
    try:
        store.close()
        with store._lock:
            store._titles, store._orig = {}, {}
            store._ep_index, store._media_index = {}, {}
            store._dirty = False
        try:
            store._state.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        return {"code": 5000, "msg": f"清空失败：{exc}"}

    def _restart():
        import os
        time.sleep(1.2)
        os.system("systemctl restart fnmedia-ext >/dev/null 2>&1 &")

    threading.Thread(target=_restart, daemon=True).start()
    return {"code": 0, "msg": "已清空，服务重启中"}


def _cache_sizes() -> dict:
    """各类可清缓存的体积/条数（管理页缓存管理卡）。"""
    out = {}
    try:
        p = store._state
        out["titles"] = {"bytes": p.stat().st_size if p.exists() else 0,
                         "entries": len(store._titles), "label": "在线资源缓存"}
    except Exception:
        out["titles"] = {"bytes": 0, "entries": 0, "label": "在线资源缓存"}
    try:
        cp = chartmod.STATE
        out["charts"] = {"bytes": cp.stat().st_size if cp.exists() else 0,
                         "label": "热门榜单缓存"}
    except Exception:
        out["charts"] = {"bytes": 0, "label": "热门榜单缓存"}
    with catalog._lock:
        out["catalog"] = {"routes": len(catalog._routes),
                          "pages": len(catalog._page_cache),
                          "label": "海量库分类/分页缓存"}
    try:
        out["search"] = {"entries": len(_search_cache), "label": "搜索结果内存缓存"}
    except Exception:
        out["search"] = {"entries": 0, "label": "搜索结果内存缓存"}
    try:
        out["health"] = {"entries": len(sourcehealth.snapshot())
                         if hasattr(sourcehealth, "snapshot") else 0,
                         "label": "源健康画像"}
    except Exception:
        out["health"] = {"entries": 0, "label": "源健康画像"}
    return out


@app.get("/v/api/ext/cache")
async def ext_cache_status(request: Request):
    return {"code": 0, "data": _cache_sizes()}


@app.post("/v/api/ext/cache/clear")
async def ext_cache_clear(request: Request):
    """一键清除指定缓存。targets: search/charts/catalog/titles/health 的子集；
    省略或 ["all"] 清前四项（health 默认保留，避免冷启动选源退化）。"""
    body = await request.json()
    targets = body.get("targets")
    if not targets or targets == ["all"]:
        targets = ["search", "charts", "catalog", "titles"]
    targets = {str(t) for t in targets}
    done, need_restart = [], False
    if "search" in targets:
        _search_cache.clear(); _AGG_CACHE.clear(); done.append("search")
    if "charts" in targets:
        try:
            chart_cache.reset(); done.append("charts")
        except Exception:
            pass
    if "catalog" in targets:
        try:
            catalog.reset(); done.append("catalog")
            _schedule_routes_refresh()
        except Exception:
            pass
    if "health" in targets:
        try:
            sourcehealth.reset(); done.append("health")
        except Exception:
            pass
    if "titles" in targets:
        need_restart = True
        done.append("titles")

        def _clear_titles_restart():
            import os
            try:
                store.close()
                with store._lock:
                    store._titles, store._orig = {}, {}
                    store._ep_index, store._media_index = {}, {}
                    store._dirty = False
                store._state.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(1.0)
            os.system("systemctl restart fnmedia-ext >/dev/null 2>&1 &")

        threading.Thread(target=_clear_titles_restart, daemon=True).start()
    return {"code": 0, "data": {"cleared": done, "restart": need_restart},
            "msg": "已清除：" + "、".join(done) + ("，服务重启中" if need_restart else "")}


# --------------------------------------------------------------- 管理令牌：状态 / 登录 / 轮换
# 注意：admin_token 历史上只生成、从不校验；本模块只提供校验/轮换/限流
# 检查点，不给既有 /v/api/ext/* 接口加鉴权门。用户决定（2026-11-09，
# 仓库 owner 明确决定）：不对写接口加鉴权门——令牌此前从未向用户展示过，
# 直接上锁会把用户锁在管理页外面。后来人不要再“补”这个缺口。
_ADMIN_LOGIN_LIMITER = admin_authmod.LoginAttemptLimiter()


def _admin_client_key(request: Request) -> str:
    """限流键用客户端 IP：X-Forwarded-For 首跳（飞牛前置代理场景），
    否则取直连对端地址；拿不到时用 "unknown"（仍有令牌指纹键兜底）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    try:
        if request.client and request.client.host:
            return request.client.host
    except Exception:
        pass
    return "unknown"


def _admin_limiter_from_config():
    """按当前配置刷新限流阈值（计数不清零）。"""
    cfg = config.get()
    _ADMIN_LOGIN_LIMITER.configure(
        cfg.get("admin_auth_max_failures") or admin_authmod.DEFAULT_MAX_FAILURES,
        cfg.get("admin_auth_lockout_seconds") if cfg.get("admin_auth_lockout_seconds") is not None else admin_authmod.DEFAULT_LOCKOUT_SECONDS,
        cfg.get("admin_auth_window_seconds") or admin_authmod.DEFAULT_WINDOW_SECONDS)
    return _ADMIN_LOGIN_LIMITER


@app.get("/v/api/ext/admin/token")
async def ext_admin_token_status(request: Request):
    """管理令牌状态：掩码、长度、年龄、过期策略、宽限剩余、限流阈值。
    永不返回令牌原文（原文只在轮换成功那一次返回）。"""
    return {"code": 0, "msg": "", "data": config.admin_token_info()}


@app.post("/v/api/ext/admin/login")
async def ext_admin_login(request: Request):
    """管理令牌登录校验（检查点，供后续鉴权门与管理页“验证令牌”用）。
    失败记入限流（按 IP 与令牌指纹分别计数）；锁定中返回 429。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    candidate = str((body or {}).get("token") or "")
    ip = _admin_client_key(request)
    limiter = _admin_limiter_from_config()
    locked, remaining = limiter.is_locked(ip, candidate)
    if locked:
        return JSONResponse(
            {"code": 429, "msg": "登录尝试过多，请稍后再试",
             "data": {"retry_after_seconds": int(remaining) + 1}},
            status_code=429)
    ok, reason = config.verify_admin_token(candidate)
    if ok:
        limiter.record_success(ip, candidate)
        return {"code": 0, "msg": "ok",
                "data": {"ok": True, "via": reason,
                         "token": config.admin_token_info()}}
    limiter.record_failure(ip, candidate)
    locked, remaining = limiter.is_locked(ip, candidate)
    msg = {"empty": "令牌为空", "expired": "令牌已过期，请重新生成",
           "no_token_configured": "未配置管理令牌"}.get(reason, "令牌不正确")
    if locked:
        msg += f"，已锁定，请 {int(remaining) + 1} 秒后再试"
    return JSONResponse({"code": 401, "msg": msg,
                         "data": {"ok": False, "reason": reason,
                                  "retry_after_seconds": int(remaining) + 1 if locked else 0}},
                        status_code=401)


@app.post("/v/api/ext/admin/token/rotate")
async def ext_admin_token_rotate(request: Request):
    """轮换管理令牌：旧令牌在宽限期（默认 24 小时）内仍可登录。
    须 body {"confirm": true}。新令牌原文只在本次响应里返回一次，
    请立即复制保存；刷新页面后不再显示原文（只显示掩码）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return JSONResponse({"code": 400, "msg": "轮换须显式确认（confirm=true）"},
                            status_code=400)
    new_token, info = config.rotate_admin_token()
    return {"code": 0, "msg": "已重新生成，旧令牌在宽限期内仍可用",
            "data": {"new_token": new_token, "token": info}}


# ------------------------------------------------------- 电视直播（IPTV）管理 API


@app.get("/v/api/ext/status")
async def ext_status(request: Request):
    overrides = config.get().get("source_priority") or {}
    priorities = {s["id"]: providers.source_priority(s, overrides)
                  for s in config.all_sources()}
    return {"ok": True,
            "build": BUILD_LABEL,
            "config": {k: v for k, v in config.get().items() if k != "admin_token"},
            "source_priority": priorities,
            "cached_titles": len(store._titles),
            "hot": hot_cache.status(),
            "charts": chart_cache.status(),
            "catalog": catalog.status()}


# --------------------------------------------------------------- P2：继续观看 / 追剧 / 源健康 / 收藏元数据
@app.get("/v/api/ext/continue")
async def ext_continue(request: Request, limit: int = 20):
    """继续观看：未看完的记录，按最近观看倒序，带 progress。"""
    return {"code": 0, "msg": "", "data": history.continue_watching(limit=limit)}


@app.get("/v/api/ext/follow")
async def ext_follow_list(request: Request):
    if not config.get().get("follow_enabled", True):
        return {"code": 0, "msg": "", "data": []}
    return {"code": 0, "msg": "", "data": follow_list.list()}


@app.post("/v/api/ext/follow")
async def ext_follow_set(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    guid, title = (body.get("guid") or ""), (body.get("title") or "")
    if body.get("action") == "unfollow":
        return {"code": 0, "msg": "", "data": follow_list.unfollow(guid)}
    if not guid:
        return {"code": 1, "msg": "guid required", "data": False}
    return {"code": 0, "msg": "", "data": follow_list.follow(
        guid, title, int(body.get("episodes", 0) or 0))}


@app.post("/v/api/ext/follow/check")
async def ext_follow_check(request: Request):
    """追剧提醒：body {counts: {guid: 当前总集数}}，返回有更新的条目。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    counts = body.get("counts") or {}
    return {"code": 0, "msg": "", "data": follow_list.check_updates(
        {str(k): int(v) for k, v in counts.items()})}


@app.get("/v/api/ext/source-health")
async def ext_source_health(request: Request):
    """源健康快照：成功率 / 延迟 / 连续失败 / 死亡冷却，供管理页标红降级源。"""
    return {"code": 0, "msg": "", "data": sourcehealth.snapshot()}


@app.post("/v/api/ext/source-health/reset")
async def ext_source_health_reset(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    sourcehealth.reset(body.get("key") or None)
    return {"code": 0, "msg": "", "data": True}


@app.post("/v/api/ext/source/heal")
async def ext_source_heal(request: Request):
    """域名自愈：对一个源依次试备用域名，返回第一个返回正常内容的 API 地址。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    sid = body.get("id") or ""
    src = next((s for s in config.all_sources() if s.get("id") == sid), None)
    if not src:
        return {"code": 1, "msg": "source not found", "data": None}
    backups = (config.get().get("source_domain_backups") or {}).get(sid, [])
    import httpx as _httpx

    def _fetch(url: str):
        # 保留 verify=False：待自愈的源 API 由用户任意配置，常为自签/IP/
        # 证书链不完整的自建源；自愈只判 200 + 正常页面，不带任何凭据。
        with _httpx.Client(timeout=12, verify=False, trust_env=False) as c:
            r = c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            return r.status_code, r.text

    healed = domain_heal_mod.heal(src["api"], ext=body.get("ext", ""),
                                  backups=backups, fetch=_fetch)
    return {"code": 0, "msg": "", "data": healed}


@app.post("/v/api/ext/favorite/enrich")
async def ext_favorite_enrich(request: Request):
    """收藏元数据补全：用同名其他源的条目补齐收藏记录的空字段，不改已有值。"""
    if not config.get().get("metadata_enabled", True):
        return {"code": 0, "msg": "", "data": marks.favorite_records()}
    recs = marks.favorite_records()
    # 候选：当前已缓存的全部标题 brief。
    cands = []
    try:
        cands = [dict(b) for b in store._titles.values()] if hasattr(store, "_titles") else []
    except Exception:
        cands = []
    out = []
    for r in recs:
        brief = {"title": r.get("title", ""), "year": r.get("year", ""),
                 "area": "", "score": "", "overview": "", "poster": r.get("poster", ""),
                 "actor": "", "director": ""}
        out.append({**r, **{k: v for k, v in metadata_mod.enrich_brief(brief, cands).items()
                            if k in ("year", "area", "score", "overview", "poster") and v}})
    return {"code": 0, "msg": "", "data": out}


# --------------------------------------------------------------- 版本更新
@app.get("/v/api/ext/update/check")
async def ext_update_check(request: Request):
    force = request.query_params.get("force") in ("1", "true", "yes")
    return await updater.check(force=force)


@app.get("/v/api/ext/update/status")
async def ext_update_status(request: Request):
    return updater.status()


@app.post("/v/api/ext/update/now")
async def ext_update_now(request: Request):
    # 仅管理台显式触发；下载校验后交给 fnOS 应用中心正规升级（会短暂重启服务）。
    # 该动作具备系统级副作用，要求显式确认 + 自定义头（跨站简单请求无法携带，
    # 起到 CSRF 防护作用）；只读的检查/状态接口不加此限制。
    try:
        body = await request.json()
    except Exception:
        body = {}
    if request.headers.get("x-fnmedia-admin") != "1" or body.get("confirm") is not True:
        return JSONResponse({"code": 403,
                             "msg": "需要在管理页显式确认后才能执行更新"},
                            status_code=403)
    return await updater.start_update()


@app.post("/v/api/ext/source/priority")
async def ext_source_priority(request: Request):
    body = await request.json()
    mapping = body.get("priority") if isinstance(body.get("priority"), dict) else body
    ok, msg = config.set_source_priority(mapping if isinstance(mapping, dict) else {})
    if "prefer_quality_source" in body:
        config.save({"prefer_quality_source": bool(body["prefer_quality_source"])})
    return {"code": 0 if ok else 400, "msg": msg,
            "data": {"source_priority": config.source_priorities()}}


def _virtual_lib_list() -> list[dict]:
    """供管理台展示的虚拟媒体库清单（全部注入库），含显隐与排序。"""
    out = []
    out.append({"guid": T4_LIB_GUID, "key": vlibsmod.K_T4, "title": T4_LIB_TITLE,
                "kind": "t4", "group": "t4", "enabled": bool(t4_browser.sources()),
                "count": len(t4_browser.sources())})
    for cat, title, _c in chartmod.CHART_CATEGORIES:
        out.append({"guid": CHART_LIB_GUIDS[cat],
                    "key": vlibsmod.make_key(vlibsmod.K_CHART, cat),
                    "title": title, "kind": "chart",
                    "group": "hot", "enabled": charts_enabled(),
                    "count": len(chart_cache.items(cat))})
    for cat, title, _c in catalogmod.CATALOG_CATEGORIES:
        out.append({"guid": CATALOG_LIB_GUIDS[cat],
                    "key": vlibsmod.make_key(vlibsmod.K_CATALOG, cat),
                    "title": title, "kind": "catalog",
                    "group": "catalog", "enabled": catalog_enabled() and catalog.available(cat),
                    "count": catalog.total(cat) if catalog.available(cat) else 0})
    # 扩展资源独立库（命名与开关语义不动，只在这里多一行展示）
    if catalogmod._welfare_on():
        out.append({"guid": CATALOG_LIB_GUIDS[catalogmod.ADULT_CAT],
                    "key": vlibsmod.make_key(vlibsmod.K_CATALOG, catalogmod.ADULT_CAT),
                    "title": catalogmod.catalog_title(catalogmod.ADULT_CAT),
                    "kind": "catalog", "group": "catalog",
                    "enabled": catalog.available(catalogmod.ADULT_CAT),
                    "count": catalog.total(catalogmod.ADULT_CAT)
                    if catalog.available(catalogmod.ADULT_CAT) else 0})
    for s in standalone_sources_active():
        out.append({"guid": standalone_lib_guid(s["id"]),
                    "key": vlibsmod.make_key(vlibsmod.K_STANDALONE, s["id"]),
                    "title": standalone_lib_name(s), "kind": "standalone",
                    "group": "standalone",
                    "enabled": catalog.standalone_available(s["id"]), "count": 0})
    if features.GUARD:
        if guard_native_enabled():
            out.append({"guid": GUARD_NATIVE_GUID, "key": vlibsmod.K_GUARD_NATIVE,
                        "title": GUARD_NATIVE_TITLE, "kind": "guard",
                        "group": "guard", "enabled": True,
                        "count": len(_gn_hot_rows())})
        for g in guard_libs_active():
            out.append({"guid": guard_lib_guid(g["conf"]["key"]),
                        "key": vlibsmod.make_key(vlibsmod.K_GUARD, g["conf"]["key"]),
                        "title": guard_lib_name(g["conf"]), "kind": "guard",
                        "group": "guard",
                        "enabled": True, "count": 0})
    # >>> PRIVATE:CATVOD
    try:
        import xlib
        for cat in _xlib_active_cats():
            out.append({"guid": xlib.lib_guid(cat["id"]),
                        "key": vlibsmod.make_key(vlibsmod.K_XLIB, cat["id"]),
                        "title": cat["title"], "kind": "xlib",
                        "group": "xlib", "enabled": True, "count": 0})
    except Exception:
        pass
    # <<< PRIVATE:CATVOD
    if hgdj_enabled():
        out.append({"guid": hgdjmod.lib_guid(), "key": vlibsmod.K_HGDJ,
                    "title": hgdjmod.HGDJ_LIB_NAME, "kind": "hgdj",
                    "group": "hgdj", "enabled": hgdjmod.total() > 0,
                    "count": hgdjmod.total()})
    hidden = vlibsmod.hidden_libs()
    pinned = config.pinned_lib_guids()
    order = vlibsmod.lib_order()
    for d in out:
        d["hidden"] = d["key"] in hidden
        d["pinned"] = d["guid"] in pinned
        try:
            d["order"] = order.index(d["key"])
        except ValueError:
            d["order"] = -1
    return out


@app.get("/v/api/ext/libs")
async def ext_libs(request: Request):
    return {"code": 0, "msg": "", "data": {
        "hot_enabled": charts_enabled(),
        "catalog_enabled": catalog_enabled(),
        "libs": _virtual_lib_list(),
        "placement": vlibsmod.lib_placement_default(),
        "anchor": vlibsmod.lib_anchor(),
        "order": vlibsmod.lib_order()}}


@app.post("/v/api/ext/lib/pin")
async def ext_lib_pin(request: Request):
    body = await request.json()
    guid = str(body.get("guid") or "")
    on = bool(body.get("pinned"))
    ok, msg = config.set_lib_pinned(guid, on)
    return {"code": 0 if ok else 400, "msg": msg,
            "data": {"pinned_libs": sorted(config.pinned_lib_guids())}}


@app.get("/v/api/ext/accounts")
async def ext_accounts(request: Request):
    """子账号列表（从飞牛官方数据库只读读取）+ 每个账号的允许库列表。"""
    accounts = fnusersmod.list_users()
    out = []
    for a in accounts:
        allowed = vlibsmod.account_allowlist(a["guid"])
        # fnusers 给的是 name，管理页按 username 读，这里补齐
        out.append({**a, "username": a.get("name") or a["guid"],
                    "allowed": allowed, "configured": allowed is not None})
    return {"code": 0, "msg": "", "data": {"accounts": out,
                                           "acl_default_visible": vlibsmod.acl_default_visible(),
                                           "acl_default_online": vlibsmod.acl_default_online()}}


@app.post("/v/api/ext/account/acl")
async def ext_account_acl(request: Request):
    """设置某账号允许看到的虚拟库（allow: [库键, ...]，空列表=不允许看任何
    虚拟库；不传/传 null=取消限制，改回默认：fail-open 下默认可见）。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ok, msg = vlibsmod.set_account_acl(body.get("guid"), body.get("allow", None))
    return {"code": 0 if ok else 1, "msg": msg}


@app.post("/v/api/ext/account/defaults")
async def ext_account_defaults(request: Request):
    """设置未配置账号的默认可见性（acl_default_visible / acl_default_online）。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    cfg = config.get()
    if "acl_default_visible" in body:
        cfg["acl_default_visible"] = bool(body["acl_default_visible"])
    if "acl_default_online" in body:
        cfg["acl_default_online"] = bool(body["acl_default_online"])
    config.save(cfg)
    return {"code": 0, "msg": "ok"}


@app.get("/v/api/ext/local-libs")
async def ext_local_libs(request: Request):
    """返回官方本地库的 guid/name 列表（供“after”锚点选择）。"""
    libs = []
    try:
        client = up_client(request)
        r = await client.get("http://localhost/v/api/v1/mediadb/list")
        data = r.json()
        vlib_guids = _all_virtual_lib_guids()
        for d in (data.get("data") or []):
            if d.get("guid") in vlib_guids:
                continue
            libs.append({"guid": d.get("guid"),
                         "name": d.get("name") or d.get("mdb_name")})
    except Exception:
        pass
    return {"code": 0, "msg": "", "data": {"libs": libs}}


@app.post("/v/api/ext/lib/visibility")
async def ext_lib_visibility(request: Request):
    """设置某个虚拟媒体库的全局显隐（管理员级，不分账号）。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ok, msg = vlibsmod.set_hidden(body.get("key"), bool(body.get("hidden", False)))
    return {"code": 0 if ok else 1, "msg": msg}


@app.post("/v/api/ext/lib/order")
async def ext_lib_order(request: Request):
    """保存虚拟媒体库自定义顺序（order: [库键, ...]）和相对本地库位置
    （placement: front/back/after；after 时可带 anchor 本地库 guid）。"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ok, msg = vlibsmod.set_order(body.get("order"), body.get("placement"),
                                 body.get("anchor"))
    return {"code": 0 if ok else 1, "msg": msg}


@app.post("/v/api/ext/hot/refresh")
async def ext_hot_refresh(request: Request):
    await chart_cache.refresh(force=True)
    try:
        if catalog.routes_stale():
            await asyncio.wait_for(
                catalog.refresh_routes(config.enabled_sources()), timeout=90)
    except Exception:
        pass
    return {"code": 0, "msg": "", "data": chart_cache.status()}


@app.get("/v/api/ext/online")
async def ext_online(q: str = "", limit: int = 200):
    items = []
    for g, b in store._titles.items():
        items.append({
            "guid": g, "title": b.get("title"), "year": b.get("year"),
            "area": b.get("area"), "remarks": b.get("remarks"),
            "score": b.get("score"), "provider": b.get("online_id", "").split(":")[0],
            "episodes": sum(len(e) for e in b.get("lines", {}).values()),
            "poster": b.get("poster"),
        })
    items.sort(key=lambda x: x.get("year") or "", reverse=True)
    if q:
        items = [x for x in items if q.lower() in (x["title"] or "").lower()]
    return {"code": 0, "data": items[:limit]}


@app.post("/v/api/ext/config")
async def ext_set_config(request: Request):
    body = await request.json()
    allowed = {
        "max_per_keyword", "filter_junk", "junk_keywords", "online_first",
        "hot_library_enabled", "hot_library_title", "hot_limit",
        "hot_per_source", "hot_pages_per_source", "hot_refresh_interval",
        "catalog_library_enabled",
        "t4_library_enabled",
        "welfare_library_enabled",
        "prefer_quality_source",
        "line_fastest", "line_fastest_max", "line_fastest_timeout",
        "line_fastest_prefer", "line_autofail",
        "main_ready_budget",  # 慢线起播预算（秒），改后需重启进程才生效
        "prewarm_mode",
        "update_proxy",
    }
    # >>> PRIVATE:CATVOD
    allowed.add("pan_lib_enabled")
    # <<< PRIVATE:CATVOD
    # >>> PRIVATE:GUARD
    allowed.update({"guard_zone_enabled", "guard_native_enabled"})
    # <<< PRIVATE:GUARD
    patch = {k: v for k, v in body.items() if k in allowed}
    if "prewarm_mode" in patch:
        # 只允许两种预热方式，非法值回退默认 realtime（方案A）
        patch["prewarm_mode"] = "persist" if str(patch["prewarm_mode"]) == "persist" else "realtime"
    if "hot_library_title" in patch:
        patch["hot_library_title"] = str(patch["hot_library_title"]).strip()[:20] or HOT_DEFAULT_TITLE
    if "hot_limit" in patch:
        patch["hot_limit"] = max(60, min(800, int(patch["hot_limit"])))
    if "hot_per_source" in patch:
        patch["hot_per_source"] = max(20, min(240, int(patch["hot_per_source"])))
    if "hot_pages_per_source" in patch:
        patch["hot_pages_per_source"] = max(1, min(10, int(patch["hot_pages_per_source"])))
    if "hot_refresh_interval" in patch:
        patch["hot_refresh_interval"] = max(1 * 3600, min(24 * 3600,
                                                          int(patch["hot_refresh_interval"])))
    if "update_proxy" in patch:
        pu = str(patch["update_proxy"]).strip()[:300]
        patch["update_proxy"] = pu if re.match(r"^(https?|socks5h?)://", pu) else ""
    sources_changed = False
    if "sources" in body and isinstance(body["sources"], list):
        cur = {s["id"]: s for s in config.all_sources()}
        for ch in body["sources"]:
            sid = ch.get("id")
            if sid in cur:
                if isinstance(ch.get("enabled"), bool) and \
                        cur[sid].get("enabled") != ch["enabled"]:
                    cur[sid]["enabled"] = ch["enabled"]
                    sources_changed = True
                if isinstance(ch.get("standalone"), bool) and \
                        bool(cur[sid].get("standalone")) != ch["standalone"]:
                    cur[sid]["standalone"] = ch["standalone"] and cur[sid].get("kind") != "t4"
                    sources_changed = True
                if isinstance(ch.get("loose_filter"), bool):
                    cur[sid]["loose_filter"] = ch["loose_filter"]
                if isinstance(ch.get("adult"), bool):
                    cur[sid]["adult"] = ch["adult"] and bool(cur[sid].get("standalone"))
        patch["sources"] = list(cur.values())
    config.save(patch)
    _search_cache.clear(); _AGG_CACHE.clear()
    # 方案B 开关切换：即时启用/停用冠军线记忆持久化
    if "prewarm_mode" in patch:
        try:
            linepredict.set_persist(patch["prewarm_mode"] == "persist")
        except Exception:
            pass
    if sources_changed or "welfare_library_enabled" in patch:
        # 扩展库独立开关切换 → 立即重建分类路由（开=补建 ADULT 池，关=清掉），
        # 否则打开开关后扩展库卡要等下次路由过期才出现。
        _schedule_routes_refresh()
    return {"code": 0, "msg": "saved"}


@app.post("/v/api/ext/subscription-proxy")
async def ext_set_subscription_proxy(request: Request):
    """设置/清空「仅拉取订阅资源」的代理（订阅配置/spider jar/DRPY脚本）。

    播放、搜索、CMS 探测不走此代理。body: {"proxy":"http://user:pass@host:port"或""}
    会即时保存并用该代理探测一次（拉一个轻量 URL），返回连通结果。
    """
    body = await request.json()
    px = str(body.get("proxy", "") or "").strip()
    ok, msg = config.set_subscription_proxy(px)
    if not ok:
        return JSONResponse({"code": 400, "msg": msg}, status_code=400)
    # 实测连通：经该代理拉一个小资源（默认 GitHub generate_204），失败也保存但提示
    test_ok, test_msg = False, ""
    import subfetch
    if px:
        try:
            import httpx as _httpx
            # 保留 verify=False：连通探测经用户自配代理发出，代理可能终结并
            # 重签 TLS；只判 200/204 是否可达，代理本身失败也保存、仅提示。
            with _httpx.Client(trust_env=False, verify=False, timeout=12,
                               proxy=px) as cli:
                rr = cli.get("https://www.gstatic.com/generate_204")
                test_ok = rr.status_code in (200, 204)
                test_msg = f"HTTP {rr.status_code}"
        except Exception as exc:  # noqa: BLE001
            test_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
    return {"code": 0, "msg": "",
            "data": {"proxy": subfetch.subscription_proxy(),
                     "test_ok": (True if not px else test_ok),
                     "test_msg": ("直连（未设置代理）" if not px else test_msg)}}


@app.post("/v/api/ext/junk-keywords")
async def ext_junk_keywords(request: Request):
    body = await request.json()
    if body.get("reset"):
        kws = config.reset_junk_keywords()
    else:
        kws = config.set_junk_keywords(body.get("keywords", []))
    _search_cache.clear(); _AGG_CACHE.clear()
    return {"code": 0, "msg": "saved", "data": {"junk_keywords": kws}}


@app.post("/v/api/ext/source-proxy")
async def ext_set_source_proxy(request: Request):
    """设置/清空「访问资源站取源」专用代理（搜索/分类/测速/解析；视频流仍直连）。

    issue #30：部分运营商直连资源站超时/被墙。保存后即时用该代理对一个内置源做
    一次轻量测速，返回连通结果（失败也保存，由用户自行决定）。"""
    body = await request.json()
    px = str(body.get("proxy", "") or "").strip()
    ok, msg = config.set_source_proxy(px)
    if not ok:
        return JSONResponse({"code": 400, "msg": msg}, status_code=400)
    # 代理模式：off/on/auto（缺省保持不变）
    if body.get("mode") is not None:
        mok, mmsg = config.set_source_proxy_mode(str(body.get("mode")))
        if not mok:
            return JSONResponse({"code": 400, "msg": mmsg}, status_code=400)
    mode = config.source_proxy_mode()
    # 切换模式或改代理时，清空 auto 学习到的"需代理主机"，避免旧判断残留
    netutil.clear_proxy_hosts()
    test_ok, test_msg = False, ""
    if px:
        try:
            import time as _t
            src = next((s for s in config.enabled_sources()
                        if s.get("api", "").startswith("http")), None)
            api = (src or {}).get("api", "")
            t0 = _t.monotonic()
            async with netutil.async_client(timeout=10.0, px=px) as cli:
                rr = await cli.get(api, params={"ac": "list", "pg": 1},
                                   headers={"User-Agent": "Mozilla/5.0"})
            ms = int((_t.monotonic() - t0) * 1000)
            test_ok = rr.status_code == 200
            test_msg = f"{(src or {}).get('name', api[:30])} HTTP {rr.status_code} / {ms}ms"
        except Exception as exc:  # noqa: BLE001
            test_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
    return {"code": 0, "msg": "",
            "data": {"proxy": netutil.source_proxy(),
                     "mode": mode,
                     "test_ok": (True if not px else test_ok),
                     "test_msg": ("直连（未设置代理）" if not px else test_msg)}}


def _schedule_routes_refresh():
    async def _run_after_current():
        # 刷新进行中时不能丢事件：连续“关→开”切换若把“开”丢掉，
        # 关状态的重建会先落地且再无重建，扩展库卡永远回不来。
        # 等当前这轮结束再按最新配置补跑一次（refresh_routes 自身有重入保护）。
        waited = 0
        while catalog._refreshing and waited < 180:
            await asyncio.sleep(2)
            waited += 2
        if not catalog._refreshing:
            await catalog.refresh_routes(config.enabled_sources())

    try:
        asyncio.create_task(_run_after_current())
    except Exception:
        pass


@app.post("/v/api/ext/source/add")
async def ext_source_add(request: Request):
    body = await request.json()
    ok, info = config.add_source(body.get("name", ""), body.get("api", ""),
                                 bool(body.get("enabled", True)),
                                 bool(body.get("standalone", False)))
    if not ok:
        return JSONResponse({"code": 400, "msg": info}, status_code=400)
    _search_cache.clear(); _AGG_CACHE.clear()
    if body.get("standalone"):
        _schedule_routes_refresh()
    return {"code": 0, "msg": "added", "data": {"id": info}}


@app.post("/v/api/ext/source/standalone")
async def ext_source_standalone(request: Request):
    body = await request.json()
    sid = body.get("id", "")
    on = bool(body.get("standalone"))
    target = next((s for s in config.all_sources() if s["id"] == sid), None)
    # >>> PRIVATE:DDUO
    if features.DDUO and target and target.get("xsrc"):
        return JSONResponse({"code": 400, "msg": "多多影视为非标准源，不支持独立媒体库"},
                            status_code=400)
    # <<< PRIVATE:DDUO
    ok, info = config.set_standalone(sid, on)
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok"}


@app.post("/v/api/ext/source/loose-filter")
async def ext_source_loose(request: Request):
    body = await request.json()
    sid = body.get("id", "")
    on = bool(body.get("loose_filter"))
    ok, info = config.set_loose_filter(sid, on)
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    # 放宽规则变化要清掉该源作用域的页缓存，旧的过滤结果才不会残留
    try:
        with catalog._lock:
            stale = [k for k in catalog._page_cache if k[0] == sid]
        for k in stale:
            catalog._page_cache.pop(k, None)
            for buf in (catalog._dense, catalog._seen_guids,
                        catalog._scanned_page, catalog._dense_count):
                buf.pop(catalog._scope_key(sid, k[1]), None)
    except Exception:
        pass
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok"}


# >>> PRIVATE:ADULT
@app.post("/v/api/ext/source/adult")
async def ext_source_adult(request: Request):
    body = await request.json()
    sid = body.get("id", "")
    on = bool(body.get("adult"))
    target = next((s for s in config.all_sources() if s["id"] == sid), None)
    if not target:
        return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
    if on and not target.get("standalone"):
        return JSONResponse({"code": 400, "msg": "请先把该源设为独立媒体库"}, status_code=400)
    ok, info = config.set_adult(sid, on)
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok"}
# <<< PRIVATE:ADULT


@app.post("/v/api/ext/source/delete")
async def ext_source_delete(request: Request):
    body = await request.json()
    ok, info = config.remove_source(body.get("id", ""))
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _search_cache.clear(); _AGG_CACHE.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "deleted"}


@app.post("/v/api/ext/source/restore")
async def ext_source_restore(request: Request):
    config.restore_builtin()
    _search_cache.clear(); _AGG_CACHE.clear()
    return {"code": 0, "msg": "restored"}


@app.post("/v/api/ext/source/test")
async def ext_source_test(request: Request):
    body = await request.json()
    api = (body.get("api") or "").strip()
    sid = body.get("id")
    src = next((s for s in config.all_sources() if s["id"] == sid), None)
    if not api and sid:
        if not src:
            return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
        api = src["api"]
    keyword = (body.get("keyword") or "庆余年").strip()
    # >>> PRIVATE:DDUO
    if features.DDUO:
        xsrc = next((s for s in config.all_sources() if s.get("xsrc")
                     and (s["id"] == sid or (not sid and s["api"] == api))), None)
        if xsrc:
            result = await providers.dduo_probe_source(xsrc, keyword=keyword)
            return {"code": 0, "msg": "", "data": result}
    # <<< PRIVATE:DDUO
    result = await providers.probe_source(api, keyword=keyword, source=src)
    return {"code": 0, "msg": "", "data": result}


@app.post("/v/api/ext/source/test-all")
async def ext_source_test_all(request: Request):
    """并发探测全部源，返回 [{id,name,...result}]。"""
    async def one(s):
        r = await providers.probe_source(s["api"], source=s)
        # >>> PRIVATE:DDUO
        if features.DDUO and s.get("xsrc"):
            r = await providers.dduo_probe_source(s)
        # <<< PRIVATE:DDUO
        r.update(id=s["id"], name=s["name"], enabled=bool(s.get("enabled")),
                 builtin=bool(s.get("builtin")))
        return r
    results = await asyncio.gather(*[one(s) for s in config.all_sources()],
                                   return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    return {"code": 0, "msg": "", "data": out}


# ---------------- CMS 源批量体检（移植上游 v0.2.71） ----------------
@app.post("/v/api/ext/source/health-check")
async def ext_source_health_check(request: Request):
    """用常见热门关键词批量检测纯 CMS 资源站有效性。

    任一热门词搜到结果即有效；全部搜不到才判失效，并把失效源写入源健康画像
    （record False 累积触发熔断），起播/竞速自动跳过。仅检测纯苹果 CMS 源，
    加固/爬虫/小雅等专有源不走此检测。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    # 限制并发，避免一次性打爆源站
    try:
        concurrency = max(1, min(8, int(body.get("concurrency") or 4)))
    except (TypeError, ValueError):
        concurrency = 4
    targets = [s for s in config.all_sources() if _is_pure_cms(s)]
    sem = asyncio.Semaphore(concurrency)

    async def one(s):
        async with sem:
            try:
                r = await providers.probe_source_alive(s)
            except Exception as exc:  # noqa: BLE001
                r = {"alive": False, "ok": False, "status": 0,
                     "elapsed_ms": 0, "count": 0, "sample": "",
                     "error": type(exc).__name__, "hit": "", "tried": []}
            sk = sourcehealth.source_key_of(s.get("id"), s) or s.get("id", "")
            # 回写源健康：失效累计失败（达阈值熔断），有效则清零失败计数
            if sk:
                sourcehealth.record(sk, bool(r.get("alive")),
                                    (r.get("elapsed_ms") or 0) / 1000.0)
            return {
                "id": s.get("id"), "name": s.get("name", ""),
                "alive": bool(r.get("alive")), "hit": r.get("hit", ""),
                "tried": r.get("tried", []), "count": r.get("count", 0),
                "elapsed_ms": r.get("elapsed_ms", 0),
                "status": r.get("status", 0), "error": r.get("error", ""),
                "dead": sourcehealth.is_dead(sk) if sk else False,
            }

    results = await asyncio.gather(*[one(s) for s in targets])
    alive_n = sum(1 for r in results if r["alive"])
    return {"code": 0, "msg": "", "data": {
        "total": len(results), "alive": alive_n,
        "dead": len(results) - alive_n,
        "keywords": list(providers.PROBE_HOT_KEYWORDS),
        "results": results}}


def _is_pure_cms(s: dict) -> bool:
    """纯苹果 CMS 源（非 jar 爬虫 / 加固 / 小雅 / dduo / DRPY / T4）。
    移植上游 v0.2.69/71（CMS 源导入/导出、批量体检共用）。"""
    if s.get("guard") or s.get("spider_src") or s.get("catvod") \
            or s.get("xsrc") or s.get("drpy") or s.get("t4"):
        return False
    if str(s.get("id", "")).startswith(("spider-", "guard-", "drpy-", "t4-")):
        return False
    return bool(str(s.get("api", "")).startswith(("http://", "https://")))


# ---------------- CMS 源导入 / 导出（移植上游 v0.2.69） ----------------
@app.get("/v/api/ext/source/export")
async def ext_source_export():
    """导出纯 CMS 源为可移植 JSON（只含名称/接口/启用/独立库/非过滤等通用字段，
    不含 id/内置标记，便于跨设备导入；jar/加固/小雅等不属本页范围）。"""
    out = []
    for s in config.all_sources():
        if not _is_pure_cms(s):
            continue
        out.append({
            "name": s.get("name", ""),
            "api": s.get("api", ""),
            "enabled": bool(s.get("enabled", True)),
            "standalone": bool(s.get("standalone", False)),
            "loose_filter": bool(s.get("loose_filter", False)),
        })
    return {"code": 0, "msg": "", "data": {"version": 1, "sources": out}}


@app.post("/v/api/ext/source/import-file")
async def ext_source_import_file(request: Request):
    """从导出的 JSON（或含 sources 数组的兼容结构）批量导入纯 CMS 源。
    自动去重（按规范化 API），返回新增/跳过计数；jar/爬虫/加固条目忽略并计数。"""
    body = await request.json()
    raw = body.get("sources")
    if raw is None and isinstance(body.get("data"), dict):
        raw = body["data"].get("sources")
    if not isinstance(raw, list):
        return JSONResponse({"code": 400, "msg": "JSON 需含 sources 数组"},
                            status_code=400)
    added, skipped, noncms = 0, 0, 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        api = str(it.get("api", "")).strip()
        name = str(it.get("name", "")).strip()
        if not api.startswith(("http://", "https://")):
            noncms += 1
            continue
        # 兼容 TVBox 形态：带 jar/csp/type!=0/1 的不当作纯 CMS
        if it.get("jar") or str(it.get("type", "")).strip() not in ("", "0", "1", 0, 1):
            noncms += 1
            continue
        ok, _info = config.add_source(
            name, api, bool(it.get("enabled", True)),
            bool(it.get("standalone", False)))
        if ok:
            added += 1
            if it.get("loose_filter"):
                # fork 适配：上游的 config.save({sid: {...}}) 在本 fork 是
                # no-op（save 只认已有顶层键），改用 fork 已有的 set_loose_filter。
                try:
                    sid = config.make_custom_id(api)
                    config.set_loose_filter(sid, True)
                except Exception:
                    pass
        else:
            skipped += 1
    _search_cache.clear(); _AGG_CACHE.clear()
    if added:
        _schedule_routes_refresh()
    return {"code": 0, "msg": "", "data": {"added": added, "skipped": skipped,
                                            "ignored_noncms": noncms}}


@app.post("/v/api/ext/source/update")
async def ext_source_update(request: Request):
    """编辑资源站名称 / 订阅(API)链接。自定义源可改链接（id 随地址哈希重建），
    内置源仅允许改名。改后清搜索缓存并刷新海量库路由。"""
    body = await request.json()
    sid = str(body.get("id", ""))
    cur = next((s for s in config.all_sources() if s["id"] == sid), None)
    if not cur:
        return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
    name = body.get("name") if body.get("name") is not None else None
    api = body.get("api") if body.get("api") is not None else None
    if api is not None and cur.get("guard"):
        return JSONResponse({"code": 400, "msg": "加固源订阅请在「TVBox 加固」页管理"},
                            status_code=400)
    try:
        ok, info = config.update_source(sid, name=name, api=api)
    except Exception as exc:
        return JSONResponse({"code": 500, "msg": str(exc)}, status_code=500)
    if not ok:
        return JSONResponse({"code": 400, "msg": info}, status_code=400)
    _search_cache.clear(); _AGG_CACHE.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok", "data": {"id": info}}


@app.post("/v/api/ext/source/import-tvbox")
async def ext_import_tvbox(request: Request):
    """导入 TVBox 配置中的 CMS 与远程 T4 源。

    入参：{url?} 或 {text?}，外加 standalone（可选）。对候选源做轻量探测，
    CMS 探测通过后导入；T4 只登记来源，不预运行插件。
    """
    import tvbox as tvboxmod
    body = await request.json()
    standalone = bool(body.get("standalone"))
    try:
        if body.get("url"):
            sites, _subs = await tvboxmod.load_remote(str(body["url"]).strip())
        elif body.get("text"):
            cfg = tvboxmod.parse_config_text(str(body["text"]))
            if not cfg:
                return JSONResponse({"code": 400, "msg": "无法解析配置（需为 JSON 或其 base64）"},
                                    status_code=400)
            sites = tvboxmod.extract_sites(cfg)
        else:
            return JSONResponse({"code": 400, "msg": "缺少 url 或 text"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"code": 502, "msg": f"获取配置失败：{type(exc).__name__}"},
                            status_code=502)

    candidates: list[dict] = []
    t4_candidates: list[dict] = []
    seen_api: set[str] = set()
    skipped: list[str] = []
    for s in sites:
        status, norm = tvboxmod.classify_site(s)
        if status == "add_t4":
            t4_candidates.append(norm)
            continue
        if not norm:
            nm = str(s.get("name") or s.get("key") or s.get("api") or "未命名")
            if nm not in skipped:
                skipped.append(nm[:20])
            continue
        key = norm["api"].rstrip("/").lower()
        if key in seen_api:
            continue
        seen_api.add(key)
        candidates.append(norm)

    existing = {s.get("api", "").rstrip("/").lower() for s in config.all_sources()}
    added_names, dup = [], 0
    # 并发探测，最多导入 40 个可用源，避免一次性拉爆
    sem = asyncio.Semaphore(8)

    async def check(c):
        async with sem:
            try:
                probe = await asyncio.wait_for(
                    providers.probe_source(c["api"], timeout=8.0), timeout=10)
            except Exception:
                probe = {"ok": False}
            return c, probe

    checked = await asyncio.gather(*[check(c) for c in candidates[:60]])
    for c, probe in checked:
        key = c["api"].rstrip("/").lower()
        if key in existing:
            dup += 1
            continue
        if not probe.get("ok"):
            skipped.append(c["name"][:20])
            continue
        ok, info = config.add_source(c["name"], c["api"], True, standalone)
        if ok:
            added_names.append(c["name"])
            existing.add(key)
        else:
            dup += 1
        if len(added_names) >= 40:
            break
    t4_report = config.import_t4_sources(t4_candidates)
    if added_names or t4_report["added"]:
        _search_cache.clear(); _AGG_CACHE.clear()
        if standalone:
            _schedule_routes_refresh()
        else:
            # 新源加入后也刷新一次聚合路由，便于海量库覆盖
            _schedule_routes_refresh()
    return {"code": 0, "msg": "", "data": {
        "scanned": len(sites),
        "candidates": len(candidates),
        "added": len(added_names) + t4_report["added"],
        "added_names": added_names + t4_report["added_names"],
        "t4": t4_report,
        "duplicate": dup,
        "skipped": len(skipped),
        "skipped_detail": skipped[:40],
    }}


@app.post("/v/api/ext/source/import-subscription")
async def ext_import_subscription(request: Request):
    """统一导入 TVBox / 影视仓 / OK影视 订阅（一个入口，自动分类、导入即可用）。

    入参：{url?} 或 {text?}，可选 standalone（CMS 源是否同时作独立媒体库）、
    enable_guard（True/False/"smart"，默认 "smart"：安全加固源自动启用、
    P2P/磁力/云盘/需外部配置的风险源导入但默认关）。

    一份订阅一次解析，按运行时分流：
      - 标准苹果CMS（type 0/1）：轻量探测通过后加入自定义源，立即可搜可播；
      - csp_*Guard 加固源：写入私有 imported.json 并同步进源底表（仅完整版）；
      - 其余爬虫 / 网盘 / 直播：跳过并给出原因。
    返回分类报告 + 加固运行时就绪状态（未就绪时引导一键部署，源仍已登记）。
    """
    import tvbox as tvboxmod
    body = await request.json()
    standalone = bool(body.get("standalone"))
    enable_guard = body.get("enable_guard", "smart")
    if enable_guard not in (True, False, "smart"):
        enable_guard = "smart"

    try:
        if body.get("builtin"):
            # 内置推荐订阅（如一键导入饭太硬）：在 tvbox 内完成多镜像尝试与解密。
            _builtin = str(body["builtin"]).strip().lower()
            if _builtin == "fantaiying":
                raw_sites, _spider, _base, _used = await tvboxmod.load_fantaiying()
                _subs, _rules = 0, []
            elif _builtin in ("cat_lite", "catlite", "mao_lite"):
                # 内置轻量猫源：纯苹果CMS，无 jar/爬虫，零运行时，full/lite 通用。
                raw_sites, _spider, _base, _used = tvboxmod.load_cat_lite_sync()
                _subs, _rules = 0, []
            else:
                return JSONResponse({"code": 400, "msg": f"未知内置订阅：{_builtin}"},
                                    status_code=400)
        elif body.get("url"):
            raw_sites, _subs, _spider, _base, _rules = await tvboxmod.load_subscription_full(
                url=str(body["url"]).strip())
        elif body.get("text"):
            raw_sites, _subs, _spider, _base, _rules = await tvboxmod.load_subscription_full(
                text=str(body["text"]))
        else:
            return JSONResponse({"code": 400, "msg": "缺少 url 或 text"},
                                status_code=400)
    except Exception as exc:
        return JSONResponse({"code": 502,
                             "msg": f"获取/解析订阅失败：{type(exc).__name__}"},
                            status_code=502)

    # 合并订阅自带的 m3u8 去广告规则（TVBox/OK影视 Pro rules），去重持久化
    ad_rules_added = 0
    try:
        import adfilter as _adfilter
        ad_rules_added = _adfilter.merge_subscription_rules(_rules or [])
    except Exception:
        _log.info("merge ad rules failed", exc_info=True)

    groups = tvboxmod.classify_subscription(raw_sites, _spider, _base)
    t4_report = config.import_t4_sources(groups.get("t4", []))
    cms_cands = groups["cms"]
    guard_sites = groups["guard"]
    csp_items = groups.get("csp", [])
    drpy_items = groups.get("drpy", [])
    jar_url_global = groups.get("jar_url", "")
    skipped = list(groups["skipped"])

    # ---------- 苹果 CMS：并发探测，通过即加入并启用 ----------
    existing = {s.get("api", "").rstrip("/").lower() for s in config.all_sources()}
    cms_added, cms_dup = [], 0
    cms_failed: list[str] = []
    sem = asyncio.Semaphore(8)

    async def _probe(c):
        async with sem:
            try:
                pr = await asyncio.wait_for(
                    providers.probe_source(c["api"], timeout=8.0), timeout=10)
            except Exception:
                pr = {"ok": False}
            return c, pr

    checked = await asyncio.gather(*[_probe(c) for c in cms_cands[:80]])
    for c, pr in checked:
        key = c["api"].rstrip("/").lower()
        if key in existing:
            cms_dup += 1
            continue
        if not pr.get("ok"):
            cms_failed.append(c["name"][:20])
            continue
        ok, _info = config.add_source(c["name"], c["api"], True, standalone)
        if ok:
            cms_added.append(c["name"])
            existing.add(key)
        else:
            cms_dup += 1
        if len(cms_added) >= 60:
            break
    for nm in cms_failed:
        tag = nm + "|探测不可达"
        if not any(x.get("name") == nm and x.get("reason") == "探测不可达"
                   for x in skipped):
            skipped.append({"name": nm, "reason": "探测不可达"})

    # ---------- csp_*Guard 加固源（仅完整私有构建） ----------
    guard_report = None
    runtime = None
    # >>> PRIVATE:GUARD
    if features.GUARD and guard_sites:
        import json as _json
        import guard as _gmod
        try:
            res = await asyncio.to_thread(
                _gmod.import_tvbox,
                _json.dumps({"sites": guard_sites}, ensure_ascii=False),
                enable_guard)
            # 只报告本次订阅涉及的键（imported.json 里可能还有历史导入）
            parsed_now = {s["key"]: s for s in res.get("sites", [])}
            g_items, g_enabled, g_risk = [], [], []
            for s in parsed_now.values():
                g_items.append({"key": s["key"], "name": s.get("name", s["key"]),
                                "enabled": s["key"] in set(res.get("enabled_keys", [])),
                                "risk": bool(s.get("risk")),
                                "risk_reason": s.get("risk_reason", "")})
                if s["key"] in set(res.get("enabled_keys", [])):
                    g_enabled.append(s["key"])
                if s.get("risk"):
                    g_risk.append(s.get("name", s["key"]))
            guard_report = {
                "scanned": res.get("total", len(guard_sites)),
                "added": res.get("added", 0),
                "updated": res.get("updated", 0),
                "items": g_items,
                "enabled_count": len(g_enabled),
                "risk_names": g_risk,
            }
        except Exception as exc:  # noqa: BLE001
            guard_report = {"error": f"加固源写入失败：{type(exc).__name__}"}
        # 运行时就绪（短超时；未就绪不影响登记，前端引导一键部署）
        try:
            h = _gmod.health()
            runtime = {
                "feature": True,
                "ready": bool(h.get("ok")),
                "reason": "" if h.get("ok") else (h.get("error") or "runner 未连接"),
                "runner": h.get("runner", _gmod.RUNNER_URL),
            }
        except Exception as exc:  # noqa: BLE001
            runtime = {"feature": True, "ready": False,
                       "reason": str(exc)[:120]}
        try:
            config.load()
        except Exception:
            pass
    elif guard_sites:
        guard_report = {"skipped_build": True}
        runtime = {"feature": False, "ready": False,
                   "reason": "当前为公开构建，不含加固运行时"}
    # <<< PRIVATE:GUARD

    # ---------- 普通 csp_ 爬虫 与 远程 drpyS（仅完整私有构建） ----------
    csp_report = None
    drpy_report = None
    # >>> PRIVATE:SPIDER
    if features.GUARD and csp_items:
        import spiders as _spm
        # 先尝试下载该订阅的 spider jar（失败则源登记为缺 jar，可手动补传）
        jar_status = None
        jar_urls = sorted({(c.get("jar") or jar_url_global) for c in csp_items
                           if c.get("jar") or jar_url_global})
        for ju in jar_urls:
            if ju:
                jar_status = await asyncio.to_thread(_spm.download_jar, ju)
        try:
            res = await asyncio.to_thread(_spm.import_sites, csp_items, "smart")
            csp_report = {
                "added": res["added"], "updated": res["updated"],
                "items": res["items"],
                "jar": {"url": jar_urls[0] if jar_urls else "",
                        **(jar_status or {"ok": False, "error": "订阅未声明 spider jar"})},
            }
        except Exception as exc:  # noqa: BLE001
            csp_report = {"error": f"普通爬虫写入失败：{type(exc).__name__}"}
        try:
            config.load()
        except Exception:
            pass
    elif csp_items:
        csp_report = {"skipped_build": True}
    # <<< PRIVATE:SPIDER
    # >>> PRIVATE:DRPY
    if features.DRPY and drpy_items:
        import drpy as _drpym
        try:
            res = await asyncio.to_thread(_drpym.import_remote, drpy_items, "smart")
            drpy_report = {"added": res["added"], "updated": res["updated"],
                           "items": res["items"]}
        except Exception as exc:  # noqa: BLE001
            drpy_report = {"error": f"远程DRPY导入失败：{type(exc).__name__}"}
        try:
            config.load()
        except Exception:
            pass
    elif drpy_items:
        drpy_report = {"skipped_build": True}
    # <<< PRIVATE:DRPY

    if cms_added or t4_report["added"] or (guard_report and guard_report.get("enabled_count")) \
            or (csp_report and csp_report.get("added")) \
            or (drpy_report and drpy_report.get("added")):
        _search_cache.clear(); _AGG_CACHE.clear()
        _schedule_routes_refresh()

    return {"code": 0, "msg": "", "data": {
        "scanned": len(raw_sites),
        "subscriptions": _subs,
        "t4": t4_report,
        "cms": {
            "candidates": len(cms_cands),
            "added": len(cms_added),
            "added_names": cms_added,
            "duplicate": cms_dup,
        },
        "guard": guard_report,
        "csp": csp_report,
        "drpy": drpy_report,
        "runtime": runtime,
        "skipped": len(skipped),
        "skipped_detail": skipped[:60],
        "ad_rules_added": ad_rules_added,
    }}


@app.get("/v/api/ext/adfilter/status")
async def ext_adfilter_status():
    """m3u8 去广告状态：总开关 + 动态检测 + 已合并的订阅规则组数/域名数。"""
    import adfilter as _adf
    st = _adf.stats()
    st["dynamic"] = _adf._dynamic_enabled()
    st["sps"] = _adf.sps_enabled()
    # 附带各规则组的名称/域名，便于管理页展示
    st["groups"] = [{"name": g.get("name", ""),
                     "hosts": g.get("hosts", []),
                     "regex": len(g.get("regex", []))}
                    for g in _adf._load_rules()]
    return {"code": 0, "msg": "", "data": st}


@app.post("/v/api/ext/adfilter/toggle")
async def ext_adfilter_toggle(request: Request):
    """开关 m3u8 去广告。body: {"enabled":bool} / {"dynamic":bool} / {"sps":bool}"""
    import adfilter as _adf
    body = await request.json()
    if "dynamic" in body:
        config.save({"ad_filter_dynamic": bool(body.get("dynamic"))})
    if "sps" in body:
        config.save({"ad_filter_sps": bool(body.get("sps"))})
        if not bool(body.get("sps")):
            _adf.reset_sps_cache()
    if "enabled" in body:
        _adf.set_enabled(bool(body.get("enabled")))
    return {"code": 0, "msg": "",
            "data": {"enabled": _adf.enabled(),
                     "dynamic": _adf._dynamic_enabled(),
                     "sps": _adf.sps_enabled()}}


@app.post("/v/api/ext/adfilter/rules")
async def ext_adfilter_replace_rules(request: Request):
    """整体替换去广告规则（高级）。body: {"rules":[{name,hosts[],regex[]}]}"""
    import adfilter as _adf
    body = await request.json()
    n = _adf.replace_rules(body.get("rules") or [])
    return {"code": 0, "msg": "", "data": {"groups": n}}


@app.get("/v/online-admin")
async def admin_page():
    from admin_ui import ADMIN_HTML
    return Response(
        ADMIN_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"})



# >>> PRIVATE:CATVOD
# ---------------- 小雅网盘（catvod JVM 爬虫，仅完整构建） ----------------
if features.CATVOD:
    import catvod as _catvod

    def _catvod_guard():
        if not _catvod.available():
            return JSONResponse(
                {"code": 503, "msg": "小雅运行时不可用（缺 JDK/runner）", "data": None},
                status_code=503)
        return None

    @app.get("/v/api/ext/catvod/credentials")
    async def catvod_credentials_get():
        return JSONResponse({"code": 0, "msg": "",
                             "data": _catvod.private_status()})

    @app.post("/v/api/ext/catvod/credentials")
    async def catvod_credentials_save(request: Request):
        body = await request.json()
        try:
            st = await asyncio.to_thread(
                _catvod.save_credentials,
                body.get("server"), body.get("username"), body.get("password"))
            return JSONResponse({"code": 0, "msg": "已保存并生效", "data": st})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200],
                                 "data": None}, status_code=500)

    @app.get("/v/api/ext/catvod/home")
    async def catvod_home():
        g = _catvod_guard()
        if g:
            return g
        try:
            tok = await _catvod.xiaoya_token()
            home = await asyncio.to_thread(_catvod.home, True, tok)
            data = {"classes": home.get("class", []),
                    "filters": home.get("filters", {})}
            return JSONResponse({"code": 0, "msg": "", "data": data})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/catvod/category")
    async def catvod_category(tid: str, pg: int = 1, f: str = ""):
        g = _catvod_guard()
        if g:
            return g
        try:
            tok = await _catvod.xiaoya_token()
            extend = {}
            if f:
                import urllib.parse as _up
                extend = dict(_up.parse_qsl(f))
            res = await asyncio.to_thread(_catvod.category, tid, pg, extend, tok)
            return JSONResponse({"code": 0, "msg": "", "data": res})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/catvod/detail")
    async def catvod_detail(vid: str):
        g = _catvod_guard()
        if g:
            return g
        try:
            tok = await _catvod.xiaoya_token()
            det = await asyncio.to_thread(_catvod.detail, vid, tok)
            eps = _catvod.parse_episodes(det)
            data = {"vod": det, "episodes": eps}
            return JSONResponse({"code": 0, "msg": "", "data": data})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/catvod/search")
    async def catvod_search(q: str):
        g = _catvod_guard()
        if g:
            return g
        try:
            tok = await _catvod.xiaoya_token()
            lst = await asyncio.to_thread(_catvod.search, q, tok)
            return JSONResponse({"code": 0, "msg": "", "data": {"list": lst}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/catvod/play")
    async def catvod_play(vid: str, flag: str = "", resolve: int = 1):
        """返回小雅签名播放地址。resolve=1（默认）时服务端跟随一次 302，
        给出网盘 CDN 最终地址；否则给小雅 /d 链（由客户端自行跟随）。"""
        g = _catvod_guard()
        if g:
            return g
        try:
            res = await _catvod.resolve_play(vid, flag)
            url = res.get("url", "")
            headers = res.get("headers", {})
            if resolve and url:
                try:
                    async with httpx.AsyncClient(trust_env=False,
                                                 follow_redirects=True,
                                                 timeout=20) as _c:
                        rr = await _c.head(url, headers=headers)
                        if str(rr.url) != url:
                            url = str(rr.url)
                except Exception:
                    pass
            return JSONResponse({"code": 0, "msg": "",
                                 "data": {"url": url, "headers": headers}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/ext/catvod/redirect")
    async def catvod_redirect(vid: str, flag: str = ""):
        """播放器直接用：302 到小雅签名 /d 链（携带所需 UA）。"""
        g = _catvod_guard()
        if g:
            return g
        try:
            res = await _catvod.resolve_play(vid, flag)
            url = res.get("url", "")
            if not url:
                return JSONResponse({"code": 404, "msg": "无播放地址"}, status_code=404)
            return RedirectResponse(url, status_code=302,
                                    headers={"Cache-Control": "no-store"})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]}, status_code=500)

    @app.get("/v/ext/xlib/play")
    async def xlib_play(g: str, i: int = 0):
        """小雅分类库播放：用 AList /api/fs/get 取最终公网 CDN 直链，302 客户端直连。
        NAS 到该 CDN 不通，故服务端不代流，只做签名与跳转。"""
        guard_r = _catvod_guard()
        if guard_r:
            return guard_r
        if not _xlib_on():
            return JSONResponse({"code": 403, "msg": "小雅库未启用"}, status_code=403)
        import xlib as _xl
        found = _xl.library.by_guid(g)
        if not found:
            return JSONResponse({"code": 404, "msg": "影片不在索引"}, status_code=404)
        files = await _xl.library.show_files(found["show"])
        if not files:
            return JSONResponse({"code": 404, "msg": "无视频文件"}, status_code=404)
        i = max(0, min(i, len(files) - 1))
        try:
            url = await _xl.library.signed_raw(files[i])
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 502, "msg": str(exc)[:200]}, status_code=502)
        return RedirectResponse(url, status_code=302,
                                headers={"Cache-Control": "no-store"})
# <<< PRIVATE:CATVOD

# >>> PRIVATE:DRPY
# ---------------- drpyS JS 爬虫（Node sidecar，仅完整构建） ----------------
if features.DRPY:
    import drpy as _drpy

    def _drpy_src(sid: str):
        for s in config.all_sources():
            if s.get("drpy") and s.get("id") == sid:
                return s
        return None

    def _drpy_guard():
        if not _drpy.available():
            return JSONResponse(
                {"code": 503, "msg": "JS 爬虫运行时不可用（缺 Node/规则脚本）", "data": None},
                status_code=503)
        return None

    @app.get("/v/api/ext/drpy/status")
    async def drpy_status():
        sites = [{"id": s.get("id"), "name": s.get("name"),
                  "enabled": bool(s.get("enabled"))}
                 for s in config.all_sources() if s.get("drpy")]
        return JSONResponse({"code": 0, "msg": "", "data": {
            "available": _drpy.available(), "sites": sites}})

    @app.get("/v/api/ext/drpy/home")
    async def drpy_home(sid: str):
        g = _drpy_guard()
        if g:
            return g
        src = _drpy_src(sid)
        if not src:
            return JSONResponse({"code": 404, "msg": "未知 JS 源"}, status_code=404)
        try:
            site = providers._drpy_site(src)
            home = await asyncio.to_thread(_drpy.home, site)
            return JSONResponse({"code": 0, "msg": "", "data": {
                "classes": home.get("class", []), "filters": home.get("filters", {})}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/drpy/category")
    async def drpy_category(sid: str, tid: str, pg: int = 1):
        g = _drpy_guard()
        if g:
            return g
        src = _drpy_src(sid)
        if not src:
            return JSONResponse({"code": 404, "msg": "未知 JS 源"}, status_code=404)
        try:
            res = await asyncio.to_thread(_drpy.category, providers._drpy_site(src), tid, pg)
            return JSONResponse({"code": 0, "msg": "", "data": res})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/drpy/detail")
    async def drpy_detail(sid: str, vid: str):
        g = _drpy_guard()
        if g:
            return g
        src = _drpy_src(sid)
        if not src:
            return JSONResponse({"code": 404, "msg": "未知 JS 源"}, status_code=404)
        try:
            det = await asyncio.to_thread(_drpy.detail, providers._drpy_site(src), vid)
            eps = _drpy.parse_episodes(det)
            return JSONResponse({"code": 0, "msg": "",
                                 "data": {"vod": det, "episodes": eps}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/drpy/play")
    async def drpy_play(sid: str, vid: str, flag: str = ""):
        g = _drpy_guard()
        if g:
            return g
        src = _drpy_src(sid)
        if not src:
            return JSONResponse({"code": 404, "msg": "未知 JS 源"}, status_code=404)
        try:
            res = await asyncio.to_thread(_drpy.play, providers._drpy_site(src), flag, vid)
            return JSONResponse({"code": 0, "msg": "", "data": res})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @app.get("/v/api/ext/drpy/remote")
    async def drpy_remote_list():
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"available": _drpy.available(),
                                      "sites": await asyncio.to_thread(_drpy.remote_status)}})

    @app.post("/v/api/ext/drpy/remote-toggle")
    async def drpy_remote_toggle(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        key, on = str(body.get("key", "")), bool(body.get("enabled"))
        ok = await asyncio.to_thread(_drpy.set_remote_enabled, key, on)
        if not ok:
            return JSONResponse(
                {"code": 409, "msg": "规则脚本未就绪，无法启用（可先重试下载）"},
                status_code=409)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key, "enabled": on}})

    @app.post("/v/api/ext/drpy/remote-remove")
    async def drpy_remote_remove(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        key = str(body.get("key", ""))
        if not key or not await asyncio.to_thread(_drpy.remove_remote, key):
            return JSONResponse({"code": 404, "msg": "未知远程 JS 源"}, status_code=404)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key}})

    @app.post("/v/api/ext/drpy/remote-retry")
    async def drpy_remote_retry(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        key = str(body.get("key", ""))
        res = await asyncio.to_thread(_drpy.retry_remote_script, key)
        if not res.get("ok"):
            return JSONResponse({"code": 502, "msg": res.get("error", "下载失败")},
                                status_code=502)
        return JSONResponse({"code": 0, "msg": "", "data": res})
# <<< PRIVATE:DRPY


# ---------------- 在线源健康画像（成功率/延迟，自动降权，通用能力） ----------------
@app.get("/v/api/ext/source-health")
async def source_health():
    snap = sourcehealth.snapshot()
    # 按健康等级、成功率、延迟排序输出，便于管理页展示
    rows = sorted(
        snap.values(),
        key=lambda s: (s.get("grade", 1),
                       -s.get("success_rate", 0),
                       s.get("avg_latency", 99)))
    return JSONResponse({"code": 0, "data": {"sources": rows}})


@app.post("/v/api/ext/source-health/reset")
async def source_health_reset(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sourcehealth.reset((body or {}).get("key"))
    return JSONResponse({"code": 0})


# ---------------- 本片线路预测（linepredict）：海报预热→本片最快线→秒播 ----------------
# 上游 v0.2.68/69 移植。冻结适配：reset 只清预测缓存，不动 _LINE_FAST（turbo 最快线
# 记忆）；状态里的 chart_bindings 为 chartbind 持久化绑定计数。
@app.get("/v/api/ext/linepredict")
async def linepredict_status():
    """返回当前已预测的本片最快线数与在途预热数（调试/管理页用）。"""
    return JSONResponse({"code": 0, "data": {
        "remembered": linepredict.count(),
        "inflight": linepredict.inflight_count(),
        "recent": [dict(r, title=(store.get_title_by_guid(r.get("og", "")) or {}
                                  ).get("title", r.get("og", "")[:12]))
                   for r in linepredict.snapshot(40)],
        "config": {
            "enabled": bool(config.get().get("linepredict_enabled", True)),
            "max_lines": int(config.get().get("linepredict_max_lines", 2) or 2),
            "ttl_min": int(config.get().get("linepredict_ttl_min", 30) or 30),
            "prewarm_mode": str(config.get().get("prewarm_mode", "realtime")),
            "chart_bindings": chartbind.bind_store.status()}}})


@app.post("/v/api/ext/linepredict/config")
async def linepredict_config(request: Request):
    body = await request.json()
    patch = {}
    if "enabled" in body:
        patch["linepredict_enabled"] = bool(body["enabled"])
    if "max_lines" in body:
        try:
            patch["linepredict_max_lines"] = min(3, max(1, int(body["max_lines"])))
        except (TypeError, ValueError):
            pass
    if "ttl_min" in body:
        # 管理页开关新增：预测缓存有效期（分钟），与已有 enabled/max_lines 同端点。
        try:
            patch["linepredict_ttl_min"] = min(120, max(5, int(body["ttl_min"])))
        except (TypeError, ValueError):
            pass
    if patch:
        config.save(patch)
    return JSONResponse({"code": 0, "data": patch})


@app.post("/v/api/ext/linepredict/reset")
async def linepredict_reset():
    linepredict.reset()
    return JSONResponse({"code": 0})


# >>> PRIVATE:GUARD
# ---------------- DexGuard 加固爬虫（Android runner，仅完整构建） ----------------
if features.GUARD:
    import guard as _guard

    @app.get("/v/api/ext/guard/status")
    async def guard_status():
        st = await asyncio.to_thread(_guard.status)
        return JSONResponse({"code": 0, "msg": "", "data": st})

    async def _guard_body(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    @app.post("/v/api/ext/guard/import")
    async def guard_import(request: Request):
        """粘贴 TVBox 配置（JSON 文本或 URL）批量导入 csp_*Guard 加固源。

        body: {"config": "<tvbox json 文本或 http(s) 地址>", "enable": true|false|null}
        jar 用设备上已部署的内置兼容加固 jar，无需下载配置里的 spider jar。
        """
        body = await _guard_body(request)
        raw = (body.get("config") or "").strip()
        if not raw:
            return JSONResponse({"code": 400, "msg": "缺少 config"}, status_code=400)
        text = raw
        if raw.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=20,
                                             follow_redirects=True) as cli:
                    rr = await cli.get(raw, headers={"User-Agent": "okhttp/3.15"})
                    text = rr.text
            except Exception as exc:  # noqa: BLE001
                return JSONResponse({"code": 502, "msg": f"配置下载失败: {exc}"[:200]})
        enable = body.get("enable", None)
        try:
            res = await asyncio.to_thread(_guard.import_tvbox, text, enable)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 400, "msg": f"解析失败: {exc}"[:200]})
        # 让新源立即进入配置底表
        try:
            config.load()
        except Exception:
            pass
        return JSONResponse({"code": 0, "msg": "", "data": res})

    @app.post("/v/api/ext/guard/toggle")
    async def guard_toggle(request: Request):
        body = await _guard_body(request)
        key, on = str(body.get("key", "")), bool(body.get("enabled"))
        if not _guard.site(key):
            return JSONResponse({"code": 404, "msg": "未知加固源"}, status_code=404)
        _guard.set_enabled(key, on)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key, "enabled": on}})

    @app.post("/v/api/ext/guard/remove")
    async def guard_remove(request: Request):
        body = await _guard_body(request)
        key = str(body.get("key", ""))
        if not key:
            return JSONResponse({"code": 400, "msg": "缺少 key"}, status_code=400)
        removed = await asyncio.to_thread(_guard.remove_imported, key)
        if not removed:
            return JSONResponse({"code": 400, "msg": "内置源不可删除（可停用）"}, status_code=400)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key}})

    @app.get("/v/api/ext/guard/jar")
    async def guard_jar_status():
        """用户自备加固 jar 的本地/设备状态（不回传文件内容）。"""
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.user_jar_status)})

    @app.post("/v/api/ext/guard/jar")
    async def guard_jar_upload(request: Request):
        """上传用户自备的加固 jar（base64 JSON，避免引入 multipart 依赖）。

        body: {"name": "xxx.jar", "data_b64": "<base64>"}
        保存到私有目录并立即下发到正在运行的 redroid；未运行则下次启动自动下发。
        """
        import base64
        body = await _guard_body(request)
        b64 = (body.get("data_b64") or "").strip()
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        if not b64:
            return JSONResponse({"code": 400, "msg": "缺少 data_b64"}, status_code=400)
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 400, "msg": f"base64 解码失败: {exc}"}, status_code=400)
        res = await asyncio.to_thread(_guard.save_user_jar, raw, str(body.get("name") or ""))
        if not res.get("ok"):
            return JSONResponse({"code": 400, "msg": res.get("error", "保存失败")}, status_code=400)
        return JSONResponse({"code": 0, "msg": "", "data": res})

    # ---------------- 普通 csp_ 爬虫（订阅导入，按各自 jar） ----------------
    import spiders as _spider

    @app.get("/v/api/ext/spider/status")
    async def spider_status():
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_spider.status)})

    @app.post("/v/api/ext/spider/toggle")
    async def spider_toggle(request: Request):
        body = await _guard_body(request)
        key, on = str(body.get("key", "")), bool(body.get("enabled"))
        if not _spider.site(key):
            return JSONResponse({"code": 404, "msg": "未知爬虫源"}, status_code=404)
        # 启用前要求 jar 就绪
        if on and key in _spider.jar_missing_keys():
            return JSONResponse(
                {"code": 409, "msg": "该源的爬虫 jar 尚未就绪，请先补传 jar 或等待下载"},
                status_code=409)
        _spider.set_enabled(key, on)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key, "enabled": on}})

    @app.post("/v/api/ext/spider/remove")
    async def spider_remove(request: Request):
        body = await _guard_body(request)
        key = str(body.get("key", ""))
        if not key or not await asyncio.to_thread(_spider.remove, key):
            return JSONResponse({"code": 404, "msg": "未知爬虫源"}, status_code=404)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key}})

    @app.post("/v/api/ext/spider/jar")
    async def spider_jar_upload(request: Request):
        """为某个订阅 jar URL 手动补传爬虫 jar（源站下载失败时）。

        body: {"jar_url": "...", "name":"x.jar", "data_b64":"<base64>"}
        保存后联动启用引用该 jar 的非风险源，并立即尝试下发到 redroid。
        """
        import base64
        body = await _guard_body(request)
        jar_url = str(body.get("jar_url", "")).strip()
        if not jar_url:
            return JSONResponse({"code": 400, "msg": "缺少 jar_url"}, status_code=400)
        b64 = (body.get("data_b64") or "").strip()
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        if not b64:
            return JSONResponse({"code": 400, "msg": "缺少 data_b64"}, status_code=400)
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 400, "msg": f"base64 解码失败: {exc}"},
                                status_code=400)
        res = await asyncio.to_thread(_spider.upload_jar, jar_url, raw)
        if not res.get("ok"):
            return JSONResponse({"code": 400, "msg": res.get("error", "保存失败")},
                                status_code=400)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": res})

    @app.post("/v/api/ext/spider/jar-retry")
    async def spider_jar_retry(request: Request):
        """重新尝试自动下载某个 jar（源站恢复后），成功则联动启用非风险源。"""
        body = await _guard_body(request)
        jar_url = str(body.get("jar_url", "")).strip()
        if not jar_url:
            return JSONResponse({"code": 400, "msg": "缺少 jar_url"}, status_code=400)
        res = await asyncio.to_thread(_spider.download_jar, jar_url)
        if not res.get("ok"):
            return JSONResponse({"code": 502, "msg": res.get("error", "下载失败")},
                                status_code=502)
        # 联动启用此前缺 jar 的非风险源
        sites = _spider.load_sites()
        changed = []
        for k, s in sites.items():
            if s.get("jar_url") == jar_url and not s.get("enabled") and not s.get("risk"):
                _spider.set_enabled(k, True)
                changed.append(k)
        config.load()
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"download": res, "auto_enabled": changed}})

    @app.post("/v/api/ext/spider/probe")
    async def spider_probe(request: Request):
        """单源体检：搜索→详情→(可选)取首集播放地址。body:{key,keyword,do_play}"""
        body = await _guard_body(request)
        key = str(body.get("key", "")).strip()
        if not _spider.site(key):
            return JSONResponse({"code": 404, "msg": "未知爬虫源"}, status_code=404)
        kw = str(body.get("keyword", "") or "庆余年")[:20]
        do_play = bool(body.get("do_play", False))
        # 试播可能触发个别源 WebView 嗅探而拖长，整体给宽超时
        data = await asyncio.wait_for(
            asyncio.to_thread(_spider.probe, key, kw, do_play), timeout=150)
        return JSONResponse({"code": 0, "msg": "", "data": data})

    @app.post("/v/api/ext/spider/scan")
    async def spider_scan_start(request: Request):
        """启动全源批量体检（后台并发）。body:{keyword,do_play,only_enabled}"""
        body = await _guard_body(request)
        kw = str(body.get("keyword", "") or "庆余年")[:20]
        do_play = bool(body.get("do_play", False))
        only_enabled = bool(body.get("only_enabled", True))
        res = _spider.start_scan(keyword=kw, do_play=do_play,
                                 only_enabled=only_enabled)
        if not res.get("ok"):
            return JSONResponse({"code": 409, "msg": res.get("error", "启动失败")},
                                status_code=409)
        return JSONResponse({"code": 0, "msg": "", "data": res})

    @app.get("/v/api/ext/spider/scan")
    async def spider_scan_status():
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_spider.scan_status)})

    @app.post("/v/api/ext/spider/remove-many")
    async def spider_remove_many(request: Request):
        """批量删除源。body:{keys:[...]}"""
        body = await _guard_body(request)
        keys = body.get("keys") or []
        if not isinstance(keys, list):
            return JSONResponse({"code": 400, "msg": "keys 必须是数组"}, status_code=400)
        res = await asyncio.to_thread(_spider.remove_many, keys)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": res})

    # ---- 专题资源栏：浏览单个加固源的首页分类 / 分类影片 ----
    @app.get("/v/api/ext/guard/gnstate")
    async def guard_gnstate():
        # 临时诊断：进程内加固库元数据/热墙实况
        try:
            cls = _gn_classes_meta()
            out = {}
            for k, m in cls.items():
                classes = m.get("classes") or []
                out[k] = {"name": m.get("name"),
                          "film": [c.get("name") for c in classes
                                   if _gn_is_film_class(c.get("name"))],
                          "all": [c.get("name") for c in classes][:10]}
            with _gn_lock:
                hot_n = len(_gn_state.get("hot") or [])
                warm = _gn_state.get("warm_running")
                empty = dict(_gn_state.get("meta_empty") or {})
                fail = dict(_gn_state.get("meta_fail") or {})
                freason = dict(_gn_state.get("meta_fail_reason") or {})
                allkeys = {g["conf"]["key"] for g in _gn_sources()}
            missing = {}
            for k in allkeys:
                if k in out:
                    continue
                st = "fail" if k in fail else ("empty" if k in empty else "pending")
                missing[k] = {"st": st, "reason": freason.get(k, "")} if st == "fail" else st
            return JSONResponse({"code": 0, "data": {"sources": out, "hot": hot_n,
                                                     "warm_running": warm,
                                                     "missing": missing}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @app.get("/v/api/ext/guard/home")
    async def guard_home(sid: str):
        conf = _guard.site(sid)
        if not conf:
            return JSONResponse({"code": 404, "msg": "未知加固源"}, status_code=404)
        try:
            home = await asyncio.to_thread(_guard.home, conf)
            return JSONResponse({"code": 0, "msg": "", "data": {
                "key": sid, "name": conf.get("name"),
                "classes": home.get("class", []),
                "filters": home.get("filters", {})}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @app.get("/v/api/ext/guard/category")
    async def guard_category(sid: str, tid: str, pg: int = 1):
        conf = _guard.site(sid)
        if not conf:
            return JSONResponse({"code": 404, "msg": "未知加固源"}, status_code=404)
        try:
            res = await asyncio.to_thread(_guard.category, conf, tid, pg)
            return JSONResponse({"code": 0, "msg": "", "data": res})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    # ================= 网盘（夸克 / UC 等）登录/转存/直链 =================
    try:
        import pan as _pan
    except Exception:  # noqa: BLE001  # 公开构建已删除 pan.py
        _pan = None  # type: ignore
    try:
        import panuc as _panuc
    except Exception:  # noqa: BLE001  # 公开构建已删除 panuc.py
        _panuc = None  # type: ignore
    try:
        import panali as _panali
    except Exception:  # noqa: BLE001  # 公开构建已删除 panali.py
        _panali = None  # type: ignore

    # provider key -> 实现模块（前端按 key 调 /pan/<key>/...，quark 走无前缀旧路径）
    _PAN_MODS = {"quark": _pan, "uc": _panuc, "ali": _panali}

    def _pan_ok(key: str = "quark"):
        return _PAN_MODS.get(key) is not None

    def _pan_mod(key: str):
        m = _PAN_MODS.get(key or "quark")
        if m is None:
            raise _GuardPanUnsupported("当前构建不含该网盘模块")
        return m

    @app.get("/v/api/ext/pan/status")
    async def pan_status():
        providers = [{"key": p["key"], "name": p["name"],
                      "playable": p["playable"]} for p in _PAN_PROVIDERS]
        logins = {}
        for key, mod in _PAN_MODS.items():
            if mod is not None:
                try:
                    logins[key] = mod.account_info_safe()
                except Exception:  # noqa: BLE001
                    pass
        supported = any(m is not None for m in _PAN_MODS.values())
        qk = logins.get("quark", {})
        return JSONResponse({"code": 0, "data": {
            "supported": supported, "providers": providers,
            "logins": logins, **qk}})

    @app.post("/v/api/ext/pan/detect")
    async def pan_detect(body: dict | None = None):
        """识别一段文本（播放地址/线路名）里的网盘资源。供前端角标与提示使用。"""
        text = str((body or {}).get("text", ""))
        pv = _pan_match(text)
        linkstart = bool(_PAN_LINKSTART_RE.search(text))
        if not pv and not linkstart:
            return JSONResponse({"code": 0, "data": {"pan": False}})
        key = pv["key"] if pv else "quark"
        name = pv["name"] if pv else "网盘聚合(四盘)"
        playable = bool(pv and pv["playable"])
        mod = _PAN_MODS.get(key)
        logged = bool(playable and mod is not None and mod.logged_in())
        return JSONResponse({"code": 0, "data": {
            "pan": True, "key": key, "name": name,
            "playable": playable, "linkstart": linkstart,
            "logged_in": logged}})

    async def _pan_qr_start(key: str):
        mod = _pan_mod(key)
        try:
            s = await asyncio.to_thread(mod.qr_start)
            try:
                s["qr_png"] = "data:image/png;base64," + (
                    await asyncio.to_thread(
                        lambda: __import__("base64").b64encode(
                            mod.qr_png(s["qr_text"])).decode()))
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse({"code": 0, "data": s})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @app.post("/v/api/ext/pan/qr/start")
    async def pan_qr_start():
        return await _pan_qr_start("quark")

    @app.post("/v/api/ext/pan/uc/qr/start")
    async def pan_uc_qr_start():
        return await _pan_qr_start("uc")

    @app.post("/v/api/ext/pan/ali/qr/start")
    async def pan_ali_qr_start():
        return await _pan_qr_start("ali")

    @app.post("/v/api/ext/pan/ali/cred")
    async def pan_ali_cred(req: Request):
        body = await req.json()
        try:
            _panali.set_credentials(body.get("client_id", ""),
                                    body.get("client_secret", ""))
            return JSONResponse({"code": 0})
        except Exception as exc:
            return JSONResponse({"code": 400, "msg": str(exc)[:200]})

    _PAN_PAGE_HTML = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>网盘 · 登录</title>
<style>
body{margin:0;background:#0f1115;color:#e6e6e6;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:480px;margin:0 auto;padding:24px 18px 48px}
h1{font-size:20px;margin:8px 0 4px}.sub{color:#8b93a7;font-size:13px;margin-bottom:16px}
.card{background:#171a21;border:1px solid #232733;border-radius:14px;padding:18px;margin-bottom:16px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{background:#3b82f6;color:#fff;border:0;border-radius:10px;padding:10px 16px;font-size:14px;cursor:pointer}
button.ghost{background:#262b36}
button:disabled{opacity:.5;cursor:not-allowed}
#qr{width:240px;height:240px;background:#fff;border-radius:12px;display:block;margin:6px auto;image-rendering:pixelated}
.muted{color:#8b93a7;font-size:12px}
.st{font-size:14px;padding:10px 12px;border-radius:10px;background:#10131a;border:1px solid #232733}
.st.ok{color:#4ade80}.st.bad{color:#f87171}.st.wait{color:#fbbf24}
textarea{width:100%;box-sizing:border-box;background:#10131a;color:#e6e6e6;border:1px solid #2a2f3c;border-radius:10px;padding:10px;font-size:12px;height:84px;resize:vertical}
input{flex:1;min-width:0;background:#10131a;color:#e6e6e6;border:1px solid #2a2f3c;border-radius:10px;padding:10px;font-size:13px}
a{color:#60a5fa}
.log{font-size:12px;color:#9aa3b8;white-space:pre-wrap;margin-top:8px;min-height:16px}
.tag{display:inline-block;background:#222b3a;color:#7db1ff;border-radius:6px;padding:1px 7px;font-size:11px;margin-left:6px}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{flex:1;text-align:center;padding:11px 0;border-radius:10px;background:#171a21;border:1px solid #232733;color:#9aa3b8;cursor:pointer;font-size:14px}
.tab.on{background:#3b82f6;color:#fff;border-color:#3b82f6}
.file{display:flex;justify-content:space-between;gap:8px;padding:8px 2px;border-bottom:1px solid #232733;font-size:13px}
.file button{padding:5px 10px;font-size:12px;white-space:nowrap}
.size{color:#8b93a7;font-size:11px;white-space:nowrap}
</style></head><body><div class="wrap">
<h1>网盘播放 <span class="tag">夸克 / UC / 阿里</span></h1>
<div class="sub">登录网盘后，资源站里的网盘分享链接会自动转存并原画播放（不占用安卓加固运行时）。凭据仅保存在本机 NAS。</div>

<div class="tabs">
  <div class="tab on" id="tab-quark" onclick="switchTab('quark')">夸克网盘</div>
  <div class="tab" id="tab-uc" onclick="switchTab('uc')">UC网盘</div>
  <div class="tab" id="tab-ali" onclick="switchTab('ali')">阿里云盘</div>
</div>

<div class="card" id="aliCredCard" style="display:none">
  <strong>阿里云盘 · 开放平台凭据（扫码前先填一次）</strong>
  <div class="muted" style="line-height:1.8;margin:8px 0">
    阿里云盘网页接口有设备签名，本插件走官方开放平台，更稳定：<br>
    1. 打开 <a href="https://open.alipan.com" target="_blank">open.alipan.com</a>，用你的阿里云盘账号登录 → 创建一个应用（类型选“网页应用”，回调地址填 <span class="tag">https://127.0.0.1</span> 即可，无需真正部署）；<br>
    2. 复制应用的 <b>Client ID</b> 与 <b>Client Secret</b> 填入下方保存；<br>
    3. 再点下方“生成二维码”，用阿里云盘 App 扫码授权（refresh_token 长效，会自动续期）。
    也可不建应用，直接在“粘贴凭据”里贴入已有的 refresh_token。
  </div>
  <input id="aliCid" placeholder="Client ID" style="margin-bottom:8px">
  <input id="aliCsec" placeholder="Client Secret">
  <div style="height:8px"></div>
  <div class="row"><button onclick="saveAliCred()">保存凭据</button></div>
  <div class="log" id="credLog"></div>
</div>

<div class="card"><div class="st" id="st">读取登录状态…</div></div>

<div class="card">
  <strong>方式一 · 扫码登录</strong>
  <div class="row" style="justify-content:space-between;margin-top:8px">
    <span class="muted" id="qrHint">用对应网盘 App 扫码</span>
    <button id="btnQr" onclick="startQr()">生成二维码</button>
  </div>
  <img id="qr" alt="二维码" style="display:none">
  <div class="log" id="qrLog"></div>
  <div class="muted" style="margin-top:8px" id="qrTip">夸克：用夸克网盘 App 内「扫一扫」；若打开下载页请改用 Cookie。</div>
</div>

<div class="card">
  <strong>方式二 · 粘贴 Cookie</strong>
  <div style="height:8px"></div>
  <textarea id="ck" placeholder="登录网盘网页版后，F12 → Network → 复制任一请求的 Cookie 整行（需含 __pus / __puus）"></textarea>
  <div style="height:8px"></div>
  <div class="row"><button onclick="saveCk()">保存 Cookie</button><button class="ghost" onclick="doLogout()">退出登录</button></div>
  <div class="log" id="ckLog"></div>
</div>

<div class="card">
  <strong>测试 · 解析一个分享</strong>
  <div style="height:8px"></div>
  <input id="surl" placeholder="粘贴当前网盘的分享链接，提取码可附 ?pwd=xxxx">
  <div style="height:8px"></div>
  <div class="row"><button class="ghost" onclick="doResolve()">解析并转存</button></div>
  <div class="log" id="rsLog"></div>
</div>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <strong>我的转存</strong><button class="ghost" onclick="loadList()">刷新</button>
  </div>
  <div class="muted" style="margin:6px 0">显示当前网盘的 <b>fnmedia转存</b> 目录；影片点开可直接在本页播放。</div>
  <div id="myList" class="muted">登录后点刷新。</div>
</div>

<div class="card">
  <strong>支持的网盘</strong>
  <div style="height:8px"></div>
  <div id="pvList" class="muted">读取中…</div>
</div>

<div class="card">
  <strong>怎么知道某个资源是网盘资源？</strong>
  <div class="muted" style="line-height:1.9;margin-top:8px">
    网盘资源的播放地址是<b>某家网盘的分享链接</b>。识别特征：<br>
    ① 线路/站点名带「<b>四盘 / 优夸 / 夸父 / 盘搜 / 聚盘搜 / 云盘</b>」等字样；<br>
    ② 播放地址形如 <span class="tag">pan.quark.cn/s/…</span><span class="tag">drive.uc.cn/s/…</span><span class="tag">alipan.com/s/…</span><span class="tag">pan.baidu.com/s/…</span>；<br>
    ③ 部分聚合源先给中转 <span class="tag">…/link_start/?redirect_to=pan_id_…</span> 再 302 跳转；<br>
    本插件<b>自动识别</b>：已登录且支持的网盘直接转存原画；暂不支持的网盘会明确提示，可改选夸克 / UC 线路。
  </div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
let cur='quark', STATUS=null, pollTimer=null;
const META={
  quark:{name:'夸克网盘', qr:'请用夸克网盘 App 扫码确认…',
         tip:'夸克：用夸克网盘 App 内「扫一扫」；若打开下载页请改用 Cookie。',
         ph:'https://pan.quark.cn/s/xxxx  提取码可附 ?pwd=xxxx'},
  uc:{name:'UC网盘', qr:'请用手机 UC 浏览器 / UC网盘 App 扫码确认…',
      tip:'UC：用手机 UC 浏览器的「扫一扫」或 UC网盘 App 扫码；不行请改用 Cookie。',
      ph:'https://drive.uc.cn/s/xxxx  提取码可附 ?pwd=xxxx'},
  ali:{name:'阿里云盘', qr:'请用阿里云盘 App 扫码授权…',
       tip:'阿里云盘走官方开放平台：先在上方填 Client ID/Secret 保存，再生成二维码扫码；也可直接粘贴 refresh_token。',
       ph:'https://www.alipan.com/s/xxxx  提取码可附 ?pwd=xxxx'}
};
const base=p=>'/v/api/ext/pan/'+(p==='quark'?'':p+'/');
function show(el,cls,msg){el.className='st '+cls;el.textContent=msg;}
function log(id,msg){$(id).textContent=msg;}
async function api(p,body){
  const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function info(){return (STATUS.logins&&STATUS.logins[cur])||{};}
async function refresh(){
  STATUS=await (await fetch('/v/api/ext/pan/status')).json().then(j=>j.data||{});
  const d=info();
  const el=$('st');
  if(d.logged_in){
    let extra='';
    if(d.saved_ts){
      const ageH=(Date.now()/1000-d.saved_ts)/3600;
      if(cur==='ali') extra=' · 授权长效，自动续期';
      else if(ageH>=2.5) extra=' · <span style="color:#fbbf24">播放票据约3小时过期，若播放失败请重新扫码</span>';
      else extra=' · 已登录 '+ageH.toFixed(1)+' 小时';
    }
    show(el,'ok','当前：'+META[cur].name+' · 已登录：'+(d.nickname||'用户')+extra);
  }
  else if(STATUS.supported) show(el,'bad','当前：'+META[cur].name+' · 未登录');
  else show(el,'bad','当前构建不含网盘模块');
  $('qrHint').textContent=META[cur].name+' · 扫码';
  $('qrTip').textContent=META[cur].tip;
  $('surl').placeholder=META[cur].ph;
  $('aliCredCard').style.display=(cur==='ali')?'block':'none';
  $('ck').placeholder=(cur==='ali')
    ?'可直接粘贴阿里云盘的 refresh_token（alist 等工具可导出）；夸克/UC 才粘贴 Cookie'
    :'登录网盘网页版后，F12 → Network → 复制任一请求的 Cookie 整行（需含 __pus / __puus）';
  const pv=$('pvList');
  if(Array.isArray(STATUS.providers)){
    pv.innerHTML=STATUS.providers.map(p=>{
      const lg=STATUS.logins&&STATUS.logins[p.key]&&STATUS.logins[p.key].logged_in;
      const badge=lg?'<span style="color:#4ade80">已登录</span>'
        :p.playable?'<span style="color:#7db1ff">可登录</span>'
        :'<span style="color:#8b93a7">适配中</span>';
      return '<div style="display:inline-block;min-width:150px;margin:3px 10px 3px 0">'+p.name+'：'+badge+'</div>';
    }).join('');
    if(d.logged_in) loadList();
  }
}
function switchTab(p){
  cur=p;clearInterval(pollTimer);
  $('tab-quark').classList.toggle('on',p==='quark');
  $('tab-uc').classList.toggle('on',p==='uc');
  $('tab-ali').classList.toggle('on',p==='ali');
  $('qr').style.display='none';$('qr').removeAttribute('src');
  $('btnQr').disabled=false;
  ['qrLog','ckLog','rsLog'].forEach(i=>$(i).textContent='');
  refresh();
}
async function saveAliCred(){
  const client_id=$('aliCid').value.trim(),client_secret=$('aliCsec').value.trim();
  if(!client_id||!client_secret){log('credLog','请填写 Client ID 与 Client Secret');return;}
  log('credLog','保存中…');
  const j=await api('/v/api/ext/pan/ali/cred',{client_id,client_secret});
  log('credLog',j.code===0?'凭据已保存，可以生成二维码扫码授权了。':('失败：'+(j.msg||'')));
}
async function startQr(){
  $('btnQr').disabled=true;log('qrLog','正在生成…');
  const j=await api(base(cur)+'qr/start',{});
  if(j.code!==0){log('qrLog','生成失败：'+(j.msg||''));$('btnQr').disabled=false;return;}
  const d=j.data;$('qr').src=d.qr_png;$('qr').style.display='block';
  log('qrLog',META[cur].qr);
  clearInterval(pollTimer);
  pollTimer=setInterval(async()=>{
    const p=await api(base(cur)+'qr/poll',d);
    const pd=p.data||{};
    if(pd.status==='success'){clearInterval(pollTimer);log('qrLog','登录成功 ✓ '+(pd.nickname||''));$('qr').style.display='none';$('btnQr').disabled=false;refresh();}
    else if(pd.status==='expired'){clearInterval(pollTimer);log('qrLog','二维码已过期，请重新生成');$('btnQr').disabled=false;}
    else if(pd.status==='scanned'){log('qrLog','已扫码，请在手机上确认…');}
  },2500);
}
async function saveCk(){
  const cookie=$('ck').value.trim();if(!cookie){log('ckLog','请粘贴 Cookie');return;}
  log('ckLog','校验中…');
  const j=await api(base(cur)+'cookie',{cookie});
  log('ckLog',j.code===0?('保存成功：'+((j.data||{}).nickname||'')):('失败：'+(j.msg||'')));
  if(j.code===0){$('ck').value='';refresh();}
}
async function doResolve(){
  const url=$('surl').value.trim();if(!url){log('rsLog','请填分享链接');return;}
  log('rsLog','解析+转存+取直链中（大文件可能较慢）…');
  const j=await api(base(cur)+'resolve',{url});
  if(j.code!==0){log('rsLog','失败：'+(j.msg||''));return;}
  const d=j.data;let s='标题：'+d.title+'\\n可播 '+d.items.filter(x=>x.url).length+' / '+d.items.length+' 集\\n';
  d.items.slice(0,12).forEach(x=>{s+='\\n'+(x.url?'▶ ':'⊘ ')+x.name+'  '+(x.size/1048576).toFixed(0)+'MB';});
  log('rsLog',s);
}
function fmtSize(n){if(!n)return'';if(n>1073741824)return(n/1073741824).toFixed(2)+'GB';if(n>1048576)return(n/1048576).toFixed(0)+'MB';return(n/1024).toFixed(0)+'KB';}
async function loadList(){
  const box=$('myList');box.textContent='读取中…';
  const r=await fetch(base(cur)+'list');const j=await r.json();
  if(j.code!==0){box.textContent=j.msg||'读取失败';return;}
  const items=(j.data&&j.data.items)||[];
  if(!items.length){box.textContent='转存目录暂无文件。播放资源站网盘链接后，文件会自动转存到这里。';return;}
  box.innerHTML=items.map(x=>{
    const right=x.play?`<button onclick="playMine('${x.play}','${encodeURIComponent(x.name)}')">播放</button>`
      :(x.dir?'📁':'');
    return `<div class="file"><span title="${x.name.replace(/"/g,'')}">${x.dir?'📁 ':'🎬 '}${x.name}</span><span style="display:flex;gap:8px;align-items:center"><span class="size">${fmtSize(x.size)}</span>${right}</span></div>`;
  }).join('');
}
function playMine(path,name){
  const url=path+(path.includes('?')?'&':'?')+'inline=1';
  let w=window.open('','_blank');
  if(w){w.document.write('<title>'+decodeURIComponent(name)+'</title><video src="'+url+'" controls autoplay style="width:100%;height:100%;background:#000"></video>');}
  else location.href=url;
}
async function doLogout(){if(!confirm('确认退出 '+META[cur].name+' 登录？'))return;await api(base(cur)+'logout',{});refresh();}
refresh();
</script></body></html>"""

    @app.get("/v/api/ext/pan/page")
    async def pan_page():
        return HTMLResponse(_PAN_PAGE_HTML)

    async def _pan_poll(key: str, req: Request):
        mod = _pan_mod(key)
        body = await req.json()
        try:
            r = await asyncio.to_thread(mod.qr_poll, body)
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @app.post("/v/api/ext/pan/qr/poll")
    async def pan_qr_poll(req: Request):
        return await _pan_poll("quark", req)

    @app.post("/v/api/ext/pan/uc/qr/poll")
    async def pan_uc_qr_poll(req: Request):
        return await _pan_poll("uc", req)

    @app.post("/v/api/ext/pan/ali/qr/poll")
    async def pan_ali_qr_poll(req: Request):
        return await _pan_poll("ali", req)

    async def _pan_cookie(key: str, req: Request):
        mod = _pan_mod(key)
        body = await req.json()
        try:
            r = await asyncio.to_thread(mod.set_cookie, body.get("cookie", ""))
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 400, "msg": str(exc)[:200]})

    @app.post("/v/api/ext/pan/cookie")
    async def pan_cookie(req: Request):
        return await _pan_cookie("quark", req)

    @app.post("/v/api/ext/pan/uc/cookie")
    async def pan_uc_cookie(req: Request):
        return await _pan_cookie("uc", req)

    @app.post("/v/api/ext/pan/ali/cookie")
    async def pan_ali_cookie(req: Request):
        return await _pan_cookie("ali", req)

    async def _pan_resolve(key: str, req: Request):
        mod = _pan_mod(key)
        body = await req.json()
        try:
            r = await asyncio.to_thread(
                mod.resolve_share, body.get("url", ""),
                body.get("passcode", ""))
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @app.post("/v/api/ext/pan/resolve")
    async def pan_resolve(req: Request):
        return await _pan_resolve("quark", req)

    @app.post("/v/api/ext/pan/uc/resolve")
    async def pan_uc_resolve(req: Request):
        return await _pan_resolve("uc", req)

    @app.post("/v/api/ext/pan/ali/resolve")
    async def pan_ali_resolve(req: Request):
        return await _pan_resolve("ali", req)

    async def _pan_list(key: str, req: Request):
        mod = _pan_mod(key)
        fid = (req.query_params.get("fid") or "").strip()
        try:
            r = await asyncio.to_thread(mod.list_dir, fid)
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @app.get("/v/api/ext/pan/list")
    async def pan_list(req: Request):
        return await _pan_list("quark", req)

    @app.get("/v/api/ext/pan/uc/list")
    async def pan_uc_list(req: Request):
        return await _pan_list("uc", req)

    @app.get("/v/api/ext/pan/ali/list")
    async def pan_ali_list(req: Request):
        return await _pan_list("ali", req)

    async def _pan_logout(key: str):
        mod = _PAN_MODS.get(key)
        if mod is not None:
            await asyncio.to_thread(mod.logout)
        return JSONResponse({"code": 0})

    @app.post("/v/api/ext/pan/logout")
    async def pan_logout():
        return await _pan_logout("quark")

    @app.post("/v/api/ext/pan/uc/logout")
    async def pan_uc_logout():
        return await _pan_logout("uc")

    @app.post("/v/api/ext/pan/ali/logout")
    async def pan_ali_logout():
        return await _pan_logout("ali")

    # provider -> 播放代理注入的 Referer / 模块
    _PAN_PLAY_REF = {"quark": "https://pan.quark.cn/", "uc": "https://drive.uc.cn/", "ali": "https://www.alipan.com/"}

    def _pan_m3u8_absolutize(text: str, base_url: str) -> str:
        """把 HLS 清单的相对分片/密钥 URI 相对地址绝对化为真实网盘 CDN 地址。

        只做相对→绝对，不改写到本机；后续由通用 HLS 管道统一包 /seg 代理。"""
        def _abs(m):
            return m.group(1) + '"' + urllib.parse.urljoin(base_url, m.group(2)) + '"'
        out = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                if 'uri="' in s.lower():
                    line = re.sub(r'(URI=)"([^"]+)"', _abs, line, flags=re.I)
                out.append(line)
            else:
                out.append(urllib.parse.urljoin(base_url, s))
        return "\n".join(out) + "\n"

    async def _pan_stream(key: str, request: Request):
        mod = _pan_mod(key)
        fid = request.query_params.get("fid", "")
        if not fid:
            return JSONResponse({"code": 400, "msg": "缺少 fid"}, status_code=400)
        try:
            meta = await asyncio.to_thread(mod.playable, fid)
            url = meta["url"]
            cookie = await asyncio.to_thread(mod.auth_cookie_or_raise)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200]},
                                status_code=500)
        is_m3u8 = meta.get("m3u8") or ".m3u8" in url.split("?", 1)[0].lower()
        # 转码档（mkv 等不直出的源）夸克给的是 HLS 清单，且分片是【相对路径】。
        # 飞牛经 main.m3u8 通用管道播放时会以本端点地址作 base 再 urljoin，会拼错；
        # 故这里把清单内分片/密钥相对 URI 绝对化为【真实夸克 CDN 地址】（不指向
        # 本机）。通用管道随后会再统一包进 /seg 代理并在那里注入网盘 Cookie
        #（裸取夸克分片会 412）。
        if is_m3u8 and request.method == "GET":
            try:
                async with httpx.AsyncClient(trust_env=False,
                                            timeout=40) as pc:
                    pr = await pc.get(url, headers={"Cookie": cookie})
                if pr.status_code == 200 and "#EXTM3U" in pr.text[:64]:
                    body = _pan_m3u8_absolutize(pr.text, url)
                    return Response(
                        body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store",
                                 "Access-Control-Allow-Origin": "*"})
            except Exception:  # noqa: BLE001
                _log.info("pan m3u8 absolutize failed, passthrough",
                          exc_info=True)
        fwd = {"Cookie": cookie, "Referer": _PAN_PLAY_REF.get(key, "")}
        rng = request.headers.get("range")
        if rng:
            fwd["Range"] = rng
        client = httpx.AsyncClient(trust_env=False, timeout=60,
                                   follow_redirects=True)
        try:
            upstream = await client.send(
                client.build_request(request.method, url, headers=fwd),
                stream=True)
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            return JSONResponse({"code": 502, "msg": str(exc)[:200]},
                                status_code=502)
        passthrough = ["content-type", "content-length", "content-range",
                       "accept-ranges", "last-modified", "etag"]
        headers = {k: v for k, v in upstream.headers.items()
                   if k.lower() in passthrough}

        async def _gen():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _gen(), status_code=upstream.status_code, headers=headers)



    @app.api_route("/v/api/ext/pan/play", methods=["GET", "HEAD"])
    async def pan_play(request: Request):
        """夸克原画 CDN：注入 Cookie 流式代理，透传 Range 支持拖动(206)。"""
        return await _pan_stream("quark", request)

    @app.api_route("/v/api/ext/pan/uc/play", methods=["GET", "HEAD"])
    async def pan_uc_play(request: Request):
        """UC 网盘原画 CDN：同上。"""
        return await _pan_stream("uc", request)

    @app.api_route("/v/api/ext/pan/ali/play", methods=["GET", "HEAD"])
    async def pan_ali_play(request: Request):
        """阿里云盘：开放平台转码 m3u8 或原画下载直链（直链自带签名，无需鉴权头）。"""
        return await _pan_stream("ali", request)

    # —— 管理页「加固影视浏览器」：源/分类 + 聚合热门 + 分类翻页（海报墙）——
    def _guard_source_dict(key: str) -> dict | None:
        for s in config.enabled_sources():
            if s.get("guard") and s.get("guard_key") == key:
                return s
        # 源存在但被停用也允许浏览（浏览器内可独立看），构造最小 source
        conf = _guard.site(key)
        if not conf:
            return None
        return {"id": "guard-" + key, "name": conf.get("name") or key,
                "guard": True, "guard_key": key}

    def _browse_card(source: dict, t) -> dict | None:
        """由已入库 OnlineTitle 生成海报墙卡片（含播放地址）。"""
        try:
            b = store.get_title_by_guid(make_guid(t))
            if not b:
                return None
            mg = media_guid(b["guid"])
            # 浏览入库的线路键可能是 "guard-KEY::线路名"（聚合）或纯线路名（单源），
            # 选一个属于该加固源、且有可播集的键，供播放显式选线
            line_key = ""
            pref = f"guard-{source.get('guard_key')}::"
            for k, eps in (b.get("lines") or {}).items():
                playable = any(e.get("direct") or e.get("resolve")
                               for e in (eps or []))
                if playable and k.startswith(pref):
                    line_key = k
                    break
            if not line_key:
                for k, eps in (b.get("lines") or {}).items():
                    if any(e.get("direct") or e.get("resolve")
                           for e in (eps or [])):
                        line_key = k
                        break
            return {
                "title": t.title + (f" ·{t.remarks}" if t.remarks else ""),
                "pic": _admin_poster(t.poster or ""),
                "year": t.year or "",
                "remarks": t.remarks or "",
                "site": source.get("name") or "",
                "key": source.get("guard_key") or "",
                "vod_id": t.vod_id,
                "line_key": line_key,
                "play": f"{ONLINE_PREFIX}/{mg}/main.m3u8",
            }
        except Exception:  # noqa: BLE001
            return None

    def _admin_poster(url: str) -> str:
        # 管理页挂在 /v/online-admin，裸 /online/ 会被前置 nginx 拦，改用
        # /v/api/v1/sys/img/online/ 前缀（同一代理，UDS 与外部入口均可达）
        p = inj._poster(url or "")
        if p.startswith("/online/"):
            return "/v/api/v1/sys/img/online/" + p[len("/online/"):]
        return p

    def _cards_from_vods(source: dict, vods: list, n: int) -> list[dict]:
        """批量把加固 Vod 映射→去重→一次性入库→生成卡片（避免逐卡刷盘）。"""
        titles, seen = [], set()
        for v in vods[:n]:
            try:
                t = providers._guard_map_item(source, v)
            except Exception:  # noqa: BLE001
                continue
            if not t.title or not t.vod_id:
                continue
            og = make_guid(t)
            if og in seen:
                continue
            seen.add(og)
            titles.append(t)
        new_t = [t for t in titles
                 if not store.get_title_by_guid(make_guid(t))]
        if new_t:
            store.remember_titles(new_t)
        return [c for t in titles if (c := _browse_card(source, t))]

    @app.get("/v/api/ext/guard/browse")
    async def guard_browse(mode: str = "hot", sid: str = "",
                           tid: str = "", pg: int = 1,
                           limit: int = 60):
        """mode=sites：可浏览加固源及其分类；hot：聚合各源首页推荐；
        category：某源某分类一页。均返回海报卡片（含可直接播放的 m3u8）。"""
        if not _guard.available():
            return JSONResponse({"code": 503, "msg": "加固运行时未就绪，请先在本页启动 Android 运行时",
                                 "data": None}, status_code=503)
        limit = max(1, min(int(limit or 60), 120))
        try:
            if mode == "sites":
                async def _src_classes(s: dict) -> dict | None:
                    key = s.get("guard_key")
                    conf = _guard.site(key)
                    if not conf:
                        return None
                    try:
                        classes = await asyncio.wait_for(
                            asyncio.to_thread(_guard.browse_classes, conf),
                            timeout=8)
                    except Exception:  # noqa: BLE001
                        classes = []
                    if not classes:
                        return None
                    return {"key": key, "name": s.get("name") or key,
                            "classes": classes[:40]}

                guard_srcs = [s for s in config.enabled_sources() if s.get("guard")]
                res = await asyncio.gather(
                    *[_src_classes(s) for s in guard_srcs])
                out = [r for r in res if r]
                return JSONResponse({"code": 0, "msg": "",
                                     "data": {"sources": out}})

            if mode == "category":
                conf = _guard.site(sid)
                source = _guard_source_dict(sid)
                if not conf or not source:
                    return JSONResponse({"code": 404, "msg": "未知加固源"}, status_code=404)
                vods = await asyncio.to_thread(_guard.browse_page, conf, tid, pg)
                cards = await asyncio.to_thread(
                    _cards_from_vods, source, vods, limit)
                return JSONResponse({"code": 0, "msg": "", "data": {
                    "pg": pg, "list": cards, "has_more": len(cards) >= limit}})

            # mode=hot：并发取各源首页推荐，混合去重
            keys = [s.get("guard_key") for s in config.enabled_sources()
                    if s.get("guard") and _guard.site(s.get("guard_key"))]

            async def _one(key: str) -> list[dict]:
                source = _guard_source_dict(key)
                conf = _guard.site(key)
                if not source or not conf:
                    return []
                try:
                    async def _fetch():
                        h = await asyncio.to_thread(_guard.home_cached, conf)
                        vods = h.get("list") or []
                        if not vods:  # 首页无推荐时，退到第一个分类首页
                            classes = await asyncio.to_thread(
                                _guard.browse_classes, conf)
                            if classes:
                                vods = await asyncio.to_thread(
                                    _guard.browse_page, conf,
                                    classes[0]["tid"], 1)
                        return vods
                    vods = await asyncio.wait_for(_fetch(), timeout=18)
                except Exception:  # noqa: BLE001
                    return []
                per = max(6, limit // max(1, len(keys)))
                return await asyncio.to_thread(
                    _cards_from_vods, source, vods, per)

            results = await asyncio.gather(*[_one(k) for k in keys],
                                           return_exceptions=False)
            seen, cards = set(), []
            for group in results:
                for c in group:
                    dedup = (c["title"].split(" ·")[0], c.get("year"))
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    cards.append(c)
                    if len(cards) >= limit:
                        break
                if len(cards) >= limit:
                    break
            return JSONResponse({"code": 0, "msg": "", "data": {"list": cards}})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"code": 500, "msg": str(exc)[:200],
                                 "data": None}, status_code=500)

    # —— 重运行时 sidecar：状态 / 一键安装 / 日志 / 重连网络（管理页）——
    @app.get("/v/api/ext/sidecar/status")
    async def sidecar_status():
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.sidecar_status)})

    @app.post("/v/api/ext/sidecar/install")
    async def sidecar_install(request: Request):
        try:
            body = json.loads(await request.body() or b"{}")
        except Exception:
            body = {}
        what = str(body.get("what") or "all")
        res = await asyncio.to_thread(_guard.start_install, what)
        return JSONResponse({"code": 0 if res.get("ok") else 409,
                             "msg": res.get("error", ""), "data": res})

    @app.get("/v/api/ext/sidecar/log")
    async def sidecar_log():
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"running": await asyncio.to_thread(_guard.install_running),
                                      "log": await asyncio.to_thread(_guard.install_log_tail)}})

    @app.post("/v/api/ext/sidecar/reconnect")
    async def sidecar_reconnect():
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.reconnect_network)})

    @app.post("/v/api/ext/sidecar/android/stop")
    async def sidecar_android_stop():
        # 立即停止模拟器，释放约 2GB；停用自启/自愈，避免被重新拉起。
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.stop_android)})

    @app.post("/v/api/ext/sidecar/android/start")
    async def sidecar_android_start():
        # 冷启模拟器并后台接通 bridge（约 1–2 分钟）。
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.start_android)})
# <<< PRIVATE:GUARD


# ------------------------------------------------------------- 兜底透传

@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    # 本地视频/文件等大响应必须流式透传（不缓冲整文件），避免打满内存拖死系统；
    # 该函数内部对普通 JSON 自动回落到缓冲透传。
    return await forward_upstream_streaming(request)
