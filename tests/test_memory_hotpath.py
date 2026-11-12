"""内存修复工人 3（启动预热与详情热路径）：复现 + 回归测试。

覆盖四项：
1. _hot_worker 预热：15/10 类串行 -> gather + 信号量 4 + 每轮总预算 120s。
2. refresh_routes 探测：约 100 路突发 -> 信号量 12 有界并发。
3. _attach_related / related_for：4000 条全池扫描 -> 按 guid 缓存命中。
4. standalone_window：深翻页每次从 0 重建 O(offset) 合并 -> merged 缓存切片。
"""
import asyncio
import time

import pytest


def _b(guid, title, **kw):
    d = {"guid": guid, "title": title, "poster": "http://x/p.jpg",
         "year": "2024", "area": "内地", "actor": "", "director": "",
         "type_name": "国产剧", "catalog_cat": "tv", "score": "8.0",
         "lines": {"L1": [{"direct": True}]}}
    d.update(kw)
    return d


CUR = _b("g0", "狂飙", actor="张译/张颂文", director="徐纪周")


def _pool(n=500):
    pool = [_b(f"g{i}", f"剧名{i}", catalog_cat="tv") for i in range(n)]
    pool[10] = _b("g10", "狂飙2", catalog_cat="tv", type_name="国产剧",
                  actor="张译", year="2024", score="9.0")
    return pool


# ---------------- 1. 预热并发 + 总预算 ----------------

def test_prewarm_concurrent_bounded_and_fast(monkeypatch):
    import app as appmod
    import catalog as catalogmod

    state = {"cur": 0, "max": 0}
    orig_get_window = catalogmod.catalog.get_window
    orig_available = catalogmod.catalog.available

    async def slow_get_window(cat, offset, limit, scope=catalogmod.AGG_SCOPE):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        try:
            await asyncio.sleep(0.3)
        finally:
            state["cur"] -= 1
        return []

    monkeypatch.setattr(catalogmod.catalog, "get_window", slow_get_window)
    monkeypatch.setattr(catalogmod.catalog, "available",
                        lambda cat, scope=catalogmod.AGG_SCOPE: True)
    try:
        t0 = time.perf_counter()
        asyncio.run(appmod._prewarm_catalog())
        elapsed = time.perf_counter() - t0
    finally:
        monkeypatch.setattr(catalogmod.catalog, "get_window", orig_get_window)
        monkeypatch.setattr(catalogmod.catalog, "available", orig_available)
    n_cats = len(catalogmod.CATALOG_CATEGORIES)
    # 串行要 n*0.3s；4 路并发约 n*0.3/4。队尾分类不再等前面慢源。
    assert elapsed < n_cats * 0.3 * 0.6, f"prewarm still serial-ish: {elapsed:.2f}s"
    assert 2 <= state["max"] <= 4, f"concurrency not bounded to 3-4: max={state['max']}"


def test_prewarm_total_budget_caps_slow_sources(monkeypatch):
    import app as appmod
    import catalog as catalogmod

    async def stuck(cat, offset, limit, scope=catalogmod.AGG_SCOPE):
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(catalogmod.catalog, "get_window", stuck)
    monkeypatch.setattr(catalogmod.catalog, "available",
                        lambda cat, scope=catalogmod.AGG_SCOPE: True)
    t0 = time.perf_counter()
    asyncio.run(appmod._prewarm_catalog(budget=1.0))
    elapsed = time.perf_counter() - t0
    # 无总预算时最坏 10 类 x 25s；有预算时 1s 预算 + 收尾内必须返回。
    assert elapsed < 5.0, f"prewarm budget not enforced: {elapsed:.2f}s"


# ---------------- 2. 探测有界并发 ----------------

