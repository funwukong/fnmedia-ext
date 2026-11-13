"""解冻第 1 项：固定线直出（fixed_line_direct）。

用户手动选定某条线路后直接出流，不再先探活；自动起播仍走竞速流程。

行为约定（_ensure_playable，auto=False = 手动选线）：
- 开关开（默认）：未记坏的线直接返回，不调 _probe_target、不调
  _race_fastest（零新增同步网络等待）；已记坏的线仍走现有故障切换
  （_find_playable 找首个可用线），无可用线时返回原 picked（现有兜底）。
- 开关关：恢复旧行为（先探当前线，失败记坏线再 _find_playable）。
- auto=True（自动起播）：不受本开关影响，仍跑 _race_fastest。
- “不可用”的判定信号是坏线记忆（_LINE_BAD，5 分钟 TTL）与下游取清单/
  分片验活：直出信任用户选择，不对未探过的线预判死活；取清单失败时
  _serve_normal 已有“记坏线 + 换线重试一次”的兜底。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app as appmod
import config as config_mod
import hls_race as racemod


@pytest.fixture(autouse=True)
def _clean_bad_lines():
    racemod._LINE_BAD.clear()
    yield
    racemod._LINE_BAD.clear()


def _picked(line="量子线"):
    return {"online_guid": "og", "index": 0, "line": line,
            "title": "测试片", "url": "https://x.test/m.m3u8"}


def _patch_switch(monkeypatch, on: bool):
    monkeypatch.setattr(appmod, "_fixed_line_direct_on",
                        lambda: on, raising=False)
    monkeypatch.setattr(appmod, "_autofail_on", lambda: True)


def _counting_race(winner):
    calls = []

    async def _race(p, request):
        calls.append(p.get("line"))
        return winner

    return calls, _race


# ---------------- 配置默认值 ----------------

def test_fixed_line_direct_in_defaults():
    assert config_mod.DEFAULTS["fixed_line_direct"] is True


def test_old_config_gets_default_on_load(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2, "hot_limit": 300}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["fixed_line_direct"] is True


# ---------------- 手动选线：开关开 ----------------

def test_manual_direct_returns_picked_without_probe_or_race(monkeypatch):
    """开关开：手动选线直接返回该线，不探活、不竞速。"""
    _patch_switch(monkeypatch, True)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(racemod, "_probe_target", probe)
    race_calls, fake_race = _counting_race(
        {"line": "竞速胜者", "url": "http://x/y.m3u8"})
    monkeypatch.setattr(appmod, "_race_fastest", fake_race)
    picked = _picked()
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=False))
    assert out is picked
    assert out["line"] == "量子线"
    assert probe.call_count == 0
    assert race_calls == []


def test_manual_known_bad_fails_over_to_available(monkeypatch):
    """开关开：选定线路已被记坏 → 不探该线，直接故障切换到可用线。"""
    _patch_switch(monkeypatch, True)
    picked = _picked()
    racemod._mark_line_bad(picked)
    assert racemod._line_is_bad(picked) is True
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(racemod, "_probe_target", probe)
    race_calls, fake_race = _counting_race(
        {"line": "竞速胜者", "url": "http://x/y.m3u8"})
    monkeypatch.setattr(appmod, "_race_fastest", fake_race)
    alt = {"online_guid": "og", "index": 0, "line": "暴风线",
           "title": "测试片", "url": "https://y.test/m.m3u8"}

    async def _find(p, request, skip_current=False):
        return alt

    monkeypatch.setattr(racemod, "_find_playable", _find)
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=False))
    assert out is alt
    assert probe.call_count == 0
    assert race_calls == []


def test_manual_known_bad_no_alternative_returns_picked(monkeypatch):
    """开关开：已知坏线且无可用替代 → 返回原 picked（现有兜底，不抛错/不回 None）。"""
    _patch_switch(monkeypatch, True)
    picked = _picked()
    racemod._mark_line_bad(picked)

    async def _find_none(p, request, skip_current=False):
        return None

    monkeypatch.setattr(racemod, "_find_playable", _find_none)
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=False))
    assert out is picked


# ---------------- 手动选线：开关关 = 旧行为 ----------------

def test_switch_off_probes_current_line(monkeypatch):
    """开关关：手动选线先探当前线，探活成功直接返回该线。"""
    _patch_switch(monkeypatch, False)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(racemod, "_probe_target", probe)
    race_calls, fake_race = _counting_race(
        {"line": "竞速胜者", "url": "http://x/y.m3u8"})
    monkeypatch.setattr(appmod, "_race_fastest", fake_race)
    picked = _picked()
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=False))
    assert out is picked
    assert probe.call_count == 1
    assert race_calls == []


def test_switch_off_probe_fail_fails_over(monkeypatch):
    """开关关：探活失败 → 记坏线 → 故障切换到可用线（旧行为）。"""
    _patch_switch(monkeypatch, False)
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(racemod, "_probe_target", probe)
    alt = {"online_guid": "og", "index": 0, "line": "暴风线",
           "title": "测试片", "url": "https://y.test/m.m3u8"}

    async def _find(p, request, skip_current=False):
        return alt

    monkeypatch.setattr(racemod, "_find_playable", _find)
    picked = _picked()
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=False))
    assert out is alt
    assert racemod._line_is_bad(picked) is True


# ---------------- 自动起播：不受影响 ----------------

def test_auto_path_still_races_with_switch_on(monkeypatch):
    """开关开也不影响自动起播：auto=True 仍跑竞速并用胜者。"""
    _patch_switch(monkeypatch, True)
    monkeypatch.setattr(appmod, "_fastest_on", lambda: True)
    winner = {"line": "竞速胜者", "url": "http://x/y.m3u8"}
    race_calls, fake_race = _counting_race(winner)
    monkeypatch.setattr(appmod, "_race_fastest", fake_race)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(racemod, "_probe_target", probe)
    picked = _picked()
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=True))
    assert out is winner
    assert race_calls == ["量子线"]
    assert probe.call_count == 0


def test_auto_path_race_all_fail_falls_back_to_probe(monkeypatch):
    """自动起播竞速全失败时仍退回探当前线（旧行为，开关开也不变）。"""
    _patch_switch(monkeypatch, True)
    monkeypatch.setattr(appmod, "_fastest_on", lambda: True)
    race_calls, fake_race = _counting_race(None)
    monkeypatch.setattr(appmod, "_race_fastest", fake_race)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(racemod, "_probe_target", probe)
    picked = _picked()
    out = asyncio.run(appmod._ensure_playable(picked, None, auto=True))
    assert out is picked
    assert len(race_calls) == 1
    assert probe.call_count == 1


# ---------------- 白名单 + 管理页 ----------------

class FixedLineDirectConfigEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        item = patch.object(config_mod, "_STATE",
                            Path(temp.name) / "config.json")
        item.start()
        self.addCleanup(item.stop)
        self.config = config_mod.Config()
        item = patch.object(appmod, "config", self.config)
        item.start()
        self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_config_endpoint_accepts_fixed_line_direct(self):
        assert self.config.get()["fixed_line_direct"] is True
        resp = await self.client.post("/v/api/ext/config",
                                      json={"fixed_line_direct": False})
        assert resp.status_code == 200
        assert resp.json().get("code") == 0
        assert self.config.get()["fixed_line_direct"] is False
        resp = await self.client.post("/v/api/ext/config",
                                      json={"fixed_line_direct": True})
        assert resp.json().get("code") == 0
        assert self.config.get()["fixed_line_direct"] is True


def test_admin_ui_has_fixed_line_toggle():
    ui = Path(__file__).resolve().parent.parent / "proxy" / "admin_ui.py"
    src = ui.read_text(encoding="utf-8")
    assert 'id="fixedLineDirect"' in src
    assert 'fixed_line_direct:$("fixedLineDirect").checked' in src
    assert '$("fixedLineDirect").checked=CFG.fixed_line_direct!==false' in src
