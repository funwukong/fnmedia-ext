"""解冻第 3 项：播放中静默换线（mid_playback_switch）。

auto 起播的线路在播放中劣化时，按已实测数据静默切到同集更快的线，
不打断播放、不弹提示；用户手动选线不受影响。

行为约定：
- 触发（main.m3u8 重取时，在 _serve_normal 内 _ensure_playable 之后）：
  当前线最近分片持续慢（linepredict：≥3 个慢分片、中位数 ≥2.2s），且
  另一条线有新鲜的更快分片样本（pick_faster_line），或备选线有实测
  吞吐达标（_line_speed ≥ _SPEED_GOOD 200 KB/s）、或为 linepredict
  冠军线记忆；只用已有实测数据，决策不新增同步探测（不调 _probe_target）。
- 防抖：每集 30 秒内最多换一次（linepredict._SWITCH_COOLDOWN=30.0，
  pick_faster_line 与后备路径共用同一冷却戳）。
- 上限：同一集连续换线失败 3 次（换过去的新线取不到清单）后不再主动
  换线，回退原线走既有失败路径；换线成功一次即清零。
- 手动选线（auto=False，即 query 带 line）：不静默换线；劣化只走既有
  故障切换（第 1 项固定线直出的语义：已知坏线换线、取清单失败换线）。
- 开关（config.mid_playback_switch，默认开）：关时恢复旧行为。
- 分片耗时记录：流式分片泵（_try_stream_segment）与整段缓冲回退
  （hls_seg）完成一次分片即记 linepredict.record_seg，供决策使用；
  播放中途断开的分片不记（避免误判慢线）。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app as appmod
import config as config_mod
import hls_race as racemod
import linepredict

OG = "og-mid"
CUR = "cms::A"
ALT = "cms::B"
EP_KEY = f"{OG}|0"


def _picked(line=CUR):
    return {"online_guid": OG, "index": 0, "line": line,
            "title": "测试片", "url": f"https://{line[-1].lower()}.test/m.m3u8"}


def _alt_target():
    return {"online_guid": OG, "index": 0, "line": ALT,
            "title": "测试片", "url": "https://b.test/m.m3u8"}


def _seed_slow_cur(n=3, sec=3.0):
    for _ in range(n):
        linepredict.record_seg(OG, 0, CUR, sec)


def _seed_fast_alt(n=2, sec=0.5):
    for _ in range(n):
        linepredict.record_seg(OG, 0, ALT, sec)


@pytest.fixture(autouse=True)
def _clean_mid_switch_state():
    linepredict.reset_seg_timing()
    racemod._LINE_BAD.clear()
    racemod._LINE_SPEED.clear()
    fails = getattr(racemod, "_MID_SWITCH_FAILS", None)
    if fails is not None:
        fails.clear()
    yield
    linepredict.reset_seg_timing()
    racemod._LINE_BAD.clear()
    racemod._LINE_SPEED.clear()
    if fails is not None:
        fails.clear()


# ---------------- linepredict：分片耗时判定 ----------------

def test_switch_cooldown_is_30s():
    assert linepredict._SWITCH_COOLDOWN == 30.0


def test_pick_faster_line_returns_alt_when_cur_slow_and_alt_fast():
    _seed_slow_cur()
    _seed_fast_alt()
    assert linepredict.pick_faster_line(OG, 0, CUR) == ALT


def test_pick_faster_line_cooldown_blocks_second_pick():
    _seed_slow_cur()
    _seed_fast_alt()
    assert linepredict.pick_faster_line(OG, 0, CUR) == ALT
    # 30 秒内第二次劣化不换线
    assert linepredict.pick_faster_line(OG, 0, CUR) is None
    # 冷却过后可再换
    linepredict._LAST_SWITCH[EP_KEY] = time.time() - 31.0
    assert linepredict.pick_faster_line(OG, 0, CUR) == ALT


def test_cur_line_slow_predicate():
    assert linepredict.cur_line_slow(OG, 0, CUR) is None
    linepredict.record_seg(OG, 0, CUR, 3.0)
    linepredict.record_seg(OG, 0, CUR, 3.0)
    assert linepredict.cur_line_slow(OG, 0, CUR) is None  # 不足 3 个样本
    linepredict.record_seg(OG, 0, CUR, 3.0)
    assert linepredict.cur_line_slow(OG, 0, CUR) == pytest.approx(3.0)
    # 快线不算慢
    linepredict.record_seg(OG, 0, ALT, 0.5)
    linepredict.record_seg(OG, 0, ALT, 0.5)
    linepredict.record_seg(OG, 0, ALT, 0.5)
    assert linepredict.cur_line_slow(OG, 0, ALT) is None


def test_switch_allowed_and_mark_switched():
    assert linepredict.switch_allowed(OG, 0) is True
    linepredict.mark_switched(OG, 0)
    assert linepredict.switch_allowed(OG, 0) is False
    linepredict._LAST_SWITCH[EP_KEY] = time.time() - 31.0
    assert linepredict.switch_allowed(OG, 0) is True


# ---------------- 决策：只用已有实测数据 ----------------

def _patch_candidates(monkeypatch, alt=True):
    def _cands(picked, include_current=True):
        out = []
        if include_current:
            out.append(_picked(CUR))
        if alt:
            out.append(_alt_target())
        return out

    monkeypatch.setattr(appmod, "_episode_candidates", _cands)


def test_pick_target_uses_seg_samples(monkeypatch):
    """分片样本路径：当前线慢 + 另一线有新鲜快样本 → 换到该线，且决策零探测。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    _seed_fast_alt()
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(appmod, "_probe_target", probe)
    monkeypatch.setattr(appmod, "_probe_latency", AsyncMock(return_value=0.1))
    out = racemod._pick_mid_switch_target(_picked())
    assert out is not None and out["line"] == ALT
    assert probe.call_count == 0


