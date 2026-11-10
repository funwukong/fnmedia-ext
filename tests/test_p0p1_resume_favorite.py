"""P0+P1 真机 bug 复现回归（分支 p0p1-bugs，基线 fc8ba16）。

Bug 1——播放记录没有记忆功能：重新打开后还是从头开始播。
  复现链：POST /v/api/v1/play/record（模拟客户端上报进度）
          → POST /v/api/v1/play/info（模拟重新打开）→ 断言续播集/ts。
  覆盖 CMS 与 T4 两条 resume 路径；resume_episode 的既有默认语义不变
  （CMS 按集顺序下一集未看完、T4 按 updated_at 最近），只修“进度丢了”。

Bug 2——收藏：首页点收藏提示失败，收藏夹无变化；只有进入具体线路才能收藏。
  复现链：PUT /v/api/v1/item/favorite（首页文件夹卡 guid）
          → POST /v/api/v1/favorite/list → 断言出现该标题。
  首页卡（热门/分类/榜单）是 _vod_directory_card 包装的文件夹卡，
  guid 为 fnmedia-line-folder:<og>；线路详情卡是 fnmedia-line-item:…。

上游官方后端用 MockTransport 假装：不认识虚拟在线 guid 的收藏返回 -6，
与真机官方行为一致；favorite/list 与 play/list 返回空本地列表。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
import config as configmod
import history as histmod
import marks as marksmod
import store as storemod
import t4_service
from store import episode_guid, season_guid

ADMIN = {"Authorization": "Bearer admin-token"}


def tv_brief(guid="og-tv", title="测试剧"):
    eps = [{"title": f"第{i + 1:02d}集",
            "url": f"https://lzi.test/v{i}.m3u8", "direct": True}
           for i in range(8)]
    return {"guid": guid, "title": title, "year": "2026",
            "type_name": "电视剧", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {"lzi::lzm3u8": eps}}


def movie_brief(guid="og-movie", title="测试电影"):
    return {"guid": guid, "title": title, "year": "2026",
            "type_name": "电影", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {"lzi::lzm3u8": [{"title": "正片",
                                        "url": "https://lzi.test/v.m3u8",
                                        "direct": True}]}}


def t4_brief(guid="og-t4", title="T4剧"):
    eps = [{"title": f"第{i + 1:02d}集", "url": f"https://t4.test/e{i}",
            "direct": True, "play_from": "A"} for i in range(3)]
    return {"guid": guid, "title": title, "year": "2026",
            "type_name": "电视剧", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "locator": {"kind": "t4", "source_id": "t4demo",
                        "vod_id": "t4-1", "detail_loaded": True},
            "lines": {"A": eps}}


def upstream_handler(request):
    p = request.url.path
    if request.method == "PUT" and p.endswith("/item/favorite"):
        # 官方后端不认识虚拟在线 guid：PUT /item/favorite 返回 -6
        return httpx.Response(200, json={"code": -6,
                                         "msg": "此媒体库不存在", "data": None})
    if request.method == "DELETE" and p.endswith("/item/favorite"):
        return httpx.Response(200, json={"code": -6,
                                         "msg": "此媒体库不存在", "data": None})
    if p.endswith("/favorite/list"):
        return httpx.Response(200, json={"code": 0, "msg": "",
                                         "data": {"list": [], "total": 0}})
    if p.endswith("/play/list"):
        return httpx.Response(200, json={"code": 0, "msg": "", "data": []})
    return httpx.Response(200, json={"code": 0, "msg": "", "data": []})


def resolve(request):
    tok = request.headers.get("authorization") or ""
    if tok == "Bearer admin-token":
        return {"known": True, "guid": "admin-guid", "name": "admin",
                "is_admin": True, "token": tok}
    return {"known": False, "guid": "", "name": "", "is_admin": False,
            "token": ""}


class BugReproTests(unittest.IsolatedAsyncioTestCase):
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
        self.store.remember_merged(
            [tv_brief(), movie_brief(), t4_brief()])
        # T4 详情已直接 seed 进 store，不再走网络 ensure_detail
        appmod.t4_service._fresh["og-t4"] = time.monotonic()
        self.tv_eps = [episode_guid("og-tv", "", i) for i in range(8)]
        self.t4_eps = self.store.tv_episode_guids("og-t4")
        assert len(self.t4_eps) == 3, self.t4_eps

    def tearDown(self):
        self.history.delete(self.tv_eps + self.t4_eps + ["og-tv", "og-t4"],
                            user="admin-guid")
        self.marks._fav = {}
        self.marks._watched = {}

    # ---- 小工具 ----
    async def record(self, ig, ts=600, duration=3600):
        r = await self.client.post("/v/api/v1/play/record", json={
            "item_guid": ig, "media_guid": "mg", "ts": ts,
            "duration": duration}, headers=ADMIN)
        return r

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        d = r.json().get("data") or {}
        return d.get("guid"), d.get("ts"), d.get("type")

    async def favorite_list(self):
        r = await self.client.post("/v/api/v1/favorite/list",
                                   json={"type": "all", "page": 1,
                                         "page_size": 50}, headers=ADMIN)
        return r.json().get("data", {}).get("list", [])

    # ---- Bug 1：对照组（基线已通过，修完不能坏） ----
    async def test_record_episode_guid_resumes(self):
        await self.record(self.tv_eps[3])
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_line_item_guid_resumes(self):
        line_g = appmod._line_item_guid("og-tv", "lzi::lzm3u8", 3)
        await self.record(line_g)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_movie_title_record_resumes(self):
        await self.record("og-movie", ts=900)
        guid, ts, typ = await self.play_info("og-movie")
        self.assertEqual(guid, "og-movie")
        self.assertEqual(ts, 900)
        self.assertEqual(typ, "Movie")

    async def test_cms_episode_resume_keeps_order_semantics(self):
        # CMS：多集未看完时按集序续播靠后的未看完集
        await self.record(self.tv_eps[1], ts=100)
        await self.record(self.tv_eps[3], ts=600)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_t4_episode_resume_keeps_latest_interaction(self):
        # T4：多集未看完时按最近一次交互选（与 CMS 集序语义不同）
        await self.record(self.t4_eps[0], ts=100)
        await asyncio.sleep(0.01)
        await self.record(self.t4_eps[1], ts=200)
        guid, ts, _ = await self.play_info("og-t4")
        self.assertEqual(guid, self.t4_eps[1])
        self.assertEqual(ts, 200)

    # ---- Bug 1：复现（基线失败） ----
    async def test_record_folder_guid_is_kept_and_resumes(self):
        """首页文件夹卡 guid 上报的进度不能丢：应落到该标题并可续播。"""
        r = await self.record(appmod._line_folder_guid("og-tv"))
        self.assertEqual(r.json()["code"], 0)
        self.assertTrue(r.json()["data"])
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 600)

    async def test_record_line_season_guid_is_kept_and_resumes(self):
        """线路季卡 guid 上报的进度不能丢：应落到该标题并可续播。"""
        sg = appmod._line_season_guid("og-tv", "lzi::lzm3u8", 1)
        r = await self.record(sg)
        self.assertEqual(r.json()["code"], 0)
        self.assertTrue(r.json()["data"])
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 600)

    async def test_record_title_guid_resumes_first_episode(self):
        """客户端以标题 guid 上报剧集进度时，重新打开应从第一集该位置续播，
        而不是悄悄存下标题记录、重开却回第 1 集 0 秒。"""
        r = await self.record("og-tv")
        self.assertEqual(r.json()["code"], 0)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 600)

    async def test_record_title_guid_resumes_via_line_card(self):
        """同一条标题进度，走线路卡播放页也要带上位置。"""
        await self.record("og-tv")
        line_g = appmod._line_item_guid("og-tv", "lzi::lzm3u8", 0)
        guid, ts, _ = await self.play_info(line_g)
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 600)

    async def test_record_title_guid_resumes_via_episode(self):
        """同一条标题进度，直接打开第 1 集也要带上位置。"""
        await self.record("og-tv")
        guid, ts, _ = await self.play_info(self.tv_eps[0])
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 600)

    async def test_t4_title_record_resumes_first_episode(self):
        """T4 同理：标题 guid 上报的进度，重新打开从第一集该位置续播。"""
        await self.record("og-t4")
        guid, ts, _ = await self.play_info("og-t4")
        self.assertEqual(guid, self.t4_eps[0])
        self.assertEqual(ts, 600)

    async def test_finished_title_record_does_not_rewind(self):
        """标题记录已看完（>=95%）时不倒回：重开仍从第 1 集 0 秒起。"""
        await self.record("og-tv", ts=3500, duration=3600)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 0)

    async def test_episode_record_wins_over_title_record(self):
        """集记录与标题记录同时存在时，以集记录为准，不被标题位置覆盖。"""
        await self.record("og-tv", ts=600)
        await self.record(self.tv_eps[3], ts=100)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 100)

class NormalizeTests(unittest.TestCase):
    def test_normalize_folder_guid(self):
        self.assertEqual(
            appmod._normalize_history_guid("fnmedia-line-folder:og-tv"),
            "og-tv")

    def test_normalize_line_season_guid(self):
        sg = appmod._line_season_guid("og-tv", "lzi::lzm3u8", 1)
        self.assertEqual(appmod._normalize_history_guid(sg), "og-tv")

    def test_normalize_plain_guid_unchanged(self):
        self.assertEqual(appmod._normalize_history_guid("og-tv"), "og-tv")


if __name__ == "__main__":
    unittest.main()
