"""解冻第 2 项：零竞速秒播（linepredict_zero_race）。

用户已解冻五项竞速机制中的第 2 项：有可信记忆时直接用该线起播、跳过竞速；
记忆不可信 / 缺失 / 过期时回退现有竞速。

“可信”的定义（见 hls_race.trusted_predicted_target 的注释）：
- 新鲜：在 linepredict 现有 30 分钟 TTL 内；
- 该线是本集的真实候选（_episode_candidates 里有这一集）；
- 未被标记坏线（_line_is_bad）；坏线记忆必须失效并回退竞速；
- 够快（二者居其一）：探测延迟 < linepredict.SLOW_SEC（1.5s），
  或该线实测吞吐 _line_speed >= _SPEED_GOOD（200 KB/s）。
  不用 grade() 作门：grade 只是海报角标档位（<0.8s 快 / <2.5s 中 /
  >=2.5s 慢），其中 2 档 0.8–2.5s 横跨了 1.5s 慢阈值，分不清
  “能直出”与“慢线”；延迟是底层实测值，SLOW_SEC 是模块自己
  “不够秒播”的阈值（ready_or_reprobe 判定就绪也用它）。
  吞吐口径与竞速内已知快线单发直探用同一条线。

不变的：_LINE_FAST 仍只有竞速胜者能写，预测记忆绝不反向写它；
竞速本体（_race_fastest）与四个竞速常量不动；竞速内的候选排序不变，
预测线在竞速里仍只作 tiebreak。
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as appmod
import config as config_mod
import hls_race as racemod
import linepredict


@pytest.fixture(autouse=True)
def _clean_prediction_state():
    linepredict.reset()
    linepredict.set_persist(False)
    appmod._LINE_FAST.clear()
    racemod._LINE_SPEED.clear()
    racemod._LINE_BAD.clear()
    yield
    linepredict.reset()
    linepredict.set_persist(False)
    appmod._LINE_FAST.clear()
    racemod._LINE_SPEED.clear()
    racemod._LINE_BAD.clear()


def _picked(line="原线"):
    return {"online_guid": "og", "index": 0, "line": line,
            "title": "测试片", "url": "https://x.test/m.m3u8"}


def _patch_auto(monkeypatch):
    monkeypatch.setattr(appmod, "_autofail_on", lambda: True)
    monkeypatch.setattr(appmod, "_fastest_on", lambda: True)


def _patch_candidates(monkeypatch, lines):
    cands = [{"line": name, "url": f"https://x.test/{name}.m3u8",
              "online_guid": "og", "index": 0} for name in lines]
    monkeypatch.setattr(appmod, "_episode_candidates",
                        lambda p: list(cands))
    return cands


def _patch_race(monkeypatch, winner_line="竞速胜者"):
    calls = []

    async def _fake_race(p, request):
        calls.append(p.get("line"))
        return {"line": winner_line, "url": "https://x.test/w.m3u8"}

    monkeypatch.setattr(appmod, "_race_fastest", _fake_race)
    return calls


def _patch_switch_off(monkeypatch):
    """用配置替身关掉零竞速开关（其余键保持默认开）。"""

    class _Cfg:
        def get(self):
            return {"linepredict_zero_race": False,
                    "linepredict_enabled": True}

    monkeypatch.setattr(appmod, "config", _Cfg())


# ---------------- 配置默认值与白名单 ----------------

def test_zero_race_in_defaults():
    assert config_mod.DEFAULTS["linepredict_zero_race"] is True
    assert config_mod.config.get()["linepredict_zero_race"] is True


def test_old_config_gets_default_on_load(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2, "hot_limit": 300}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["linepredict_zero_race"] is True


def _fresh_config_env(tmp_path, monkeypatch):
    cfg_state = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", cfg_state)
    cfg = config_mod.Config()
    monkeypatch.setattr(appmod, "config", cfg)
    monkeypatch.setattr(config_mod, "config", cfg)
    return cfg


def test_config_endpoint_accepts_zero_race(tmp_path, monkeypatch):
    _fresh_config_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/config",
                     json={"linepredict_zero_race": False})
        assert r.status_code == 200
        assert appmod.config.get()["linepredict_zero_race"] is False
        # 白名单外键不被接受
        r = cli.post("/v/api/ext/config",
                     json={"linepredict_zero_race": True, "bogus_key": 1})
        assert r.status_code == 200
        assert appmod.config.get()["linepredict_zero_race"] is True
        assert "bogus_key" not in appmod.config.get()


def test_linepredict_config_endpoint_accepts_zero_race(tmp_path, monkeypatch):
    _fresh_config_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/linepredict/config",
                     json={"zero_race": False})
        assert r.status_code == 200
        assert r.json()["data"]["linepredict_zero_race"] is False
        assert appmod.config.get()["linepredict_zero_race"] is False
        r = cli.get("/v/api/ext/linepredict")
        assert r.json()["data"]["config"]["zero_race"] is False


def test_admin_ui_has_zero_race_toggle():
    src = (Path(__file__).resolve().parent.parent
           / "proxy" / "admin_ui.py").read_text(encoding="utf-8")
    assert "linePredictZeroRace" in src
    assert "zero_race" in src


# ---------------- 零竞速直出 ----------------

def test_trusted_memory_skips_race(monkeypatch):
    """记忆可信（延迟 0.2s < 1.5s）时零竞速起播，竞速一次也不跑。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    linepredict.remember("og|0", "预测线", 0.2)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == []
    assert out["line"] == "预测线"
    # 直出不写竞速最快线记忆：预测绝不反向升级成竞速实测结果
    assert appmod.remembered_fast_line(_picked()) is None
    assert appmod._LINE_FAST == {}


