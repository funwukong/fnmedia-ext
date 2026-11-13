"""续播 item.ts 镜像 + 海报重试/缓存 + 榜单卡冷缓存刷新（分支 resumeposter）。

证据（2026-11-12 真机 diag）：《伟大的长征》S1E1，play/info 三次都返回
ts=706、duration=2809，分区一致，记录 canon:tv:伟大的长征:s1:e1 也在，
但客户端还是从头播——客户端不消费顶层 data.ts。头号嫌疑：嵌套
data.item.ts 恒为 0（_base_item 默认 "ts": 0，detail_item 连 ts 键都没有）。

修复 1——play/info 把算出的续播 ts 同时写进嵌套 item.ts，覆盖所有返回
真实 ts 的分支（集 guid、标题 guid 经 resume_for_title、快照恢复、
电影标题记录、_vod_play_info 线路卡）；榜单骨架分支（ts=0）保持原样。

修复 2——海报蓝色卡：_poster_upstream_get 对 429/5xx 重试一次（间隔约
0.8 秒）；失败占位图改短缓存（max-age=60）；只缓存成功抓到的图的有界
内存 LRU（条数与总字节双上限，TTL 约 6 小时），不加磁盘缓存。

修复 3——顶部榜单卡：mediadb/list 算榜单卡 poster 时，若该分类的
chart_cache 为空，先触发/等待一次热榜刷新（有界，最多 5 秒），再算
poster；仍为空则 poster 保持 ""。
"""
from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import charts as chartmod
import config as configmod
import history as histmod
import inject as injmod
import marks as marksmod
import store as storemod
import t4_service
from store import episode_guid
from test_p0p1_resume_favorite import (
    ADMIN, movie_brief, resolve, tv_brief, upstream_handler,
)


# =====================================================================
# 修复 1：play/info 续播 ts 镜像进嵌套 item
# =====================================================================
class ResumeItemTsTests(unittest.IsolatedAsyncioTestCase):
    """复现链：POST /play/record（ts=706）→ POST /play/info → 断言
    data["item"]["ts"] == 706。修之前 item.ts 为 0，测试失败。"""

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
        self.config.save({"sources": [
            {"id": "t4demo", "kind": "t4", "enabled": True,
             "name": "Test T4", "api": "http://t4.test/api"}],
            "removed_ids": []})
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
        self.store.remember_merged([tv_brief(), movie_brief()])
        self.tv_eps = [episode_guid("og-tv", "", i) for i in range(8)]

    def tearDown(self):
        self.history.delete(self.tv_eps + ["og-tv", "og-movie"],
                            user="admin-guid")
        self.marks._fav = {}
        self.marks._watched = {}

    async def record(self, ig, ts=706, duration=2809):
        return await self.client.post("/v/api/v1/play/record", json={
            "item_guid": ig, "media_guid": "mg", "ts": ts,
            "duration": duration}, headers=ADMIN)

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.json().get("data") or {}

    async def test_episode_play_info_mirrors_ts_into_item(self):
        # 集 guid 分支：_online_play_info / play_info 的集直查路径
        await self.record(self.tv_eps[0], ts=706)
        d = await self.play_info(self.tv_eps[0])
        self.assertEqual(d.get("ts"), 706)
        self.assertEqual(d.get("item", {}).get("ts"), 706)
        self.assertIn("duration", d.get("item", {}))

    async def test_title_play_info_mirrors_ts_into_item(self):
        # 标题 guid 经 resume_for_title：续播到第 4 集
        await self.record(self.tv_eps[3], ts=600)
        d = await self.play_info("og-tv")
        self.assertEqual(d.get("guid"), self.tv_eps[3])
        self.assertEqual(d.get("ts"), 600)
        self.assertEqual(d.get("item", {}).get("ts"), 600)
        self.assertIn("duration", d.get("item", {}))

    async def test_movie_play_info_mirrors_ts_into_item(self):
        # 电影标题记录分支
        await self.record("og-movie", ts=900)
        d = await self.play_info("og-movie")
        self.assertEqual(d.get("ts"), 900)
        self.assertEqual(d.get("item", {}).get("ts"), 900)
        self.assertIn("duration", d.get("item", {}))

    async def test_line_item_play_info_mirrors_ts_into_item(self):
        # _vod_play_info 线路卡分支：直接用线路集 guid 问 play/info
        line_g = appmod._line_item_guid("og-tv", "lzi::lzm3u8", 3)
        await self.record(line_g, ts=600)
        d = await self.play_info(line_g)
        self.assertEqual(d.get("ts"), 600)
        self.assertEqual(d.get("item", {}).get("ts"), 600)
        self.assertIn("duration", d.get("item", {}))

    async def test_snapshot_play_info_mirrors_ts_into_item(self):
        # 快照恢复分支：store 淘汰后，标题 guid 靠历史快照找回集与 ts
        await self.record(self.tv_eps[0], ts=706)
        with patch.object(self.store, "get_episode", return_value=None), \
                patch.object(self.store, "get_title_by_guid",
                             return_value=None):
            d = await self.play_info("og-tv")
        self.assertEqual(d.get("ts"), 706)
        self.assertEqual(d.get("item", {}).get("ts"), 706)
        self.assertIn("duration", d.get("item", {}))


