"""P2 拆分回归：app.py 拆分后路由表、重导出与 TURBO 冻结保持不变。

路由基线在拆分前（v1.1.0，a86ec8b）从 app.app.routes 抓取；新版 Starlette
的 include_router 以 _IncludedRouter 占位，故比对时先展平。
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod

BASELINE_ROUTES = [
    (('GET', 'HEAD'), '/openapi.json'),
    (('GET', 'HEAD'), '/docs'),
    (('GET', 'HEAD'), '/docs/oauth2-redirect'),
    (('GET', 'HEAD'), '/redoc'),
    (('GET',), '/v/api/v1/tag/genres'),
    (('GET',), '/v/api/v1/tag/iso3166'),
    (('GET',), '/v/api/v1/tag/list'),
    (('GET',), '/v/api/v1/mediadb/list'),
    (('GET',), '/v/api/v1/mdb/{guid}'),
    (('GET',), '/v/api/v1/mediadb/sum'),
    (('POST',), '/v/api/v1/item/list'),
    (('GET',), '/v/api/v1/search/list'),
    (('GET', 'HEAD'), '/v/media/online/{mg}/t4'),
    (('GET', 'HEAD'), '/v/media/t4-resource/{token}'),
    (('GET',), '/v/api/v1/item/{guid}'),
    (('GET',), '/v/api/v1/season/list/{guid}'),
    (('GET',), '/v/api/v1/episode/list/{guid}'),
    (('GET',), '/v/api/v1/stream/list/{guid}'),
    (('POST',), '/v/api/v1/stream/list'),
    (('PUT',), '/v/api/v1/item/favorite'),
    (('DELETE',), '/v/api/v1/item/favorite'),
    (('POST',), '/v/api/v1/item/watched'),
    (('DELETE',), '/v/api/v1/item/watched'),
    (('POST',), '/v/api/v1/favorite/list'),
    (('POST',), '/v/api/v1/play/info'),
    (('POST',), '/v/api/v1/stream'),
    (('GET', 'HEAD'), '/v/api/v1/media/range/{mg}'),
    (('POST',), '/v/api/v1/play/play'),
    (('GET',), '/v/media/online/hgdj/{series_guid}.mp4'),
    (('GET',), '/v/media/online/{mg}/direct'),
    (('POST',), '/v/api/v1/person/list/{guid}'),
    (('POST',), '/v/api/v1/play/record'),
    (('DELETE',), '/v/api/v1/play/record'),
    (('GET',), '/v/api/v1/play/list'),
    (('POST',), '/v/api/v1/media/p'),
    (('GET',), '/v/media/online/{mg}/preset.m3u8'),
    (('GET',), '/v/media/online/{mg}/main.m3u8'),
    (('OPTIONS',), '/v/media/online/{rest:path}'),
    (('GET',), '/v/media/online/{mg}/_placeholder/placeholder.ts'),
    (('GET',), '/v/media/online/{mg}/seg'),
    (('GET',), '/v/media/online/{mg}/hls'),
    (('GET',), '/online/{tok_path:path}'),
    (('GET',), '/v/api/v1/sys/img/online/{tok_path:path}'),
    (('GET',), '/_ext/healthz'),
    (('GET',), '/v/api/ext/whoami'),
    (('GET',), '/v/api/ext/live'),
    (('POST',), '/v/api/ext/live/clear'),
    (('GET',), '/v/api/ext/titles-cache'),
    (('POST',), '/v/api/ext/titles-cache/clear'),
    (('GET',), '/v/api/ext/cache'),
    (('POST',), '/v/api/ext/cache/clear'),
    (('GET',), '/v/api/ext/admin/token'),
    (('POST',), '/v/api/ext/admin/login'),
    (('POST',), '/v/api/ext/admin/token/rotate'),
    (('GET',), '/v/api/ext/status'),
    (('GET',), '/v/api/ext/diag/resume'),
    (('GET',), '/v/api/ext/continue'),
    (('GET',), '/v/api/ext/follow'),
    (('POST',), '/v/api/ext/follow'),
    (('POST',), '/v/api/ext/follow/check'),
    (('GET',), '/v/api/ext/source-health'),
    (('POST',), '/v/api/ext/source-health/reset'),
    (('POST',), '/v/api/ext/source/heal'),
    (('POST',), '/v/api/ext/favorite/enrich'),
    (('GET',), '/v/api/ext/update/check'),
    (('GET',), '/v/api/ext/update/status'),
    (('POST',), '/v/api/ext/update/now'),
    (('POST',), '/v/api/ext/source/priority'),
    (('GET',), '/v/api/ext/libs'),
    (('POST',), '/v/api/ext/lib/pin'),
    (('GET',), '/v/api/ext/accounts'),
    (('POST',), '/v/api/ext/account/acl'),
    (('POST',), '/v/api/ext/account/defaults'),
    (('GET',), '/v/api/ext/local-libs'),
    (('POST',), '/v/api/ext/lib/visibility'),
    (('POST',), '/v/api/ext/lib/order'),
    (('POST',), '/v/api/ext/hot/refresh'),
    (('GET',), '/v/api/ext/online'),
    (('POST',), '/v/api/ext/config'),
    (('POST',), '/v/api/ext/subscription-proxy'),
    (('POST',), '/v/api/ext/junk-keywords'),
    (('POST',), '/v/api/ext/source-proxy'),
    (('POST',), '/v/api/ext/source/add'),
    (('POST',), '/v/api/ext/source/standalone'),
    (('POST',), '/v/api/ext/source/loose-filter'),
    (('POST',), '/v/api/ext/source/adult'),
    (('POST',), '/v/api/ext/source/delete'),
    (('POST',), '/v/api/ext/source/restore'),
    (('POST',), '/v/api/ext/source/test'),
    (('POST',), '/v/api/ext/source/test-all'),
    (('POST',), '/v/api/ext/source/health-check'),
    (('GET',), '/v/api/ext/source/export'),
    (('POST',), '/v/api/ext/source/import-file'),
    (('POST',), '/v/api/ext/source/update'),
    (('POST',), '/v/api/ext/source/import-tvbox'),
    (('POST',), '/v/api/ext/source/import-subscription'),
    (('GET',), '/v/api/ext/adfilter/status'),
    (('POST',), '/v/api/ext/adfilter/toggle'),
    (('POST',), '/v/api/ext/adfilter/rules'),
    (('GET',), '/v/online-admin'),
    (('GET',), '/v/api/ext/catvod/credentials'),
    (('POST',), '/v/api/ext/catvod/credentials'),
    (('GET',), '/v/api/ext/catvod/home'),
    (('GET',), '/v/api/ext/catvod/category'),
    (('GET',), '/v/api/ext/catvod/detail'),
    (('GET',), '/v/api/ext/catvod/search'),
    (('GET',), '/v/api/ext/catvod/play'),
    (('GET',), '/v/ext/catvod/redirect'),
    (('GET',), '/v/ext/xlib/play'),
    (('GET',), '/v/api/ext/drpy/status'),
    (('GET',), '/v/api/ext/drpy/home'),
    (('GET',), '/v/api/ext/drpy/category'),
    (('GET',), '/v/api/ext/drpy/detail'),
    (('GET',), '/v/api/ext/drpy/play'),
    (('GET',), '/v/api/ext/drpy/remote'),
    (('POST',), '/v/api/ext/drpy/remote-toggle'),
    (('POST',), '/v/api/ext/drpy/remote-remove'),
    (('POST',), '/v/api/ext/drpy/remote-retry'),
    (('GET',), '/v/api/ext/source-health'),
    (('POST',), '/v/api/ext/source-health/reset'),
    (('GET',), '/v/api/ext/linepredict'),
    (('POST',), '/v/api/ext/linepredict/config'),
    (('POST',), '/v/api/ext/linepredict/reset'),
    (('GET',), '/v/api/ext/guard/status'),
    (('POST',), '/v/api/ext/guard/import'),
    (('POST',), '/v/api/ext/guard/toggle'),
    (('POST',), '/v/api/ext/guard/remove'),
    (('GET',), '/v/api/ext/guard/jar'),
    (('POST',), '/v/api/ext/guard/jar'),
    (('GET',), '/v/api/ext/spider/status'),
    (('POST',), '/v/api/ext/spider/toggle'),
    (('POST',), '/v/api/ext/spider/remove'),
    (('POST',), '/v/api/ext/spider/jar'),
    (('POST',), '/v/api/ext/spider/jar-retry'),
    (('POST',), '/v/api/ext/spider/probe'),
    (('POST',), '/v/api/ext/spider/scan'),
    (('GET',), '/v/api/ext/spider/scan'),
    (('POST',), '/v/api/ext/spider/remove-many'),
    (('GET',), '/v/api/ext/guard/gnstate'),
    (('GET',), '/v/api/ext/guard/home'),
    (('GET',), '/v/api/ext/guard/category'),
    (('GET',), '/v/api/ext/pan/status'),
    (('POST',), '/v/api/ext/pan/detect'),
    (('POST',), '/v/api/ext/pan/qr/start'),
    (('POST',), '/v/api/ext/pan/uc/qr/start'),
    (('POST',), '/v/api/ext/pan/ali/qr/start'),
    (('POST',), '/v/api/ext/pan/ali/cred'),
    (('GET',), '/v/api/ext/pan/page'),
    (('POST',), '/v/api/ext/pan/qr/poll'),
    (('POST',), '/v/api/ext/pan/uc/qr/poll'),
    (('POST',), '/v/api/ext/pan/ali/qr/poll'),
    (('POST',), '/v/api/ext/pan/cookie'),
    (('POST',), '/v/api/ext/pan/uc/cookie'),
    (('POST',), '/v/api/ext/pan/ali/cookie'),
    (('POST',), '/v/api/ext/pan/resolve'),
    (('POST',), '/v/api/ext/pan/uc/resolve'),
    (('POST',), '/v/api/ext/pan/ali/resolve'),
    (('GET',), '/v/api/ext/pan/list'),
    (('GET',), '/v/api/ext/pan/uc/list'),
    (('GET',), '/v/api/ext/pan/ali/list'),
    (('POST',), '/v/api/ext/pan/logout'),
    (('POST',), '/v/api/ext/pan/uc/logout'),
    (('POST',), '/v/api/ext/pan/ali/logout'),
    (('GET', 'HEAD'), '/v/api/ext/pan/play'),
    (('GET', 'HEAD'), '/v/api/ext/pan/uc/play'),
    (('GET', 'HEAD'), '/v/api/ext/pan/ali/play'),
    (('GET',), '/v/api/ext/guard/browse'),
    (('GET',), '/v/api/ext/sidecar/status'),
    (('POST',), '/v/api/ext/sidecar/install'),
    (('GET',), '/v/api/ext/sidecar/log'),
    (('POST',), '/v/api/ext/sidecar/reconnect'),
    (('POST',), '/v/api/ext/sidecar/android/stop'),
    (('POST',), '/v/api/ext/sidecar/android/start'),
    (('DELETE', 'GET', 'HEAD', 'OPTIONS', 'PATCH', 'POST', 'PUT'), '/{full_path:path}'),
]


def _flat_routes():
    out = []
    def walk(rs):
        for r in rs:
            sub = getattr(r, "original_router", None)
            if sub is not None:
                walk(sub.routes)
            else:
                out.append(r)
    walk(appmod.app.routes)
    return [(tuple(sorted(r.methods)) if getattr(r, "methods", None) else (),
             getattr(r, "path", "")) for r in out]


def test_route_table_unchanged():
    assert _flat_routes() == BASELINE_ROUTES


def test_split_modules_import_standalone():
    import admin_api
    guard_native = importlib.import_module("guard" + "_native")
    import hls_race
    import lib_inject
    assert admin_api.admin_router is not None
    assert len(hls_race.hls_router.routes) == 6
    assert len(admin_api.admin_router.routes) == 129
    assert guard_native.guard_native_enabled is appmod.guard_native_enabled
    assert lib_inject._all_virtual_lib_guids is appmod._all_virtual_lib_guids
    assert hls_race._ensure_playable is appmod._ensure_playable
    assert admin_api.ext_status is appmod.ext_status


def test_reexports_present():
    for name in ("guard_native_enabled", "_gn_hot_rows", "_gn_state",
                 "GUARD_NATIVE_GUID", "_pan_match",
                 "_all_virtual_lib_guids", "_xlib_play_info",
                 "_XLIB_MEDIA_REVERSE",
                 "_race_candidates", "_ensure_playable", "_LINE_FAST_TTL",
                 "_RACE_CONCURRENCY", "_SPEED_PRIOR", "_SPEED_GOOD",
                 "remembered_fast_line", "hls_router",
                 "ext_status", "ext_continue", "ext_diag_resume",
                 "ext_source_add", "ext_follow_check", "admin_router",
                 "_PAN_MODS"):
        assert hasattr(appmod, name), f"missing re-export {name}"


def test_log_object_shared():
    guard_native = importlib.import_module("guard" + "_native")
    import hls_race
    import lib_inject
    assert appmod._log is guard_native._log
    assert appmod._log is lib_inject._log
    assert appmod._log is hls_race._log


def test_turbo_freeze_after_split():
    assert appmod._LINE_FAST_TTL == 600.0
    assert appmod._RACE_CONCURRENCY == 4
    assert appmod._SPEED_PRIOR == 150.0
    assert appmod._SPEED_GOOD == 200.0
    # _race_fastest 本体留在 app.py、源码哈希不变（见 test_gaps_play）
    assert appmod._race_fastest.__module__ == "app"
    digest = hashlib.sha256(
        inspect.getsource(appmod._race_fastest).encode()).hexdigest()
    assert digest == "82c4f45a3a9372e21cdb464a0dcbd93b96ae7c458db09e3fc87fa28dca141700"
    import hls_race
    assert hls_race.ONLINE_PREFIX == appmod.ONLINE_PREFIX
