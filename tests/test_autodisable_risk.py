"""探活自动禁用 + 风险源判定。

覆盖 2026-11-13 新增：
- 探活明确失效（alive is False）→ 自动禁用；
- alive=None（不可达 / 无运行时）→ 不动；
- auto_disable=False → 不动；
- 风险源（关键词命中）→ 探活通过也禁用；
- providers.source_is_risky 纯函数矩阵。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import admin_api  # noqa: E402
import config  # noqa: E402
import providers  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def srcs():
    """两个 CMS 源：一个正常、一个已死。"""
    cfg = config.config
    before = cfg.all_sources()
    cfg.set_sources([
        {"id": "ok_1", "name": "正常站", "api": "https://a.example/api.php/provide/vod"},
        {"id": "dead_1", "name": "死站", "api": "https://b.example/api.php/provide/vod"},
    ])
    try:
        yield cfg
    finally:
        cfg.set_sources(before)


def _probe_map(dead_ids=()):
    async def fake(source, timeout=8.0):
        sid = source.get("id")
        if sid in dead_ids:
            return {"alive": False, "hit": "", "elapsed_ms": 30}
        return {"alive": True, "hit": "庆余年", "elapsed_ms": 20}
    return fake


def _enabled(cfg, sid):
    for s in cfg.all_sources():
        if s.get("id") == sid:
            return s.get("enabled")
    return None


def test_dead_source_auto_disabled(srcs):
    with patch.object(providers, "probe_any_source",
                      new=AsyncMock(side_effect=_probe_map({"dead_1"}))), \
            TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/source/health-check", json={})
        d = r.json()["data"]
        assert d["dead"] == 1
        assert d["auto_disabled"] == 1
    assert _enabled(srcs, "dead_1") is False
    assert _enabled(srcs, "ok_1") is not False


def test_unknown_alive_untouched(srcs):
    async def fake(source, timeout=8.0):
        return {"alive": None, "hit": "", "elapsed_ms": 0}

    with patch.object(providers, "probe_any_source",
                      new=AsyncMock(side_effect=fake)), \
            TestClient(appmod.app, raise_server_exceptions=False) as cli:
        d = cli.post("/v/api/ext/source/health-check", json={}).json()["data"]
        assert d["auto_disabled"] == 0
    assert _enabled(srcs, "dead_1") is not False


def test_auto_disable_off(srcs):
    with patch.object(providers, "probe_any_source",
                      new=AsyncMock(side_effect=_probe_map({"dead_1"}))), \
            TestClient(appmod.app, raise_server_exceptions=False) as cli:
        d = cli.post("/v/api/ext/source/health-check",
                     json={"auto_disable": False}).json()["data"]
        assert d["auto_disabled"] == 0
    assert _enabled(srcs, "dead_1") is not False


def test_risky_source_disabled_even_alive(srcs):
    cfg = srcs
    cfg.set_sources([
        {"id": "risky_1", "name": "麻豆传媒资源", "api": "https://c.example/api.php/provide/vod"},
        {"id": "ok_2", "name": "正常站", "api": "https://a.example/api.php/provide/vod"},
    ])
    with patch.object(providers, "probe_any_source",
                      new=AsyncMock(side_effect=_probe_map())), \
            TestClient(appmod.app, raise_server_exceptions=False) as cli:
        d = cli.post("/v/api/ext/source/health-check", json={}).json()["data"]
        assert d["alive"] == 2
        assert d["auto_disabled"] == 1
    assert _enabled(cfg, "risky_1") is False
    assert _enabled(cfg, "ok_2") is not False


@pytest.mark.parametrize("src,expect", [
    ({"name": "正常站", "api": "https://a/x"}, False),
    ({"name": "麻豆传媒", "api": "https://a/x"}, True),
    ({"name": "午夜福利社", "api": "https://a/x"}, True),
    ({"name": "SWAG合集", "api": "https://a/x"}, True),
    ({"name": "正常站", "api": "https://a/Adult/list"}, True),
    ({"name": "正常站", "api": "https://a/x", "risk": True}, True),
    ({}, False),
])
def test_source_is_risky_matrix(src, expect):
    assert providers.source_is_risky(src) is expect
