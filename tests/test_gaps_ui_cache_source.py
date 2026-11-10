"""管理页开关 + 三个管理端点组（移植上游 v0.2.54 缓存管理 / v0.2.69 CMS 源
导入导出 / v0.2.71 批量体检；源代理/SPS/linepredict 开关只补管理页 UI，
后端早已在）。

只测本移植引入的行为；不碰真实网络/飞牛/播放链路。凡是会写盘的配置、
缓存一律打到 tmp_path，绝不碰仓库真实的 state/。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import catalog as catalogmod
import charts as chartmod
import config as configmod
import providers
import sourcehealth
import store as storemod

CMS_A = {"id": "custom_aaaa", "name": "甲站",
         "api": "https://a.example.com/api.php/provide/vod/at/json",
         "enabled": True, "standalone": False, "builtin": False}
CMS_B = {"id": "custom_bbbb", "name": "乙站",
         "api": "https://b.example.com/api.php/provide/vod/at/json",
         "enabled": True, "standalone": False, "builtin": False}


def _fresh_env(tmp_path, monkeypatch, sources=None):
    """配置 + titles 缓存打到 tmp；appmod.config/store 与定时刷新打桩。"""
    cfg_state = tmp_path / "config.json"
    monkeypatch.setattr(configmod, "_STATE", cfg_state)
    cfg = configmod.Config()
    if sources is not None:
        cfg.save({"sources": sources})
    monkeypatch.setattr(appmod, "config", cfg)
    monkeypatch.setattr(configmod, "config", cfg)

    titles_state = tmp_path / "titles.json"
    monkeypatch.setattr(storemod, "_STATE", titles_state)
    st = storemod.Store()
    monkeypatch.setattr(appmod, "store", st)
    monkeypatch.setattr(storemod, "store", st)
    monkeypatch.setattr(appmod, "_schedule_routes_refresh", lambda: None)
    monkeypatch.setattr(chartmod, "STATE", tmp_path / "charts.json")
    return cfg, st


# ---------------- 路由注册 ----------------

def test_gaps_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ("/v/api/ext/cache", "/v/api/ext/cache/clear",
              "/v/api/ext/titles-cache", "/v/api/ext/titles-cache/clear",
              "/v/api/ext/source/export", "/v/api/ext/source/import-file",
              "/v/api/ext/source/update", "/v/api/ext/source/health-check"):
        assert p in paths, f"missing route {p}"


# ---------------- probe_source_alive（上游 v0.2.71） ----------------

def test_probe_source_alive_second_keyword_hit():
    calls = []

    async def fake_probe(api, keyword="庆余年", timeout=8.0, source=None):
        calls.append(keyword)
        if keyword == "狂飙":
            return {"ok": True, "status": 200, "elapsed_ms": 12,
                    "count": 3, "sample": "狂飙", "error": ""}
        return {"ok": True, "status": 200, "elapsed_ms": 10,
                "count": 0, "sample": "", "error": "接口正常（该词无结果）"}

    with patch.object(providers, "probe_source", new=fake_probe):
        r = asyncio.run(providers.probe_source_alive(
            {"api": "https://x.example.com/api.php", "id": "custom_1"}))
    assert r["alive"] is True
    assert r["hit"] == "狂飙"
    assert r["tried"] == ["庆余年", "狂飙"]
    assert r["count"] == 3
    assert calls == ["庆余年", "狂飙"]  # 命中即停，不继续试


def test_probe_source_alive_all_miss():
    async def fake_probe(api, keyword="庆余年", timeout=8.0, source=None):
        return {"ok": False, "status": 200, "elapsed_ms": 5,
                "count": 0, "sample": "", "error": "无返回结果"}

    with patch.object(providers, "probe_source", new=fake_probe):
        r = asyncio.run(providers.probe_source_alive(
            {"api": "https://x.example.com/api.php", "id": "custom_1"}))
    assert r["alive"] is False
    assert r["hit"] == ""
    assert r["tried"] == list(providers.PROBE_HOT_KEYWORDS)


# ---------------- config.update_source（上游 v0.2.69） ----------------

def test_update_source_rename_and_api_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(configmod, "_STATE", tmp_path / "config.json")
    cfg = configmod.Config()
    cfg.save({"sources": [dict(CMS_A)]})
    ok, sid = cfg.update_source("custom_aaaa", name="甲站改名")
    assert ok and sid == "custom_aaaa"
    assert cfg.all_sources()[0]["name"] == "甲站改名"
    new_api = "https://a2.example.com/api.php/provide/vod/at/json"
    ok, sid2 = cfg.update_source("custom_aaaa", api=new_api)
    assert ok and sid2 == cfg.make_custom_id(new_api)
    assert cfg.all_sources()[0]["api"] == new_api


def test_update_source_builtin_api_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(configmod, "_STATE", tmp_path / "config.json")
    cfg = configmod.Config()
    builtin = [s for s in cfg.all_sources() if s.get("builtin")]
    assert builtin, "需要至少一个内置源做拒绝分支"
    ok, msg = cfg.update_source(builtin[0]["id"],
                                api="https://evil.example.com/api.php")
    assert ok is False and "内置源" in msg


# ---------------- charts / catalog reset ----------------

def test_charts_reset_clears_memory_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(chartmod, "STATE", tmp_path / "charts.json")
    cache = chartmod.ChartCache()
    with cache._lock:
        cache._charts = {"movie": [{"guid": "g1", "title": "片"}]}
        cache._index = {"g1": {"guid": "g1"}}
    (tmp_path / "charts.json").write_text("{}", encoding="utf-8")
    cache.reset()
    assert cache.items("movie") == []
    assert cache.get_guid("g1") is None
    assert not (tmp_path / "charts.json").exists()


def test_catalog_reset_clears_routes_and_pages():
    cat = catalogmod.Catalog()
    with cat._lock:
        cat._routes["agg:movie"] = [{"source_id": "s"}]
        cat._page_cache[("agg", "movie", 1)] = (0.0, [])
        cat._fallbacks[("s", "movie")] = [{"source_id": "s"}]
    cat.reset()
    assert cat._routes == {}
    assert cat._page_cache == {}
    assert cat._fallbacks == {}


# ---------------- 缓存管理端点（上游 v0.2.54） ----------------

def test_cache_status_and_clear(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch)
    appmod._search_cache["kw"] = (0.0, [])
    appmod._AGG_CACHE["kw"] = (0.0, [])
    with chartmod.chart_cache._lock:
        chartmod.chart_cache._charts = {"movie": [{"guid": "g1"}]}
    with catalogmod.catalog._lock:
        catalogmod.catalog._routes["agg:movie"] = [{"source_id": "s"}]
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.get("/v/api/ext/cache")
        assert r.status_code == 200
        d = r.json()["data"]
        assert set(d) >= {"titles", "charts", "catalog", "search", "health"}
        assert d["search"]["entries"] == 1
        r = cli.post("/v/api/ext/cache/clear",
                     json={"targets": ["search", "charts", "catalog"]})
        assert r.status_code == 200
        assert r.json()["data"]["cleared"] == ["search", "charts", "catalog"]
        assert r.json()["data"]["restart"] is False
    assert appmod._search_cache == {}
    assert appmod._AGG_CACHE == {}
    assert chartmod.chart_cache.items("movie") == []
    assert catalogmod.catalog._routes == {}


def test_cache_clear_health_resets_portrait(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch)
    sourcehealth.record("k1", True, 0.1)
    assert sourcehealth.snapshot()
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/cache/clear", json={"targets": ["health"]})
        assert r.status_code == 200
        assert "health" in r.json()["data"]["cleared"]
    assert sourcehealth.snapshot() == {}


def test_titles_cache_status(tmp_path, monkeypatch):
    _cfg, st = _fresh_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.get("/v/api/ext/titles-cache")
        assert r.status_code == 200
        assert set(r.json()) >= {"entries", "bytes", "dirty"}


def test_titles_cache_clear_no_real_restart(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch)
    # 挡住 systemctl 重启线程：只断言内存/落盘被清空，不真的重启服务。
    # 注意：Thread 只能在 TestClient 建好之后再 mock——TestClient 本身
    # 要靠真实线程跑 portal，先 mock 会死锁。
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        with patch("threading.Thread") as mock_thread:
            r = cli.post("/v/api/ext/titles-cache/clear")
        assert r.status_code == 200
        assert r.json()["code"] == 0
        mock_thread.assert_called_once()
        assert appmod.store._titles == {}


# ---------------- CMS 源导入 / 导出 / 编辑（上游 v0.2.69） ----------------

def test_source_export_only_pure_cms(tmp_path, monkeypatch):
    sources = [dict(CMS_A),
               {"id": "guard-1", "name": "加固站",
                "api": "https://g.example.com/api.php", "enabled": True,
                "guard": True, "builtin": False},
               {"id": "t4-1", "name": "T4站",
                "api": "https://t.example.com/", "enabled": True,
                "t4": True, "builtin": False}]
    _fresh_env(tmp_path, monkeypatch, sources=sources)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.get("/v/api/ext/source/export")
        assert r.status_code == 200
        lst = r.json()["data"]["sources"]
        assert [s["name"] for s in lst] == ["甲站"]
        assert "id" not in lst[0]  # 不含 id/内置标记，便于跨设备导入


def test_source_import_file_add_skip_noncms(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch, sources=[dict(CMS_A)])
    payload = {"sources": [
        {"name": "乙站", "api": CMS_B["api"], "enabled": True,
         "loose_filter": True},
        {"name": "甲站重复", "api": CMS_A["api"]},
        {"name": "爬虫站", "api": "https://c.example.com/api.php",
         "jar": "https://c.example.com/jar.jar"},
        {"name": "无地址", "api": ""},
    ]}
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/source/import-file", json=payload)
        assert r.status_code == 200
        assert r.json()["data"] == {"added": 1, "skipped": 1,
                                     "ignored_noncms": 2}
        names = [s["name"] for s in appmod.config.all_sources()
                 if s["id"] == configmod.Config.make_custom_id(CMS_B["api"])]
        assert names == ["乙站"]
        # loose_filter 走 fork 的 set_loose_filter 落盘（上游 save({sid}) 在
        # fork 是 no-op，见 app.py 适配注释）
        imported = [s for s in appmod.config.all_sources()
                    if s["api"] == CMS_B["api"]][0]
        assert imported.get("loose_filter") is True


def test_source_import_file_bad_body(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch, sources=[])
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/source/import-file", json={"nope": 1})
        assert r.status_code == 400
        assert r.json()["code"] == 400


def test_source_update_endpoint(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch, sources=[dict(CMS_A), dict(CMS_B)])
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/source/update",
                     json={"id": "custom_aaaa", "name": "甲站新名"})
        assert r.status_code == 200
        assert r.json()["data"]["id"] == "custom_aaaa"
        new_api = "https://a9.example.com/api.php/provide/vod/at/json"
        r = cli.post("/v/api/ext/source/update",
                     json={"id": "custom_aaaa", "name": "甲",
                           "api": new_api})
        assert r.status_code == 200
        assert r.json()["data"]["id"] == \
            configmod.Config.make_custom_id(new_api)
        r = cli.post("/v/api/ext/source/update",
                     json={"id": "no-such", "name": "x"})
        assert r.status_code == 404


# ---------------- 批量体检（上游 v0.2.71） ----------------

def test_source_health_check(tmp_path, monkeypatch):
    sources = [dict(CMS_A),
               {"id": "guard-1", "name": "加固站",
                "api": "https://g.example.com/api.php", "enabled": True,
                "guard": True, "builtin": False}]
    _fresh_env(tmp_path, monkeypatch, sources=sources)
    sourcehealth.reset()

    async def fake_alive(source):
        if source["id"] == "custom_aaaa":
            return {"alive": True, "ok": True, "status": 200,
                    "elapsed_ms": 20, "count": 5, "sample": "庆余年",
                    "error": "", "hit": "庆余年", "tried": ["庆余年"]}
        return {"alive": False, "ok": False, "status": 0,
                "elapsed_ms": 0, "count": 0, "sample": "",
                "error": "无返回结果", "hit": "", "tried": ["庆余年"]}

    with patch.object(providers, "probe_source_alive",
                      new=AsyncMock(side_effect=fake_alive)), \
            TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/source/health-check", json={})
        assert r.status_code == 200
        d = r.json()["data"]
        # 只检测纯 CMS 源：加固站被排除
        assert d["total"] == 1
        assert d["alive"] == 1 and d["dead"] == 0
        assert d["keywords"] == list(providers.PROBE_HOT_KEYWORDS)
        assert d["results"][0]["id"] == "custom_aaaa"
        assert d["results"][0]["hit"] == "庆余年"
    # 有效结果回写健康画像（清零失败计数），画像里有该源
    snap = sourcehealth.snapshot()
    assert any("custom_aaaa" in k for k in snap)
    sourcehealth.reset()


def test_source_health_check_dead_written_to_portrait(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch, sources=[dict(CMS_A)])
    sourcehealth.reset()

    async def fake_alive(source):
        return {"alive": False, "ok": False, "status": 0,
                "elapsed_ms": 0, "count": 0, "sample": "",
                "error": "无返回结果", "hit": "", "tried": ["庆余年"]}

    with patch.object(providers, "probe_source_alive",
                      new=AsyncMock(side_effect=fake_alive)), \
            TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/source/health-check", json={})
        assert r.json()["data"]["dead"] == 1
    snap = sourcehealth.snapshot()
    key = next(k for k in snap if "custom_aaaa" in k)
    assert snap[key]["fails"] >= 1
    sourcehealth.reset()


# ---------------- linepredict ttl 保存（管理页开关配套） ----------------

def test_linepredict_config_saves_ttl(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/linepredict/config",
                     json={"enabled": True, "max_lines": 2, "ttl_min": 45})
        assert r.status_code == 200
        assert r.json()["data"]["linepredict_ttl_min"] == 45
        assert appmod.config.get()["linepredict_ttl_min"] == 45
        # 越界值钳制，不改默认值语义
        r = cli.post("/v/api/ext/linepredict/config",
                     json={"ttl_min": 9999})
        assert r.json()["data"]["linepredict_ttl_min"] == 120
