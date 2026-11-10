"""补缺口 port/gaps-play：占位视频 / 取流熔断 / 聚合缓存池 / 自动优选源。

外加 TURBO 冻结断言：竞速函数体与四值未变、最快线记忆只由竞速胜者写。
只测纯逻辑与进程内路由，不碰真实网络与飞牛。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import sourcehealth
import store as storemod
import t4_media
import t4_service

GOOD = {"id": "good-src", "name": "Good",
        "api": "https://good.test/api.php/provide/vod/at/json", "enabled": True}
DEAD = {"id": "dead-src", "name": "Dead",
        "api": "https://dead.test/api.php/provide/vod/at/json", "enabled": True}

RACE_FASTEST_SHA256 = "82c4f45a3a9372e21cdb464a0dcbd93b96ae7c458db09e3fc87fa28dca141700"


def _title(vod_id, name, src, play_url, year="2024"):
    return providers._map_vod({
        "vod_id": vod_id, "vod_name": name, "type_name": "电影",
        "vod_year": year,
        "vod_play_from": "A", "vod_play_url": play_url}, src)


class _Req:
    """只满足 query_params.get 的最小请求替身。"""

    def __init__(self, params=None):
        self.query_params = params or {}


class GapsPlayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [GOOD, DEAD],
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
        sourcehealth.reset("good-src")
        sourcehealth.reset("dead-src")
        self.addCleanup(lambda: sourcehealth.reset("good-src"))
        self.addCleanup(lambda: sourcehealth.reset("dead-src"))
        appmod.app.state.ext = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(404)))
        self.addAsyncCleanup(appmod.app.state.ext.aclose)
        appmod.app.state.up = appmod.app.state.ext
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    def _two_titles(self):
        good = _title("1", "好片", GOOD, "正片$https://good.test/m.m3u8")
        dead = _title("2", "差片", DEAD,
                      "第1集$https://dead.test/1.m3u8#第2集$https://dead.test/2.m3u8")
        return [good, dead]

    # ---------- 聚合缓存池（上游 v0.2.69）----------

    async def test_agg_cache_reuses_within_ttl(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search:
            first = await appmod._aggregate_briefs("测试片")
            second = await appmod._aggregate_briefs("测试片")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(first[0], second[0])
        self.assertIsNot(first[0], second[0])  # 命中返回拷贝，不共享同一 dict
        self.assertNotIn("测试片", appmod._search_cache)  # 现有搜索缓存层不动

    async def test_agg_cache_key_splits_fast_and_t4(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search:
            await appmod._aggregate_briefs("测试片", fast_only=True)
            await appmod._aggregate_briefs("测试片", fast_only=False)
            await appmod._aggregate_briefs("测试片", fast_only=True)
        self.assertEqual(search.call_count, 2)

    async def test_online_search_defaults_to_fast_only(self):
        # 上游 v0.2.43：全局搜索框默认只聚合秒回的在线直链源，不被少数慢源拖到超时。
        with patch.object(appmod, "_aggregate_briefs",
                          return_value=[]) as agg:
            await appmod.online_search("测试片", self.client)
        agg.assert_awaited_once()
        self.assertIs(agg.call_args.kwargs.get("fast_only"), True)
        # fast_only 与全量走不同缓存键，互不串味
        with patch.object(appmod, "_aggregate_briefs",
                          return_value=[]) as agg2:
            await appmod.online_search("测试片", self.client, fast_only=False)
        agg2.assert_awaited_once()
        self.assertIs(agg2.call_args.kwargs.get("fast_only"), False)

    async def test_agg_cache_refresh_bypasses(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search:
            await appmod._aggregate_briefs("测试片")
            await appmod._aggregate_briefs("测试片", refresh=True)
        self.assertEqual(search.call_count, 2)

    async def test_agg_cache_expires_after_ttl(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search:
            await appmod._aggregate_briefs("测试片")
            key = "测试片\x000\x001"
            ts, val = appmod._AGG_CACHE[key]
            appmod._AGG_CACHE[key] = (ts - appmod.AGG_CACHE_TTL - 1, val)
            await appmod._aggregate_briefs("测试片")
        self.assertEqual(search.call_count, 2)

    async def test_agg_cache_singleflight(self):
        calls = 0

        async def fake_search(*args, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return self._two_titles()

        with patch.object(providers, "search_all", side_effect=fake_search):
            first, second = await asyncio.gather(
                appmod._aggregate_briefs("单飞片"),
                appmod._aggregate_briefs("单飞片"))
        self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    # ---------- 自动优选源（上游 v0.2.47）----------

    def test_auto_best_source_default_true(self):
        self.assertIs(configmod.DEFAULTS["auto_best_source"], True)
        self.assertIs(self.config.get().get("auto_best_source"), True)

    async def test_auto_best_sorts_healthy_first(self):
        # dead-src 连续失败进熔断（grade 3），good-src 健康（grade 0）；
        # dead 组直链更多（2 集），只看质量权重时会排前面。
        for _ in range(3):
            sourcehealth.record("dead-src", False, 1.0)
        sourcehealth.record("good-src", True, 0.5)
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()):
            ranked = await appmod._aggregate_briefs("优选片")
        titles = [b.get("title") for b in ranked]
        self.assertEqual(titles, ["好片", "差片"])

    async def test_auto_best_off_ignores_health(self):
        for _ in range(3):
            sourcehealth.record("dead-src", False, 1.0)
        sourcehealth.record("good-src", True, 0.5)
        self.config.save({"auto_best_source": False})
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()):
            ranked = await appmod._aggregate_briefs("优选片")
        titles = [b.get("title") for b in ranked]
        # 关掉优选：只看质量权重 + 直链数，直链更多的差片组排前（死源降权逻辑本身不动）
        self.assertEqual(titles, ["差片", "好片"])

    # ---------- 取流级熔断（上游 v0.2.70，接已有 sourcehealth）----------

    def test_fetch_breaker_trips_only_after_consecutive_failures(self):
        self.assertFalse(sourcehealth.is_dead("dead-src"))
        sourcehealth.record("dead-src", False, 1.0)
        sourcehealth.record("dead-src", False, 1.0)
        self.assertFalse(sourcehealth.is_dead("dead-src"))  # 未触发：不跳过
        sourcehealth.record("dead-src", False, 1.0)
        self.assertTrue(sourcehealth.is_dead("dead-src"))  # 触发：零成本跳过
        sourcehealth.record("dead-src", True, 0.5)
        self.assertFalse(sourcehealth.is_dead("dead-src"))  # 成功一次即复活

    def test_dodge_known_bad_skips_melted_source(self):
        for _ in range(3):
            sourcehealth.record("dead-src", False, 1.0)
        picked = {"line": "dead-src::A", "online_guid": "og1",
                  "index": 0, "title": "差片"}
        healthy = {"line": "good-src::A", "online_guid": "og1",
                   "index": 0, "title": "好片"}
        with patch.object(appmod, "_episode_candidates",
                          return_value=[healthy]):
            out = appmod._dodge_known_bad(picked, _Req())
        self.assertEqual(out["line"], "good-src::A")

    def test_dodge_known_bad_keeps_healthy_line(self):
        picked = {"line": "good-src::A", "online_guid": "og1",
                  "index": 0, "title": "好片"}
        with patch.object(appmod, "_episode_candidates", return_value=[]):
            out = appmod._dodge_known_bad(picked, _Req())
        self.assertIs(out, picked)

    def test_dodge_known_bad_respects_explicit_line(self):
        for _ in range(3):
            sourcehealth.record("dead-src", False, 1.0)
        picked = {"line": "dead-src::A", "online_guid": "og1",
                  "index": 0, "title": "差片"}
        healthy = {"line": "good-src::A", "online_guid": "og1",
                   "index": 0, "title": "好片"}
        with patch.object(appmod, "_episode_candidates",
                          return_value=[healthy]):
            out = appmod._dodge_known_bad(picked, _Req({"line": "dead-src::A"}))
        self.assertIs(out, picked)  # 用户显式选线：尊重选择，不换线

    def test_race_candidates_sinks_dead_source_but_keeps_it(self):
        for _ in range(3):
            sourcehealth.record("dead-src", False, 1.0)
        sourcehealth.record("good-src", True, 0.5)
        dead = {"line": "dead-src::A", "online_guid": "og1",
                "index": 0, "title": "差片"}
        good = {"line": "good-src::A", "online_guid": "og1",
                "index": 0, "title": "好片"}
        with patch.object(appmod, "_episode_candidates",
                          return_value=[dead, good]), \
                patch.object(self.store, "best_line", return_value=""), \
                patch.object(appmod, "remembered_fast_line", return_value=""), \
                patch.object(appmod.linepredict, "fastest", return_value=None):
            out = appmod._race_candidates(
                {"line": "good-src::A", "online_guid": "og1", "index": 0})
        lines = [c["line"] for c in out]
        self.assertEqual(lines, ["good-src::A", "dead-src::A"])  # 熔断沉底
        self.assertIn("dead-src::A", lines)  # 只沉底、绝不排除

    def test_race_candidates_healthy_order_unchanged(self):
        first = {"line": "good-src::A", "online_guid": "og1",
                 "index": 0, "title": "好片"}
        second = {"line": "dead-src::A", "online_guid": "og1",
                  "index": 0, "title": "差片"}
        with patch.object(appmod, "_episode_candidates",
                          return_value=[first, second]), \
                patch.object(self.store, "best_line", return_value=""), \
                patch.object(appmod, "remembered_fast_line", return_value=""), \
                patch.object(appmod.linepredict, "fastest", return_value=None):
            out = appmod._race_candidates(
                {"line": "good-src::A", "online_guid": "og1", "index": 0})
        self.assertEqual([c["line"] for c in out],
                         ["good-src::A", "dead-src::A"])

    # ---------- 占位视频（上游 v0.2.65）----------

    def test_placeholder_playlist_is_live_loop(self):
        resp = appmod._placeholder_playlist_response("mg1")
        body = resp.body.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
        self.assertIn("#EXTM3U", body)
        self.assertIn("_placeholder/placeholder.ts", body)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:", body)
        self.assertNotIn("#EXT-X-ENDLIST", body)  # 直播式循环，无 ENDLIST
        self.assertEqual(body.count("#EXTINF:"), appmod._PH_WIN)

    def test_placeholder_master_points_to_phwait_main(self):
        resp = appmod._placeholder_master_response(3)
        body = resp.body.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
        self.assertIn("main.m3u8?index=3&phwait=1", body)

    def test_placeholder_ts_serves_asset(self):
        resp = appmod._placeholder_ts_response()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "video/mp2t")
        # FileResponse 的 content-length 只在实际发送时才填；这里核对它指向的文件
        self.assertTrue(str(resp.path).endswith("placeholder.ts"))
        self.assertEqual(Path(resp.path).stat().st_size, 34028)

    def test_is_slow_line_gate(self):
        for slow in ({"guard": True}, {"spider_src": "x"}, {"drpy": True},
                     {"catvod": True}, {"xsrc": "y"},
                     {"line": "guard-foo::A"}, {"line": "spider-x::A"},
                     {"line": "drpy-x::A"}):
            self.assertTrue(appmod._is_slow_line(slow), slow)
        for fast in ({}, {"line": "baofeng::m3u8"},
                     {"line": "cms::A", "m3u8": True}):
            self.assertFalse(appmod._is_slow_line(fast), fast)

    async def test_preset_ph_returns_placeholder_master(self):
        resp = await self.client.get("/v/media/online/nope/preset.m3u8",
                                     params={"index": 2, "ph": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("main.m3u8?index=2&phwait=1", resp.text)
        self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")

    async def test_preset_unbound_returns_placeholder_master(self):
        resp = await self.client.get("/v/media/online/nope/preset.m3u8",
                                     params={"index": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("phwait=1", resp.text)

    async def test_main_phwait_unbound_returns_placeholder_playlist(self):
        resp = await self.client.get("/v/media/online/nope/main.m3u8",
                                     params={"index": 0, "phwait": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("_placeholder/placeholder.ts", resp.text)
        self.assertNotIn("#EXT-X-ENDLIST", resp.text)

    async def test_main_plain_unbound_still_404(self):
        resp = await self.client.get("/v/media/online/nope/main.m3u8",
                                     params={"index": 0})
        self.assertEqual(resp.status_code, 404)

    async def test_direct_unbound_redirects_to_preset(self):
        resp = await self.client.get("/v/media/online/nope/direct",
                                     params={"index": 0})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/v/media/online/nope/preset.m3u8?index=0",
                      resp.headers.get("location", ""))

    async def test_placeholder_ts_route(self):
        resp = await self.client.get(
            "/v/media/online/nope/_placeholder/placeholder.ts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("video/mp2t", resp.headers.get("content-type", ""))
        self.assertEqual(len(resp.content), 34028)

    # ---------- TURBO 冻结断言 ----------

    def test_turbo_freeze_constants(self):
        self.assertEqual(appmod._LINE_FAST_TTL, 600.0)
        self.assertEqual(appmod._RACE_CONCURRENCY, 4)
        self.assertEqual(appmod._SPEED_PRIOR, 150.0)
        self.assertEqual(appmod._SPEED_GOOD, 200.0)

    def test_turbo_freeze_race_fastest_body_unchanged(self):
        digest = hashlib.sha256(
            inspect.getsource(appmod._race_fastest).encode()).hexdigest()
        self.assertEqual(digest, RACE_FASTEST_SHA256)

    def test_turbo_freeze_fast_memory_only_written_by_race_winner(self):
        mod_src = inspect.getsource(appmod)
        # _remember_fast 全模块只有定义 + _race_fastest.finish 内一次调用
        self.assertEqual(mod_src.count("_remember_fast("), 2)
        self.assertIn("_remember_fast(",
                      inspect.getsource(appmod._race_fastest))
        # _LINE_FAST 唯一写点在 _remember_fast 内
        self.assertEqual(mod_src.count("_LINE_FAST["), 1)
        self.assertIn("_LINE_FAST[",
                      inspect.getsource(appmod._remember_fast))


if __name__ == "__main__":
    unittest.main()
