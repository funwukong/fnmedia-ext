"""探活判定回归：覆盖「探活探不全 / 误删 / 漏删」四类根因。

1. T4 源带 kind="t4" 而非 t4 布尔字段 —— 旧代码会掉进 CMS 分支用空 api 探测。
2. 非 CMS 源搜索超时 —— 旧代码一律 alive=None，站点已死却永远清不掉。
3. 运行时缺失（sidecar 未启动）—— 必须 alive=None，绝不能判死。
4. probe.start 的 limit —— 旧实现硬顶 500，站多则后半段永远探不到。
"""
import asyncio
import time
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "proxy"))

import providers  # noqa: E402
import source_probe  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def _start_and_settle(sources, **kw):
    """start 起后台 task，然后在同一循环里把它等完。"""

    async def _go():
        await source_probe.start(sources, **kw)
        for _ in range(2000):
            if not source_probe.status().get("running"):
                return
            await asyncio.sleep(0.01)

    return _run(_go())


def _drain(rounds: int = 200) -> None:
    """轮询 status 直到本轮跑完（start 的 task 属于其内部 loop，
    无法跨 loop await，只能靠状态轮询）。"""
    for _ in range(rounds):
        st = _run(source_probe.status())
        if not st.get("running"):
            return
        time.sleep(0.01)


# ---------------- 类型归一化 ----------------

def test_source_kind_t4_by_kind_field():
    """T4 源以 kind 标记，不能靠 t4 布尔字段识别。"""
    assert providers._source_kind_of({"kind": "t4", "api": "http://x"}) == "t4"


def test_source_kind_matrix():
    assert providers._source_kind_of({"spider_src": 1}) == "spider"
    assert providers._source_kind_of({"drpy": "http://a"}) == "drpy"
    assert providers._source_kind_of({"catvod": "http://b"}) == "catvod"
    assert providers._source_kind_of({"xsrc": True}) == "dduo"
    assert providers._source_kind_of({"api": "http://c"}) == "cms"
    assert providers._source_kind_of({}) == "cms"


def test_t4_goes_to_cms_probe(monkeypatch):
    """T4 源必须走 probe_source_alive，而不是被当成未知类型。"""
    seen = {}

    async def fake(src, timeout=8.0, keywords=None):
        seen["api"] = src.get("api")
        return {"ok": True, "alive": True}

    monkeypatch.setattr(providers, "probe_source_alive", fake)
    out = _run(providers.probe_any_source({"kind": "t4", "api": "http://t4.local"}))
    assert out["alive"] is True
    assert seen["api"] == "http://t4.local"


# ---------------- 运行时缺失 vs 站点失效 ----------------

def test_runtime_missing_detected():
    assert providers._is_runtime_missing(FileNotFoundError("node"))
    assert providers._is_runtime_missing(RuntimeError("sidecar not ready"))
    assert providers._is_runtime_missing(RuntimeError("Connection refused"))


def test_runtime_missing_not_judged_dead(monkeypatch):
    """侧车没起来 → alive=None，绝不能判死删源。"""
    async def boom(client, src, kw):
        raise RuntimeError("sidecar not ready")

    monkeypatch.setattr(providers, "_dispatch_search", boom)
    out = _run(providers.probe_any_source(
        {"drpy": "http://a", "id": "x"}, keywords=["a", "b"]))
    assert out["alive"] is None
    assert "运行时" in out["reason"]


def test_all_keywords_timeout_keeps_undecided(monkeypatch):
    """每个基准词都超时/连不上 → 属网络层失败，不下结论（防止误删健康源）。

    沙箱/内网 DNS 抖动、移动网络握手超时都会落到这条分支；旧逻辑判 alive=False
    会把整批真源当失效删除（实测真 CMS 源被一次 ConnectTimeout 判死）。
    """
    async def boom(client, src, kw):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(providers, "_dispatch_search", boom)
    out = _run(providers.probe_any_source(
        {"catvod": "http://a", "id": "x"}, keywords=["a", "b"]))
    assert out["alive"] is None
    assert out["count"] == 0
    assert "不下结论" in out["reason"]


def test_hard_site_error_still_marks_dead(monkeypatch):
    """站点明确应答错误（非网络层）仍判失效，不能矫枉过正。"""
    async def bad_json(client, src, kw):
        raise ValueError("JSONDecodeError: Expecting value")

    monkeypatch.setattr(providers, "_dispatch_search", bad_json)
    out = _run(providers.probe_any_source(
        {"catvod": "http://a", "id": "x"}, keywords=["a", "b"]))
    assert out["alive"] is False


def test_partial_timeout_keeps_undecided(monkeypatch):
    """部分词超时、部分词空结果 → 不下结论（防止抖动误杀）。"""
    calls = {"n": 0}

    async def flaky(client, src, kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timeout")
        return []

    monkeypatch.setattr(providers, "_dispatch_search", flaky)
    out = _run(providers.probe_any_source(
        {"catvod": "http://a", "id": "x"}, keywords=["a", "b"]))
    assert out["alive"] is None


def test_hit_marks_alive(monkeypatch):
    async def hit(client, src, kw):
        return [{"title": "x"}]

    monkeypatch.setattr(providers, "_dispatch_search", hit)
    out = _run(providers.probe_any_source(
        {"drpy": "http://a", "id": "x"}, keywords=["a"]))
    assert out["alive"] is True
    assert out["hit"] == "a"


# ---------------- limit 不再硬顶 500 ----------------

def test_start_limit_zero_means_all(monkeypatch):
    seen = {}

    async def fake_probe(src, timeout=8.0, keywords=None):
        seen.setdefault("ids", []).append(src["id"])
        return {"ok": True, "alive": True, "reason": ""}

    monkeypatch.setattr(providers, "probe_any_source", fake_probe)
    srcs = [{"id": f"s{i}"} for i in range(700)]
    _start_and_settle(srcs, limit=0, concurrency=64)
    assert len(seen.get("ids", [])) == 700


def test_start_limit_respected(monkeypatch):
    seen = {"n": 0}

    async def fake_probe(src, timeout=8.0, keywords=None):
        seen["n"] += 1
        return {"ok": True, "alive": True}

    monkeypatch.setattr(providers, "probe_any_source", fake_probe)
    _start_and_settle([{"id": f"s{i}"} for i in range(20)],
                      limit=5, concurrency=4)
    assert seen["n"] == 5
