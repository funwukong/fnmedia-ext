#!/usr/bin/env python3
"""管理 API（/v/api/ext/* 管理端点）。

P2 拆分：从 proxy/app.py 抽出（行为零变化）。本模块不得反向依赖 app.py
的运行时单例；与 app.py 共享的名字在各函数内经 ``from app import ...``
延迟导入（避免循环导入，同时保留测试对 appmod.<name> 的 patch 点）。
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi import Request
from fastapi import Response
from fastapi.responses import StreamingResponse
from t4_browser import LIB_GUID as T4_LIB_GUID
from t4_browser import LIB_TITLE as T4_LIB_TITLE
import admin_auth as admin_authmod
import asyncio
import base64
from catalog import catalog
import catalog as catalogmod
import config as cfgmod
import chartbind
import charts as chartmod
import domain_heal as domain_heal_mod
import features
import fnusers as fnusersmod
from follow import follow_list
import hgdj as hgdjmod
from hot import hot_cache
import httpx
import inject as inj
import json
import linepredict
import live as livemod
from store import make_guid
from store import media_guid
import metadata as metadata_mod
import netutil
import os
import providers
import re
import resume_diag as resume_diagmod
import sourcehealth
import threading
import time
import updater
import urllib.parse
import vlibs as vlibsmod

from fnlog import get_logger

_log = get_logger("app")  # 与 app._log 同一 logger 对象

from fastapi import APIRouter

admin_router = APIRouter()
@admin_router.get("/v/api/ext/whoami")
async def ext_whoami(request: Request):
    """回显当前请求被判定为哪种客户端，供在出问题的设备上打开管理台自检。"""
    from app import _client_of, app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    cl = _client_of(request)
    return {"code": 0, "msg": "", "data": {
        "kind": cl.kind, "label": cl.label, "is_tv": cl.is_tv,
        "is_mobile": cl.is_mobile, "is_web": cl.is_web,
        "strict_card": cl.strict_card,
        "ua": request.headers.get("user-agent", "")}}


@admin_router.get("/v/api/ext/live")
async def ext_live(request: Request):
    """实时播放诊断：各端最近请求、当前播放会话、转发来源与错误。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return {"code": 0, "msg": "", "data": livemod.snapshot()}