def test_pick_target_falls_back_to_measured_speed(monkeypatch):
    """无他线分片样本时：备选线有实测吞吐达标（≥200 KB/s）→ 换线。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    racemod._LINE_SPEED[ALT] = (500.0, time.time())
    out = racemod._pick_mid_switch_target(_picked())
    assert out is not None and out["line"] == ALT


def test_pick_target_ignores_unmeasured_alt(monkeypatch):
    """备选线既无分片样本、又无实测吞吐、又非冠军线 → 不赌博，不换线。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    assert racemod._pick_mid_switch_target(_picked()) is None


def test_pick_target_skips_bad_alt(monkeypatch):
    """备选线已被记坏 → 不换过去（即使它有快样本）。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    _seed_fast_alt()
    racemod._mark_line_bad(_alt_target())
    assert racemod._pick_mid_switch_target(_picked()) is None


def test_pick_target_respects_cooldown(monkeypatch):
    """换线后 30 秒内第二次劣化不换线（后备路径与样本路径共用冷却）。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    racemod._LINE_SPEED[ALT] = (500.0, time.time())
    assert racemod._pick_mid_switch_target(_picked())["line"] == ALT
    assert racemod._pick_mid_switch_target(_picked()) is None


def test_pick_target_no_switch_when_cur_not_slow(monkeypatch):
    """当前线不慢 → 不换线（无分片历史的首播也不换）。"""
    _patch_candidates(monkeypatch)
    racemod._LINE_SPEED[ALT] = (500.0, time.time())
    assert racemod._pick_mid_switch_target(_picked()) is None


# ---------------- 上限：连续换线失败 3 次后回退 ----------------