# =====================================================================
# 修复 2：海报 429/5xx 重试 + 占位图短缓存 + 成功图内存 LRU
# =====================================================================
class PosterRetryCacheTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        # 内存海报缓存是模块级共享：每个用例后清空，避免互相污染。
        appmod._POSTER_OK_CACHE.clear()
        appmod._POSTER_OK_BYTES = 0

    @staticmethod
    def _resp(status, body=b"\xff\xd8\xff\xe0fake-jpeg",
              ctype="image/jpeg"):
        return httpx.Response(status, content=body,
                              headers={"content-type": ctype})

    @staticmethod
    def _fallback_bytes():
        return base64.b64decode(appmod._POSTER_FALLBACK_JPEG_B64)

    async def test_429_retried_once_then_success(self):
        calls = []

        async def fake_get(req, url, headers=None, tries=1):
            calls.append(url)
            return (self._resp(429) if len(calls) == 1
                    else self._resp(200))

        with patch.object(appmod, "ext_get_retry", side_effect=fake_get):
            r = await appmod._poster_upstream_get(
                SimpleNamespace(), "https://img.test/p.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(calls), 2)

    async def test_429_twice_returns_429_after_one_retry(self):
        calls = []

        async def fake_get(req, url, headers=None, tries=1):
            calls.append(url)
            return self._resp(429)

        with patch.object(appmod, "ext_get_retry", side_effect=fake_get):
            r = await appmod._poster_upstream_get(
                SimpleNamespace(), "https://img.test/p.jpg")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(calls), 2)

    async def test_500_retried_once_then_success(self):
        calls = []

        async def fake_get(req, url, headers=None, tries=1):
            calls.append(url)
            return (self._resp(500) if len(calls) == 1
                    else self._resp(200))

        with patch.object(appmod, "ext_get_retry", side_effect=fake_get):
            r = await appmod._poster_upstream_get(
                SimpleNamespace(), "https://img.test/p.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_fallback_uses_short_cache(self):
        r = appmod._poster_fallback()
        self.assertEqual(r.headers.get("cache-control"), "max-age=60")

    async def test_success_cached_second_request_no_upstream(self):
        tok = injmod._poster("https://img.test/p.jpg")
        calls = []

        async def fake_get(req, url, headers=None, tries=1):
            calls.append(url)
            return self._resp(200)

        with patch.object(appmod, "ext_get_retry", side_effect=fake_get):
            r1 = await appmod._serve_online_poster(tok, SimpleNamespace())
            r2 = await appmod._serve_online_poster(tok, SimpleNamespace())
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.body, b"\xff\xd8\xff\xe0fake-jpeg")
        self.assertEqual(r2.body, b"\xff\xd8\xff\xe0fake-jpeg")
        self.assertEqual(len(calls), 1)

    async def test_failure_not_cached_as_success(self):
        # 失败（500×2：原请求 + 一次重试）不进成功缓存：下次仍会再抓，
        # 图床恢复后立刻能拿到真图，而不是一直返回占位图。
        tok = injmod._poster("https://img.test/q.jpg")
        calls = []

        async def fake_get(req, url, headers=None, tries=1):
            calls.append(url)
            return (self._resp(500) if len(calls) <= 2
                    else self._resp(200))

        with patch.object(appmod, "ext_get_retry", side_effect=fake_get):
            r1 = await appmod._serve_online_poster(tok, SimpleNamespace())
            r2 = await appmod._serve_online_poster(tok, SimpleNamespace())
        self.assertEqual(r1.body, self._fallback_bytes())
        self.assertEqual(r2.body, b"\xff\xd8\xff\xe0fake-jpeg")
        self.assertEqual(len(calls), 3)

    def test_cache_evicts_oldest_when_over_limits(self):
        with patch.object(appmod, "_POSTER_OK_MAX_ITEMS", 2):
            appmod._poster_ok_put("https://a.test/1.jpg", b"x" * 10,
                                  "image/jpeg")
            appmod._poster_ok_put("https://b.test/2.jpg", b"y" * 10,
                                  "image/jpeg")
            appmod._poster_ok_put("https://c.test/3.jpg", b"z" * 10,
                                  "image/jpeg")
            self.assertIsNone(
                appmod._poster_ok_get("https://a.test/1.jpg"))
            self.assertIsNotNone(
                appmod._poster_ok_get("https://c.test/3.jpg"))
            self.assertEqual(appmod._POSTER_OK_BYTES, 20)

    def test_cache_entry_expires(self):
        with patch.object(appmod, "_POSTER_OK_TTL", -1):
            appmod._poster_ok_put("https://a.test/1.jpg", b"x" * 10,
                                  "image/jpeg")
        self.assertIsNone(appmod._poster_ok_get("https://a.test/1.jpg"))


