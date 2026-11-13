"""浏览时探活（解冻第 4 项）：列表先返回，探活在后台异步跑。

覆盖：
- 列表调度只创建后台任务、不阻塞返回；
- 探活结果只写 linepredict 记忆，不写最快线记忆；
- 探活失败不影响列表、不标坏线；
- 候选上限（每片线路数 / 每页影片数）；
- 已有够快预测的片跳过；
- 总预算超时即停、在途标记清理；
- 开关默认开、关掉恢复旧行为（含配置白名单）；
- handler 级：榜单 item/list 返回不等待探活，事后记忆写入。
"""
import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import app as appmod
import config as configmod
import linepredict
import providers
import store as storemod
from config import config


@pytest.fixture(autouse=True)
def _clean():
    linepredict.reset()
    linepredict.set_persist(False)
    appmod._LINE_FAST.clear()
    appmod._LINE_BAD.clear()
    getattr(appmod, "_BROWSE_PROBE_TASKS", set()).clear()
    yield
    linepredict.reset()
    linepredict.set_persist(False)
    appmod._LINE_FAST.clear()
    appmod._LINE_BAD.clear()
    getattr(appmod, "_BROWSE_PROBE_TASKS", set()).clear()


def _cfg_with(monkeypatch, **overrides):
    """在真实配置上叠加指定键（其余键保持原值），测试结束自动恢复。"""
    real_get = appmod.config.get

    def _get():
        d = real_get()
        d.update(overrides)
        return d

    monkeypatch.setattr(appmod.config, "get", _get)


def _fake_store(monkeypatch, ogs):
    monkeypatch.setattr(appmod.store, "get_title_by_guid",
                        lambda g: {"guid": g} if g in ogs else None)


def _fake_candidates(monkeypatch, lines, bad=()):
    def _cands(picked):
        return [{"line": ln,
                 "url": f"http://127.0.0.1:9/{ln}.m3u8",
                 "online_guid": picked.get("online_guid"), "index": 0,
                 "title": "测试片", "headers": {}}
                for ln in lines]

    monkeypatch.setattr(appmod, "_episode_candidates", _cands)
    monkeypatch.setattr(appmod, "_line_is_bad",
                        lambda c: c.get("line") in bad)


def _fake_probe(monkeypatch, latencies, calls, delay=0.0):
    async def _probe(cand, request, build_hls=False):
        calls.append(cand.get("line"))
        if delay:
            await asyncio.sleep(delay)
        return latencies.get(cand.get("line"))

    monkeypatch.setattr(appmod, "_probe_latency", _probe)


def _run_schedule_and_wait(rows, timeout=5.0):
    """在事件循环内调度一次探活，并等后台任务跑完；返回后台任务。"""
    holder = {}

    async def main():
        appmod._schedule_browse_probe(rows, None)
        tasks = list(appmod._BROWSE_PROBE_TASKS)
        assert len(tasks) == 1, "调度后应恰好有一个后台探活任务"
        holder["task"] = tasks[0]
        await asyncio.wait_for(asyncio.shield(tasks[0]), timeout=timeout)

    asyncio.run(main())
    return holder["task"]


# ---------------- 调度：不阻塞 ----------------

def test_schedule_returns_immediately_and_probes_in_background(monkeypatch):
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["A线", "B线"])
    calls = []
    _fake_probe(monkeypatch, {"A线": 0.5, "B线": 0.2}, calls, delay=0.3)

    async def main():
        appmod._schedule_browse_probe([{"guid": "og1", "title": "测试片"}], None)
        # 列表 handler 视角：调度瞬间返回，记忆尚未写入（探活还在后台跑）
        assert linepredict.count() == 0
        assert len(appmod._BROWSE_PROBE_TASKS) == 1
        await asyncio.sleep(1.0)

    asyncio.run(main())
    assert calls == ["A线", "B线"]
    # 延迟最低的线写入记忆
    assert linepredict.fastest("og1|0") == "B线"
    assert linepredict.latency("og1|0") == 0.2
    # 后台任务做完即从跟踪集合里摘掉，异常已被取回（无悬空 future）
    assert appmod._BROWSE_PROBE_TASKS == set()


def test_probe_result_writes_prediction_only(monkeypatch):
    """探活结果只写 linepredict：最快线记忆 _LINE_FAST 绝不动。"""
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["A线"])
    _fake_probe(monkeypatch, {"A线": 0.3}, [])
    _run_schedule_and_wait([{"guid": "og1"}])
    assert linepredict.fastest("og1|0") == "A线"
    assert appmod._LINE_FAST == {}
    assert appmod.remembered_fast_line({"online_guid": "og1", "index": 0}) is None


# ---------------- 失败：不影响列表、不标坏线 ----------------

