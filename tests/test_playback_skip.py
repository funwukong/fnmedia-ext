"""跳过片头/片尾（playback-skip）：先写失败测试，再实现。

约定（见实现）：
- 配置 4 键：skip_intro_enabled / skip_intro_seconds /
  skip_outro_enabled / skip_outro_seconds，默认关 / 0；秒数夹取 [0, 300]。
- 生效路径：_build_playlist_body 对剧集（TV）点播媒体清单剪裁分片；
  直播（无 ENDLIST）、电影、未知标题、片长 <= 片头+片尾时不剪。
- play/info 与 stream/list 的 duration 扣掉剪掉的部分（CMS 时长减配置
  秒数；已缓存的裁剪后清单用 EXTINF 求和精确值）；续播 ts 仍是客户端
  在裁剪后时间线上报的值，原样续播。
- 开关关时行为与改前完全一致（清单原文、duration、续播位置）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter
import app as appmod
import config as configmod
import history as histmod
import hls_race
import inject as injmod
import marks as marksmod
import store as storemod
import t4_service
from store import episode_guid

ADMIN = {"Authorization": "Bearer admin-token"}
LINE = "lzi::lzm3u8"

# 10 分片 x 10.0s = 100s；MEDIA-SEQUENCE:7；seg5 前有 DISCONTINUITY。
PLAYLIST_100 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXTINF:10.0,
seg3.ts
#EXTINF:10.0,
seg4.ts
#EXT-X-DISCONTINUITY
#EXTINF:10.0,
seg5.ts
#EXTINF:10.0,
seg6.ts
#EXTINF:10.0,
seg7.ts
#EXTINF:10.0,
seg8.ts
#EXTINF:10.0,
seg9.ts
#EXT-X-ENDLIST
"""

FAKE_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:11
#EXTINF:10.0,
seg0.ts
#EXTINF:9.5,
seg1.ts
#EXTINF:10.5,
seg2.ts
#EXT-X-ENDLIST
"""
# 剪掉 seg0（10s）后的裁剪版：9.5 + 10.5 = 20s，MEDIA-SEQUENCE:1。
FAKE_M3U8_TRIMMED_20 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:11
#EXT-X-MEDIA-SEQUENCE:1
#EXTINF:9.5,
seg1.ts
#EXTINF:10.5,
seg2.ts
#EXT-X-ENDLIST
"""


def _seg_uris(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _media_seq(body: str) -> int | None:
    import re
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", body)
    return int(m.group(1)) if m else None


def _rewritten_seg_names(body: str) -> list[str]:
    """改写后清单里的分片名（/seg?u=<b64> 解码后取尾名），按出现序。"""
    import urllib.parse
    out = []
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("/v/media/online/mg/seg?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(s).query)
            u = (q.get("u") or [""])[0]
            try:
                out.append(appmod.b64d(u).rsplit("/", 1)[-1])
            except Exception:
                out.append(s)
    return out


# ---------------- 1. 配置：DEFAULTS / 夹取 ----------------

def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(configmod, "_STATE", state)
    return state


def test_skip_defaults(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    c = configmod.Config()
    cfg = c.get()
    assert cfg["skip_intro_enabled"] is False
    assert cfg["skip_intro_seconds"] == 0
    assert cfg["skip_outro_enabled"] is False
    assert cfg["skip_outro_seconds"] == 0


def test_old_config_gets_skip_defaults_on_load(tmp_path, monkeypatch):
    import json
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2, "hot_limit": 300}),
                     encoding="utf-8")
    c = configmod.Config()
    assert c.get()["skip_intro_enabled"] is False
    assert c.get()["skip_intro_seconds"] == 0
    assert c.get()["skip_outro_enabled"] is False
    assert c.get()["skip_outro_seconds"] == 0


def test_clamp_skip_seconds_matrix():
    f = configmod.clamp_skip_seconds
    for raw, want in ((999, 300), (-5, 0), ("junk", 0), (None, 0),
                      (90, 90), (300, 300), (0, 0), (45.7, 45),
                      (float("nan"), 0), ("120", 120)):
        assert f(raw) == want, raw


# ---------------- 2. _trim_playlist 纯函数 ----------------