def test_fail_cap_blocks_after_three(monkeypatch):
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    _seed_fast_alt()
    picked = _picked()
    assert racemod._mid_switch_failures(picked) == 0
    for i in (1, 2):
        racemod._mid_switch_note_fail(picked)
        assert racemod._mid_switch_failures(picked) == i
        # 未达上限仍可换（冷却需手动清掉：失败记数不绕过防抖）
        linepredict._LAST_SWITCH.pop(EP_KEY, None)
        assert racemod._pick_mid_switch_target(picked)["line"] == ALT
    racemod._mid_switch_note_fail(picked)
    assert racemod._mid_switch_failures(picked) == 3
    linepredict._LAST_SWITCH.pop(EP_KEY, None)
    assert racemod._pick_mid_switch_target(picked) is None
    # 成功一次清零，之后可再换
    racemod._mid_switch_note_ok(picked)
    assert racemod._mid_switch_failures(picked) == 0
    assert racemod._pick_mid_switch_target(picked)["line"] == ALT


# ---------------- 手动选线 / 开关 ----------------

def test_maybe_mid_switch_manual_returns_none(monkeypatch):
    """手动选线（auto=False）：即使当前线很慢也不擅自换走。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    _seed_fast_alt()
    assert racemod._maybe_mid_switch(_picked(), auto=False) is None


def test_maybe_mid_switch_auto_returns_alt(monkeypatch):
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    _seed_fast_alt()
    monkeypatch.setattr(racemod, "_mid_switch_on", lambda: True)
    out = racemod._maybe_mid_switch(_picked(), auto=True)
    assert out is not None and out["line"] == ALT


def test_maybe_mid_switch_off_returns_none(monkeypatch):
    """开关关：恢复旧行为，不静默换线。"""
    _patch_candidates(monkeypatch)
    _seed_slow_cur()
    _seed_fast_alt()
    monkeypatch.setattr(racemod, "_mid_switch_on", lambda: False)
    assert racemod._maybe_mid_switch(_picked(), auto=True) is None


def test_mid_switch_on_reads_config(monkeypatch):
    assert racemod._mid_switch_on() is True
    monkeypatch.setattr(appmod.config, "get",
                        lambda: {"mid_playback_switch": False})
    assert racemod._mid_switch_on() is False


# ---------------- 分片耗时记录：流式泵 ----------------

async def _drain_stream(resp):
    return b"".join([chunk async for chunk in resp.body_iterator])


def test_stream_pump_records_seg_timing(monkeypatch):
    """流式分片泵每完成一个分片记一条耗时样本（播放中途断开的不记）。"""
    payload = b"T" * 4096

    class _ChunkStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.02)
            yield payload

    def _handler(request):
        # 注意：content= 会被 MockTransport 预读成已消费，aiter_raw 会抛
        # StreamConsumed；必须用真正的异步流才能走通流式泵。
        return httpx.Response(200, headers={"content-length": str(len(payload)),
                                            "content-type": "video/mp2t"},
                              stream=_ChunkStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(appmod, "ext_client", lambda req: client)
    picked = _picked()
    for _ in range(3):
        resp = asyncio.run(racemod._try_stream_segment(
            None, "https://a.test/seg0.ts", {}, picked))
        assert resp is not None
        body = asyncio.run(_drain_stream(resp))
        assert body == payload
    assert linepredict.cur_line_slow(OG, 0, CUR) is None  # 快分片不算慢
    samples = linepredict._SEG.get(f"{OG}|0|{CUR}")
    assert samples is not None and len(samples[1]) == 3
    asyncio.run(client.aclose())


# ---------------- 配置默认值 / 白名单 / 管理页 ----------------

def test_mid_playback_switch_in_defaults():
    assert config_mod.DEFAULTS["mid_playback_switch"] is True


def test_old_config_gets_default_on_load(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2, "hot_limit": 300}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["mid_playback_switch"] is True


class MidSwitchConfigEndpointTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_config_endpoint_accepts_mid_playback_switch(self):
        assert self.config.get()["mid_playback_switch"] is True
        resp = await self.client.post("/v/api/ext/config",
                                      json={"mid_playback_switch": False})
        assert resp.status_code == 200
        assert resp.json().get("code") == 0
        assert self.config.get()["mid_playback_switch"] is False
        resp = await self.client.post("/v/api/ext/config",
                                      json={"mid_playback_switch": True})
        assert resp.json().get("code") == 0
        assert self.config.get()["mid_playback_switch"] is True


def test_admin_ui_has_mid_switch_toggle():
    ui = Path(__file__).resolve().parent.parent / "proxy" / "admin_ui.py"
    src = ui.read_text(encoding="utf-8")
    assert 'id="midPlaybackSwitch"' in src
    assert 'mid_playback_switch:$("midPlaybackSwitch").checked' in src
    assert '$("midPlaybackSwitch").checked=CFG.mid_playback_switch!==false' in src


# ---------------- 播放链路集成：main.m3u8 重取 ----------------

PLAYLIST_FOR = {
    CUR: ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
          "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nseg-cms-A.ts\n"),
    ALT: ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
          "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nseg-cms-B.ts\n"),
}


async def _identity_playable(picked, request, auto=False):
    return picked


async def _verify_ok(body, request, picked=None):
    return True


class MidSwitchPlaychainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import history as histmod
        import providers
        import sourcehealth
        import store as storemod
        import t4_media
        import t4_service

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(config_mod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = config_mod.Config()
        self.config.save({"sources": [],
                          "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        for module, name, value in [
            (appmod, "config", self.config), (appmod, "store", self.store),
            (appmod, "history", self.history), (storemod, "store", self.store),
            (appmod, "t4_service", t4_service.T4Service(self.store, self.config)),
            (appmod, "t4_media", t4_media.T4Media()),
            (appmod, "_schedule_routes_refresh", lambda: None),
        ]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        appmod._AGG_CACHE.clear()
        appmod._AGG_INFLIGHT.clear()
        appmod._PLAYLIST_CACHE.clear()
        self.addCleanup(appmod._PLAYLIST_CACHE.clear)
        sourcehealth.reset("good-src")
        self.addCleanup(lambda: sourcehealth.reset("good-src"))
        appmod.app.state.ext = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(404)))
        self.addAsyncCleanup(appmod.app.state.ext.aclose)
        appmod.app.state.up = appmod.app.state.ext
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)
        self.fetch_calls = []
        self.fail_alt = False

    def _enter(self):
        self._stack = [
            patch.object(appmod, "_pick_episode", return_value=_picked()),
            patch.object(appmod, "_ensure_playable", new=_identity_playable),
            patch.object(appmod, "_verify_rewritten_first_seg",
                         new=AsyncMock(return_value=True)),
            patch.object(appmod, "_get_playlist_cached", new=self._fetch),
            patch.object(appmod, "_episode_candidates", new=self._cands),
        ]
        for item in self._stack:
            item.start()

    def _exit(self):
        for item in reversed(self._stack):
            item.stop()

    def _cands(self, picked, include_current=True):
        out = []
        if include_current:
            out.append(_picked(CUR))
        out.append(_alt_target())
        return out

    async def _fetch(self, mg, picked, request):
        self.fetch_calls.append(picked["line"])
        if self.fail_alt and picked["line"] == ALT:
            return None
        return PLAYLIST_FOR[picked["line"]]

    async def _get_main(self, params):
        return await self.client.get("/v/media/online/mg-mid/main.m3u8",
                                     params=params)

    async def test_auto_refetch_switches_to_faster_line(self):
        """劣化重取 main：静默切到快线，播放继续（200 + 新线清单，无占位/报错）。"""
        _seed_slow_cur()
        _seed_fast_alt()
        self._enter()
        try:
            resp = await self._get_main({"index": 0, "auto": "1"})
        finally:
            self._exit()
        assert resp.status_code == 200
        assert resp.headers.get("X-fnmedia-Placeholder") is None
        assert "seg-cms-B.ts" in resp.text
        assert self.fetch_calls == [ALT]

    async def test_auto_refetch_switches_via_measured_speed(self):
        _seed_slow_cur()
        racemod._LINE_SPEED[ALT] = (500.0, time.time())
        self._enter()
        try:
            resp = await self._get_main({"index": 0, "auto": "1"})
        finally:
            self._exit()
        assert resp.status_code == 200
        assert "seg-cms-B.ts" in resp.text

    async def test_first_play_without_history_does_not_switch(self):
        """无分片历史的首播：不换线。"""
        self._enter()
        try:
            resp = await self._get_main({"index": 0, "auto": "1"})
        finally:
            self._exit()
        assert resp.status_code == 200
        assert "seg-cms-A.ts" in resp.text
        assert self.fetch_calls == [CUR]

    async def test_manual_refetch_stays_on_picked_line(self):
        """手动选线重取 main：即使当前线很慢也不擅自换走。"""
        _seed_slow_cur()
        _seed_fast_alt()
        self._enter()
        try:
            resp = await self._get_main({"index": 0, "line": CUR})
        finally:
            self._exit()
        assert resp.status_code == 200
        assert "seg-cms-A.ts" in resp.text
        assert self.fetch_calls == [CUR]

    async def test_switch_off_restores_old_behavior(self):
        """开关关：auto 重取也不换线（旧行为）。"""
        _seed_slow_cur()
        _seed_fast_alt()
        self.config.save({"mid_playback_switch": False})
        self._enter()
        try:
            resp = await self._get_main({"index": 0, "auto": "1"})
        finally:
            self._exit()
        assert resp.status_code == 200
        assert "seg-cms-A.ts" in resp.text
        assert self.fetch_calls == [CUR]

    async def test_second_degradation_within_cooldown_does_not_switch(self):
        """防抖：30 秒内第二次劣化不换线（第一次已切，第二次仍回原线清单）。"""
        _seed_slow_cur()
        _seed_fast_alt()
        self._enter()
        try:
            r1 = await self._get_main({"index": 0, "auto": "1"})
            r2 = await self._get_main({"index": 0, "auto": "1"})
        finally:
            self._exit()
        assert "seg-cms-B.ts" in r1.text
        assert "seg-cms-A.ts" in r2.text
        assert self.fetch_calls == [ALT, CUR]

    async def test_switch_failure_falls_back_to_original(self):
        """换过去的新线取不到清单：记一次失败，回退原线继续播（不 502）。"""
        _seed_slow_cur()
        _seed_fast_alt()
        self.fail_alt = True
        self._enter()
        try:
            resp = await self._get_main({"index": 0, "auto": "1"})
        finally:
            self._exit()
        assert resp.status_code == 200
        assert "seg-cms-A.ts" in resp.text
        assert self.fetch_calls == [ALT, CUR]
        assert racemod._mid_switch_failures(_picked()) == 1

    async def test_fail_cap_blocks_further_switches(self):
        """连续 3 次换线失败后不再主动换线（回退原线走既有失败路径）。"""
        _seed_slow_cur()
        _seed_fast_alt()
        self.fail_alt = True
        self._enter()
        try:
            for _ in range(3):
                resp = await self._get_main({"index": 0, "auto": "1"})
                assert "seg-cms-A.ts" in resp.text
                linepredict._LAST_SWITCH.pop(EP_KEY, None)  # 只清冷却，不清失败计数
            assert racemod._mid_switch_failures(_picked()) == 3
            assert self.fetch_calls.count(ALT) == 3
            # 第 4 次：已达上限，不再试新线，直接取原线
            resp = await self._get_main({"index": 0, "auto": "1"})
            assert "seg-cms-A.ts" in resp.text
            assert self.fetch_calls.count(ALT) == 3
        finally:
            self._exit()


if __name__ == "__main__":
    unittest.main()
