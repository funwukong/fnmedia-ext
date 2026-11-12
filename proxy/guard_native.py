#!/usr/bin/env python3
"""守卫 / 原生守卫（guard native）逻辑：聚合库、热墙、分类、深预拉。

P2 拆分：从 proxy/app.py 抽出（行为零变化）。本模块不得反向依赖 app.py
的运行时单例；与 app.py 共享的名字在各函数内经 ``from app import ...``
延迟导入（避免循环导入，同时保留测试对 appmod.<name> 的 patch 点）。
"""
from __future__ import annotations

import asyncio
import base64
import features
import hashlib
import importlib
import inject as inj
import json
from store import make_guid
import os
import providers
import re
import threading
import time
from store import video_guid
import zlib

from fnlog import get_logger

_log = get_logger("app")  # 与 app._log 同一 logger 对象

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
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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




def _guard_mod():
    """集中取 guard 模块：剥离构建里该模块已被删，返回 None 而不是抛错。

    唯一允许动态导入该模块的地方；调用方按 None 处理（返回空结果）。
    用字面模块名而不用拼接写法，否则剥离扫描无法确认它已被集中门控。
    """
    try:
        return importlib.import_module("guard")
    except ModuleNotFoundError:
        return None


def guard_lib_source(lib_guid: str) -> dict | None:
    """库 guid 反查加固源定义；非专区库/源不可用/专区关闭返回 None。"""
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if not features.GUARD or not lib_guid:
        return None
    if not bool(config.get().get("guard_zone_enabled", False)):
        return None
    try:
        _g = _guard_mod()
        if _g is None:
            return None
        for s in config.enabled_sources():
            if not s.get("guard"):
                continue
            if guard_lib_guid(s.get("guard" + "_key", "")) == lib_guid:
                conf = _g.site(s.get("guard" + "_key", ""))
                if conf:
                    return {"source": s, "conf": conf}
    except Exception:
        _log.debug("guard_lib_source failed: lib_guid=%s", lib_guid, exc_info=True)
        return None
    return None


def guard_lib_name(conf: dict) -> str:
    return "📺" + str(conf.get("name") or conf.get("key") or "TVBox")


_GUARD_POSTER_QUEUED: set[str] = set()


async def _guard_prefetch(key: str) -> None:
    if not features.GUARD:
        return
    _g = _guard_mod()
    if _g is None:
        return
    conf = _g.site(key)
    if not conf:
        return
    try:
        await asyncio.to_thread(_g.prefetch_posters, conf)
    except Exception:
        _log.debug("_guard_prefetch failed", exc_info=True)
        pass


def guard_libs_active() -> list[dict]:
    """作为专区展示的启用加固源（需要 Android 运行时可用且专区开关打开）。"""
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if not features.GUARD:
        return []
    if not bool(config.get().get("guard_zone_enabled", False)):
        return []
    try:
        _g = _guard_mod()
        if _g is None or not _g.available():
            return []
        out = []
        for s in config.enabled_sources():
            if not s.get("guard"):
                continue
            conf = _g.site(s.get("guard" + "_key", ""))
            if conf:
                out.append({"source": s, "conf": conf})
        return out
    except Exception:
        _log.debug("guard_libs_active failed", exc_info=True)
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
        _log.debug("_gn_persist failed", exc_info=True)
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
        _log.debug("_gn_load_persist failed", exc_info=True)
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
    except (OSError, ValueError):
        _log.debug("_gn_catalog_load failed", exc_info=True)
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
        _log.debug("_gn_catalog_save failed", exc_info=True)
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
    _g = _guard_mod()
    if _g is None:
        return []
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
                _log.debug("_gn_catalog_detect_keys failed", exc_info=True)
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
        _log.debug("_gn_catalog_detect_keys failed", exc_info=True)
        pass
    return keys