def test_probe_failure_never_marks_line_bad(monkeypatch):
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["A线", "B线"])
    calls = []
    _fake_probe(monkeypatch, {}, calls)  # 全部探测失败（返回 None）
    _run_schedule_and_wait([{"guid": "og1"}])
    assert calls == ["A线", "B线"]
    assert linepredict.count() == 0
    assert appmod._LINE_BAD == {}
    assert not appmod._line_is_bad(
        {"online_guid": "og1", "line": "A线", "index": 0})


def test_probe_exception_does_not_break_schedule(monkeypatch):
    """探测抛异常也只记日志：调度本身不抛，任务正常结束。"""
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["A线"])

    async def _boom(cand, request, build_hls=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(appmod, "_probe_latency", _boom)
    task = _run_schedule_and_wait([{"guid": "og1"}])
    assert task.done() and task.exception() is None
    assert linepredict.count() == 0
    assert appmod._LINE_BAD == {}


# ---------------- 上限：每片线路数 / 每页影片数 ----------------

def test_candidate_cap_respects_max_lines(monkeypatch):
    _cfg_with(monkeypatch, linepredict_max_lines=2)
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["L1", "L2", "L3", "L4", "L5"])
    calls = []
    _fake_probe(monkeypatch, {f"L{i}": 0.1 * i for i in range(1, 6)}, calls)
    _run_schedule_and_wait([{"guid": "og1"}])
    assert calls == ["L1", "L2"]


def test_title_cap_probes_first_screen_only(monkeypatch):
    _fake_store(monkeypatch, {f"og{i}" for i in range(10)})
    _fake_candidates(monkeypatch, ["A线"])
    calls = []
    _fake_probe(monkeypatch, {"A线": 0.2}, calls)
    rows = [{"guid": f"og{i}"} for i in range(10)]
    _run_schedule_and_wait(rows)
    assert len(calls) == appmod._BROWSE_PROBE_MAX_TITLES
    assert linepredict.count() == appmod._BROWSE_PROBE_MAX_TITLES


def test_ready_titles_skipped(monkeypatch):
    """已有够快预测的片不再探，只探没记忆的片。"""
    linepredict.remember("og1|0", "快线", 0.3)
    _fake_store(monkeypatch, {"og1", "og2"})
    _fake_candidates(monkeypatch, ["A线"])
    calls = []
    _fake_probe(monkeypatch, {"A线": 0.2}, calls)
    _run_schedule_and_wait([{"guid": "og1"}, {"guid": "og2"}])
    assert calls == ["A线"]  # 只探了 og2
    assert linepredict.fastest("og1|0") == "快线"
    assert linepredict.fastest("og2|0") == "A线"


def test_folder_guid_rows_resolve_to_title(monkeypatch):
    """目录卡 guid（一级影片文件夹）能还原回标题 og 再探活。"""
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["A线"])
    _fake_probe(monkeypatch, {"A线": 0.2}, [])
    folder_guid = appmod._line_folder_guid("og1")
    _run_schedule_and_wait([{"guid": folder_guid}])
    assert linepredict.fastest("og1|0") == "A线"


def test_unbound_rows_skipped(monkeypatch):
    """不在 store 里的行（榜单未绑定片、本地库行）直接跳过，不探。"""
    _fake_store(monkeypatch, set())
    _fake_candidates(monkeypatch, ["A线"])
    calls = []
    _fake_probe(monkeypatch, {"A线": 0.2}, calls)

    async def main():
        appmod._schedule_browse_probe([{"guid": "chart-x"}], None)
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert calls == []
    assert appmod._BROWSE_PROBE_TASKS == set()


# ---------------- 总预算：超时即停 ----------------

def test_budget_caps_total_probe_time(monkeypatch):
    monkeypatch.setattr(appmod, "_BROWSE_PROBE_BUDGET", 0.3)
    _fake_store(monkeypatch, {"og1", "og2"})
    _fake_candidates(monkeypatch, ["A线", "B线"])
    calls = []
    _fake_probe(monkeypatch, {"A线": 0.2, "B线": 0.2}, calls, delay=5.0)

    async def main():
        t0 = time.monotonic()
        appmod._schedule_browse_probe(
            [{"guid": "og1"}, {"guid": "og2"}], None)
        task = next(iter(appmod._BROWSE_PROBE_TASKS))
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        return time.monotonic() - t0

    elapsed = asyncio.run(main())
    assert elapsed < 2.0, f"总预算 0.3s 应截断 5s 探测，实测 {elapsed:.2f}s"
    # 超时即停：无一完成，不写记忆；在途标记必须清理（不留脏标记堵住下次探活）
    assert linepredict.count() == 0
    assert linepredict.inflight_count() == 0
    assert appmod._BROWSE_PROBE_TASKS == set()


# ---------------- 开关 ----------------

def test_switch_off_restores_old_behavior(monkeypatch):
    _cfg_with(monkeypatch, browse_probe=False)
    _fake_store(monkeypatch, {"og1"})
    _fake_candidates(monkeypatch, ["A线"])
    calls = []
    _fake_probe(monkeypatch, {"A线": 0.2}, calls)

    async def main():
        appmod._schedule_browse_probe([{"guid": "og1"}], None)
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert calls == []
    assert linepredict.count() == 0
    assert appmod._BROWSE_PROBE_TASKS == set()


