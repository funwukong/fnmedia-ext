"""运行缓存自动定时清理回归。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))


def test_defaults():
    import sys as _s
    _s.path.insert(0, str(ROOT / "proxy"))
    import config as cfgmod
    assert cfgmod.DEFAULTS["auto_cache_clear_enabled"] is True
    assert cfgmod.DEFAULTS["auto_cache_clear_hours"] == 6


def test_whitelist_and_validation():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '"auto_cache_clear_enabled"' in src
    assert '"auto_cache_clear_hours"' in src
    # 夹取 1-72
    assert "min(72, int(patch[\"auto_cache_clear_hours\"]))" in src


def test_loop_wired_and_cleans_search_charts_catalog():
    src = (ROOT / "proxy" / "app.py").read_text(encoding="utf-8")
    assert "_auto_cache_clear_loop" in src
    assert "auto-cache-clear" in src
    # 清理后重建：catalog.reset + refresh_routes 必须在循环里
    i = src.index("async def _auto_cache_clear_loop")
    body = src[i:i + 2600]
    assert "_search_cache.clear()" in body
    assert "_AGG_CACHE.clear()" in body
    assert "chart_cache.reset()" in body
    assert "catalog.reset()" in body
    assert "catalog.refresh_routes" in body
    # titles 不清（避免重启）、health 保留
    assert "titles" not in body
    assert "sourcehealth.reset" not in body


def test_status_endpoint_exists():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '@admin_router.get("/v/api/ext/cache/auto-clear")' in src
    assert "auto_cache_last_run" in src
    assert "auto_cache_next_run" in src


def test_ui_card_present():
    html = (ROOT / "proxy" / "assets" / "admin.html").read_text(encoding="utf-8")
    assert 'id="autoCacheClear"' in html
    assert 'id="autoCacheClearHours"' in html
    assert 'id="autoCacheClearNext"' in html
    assert "saveAutoCacheClear" in html
    assert "loadAutoCacheClear" in html
    # 手动清理按钮仍在
    assert "clearCaches(['search','charts','catalog'])" in html