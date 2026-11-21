"""分类作品去重及 reset/分页共享任务竞态回归（不访问网络）。"""
import asyncio

import pytest

import catalog as mod
from catalog import Catalog


def brief(guid, title="同一作品", year="2024", kind="电影"):
    return {"guid": guid, "title": title, "year": year, "type_name": kind,
            "lines": {guid: [{"url": "https://example.test/" + guid}]}}


@pytest.fixture
def cat(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "ROUTE_STATE", tmp_path / "routes.json")
    obj = Catalog()
    obj._routes[obj._scope_key(mod.AGG_SCOPE, "movie")] = [{"pagecount": 2}]
    return obj


def test_dense_cross_source_dedup_preserves_playable_identity(cat):
    first, duplicate = brief("source-a"), brief("source-b")
    remake = brief("remake", year="1999")
    series = brief("series", kind="电视剧")
    unknown1, unknown2 = brief("unknown-a", year=""), brief("unknown-b", year="未知")
    season = brief("season", title="同一作品 第二季")

    async def pages(scope, category, batch):
        data = {1: [first, remake, unknown1],
                2: [duplicate, series, unknown2, season, first]}
        return [(p, data[p]) for p in batch]

    cat._fetch_pages = pages
    result = asyncio.run(cat.get_window("movie", 0, 100))
    assert [b["guid"] for b in result] == ["source-a", "remake", "unknown-a", "series", "unknown-b", "season"]
    assert result[0] is first
    assert result[0]["lines"] == {"source-a": [{"url": "https://example.test/source-a"}]}
    assert duplicate["guid"] == "source-b"  # 不改写来源记录及线路


def test_reset_discards_dense_batch_and_cursor(cat):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def pages(*args):
            entered.set()
            await release.wait()
            return [(1, [brief("old")])]

        cat._fetch_pages = pages
        task = asyncio.create_task(cat.get_window("movie", 0, 1))
        await entered.wait()
        cat.reset()
        release.set()
        assert await task == []
        assert cat._dense == cat._seen_guids == cat._scanned_page == cat._dense_count == {}

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_old_page_cannot_publish_or_remove_new_inflight(cat):
    async def scenario():
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def load(*args):
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            return [brief("old" if index == 0 else "new")]

        cat._load_page = load
        old = asyncio.create_task(cat.get_page("movie", 1))
        await entered[0].wait()
        cat.reset()
        new = asyncio.create_task(cat.get_page("movie", 1))
        await entered[1].wait()
        key = (mod.AGG_SCOPE, "movie", 1)
        new_future = cat._inflight[key]
        release[0].set()
        assert await old == []
        assert not cat._page_cache
        assert cat._inflight[key] is new_future
        release[1].set()
        assert (await new)[0]["guid"] == "new"
        assert cat._page_cache[key][1][0]["guid"] == "new"
        assert not cat._inflight

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_cancelling_one_page_waiter_does_not_cancel_shared_work(cat):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def load(*args):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return [brief("ok")]

        cat._load_page = load
        first = asyncio.create_task(cat.get_page("movie", 1))
        await entered.wait()
        second = asyncio.create_task(cat.get_page("movie", 1))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not next(iter(cat._inflight.values())).cancelled()
        release.set()
        assert (await second)[0]["guid"] == "ok"
        assert calls == 1

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_reset_discards_standalone_merge(cat, monkeypatch):
    monkeypatch.setattr(cat, "_source_by_id", lambda scope: {})
    monkeypatch.setattr(cat, "available", lambda category, scope: category == "movie")

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def window(*args):
            entered.set()
            await release.wait()
            return [brief("old")]

        cat._source_cat_window = window
        task = asyncio.create_task(cat.standalone_window("source", 0, 10))
        await entered.wait()
        cat.reset()
        release.set()
        assert await task == ([], 0)
        assert not cat._standalone_cache

    asyncio.run(asyncio.wait_for(scenario(), 3))


@pytest.mark.parametrize("scope", [mod.AGG_SCOPE, "source"])
def test_reset_stops_real_loader_before_mapping_and_store(cat, monkeypatch, scope):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def client(**kwargs):
        yield object()

    monkeypatch.setattr(mod.netutil, "async_client", client)
    monkeypatch.setattr(cat, "_source_by_id", lambda sid: {"id": sid, "api": "test"})
    cat._routes[cat._scope_key(scope, "movie")] = [
        {"source_id": "source", "type_id": i} for i in (1, 2)]
    mapped = []
    monkeypatch.setattr(cat, "_briefs_from_vods", lambda *args: mapped.append(args))

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args, **kwargs):
            entered.set()
            await release.wait()
            return [{"vod_name": "old 4K"}], 1

        monkeypatch.setattr(mod, "_fetch_category_page", fetch)
        task = asyncio.create_task(cat.get_page("movie", 1, scope))
        await entered.wait()
        cat.reset()
        release.set()
        assert await task == []
        assert not mapped
        assert not cat._dense
        assert not cat._page_cache

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_standalone_category_deduplicates_source_ids(cat):
    async def pages(scope, category, batch):
        return [(p, [brief("a"), brief("b")] if p == 1 else []) for p in batch]

    cat._fetch_pages = pages
    result = asyncio.run(cat._source_cat_window("source", "movie", 0, 10))
    assert [b["guid"] for b in result] == ["a"]