def test_config_default_browse_probe_on():
    assert config.get().get("browse_probe") is True


def test_config_endpoint_accepts_browse_probe(tmp_path, monkeypatch):
    """POST /v/api/ext/config 白名单含 browse_probe，管理页能改。"""
    from fastapi.testclient import TestClient

    with patch.object(configmod, "_STATE", tmp_path / "config.json"):
        fresh = configmod.Config()
        fresh.save({"sources": [], "removed_ids": []})
        with patch.object(appmod, "config", fresh):
            with TestClient(appmod.app,
                            raise_server_exceptions=False) as cli:
                r = cli.post("/v/api/ext/config",
                             json={"browse_probe": False})
                assert r.status_code == 200, r.text
                assert fresh.get().get("browse_probe") is False
                r = cli.post("/v/api/ext/config",
                             json={"browse_probe": True})
                assert r.status_code == 200, r.text
                assert fresh.get().get("browse_probe") is True


def test_linepredict_config_endpoint_accepts_browse_probe(tmp_path, monkeypatch):
    """预测设置端点同样接受 browse_probe，管理页开关与之一致。"""
    from fastapi.testclient import TestClient

    with patch.object(configmod, "_STATE", tmp_path / "config.json"):
        fresh = configmod.Config()
        fresh.save({"sources": [], "removed_ids": []})
        with patch.object(appmod, "config", fresh):
            with TestClient(appmod.app,
                            raise_server_exceptions=False) as cli:
                r = cli.post("/v/api/ext/linepredict/config",
                             json={"browse_probe": False})
                assert r.status_code == 200, r.text
                assert fresh.get().get("browse_probe") is False
                r = cli.get("/v/api/ext/linepredict")
                assert r.json()["data"]["config"]["browse_probe"] is False


# ---------------- handler 级：榜单 item/list ----------------

class _FakeChartCache:
    def __init__(self, items):
        self._items = items
        self._refreshing = True

    def items(self, cat):
        return self._items

    def stale(self, interval):
        return False


def test_item_list_chart_schedules_probe_without_blocking(tmp_path, monkeypatch):
    """榜单列表返回不等待探活：慢探测（2s）下列表仍立即返回，事后记忆写入。"""
    from fastapi.testclient import TestClient

    with patch.object(configmod, "_STATE", tmp_path / "config.json"):
        fresh_cfg = configmod.Config()
        fresh_cfg.save({"sources": [], "removed_ids": []})
        fresh_store = storemod.Store()
        try:
            src = {"id": "probe-src", "name": "Probe",
                   "api": "http://127.0.0.1:9/cms", "enabled": True}
            title = providers._map_vod({
                "vod_id": "m1", "vod_name": "探活片", "type_name": "电影",
                "vod_year": "2024",
                "vod_play_from": "A",
                "vod_play_url": "正片$http://127.0.0.1:9/media/list.m3u8"},
                src)
            og = fresh_store.remember_titles([title])[0]["guid"]
            fake_cache = _FakeChartCache(
                [{"guid": og, "title": "探活片", "cat": "tv",
                  "year": "2024"}])
            calls = []

            async def _slow_probe(cand, request, build_hls=False):
                calls.append(cand.get("line"))
                await asyncio.sleep(2.0)
                return 0.2

            with patch.object(appmod, "config", fresh_cfg), \
                    patch.object(appmod, "store", fresh_store), \
                    patch.object(appmod, "chart_cache", fake_cache), \
                    patch.object(appmod, "_probe_latency", _slow_probe):
                with TestClient(appmod.app,
                                raise_server_exceptions=False) as cli:
                    t0 = time.monotonic()
                    r = cli.post("/v/api/v1/item/list", json={
                        "ancestor_guid": appmod.CHART_LIB_GUIDS["tv"],
                        "page": 1})
                    elapsed = time.monotonic() - t0
                    assert r.status_code == 200, r.text
                    data = r.json()["data"]
                    # 榜单行是目录卡：guid 为 folder 包装，探活时再还原回标题 og
                    assert [row["guid"] for row in data["list"]] == [
                        appmod._line_folder_guid(og)]
                    # 2s 探测在后台：列表返回远快于探测完成
                    assert elapsed < 1.0, f"列表返回被探活拖慢：{elapsed:.2f}s"
                    # 探测跑完后记忆写入，且最快线记忆不动
                    # （等待必须在 TestClient 上下文内：退出会关闭 portal 循环）
                    time.sleep(3.0)
                    assert linepredict.fastest(f"{og}|0") == "A"
                    assert appmod._LINE_FAST == {}
                    assert appmod._BROWSE_PROBE_TASKS == set()
        finally:
            fresh_store.close()
