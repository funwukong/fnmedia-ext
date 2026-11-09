"""上游 v0.2.68/69 移植：linepredict + chartbind。

覆盖：模块行为、配置默认值、管理端点注册、chartbind 持久化往返、
以及冻结清单——竞速 first-success wins / 吞吐打分 / 最快线记忆三项未变，
预测线只作竞速优先种子，持久化绑定只作起播种子。
"""
import asyncio
import json
import time

import pytest

import chartbind
import linepredict
import app as appmod
from config import config


@pytest.fixture(autouse=True)
def _clean_linepredict():
    linepredict.reset()
    linepredict.set_persist(False)
    appmod._LINE_FAST.clear()
    yield
    linepredict.reset()
    linepredict.set_persist(False)
    appmod._LINE_FAST.clear()


# ---------------- linepredict 模块 ----------------

def test_linepredict_remember_fastest_grade():
    linepredict.remember("og|0", "量子线", 0.3)
    assert linepredict.fastest("og|0") == "量子线"
    assert linepredict.grade("og|0") == 1
    assert linepredict.latency("og|0") == 0.3
    assert linepredict.count() == 1
    assert linepredict.ready_or_reprobe("og|0") is True
    snap = linepredict.snapshot(10)
    assert snap and snap[0]["line"] == "量子线" and snap[0]["og"] == "og"


def test_linepredict_keeps_faster_line():
    linepredict.remember("og|0", "慢线", 2.0)
    linepredict.remember("og|0", "快线", 0.4)
    assert linepredict.fastest("og|0") == "快线"
    # 更慢的结果不能覆盖已验证的快线
    linepredict.remember("og|0", "更慢线", 2.5)
    assert linepredict.fastest("og|0") == "快线"


def test_linepredict_expired_entry_dropped():
    linepredict.remember("og|0", "量子线", 0.3)
    with linepredict._LOCK:
        ts, line, lat = linepredict._FAST["og|0"]
        linepredict._FAST["og|0"] = (ts - linepredict._TTL - 1, line, lat)
    assert linepredict.fastest("og|0") is None
    assert linepredict.count() == 0


def test_linepredict_inflight_dedup():
    assert linepredict.try_mark_inflight("og|0") is True
    assert linepredict.try_mark_inflight("og|0") is False
    assert linepredict.inflight_count() == 1
    linepredict.clear_inflight("og|0")
    assert linepredict.inflight_count() == 0


def test_linepredict_persist_roundtrip(tmp_path, monkeypatch):
    state_file = tmp_path / "linepredict_fast.json"
    monkeypatch.setattr(linepredict, "_STATE", state_file)
    linepredict.set_persist(True)
    linepredict.remember("og|0", "量子线", 0.3)
    linepredict._persist_now()
    assert state_file.exists()
    # 注意：reset() 在 persist 开启时会把空快照落盘，先关 persist 再清内存
    linepredict.set_persist(False)
    linepredict.reset()
    assert linepredict.fastest("og|0") is None
    assert linepredict.load_persisted() == 1
    assert linepredict.fastest("og|0") == "量子线"


# ---------------- chartbind 模块 ----------------

def _fresh_bind_store(tmp_path, monkeypatch):
    monkeypatch.setattr(chartbind, "STATE", tmp_path / "chart_bindings.json")
    return chartbind.ChartBindStore()


def test_chartbind_remember_get_status(tmp_path, monkeypatch):
    st = _fresh_bind_store(tmp_path, monkeypatch)
    brief = {"title": "测试片", "lines": {"量子线": []}}
    st.remember("abc123", brief)
    assert st.get("abc123") == brief
    assert st.is_fresh("abc123") is True
    assert st.has("abc123") is True
    assert st.count() == 1
    assert st.status()["entries"] == 1
    st.forget("abc123")
    assert st.get("abc123") is None