class _DummyClient:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def test_refresh_routes_probe_bounded_concurrency(monkeypatch):
    import catalog as catalogmod

    cat = catalogmod.Catalog()
    sids = [f"s{i}" for i in range(6)]
    cats = ["movie", "tv", "anime", "variety", "documentary"]
    cat._classes = {
        sid: [{"type_id": j + 1, "type_name": f"类{j}",
               "type_pid": "0", "cat": c}
              for j, c in enumerate(cats)]
        for sid in sids}
    state = {"cur": 0, "max": 0}

    async def slow_probe(client, source, type_id, page, timeout=12.0):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        try:
            await asyncio.sleep(0.15)
        finally:
            state["cur"] -= 1
        return [{"vod_id": 1}], 10

    async def noop_load(self, client, source):
        return None

    monkeypatch.setattr(catalogmod, "_fetch_category_page", slow_probe)
    monkeypatch.setattr(catalogmod.Catalog, "_load_source_classes", noop_load)
    monkeypatch.setattr(catalogmod.netutil, "async_client",
                        lambda timeout=None: _DummyClient())
    monkeypatch.setattr(catalogmod.Catalog, "_source_by_id",
                        lambda self, sid: {"id": sid, "api": "http://x/api.php"})
    monkeypatch.setattr(catalogmod.Catalog, "standalone_scopes", lambda self: [])
    t0 = time.perf_counter()
    status = asyncio.run(cat.refresh_routes(
        [{"id": sid, "kind": "cms"} for sid in sids]))
    elapsed = time.perf_counter() - t0
    # 30 个 (源,类) 对：串行 30x0.15=4.5s；12 路约 0.45s；绝不超过 16 路。
    assert state["max"] <= 16, f"probe burst unbounded: max={state['max']}"
    assert state["max"] >= 2, "probe not concurrent at all"
    assert elapsed < 2.5, f"probe too slow: {elapsed:.2f}s"
    assert status["categories"]["movie"]["routes"] == 6


# ---------------- 3. 关联缓存 ----------------

def test_related_cached_hit_avoids_rescan():
    from related import (invalidate_related_cache, related_cache_stats,
                         related_for_cached)
    invalidate_related_cache()
    pool = _pool(500)
    first = related_for_cached(CUR, pool, pool_version=1)
    stats1 = related_cache_stats()
    second = related_for_cached(CUR, pool, pool_version=1)
    stats2 = related_cache_stats()
    assert [b["guid"] for b in first] == [b["guid"] for b in second]
    assert first and first[0]["guid"] == "g10"
    assert stats2["hits"] == stats1["hits"] + 1
    assert stats2["misses"] == stats1["misses"]


def test_related_cached_invalidation_on_pool_version_bump():
    from related import (invalidate_related_cache, related_for_cached)
    invalidate_related_cache()
    pool = _pool(200)
    out_v1 = related_for_cached(CUR, pool, pool_version=1)
    assert "gNEW" not in [b["guid"] for b in out_v1]
    # 入库新标题（同系列、高分），池长度不变：旧版本缓存不得返回 stale 结果。
    pool[5] = _b("gNEW", "狂飙3", catalog_cat="tv", type_name="国产剧",
                 actor="张译", year="2024", score="9.9")
    stale = related_for_cached(CUR, pool, pool_version=1)
    assert "gNEW" not in [b["guid"] for b in stale]
    fresh = related_for_cached(CUR, pool, pool_version=2)
    assert fresh[0]["guid"] == "gNEW"
    # 显式失效后，旧版本号也能重新算出新标题。
    invalidate_related_cache()
    assert related_for_cached(CUR, pool, pool_version=1)[0]["guid"] == "gNEW"


def test_related_cached_brief_change_misses():
    from related import invalidate_related_cache, related_for_cached
    invalidate_related_cache()
    pool = _pool(200)
    pool.append(_b("gLE2", "流浪地球2", catalog_cat="movie",
                   type_name="科幻片", actor="", year="2023", score="9.0"))
    out1 = related_for_cached(CUR, pool, pool_version=1)
    assert out1[0]["guid"] == "g10"
    other = _b("g0", "流浪地球", actor="", director="郭帆")
    out2 = related_for_cached(other, pool, pool_version=1)
    # 不同 brief（同系列候选不同）不得命中同一缓存项。
    assert out2[0]["guid"] == "gLE2"
    assert [b["guid"] for b in out1] != [b["guid"] for b in out2]


