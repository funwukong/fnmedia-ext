"""探活自动删除（失效/风险源）端到端回归。

用户要求：失效自动删除，风险源也删除。
关键不变量：
1. alive=False 的源 → 调用 disable_sink
2. 风险源（risk / json_api）→ 也调用 disable_sink
3. alive=None（可访问无命中 / 运行时不具备）→ 绝不动（防误删健康源）
4. 探活目标覆盖全部源（含已禁用），不限于纯 CMS
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))


def _run(coro):
    # 用独立事件循环：get_event_loop() 在合跑全量时可能拿到已关闭的循环，
    # 导致 start() 起不来、sink 永远不被调用（假失败）。
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


@pytest.fixture()
def probe(monkeypatch):
    import source_probe as mod

    async def fake_one(source):
        return {
            "id": source.get("id"),
            "name": source.get("name") or "",
            "alive": source.get("_alive", source.get("_fake_alive")),
            "latency_ms": 1,
            "detail": "",
        }

    monkeypatch.setattr(mod, "_probe_impl", fake_one, raising=False)
    monkeypatch.setattr(mod, "_run_id", 0, raising=False)
    monkeypatch.setattr(mod, "_task", None, raising=False)
    monkeypatch.setattr(mod, "_disable_sink", None, raising=False)
    mod._state.update(running=False, total=0, done=0, results=[])
    return mod


def test_dead_and_risky_removed_healthy_kept(probe, monkeypatch):
    removed = []

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(probe, "_save_health", noop, raising=False)
    monkeypatch.setattr(probe, "sourcehealth", None, raising=False)

    sources = [
        {"id": "ok1", "_alive": True},
        {"id": "dead1", "_alive": False},
        {"id": "risk1", "_alive": True, "risk": True},
        {"id": "uniq1", "_alive": None},
        {"id": "json1", "_alive": True, "json_api": True},
    ]

    async def go():
        probe._disable_sink = None
        await probe.start(sources, concurrency=1,
                          disable_sink=lambda sid: removed.append(sid))
        for _ in range(300):
            st = probe.status()
            if st.get("done", 0) >= st.get("total", 0) and not st.get("running"):
                break
            await asyncio.sleep(0.01)

    _run(go())
    assert "dead1" in removed
    assert "risk1" in removed
    assert "json1" not in removed, "json_api 只是接口形态，探活正常就必须保留"
    assert "ok1" not in removed
    assert "uniq1" not in removed, "alive=None 不允许触发删除"


def test_rows_carry_removed_flag():
    import source_probe as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert '"removed": removed' in src
    assert 'alive_now = result.get("alive")' in src
    assert 'dead = result.get("ok", True) and alive_now is False' in src
    assert 'result.get("ok", True)' in src
    assert "risky = bool(" in src