def test_slow_memory_with_good_throughput_directs(monkeypatch):
    """延迟慢（2.0s）但该线实测吞吐 250 KB/s ≥ 200 时也可直出（或口径）。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    linepredict.remember("og|0", "预测线", 2.0)
    racemod._LINE_SPEED["预测线"] = (250.0, time.time())
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == []
    assert out["line"] == "预测线"


def test_fast_memory_with_bad_throughput_directs(monkeypatch):
    """延迟快（0.2s）时即使吞吐记录很差（100 KB/s）也可直出（或口径）。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    linepredict.remember("og|0", "预测线", 0.2)
    racemod._LINE_SPEED["预测线"] = (100.0, time.time())
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == []
    assert out["line"] == "预测线"


# ---------------- 回退竞速 ----------------

def test_expired_memory_falls_back_to_race(monkeypatch):
    """记忆过期（超过 30 分钟 TTL）时回退竞速，竞速胜者决定线路。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    linepredict.remember("og|0", "预测线", 0.2)
    with linepredict._LOCK:
        ts, line, lat = linepredict._FAST["og|0"]
        linepredict._FAST["og|0"] = (ts - linepredict._TTL - 1, line, lat)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == ["原线"]
    assert out["line"] == "竞速胜者"


def test_slow_memory_falls_back_to_race(monkeypatch):
    """记忆线慢（延迟 2.0s、无吞吐记录）时不直出，回退竞速；慢记忆保留作种子。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    linepredict.remember("og|0", "预测线", 2.0)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == ["原线"]
    assert out["line"] == "竞速胜者"
    assert linepredict.fastest("og|0") == "预测线"


def test_bad_line_memory_invalidated_and_falls_back(monkeypatch):
    """记忆线被标坏线时必须失效该条记忆并回退竞速。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    monkeypatch.setattr(appmod, "_line_is_bad",
                        lambda c: c.get("line") == "预测线")
    linepredict.remember("og|0", "预测线", 0.2)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == ["原线"]
    assert out["line"] == "竞速胜者"
    assert linepredict.fastest("og|0") is None


def test_no_memory_falls_back_to_race(monkeypatch):
    """记忆缺失时回退竞速。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == ["原线"]
    assert out["line"] == "竞速胜者"


def test_predicted_line_not_a_candidate_falls_back(monkeypatch):
    """记忆的线不是本集候选（该线没有这一集）时回退竞速。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["其他线"])
    calls = _patch_race(monkeypatch)
    linepredict.remember("og|0", "预测线", 0.2)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == ["原线"]
    assert out["line"] == "竞速胜者"


def test_switch_off_restores_old_behavior(monkeypatch):
    """开关关时恢复旧行为：记忆可信也不直出，竞速全量跑、胜者决定线路。"""
    _patch_auto(monkeypatch)
    _patch_candidates(monkeypatch, ["预测线", "其他线"])
    calls = _patch_race(monkeypatch)
    _patch_switch_off(monkeypatch)
    linepredict.remember("og|0", "预测线", 0.2)
    out = asyncio.run(appmod._ensure_playable(_picked(), None, auto=True))
    assert calls == ["原线"]
    assert out["line"] == "竞速胜者"
    assert appmod.remembered_fast_line(_picked()) is None


def test_switch_off_helper_defaults_on(monkeypatch):
    """开关 helper：真实配置默认开；显式关时读到关。"""
    assert appmod._zero_race_on() is True
    _patch_switch_off(monkeypatch)
    assert racemod._zero_race_on() is False