def test_attach_related_uses_cache_and_invalidates_on_store_bump():
    import app as appmod
    from related import invalidate_related_cache, related_cache_stats
    from store import store

    invalidate_related_cache()
    pool = _pool(200)
    saved = dict(store._titles)
    try:
        store._titles = {b["guid"]: b for b in pool}
        data1: dict = {}
        appmod._attach_related(data1, CUR)
        # search_item 会给标题加【在线】前缀，只断言包含片名。
        assert "狂飙2" in data1["recommendations"][0]["title"]
        hits_before = related_cache_stats()["hits"]
        data2: dict = {}
        appmod._attach_related(data2, CUR)
        assert related_cache_stats()["hits"] == hits_before + 1
        assert [r["title"] for r in data1["recommendations"]] == \
               [r["title"] for r in data2["recommendations"]]
        # 入库新标题 + 版本号 +1：下一次详情必须看到新关联，不得 stale。
        store._titles["gNEW"] = _b(
            "gNEW", "狂飙3", catalog_cat="tv", type_name="国产剧",
            actor="张译", year="2024", score="9.9")
        store.bump_titles_version()
        data3: dict = {}
        appmod._attach_related(data3, CUR)
        assert "狂飙3" in data3["recommendations"][0]["title"]
    finally:
        store._titles = saved
        invalidate_related_cache()


# ---------------- 4. standalone_window merged 缓存 ----------------

def _standalone_cat():
    import catalog as catalogmod
    cat = catalogmod.Catalog()
    calls = {"n": 0}

    async def fake_get_page(cat_, page, scope=catalogmod.AGG_SCOPE):
        calls["n"] += 1
        return [_b(f"{cat_}-{page}-{i}", f"{cat_}片{page}-{i}",
                   catalog_cat=cat_) for i in range(20)]

    cat.get_page = fake_get_page
    scope = "src1"
    for c in catalogmod.CATS[:4]:
        cat._routes[catalogmod.Catalog._scope_key(scope, c)] = [
            {"source_id": scope, "type_id": 1, "type_name": "x",
             "pagecount": 250}]
    cat._source_by_id = lambda sid: {"id": sid}
    return cat, scope, calls


def test_standalone_window_cached_within_merged_range():
    import catalog as catalogmod
    cat, scope, calls = _standalone_cat()
    rows1, _ = asyncio.run(cat.standalone_window(scope, 0, 22))
    assert len(rows1) == 22
    first_calls = calls["n"]
    assert first_calls > 0
    # 同一 merged 覆盖范围内的深翻页：直接切片，不再重建 O(offset) 合并。
    rows2, _ = asyncio.run(cat.standalone_window(scope, 22, 22))
    assert calls["n"] == first_calls
    assert len(rows2) == 22
    assert [b["guid"] for b in rows2] != [b["guid"] for b in rows1]
    rows3, _ = asyncio.run(cat.standalone_window(scope, 0, 22))
    assert calls["n"] == first_calls
    assert [b["guid"] for b in rows3] == [b["guid"] for b in rows1]


def test_standalone_window_cache_invalidated_by_reset_and_route_refresh():
    import catalog as catalogmod
    cat, scope, calls = _standalone_cat()
    asyncio.run(cat.standalone_window(scope, 0, 22))
    n0 = calls["n"]
    cat.reset()
    # reset() 同时清路由：重新挂路由再测，缓存失效与否只看是否重新抓取。
    for c in catalogmod.CATS[:4]:
        cat._routes[catalogmod.Catalog._scope_key(scope, c)] = [
            {"source_id": scope, "type_id": 1, "type_name": "x",
             "pagecount": 250}]
    asyncio.run(cat.standalone_window(scope, 0, 22))
    assert calls["n"] > n0, "reset() must invalidate standalone merged cache"
    # 路由刷新（_updated 变化）同样使缓存失效。
    asyncio.run(cat.standalone_window(scope, 22, 22))
    n1 = calls["n"]
    cat._updated = time.time() + 1000
    asyncio.run(cat.standalone_window(scope, 22, 22))
    assert calls["n"] > n1, "route refresh must invalidate standalone cache"
