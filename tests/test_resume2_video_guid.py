"""Resume round 2：客户端上报进度时 item_guid 用 video/media guid 的复现回归。

上一批（test_p0p1_resume_favorite.py）只覆盖了集 guid / 文件夹卡 /
线路卡 / 季卡 / 标题 guid 上报；play/info 返回给播放器的却是
info["video_guid"] = ep_video_guid(eg, line) 与
info["media_guid"] = ep_media_guid(eg, line)。若真实客户端上报时
item_guid 用的是这两个 hash guid 之一，play/record 的守门
（store.get_title_by_guid(ig) or store.get_episode(ig)）直接失败，
进度透传给官方后端丢掉——三端共用同一上报协议，所以三端表现一致。

本文件先写失败测试：以下除对照组外在基线 24475e9 上应全部失败，
修完应全部通过。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
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
from store import (episode_guid, ep_media_guid, ep_video_guid,
                   media_guid, video_guid)

ADMIN = {"Authorization": "Bearer admin-token"}
LINE = "lzi::lzm3u8"


def tv_brief(guid="og-tv", title="测试剧"):
    eps = [{"title": f"第{i + 1:02d}集",
            "url": f"https://lzi.test/v{i}.m3u8", "direct": True}
           for i in range(8)]
    return {"guid": guid, "title": title, "year": "2026",
            "type_name": "电视剧", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {LINE: eps}}


def movie_brief(guid="og-movie", title="测试电影"):
    return {"guid": guid, "title": title, "year": "2026",
            "type_name": "电影", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {LINE: [{"title": "正片",
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


@contextmanager
def as_marks_user(guid, admin=False):
    tokens = marksmod.set_current_user(guid, admin)
    try:
        yield
    finally:
        marksmod.reset_current_user(tokens)


class VideoGuidResumeTests(unittest.IsolatedAsyncioTestCase):
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
        appmod.t4_service._fresh["og-t4"] = time.monotonic()
        self.tv_eps = [episode_guid("og-tv", "", i) for i in range(8)]
        self.t4_eps = self.store.tv_episode_guids("og-t4")
        assert len(self.t4_eps) == 3, self.t4_eps
        self.movie_eg = episode_guid("og-movie", "", 0)

    def tearDown(self):
        self.history.delete(
            self.tv_eps + self.t4_eps + [self.movie_eg, "og-tv", "og-t4",
                                         "og-movie"], user="admin-guid")
        self.marks._fav = {}
        self.marks._watched = {}

    # ---- 小工具 ----
    async def record_raw(self, body):
        return await self.client.post(
            "/v/api/v1/play/record", json=body, headers=ADMIN)

    async def record(self, ig, ts=600, duration=3600, **extra):
        body = {"item_guid": ig, "ts": ts, "duration": duration}
        body.update(extra)
        return await self.record_raw(body)

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

    def assert_record_kept(self, r):
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertTrue(r.json()["data"], r.json())

    # ---- 对照组：基线已通过，修完不能坏 ----
    async def test_record_episode_guid_still_works(self):
        await self.record(self.tv_eps[3])
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_episode_guid_with_video_fields_still_works(self):
        """item_guid 是集 guid、body 里 video/media 字段带同集 video guid：
        本就应通过；修完 body 字段解析不能把它带偏。"""
        r = await self.record(
            self.tv_eps[3],
            video_guid=ep_video_guid(self.tv_eps[3], LINE),
            media_guid=ep_media_guid(self.tv_eps[3], LINE))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    # ---- 复现：item_guid 用 video/media guid（基线失败） ----
    async def test_record_video_guid_with_line_resumes(self):
        """play/info（线路卡）返回的 video guid 上报：应落到该集并可续播。"""
        r = await self.record(ep_video_guid(self.tv_eps[3], LINE))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_media_guid_with_line_resumes(self):
        r = await self.record(ep_media_guid(self.tv_eps[3], LINE))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_video_guid_no_line_resumes(self):
        """episode_play_info 返回的无线路 video guid（line=""）同样要认。"""
        r = await self.record(ep_video_guid(self.tv_eps[3]))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_media_guid_no_line_resumes(self):
        r = await self.record(ep_media_guid(self.tv_eps[3]))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_title_video_guid_resumes(self):
        """电影 play/info（inj.play_info）返回的是标题级 video guid：
        应落到该标题并可续播。"""
        r = await self.record(video_guid("og-movie"))
        self.assert_record_kept(r)
        guid, ts, typ = await self.play_info("og-movie")
        self.assertEqual(guid, "og-movie")
        self.assertEqual(ts, 600)
        self.assertEqual(typ, "Movie")

    async def test_record_movie_episode_video_guid_resumes(self):
        """电影走线路卡时 play/info 返回的是集级 video guid：
        电影按标题续播，应落到标题而不是集 guid。"""
        r = await self.record(ep_video_guid(self.movie_eg, LINE))
        self.assert_record_kept(r)
        guid, ts, typ = await self.play_info("og-movie")
        self.assertEqual(guid, "og-movie")
        self.assertEqual(ts, 600)
        self.assertEqual(typ, "Movie")

    async def test_record_t4_video_guid_resumes(self):
        r = await self.record(ep_video_guid(self.t4_eps[1], "A"))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-t4")
        self.assertEqual(guid, self.t4_eps[1])
        self.assertEqual(ts, 600)

    # ---- 复现：body 三字段交叉（基线失败） ----
    async def test_record_title_guid_with_episode_video_field(self):
        """item_guid 是标题、body video_guid 是第 4 集的 video guid：
        进度属于第 4 集，不能只落到标题（标题记录只回填第 1 集）。"""
        r = await self.record(
            "og-tv", video_guid=ep_video_guid(self.tv_eps[3], LINE),
            media_guid=ep_media_guid(self.tv_eps[3], LINE))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)

    async def test_record_title_guid_with_episode_media_field_only(self):
        r = await self.record(
            "og-tv", media_guid=ep_media_guid(self.tv_eps[5], LINE))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[5])
        self.assertEqual(ts, 600)

    # ---- 删除与收藏/已看：同一身份要认 ----
    async def test_delete_record_by_video_guid(self):
        await self.record(ep_video_guid(self.tv_eps[3], LINE))
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual((guid, ts), (self.tv_eps[3], 600))
        r = await self.client.request(
            "DELETE", "/v/api/v1/play/record",
            json={"item_guid": ep_video_guid(self.tv_eps[3], LINE)},
            headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual((guid, ts), (self.tv_eps[0], 0))

    async def test_favorite_by_video_guid_succeeds_and_lists(self):
        r = await self.client.request(
            "PUT", "/v/api/v1/item/favorite",
            json={"guid": ep_video_guid(self.tv_eps[0], LINE)},
            headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        rows = await self.favorite_list()
        self.assertEqual(len(rows), 1)
        self.assertIn("测试剧", rows[0]["title"])

    async def test_unfavorite_by_video_guid_removes(self):
        """video guid 收藏后，再用同一 video guid 取消：应删掉。
        （注：PUT 标题 guid、DELETE video guid 这种跨形态组合删不掉，
        是 marks 按键精确匹配的老口径，线路卡集 guid 与标题 guid 混用
        时同样存在，不在本轮 video guid 修复范围内。）"""
        vg = ep_video_guid(self.tv_eps[0], LINE)
        await self.client.request(
            "PUT", "/v/api/v1/item/favorite", json={"guid": vg},
            headers=ADMIN)
        self.assertEqual(len(await self.favorite_list()), 1)
        r = await self.client.request(
            "DELETE", "/v/api/v1/item/favorite",
            json={"guid": vg},
            headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertEqual(await self.favorite_list(), [])

    async def test_watched_by_video_guid(self):
        """video guid 标已看：应成功落到本地（按集 guid 存，与线路卡
        收藏/已看的既有口径一致），不再透传官方丢掉。"""
        r = await self.client.request(
            "POST", "/v/api/v1/item/watched",
            json={"guid": ep_video_guid(self.tv_eps[0], LINE)},
            headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertTrue(
            self.marks.is_watched(self.tv_eps[0], user="admin-guid"))

    async def test_normalize_video_guid_forms(self):
        """归一化（需已播种的 store）：video/media guid → 集 guid，
        标题级 video guid → 标题 guid。"""
        eg = self.tv_eps[3]
        self.assertEqual(
            appmod._normalize_history_guid(ep_video_guid(eg, LINE)), eg)
        self.assertEqual(
            appmod._normalize_history_guid(ep_media_guid(eg, LINE)), eg)
        self.assertEqual(
            appmod._normalize_history_guid(video_guid("og-tv")), "og-tv")

    async def test_movie_favorite_by_episode_video_guid(self):
        """电影线路卡的集级 video guid 收藏：应归一化到标题。"""
        r = await self.client.request(
            "PUT", "/v/api/v1/item/favorite",
            json={"guid": ep_video_guid(self.movie_eg, LINE)},
            headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        rows = await self.favorite_list()
        self.assertEqual([x["guid"] for x in rows], ["og-movie"])

    # ---- 守门：解析不到的仍透传官方，不误吞 ----
    async def test_unknown_video_guid_still_forwards(self):
        """不在 store 里的 video guid：不上报本地、不留记录。"""
        unknown = ep_video_guid(episode_guid("og-nope", "", 0), LINE)
        r = await self.record(unknown)
        # 透传上游：mock 上游对未知路径返回 data []，不是本地 True
        self.assertEqual(r.json()["data"], [], r.json())
        self.assertIsNone(self.history.get(unknown, user="admin-guid"))
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual((guid, ts), (self.tv_eps[0], 0))

    async def test_unknown_video_field_does_not_hijack_episode_record(self):
        """body video 字段是未知 guid、item_guid 是有效集 guid 时，
        仍按 item_guid 记录，不被未知字段带偏、不透传。"""
        r = await self.record(
            self.tv_eps[2],
            video_guid=ep_video_guid(episode_guid("og-nope", "", 0), LINE))
        self.assert_record_kept(r)
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[2])
        self.assertEqual(ts, 600)


class NormalizeVideoGuidTests(unittest.TestCase):
    def test_normalize_unknown_video_guid_unchanged(self):
        # 不在 store 里的 video guid：原样返回，调用方守门仍透传官方。
        unknown = ep_video_guid(episode_guid("og-nope", "", 0), LINE)
        self.assertEqual(appmod._normalize_history_guid(unknown), unknown)


if __name__ == "__main__":
    unittest.main()