class TrimPlaylistTests(unittest.TestCase):
    def test_intro_drops_leading_segments_only(self):
        out = hls_race._trim_playlist(PLAYLIST_100, 25, 0)
        # 25s：剪 seg0+seg1（20s），seg2 横跨边界保留
        assert _seg_uris(out) == [f"seg{i}.ts" for i in range(2, 10)]
        assert _media_seq(out) == 9
        assert "#EXT-X-MAP" in out
        assert "#EXT-X-ENDLIST" in out
        assert hls_race._playlist_total_seconds(out) == 80

    def test_intro_exact_boundary_drops_whole_segments(self):
        out = hls_race._trim_playlist(PLAYLIST_100, 30, 0)
        assert _seg_uris(out) == [f"seg{i}.ts" for i in range(3, 10)]
        assert _media_seq(out) == 10
        assert hls_race._playlist_total_seconds(out) == 70

    def test_outro_drops_trailing_segments_only(self):
        out = hls_race._trim_playlist(PLAYLIST_100, 0, 15)
        assert _seg_uris(out) == [f"seg{i}.ts" for i in range(0, 9)]
        assert _media_seq(out) == 7  # 片尾剪裁不移动起始序号
        assert hls_race._playlist_total_seconds(out) == 90

    def test_intro_and_outro_combined(self):
        out = hls_race._trim_playlist(PLAYLIST_100, 25, 15)
        assert _seg_uris(out) == [f"seg{i}.ts" for i in range(2, 9)]
        assert _media_seq(out) == 9
        assert hls_race._playlist_total_seconds(out) == 70

    def test_total_shorter_than_skip_is_untouched(self):
        out = hls_race._trim_playlist(PLAYLIST_100, 60, 50)
        assert out == PLAYLIST_100

    def test_skip_zero_is_untouched(self):
        assert hls_race._trim_playlist(PLAYLIST_100, 0, 0) == PLAYLIST_100

    def test_live_playlist_without_endlist_is_untouched(self):
        live = PLAYLIST_100.replace("#EXT-X-ENDLIST\n", "")
        assert hls_race._trim_playlist(live, 25, 15) == live

    def test_master_playlist_without_extinf_is_untouched(self):
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
                  "low.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
                  "high.m3u8\n")
        assert hls_race._trim_playlist(master, 25, 15) == master

    def test_discontinuity_tag_stays_with_kept_segment(self):
        # intro 55：剪 seg0..seg4（50s），seg5 保留，其前的
        # #EXT-X-DISCONTINUITY 必须跟着 seg5 一起保留。
        out = hls_race._trim_playlist(PLAYLIST_100, 55, 0)
        assert _seg_uris(out) == [f"seg{i}.ts" for i in range(5, 10)]
        assert "#EXT-X-DISCONTINUITY" in out
        assert _media_seq(out) == 12

    def test_intro_longer_than_total_is_untouched(self):
        assert hls_race._trim_playlist(PLAYLIST_100, 200, 0) == PLAYLIST_100


# ---------------- 3. _build_playlist_body 接线 ----------------

class _CfgStub:
    def __init__(self, skip):
        self._skip = skip

    def get(self):
        return dict(self._skip)


class _StoreStub:
    def __init__(self, brief):
        self._brief = brief

    def get_title_by_guid(self, guid):
        return self._brief


TV_BRIEF = {"guid": "og-tv", "title": "测试剧", "type_name": "电视剧",
            "lines": {LINE: [{"title": "第1集",
                              "url": "https://lzi.test/v0.m3u8",
                              "direct": True}]}}
MOVIE_BRIEF = {"guid": "og-movie", "title": "测试电影",
               "type_name": "电影",
               "lines": {LINE: [{"title": "正片",
                                 "url": "https://lzi.test/v.m3u8",
                                 "direct": True}]}}