# =====================================================================
# 修复 3：mediadb/list 榜单卡冷缓存时有界等一次热榜刷新
# =====================================================================
class _FakeChartCache:
    """items() 先空；refresh() 成功时填满各分类（cover 为真图 URL），
    fail 时抛错，slow 时睡 30 秒。"""

    def __init__(self):
        self._data = {}
        self.refresh_calls = 0
        self.fail = False
        self.slow = False

    def items(self, cat):
        return list(self._data.get(cat, []))

    async def refresh(self, force=False):
        self.refresh_calls += 1
        if self.slow:
            await asyncio.sleep(30)
        if self.fail:
            raise RuntimeError("douban unreachable")
        self._data = {cat: [{"cover": "https://img.test/p.jpg",
                             "title": "T"}]
                      for cat, _, _ in chartmod.CHART_CATEGORIES}
        return self._data


class ChartPosterColdCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        def official(request):
            return httpx.Response(200, json={"code": 0, "data": []})

        self.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(official))
        self.addAsyncCleanup(self.upstream.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="https://media.example.com")
        self.addAsyncCleanup(self.client.aclose)
        self.charts = _FakeChartCache()
        for name, value in (
            ("up_client", lambda request: self.upstream),
            ("charts_enabled", lambda: True),
            ("catalog_enabled", lambda: False),
            ("standalone_sources_active", lambda: []),
            ("chart_cache", self.charts),
        ):
            item = patch.object(appmod, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(appmod.fnusersmod, "resolve", return_value={
            "known": True, "guid": "admin-guid", "name": "admin",
            "is_admin": True, "token": "tok-admin"})
        item.start()
        self.addCleanup(item.stop)

    async def _chart_rows(self):
        r = await self.client.get("/v/api/v1/mediadb/list", headers={
            "User-Agent": "com.trim.tv"})
        self.assertEqual(r.status_code, 200, r.text[:300])
        rows = {row["guid"]: row for row in r.json()["data"]}
        return {cat: rows[appmod.CHART_LIB_GUIDS[cat]]
                for cat, _, _ in chartmod.CHART_CATEGORIES}

    async def test_cold_cache_refreshes_once_and_poster_filled(self):
        rows = await self._chart_rows()
        self.assertEqual(self.charts.refresh_calls, 1)
        for cat, row in rows.items():
            self.assertTrue(row["poster"], cat)
            self.assertTrue(row["poster"].startswith(
                "/v/api/v1/sys/img/online/"), cat)

    async def test_refresh_failure_keeps_empty_poster_and_returns(self):
        self.charts.fail = True
        rows = await self._chart_rows()
        self.assertEqual(self.charts.refresh_calls, 1)
        for cat, row in rows.items():
            self.assertEqual(row["poster"], "", cat)

    async def test_slow_refresh_bounded_to_five_seconds(self):
        self.charts.slow = True
        t0 = time.monotonic()
        rows = await self._chart_rows()
        dt = time.monotonic() - t0
        self.assertEqual(self.charts.refresh_calls, 1)
        for cat, row in rows.items():
            self.assertEqual(row["poster"], "", cat)
        # 有界等待 5 秒：即使刷新要 30 秒，调用也应在 10 秒内返回。
        self.assertLess(dt, 10.0)


if __name__ == "__main__":
    unittest.main()