@admin_router.post("/v/api/ext/live/clear")
async def ext_live_clear(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    livemod.reset()
    return {"code": 0, "msg": "", "data": True}


# ---------------- 缓存管理（移植上游 v0.2.54） ----------------
@admin_router.get("/v/api/ext/titles-cache")
async def ext_titles_cache(request: Request):
    """在线资源缓存 titles.json 的条数/字节（管理页性能卡，issue #42）。
    文件超过配置字节上限时，顺手调度一次后台裁剪落盘（重启后打开管理页即自动瘦身）。"""
    from app import app, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        p = store._state
        size = p.stat().st_size if p.exists() else 0
    except Exception:
        _log.debug("ext_titles_cache failed", exc_info=True)
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
        _log.debug("ext_titles_cache failed", exc_info=True)
        pass
    return {"code": 0, "entries": entries, "bytes": size, "dirty": dirty}


@admin_router.post("/v/api/ext/titles-cache/clear")
async def ext_titles_cache_clear(request: Request):
    """清空在线资源缓存并重启服务（慢盘用户搜索退化时一键回到冷态，issue #42）。"""
    from app import app, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        store.close()
        with store._lock:
            store._titles, store._orig = {}, {}
            store._ep_index, store._media_index = {}, {}
            store._dirty = False
        try:
            store._state.unlink(missing_ok=True)
        except Exception:
            _log.debug("ext_titles_cache_clear failed", exc_info=True)
            pass
    except Exception as exc:
        _log.debug("ext_titles_cache_clear failed", exc_info=True)
        return {"code": 5000, "msg": f"清空失败：{exc}"}

    def _restart():
        import os
        time.sleep(1.2)
        os.system("systemctl restart fnmedia-ext >/dev/null 2>&1 &")

    threading.Thread(target=_restart, daemon=True).start()
    return {"code": 0, "msg": "已清空，服务重启中"}


def _cache_sizes() -> dict:
    """各类可清缓存的体积/条数（管理页缓存管理卡）。"""
    from app import _search_cache, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    out = {}
    try:
        p = store._state
        out["titles"] = {"bytes": p.stat().st_size if p.exists() else 0,
                         "entries": len(store._titles), "label": "在线资源缓存"}
    except Exception:
        _log.debug("_cache_sizes failed", exc_info=True)
        out["titles"] = {"bytes": 0, "entries": 0, "label": "在线资源缓存"}
    try:
        cp = chartmod.STATE
        out["charts"] = {"bytes": cp.stat().st_size if cp.exists() else 0,
                         "label": "热门榜单缓存"}
    except Exception:
        _log.debug("_cache_sizes failed", exc_info=True)
        out["charts"] = {"bytes": 0, "label": "热门榜单缓存"}
    with catalog._lock:
        out["catalog"] = {"routes": len(catalog._routes),
                          "pages": len(catalog._page_cache),
                          "label": "海量库分类/分页缓存"}
    try:
        out["search"] = {"entries": len(_search_cache), "label": "搜索结果内存缓存"}
    except Exception:
        _log.debug("_cache_sizes failed", exc_info=True)
        out["search"] = {"entries": 0, "label": "搜索结果内存缓存"}
    try:
        out["health"] = {"entries": len(sourcehealth.snapshot())
                         if hasattr(sourcehealth, "snapshot") else 0,
                         "label": "源健康画像"}
    except Exception:
        _log.debug("_cache_sizes failed", exc_info=True)
        out["health"] = {"entries": 0, "label": "源健康画像"}
    return out


@admin_router.get("/v/api/ext/cache")
async def ext_cache_status(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return {"code": 0, "data": _cache_sizes()}


@admin_router.post("/v/api/ext/cache/clear")
async def ext_cache_clear(request: Request):
    """一键清除指定缓存。targets: search/charts/catalog/titles/health 的子集；
    省略或 ["all"] 清前四项（health 默认保留，避免冷启动选源退化）。"""
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, chart_cache, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("ext_cache_clear failed", exc_info=True)
            pass
    if "catalog" in targets:
        try:
            catalog.reset(); done.append("catalog")
            _schedule_routes_refresh()
        except Exception:
            _log.debug("ext_cache_clear failed", exc_info=True)
            pass
    if "health" in targets:
        try:
            sourcehealth.reset(); done.append("health")
        except Exception:
            _log.debug("ext_cache_clear failed", exc_info=True)
            pass
    if "titles" in targets:
        need_restart = True
        done.append("titles")

        def _clear_titles_restart():
            from app import store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
            import os
            try:
                store.close()
                with store._lock:
                    store._titles, store._orig = {}, {}
                    store._ep_index, store._media_index = {}, {}
                    store._dirty = False
                store._state.unlink(missing_ok=True)
            except Exception:
                _log.debug("_clear_titles_restart failed", exc_info=True)
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
        _log.debug("_admin_client_key failed", exc_info=True)
        pass
    return "unknown"


def _admin_limiter_from_config():
    """按当前配置刷新限流阈值（计数不清零）。"""
    from app import _ADMIN_LOGIN_LIMITER, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    cfg = config.get()
    _ADMIN_LOGIN_LIMITER.configure(
        cfg.get("admin_auth_max_failures") or admin_authmod.DEFAULT_MAX_FAILURES,
        cfg.get("admin_auth_lockout_seconds") if cfg.get("admin_auth_lockout_seconds") is not None else admin_authmod.DEFAULT_LOCKOUT_SECONDS,
        cfg.get("admin_auth_window_seconds") or admin_authmod.DEFAULT_WINDOW_SECONDS)
    return _ADMIN_LOGIN_LIMITER


@admin_router.get("/v/api/ext/admin/token")
async def ext_admin_token_status(request: Request):
    """管理令牌状态：掩码、长度、年龄、过期策略、宽限剩余、限流阈值。
    永不返回令牌原文（原文只在轮换成功那一次返回）。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return {"code": 0, "msg": "", "data": config.admin_token_info()}


@admin_router.post("/v/api/ext/admin/login")
async def ext_admin_login(request: Request):
    """管理令牌登录校验（检查点，供后续鉴权门与管理页“验证令牌”用）。
    失败记入限流（按 IP 与令牌指纹分别计数）；锁定中返回 429。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except Exception:
        _log.debug("ext_admin_login failed", exc_info=True)
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


@admin_router.post("/v/api/ext/admin/token/rotate")
async def ext_admin_token_rotate(request: Request):
    """轮换管理令牌：旧令牌在宽限期（默认 24 小时）内仍可登录。
    须 body {"confirm": true}。新令牌原文只在本次响应里返回一次，
    请立即复制保存；刷新页面后不再显示原文（只显示掩码）。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except Exception:
        _log.debug("ext_admin_token_rotate failed", exc_info=True)
        body = {}
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return JSONResponse({"code": 400, "msg": "轮换须显式确认（confirm=true）"},
                            status_code=400)
    new_token, info = config.rotate_admin_token()
    return {"code": 0, "msg": "已重新生成，旧令牌在宽限期内仍可用",
            "data": {"new_token": new_token, "token": info}}


# ------------------------------------------------------- 电视直播（IPTV）管理 API


@admin_router.get("/v/api/ext/status")
async def ext_status(request: Request):
    from app import BUILD_LABEL, app, chart_cache, config, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.get("/v/api/ext/diag/resume")
async def ext_diag_resume(request: Request, guid: str = ""):
    """续播诊断（只读，内存）：最近 20 条续播事件 + 指定 guid 在各分区里的记录。

    用法：真机播 30 秒退出、重进一次，再打开本端点，把 JSON 发回排查。
    只含 guid / 分区键 / ts / duration / updated_at，不含任何 token 或凭证原文。
    lookup 按 guid 与归一化后的 guid 两个键查（文件夹/线路卡 guid 上报的
    进度落在归一化后的标题/集键上）；给定 guid 能解析出规范键时，也查
    该规范键下的记录（跨线命中的那条，不用自己拼 canon:… 键）。
    """
    from app import _normalize_history_guid, app, history  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    g = (guid or "").strip()
    keys = []
    if g:
        keys = [g]
        norm = _normalize_history_guid(g)
        if norm and norm != g:
            keys.append(norm)
    return {"code": 0, "msg": "", "data": {
        "events": resume_diagmod.events(),
        "lookup": resume_diagmod.lookup(history, keys) if g else {}}}


# --------------------------------------------------------------- P2：继续观看 / 追剧 / 源健康 / 收藏元数据
@admin_router.get("/v/api/ext/continue")
async def ext_continue(request: Request, limit: int = 20):
    """继续观看：未看完的记录，按最近观看倒序，带 progress。

    P0-3 按库过滤：先取全量（history 按账号分区，每账号上限 500 条）
    再按归属库过滤、最后截到 limit——权限收回后旧记录不再展示；
    若直接在 limit 内过滤，“前 limit 条全被隐藏时”可见的旧记录就
    永远翻不出来。无归属 brief（缓存已淘汰）的记录不再展示，与
    play/list 的口径一致。
    """
    from app import _brief_viewable, _chart_extra_keys, _viewer_ctx, app, chart_cache, history, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    out = []
    for rec in history.continue_watching(limit=500):
        ig = rec.get("item_guid", "")
        brief = store.get_title_by_guid(ig)
        if brief is None:
            ep = store.get_episode(ig) or {}
            brief = store.get_title_by_guid(ep.get("online_guid", "")) or None
        if brief is None:
            continue
        extra = _chart_extra_keys(ig) if chart_cache.is_chart_guid(ig) \
            else None
        if not _brief_viewable(request, brief, extra_keys=extra, _ctx=_viewer_ctx(request)):
            continue
        out.append(rec)
    return {"code": 0, "msg": "", "data": out[:max(1, int(limit))]}


@admin_router.get("/v/api/ext/follow")
async def ext_follow_list(request: Request):
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if not config.get().get("follow_enabled", True):
        return {"code": 0, "msg": "", "data": []}
    return {"code": 0, "msg": "", "data": follow_list.list()}


@admin_router.post("/v/api/ext/follow")
async def ext_follow_set(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    guid, title = (body.get("guid") or ""), (body.get("title") or "")
    if body.get("action") == "unfollow":
        return {"code": 0, "msg": "", "data": follow_list.unfollow(guid)}
    if not guid:
        return {"code": 1, "msg": "guid required", "data": False}
    return {"code": 0, "msg": "", "data": follow_list.follow(
        guid, title, int(body.get("episodes", 0) or 0))}


@admin_router.post("/v/api/ext/follow/check")
async def ext_follow_check(request: Request):
    """追剧提醒：body {counts: {guid: 当前总集数}}，返回有更新的条目。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    counts = body.get("counts") or {}
    return {"code": 0, "msg": "", "data": follow_list.check_updates(
        {str(k): int(v) for k, v in counts.items()})}


@admin_router.get("/v/api/ext/source-health")
async def ext_source_health(request: Request):
    """源健康快照：成功率 / 延迟 / 连续失败 / 死亡冷却，供管理页标红降级源。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return {"code": 0, "msg": "", "data": sourcehealth.snapshot()}


@admin_router.post("/v/api/ext/source-health/reset")
async def ext_source_health_reset(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    sourcehealth.reset(body.get("key") or None)
    return {"code": 0, "msg": "", "data": True}


@admin_router.post("/v/api/ext/source/heal")
async def ext_source_heal(request: Request):
    """域名自愈：对一个源依次试备用域名，返回第一个返回正常内容的 API 地址。"""
    from app import _domain_heal_enabled, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    sid = body.get("id") or ""
    src = next((s for s in config.all_sources() if s.get("id") == sid), None)
    if not src:
        return {"code": 1, "msg": "source not found", "data": None}
    if not _domain_heal_enabled():
        # 域名自愈开关关闭：不试备用域名、不换域名，直接返回空结果。
        return {"code": 0, "msg": "domain heal disabled", "data": None}
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


@admin_router.post("/v/api/ext/favorite/enrich")
async def ext_favorite_enrich(request: Request):
    """收藏元数据补全：用同名其他源的条目补齐收藏记录的空字段，不改已有值。"""
    # P0-3：与 favorite/list 同口径——先按账号分区 + 按库过滤再补全，
    # 否则未过滤的原始记录会经此端点漏回给无权账号。
    from app import _favorite_viewable, _viewer_ctx, app, config, marks, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    recs = [r for r in marks.favorite_records()
            if _favorite_viewable(request, r, _ctx=_viewer_ctx(request))]
    if not config.get().get("metadata_enabled", True):
        return {"code": 0, "msg": "", "data": recs}
    # 候选：当前已缓存的全部标题 brief。
    cands = []
    try:
        cands = [dict(b) for b in store._titles.values()] if hasattr(store, "_titles") else []
    except Exception:
        _log.debug("ext_favorite_enrich failed", exc_info=True)
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
@admin_router.get("/v/api/ext/update/check")
async def ext_update_check(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    force = request.query_params.get("force") in ("1", "true", "yes")
    return await updater.check(force=force)


@admin_router.get("/v/api/ext/update/status")
async def ext_update_status(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return updater.status()


@admin_router.post("/v/api/ext/update/now")
async def ext_update_now(request: Request):
    # 仅管理台显式触发；下载校验后交给 fnOS 应用中心正规升级（会短暂重启服务）。
    # 该动作具备系统级副作用，要求显式确认 + 自定义头（跨站简单请求无法携带，
    # 起到 CSRF 防护作用）；只读的检查/状态接口不加此限制。
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except Exception:
        _log.debug("ext_update_now failed", exc_info=True)
        body = {}
    if request.headers.get("x-fnmedia-admin") != "1" or body.get("confirm") is not True:
        return JSONResponse({"code": 403,
                             "msg": "需要在管理页显式确认后才能执行更新"},
                            status_code=403)
    return await updater.start_update()


@admin_router.post("/v/api/ext/source/priority")
async def ext_source_priority(request: Request):
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json()
    mapping = body.get("priority") if isinstance(body.get("priority"), dict) else body
    ok, msg = config.set_source_priority(mapping if isinstance(mapping, dict) else {})
    if "prefer_quality_source" in body:
        config.save({"prefer_quality_source": bool(body["prefer_quality_source"])})
    return {"code": 0 if ok else 400, "msg": msg,
            "data": {"source_priority": config.source_priorities()}}


def _virtual_lib_list() -> list[dict]:
    """供管理台展示的虚拟媒体库清单（全部注入库），含显隐与排序。"""
    from app import CATALOG_LIB_GUIDS, CHART_LIB_GUIDS, GUARD_NATIVE_GUID, GUARD_NATIVE_TITLE, _gn_hot_rows, catalog_enabled, chart_cache, charts_enabled, config, guard_lib_guid, guard_lib_name, guard_libs_active, guard_native_enabled, hgdj_enabled, standalone_lib_guid, standalone_lib_name, standalone_sources_active, t4_browser  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        from app import _xlib_active_cats  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        for cat in _xlib_active_cats():
            out.append({"guid": xlib.lib_guid(cat["id"]),
                        "key": vlibsmod.make_key(vlibsmod.K_XLIB, cat["id"]),
                        "title": cat["title"], "kind": "xlib",
                        "group": "xlib", "enabled": True, "count": 0})
    except Exception:
        _log.debug("_virtual_lib_list failed", exc_info=True)
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


@admin_router.get("/v/api/ext/libs")
async def ext_libs(request: Request):
    from app import app, catalog_enabled, charts_enabled  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return {"code": 0, "msg": "", "data": {
        "hot_enabled": charts_enabled(),
        "catalog_enabled": catalog_enabled(),
        "libs": _virtual_lib_list(),
        "placement": vlibsmod.lib_placement_default(),
        "anchor": vlibsmod.lib_anchor(),
        "order": vlibsmod.lib_order()}}


@admin_router.post("/v/api/ext/lib/pin")
async def ext_lib_pin(request: Request):
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json()
    guid = str(body.get("guid") or "")
    on = bool(body.get("pinned"))
    ok, msg = config.set_lib_pinned(guid, on)
    return {"code": 0 if ok else 400, "msg": msg,
            "data": {"pinned_libs": sorted(config.pinned_lib_guids())}}


@admin_router.get("/v/api/ext/accounts")
async def ext_accounts(request: Request):
    """子账号列表（从飞牛官方数据库只读读取）+ 每个账号的允许库列表。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/account/acl")
async def ext_account_acl(request: Request):
    """设置某账号允许看到的虚拟库（allow: [库键, ...]，空列表=不允许看任何
    虚拟库；不传/传 null=取消限制，改回默认：fail-open 下默认可见）。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ok, msg = vlibsmod.set_account_acl(body.get("guid"), body.get("allow", None))
    return {"code": 0 if ok else 1, "msg": msg}


@admin_router.post("/v/api/ext/account/defaults")
async def ext_account_defaults(request: Request):
    """设置未配置账号的默认可见性（acl_default_visible / acl_default_online）。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    cfg = config.get()
    if "acl_default_visible" in body:
        cfg["acl_default_visible"] = bool(body["acl_default_visible"])
    if "acl_default_online" in body:
        cfg["acl_default_online"] = bool(body["acl_default_online"])
    config.save(cfg)
    return {"code": 0, "msg": "ok"}


@admin_router.get("/v/api/ext/local-libs")
async def ext_local_libs(request: Request):
    """返回官方本地库的 guid/name 列表（供“after”锚点选择）。"""
    from app import _all_virtual_lib_guids, app, up_client  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        _log.debug("ext_local_libs failed", exc_info=True)
        pass
    return {"code": 0, "msg": "", "data": {"libs": libs}}


@admin_router.post("/v/api/ext/lib/visibility")
async def ext_lib_visibility(request: Request):
    """设置某个虚拟媒体库的全局显隐（管理员级，不分账号）。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ok, msg = vlibsmod.set_hidden(body.get("key"), bool(body.get("hidden", False)))
    return {"code": 0 if ok else 1, "msg": msg}


@admin_router.post("/v/api/ext/lib/order")
async def ext_lib_order(request: Request):
    """保存虚拟媒体库自定义顺序（order: [库键, ...]）和相对本地库位置
    （placement: front/back/after；after 时可带 anchor 本地库 guid）。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ok, msg = vlibsmod.set_order(body.get("order"), body.get("placement"),
                                 body.get("anchor"))
    return {"code": 0 if ok else 1, "msg": msg}


@admin_router.post("/v/api/ext/hot/refresh")
async def ext_hot_refresh(request: Request):
    from app import app, chart_cache, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    await chart_cache.refresh(force=True)
    try:
        if catalog.routes_stale():
            await asyncio.wait_for(
                catalog.refresh_routes(config.enabled_sources()), timeout=90)
    except Exception:
        _log.debug("ext_hot_refresh failed", exc_info=True)
        pass
    return {"code": 0, "msg": "", "data": chart_cache.status()}


@admin_router.get("/v/api/ext/online")
async def ext_online(q: str = "", limit: int = 200):
    from app import app, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/config")
async def ext_set_config(request: Request):
    from app import HOT_DEFAULT_TITLE, _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        "linepredict_zero_race",  # 可信预测零竞速直出开关（默认开）
        "main_ready_budget",  # 慢线起播预算（秒），实时读取，改后即时生效
        "prewarm_mode",
        "pan_cache_cleanup_enabled", "pan_cache_ttl_hours",
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
    if "main_ready_budget" in patch:
        # 实时读取 + 夹取 [0.8, 2.8]：非法值回落 2.4（见 config.clamp_main_ready_budget）
        patch["main_ready_budget"] = cfgmod.clamp_main_ready_budget(
            patch["main_ready_budget"])
    if "update_proxy" in patch:
        pu = str(patch["update_proxy"]).strip()[:300]
        patch["update_proxy"] = pu if re.match(r"^(https?|socks5h?)://", pu) else ""
    if "pan_cache_ttl_hours" in patch:
        try:
            # 允许 1 小时 ~ 30 天（与上游钳制一致）
            patch["pan_cache_ttl_hours"] = max(
                1, min(720, int(float(patch["pan_cache_ttl_hours"]))))
        except (TypeError, ValueError):
            patch.pop("pan_cache_ttl_hours", None)
    if "pan_cache_cleanup_enabled" in patch:
        patch["pan_cache_cleanup_enabled"] = bool(
            patch["pan_cache_cleanup_enabled"])
    if "linepredict_zero_race" in patch:
        patch["linepredict_zero_race"] = bool(patch["linepredict_zero_race"])
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
            _log.debug("ext_set_config failed", exc_info=True)
            pass
    if sources_changed or "welfare_library_enabled" in patch:
        # 扩展库独立开关切换 → 立即重建分类路由（开=补建 ADULT 池，关=清掉），
        # 否则打开开关后扩展库卡要等下次路由过期才出现。
        _schedule_routes_refresh()
    return {"code": 0, "msg": "saved"}


@admin_router.post("/v/api/ext/subscription-proxy")
async def ext_set_subscription_proxy(request: Request):
    """设置/清空「仅拉取订阅资源」的代理（订阅配置/spider jar/DRPY脚本）。

    播放、搜索、CMS 探测不走此代理。body: {"proxy":"http://user:pass@host:port"或""}
    会即时保存并用该代理探测一次（拉一个轻量 URL），返回连通结果。
    """
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("ext_set_subscription_proxy failed", exc_info=True)
            test_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
    return {"code": 0, "msg": "",
            "data": {"proxy": subfetch.subscription_proxy(),
                     "test_ok": (True if not px else test_ok),
                     "test_msg": ("直连（未设置代理）" if not px else test_msg)}}


@admin_router.post("/v/api/ext/junk-keywords")
async def ext_junk_keywords(request: Request):
    from app import _AGG_CACHE, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json()
    if body.get("reset"):
        kws = config.reset_junk_keywords()
    else:
        kws = config.set_junk_keywords(body.get("keywords", []))
    _search_cache.clear(); _AGG_CACHE.clear()
    return {"code": 0, "msg": "saved", "data": {"junk_keywords": kws}}


@admin_router.post("/v/api/ext/source-proxy")
async def ext_set_source_proxy(request: Request):
    """设置/清空「访问资源站取源」专用代理（搜索/分类/测速/解析；视频流仍直连）。

    issue #30：部分运营商直连资源站超时/被墙。保存后即时用该代理对一个内置源做
    一次轻量测速，返回连通结果（失败也保存，由用户自行决定）。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("ext_set_source_proxy failed", exc_info=True)
            test_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
    return {"code": 0, "msg": "",
            "data": {"proxy": netutil.source_proxy(),
                     "mode": mode,
                     "test_ok": (True if not px else test_ok),
                     "test_msg": ("直连（未设置代理）" if not px else test_msg)}}


def _schedule_routes_refresh():
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    async def _run_after_current():
        # 刷新进行中时不能丢事件：连续“关→开”切换若把“开”丢掉，
        # 关状态的重建会先落地且再无重建，扩展库卡永远回不来。
        # 等当前这轮结束再按最新配置补跑一次（refresh_routes 自身有重入保护）。
        from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        waited = 0
        while catalog._refreshing and waited < 180:
            await asyncio.sleep(2)
            waited += 2
        if not catalog._refreshing:
            await catalog.refresh_routes(config.enabled_sources())

    try:
        asyncio.create_task(_run_after_current())
    except Exception:
        _log.debug("_schedule_routes_refresh failed", exc_info=True)
        pass


@admin_router.post("/v/api/ext/source/add")
async def ext_source_add(request: Request):
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/source/standalone")
async def ext_source_standalone(request: Request):
    from app import _schedule_routes_refresh, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/source/loose-filter")
async def ext_source_loose(request: Request):
    from app import _schedule_routes_refresh, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        _log.debug("ext_source_loose failed", exc_info=True)
        pass
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok"}


# >>> PRIVATE:ADULT
@admin_router.post("/v/api/ext/source/adult")
async def ext_source_adult(request: Request):
    from app import _schedule_routes_refresh, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/source/delete")
async def ext_source_delete(request: Request):
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    body = await request.json()
    ok, info = config.remove_source(body.get("id", ""))
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _search_cache.clear(); _AGG_CACHE.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "deleted"}


@admin_router.post("/v/api/ext/source/restore")
async def ext_source_restore(request: Request):
    from app import _AGG_CACHE, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    config.restore_builtin()
    _search_cache.clear(); _AGG_CACHE.clear()
    return {"code": 0, "msg": "restored"}


@admin_router.post("/v/api/ext/source/test")
async def ext_source_test(request: Request):
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/source/test-all")
async def ext_source_test_all(request: Request):
    """并发探测全部源，返回 [{id,name,...result}]。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
@admin_router.post("/v/api/ext/source/health-check")
async def ext_source_health_check(request: Request):
    """用常见热门关键词批量检测纯 CMS 资源站有效性。

    任一热门词搜到结果即有效；全部搜不到才判失效，并把失效源写入源健康画像
    （record False 累积触发熔断），起播/竞速自动跳过。仅检测纯苹果 CMS 源，
    加固/爬虫/小雅等专有源不走此检测。"""
    from app import _source_health_enabled, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except Exception:
        _log.debug("ext_source_health_check failed", exc_info=True)
        body = {}
    # 限制并发，避免一次性打爆源站
    try:
        concurrency = max(1, min(8, int(body.get("concurrency") or 4)))
    except (TypeError, ValueError):
        concurrency = 4
    targets = [s for s in config.all_sources() if _is_pure_cms(s)]
    sem = asyncio.Semaphore(concurrency)

    async def one(s):
        from app import _source_health_enabled  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        async with sem:
            try:
                r = await providers.probe_source_alive(s)
            except Exception as exc:  # noqa: BLE001
                _log.debug("one failed", exc_info=True)
                r = {"alive": False, "ok": False, "status": 0,
                     "elapsed_ms": 0, "count": 0, "sample": "",
                     "error": type(exc).__name__, "hit": "", "tried": []}
            sk = sourcehealth.source_key_of(s.get("id"), s) or s.get("id", "")
            # 回写源健康：失效累计失败（达阈值熔断），有效则清零失败计数。
            # 源健康降级开关关闭时不记录（画像冻结，搜索不再按它降级）。
            if sk and _source_health_enabled():
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
@admin_router.get("/v/api/ext/source/export")
async def ext_source_export():
    """导出纯 CMS 源为可移植 JSON（只含名称/接口/启用/独立库/非过滤等通用字段，
    不含 id/内置标记，便于跨设备导入；jar/加固/小雅等不属本页范围）。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/source/import-file")
async def ext_source_import_file(request: Request):
    """从导出的 JSON（或含 sources 数组的兼容结构）批量导入纯 CMS 源。
    自动去重（按规范化 API），返回新增/跳过计数；jar/爬虫/加固条目忽略并计数。"""
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
                    _log.debug("ext_source_import_file failed", exc_info=True)
                    pass
        else:
            skipped += 1
    _search_cache.clear(); _AGG_CACHE.clear()
    if added:
        _schedule_routes_refresh()
    return {"code": 0, "msg": "", "data": {"added": added, "skipped": skipped,
                                            "ignored_noncms": noncms}}


@admin_router.post("/v/api/ext/source/update")
async def ext_source_update(request: Request):
    """编辑资源站名称 / 订阅(API)链接。自定义源可改链接（id 随地址哈希重建），
    内置源仅允许改名。改后清搜索缓存并刷新海量库路由。"""
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        _log.debug("ext_source_update failed", exc_info=True)
        return JSONResponse({"code": 500, "msg": str(exc)}, status_code=500)
    if not ok:
        return JSONResponse({"code": 400, "msg": info}, status_code=400)
    _search_cache.clear(); _AGG_CACHE.clear()
    _schedule_routes_refresh()
    return {"code": 0, "msg": "ok", "data": {"id": info}}


@admin_router.post("/v/api/ext/source/import-tvbox")
async def ext_import_tvbox(request: Request):
    """导入 TVBox 配置中的 CMS 与远程 T4 源。

    入参：{url?} 或 {text?}，外加 standalone（可选）。对候选源做轻量探测，
    CMS 探测通过后导入；T4 只登记来源，不预运行插件。
    """
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        _log.debug("ext_import_tvbox failed", exc_info=True)
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
                _log.debug("check failed", exc_info=True)
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


@admin_router.post("/v/api/ext/source/import-subscription")
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
    from app import _AGG_CACHE, _schedule_routes_refresh, _search_cache, app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        _log.debug("ext_import_subscription failed", exc_info=True)
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
                _log.debug("_probe failed", exc_info=True)
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
            _log.debug("ext_import_subscription failed", exc_info=True)
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
            _log.debug("ext_import_subscription failed", exc_info=True)
            runtime = {"feature": True, "ready": False,
                       "reason": str(exc)[:120]}
        try:
            config.load()
        except Exception:
            _log.debug("ext_import_subscription failed", exc_info=True)
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
            _log.debug("ext_import_subscription failed", exc_info=True)
            csp_report = {"error": f"普通爬虫写入失败：{type(exc).__name__}"}
        try:
            config.load()
        except Exception:
            _log.debug("ext_import_subscription failed", exc_info=True)
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
            _log.debug("ext_import_subscription failed", exc_info=True)
            drpy_report = {"error": f"远程DRPY导入失败：{type(exc).__name__}"}
        try:
            config.load()
        except Exception:
            _log.debug("ext_import_subscription failed", exc_info=True)
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


@admin_router.get("/v/api/ext/adfilter/status")
async def ext_adfilter_status():
    """m3u8 去广告状态：总开关 + 动态检测 + 已合并的订阅规则组数/域名数。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/adfilter/toggle")
async def ext_adfilter_toggle(request: Request):
    """开关 m3u8 去广告。body: {"enabled":bool} / {"dynamic":bool} / {"sps":bool}"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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


@admin_router.post("/v/api/ext/adfilter/rules")
async def ext_adfilter_replace_rules(request: Request):
    """整体替换去广告规则（高级）。body: {"rules":[{name,hosts[],regex[]}]}"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    import adfilter as _adf
    body = await request.json()
    n = _adf.replace_rules(body.get("rules") or [])
    return {"code": 0, "msg": "", "data": {"groups": n}}


@admin_router.get("/v/online-admin")
async def admin_page():
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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

    @admin_router.get("/v/api/ext/catvod/credentials")
    async def catvod_credentials_get():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": _catvod.private_status()})

    @admin_router.post("/v/api/ext/catvod/credentials")
    async def catvod_credentials_save(request: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        body = await request.json()
        try:
            st = await asyncio.to_thread(
                _catvod.save_credentials,
                body.get("server"), body.get("username"), body.get("password"))
            return JSONResponse({"code": 0, "msg": "已保存并生效", "data": st})
        except Exception as exc:  # noqa: BLE001
            _log.debug("catvod_credentials_save failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200],
                                 "data": None}, status_code=500)

    @admin_router.get("/v/api/ext/catvod/home")
    async def catvod_home():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("catvod_home failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/catvod/category")
    async def catvod_category(tid: str, pg: int = 1, f: str = ""):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("catvod_category failed: tid=%s", tid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/catvod/detail")
    async def catvod_detail(vid: str):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("catvod_detail failed: vid=%s", vid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/catvod/search")
    async def catvod_search(q: str):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        g = _catvod_guard()
        if g:
            return g
        try:
            tok = await _catvod.xiaoya_token()
            lst = await asyncio.to_thread(_catvod.search, q, tok)
            return JSONResponse({"code": 0, "msg": "", "data": {"list": lst}})
        except Exception as exc:  # noqa: BLE001
            _log.debug("catvod_search failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/catvod/play")
    async def catvod_play(vid: str, flag: str = "", resolve: int = 1):
        """返回小雅签名播放地址。resolve=1（默认）时服务端跟随一次 302，
        给出网盘 CDN 最终地址；否则给小雅 /d 链（由客户端自行跟随）。"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
                    _log.debug("catvod_play failed: vid=%s", vid, exc_info=True)
                    pass
            return JSONResponse({"code": 0, "msg": "",
                                 "data": {"url": url, "headers": headers}})
        except Exception as exc:  # noqa: BLE001
            _log.debug("catvod_play failed: vid=%s", vid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/ext/catvod/redirect")
    async def catvod_redirect(vid: str, flag: str = ""):
        """播放器直接用：302 到小雅签名 /d 链（携带所需 UA）。"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("catvod_redirect failed: vid=%s", vid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]}, status_code=500)

    @admin_router.get("/v/ext/xlib/play")
    async def xlib_play(g: str, i: int = 0):
        """小雅分类库播放：用 AList /api/fs/get 取最终公网 CDN 直链，302 客户端直连。
        NAS 到该 CDN 不通，故服务端不代流，只做签名与跳转。"""
        from app import _xlib_on, app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("xlib_play failed", exc_info=True)
            return JSONResponse({"code": 502, "msg": str(exc)[:200]}, status_code=502)
        return RedirectResponse(url, status_code=302,
                                headers={"Cache-Control": "no-store"})
# <<< PRIVATE:CATVOD

# >>> PRIVATE:DRPY
# ---------------- drpyS JS 爬虫（Node sidecar，仅完整构建） ----------------
if features.DRPY:
    import drpy as _drpy

    def _drpy_src(sid: str):
        from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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

    @admin_router.get("/v/api/ext/drpy/status")
    async def drpy_status():
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        sites = [{"id": s.get("id"), "name": s.get("name"),
                  "enabled": bool(s.get("enabled"))}
                 for s in config.all_sources() if s.get("drpy")]
        return JSONResponse({"code": 0, "msg": "", "data": {
            "available": _drpy.available(), "sites": sites}})

    @admin_router.get("/v/api/ext/drpy/home")
    async def drpy_home(sid: str):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("drpy_home failed: sid=%s", sid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/drpy/category")
    async def drpy_category(sid: str, tid: str, pg: int = 1):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("drpy_category failed: sid=%s tid=%s", sid, tid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/drpy/detail")
    async def drpy_detail(sid: str, vid: str):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("drpy_detail failed: sid=%s vid=%s", sid, vid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/drpy/play")
    async def drpy_play(sid: str, vid: str, flag: str = ""):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("drpy_play failed: sid=%s vid=%s", sid, vid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200], "data": None})

    @admin_router.get("/v/api/ext/drpy/remote")
    async def drpy_remote_list():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"available": _drpy.available(),
                                      "sites": await asyncio.to_thread(_drpy.remote_status)}})

    @admin_router.post("/v/api/ext/drpy/remote-toggle")
    async def drpy_remote_toggle(request: Request):
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        try:
            body = await request.json()
        except Exception:
            _log.debug("drpy_remote_toggle failed", exc_info=True)
            body = {}
        key, on = str(body.get("key", "")), bool(body.get("enabled"))
        ok = await asyncio.to_thread(_drpy.set_remote_enabled, key, on)
        if not ok:
            return JSONResponse(
                {"code": 409, "msg": "规则脚本未就绪，无法启用（可先重试下载）"},
                status_code=409)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key, "enabled": on}})

    @admin_router.post("/v/api/ext/drpy/remote-remove")
    async def drpy_remote_remove(request: Request):
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        try:
            body = await request.json()
        except Exception:
            _log.debug("drpy_remote_remove failed", exc_info=True)
            body = {}
        key = str(body.get("key", ""))
        if not key or not await asyncio.to_thread(_drpy.remove_remote, key):
            return JSONResponse({"code": 404, "msg": "未知远程 JS 源"}, status_code=404)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key}})

    @admin_router.post("/v/api/ext/drpy/remote-retry")
    async def drpy_remote_retry(request: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        try:
            body = await request.json()
        except Exception:
            _log.debug("drpy_remote_retry failed", exc_info=True)
            body = {}
        key = str(body.get("key", ""))
        res = await asyncio.to_thread(_drpy.retry_remote_script, key)
        if not res.get("ok"):
            return JSONResponse({"code": 502, "msg": res.get("error", "下载失败")},
                                status_code=502)
        return JSONResponse({"code": 0, "msg": "", "data": res})
# <<< PRIVATE:DRPY


# ---------------- 在线源健康画像（成功率/延迟，自动降权，通用能力） ----------------
@admin_router.get("/v/api/ext/source-health")
async def source_health():
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    snap = sourcehealth.snapshot()
    # 按健康等级、成功率、延迟排序输出，便于管理页展示
    rows = sorted(
        snap.values(),
        key=lambda s: (s.get("grade", 1),
                       -s.get("success_rate", 0),
                       s.get("avg_latency", 99)))
    return JSONResponse({"code": 0, "data": {"sources": rows}})


@admin_router.post("/v/api/ext/source-health/reset")
async def source_health_reset(request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        body = await request.json()
    except Exception:
        _log.debug("source_health_reset failed", exc_info=True)
        body = {}
    sourcehealth.reset((body or {}).get("key"))
    return JSONResponse({"code": 0})


# ---------------- 本片线路预测（linepredict）：海报预热→本片最快线→秒播 ----------------
# 上游 v0.2.68/69 移植。reset 只清预测缓存，不动竞速最快线记忆；
# 状态里的 chart_bindings 为 chartbind 持久化绑定计数。
@admin_router.get("/v/api/ext/linepredict")
async def linepredict_status():
    """返回当前已预测的本片最快线数与在途预热数（调试/管理页用）。"""
    from app import app, config, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            "zero_race": bool(config.get().get("linepredict_zero_race", True)),
            "prewarm_mode": str(config.get().get("prewarm_mode", "realtime")),
            "chart_bindings": chartbind.bind_store.status()}}})


