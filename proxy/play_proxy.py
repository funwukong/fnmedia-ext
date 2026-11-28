#!/usr/bin/env python3
"""播放链：流容器 / 播放信息 / 起播 / 直链与媒体代理 / 收藏与已看 / 跳片头片尾。

v1.2.9 从 proxy/app.py 拆出（行为零变化）。路由挂在 play_router 上，由
app.py 在原有位置 extend，路由注册顺序与拆分前完全一致。

与 app.py 共享的名字一律在函数内经 ``from app import ...`` 延迟导入：
既避免循环导入，又保留测试对 ``appmod.<name>`` 的 patch 点（与 hls_race
的既有做法一致）。本模块不得反向持有 app 的运行时单例。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from fnlog import get_logger

_log = get_logger("app")  # 与 app._log 同一 logger 对象

# 与 app.ONLINE_PREFIX 同值（路由装饰器在定义时求值，无法经 app 懒取）。
ONLINE_PREFIX = "/v/media/online"

play_router = APIRouter()

@play_router.get("/v/api/v1/stream/list/{guid}")
async def stream_list(guid: str, request: Request):
    from app import _stream_list_impl  # noqa: E402 延迟导入：避免与 app.py 循环导入
    return await _stream_list_impl(guid, request)


@play_router.post("/v/api/v1/stream/list")
async def stream_list_post(request: Request):
    from app import _stream_list_impl, forward_upstream  # noqa: E402 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except ValueError:
        body = {}
    guid = str(body.get("guid") or body.get("item_guid") or "")
    if not guid:
        return await forward_upstream(request)
    return await _stream_list_impl(guid, request)


async def _stream_list_impl(guid: str, request: Request):
    from app import _CHART_FAILED_TS, _CHART_FAILED_TTL, _DURATION_PROBE_TIMEOUT, _NOT_HLS, _PENDING_MEDIA_CHART, _cached_playlist_seconds, _chart_key_open, _denied_lib_response, _get_playlist_cached, _instant_stream_payload, _is_pansou_line_name, _load_t4_detail, _monotonic, _online_guid_viewable, _playlist_total_seconds, _qualities, _remember_episode_duration, _seeded_media_target, _skip_adjusted_duration, _viewer_ctx, _warm_playlist, _xlib_on, _xlib_stream_payload, chart_cache, episode_guid, forward_upstream, hgdj_enabled, hgdjmod, history, inj, media_guid, start_resolve_chart, store, t4_media, t4_metadata, t4_service  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
                if _monotonic() - _CHART_FAILED_TS.get(guid, 0) < _CHART_FAILED_TTL:
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
    # 打开详情/线路菜单即后台预热默认线路播放列表（单飞，真正取 main 时并入）。
    # PanSou 是虚拟手动线，只在用户真正播放时才搜索转存，菜单阶段绝不预热。
    # （抄上游 v1.1.0 app.py:3480-3482）
    if target and not target.get("_pansou_manual") \
            and not _is_pansou_line_name(target.get("line", "")):
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
    from app import _parse_line_folder_guid, chart_cache, inj, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import CATALOG_GUID_CAT, CHART_GUID_CAT, _parse_line_item_guid, _parse_line_season_guid, chart_cache, hgdj_enabled, hgdjmod, is_hot_guid, standalone_source_of, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import _normalize_history_guid, marks  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import catalog, catalogmod, config, inj, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    try:
        if not config.read("related_enabled",True):
            return
    except Exception as exc:
        _log.debug("related config check failed: %s", type(exc).__name__)
        return
    try:
        from related import (DEFAULT_LIMIT, related_cache_lookup,
                             related_cache_store, related_for,
                             related_for_with_dense)
        pool = list(store._titles.values())
        # 内存工人 3：按 guid 缓存。失效依据 = 标题池代际 + dense 指纹；
        # 命中时不再全池扫描，dense 列表也不用现拼。
        pool_version = getattr(store, "titles_version", 0)
        dense_fp = catalog.dense_fingerprint()
        dense_len = sum(dense_fp)
        items = related_cache_lookup(
            brief, pool_version=pool_version, dense_fingerprint=dense_fp,
            pool_len=len(pool), dense_len=dense_len)
        if items is None:
            items = related_for(brief, pool)
            if len(items) < DEFAULT_LIMIT:
                dense: list[dict] = []
                for _cat in catalogmod.CATS:
                    dense.extend(catalog.cached_dense(_cat, 40))
                if dense:
                    items = related_for_with_dense(brief, pool, dense)
            related_cache_store(
                brief, items, pool_version=pool_version,
                dense_fingerprint=dense_fp, pool_len=len(pool),
                dense_len=dense_len)
    except Exception as exc:
        _log.debug("related failed: %s", type(exc).__name__)
        return
    if items:
        data["recommendations"] = [inj.search_item(b) for b in items]


def _mark_lib_keys(guid: str) -> list:
    """收藏/已看记录的归属库键：供 brief 淘汰后仍能按库过滤
    （见 _favorite_viewable）。live brief 优先；集节点取父标题；
    榜单收藏取榜单库键。"""
    from app import _chart_extra_keys, chart_cache, libfiltermod, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    brief = store.get_title_by_guid(guid)
    if brief is None:
        ep = store.get_episode(guid) or {}
        brief = store.get_title_by_guid(ep.get("online_guid", "")) or None
    if brief is not None:
        try:
            return sorted(libfiltermod.brief_lib_keys(brief))
        except Exception:
            _log.debug("_mark_lib_keys failed: guid=%s", guid, exc_info=True)
            return []
    if chart_cache.is_chart_guid(guid):
        return sorted(_chart_extra_keys(guid))
    return []


def _mark_extra(guid: str) -> dict:
    from app import _mark_lib_keys, _online_card_meta, inj, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.put("/v/api/v1/item/favorite")
async def item_favorite_add(request: Request):
    from app import _is_online_item_guid, _mark_extra, _normalize_history_guid, forward_upstream, marks  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.delete("/v/api/v1/item/favorite")
async def item_favorite_del(request: Request):
    from app import _is_online_item_guid, _normalize_history_guid, forward_upstream, marks  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.post("/v/api/v1/item/watched")
async def item_watched_set(request: Request):
    from app import _is_online_item_guid, _mark_extra, _normalize_history_guid, forward_upstream, marks  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.delete("/v/api/v1/item/watched")
async def item_watched_del(request: Request):
    from app import _is_online_item_guid, _normalize_history_guid, forward_upstream, marks  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import _apply_marks, _brief_speed_badge, _parse_line_folder_guid, chart_cache, chart_card, inj, store, video_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.post("/v/api/v1/favorite/list")
async def favorite_list(request: Request):
    """收藏夹：合并官方本地收藏与本插件在线收藏，统一分页。"""
    from app import _client_of, _favorite_card, _favorite_viewable, _viewer_ctx, clientmod, forward_upstream, marks  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import _attach_play_duration, _diag_play_info, _history_from_snapshot, _mirror_item_ts, history, inj, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    # 与 play/info 路由一致：先看该 guid 是否已是某个（动态解析出的）可播集
    ep = store.get_episode(guid)
    if ep:
        info = inj.episode_play_info(ep)
        # 续播位置：优先该集自己的记录；第 1 集没有集记录时回填标题记录的位置
        #（客户端以标题/季/文件夹 guid 上报进度时落到标题键上）。
        info["ts"] = history.position(
            guid, ep.get("online_guid"),
            is_first=(int(ep.get("index", 0) or 0) == 0))
        _mirror_item_ts(info)
        _attach_play_duration(info, ep=ep)
        return _diag_play_info(guid, info)
    brief = store.get_title_by_guid(guid)
    if not brief:
        # store 缓存淘汰兜底：用历史快照重建（续播不再依赖易失的搜索缓存）
        snap_rec = history.get(guid)
        if snap_rec is None:
            # 影片级 guid 请求：找该剧名下最近交互的集级记录
            #（同线按 snapshot.guid 命中；跨线需 store 解析标题，store 已
            # 淘汰时只剩同线可命中，见 latest_episode_record 注释）
            snap_rec = history.latest_episode_record(guid)
        if snap_rec:
            brief, snap_ep = _history_from_snapshot(snap_rec)
            if brief is not None:
                info = (inj.episode_play_info(snap_ep) if snap_ep
                        else inj.play_info(brief))
                info["ts"] = snap_rec.get("ts", 0)
                _mirror_item_ts(info)
                _attach_play_duration(info, ep=snap_ep, brief=brief)
                return _diag_play_info(guid, info)
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
                    _mirror_item_ts(info)
                    _attach_play_duration(info, ep=ep)
                    return _diag_play_info(guid, info)
    # store 剧集结构缺失（有 brief 但无可用集 guid）时，用该剧名下最近交互
    # 的集级记录快照恢复到具体集与 ts；T4 保留原语义，已看完的集记录不触发。
    # fork 适配：快照 ep 是记录那条线的集身份，store 有本线同集时换成本线
    # 的 ep（集 guid 按请求线路走，跨线口径不变）；store 里没有才用快照 ep。
    if brief.get("locator", {}).get("kind") != "t4":
        latest_ep_rec = history.latest_episode_record(guid)
        if latest_ep_rec and not history._finished(latest_ep_rec):
            _sb, _se = _history_from_snapshot(latest_ep_rec)
            if _se is not None:
                _idx = _se["index"]
                _egs = store.tv_episode_guids(guid, regular_only=True)
                _cur = None
                if 0 <= _idx < len(_egs):
                    _cur = store.get_episode(_egs[_idx])
                if _cur is not None:
                    info = inj.episode_play_info(_cur)
                    info["ts"] = latest_ep_rec.get("ts", 0)
                    _attach_play_duration(info, ep=_cur)
                else:
                    info = inj.episode_play_info(_se)
                    info["ts"] = latest_ep_rec.get("ts", 0)
                    _attach_play_duration(info, ep=_se, brief=_sb)
                _mirror_item_ts(info)
                return _diag_play_info(guid, info)
    info = inj.play_info(brief)
    if rec:
        info["ts"] = rec.get("ts", 0)
        _mirror_item_ts(info)
    _attach_play_duration(info, brief=brief)
    return _diag_play_info(guid, info)


@play_router.post("/v/api/v1/play/info")
async def play_info(request: Request):
    from app import _CHART_DETAIL_WAIT, _PENDING_MEDIA_CHART, _attach_play_duration, _chart_key_open, _client_of, _denied_lib_response, _diag_play_info, _load_t4_detail, _mirror_item_ts, _online_guid_viewable, _online_play_info, _parse_line_item_guid, _pick_episode, _viewer_ctx, _vod_play_info, _warm_playlist, _xlib_on, _xlib_play_info, chart_cache, clientmod, forward_upstream, hgdj_enabled, hgdjmod, history, inj, media_guid, start_resolve_chart, store, video_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
        _mirror_item_ts(info)
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
        # store 已淘汰：先试历史快照恢复（_online_play_info 的快照分支），
        # 恢复不到才 forward 上游。
        info = _online_play_info(guid, request)
        if info is not None:
            _prewarm_from_info(info)
            return JSONResponse({"code": 0, "msg": "",
                                 "data": clientmod.normalize_detail(info, client)})
        return await forward_upstream(request)
    info = _online_play_info(guid, request)
    if info is None:
        return await forward_upstream(request)
    _prewarm_from_info(info)
    return JSONResponse({"code": 0, "msg": "",
                         "data": clientmod.normalize_detail(info, client)})


def _skip_mark_guid(guid: str) -> str:
    """skip-marks 的标记身份：剧集统一归一到季 guid。

    episode_play_info 按 ep["season_guid"] 查标记；客户端回写可能带
    集 guid / 标题 guid / 线路季 guid / 季 guid，全部归一到季身份，
    否则写读两端身份不一致、标记永远查不到。电影与未知身份保持原 guid。
    线路季 guid 只可能来自剧集：直接按季号算季 guid，不依赖 store
    里还有没有这部剧（与上游的归一语义一致）。
    """
    from app import _normalize_history_guid, _parse_line_season_guid, inj, season_guid, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    if not guid:
        return ""
    season = _parse_line_season_guid(guid)
    if season:
        og, _line, sn = season
        return season_guid(og, sn)
    g = _normalize_history_guid(guid)
    ep = store.get_episode(g)
    if ep is not None:
        return ep.get("season_guid") or season_guid(
            ep.get("online_guid", "") or g)
    brief = store.get_title_by_guid(g)
    if brief is not None and inj.is_tv(brief):
        return season_guid(g)
    return g


@play_router.post("/v/api/v1/play/setConfigByItem")
async def play_set_config_by_item(request: Request):
    """飞牛原生跳片头/片尾配置；同时接受通用 opening/middle/ending 标记。

    body 直接是配置对象（不是 {data:{...}} 包装），与官方 Web 调用一致。
    标记身份经 _skip_mark_guid 归一到季 guid（剧集）/标题 guid（电影），
    与 play/info 的查标记身份一致，保证换线/换集后仍共享同一份标记。
    （移植自上游 yujian0815/fnmedia-ext：原生播放器只理解
    play_config.skip_opening / skip_ending（秒），并通过本接口写回。）
    """
    from app import _skip_mark_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    guid = _skip_mark_guid(
        str(body.get("guid") or body.get("parent_guid") or ""))
    if not guid:
        return JSONResponse({"code": 400, "msg": "缺少 guid", "data": None},
                            status_code=400)
    import skip_marks as _sm
    try:
        _sm.set_mark(guid, body)
    except ValueError as exc:
        return JSONResponse({"code": 400, "msg": str(exc), "data": None},
                            status_code=400)
    return JSONResponse({"code": 0, "msg": "", "data": {
        "guid": guid, "play_config": _sm.play_config(guid)}})


@play_router.get("/v/api/ext/skip-marks")
async def ext_skip_marks_get(request: Request):
    from app import _skip_mark_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
    import skip_marks as _sm
    guid = _skip_mark_guid(request.query_params.get("guid") or "")
    if not guid:
        return JSONResponse({"code": 400, "msg": "缺少 guid", "data": None},
                            status_code=400)
    return JSONResponse({"code": 0, "msg": "", "data": {
        "guid": guid, "mark": _sm.get(guid), "play_config": _sm.play_config(guid)}})


@play_router.delete("/v/api/ext/skip-marks")
async def ext_skip_marks_delete(request: Request):
    from app import _skip_mark_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
    import skip_marks as _sm
    guid = _skip_mark_guid(request.query_params.get("guid") or "")
    if not guid:
        return JSONResponse({"code": 400, "msg": "缺少 guid", "data": None},
                            status_code=400)
    _sm.delete(guid)
    return JSONResponse({"code": 0, "msg": "", "data": True})


def _pansou_real_lines(target: dict | None) -> list[str] | None:
    """该片当前可切的真实线路名（不含网盘线）。

    网盘线是虚拟手动线：**没有真实线路可回时绝不能把它设为当前线**，否则
    客户端线路菜单里只剩网盘线一项，用户「切回其它线路」没有目标，只能
    一直停在占位画面——正是用户报的现象。
    """
    from app import PANSOU_LINE, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    if not target:
        # 目标解析不出来（新片首次进播放页、mg 尚未绑定记录）：无从判断，
        # 返回 None 表示「不知道」，不阻断，交给下游原逻辑。
        return None
    og = target.get("online_guid") or ""
    try:
        if not og or store.get_title_by_guid(og) is None:
            # store 不认识这部片（榜单骨架/外部 guid/已被淘汰）：同样未知。
            return None
        return [t["line"] for t in store.target_lines(target)
                if t.get("line") and t.get("line") != PANSOU_LINE]
    except Exception:
        _log.debug("_pansou_real_lines failed", exc_info=True)
        return None


def _pansou_pick_usable(mg: str, target: dict | None = None) -> bool:
    """已有手切意图时，该片此刻是否仍该按网盘线播放。

    用于后续**不带 bitrate** 的 preset/main 请求（含播放中的分片轮询）：

    * 意图必须还在有效期内（``_pansou_is_user_pick``，含滑动续期）；
    * 必须还有真实线路可回，否则按真实线路播。
    """
    from app import _pansou_is_user_pick, _pansou_real_lines  # noqa: E402 延迟导入：避免与 app.py 循环导入
    if not _pansou_is_user_pick(mg):
        return False
    real = _pansou_real_lines(target)
    return True if real is None else bool(real)


def _pansou_switch_allowed(mg: str, target: dict | None = None) -> bool:
    """play/play 解码出「网盘线 bitrate」时，判定这是不是用户此刻真的在切。

    飞牛的清晰度选择是**跨片全局记忆**的：A 片切过网盘线后，点播 B 片仍会
    带同一个 bitrate 回来。所以命中网盘线编码时必须要求 **本片真的用真实
    线路起播过**（``_PANSOU_REAL_PLAY``，由 play/play 与 preset 拉着真实线路
    时登记）。新片没有这份证据 => 是跨片残留，按默认线路播。
    """
    from app import _pansou_real_lines, _pansou_real_played  # noqa: E402 延迟导入：避免与 app.py 循环导入
    if not _pansou_real_played(mg):
        return False
    real = _pansou_real_lines(target)
    return True if real is None else bool(real)


def _line_choices(target: dict, *, include_pansou: bool = True) -> list[dict]:
    """该播放目标可切换的线路：按资源站去重（同一站直链优先于 share 页），
    当前播放线路排第一；T4 保持稳定菜单顺序，防止切线后序号漂移。

    include_pansou=True 时在末尾全局追加一条“网盘高清（PanSou）”手动线
    （驱动模块在时）。该线不在 store、不进搜索、不参与测速，仅供用户
    手动切到后触发转存（见 proxy/pansou.py）。"""
    from app import PANSOU_LABEL, PANSOU_LINE, _PROVIDER_CN, _pansou_available, _pansou_manual_target, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
        if t.get("line") == PANSOU_LINE:
            continue
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
    # 全局追加网盘高清手动线（始终最后，且永不作为默认/竞速对象）。
    if include_pansou and _pansou_available():
        try:
            _idx = int(target.get("index", 0) or 0)
        except (TypeError, ValueError):
            _idx = 0
        if not any(t.get("line") == PANSOU_LINE for t in out):
            out.append({**_pansou_manual_target(_idx), "label": PANSOU_LABEL})
    # 保持当前线路第一
    out.sort(key=lambda t: t["line"] != cur)
    return out


from line_identity import bitrate as _stable_line_bitrate, line_of_bitrate as _stable_line_of_bitrate


def _line_bitrate(line: str, lines: list[str]) -> int:
    """线路 -> 播放器「清晰度」菜单里的整数值。

    **按线路名稳定编码，不按菜单下标**。飞牛播放器会记住上次选的档位并带到
    下一部片，而菜单里「当前线路置顶」会让下标整体漂移：网盘线一旦成为当前
    线，真实线路的编码全部 +1，播放器记着的「A 线」实际落到「B 线」，切回来
    就永远放不出画面（用户报的正是这个现象）。改按名字哈希后，同一线路在
    任何菜单顺序下都是同一个整数。
    """
    return _stable_line_bitrate(line)


def _line_of_bitrate(br, lines: list[str]) -> str | None:
    hit = _stable_line_of_bitrate(br, lines)
    if hit is not None:
        return hit
    try:
        b = int(br or 0)
    except (TypeError, ValueError):
        return None
    if not 90_000_000 <= b < 91_000_000:
        return None
    # 兼容旧编码（下标 x 10001）：只用于升级前播过的档位，以及外部直接传
    # 下标编码的调用方；本端新生成的 URL 永远走上面的精确分支。
    idx = (b - 90_000_000) // 10001
    if 0 <= idx < len(lines):
        return lines[idx]
    return None


def _qualities(target: dict) -> list[dict]:
    """把每条播放线路表达成播放器「清晰度」菜单里的一个可选项。"""
    from app import _friendly_line, _line_bitrate, _line_choices  # noqa: E402 延迟导入：避免与 app.py 循环导入
    lines = _line_choices(target)
    if len(lines) <= 1:
        return []
    names = [t["line"] for t in lines]
    out = []
    for t in lines:
        out.append({
            "resolution": t.get("label") or _friendly_line(t["line"]),
            # 与 _line_of_bitrate 同一套稳定编码：菜单顺序变了也不会让播放器
            # 记住的档位指向另一条线路。
            "bitrate": _line_bitrate(t["line"], names),
            "progressive": True,
            "is_m3u8": False,  # 置 False 才不会被菜单项置灰
        })
    return out


def _t4_selected_target(mg: str, target: dict, selection, request: Request) -> dict:
    """Use the same episode/line identity for stream, play and native Range."""
    from app import _line_choices, _line_of_bitrate, t4, t4_service  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.post("/v/api/v1/stream")
async def stream_post(request: Request):
    from app import T4_STREAM_READY_BUDGET, _XLIB_EP_MAP, _chart_key_open, _client_of, _denied_lib_response, _instant_stream_response, _line_bitrate, _line_choices, _online_target_viewable, _qualities, _seeded_media_target, _t4_media_url, _t4_selected_target, _viewer_ctx, _xlib_on, _xlib_recover_media, _xlib_resolve_media, _xlib_stream_payload, chart_guid_by_media, ext_client, forward_upstream, inj, start_resolve_chart, store, t4, t4_media, t4_metadata, t4_service  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.api_route("/v/api/v1/media/range/{mg}", methods=["GET", "HEAD"])
async def media_range(mg: str, request: Request):
    """App（Flutter 原生播放器）不走 /play/play，而是直接 GET /media/range/<mg>。

    官方后端对我们的虚拟 mg 无本地文件记录，会返回 403（App 提示“文件权限不足”）。
    T4 直接流式交付，保留 Range 和线路选择；CMS 引导到既有代理（302）。
    非在线 mg 原样流式透传官方。
    """
    from app import _CHART_FAILED_TS, _CHART_FAILED_TTL, _chart_key_open, _client_of, _denied_lib_response, _guess_wrapper, _monotonic, _online_target_viewable, _seeded_media_target, _viewer_ctx, _xlib_on, _xlib_play_link, _xlib_recover_media, _xlib_resolve_media, chart_guid_by_media, clientmod, forward_upstream_streaming, hls_main, start_resolve_chart, store, t4_range  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
        if _monotonic() - _CHART_FAILED_TS.get(chart_g, 0) < _CHART_FAILED_TTL:
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
        # iOS 原生 AVPlayer 需要直接获得最终主播放列表；TV/安卓仍保留
        # preset 占位与线路自修复入口，避免改变其既有起播协议。
        if _client_of(request).kind == clientmod.IOS:
            main_scope = dict(request.scope)
            main_scope["path"] = f"{ONLINE_PREFIX}/{mg}/main.m3u8"
            main_scope["query_string"] = q.encode()
            return await hls_main(mg, Request(main_scope))
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}",
                                status_code=302)
    return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)






def _guess_wrapper(target: dict) -> tuple[str, bool]:
    """不发起任何网络请求，预判该线路该用 HLS 还是 MP4 包装。
    返回 (wrapper, is_hls)。预判可能失误：main/direct 入口在真正解析后会做
    双向 302 兜底，所以这里宁可默认 HLS（绝大多数资源是 m3u8）。"""
    from app import _DDUO_DECODE_CACHE, features  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.post("/v/api/v1/play/play")
async def play_play(request: Request):
    from app import PANSOU_LINE, _chart_key_open, _denied_lib_response, _guess_wrapper, _instant_play_response, _is_pansou_line_name, _line_choices, _line_of_bitrate, _online_target_viewable, _pansou_clear_user_pick, _pansou_manual_arm, _pansou_mark_real_play, _pansou_mark_user_pick, _pansou_switch_allowed, _seeded_media_target, _t4_play_response, _t4_selected_target, _viewer_ctx, _warm_playlist, chart_guid_by_media, _client_of, clientmod, forward_upstream, hgdj_enabled, hgdjmod, start_resolve_chart, store, t4, vg_for  # noqa: E402 延迟导入：避免与 app.py 循环导入
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    # 原代码此函数未定义 client，HGDJ 分支引用会 NameError；拆分时补齐。
    client = _client_of(request)
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
    # 播放器「清晰度/线路」菜单切换时会带我们编码的 bitrate，据此选线路。
    # 注意：飞牛的清晰度选择是**跨片全局记忆**的——上一部片切到网盘线后，
    # 点播下一部片仍会带同一个 bitrate 回来。所以这里的命中不是「用户想
    # 切到那条线」，只是「播放器记得上次的档位」。命中网盘线时必须另有
    # 本片的起播证据（见 _pansou_switch_allowed），否则按默认线路起播。
    choices = _line_choices(base_target)
    chosen_line = None
    if len(choices) > 1:
        chosen_line = _line_of_bitrate(body.get("bitrate"), [t["line"] for t in choices])
        if chosen_line and _is_pansou_line_name(chosen_line):
            if not _pansou_switch_allowed(mg, base_target):
                # 跨片残留的档位 / 没有真实线路可回：按默认线路起播，
                # 并清掉可能过期的意图，避免后续 preset 又被拉回网盘线。
                chosen_line = None
                _pansou_clear_user_pick(mg)
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
    # 手切 PanSou 网盘高清线（线路菜单 bitrate 或线路墙卡种子）：登记手切意图
    # 并 arm（带片名），播放地址直挂 PanSou 占位 preset；真正搜索+转存延迟
    # 到播放器拉占位分片时（placeholder kick），不预热、不参与测速。
    # 网盘永不作跨片默认：无意图的 bitrate 命中不会走到这里（_pick_episode
    # 与 _line_choices 只在有意图时才把该线当真线）。
    if chosen_line == PANSOU_LINE or target.get("line") == PANSOU_LINE:
        _pansou_mark_user_pick(mg)
        _pidx = int(base_target.get("index", 0) or 0)
        try:
            _t0 = base_target.get("title") or base_target.get("ep_title") or ""
            if not _t0:
                _og0 = base_target.get("online_guid") or store.online_guid_by_media(mg)
                _rec0 = store.get_title_by_guid(_og0) if _og0 else None
                _t0 = (_rec0 or {}).get("title") or ""
            if _t0:
                _pansou_manual_arm(mg, _t0, _pidx)
        except Exception:
            _log.debug("stream pansou arm failed", exc_info=True)
        _pq = urllib.parse.urlencode({"line": PANSOU_LINE, "index": _pidx})
        return JSONResponse({"code": 0, "msg": "", "data": {
            "audio_guid": "", "audio_index": -1, "hls_time": 6,
            "is_subtitle_external": 0, "media_guid": mg,
            "play_link": f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{_pq}",
            "subtitle_guid": "", "subtitle_index": -1,
            "subtitle_link": "", "video_guid": body.get("video_guid", vg_for(mg)),
            "video_index": 0, "wrapper": "HLS",
            "non_fatal_errno": None, "video_encoder": "",
            "not_supported_resolution": "", "supported_highest_resolution": ""}})
    # 起播体验：此处【不再同步解析】播放页/302/专有解码（慢源要 1~3 秒，会把用户
    # 卡在海报墙像“点了没反应”）。先瞬时返回播放地址让播放器立刻进入加载页转圈，
    # 真正的解析延迟到播放器内部取 main.m3u8 / direct 时再做（那里本就有加载指示）。
    # 走到这里 = 用真实线路起播（上面网盘分支已早返回）。登记为本片的
    # 「真实起播证据」：之后从播放中的清晰度菜单切网盘线才被认。
    try:
        _pansou_mark_real_play(mg)
    except Exception:
        _log.debug("pansou mark real play failed", exc_info=True)
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


@play_router.get(f"{ONLINE_PREFIX}/hgdj/{{series_guid}}.mp4")
async def hgdj_media_proxy(series_guid: str, request: Request):
    """代理 HGDJ companion 播放流：GET /api/play/<sid>/<ep>，透传 Range。

    注意：app 底层还有一个 /{mg} 通配符路由，会优先拦截以 "/" 开头的任意 mg。
    这里显式用 /hgdj/{series_guid}.mp4，与 play_link 生成的一致。
    """
    from app import _denied_lib_response, _viewer_ctx, hgdjmod  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.get(f"{ONLINE_PREFIX}/{{mg}}/direct")
async def direct_proxy(mg: str, request: Request):
    """mp4 等非 HLS 直链代理：同源、带 Range 支持（播放器 seek）。"""
    from app import _autofail_on, _await_pending_chart, _chart_key_open, _client_direct_only, _dduo_strip_referer, _denied_lib_response, _direct_to_placeholder, _ensure_playable, _find_playable, _mark_line_bad, _online_target_viewable, _probe_target, _resolve_if_needed, _safe_response_headers, _self_url, _viewer_ctx, _xlib_direct_redirect, _xlib_on, chart_guid_by_media, ext_client, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    if target.get("catvod_direct"):
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
    if _dduo_strip_referer(target):
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
    from app import ep_video_guid, store, video_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
    t = store.resolve_media_guid(mg)
    if not t:
        return ""
    if t.get("kind") == "episode":
        return ep_video_guid(t["ep_guid"])
    return video_guid(t["online_guid"])


@play_router.post("/v/api/v1/person/list/{guid}")
async def person_list(guid: str, request: Request):
    from app import forward_upstream, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    if not store.get_title_by_guid(guid) and not store.get_episode(guid):
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "", "data": {"total": 0, "list": []}})


def _history_snapshot(raw_guid: str, norm_guid: str) -> dict | None:
    """为一次播放记录构造精简 brief 快照（离线容错，任何异常返回 None）。

    store 缓存可能已淘汰该片；快照让续播不依赖 store 是否仍保留。剧集按归一化
    后的集 guid 提取当前集线路条目，电影存首条可播线路。
    """
    from app import _parse_line_item_guid, histmod, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    try:
        ep = store.get_episode(norm_guid)
        brief = None
        if ep:
            brief = store.get_title_by_guid(ep.get("online_guid", ""))
        if brief is None:
            brief = store.get_title_by_guid(norm_guid)
        if brief is None:
            # 复合线路卡：解析出原影片 guid 再试
            parsed = _parse_line_item_guid(raw_guid)
            if parsed:
                brief = store.get_title_by_guid(parsed[0])
                if brief is None and ep is None:
                    ep = store.get_episode(norm_guid) or None
        if brief is None:
            return None
        return histmod.build_snapshot(brief, norm_guid, ep)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError,
            OverflowError):  # 快照失败不得影响进度记录主流程
        return None


def _history_from_snapshot(rec: dict):
    """store 缓存淘汰后，用历史快照重建 (brief, ep)。

    返回 (brief, ep_or_None)；快照缺失/无效返回 (None, None)。
    剧集快照的 lines 只含当前集条目，据此构造最小 ep 供 history_item/play_info 复用。

    fork 适配：ep 带上记录里的 duration——episode_duration 优先读它，
    淘汰后 play/info 仍有合法时长（客户端续播守门用），不依赖 store。
    注意：不把精简快照回注 store 主缓存。快照每条线路只含当前集，回注会
    覆盖 store 里可能存在的完整 brief（导致 is_tv 误判、ep_guid 对不上）。
    快照仅在本次请求内用于构造 play/info 卡片，store 仍由搜索链路自行维护。
    快照内直链可能已过期：这里只恢复标题/集身份/ts，不校验直链有效性；
    过期直链由后续取流/重搜链路补线（_slim 快照连 lines 都没有，同样走重搜）。
    """
    from app import season_guid  # noqa: E402 延迟导入：避免与 app.py 循环导入
    try:
        snap = rec.get("snapshot")
        if not isinstance(snap, dict) or not snap.get("guid"):
            return None, None
        brief = dict(snap)
        ig = rec.get("item_guid", "")
        # 集序号：真实 ep guid 是 hash 无法反解，依赖快照写入时存的 _ep_index
        ep_idx = snap.get("_ep_index")
        if ep_idx is None and isinstance(ig, str) and ig.startswith("ep:"):
            try:
                ep_idx = int(ig.rsplit(":", 1)[-1])
            except ValueError:
                ep_idx = None
        ep = None
        if ep_idx is not None:
            try:
                ep_idx = int(ep_idx)
            except (TypeError, ValueError):
                ep_idx = None
        if ep_idx is not None:
            lines = brief.get("lines") or {}
            default_line = next(iter(lines), "")
            ep = {
                "ep_guid": snap.get("_ep_guid") or ig,
                "online_guid": brief["guid"],
                "index": ep_idx,
                "episode_number": ep_idx + 1,
                "season": 1,
                "season_guid": season_guid(brief["guid"]),
                "title": brief.get("title", ""),
                "default_line": default_line,
                "lines": list(lines.keys()),
                "ep_title": (lines.get(default_line) or [{}])[0].get("title", "")
                if default_line else "",
                "line": default_line,
                "duration": int(rec.get("duration", 0) or 0),
            }
        return brief, ep
    except (TypeError, ValueError, KeyError, AttributeError, IndexError,
            OverflowError):
        # 数据形态失败才静默回退（快照来自 history.json，字段可能缺失/类型错乱，
        # 如 duration="abc" 或 int(inf)；其余异常应上抛而非吞掉，便于测试发现）。
        return None, None


@play_router.post("/v/api/v1/play/record")
async def play_record(request: Request):
    from app import _diag_record_canon_key, _history_snapshot, _record_item_guid, chart_cache, forward_upstream, history, resume_diagmod, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
        history.record(body, snapshot=_history_snapshot(raw_guid, ig))
        try:
            resume_diagmod.note_record(raw_guid, ig, body,
                                       outcome="history", parsed=parsed,
                                       canon_key=_diag_record_canon_key(ig))
        except Exception:
            _log.debug("play_record failed", exc_info=True)
            pass
        return JSONResponse({"code": 0, "msg": "", "data": True})
    if history.get(ig):
        # store 缓存可能已淘汰该片，但 history 里已有该 guid（含快照）：进度仍应更新，
        # 不能因此 forward 到官方（否则在线片的续播点会丢失、从头播放）。
        # 快照沿用旧的（不重算——store 里已无 brief 可算）；record() 会把它
        # 落回原规范键桶。
        history.record(body, snapshot=history.get_snapshot(ig))
        try:
            resume_diagmod.note_record(raw_guid, ig, body,
                                       outcome="history", parsed=parsed,
                                       canon_key=_diag_record_canon_key(ig))
        except (TypeError, ValueError, KeyError, AttributeError, IndexError,
                OSError):
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


@play_router.delete("/v/api/v1/play/record")
async def play_record_delete(request: Request):
    from app import _normalize_history_guid, forward_upstream, history, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


@play_router.get("/v/api/v1/play/list")
async def play_list(request: Request):
    from app import _brief_viewable, _chart_extra_keys, _client_of, _history_from_snapshot, _viewer_ctx, chart_cache, clientmod, forward_upstream, history, inj, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    best_by_show: dict = {}
    standalone = []
    for rec in history.all():
        ig = rec.get("item_guid", "")
        ep = store.get_episode(ig)
        brief = store.get_title_by_guid(ig)
        if ep:
            brief = store.get_title_by_guid(ep["online_guid"])
        if not brief:
            # store 缓存已淘汰：用历史快照重建，使「继续播放」不丢失
            brief, snap_ep = _history_from_snapshot(rec)
            if snap_ep and not ep:
                ep = snap_ep
        if not brief:
            continue  # 既无缓存也无快照（旧记录），无法展示
        extra = _chart_extra_keys(ig) if chart_cache.is_chart_guid(ig) \
            else None
        if not _brief_viewable(request, brief, extra_keys=extra, _ctx=_viewer_ctx(request)):
            continue  # P0-3：权限收回后旧记录不再展示
        card = inj.history_item(brief, rec, ep)
        # 同一部剧可能同时有影片级记录和各集记录：集级（快照带 _ep_index）才是
        # 真实续播点，优先于影片级；同级再按最近交互时间。
        is_ep_rec = isinstance(rec.get("snapshot"), dict) and \
            rec["snapshot"].get("_ep_index") is not None
        show = ep.get("online_guid") if ep else brief.get("guid") or ig
        up = float(rec.get("updated_at", 0) or 0)
        rank = (1 if is_ep_rec else 0, up)
        if show:
            prev = best_by_show.get(show)
            if prev is None or rank >= prev[0]:
                best_by_show[show] = (rank, card)
        else:
            standalone.append(card)
    online_items = [c for _, c in best_by_show.values()] + standalone

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
@play_router.post("/v/api/v1/media/p")
async def media_p(request: Request):
    from app import _line_choices, _line_of_bitrate, _t4_selected_target, ext_client, forward_upstream, t4, t4_media, t4_service  # noqa: E402 延迟导入：避免与 app.py 循环导入
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


# >>> PRIVATE:PAN
async def _resolve_pan_share(picked: dict) -> bool:
    """网盘分享链接 → 转存 → 可播放地址（上游 app.py 同款分支）。

    上游（完整版 app.py:8120 起）就是这样做的：_pan_match 识别是哪家网盘
    → mod.resolve_share(share, pwd) 转存拿到 items[{fid,...}] → 把 picked.url
    换成本服务的 /v/api/ext/pan/{key}/play?fid=... 并标记 picked["pan"]=True。
    我们之前只有网页嗅探（providers.resolve_share），对网盘分享链接无效，
    导致「网盘结果在线路里播不出来」。

    返回 True 表示已解析成可播地址。
    """
    if not isinstance(picked, dict):
        return False
    raw = str(picked.get("url") or "")
    if not raw:
        return False
    import asyncio as _aio
    import urllib.parse as _up
    from panmatch import _PAN_LINKSTART_RE, _PAN_PROVIDERS, _pan_match

    pv = _pan_match(raw)
    share = ""
    if pv:
        m = pv["re"].search(raw)
        if not m:
            return False
        # 分享地址用【完整原始 URL】：网盘正则不覆盖 ?pwd= 提取码，
        # 截断后 parse_share 取不到提取码，转存会失败（分享需要提取码时）。
        # 只在没有提取码参数时用正则匹配段兜底。
        if re.search(r"[?&](?:pwd|passcode)=", raw):
            share = raw
        else:
            share = m.group(0)
            if not share.lower().startswith(("http://", "https://")):
                share = "https://" + share
    else:
        # 四盘聚合（Seedhub）中转：跟随 302 解真实链接。
        lm = _PAN_LINKSTART_RE.search(raw)
        if not lm:
            return False
        try:
            import pan as _panq
            resolved = await _aio.to_thread(_panq.resolve_linkstart, lm.group(1))
        except Exception:
            _log.debug("_resolve_pan_share linkstart failed", exc_info=True)
            return False
        if not resolved:
            picked["_pan_error"] = ("该资源是四盘聚合中转，本机无法自动解出真实网盘链接")
            return False
        pv = _pan_match(resolved) or _PAN_PROVIDERS[0]
        share = resolved

    if not pv or not pv.get("playable"):
        picked["_pan_error"] = ("该资源来自暂未支持的网盘家"
                                "（目前支持夸克 / UC / 阿里）")
        return False
    key = pv["key"]
    mod = None
    for _k, _m in (("quark", "pan"), ("uc", "panuc"), ("ali", "panali")):
        if _k != key:
            continue
        try:
            mod = __import__(_m)
        except Exception:
            mod = None
        break
    if mod is None:
        picked["_pan_error"] = "当前构建不含该网盘的播放模块"
        return False
    hint = _up.parse_qs(_up.urlparse(share).query).get("pwd", [""])[0]
    try:
        r = await _aio.to_thread(mod.resolve_share, share, hint)
    except Exception as exc:  # noqa: BLE001
        picked["_pan_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return False
    items = [x for x in (r.get("items") or []) if x.get("url")]
    if not items:
        picked["_pan_error"] = "分享内没有可播放的视频文件"
        return False
    try:
        idx = int(picked.get("index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    item = items[min(idx, len(items) - 1)]
    play_path = ("/v/api/ext/pan/play" if key == "quark"
                 else f"/v/api/ext/pan/{key}/play")
    picked["url"] = f"{play_path}?fid={item['fid']}"
    picked["resolve"] = False
    picked["pan"] = True
    picked["pan_provider"] = key
    picked["headers"] = {"User-Agent": "Mozilla/5.0"}
    picked["m3u8"] = False
    return True
# <<< PRIVATE:PAN


def _request_without_line(request: Request) -> Request:
    """复制一个去掉 query 里 line 参数的 Request（浅拷贝 scope，state 共享）。

    PanSou 手动线无手切意图时调用：下游选线按“无显式线”走默认/CMS 线，
    而不是拿 PANSOU_LINE 去 store 里解析出 None。"""
    try:
        qs = urllib.parse.parse_qsl(request.url.query, keep_blank_values=True)
        qs = [(k, v) for k, v in qs if k != "line"]
        scope = dict(request.scope)
        scope["query_string"] = urllib.parse.urlencode(qs).encode()
        return Request(scope, request.receive)
    except Exception:
        _log.debug("_request_without_line failed", exc_info=True)
        return request


def _pick_episode(mg: str, request: Request) -> dict | None:
    # PanSou 手动线（上游 hls_preset/main 同款语义）：显式点网盘线时，
    # 无手切意图一律忽略该线、走正常选线（网盘永不作默认/残留默认）；
    # 有意图时返回手动 target（ready 时直接返回转存快照），不解析 store。
    from app import PANSOU_LINE, _MEDIA_REDIRECT, _PENDING_MEDIA_CHART, _is_pansou_line_name, _line_key_match, _pansou_manual_state, _pansou_manual_target, _pansou_mark_user_pick, _pansou_pick_usable, _pansou_reset, _pansou_snapshot, _request_without_line, _seeded_media_target, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
    _qline = request.query_params.get("line")
    if _qline and _is_pansou_line_name(_qline):
        if not _pansou_pick_usable(mg, store.resolve_media_guid(mg)):
            # 无意图 / 意图过期 / 本片从未真实起播 / 已无真实线路可回：
            # 一律忽略该线按正常选线，网盘永不作跨片残留默认。
            request = _request_without_line(request)
        else:
            try:
                _idx = int(request.query_params.get("index", "0"))
            except ValueError:
                _idx = 0
            if _pansou_manual_state(mg) == "ready":
                # 快照必须校验新鲜度：网盘直链 2~4h 失效，过期快照会让
                # 播放拿到死链（「同一集有时能播有时不能」）。过期则退
                # armed 重转存，下面走 manual_target 分支。
                _snap = _pansou_snapshot(mg, mark_stale=True)
                if _snap:
                    _base = store.resolve_media_guid(mg)
                    _og = (_base or {}).get("online_guid") or \
                        store.online_guid_by_media(mg) or ""
                    return dict(_snap, index=_idx, online_guid=_og,
                                title=(_base or {}).get("title") or _snap.get("title", ""),
                                kind=(_base or {}).get("kind") or "title")
            _base = store.resolve_media_guid(mg)
            _og = (_base or {}).get("online_guid") or \
                store.online_guid_by_media(mg) or ""
            _title = ""
            if _base:
                _title = _base.get("title") or _base.get("ep_title") or ""
            if not _title and _og:
                _rec = store.get_title_by_guid(_og)
                _title = (_rec or {}).get("title") or ""
            return dict(_pansou_manual_target(_idx), online_guid=_og,
                        title=_title,
                        kind=(_base or {}).get("kind") or "title")
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
            # 手切离开网盘线（或从别的线切到网盘线）时同步清/设手切意图：
            # 意图残留会让后续不带 line 的 master 请求又被拉回网盘线。
            if qline == PANSOU_LINE:
                _pansou_mark_user_pick(mg)
            else:
                # 明确切到真实线路：只清手切意图，保留已转存的网盘快照
                # （切回网盘线时免重转存），且清除网盘态避免残留臂。
                _pansou_reset(mg)
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
    from app import _autofail_on, _episode_candidates, _line_is_bad, sourcehealth  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import _CHART_PLAY_WAIT, _PENDING_MEDIA_CHART, chart_cache, resolve_chart, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import b64e  # noqa: E402 延迟导入：避免与 app.py 循环导入
    suffix = f"&{qs}" if qs else ""
    return f"{ONLINE_PREFIX}/{mg}/seg?u={b64e(absolute)}{suffix}"


# >>> PRIVATE:DDUO
_DDUO_DECODE_CACHE: dict[str, tuple[float, str]] = {}
_DDUO_CACHE_TTL = 50 * 60  # 解出的地址多为带时效签名，缓存不到 1 小时


async def _resolve_dduo(picked: dict, request: Request) -> bool:
    """多多影视：把加密 token 解码为真实媒体地址（原地更新）。成功返回 True。"""
    from app import _DDUO_CACHE_TTL, _DDUO_DECODE_CACHE, config, ext_client, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import _resolve_dduo, store  # noqa: E402 延迟导入：避免与 app.py 循环导入
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
    from app import config  # noqa: E402 延迟导入：避免与 app.py 循环导入
    pf = picked.get("play_from", "")
    sid = pf.split(":", 1)[0] if ":" in pf else pf
    for s in config.all_sources():
        if s.get("drpy") and s.get("id") == sid:
            return s
    return None


async def _resolve_drpy(picked: dict) -> bool:
    """drpyS JS 爬虫：用 Node sidecar 的 playerContent 解析真实播放地址。"""
    from app import _drpy_source_for, providers  # noqa: E402 延迟导入：避免与 app.py 循环导入
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



def _safe_index(picked: dict) -> int:
    """集序号容错取值：缺失/非数字/负数一律按 0 集处理，绝不抛错。
    集序号读取统一走这里，保持缺失或非法集序号的兼容处理。"""
    try:
        return max(0, int(picked.get("index", 0) or 0))
    except (TypeError, ValueError):
        return 0


