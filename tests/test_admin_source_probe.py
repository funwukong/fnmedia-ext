"""管理台资源站测试接口回归。"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "proxy"))

import admin_api  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_test_source_runtime_missing_is_undecided(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("sidecar not ready")

    monkeypatch.setattr(admin_api.providers, "probe_source", boom)
    out = run(admin_api._test_source_one({
        "id": "cms-1", "name": "CMS", "api": "https://example.test",
        "kind": "cms", "enabled": True,
    }))
    assert out["alive"] is None
    assert out["state"] == "runtime-missing"
    assert "未参与判定" in out["reason"]


def test_test_source_exception_does_not_escape(monkeypatch):
    async def boom(*args, **kwargs):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(admin_api.providers, "probe_any_source", boom)
    out = run(admin_api._test_source_one({
        "id": "drpy-1", "name": "DRPY", "drpy": "https://example.test",
        "kind": "drpy", "enabled": True,
    }))
    assert out["alive"] is False
    assert out["state"] == "dead"
    assert "未影响源配置" in out["reason"]


def test_test_source_routes_non_cms_to_any_probe(monkeypatch):
    calls = []

    async def any_probe(source, **kwargs):
        calls.append((source["id"], kwargs["keywords"]))
        return {"ok": True, "alive": True, "count": 3, "elapsed_ms": 7}

    monkeypatch.setattr(admin_api.providers, "probe_any_source", any_probe)
    out = run(admin_api._test_source_one({
        "id": "cat-1", "name": "CatVod", "catvod": "https://example.test",
        "kind": "catvod", "enabled": True,
    }, keyword="测试词"))
    assert out["state"] == "alive"
    assert calls == [("cat-1", ("测试词",))]


def test_test_source_normalizes_adapter_error_for_ui(monkeypatch):
    async def adapter(*args, **kwargs):
        return {"ok": False, "error": "RuntimeError"}

    monkeypatch.setattr(admin_api.providers, "probe_source", adapter)
    out = run(admin_api._test_source_one({
        "id": "cms-err", "name": "CMS", "api": "https://example.test",
        "kind": "cms", "enabled": True,
    }))
    assert out["alive"] is None
    assert out["state"] == "unknown"
    assert out["reason"] == "探测未完成：RuntimeError"


@pytest.mark.parametrize("alive,state", [(True, "alive"), (False, "dead"), (None, "unknown")])
def test_probe_preserves_tristate_alive(monkeypatch, alive, state):
    import source_probe

    async def fake_probe(source):
        return {"ok": True, "alive": alive, "count": 0, "elapsed_ms": 1}

    monkeypatch.setattr(source_probe, "_probe_impl", fake_probe)
    async def go():
        await source_probe.start([{"id": "s", "name": "S", "api": "https://example.test"}],
                                 concurrency=1)
        return await source_probe.wait()

    try:
        result = run(go())
        row = result["results"][0]
        assert row["alive"] is alive
        assert row["state"] == state
    finally:
        source_probe._probe_impl = None
        source_probe.cancel()


def test_test_source_timeout_is_normalized(monkeypatch):
    async def slow(*args, **kwargs):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(admin_api.providers, "probe_source", slow)
    out = asyncio.run(admin_api._test_source_one({
        "id": "cms-timeout", "name": "CMS", "api": "https://example.test",
        "kind": "cms", "enabled": True,
    }))
    assert out["alive"] is False
    assert out["stage"] == "timeout"
    assert "RuntimeError" not in out["reason"]


def test_test_all_does_not_leak_runtime_error(monkeypatch):
    import app

    async def boom(*args, **kwargs):
        raise RuntimeError("raw boom")

    monkeypatch.setattr(admin_api.providers, "probe_any_source", boom)
    monkeypatch.setattr(app.config, "all_sources", lambda: [
        {"id": "s1", "name": "S1", "api": "https://example.test", "kind": "drpy", "enabled": True},
        {"id": "s2", "name": "S2", "api": "https://example.test", "kind": "drpy", "enabled": True},
    ])

    class FakeRequest:
        async def json(self):
            return {}

    out = asyncio.run(admin_api.ext_source_test_all(FakeRequest()))
    rows = out["data"]
    assert len(rows) == 2
    assert all(r["alive"] is False for r in rows)
    assert all(r["stage"] in ("runtime", "network") for r in rows)
    assert all("RuntimeError" not in ((r.get("reason") or "") + (r.get("error") or "")) for r in rows)


def test_probe_timeout_reports_stage(monkeypatch):
    import source_probe

    async def slow(source):
        await asyncio.sleep(9)
        return {"ok": True, "alive": True}

    monkeypatch.setattr(source_probe, "_probe_impl", slow)
    async def go():
        await source_probe.start([{"id": "s", "name": "S", "api": "https://example.test"}],
                                 concurrency=1)
        return await source_probe.wait()

    try:
        result = asyncio.run(go())
        row = result["results"][0]
        assert row["alive"] is None
        assert row["state"] == "unknown"
        assert row["stage"] == "timeout"
    finally:
        source_probe._probe_impl = None
        source_probe.cancel()
