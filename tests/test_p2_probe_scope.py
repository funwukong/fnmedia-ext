"""P2 回归：复活探测 scope / 探活结束联动刷新 / 起播开关报告字段。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))


def test_probe_scope_suspect_filtering():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert 'scope == "suspect"' in src
    # 疑似判定：样本 >= 3 且从未成功
    assert '(v.get("samples") or 0) >= 3' in src
    assert "not (v.get(\"last_ok\") or 0)" in src
    # 复活探测不删源
    assert "disable_sink=None)" in src


def test_probe_all_scope_unchanged():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    # 全源探活保持原语义：删失效/风险源
    assert "config.remove_source(sid)" in src


def test_ui_revive_button_and_poll_refresh():
    html = (ROOT / "proxy" / "assets" / "admin.html").read_text(encoding="utf-8")
    assert 'id="srcReviveBtn"' in html
    assert "sourceProbeStart('suspect')" in html
    # scope 透传
    assert "scope:scope||\"all\"" in html
    # 空目标提示
    assert "没有疑似失效的源" in html
    # 探活结束联动刷新（质量徽章 + 健康页）
    assert "renderSources();}catch(e){}" in html
    assert "loadHealth();}catch(e){}" in html


def test_probe_start_empty_targets_returns_status():
    import source_probe
    import asyncio

    async def run():
        st = await source_probe.start([], concurrency=4, limit=0,
                                      disable_sink=None)
        await source_probe.wait()
        return source_probe.status()

    st = asyncio.run(run())
    assert st["total"] == 0
    assert st["running"] is False


def test_probe_writes_health_on_success_and_failure():
    import source_probe
    import sourcehealth
    import asyncio

    sourcehealth.reset()

    async def fake_ok(source):
        return {"ok": True, "alive": True, "elapsed_ms": 100, "count": 3,
                "hit": "kw", "tried": ["kw"], "error": "", "status": 200}

    async def fake_dead(source):
        return {"ok": True, "alive": False, "elapsed_ms": 100, "count": 0,
                "hit": "", "tried": ["kw"], "error": "", "status": 200}

    src_ok = {"id": "__p_ok", "name": "ok", "api": "https://x/a"}
    src_dead = {"id": "__p_dead", "name": "dead", "api": "https://x/b"}

    async def run():
        await source_probe.start([src_ok], concurrency=1, limit=0,
                                 disable_sink=None)
        orig = source_probe._probe_impl
        await source_probe.wait()
        assert sourcehealth.stats("__p_ok").get("success_rate") == 1
        source_probe._probe_impl = fake_dead
        try:
            await source_probe.start([src_dead], concurrency=1, limit=0,
                                     disable_sink=None)
            await source_probe.wait()
        finally:
            source_probe._probe_impl = orig
        st = sourcehealth.stats("__p_dead")
        assert st.get("samples", 0) >= 1
        assert st.get("success_rate", 0) == 0

    # fake_ok 注入
    source_probe._probe_impl = fake_ok
    try:
        asyncio.run(run())
    finally:
        source_probe._probe_impl = None
    sourcehealth.reset()