class BuildPlaylistSkipTests(unittest.IsolatedAsyncioTestCase):
    async def _build(self, text, skip, brief):
        picked = {"online_guid": "og-tv", "line": LINE, "index": 0,
                  "url": "https://media.test/list.m3u8", "headers": {}}
        from starlette.requests import Request as StarRequest
        sreq = StarRequest({"type": "http", "method": "GET",
                            "path": "/online/mg/main.m3u8",
                            "headers": [], "app": appmod.app})
        sreq.state._play_mg = "mg"
        with patch.object(appmod, "config", _CfgStub(skip)), \
                patch.object(appmod, "store", _StoreStub(brief)), \
                patch.object(appmod, "_resolve_if_needed",
                             new=AsyncMock()), \
                patch.object(appmod, "_fetch_playlist", new=AsyncMock(
                    return_value=(text, picked["url"]))), \
                patch.object(adfilter, "enabled", return_value=False):
            body, _ = await appmod._build_playlist_body("mg", picked, sreq)
        assert body is not None
        return body

    async def test_skip_off_body_unchanged(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": False, "skip_intro_seconds": 90,
            "skip_outro_enabled": False, "skip_outro_seconds": 120},
            TV_BRIEF)
        assert "/v/media/online/mg/seg?" in body
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(10)]

    async def test_skip_on_tv_trims_intro(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": True, "skip_intro_seconds": 25,
            "skip_outro_enabled": False, "skip_outro_seconds": 0},
            TV_BRIEF)
        assert _media_seq(body) == 9
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(2, 10)]

    async def test_skip_on_tv_trims_outro(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": False, "skip_intro_seconds": 0,
            "skip_outro_enabled": True, "skip_outro_seconds": 15},
            TV_BRIEF)
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(0, 9)]

    async def test_skip_on_movie_is_untouched(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": True, "skip_intro_seconds": 25,
            "skip_outro_enabled": True, "skip_outro_seconds": 15},
            MOVIE_BRIEF)
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(10)]

    async def test_skip_on_live_is_untouched(self):
        live = PLAYLIST_100.replace("#EXT-X-ENDLIST\n", "")
        body = await self._build(live, {
            "skip_intro_enabled": True, "skip_intro_seconds": 25,
            "skip_outro_enabled": False, "skip_outro_seconds": 0},
            TV_BRIEF)
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(10)]

    async def test_skip_on_unknown_title_is_untouched(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": True, "skip_intro_seconds": 25,
            "skip_outro_enabled": False, "skip_outro_seconds": 0},
            None)
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(10)]

    async def test_skip_longer_than_playlist_is_untouched(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": True, "skip_intro_seconds": 200,
            "skip_outro_enabled": False, "skip_outro_seconds": 0},
            TV_BRIEF)
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(10)]

    async def test_skip_seconds_zero_with_switch_on_is_untouched(self):
        body = await self._build(PLAYLIST_100, {
            "skip_intro_enabled": True, "skip_intro_seconds": 0,
            "skip_outro_enabled": True, "skip_outro_seconds": 0},
            TV_BRIEF)
        assert _media_seq(body) == 7
        assert _rewritten_seg_names(body) == [f"seg{i}.ts" for i in range(10)]


# ---------------- 4. 缓存键含跳过签名 ----------------

class PlaylistCacheKeyTests(unittest.TestCase):
    def test_key_differs_when_skip_on(self):
        picked = {"online_guid": "og-tv", "line": LINE, "index": 0}
        with patch.object(appmod, "config", _CfgStub({
                "skip_intro_enabled": False, "skip_intro_seconds": 0,
                "skip_outro_enabled": False, "skip_outro_seconds": 0})):
            off = appmod._playlist_cache_key(picked)
        with patch.object(appmod, "config", _CfgStub({
                "skip_intro_enabled": True, "skip_intro_seconds": 90,
                "skip_outro_enabled": False, "skip_outro_seconds": 0})):
            on = appmod._playlist_cache_key(picked)
        assert off != on

    def test_key_stable_when_skip_off(self):
        picked = {"online_guid": "og-tv", "line": LINE, "index": 0}
        with patch.object(appmod, "config", _CfgStub({
                "skip_intro_enabled": False, "skip_intro_seconds": 0,
                "skip_outro_enabled": False, "skip_outro_seconds": 0})):
            assert (appmod._playlist_cache_key(picked)
                    == appmod._playlist_cache_key(picked))


# ---------------- 5. 管理页 ----------------

def test_admin_ui_has_skip_controls():
    src = (Path(__file__).resolve().parents[1]
           / "proxy" / "admin_ui.py").read_text(encoding="utf-8")
    assert 'id="skipIntroEnabled"' in src
    assert 'id="skipIntroSeconds"' in src
    assert 'id="skipOutroEnabled"' in src
    assert 'id="skipOutroSeconds"' in src
    assert 'skip_intro_enabled:$("skipIntroEnabled").checked' in src
    assert 'skip_outro_enabled:$("skipOutroEnabled").checked' in src
    assert '$("skipIntroEnabled").checked=CFG.skip_intro_enabled===true' in src
    assert '$("skipIntroSeconds").value=CFG.skip_intro_seconds||0' in src
    assert '$("skipOutroEnabled").checked=CFG.skip_outro_enabled===true' in src
    assert '$("skipOutroSeconds").value=CFG.skip_outro_seconds||0' in src


# ---------------- 6. 全链路：配置端点 + play/info + stream/list ----------------

