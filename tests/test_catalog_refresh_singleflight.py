"""Catalog route refresh single-flight regression tests."""
import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog as catalog_mod


def test_refresh_routes_reuses_inflight_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_mod, "ROUTE_STATE", tmp_path / "routes.json")
    cat = catalog_mod.Catalog()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_load(client, source):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        cat._classes[source["id"]] = []

    monkeypatch.setattr(cat, "_load_source_classes", fake_load)

    async def run():
        first = asyncio.create_task(cat.refresh_routes([{"id": "cms", "api": "https://x/api"}]))
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(cat.refresh_routes([{"id": "cms", "api": "https://x/api"}]))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(asyncio.wait_for(run(), 5))
    assert calls == 1


def _stub_refresh(monkeypatch, cat, *, entered=None, release=None):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def client(**kwargs):
        yield object()

    monkeypatch.setattr(catalog_mod.netutil, "async_client", client)

    async def load(_client, source):
        if entered:
            entered.set()
        if release:
            await release.wait()
        cat._classes[source["id"]] = [{"type_id": 1, "type_name": "电影", "cat": "movie"}]

    monkeypatch.setattr(cat, "_load_source_classes", load)
    monkeypatch.setattr(catalog_mod, "_fetch_category_page",
                        lambda *args, **kwargs: None)


def test_memory_cache_hit_then_explicit_refresh_and_config_change(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_mod, "ROUTE_STATE", tmp_path / "routes.json")
    cat = catalog_mod.Catalog()
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()
    _stub_refresh(monkeypatch, cat, entered=entered, release=release)
    # Keep probing deterministic and network-free.
    from unittest.mock import AsyncMock
    monkeypatch.setattr(catalog_mod, "_fetch_category_page",
                        AsyncMock(return_value=([{"vod_id": 1}], 1)))

    async def run():
        src = [{"id": "cms", "api": "https://x/api"}]
        await cat.refresh_routes(src)
        assert cat._routes
        # A cache hit is valid only for the original signature. A changed API
        # must perform a fresh category load even though old routes are present.
        entered.clear()
        await cat.refresh_routes([{"id": "cms", "api": "https://y/api"}])
        assert entered.is_set()
        assert cat._route_state_signature == cat._route_signature(
            [{"id": "cms", "api": "https://y/api"}])

    asyncio.run(asyncio.wait_for(run(), 5))


def test_reset_during_disk_read_does_not_restore_old_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_mod, "ROUTE_STATE", tmp_path / "routes.json")
    cat = catalog_mod.Catalog()
    entered, release = threading.Event(), threading.Event()
    state = {"schema": catalog_mod.CATALOG_SCHEMA_VERSION,
             "signature": cat._route_signature([]), "updated": 1,
             "classes": {}, "routes": {"__agg__:movie": [{"type_id": 1}]}}
    def read():
        entered.set()
        assert release.wait(3)
        return state
    monkeypatch.setattr(cat, "_read_route_state_file", read)

    async def run():
        task = asyncio.create_task(cat._load_route_state([]))
        assert await asyncio.to_thread(entered.wait, 3)
        cat.reset()
        release.set()
        assert not await asyncio.wait_for(task, 3)
        assert cat._routes == {}

    asyncio.run(asyncio.wait_for(run(), 5))


def test_reset_during_disk_write_does_not_resurrect_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_mod, "ROUTE_STATE", tmp_path / "routes.json")
    cat = catalog_mod.Catalog()
    entered, release = threading.Event(), threading.Event()
    started = threading.Event()
    def write(data):
        started.set()
        assert release.wait(3)
        catalog_mod.Catalog._write_route_state(data)
    monkeypatch.setattr(cat, "_write_route_state", write)
    cat._routes["__agg__:movie"] = [{"type_id": 1}]

    async def run():
        task = asyncio.create_task(cat._persist_route_state([], cat._cache_generation))
        assert await asyncio.to_thread(started.wait, 3)
        reset = asyncio.create_task(asyncio.to_thread(cat.reset))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(task, 3)
        await asyncio.wait_for(reset, 3)
        assert not catalog_mod.ROUTE_STATE.exists()

    asyncio.run(asyncio.wait_for(run(), 5))
