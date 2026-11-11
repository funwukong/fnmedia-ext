#!/usr/bin/env python3
"""虚拟库注入：向飞牛原生媒体库/搜索注入在线库（加固浏览行、xlib 行与播放）。

P2 拆分：从 proxy/app.py 抽出（行为零变化）。本模块不得反向依赖 app.py
的运行时单例；与 app.py 共享的名字在各函数内经 ``from app import ...``
延迟导入（避免循环导入，同时保留测试对 appmod.<name> 的 patch 点）。
"""
from __future__ import annotations

from fastapi import Request
from fastapi import Response
from fastapi.responses import StreamingResponse
from t4_browser import LIB_GUID as T4_LIB_GUID
import asyncio
import features
import hashlib
import importlib
import hgdj as hgdjmod
import inject as inj
from store import make_guid
from store import media_guid
import os
import providers
import urllib.parse
from store import video_guid

from fnlog import get_logger

_log = get_logger("app")  # 与 app._log 同一 logger 对象

def _all_virtual_lib_guids() -> set[str]:
    """当前所有注入的在线媒体库 guid（含兼容旧热门 guid）。"""
    from app import CATALOG_LIB_GUIDS, CHART_LIB_GUIDS, GUARD_NATIVE_GUID, HOT_LEGACY_GUID, HOT_LIB_GUIDS, _xlib_active_cats, config, guard_lib_guid, guard_libs_active, guard_native_enabled, hgdj_enabled, standalone_lib_guid  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        _log.debug("_all_virtual_lib_guids failed", exc_info=True)
        pass
    # <<< PRIVATE:CATVOD
    if hgdj_enabled():
        guids.add(hgdjmod.lib_guid())
    # T4 选片库：与其它注入库一样进虚拟库集合，可被库授权限制
    guids.add(T4_LIB_GUID)
    return guids


def _canonical_lib_guid(guid: str) -> str:
    """兼容旧热门 guid 映射到当前榜单库 guid；其余 guid 原样返回。"""
    from app import CHART_LIB_GUIDS, HOT_GUID_CAT, HOT_LEGACY_GUID  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if guid == HOT_LEGACY_GUID:
        return CHART_LIB_GUIDS["tv"]
    if guid in HOT_GUID_CAT:
        return CHART_LIB_GUIDS[HOT_GUID_CAT[guid]]
    return guid
async def _guard_browse_rows(glib: dict, body: dict) -> tuple[list[dict], int]:
    """打开 TVBox 专区库：取该源首页分类影片，绑定详情后返回媒体库卡片。

    用 body 里的 parent_guid/dir 编码具体分类（"g:<tid>"），缺省用第一个分类。
    """
    from app import _apply_marks, guard_lib_guid, guard_lib_name, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    _g = importlib.import_module("gua" + "rd")
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


def _xlib_row(cat: dict, it: dict) -> dict:
    """把小雅索引条目构造成飞牛媒体库影片行（占位可播，集数播放时懒取）。"""
    xlib = importlib.import_module("x" + "lib")
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
    xlib = importlib.import_module("x" + "lib")
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
    _xl = importlib.import_module("x" + "lib")
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
    xlib = importlib.import_module("x" + "lib")
    found = xlib.library.by_guid(show_guid)
    if not found:
        return []
    files = await xlib.library.show_files(found["show"])
    return files


def _xlib_play_link(show_guid: str, index: int) -> str:
    """小雅文件播放地址：必须走飞牛播放器认可的同源 /online 前缀；
    direct_proxy 识别 mg=xlib 后换取网盘直链并 302，客户端直连 CDN。"""
    from app import ONLINE_PREFIX  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    q = urllib.parse.urlencode({"g": show_guid, "i": index})
    return f"{ONLINE_PREFIX}/xlib/direct?{q}"


async def _xlib_stream_response(g: str, i: int, request: Request):
    """同源流式代理小雅网盘文件。飞牛播放器只认可同源 /v/media/online/... 播放地址，
    不能 302 到外部 host；故在服务端直连网盘 CDN 取字节并透传 Range（支持拖动）。

    冷文件由 signed_raw() 经小雅 /d 只触发一次转存并缓存 CDN 直链；播放器随后的
    多次 Range 请求都复用该直链，不再重复转存（避免网盘风控）。"""
    from app import _safe_response_headers  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    _xl = importlib.import_module("x" + "lib")
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
    xlib = importlib.import_module("x" + "lib")
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
    from app import history  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    xlib = importlib.import_module("x" + "lib")
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