def tv_brief(guid="og-tv", title="测试剧", duration=0):
    eps = [{"title": f"第{i + 1:02d}集",
            "url": f"https://lzi.test/v{i}.m3u8", "direct": True}
           for i in range(8)]
    b = {"guid": guid, "title": title, "year": "2026",
         "type_name": "电视剧", "poster": "https://img.test/p.jpg",
         "overview": "简介", "score": "8", "actor": "", "director": "",
         "area": "", "remarks": "",
         "lines": {LINE: eps}}
    if duration:
        b["duration"] = duration
    return b


def movie_brief(guid="og-movie", title="测试电影", duration=0):
    b = {"guid": guid, "title": title, "year": "2026",
         "type_name": "电影", "poster": "https://img.test/p.jpg",
         "overview": "简介", "score": "8", "actor": "", "director": "",
         "area": "", "remarks": "",
         "lines": {LINE: [{"title": "正片",
                           "url": "https://lzi.test/v.m3u8",
                           "direct": True}]}}
    if duration:
        b["duration"] = duration
    return b


def upstream_handler(request):
    return httpx.Response(200, json={"code": 0, "msg": "", "data": []})


def resolve(request):
    tok = request.headers.get("authorization") or ""
    if tok == "Bearer admin-token":
        return {"known": True, "guid": "admin-guid", "name": "admin",
                "is_admin": True, "token": tok}
    return {"known": False, "guid": "", "name": "", "is_admin": False,
            "token": ""}


class PlaybackSkipApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history"), (marksmod, "marks")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [], "removed_ids": []})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        self.marks = marksmod.Marks()
        for module, name, value in [
                (appmod, "config", self.config),
                (appmod, "store", self.store),
                (appmod, "history", self.history),
                (appmod, "marks", self.marks),
                (storemod, "store", self.store),
                (appmod, "t4_service",
                 t4_service.T4Service(self.store, self.config)),
                (appmod, "_schedule_routes_refresh", lambda: None)]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(appmod.fnusersmod, "resolve",
                            side_effect=resolve)
        item.start()
        self.addCleanup(item.stop)
        self.network = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream_handler))
        self.addAsyncCleanup(self.network.aclose)
        appmod.app.state.ext = self.network
        appmod.app.state.up = self.network
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)
        self.store.remember_merged([
            tv_brief(duration=2700),
            movie_brief(duration=5400),
            tv_brief(guid="og-tv-nodur", title="无时长剧"),
        ])
        self.tv_eps = [episode_guid("og-tv", "", i) for i in range(8)]
        self.nodur_eps = [episode_guid("og-tv-nodur", "", i)
                          for i in range(8)]
        self.movie_eg = episode_guid("og-movie", "", 0)

    def tearDown(self):
        self.history.delete(
            self.tv_eps + self.nodur_eps + [self.movie_eg, "og-tv",
                                            "og-tv-nodur", "og-movie"],
            user="admin-guid")
        self.marks._fav = {}
        self.marks._watched = {}
        appmod._PLAYLIST_CACHE.clear()

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        return r.json().get("data") or {}

    async def stream_list(self, ig):
        r = await self.client.get(f"/v/api/v1/stream/list/{ig}",
                                  headers=ADMIN)
        return r.json().get("data") or {}

    async def record(self, ig, ts=941, duration=0, **extra):
        body = {"item_guid": ig, "ts": ts, "duration": duration}
        body.update(extra)
        return await self.client.post("/v/api/v1/play/record", json=body,
                                      headers=ADMIN)

    async def play_list(self):
        r = await self.client.get("/v/api/v1/play/list", headers=ADMIN)
        return r.json().get("data") or []

    def test_config_endpoint_accepts_skip_keys(self):
        from fastapi.testclient import TestClient
        with TestClient(appmod.app,
                        raise_server_exceptions=False) as cli:
            r = cli.post("/v/api/ext/config", json={
                "skip_intro_enabled": True, "skip_intro_seconds": 90,
                "skip_outro_enabled": True, "skip_outro_seconds": 120,
                "skip_bogus": 123}, headers=ADMIN)
            assert r.status_code == 200, r.text
            cfg = self.config.get()
            assert cfg["skip_intro_enabled"] is True
            assert cfg["skip_intro_seconds"] == 90
            assert cfg["skip_outro_enabled"] is True
            assert cfg["skip_outro_seconds"] == 120
            assert "skip_bogus" not in cfg
            # 秒数夹取：999 -> 300；非法 -> 0
            r = cli.post("/v/api/ext/config", json={
                "skip_intro_seconds": 999,
                "skip_outro_seconds": "junk"}, headers=ADMIN)
            assert r.status_code == 200, r.text
            cfg = self.config.get()
            assert cfg["skip_intro_seconds"] == 300
            assert cfg["skip_outro_seconds"] == 0

    async def test_play_info_duration_unchanged_when_skip_off(self):
        info = await self.play_info(self.tv_eps[0])
        assert info.get("duration") == 2700
        assert "skip_intro" not in info
        assert "skip_outro" not in info

    async def test_play_info_duration_subtracts_skip_when_on(self):
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90,
                          "skip_outro_enabled": True,
                          "skip_outro_seconds": 120})
        info = await self.play_info(self.tv_eps[0])
        assert info.get("duration") == 2700 - 90 - 120
        assert info.get("skip_intro") == 90
        assert info.get("skip_outro") == 120
        assert (info.get("item") or {}).get("duration") == 2700 - 90 - 120

    async def test_play_info_movie_duration_unchanged_when_skip_on(self):
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90,
                          "skip_outro_enabled": True,
                          "skip_outro_seconds": 120})
        info = await self.play_info("og-movie")
        assert info.get("duration") == 5400
        assert "skip_intro" not in info

    async def test_play_info_resume_ts_stays_in_trimmed_range(self):
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90})
        # 客户端在裁剪后时间线上报：ts=2400，duration=2610
        await self.record(self.tv_eps[0], ts=2400, duration=2610)
        info = await self.play_info(self.tv_eps[0])
        assert info.get("ts") == 2400
        assert info.get("duration") == 2610
        assert info.get("ts") < info.get("duration")

    async def test_stream_list_duration_subtracts_skip_when_on(self):
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90})
        sl = await self.stream_list(self.tv_eps[0])
        vs = sl.get("video_streams") or []
        assert vs, sl
        assert vs[0].get("duration") == 2610
        assert sl.get("duration") == 2610

    async def test_stream_list_movie_duration_unchanged_when_skip_on(self):
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90})
        sl = await self.stream_list("og-movie")
        vs = sl.get("video_streams") or []
        assert vs, sl
        assert vs[0].get("duration") == 5400

    async def test_play_info_uses_trimmed_playlist_seconds_when_cached(self):
        # 无 CMS 时长：已缓存的裁剪后清单（20s）即精确时长，不再二次扣减。
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 10})
        key = appmod._playlist_cache_key(
            {"online_guid": "og-tv-nodur", "line": LINE, "index": 0})
        appmod._PLAYLIST_CACHE[key] = (time.time(), FAKE_M3U8_TRIMMED_20)
        info = await self.play_info(self.nodur_eps[0])
        assert info.get("duration") == 20
        assert info.get("skip_intro") == 10

    async def test_play_info_no_cms_no_cache_falls_back_zero(self):
        # 无 CMS 时长、无缓存清单：duration 回退 0，不炸、不变负。
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90})
        info = await self.play_info(self.nodur_eps[1])
        assert info.get("duration") == 0

    async def test_play_list_keeps_probe_duration_when_skip_off(self):
        # 开关关时行为与改前完全一致：集记录上的探测时长（1800）优先
        # 于 CMS 时长（2700），play/list 兜底时长不得被改写成 CMS 值。
        brief = self.store.get_title_by_guid("og-tv")
        brief["ep_durations"] = {"0": 1800}
        await self.record(self.tv_eps[0], ts=941, duration=0)
        items = await self.play_list()
        mine = [x for x in items if x.get("guid") == self.tv_eps[0]]
        assert mine, items
        assert mine[0].get("duration") == 1800
        assert mine[0].get("ts") == 941

    async def test_play_list_uses_cms_minus_skip_when_skip_on(self):
        # 开关开时：play/list 兜底时长按 CMS 时长扣掉跳过秒数。
        brief = self.store.get_title_by_guid("og-tv")
        brief["ep_durations"] = {"0": 1800}
        self.config.save({"skip_intro_enabled": True,
                          "skip_intro_seconds": 90})
        await self.record(self.tv_eps[0], ts=941, duration=0)
        items = await self.play_list()
        mine = [x for x in items if x.get("guid") == self.tv_eps[0]]
        assert mine, items
        assert mine[0].get("duration") == 2610
        assert mine[0].get("ts") == 941


if __name__ == "__main__":
    unittest.main()