def test_chartbind_persist_roundtrip(tmp_path, monkeypatch):
    st = _fresh_bind_store(tmp_path, monkeypatch)
    st.remember("abc123", {"title": "测试片"})
    st.flush()
    st2 = chartbind.ChartBindStore()
    assert st2.get("abc123") == {"title": "测试片"}
    assert st2.is_fresh("abc123") is True
    st2.clear()
    assert st2.count() == 0
    assert not (tmp_path / "chart_bindings.json").exists()


def test_chartbind_stale_after_ttl(tmp_path, monkeypatch):
    st = _fresh_bind_store(tmp_path, monkeypatch)
    st.remember("abc123", {"title": "测试片"})
    with st._lock:
        st._data["abc123"]["ts"] = time.time() - chartbind.BIND_TTL - 1
    assert st.is_fresh("abc123") is False
    assert st.get("abc123") == {"title": "测试片"}  # 过期仍恢复，由播放链懒校验
    assert st.status()["stale"] == 1


# ---------------- 配置默认值 ----------------

def test_config_defaults():
    cfg = config.get()
    assert cfg.get("linepredict_enabled") is True
    assert cfg.get("linepredict_max_lines") == 3
    assert cfg.get("linepredict_ttl_min") == 30
    assert cfg.get("prewarm_mode") == "realtime"


# ---------------- 管理端点 ----------------

def test_linepredict_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ("/v/api/ext/linepredict", "/v/api/ext/linepredict/config",
              "/v/api/ext/linepredict/reset"):
        assert p in paths, f"missing route {p}"


def test_linepredict_endpoints():
    from fastapi.testclient import TestClient
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        linepredict.remember("og|0", "量子线", 0.3)
        r = cli.get("/v/api/ext/linepredict")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["remembered"] == 1
        assert data["config"]["enabled"] is True
        assert data["config"]["prewarm_mode"] == "realtime"
        assert "chart_bindings" in data["config"]
        r = cli.post("/v/api/ext/linepredict/reset")
        assert r.status_code == 200
        assert linepredict.count() == 0


# ---------------- chartbind 接入 ----------------

def test_persist_chart_binding_only_in_persist_mode(tmp_path, monkeypatch):
    fresh = _fresh_bind_store(tmp_path, monkeypatch)
    monkeypatch.setattr(chartbind, "bind_store", fresh)
    monkeypatch.setattr(appmod, "prewarm_persist_mode", lambda: False)
    appmod._persist_chart_binding("abc123", {"title": "测试片"})
    assert fresh.get("abc123") is None
    monkeypatch.setattr(appmod, "prewarm_persist_mode", lambda: True)
    appmod._persist_chart_binding("abc123", {"title": "测试片"})
    assert fresh.get("abc123") == {"title": "测试片"}


def test_chart_title_rank():
    assert appmod._chart_title_rank({"title": "测试片", "year": "2024"},
                                    {"title": "测试片", "year": "2024"}) == 0
    assert appmod._chart_title_rank({"title": "测试片", "year": ""},
                                    {"title": "测试片高清", "year": ""}) == 1
    assert appmod._chart_title_rank({"title": "测试片", "year": "2024"},
                                    {"title": "测试片", "year": "2023"}) == 99
    assert appmod._chart_title_rank({"title": "突如其来", "year": ""},
                                    {"title": "突如其来的假期", "year": ""}) == 99


def test_restore_chart_bindings(tmp_path, monkeypatch):
    fresh = _fresh_bind_store(tmp_path, monkeypatch)
    monkeypatch.setattr(chartbind, "bind_store", fresh)
    fresh.remember("abc123", {"title": "测试片"})
    monkeypatch.setattr(appmod, "prewarm_persist_mode", lambda: True)
    monkeypatch.setattr(appmod.chartmod, "CHART_CATEGORIES",
                        [("movie", "电影", "mdb")])

    class _FakeCache:
        def items(self, cat):
            return [{"guid": "abc123", "title": "测试片", "year": ""}]

        def get_guid(self, guid):
            return {"guid": "abc123", "title": "测试片", "year": ""} \
                if guid == "abc123" else None

    monkeypatch.setattr(appmod, "chart_cache", _FakeCache())
    monkeypatch.setattr(appmod.store, "get_title_by_guid", lambda g: None)
    seen = []
    monkeypatch.setattr(appmod.store, "remember_merged",
                        lambda items: seen.extend(items))
    assert appmod._restore_chart_bindings() == 1
    assert seen and seen[0]["guid"] == "abc123"


