"""站点列表刷新 + 缓存载入性能优化 —— 守护测试。

覆盖三件事：
1) /v/api/ext/source/refresh 端点契约（返回源列表/计数/健康/路由/耗时，
   且超时不阻塞用户——走后台补跑）。
2) config.read() 单键读取：值与 config.get()[k] 一致，且不做整份深拷贝。
3) store 流式载入：增量按条数与字节裁剪，不再对每条做
   expand→compact→dumps 往返；裁剪后保留的是最新的 N 条。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

PROXY = Path(__file__).resolve().parents[1] / "proxy"
if str(PROXY) not in sys.path:
    sys.path.insert(0, str(PROXY))


# ---------------- 1. 刷新端点 ----------------

def _client(monkeypatch, token="t0k"):
    """用真实 app 做测试客户端：admin 鉴权在 app 层包装，不能只挂 router。"""
    import os
    monkeypatch.setenv("FNMEDIA_ADMIN_TOKEN", token)
    import app as appmod
    from fastapi.testclient import TestClient
    return TestClient(appmod.app), appmod


def test_refresh_endpoint_returns_full_snapshot(monkeypatch):
    """刷新端点：一次调用返回源列表 + 计数 + 健康 + 路由 + 耗时。"""
    cl, _ = _client(monkeypatch)
    r = cl.post("/v/api/ext/source/refresh", json={"scope": "routes"},
                headers={"X-Admin-Token": "t0k"})
    assert r.status_code == 200
    d = r.json()
    assert d["code"] == 0
    data = d["data"]
    assert isinstance(data["sources"], list)
    counts = data["counts"]
    assert counts["total"] == len(data["sources"])
    assert counts["enabled"] == sum(1 for s in data["sources"] if s["enabled"])
    assert "health" in data and isinstance(data["health"], dict)
    assert "routes" in data
    assert isinstance(data["elapsed_ms"], int)
    assert data["cleared"] == {"search": True, "agg": True, "playback": True}
    # 每条源都带前端渲染要用的字段
    if data["sources"]:
        s = data["sources"][0]
        for k in ("id", "name", "api", "enabled", "standalone",
                  "kind", "priority"):
            assert k in s, k


def test_refresh_does_not_block_on_slow_routes(monkeypatch):
    """路由重建慢时必须快速返回，并把结果标记为后台进行中。"""
    cl, appmod = _client(monkeypatch)
    import catalog
    import admin_api

    async def _slow(self, _sources):
        import asyncio
        await asyncio.sleep(30)
        return {"categories": 999}

    # admin_api 里用的是 `catalog.refresh_routes(...)`，catalog 是该模块
    # 导入的模块对象；refresh_routes 实为 Catalog 实例方法，patch 类属性。
    monkeypatch.setattr(type(catalog.catalog), "refresh_routes", _slow)
    # 后台补跑的任务别真去跑网络
    monkeypatch.setattr(admin_api, "_schedule_routes_refresh", lambda: None)
    t0 = time.perf_counter()
    r = cl.post("/v/api/ext/source/refresh",
                json={"scope": "routes", "routes_timeout": 0.2},
                headers={"X-Admin-Token": "t0k"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    routes = r.json()["data"]["routes"]
    assert routes.get("pending") is True
    assert elapsed < 5.0, "刷新不应被慢路由拖住"


def test_refresh_registered_on_admin_router(monkeypatch):
    """端点必须注册在 admin_router 上（鉴权由 app 层统一包装）。"""
    import admin_api
    paths = {getattr(r, "path", "") for r in admin_api.admin_router.routes}
    assert "/v/api/ext/source/refresh" in paths
    # 必须被 app.py 显式导入并重挂（否则走不到统一鉴权）
    import app as appmod
    assert hasattr(appmod, "ext_source_refresh")


# ---------------- 2. config.read ----------------

def test_read_matches_get_for_every_key():
    import config
    c = config.config
    full = c.get()
    for k, v in full.items():
        assert c.read(k, "__missing__") == v, k


def test_read_returns_default_for_missing_key():
    import config
    assert config.config.read("__no_such_key__", 42) == 42
    assert config.config.read("__no_such_key__") is None


def test_read_many_preserves_order_and_default():
    import config
    c = config.config
    full = c.get()
    keys = list(full)[:5]
    got = c.read_many(*keys, default="d")
    assert got == tuple(full[k] for k in keys)
    assert c.read_many("__nope__", default=7) == (7,)


def test_read_is_much_faster_than_get():
    """单键读取不得走整份深拷贝（这里只证明数量级差异）。"""
    import config
    c = config.config
    c.get()
    n = 3000
    t = time.perf_counter()
    for _ in range(n):
        c.get()
    slow = (time.perf_counter() - t) / n
    t = time.perf_counter()
    for _ in range(n):
        c.read("hot_limit", 100)
    fast = (time.perf_counter() - t) / n
    assert fast < slow / 20, f"read={fast*1e6:.1f}us get={slow*1e6:.1f}us"


def test_read_respects_patched_get(monkeypatch):
    """测试/外部若替换了 get()，read() 必须跟着走替换后的语义。"""
    import config
    c = config.config

    def _fake_get():
        return {"hot_limit": 7}

    monkeypatch.setattr(c, "get", _fake_get)
    assert c.read("hot_limit", 999) == 7
    assert c.read_many("hot_limit") == (7,)


# ---------------- 3. store 流式载入 ----------------

def _make_titles(n_titles: int, n_lines: int, n_eps: int) -> dict:
    titles = {}
    for i in range(n_titles):
        lines = {}
        for ln in range(n_lines):
            lines["src%d::线路%d" % (ln, ln)] = [
                {"guid": "g%d_%d_%d" % (i, ln, e),
                 "title": "第%d集" % e,
                 "url": "https://x/%d.m3u8" % e}
                for e in range(n_eps)
            ]
        titles["guid%d" % i] = {
            "title": "片名%d" % i, "year": "2024", "lines": lines,
            "online_id": "src:a", "poster": "https://p/%d.jpg" % i,
        }
    return titles


def _store_with(path: Path):
    import store as S
    obj = S.Store.__new__(S.Store)
    obj._lock = threading.RLock()
    obj._titles = {}
    obj._orig = {}
    obj._titles_version = 0
    obj._state = path
    return obj, S


def test_stream_load_keeps_newest_and_respects_max(tmp_path, monkeypatch):
    """超上限时保留最后写入的 N 条（deque 尾进头出）。"""
    import config
    # 注意 _cache_cfg 有 max(100, max_n) 下限保护，上限必须 >=100 才生效。
    monkeypatch.setattr(config.config, "read",
                        lambda k, d=None: {"titles_cache_max_entries": 150,
                                           "titles_cache_max_mb": 0,
                                           "titles_flush_debounce": 5}.get(k, d))
    p = tmp_path / "titles.json"
    # 每集塞长 url：文件必须 >=1MB 才走流式分支，否则测的是小文件快速路径。
    pad = "https://cdn.example.com/vod/" + "x" * 2000 + "/play.m3u8"
    titles = _make_titles(300, 2, 3)
    for guid, rec in titles.items():
        for line in rec["lines"].values():
            for ep in line:
                ep["url"] = pad
    p.write_text(json.dumps({"titles": titles, "orig": {}}),
                 encoding="utf-8")
    assert p.stat().st_size > 1_000_000, "测试文件必须大到触发流式路径"
    obj, _ = _store_with(p)
    obj._load()
    assert len(obj._titles) == 150
    # 最新写入的最后 150 条必须在
    keys = list(titles)
    for k in keys[-150:]:
        assert k in obj._titles, k
    for k in keys[:-150]:
        assert k not in obj._titles, k


def test_stream_load_preserves_content(tmp_path, monkeypatch):
    """裁剪不得破坏条目内容：展开后的结构与原文件一致。"""
    import config
    monkeypatch.setattr(config.config, "read",
                        lambda k, d=None: {"titles_cache_max_entries": 4000,
                                           "titles_cache_max_mb": 0,
                                           "titles_flush_debounce": 5}.get(k, d))
    p = tmp_path / "titles.json"
    titles = _make_titles(12, 2, 2)
    p.write_text(json.dumps({"titles": titles, "orig": {"guid0": "x"}}),
                 encoding="utf-8")
    obj, _ = _store_with(p)
    obj._load()
    assert len(obj._titles) == 12
    for k, v in titles.items():
        got = obj._titles[k]
        assert got["title"] == v["title"]
        assert len(got["lines"]) == len(v["lines"])
        assert got["online_id"] == v["online_id"]
    assert obj._orig.get("guid0") == "x"


def test_stream_load_byte_budget_drops_oldest(tmp_path, monkeypatch):
    """字节上限生效时，超出预算的最旧条目被丢弃，且文件远小于预算。"""
    import config
    p = tmp_path / "titles.json"
    titles = _make_titles(200, 2, 3)
    raw = json.dumps({"titles": titles, "orig": {}})
    p.write_text(raw, encoding="utf-8")
    total_mb = len(raw.encode()) / 1024 / 1024
    # 预算取文件体积的 30%
    budget_mb = max(0.01, total_mb * 0.30)
    monkeypatch.setattr(config.config, "read",
                        lambda k, d=None: {"titles_cache_max_entries": 4000,
                                           "titles_cache_max_mb": budget_mb,
                                           "titles_flush_debounce": 5}.get(k, d))
    obj, _ = _store_with(p)
    obj._load()
    assert 0 < len(obj._titles) < 200
    # 保留的是尾部（最新）条目
    keys = list(titles)
    assert keys[-1] in obj._titles
    assert keys[0] not in obj._titles


def test_stream_load_handles_corrupt_entry(tmp_path, monkeypatch):
    """非对象条目被丢弃，不影响其余条目载入。"""
    import config
    monkeypatch.setattr(config.config, "read",
                        lambda k, d=None: {"titles_cache_max_entries": 4000,
                                           "titles_cache_max_mb": 0,
                                           "titles_flush_debounce": 5}.get(k, d))
    p = tmp_path / "titles.json"
    good = {"title": "好片", "lines": {}, "online_id": "a:b"}
    p.write_text(json.dumps({"titles": {"a": good, "bad": [1, 2, 3]},
                             "orig": {}}), encoding="utf-8")
    obj, _ = _store_with(p)
    obj._load()
    assert "a" in obj._titles
    assert "bad" not in obj._titles


# ---------------- 4. 前端渲染守护 ----------------

def test_render_uses_event_delegation():
    """资源站列表必须走事件委托，不得每行 addEventListener。"""
    ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
    assert "function bindSrcDelegate()" in ui
    assert "tb.dataset.bound" in ui
    # 旧的 per-row 绑定循环必须已删除
    assert '$$("#srcRows tr").forEach(tr=>{' not in ui


def test_render_is_chunked():
    """首屏同步 + 余量 requestAnimationFrame 分批，且有代数防串。"""
    ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
    i = ui.index("function renderSources()")
    seg = ui[i:i + 4000]
    assert "SRC_CHUNK" in seg
    assert "requestAnimationFrame" in seg
    assert "gen!==SRC_GEN" in seg  # 旧批次自动放弃


def test_refresh_button_wired():
    """页面必须有刷新按钮，且绑定到 source/refresh。"""
    ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
    assert 'id="srcRefreshBtn"' in ui
    assert "async function refreshSources()" in ui
    assert "/v/api/ext/source/refresh" in ui