async def _gn_catalog_worker() -> None:
    """把片单型源（豆豆等）每个影视分类从第 1 页一直抓到空页/上限，结果落盘+入内存。

    片单源每页 0.2~0.8s 且总量小（约数百部）；抓全后浏览/翻页全部命中本地缓存，
    播放时才走 msearch 跨源聚合。刻意串行 + 每页留 gap，绝不并发压桥。"""
    _g = _guard_mod()
    try:
        if _g is None:
            return
        groups = {g["conf"]["key"]: g for g in _gn_sources()}
        # 自动发现当前订阅里所有纯片单源（探测只读元数据+取首页，结果持久化）
        src_keys = list(_GN_CATALOG_SRCS)
        try:
            det = await asyncio.to_thread(_gn_catalog_detect_keys)
            for k in det:
                if k not in src_keys:
                    src_keys.append(k)
        except Exception:  # noqa: BLE001
            _log.debug("_gn_catalog_worker failed", exc_info=True)
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
                        _log.debug("_gn_catalog_worker failed", exc_info=True)
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
                            _log.debug("_gn_catalog_worker failed", exc_info=True)
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
    from app import config, guard_native_enabled  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
    _g = _guard_mod()
    try:
        if _g is None:
            return
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
                _log.debug("fetch_page failed", exc_info=True)
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
                                _log.debug("_gn_deep_worker failed", exc_info=True)
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
        _log.debug("_gn_parse_cls_token failed", exc_info=True)
        return None


def _gn_folder_row(guid: str, title: str, subtitle: str = "",
                  poster: str = "") -> dict:
    """飞牛可下钻的目录行（type=Directory）；点击后以 parent_guid=本行 guid 再查列表。"""
    from app import _apply_marks  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
    if not features.GUARD:
        return []
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    _g = _guard_mod()
    if _g is None:
        return []
    out = []
    if not _g.available():
        return out
    for s in config.enabled_sources():
        if not s.get("guard"):
            continue
        conf = _g.site(s.get("guard" + "_key"))
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
    from app import _apply_marks, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    source = group["source"]
    # 让 inject.is_tv 能据显式词判定：TV→“电视剧”（且至少 1 集即判 TV），Movie→“电影”
    forced_type_name = {"TV": "电视剧", "Movie": "电影"}.get(force_type or "")
    titles, seen = [], set()
    for v in vods[:n]:
        try:
            t = providers._guard_map_item(source, v)
        except Exception:  # noqa: BLE001
            _log.debug("_gn_map_cards failed", exc_info=True)
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
                _log.debug("_gn_map_cards failed", exc_info=True)
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
    if not features.GUARD:
        return
    _g = _guard_mod()
    if _g is None:
        return

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
                    _log.debug("one failed", exc_info=True)
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
            _log.debug("_fetch failed", exc_info=True)
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
                    _log.debug("run failed", exc_info=True)
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
    _g = _guard_mod()
    if _g is None:
        return

    async def run():
        try:
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
                        _log.debug("run failed", exc_info=True)
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
        finally:
            # 无论成败都必须复位，否则一次异常会让分类元数据永久
            # 不再刷新（与 _gn_refresh_hot 的 hot_refreshing 复位同款写法）。
            with _gn_lock:
                _gn_state["meta_refreshing"] = False

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


def _gn_category_task(key: str, tid: str, pg: int = 1,
                      extend: dict | None = None,
                      force_type: str | None = None):
    """返回该分类页的在途取数任务（去重、持久，不被等待方超时取消）。"""
    _g = _guard_mod()
    ext = dict(extend or {})
    ckey = f"{key}|{tid}|{pg}{_gn_ext_key(ext)}"
    with _gn_lock:
        inflight = _gn_state.setdefault("inflight", {})
        existing = inflight.get(ckey)
        if existing and not existing.done():
            return existing
        groups = {g["conf"]["key"]: g for g in _gn_sources()}
        group = groups.get(key)
        if _g is None or not group:
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
                    _log.debug("fetch failed", exc_info=True)
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
                        _log.debug("fetch failed", exc_info=True)
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
    本次快速失败时返回 (rows=[], handled=False, more=True)：空页不代表“已处理”，
    调用方不得把“失败待重试”当成“分类本来就是空的”。
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
        # 本次快速失败，但持久任务仍在后台取数（结果落缓存，下次秒回）；
        # handled=False：本次未产出有效页，与“分类本来就是空的”区分开。
        _log.warning("_gn_category_page failed: key=%s tid=%s pg=%s",
                     key, tid, pg, exc_info=True)
        return [], False, True


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
                    _log.debug("_gn_warm_worker failed", exc_info=True)
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
    guard_native_enabled = importlib.import_module("app").guard_native_enabled  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    _g = _guard_mod()
    if _g is None:
        return
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