# ---------------- 冻结清单：竞速 / 吞吐打分 / 最快线记忆 ----------------

def test_prediction_never_writes_fastest_memory():
    """预测结果不能覆盖/改写最快线记忆：只记 linepredict，_LINE_FAST 不动。"""
    picked = {"online_guid": "og", "index": 0}
    assert appmod.remembered_fast_line(picked) is None
    linepredict.remember("og|0", "预测线", 0.2)
    assert appmod.remembered_fast_line(picked) is None
    assert appmod._LINE_FAST == {}


def test_remember_fast_writes_both_but_prediction_never_writes_back():
    """竞速胜者写 _LINE_FAST（实测为准），并单向同步进预测缓存作种子。"""
    picked = {"online_guid": "og", "index": 0}
    appmod._remember_fast(picked, "竞速胜者", 0.35)
    assert appmod.remembered_fast_line(picked) == "竞速胜者"
    assert linepredict.fastest("og|0") == "竞速胜者"
    # 反向无效：改预测缓存不影响最快线记忆
    linepredict.remember("og|0", "另一条线", 0.1)
    assert appmod.remembered_fast_line(picked) == "竞速胜者"


def _race_setup(monkeypatch, scores):
    picked = {"online_guid": "og", "index": 0}
    cands = [{"line": name} for name in scores]
    monkeypatch.setattr(appmod, "_episode_candidates", lambda p: list(cands))
    monkeypatch.setattr(appmod, "_line_is_bad", lambda c: False)
    monkeypatch.setattr(appmod.store, "best_line", lambda og: "默认线")
    monkeypatch.setattr(appmod, "_line_score", lambda c: scores[c["line"]])
    return picked


def test_race_candidates_throughput_still_primary(monkeypatch):
    """吞吐打分仍是第一排序键：预测线吞吐低也不能排到高分线前面。"""
    picked = _race_setup(monkeypatch, {"预测线": 100.0, "高分线": 200.0,
                                       "默认线": 150.0})
    linepredict.remember("og|0", "预测线", 0.2)
    order = [c["line"] for c in appmod._race_candidates(picked)]
    assert order[0] == "高分线"
    assert order.index("预测线") > order.index("高分线")


def test_race_candidates_prediction_only_tiebreak(monkeypatch):
    """吞吐相同、非上次最快线时，预测线排前一位（种子作用，仅此而已）。"""
    picked = _race_setup(monkeypatch, {"预测线": 150.0, "其他线": 150.0,
                                       "默认线": 150.0})
    linepredict.remember("og|0", "预测线", 0.2)
    order = [c["line"] for c in appmod._race_candidates(picked)]
    assert order.index("预测线") < order.index("其他线")


def test_ensure_playable_still_races(monkeypatch):
    """起播仍跑竞速：即使预测缓存里有线，_ensure_playable 也必须调竞速，
    且竞速胜者（而非预测线）决定播放线路。"""
    picked = {"online_guid": "og", "index": 0, "line": "原线",
              "title": "测试片"}
    linepredict.remember("og|0", "预测线", 0.2)
    called = {}

    async def _fake_race(p, request):
        called["ran"] = True
        return {"line": "竞速胜者", "url": "http://x/y.m3u8"}

    monkeypatch.setattr(appmod, "_race_fastest", _fake_race)
    monkeypatch.setattr(appmod, "_autofail_on", lambda: True)
    monkeypatch.setattr(appmod, "_fastest_on", lambda: True)
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=True))
    assert called.get("ran") is True
    assert out["line"] == "竞速胜者"
    assert appmod.remembered_fast_line(picked) is None  # 预测没写最快线记忆