@admin_router.post("/v/api/ext/linepredict/config")
async def linepredict_config(request: Request):
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
    if "zero_race" in body:
        # 零竞速直出开关：关时恢复旧行为（预测线只作竞速候选排序种子）。
        patch["linepredict_zero_race"] = bool(body["zero_race"])
    if patch:
        config.save(patch)
    return JSONResponse({"code": 0, "data": patch})


@admin_router.post("/v/api/ext/linepredict/reset")
async def linepredict_reset():
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    linepredict.reset()
    return JSONResponse({"code": 0})


# >>> PRIVATE:GUARD
# ---------------- DexGuard 加固爬虫（Android runner，仅完整构建） ----------------
if features.GUARD:
    import guard as _guard

    @admin_router.get("/v/api/ext/guard/status")
    async def guard_status():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        st = await asyncio.to_thread(_guard.status)
        return JSONResponse({"code": 0, "msg": "", "data": st})

    async def _guard_body(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:
            _log.debug("_guard_body failed", exc_info=True)
            return {}

    @admin_router.post("/v/api/ext/guard/import")
    async def guard_import(request: Request):
        """粘贴 TVBox 配置（JSON 文本或 URL）批量导入 csp_*Guard 加固源。

        body: {"config": "<tvbox json 文本或 http(s) 地址>", "enable": true|false|null}
        jar 用设备上已部署的内置兼容加固 jar，无需下载配置里的 spider jar。
        """
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
                _log.debug("guard_import failed", exc_info=True)
                return JSONResponse({"code": 502, "msg": f"配置下载失败: {exc}"[:200]})
        enable = body.get("enable", None)
        try:
            res = await asyncio.to_thread(_guard.import_tvbox, text, enable)
        except Exception as exc:  # noqa: BLE001
            _log.debug("guard_import failed", exc_info=True)
            return JSONResponse({"code": 400, "msg": f"解析失败: {exc}"[:200]})
        # 让新源立即进入配置底表
        try:
            config.load()
        except Exception:
            _log.debug("guard_import failed", exc_info=True)
            pass
        return JSONResponse({"code": 0, "msg": "", "data": res})

    @admin_router.post("/v/api/ext/guard/toggle")
    async def guard_toggle(request: Request):
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        body = await _guard_body(request)
        key, on = str(body.get("key", "")), bool(body.get("enabled"))
        if not _guard.site(key):
            return JSONResponse({"code": 404, "msg": "未知加固源"}, status_code=404)
        _guard.set_enabled(key, on)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key, "enabled": on}})

    @admin_router.post("/v/api/ext/guard/remove")
    async def guard_remove(request: Request):
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        body = await _guard_body(request)
        key = str(body.get("key", ""))
        if not key:
            return JSONResponse({"code": 400, "msg": "缺少 key"}, status_code=400)
        removed = await asyncio.to_thread(_guard.remove_imported, key)
        if not removed:
            return JSONResponse({"code": 400, "msg": "内置源不可删除（可停用）"}, status_code=400)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key}})

    @admin_router.get("/v/api/ext/guard/jar")
    async def guard_jar_status():
        """用户自备加固 jar 的本地/设备状态（不回传文件内容）。"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.user_jar_status)})

    @admin_router.post("/v/api/ext/guard/jar")
    async def guard_jar_upload(request: Request):
        """上传用户自备的加固 jar（base64 JSON，避免引入 multipart 依赖）。

        body: {"name": "xxx.jar", "data_b64": "<base64>"}
        保存到私有目录并立即下发到正在运行的 redroid；未运行则下次启动自动下发。
        """
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("guard_jar_upload failed", exc_info=True)
            return JSONResponse({"code": 400, "msg": f"base64 解码失败: {exc}"}, status_code=400)
        res = await asyncio.to_thread(_guard.save_user_jar, raw, str(body.get("name") or ""))
        if not res.get("ok"):
            return JSONResponse({"code": 400, "msg": res.get("error", "保存失败")}, status_code=400)
        return JSONResponse({"code": 0, "msg": "", "data": res})

    # ---------------- 普通 csp_ 爬虫（订阅导入，按各自 jar） ----------------
    import spiders as _spider

    @admin_router.get("/v/api/ext/spider/status")
    async def spider_status():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_spider.status)})

    @admin_router.post("/v/api/ext/spider/toggle")
    async def spider_toggle(request: Request):
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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

    @admin_router.post("/v/api/ext/spider/remove")
    async def spider_remove(request: Request):
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        body = await _guard_body(request)
        key = str(body.get("key", ""))
        if not key or not await asyncio.to_thread(_spider.remove, key):
            return JSONResponse({"code": 404, "msg": "未知爬虫源"}, status_code=404)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": {"key": key}})

    @admin_router.post("/v/api/ext/spider/jar")
    async def spider_jar_upload(request: Request):
        """为某个订阅 jar URL 手动补传爬虫 jar（源站下载失败时）。

        body: {"jar_url": "...", "name":"x.jar", "data_b64":"<base64>"}
        保存后联动启用引用该 jar 的非风险源，并立即尝试下发到 redroid。
        """
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("spider_jar_upload failed", exc_info=True)
            return JSONResponse({"code": 400, "msg": f"base64 解码失败: {exc}"},
                                status_code=400)
        res = await asyncio.to_thread(_spider.upload_jar, jar_url, raw)
        if not res.get("ok"):
            return JSONResponse({"code": 400, "msg": res.get("error", "保存失败")},
                                status_code=400)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": res})

    @admin_router.post("/v/api/ext/spider/jar-retry")
    async def spider_jar_retry(request: Request):
        """重新尝试自动下载某个 jar（源站恢复后），成功则联动启用非风险源。"""
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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

    @admin_router.post("/v/api/ext/spider/probe")
    async def spider_probe(request: Request):
        """单源体检：搜索→详情→(可选)取首集播放地址。body:{key,keyword,do_play}"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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

    @admin_router.post("/v/api/ext/spider/scan")
    async def spider_scan_start(request: Request):
        """启动全源批量体检（后台并发）。body:{keyword,do_play,only_enabled}"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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

    @admin_router.get("/v/api/ext/spider/scan")
    async def spider_scan_status():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_spider.scan_status)})

    @admin_router.post("/v/api/ext/spider/remove-many")
    async def spider_remove_many(request: Request):
        """批量删除源。body:{keys:[...]}"""
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        body = await _guard_body(request)
        keys = body.get("keys") or []
        if not isinstance(keys, list):
            return JSONResponse({"code": 400, "msg": "keys 必须是数组"}, status_code=400)
        res = await asyncio.to_thread(_spider.remove_many, keys)
        config.load()
        return JSONResponse({"code": 0, "msg": "", "data": res})

    # ---- 专题资源栏：浏览单个加固源的首页分类 / 分类影片 ----
    @admin_router.get("/v/api/ext/guard/gnstate")
    async def guard_gnstate():
        # 临时诊断：进程内加固库元数据/热墙实况
        from app import _gn_classes_meta, _gn_is_film_class, _gn_lock, _gn_sources, _gn_state, app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("guard_gnstate failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @admin_router.get("/v/api/ext/guard/home")
    async def guard_home(sid: str):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("guard_home failed: sid=%s", sid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @admin_router.get("/v/api/ext/guard/category")
    async def guard_category(sid: str, tid: str, pg: int = 1):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        conf = _guard.site(sid)
        if not conf:
            return JSONResponse({"code": 404, "msg": "未知加固源"}, status_code=404)
        try:
            res = await asyncio.to_thread(_guard.category, conf, tid, pg)
            return JSONResponse({"code": 0, "msg": "", "data": res})
        except Exception as exc:  # noqa: BLE001
            _log.debug("guard_category failed: sid=%s tid=%s", sid, tid, exc_info=True)
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
        from app import _GuardPanUnsupported  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        m = _PAN_MODS.get(key or "quark")
        if m is None:
            raise _GuardPanUnsupported("当前构建不含该网盘模块")
        return m

    @admin_router.get("/v/api/ext/pan/status")
    async def pan_status():
        from app import _PAN_PROVIDERS, app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        providers = [{"key": p["key"], "name": p["name"],
                      "playable": p["playable"]} for p in _PAN_PROVIDERS]
        logins = {}
        for key, mod in _PAN_MODS.items():
            if mod is not None:
                try:
                    logins[key] = mod.account_info_safe()
                except Exception:  # noqa: BLE001
                    _log.debug("pan_status failed", exc_info=True)
                    pass
        supported = any(m is not None for m in _PAN_MODS.values())
        qk = logins.get("quark", {})
        return JSONResponse({"code": 0, "data": {
            "supported": supported, "providers": providers,
            "logins": logins, **qk}})

    @admin_router.post("/v/api/ext/pan/detect")
    async def pan_detect(body: dict | None = None):
        """识别一段文本（播放地址/线路名）里的网盘资源。供前端角标与提示使用。"""
        from app import _PAN_LINKSTART_RE, _pan_match, app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
                _log.debug("_pan_qr_start failed", exc_info=True)
                pass
            return JSONResponse({"code": 0, "data": s})
        except Exception as exc:  # noqa: BLE001
            _log.debug("_pan_qr_start failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @admin_router.post("/v/api/ext/pan/qr/start")
    async def pan_qr_start():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_qr_start("quark")

    @admin_router.post("/v/api/ext/pan/uc/qr/start")
    async def pan_uc_qr_start():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_qr_start("uc")

    @admin_router.post("/v/api/ext/pan/ali/qr/start")
    async def pan_ali_qr_start():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_qr_start("ali")

    @admin_router.post("/v/api/ext/pan/ali/cred")
    async def pan_ali_cred(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        body = await req.json()
        try:
            _panali.set_credentials(body.get("client_id", ""),
                                    body.get("client_secret", ""))
            return JSONResponse({"code": 0})
        except Exception as exc:
            _log.debug("pan_ali_cred failed", exc_info=True)
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

    @admin_router.get("/v/api/ext/pan/page")
    async def pan_page():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return HTMLResponse(_PAN_PAGE_HTML)

    async def _pan_poll(key: str, req: Request):
        mod = _pan_mod(key)
        body = await req.json()
        try:
            r = await asyncio.to_thread(mod.qr_poll, body)
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            _log.debug("_pan_poll failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @admin_router.post("/v/api/ext/pan/qr/poll")
    async def pan_qr_poll(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_poll("quark", req)

    @admin_router.post("/v/api/ext/pan/uc/qr/poll")
    async def pan_uc_qr_poll(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_poll("uc", req)

    @admin_router.post("/v/api/ext/pan/ali/qr/poll")
    async def pan_ali_qr_poll(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_poll("ali", req)

    async def _pan_cookie(key: str, req: Request):
        mod = _pan_mod(key)
        body = await req.json()
        try:
            r = await asyncio.to_thread(mod.set_cookie, body.get("cookie", ""))
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            _log.debug("_pan_cookie failed", exc_info=True)
            return JSONResponse({"code": 400, "msg": str(exc)[:200]})

    @admin_router.post("/v/api/ext/pan/cookie")
    async def pan_cookie(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_cookie("quark", req)

    @admin_router.post("/v/api/ext/pan/uc/cookie")
    async def pan_uc_cookie(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_cookie("uc", req)

    @admin_router.post("/v/api/ext/pan/ali/cookie")
    async def pan_ali_cookie(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("_pan_resolve failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @admin_router.post("/v/api/ext/pan/resolve")
    async def pan_resolve(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_resolve("quark", req)

    @admin_router.post("/v/api/ext/pan/uc/resolve")
    async def pan_uc_resolve(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_resolve("uc", req)

    @admin_router.post("/v/api/ext/pan/ali/resolve")
    async def pan_ali_resolve(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_resolve("ali", req)

    async def _pan_list(key: str, req: Request):
        mod = _pan_mod(key)
        fid = (req.query_params.get("fid") or "").strip()
        try:
            r = await asyncio.to_thread(mod.list_dir, fid)
            return JSONResponse({"code": 0, "data": r})
        except Exception as exc:  # noqa: BLE001
            _log.debug("_pan_list failed", exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200]})

    @admin_router.get("/v/api/ext/pan/list")
    async def pan_list(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_list("quark", req)

    @admin_router.get("/v/api/ext/pan/uc/list")
    async def pan_uc_list(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_list("uc", req)

    @admin_router.get("/v/api/ext/pan/ali/list")
    async def pan_ali_list(req: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_list("ali", req)

    async def _pan_logout(key: str):
        mod = _PAN_MODS.get(key)
        if mod is not None:
            await asyncio.to_thread(mod.logout)
        return JSONResponse({"code": 0})

    @admin_router.post("/v/api/ext/pan/logout")
    async def pan_logout():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_logout("quark")

    @admin_router.post("/v/api/ext/pan/uc/logout")
    async def pan_uc_logout():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_logout("uc")

    @admin_router.post("/v/api/ext/pan/ali/logout")
    async def pan_ali_logout():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("_pan_stream failed", exc_info=True)
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
            _log.debug("_pan_stream failed", exc_info=True)
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



    @admin_router.api_route("/v/api/ext/pan/play", methods=["GET", "HEAD"])
    async def pan_play(request: Request):
        """夸克原画 CDN：注入 Cookie 流式代理，透传 Range 支持拖动(206)。"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_stream("quark", request)

    @admin_router.api_route("/v/api/ext/pan/uc/play", methods=["GET", "HEAD"])
    async def pan_uc_play(request: Request):
        """UC 网盘原画 CDN：同上。"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_stream("uc", request)

    @admin_router.api_route("/v/api/ext/pan/ali/play", methods=["GET", "HEAD"])
    async def pan_ali_play(request: Request):
        """阿里云盘：开放平台转码 m3u8 或原画下载直链（直链自带签名，无需鉴权头）。"""
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return await _pan_stream("ali", request)

    # —— 管理页「加固影视浏览器」：源/分类 + 聚合热门 + 分类翻页（海报墙）——
    def _guard_source_dict(key: str) -> dict | None:
        from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
        from app import ONLINE_PREFIX, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
            _log.debug("_browse_card failed: source=%s", source, exc_info=True)
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
        from app import store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        titles, seen = [], set()
        for v in vods[:n]:
            try:
                t = providers._guard_map_item(source, v)
            except Exception:  # noqa: BLE001
                _log.debug("_cards_from_vods failed: source=%s", source, exc_info=True)
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

    @admin_router.get("/v/api/ext/guard/browse")
    async def guard_browse(mode: str = "hot", sid: str = "",
                           tid: str = "", pg: int = 1,
                           limit: int = 60):
        """mode=sites：可浏览加固源及其分类；hot：聚合各源首页推荐；
        category：某源某分类一页。均返回海报卡片（含可直接播放的 m3u8）。"""
        from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
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
                        _log.debug("_src_classes failed", exc_info=True)
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
                    _log.debug("_one failed", exc_info=True)
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
            _log.debug("guard_browse failed: sid=%s tid=%s", sid, tid, exc_info=True)
            return JSONResponse({"code": 500, "msg": str(exc)[:200],
                                 "data": None}, status_code=500)

    # —— 重运行时 sidecar：状态 / 一键安装 / 日志 / 重连网络（管理页）——
    @admin_router.get("/v/api/ext/sidecar/status")
    async def sidecar_status():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.sidecar_status)})

    @admin_router.post("/v/api/ext/sidecar/install")
    async def sidecar_install(request: Request):
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        try:
            body = json.loads(await request.body() or b"{}")
        except Exception:
            _log.debug("sidecar_install failed", exc_info=True)
            body = {}
        what = str(body.get("what") or "all")
        res = await asyncio.to_thread(_guard.start_install, what)
        return JSONResponse({"code": 0 if res.get("ok") else 409,
                             "msg": res.get("error", ""), "data": res})

    @admin_router.get("/v/api/ext/sidecar/log")
    async def sidecar_log():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"running": await asyncio.to_thread(_guard.install_running),
                                      "log": await asyncio.to_thread(_guard.install_log_tail)}})

    @admin_router.post("/v/api/ext/sidecar/reconnect")
    async def sidecar_reconnect():
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.reconnect_network)})

    @admin_router.post("/v/api/ext/sidecar/android/stop")
    async def sidecar_android_stop():
        # 立即停止模拟器，释放约 2GB；停用自启/自愈，避免被重新拉起。
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.stop_android)})

    @admin_router.post("/v/api/ext/sidecar/android/start")
    async def sidecar_android_start():
        # 冷启模拟器并后台接通 bridge（约 1–2 分钟）。
        from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return JSONResponse({"code": 0, "msg": "",
                             "data": await asyncio.to_thread(_guard.start_android)})
# <<< PRIVATE:GUARD
