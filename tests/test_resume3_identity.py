"""Resume round 3：跨线身份统一——同一部剧/电影换源后续播不断。

定性背景（真机诊断 JSON）：《伟大的长征》第 1 集进度确实存下来了，
play/info 也返回了正确位置，但播放每次仍从头开始；且跨线身份碎裂：
同一部剧第 1 集，ffzy 线的集 guid 与 dytt 线的不同——因为标题 guid
（og）= md5("on_<provider>_<vod_id>")，分源不同 og 不同，集 guid
= md5("ep:<og>:<index>") 也不同。ffzy 看到 1097 秒，换 dytt 线就是
一张白纸（ts=0）。

本文件先写失败测试：以下除对照组与 marks 核实组外，在基线 09f6fbc
上应全部失败，修完应全部通过。修复只动 proxy/history.py（规范键
+ 回退链），不改 _normalize_history_guid、不改 marks、不改 app.py。
"""
from __future__ import annotations

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
import config as configmod
import history as histmod
import marks as marksmod
import store as storemod
import t4_service
from history import History
from store import episode_guid

ADMIN = {"Authorization": "Bearer admin-token"}
FFZY = "ffzy::ffzy"
DYTT = "dytt::dytt"
TITLE = "伟大的长征"


def tv_brief(guid, title, line, n=8):
    eps = [{"title": f"第{i + 1:02d}集",
            "url": f"https://{line.split('::')[0]}.test/v{i}.m3u8",
            "direct": True} for i in range(n)]
    return {"guid": guid, "title": title, "year": "2024",
            "type_name": "电视剧", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {line: eps}}


def movie_brief(guid, title, line):
    return {"guid": guid, "title": title, "year": "2024",
            "type_name": "电影", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {line: [{"title": "正片",
                              "url": f"https://{line.split('::')[0]}.test/v.m3u8",
                              "direct": True}]}}


def t4_brief(guid, title, source_id, ep_titles, line="A"):
    eps = [{"title": t, "url": f"https://t4.test/{guid}/{i}",
            "direct": True, "play_from": line}
           for i, t in enumerate(ep_titles)]
    return {"guid": guid, "title": title, "year": "2024",
            "type_name": "电视剧", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "locator": {"kind": "t4", "source_id": source_id,
                        "vod_id": guid, "detail_loaded": True},
            "lines": {line: eps}}


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
def as_history_user(guid, admin=False):
    tokens = histmod.set_current_user(guid, admin)
    try:
        yield
    finally:
        histmod.reset_current_user(tokens)


class CrossLineResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._tmp = Path(temp.name)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history"), (marksmod, "marks")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [
            {"id": "t4demo", "kind": "t4", "enabled": True,
             "name": "Test T4 A", "api": "http://t4.test/api"},
            {"id": "t4srcB", "kind": "t4", "enabled": True,
             "name": "Test T4 B", "api": "http://t4.test/api"}],
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
        self.store.remember_merged([
            tv_brief("og-ffzy", TITLE, FFZY),
            tv_brief("og-dytt", TITLE, DYTT),
            tv_brief("og-other", "另一部剧", FFZY),
            tv_brief("og-var-a", "变体剧", FFZY),
            tv_brief("og-var-b", "变体剧（2024）", DYTT),
            movie_brief("og-mv-ffzy", "测试电影", FFZY),
            movie_brief("og-mv-dytt", "测试电影", DYTT),
            t4_brief("og-t4a", "T4长征", "t4demo",
                     [f"第{i + 1}集" for i in range(3)]),
            t4_brief("og-t4b", "T4长征", "t4srcB",
                     [f"第{i + 1}集" for i in range(3)]),
            t4_brief("og-t4s1", "多季剧", "t4demo",
                     ["第1季第1集", "第1季第2集",
                      "第2季第1集", "第2季第2集", "第2季第3集"]),
            t4_brief("og-t4s2", "多季剧", "t4srcB",
                     ["第1季第1集", "第1季第2集",
                      "第2季第1集", "第2季第2集", "第2季第3集"]),
        ])
        for og in ("og-t4a", "og-t4b", "og-t4s1", "og-t4s2"):
            appmod.t4_service._fresh[og] = time.monotonic()
        self.ffzy_eps = [episode_guid("og-ffzy", "", i) for i in range(8)]
        self.dytt_eps = [episode_guid("og-dytt", "", i) for i in range(8)]
        assert self.ffzy_eps[0] != self.dytt_eps[0], "两源集 guid 必须不同"
        self.t4a_eps = self.store.tv_episode_guids("og-t4a")
        self.t4b_eps = self.store.tv_episode_guids("og-t4b")
        self.t4s1_eps = self.store.tv_episode_guids("og-t4s1")
        self.t4s2_eps = self.store.tv_episode_guids("og-t4s2")
        assert len(self.t4a_eps) == 3 and len(self.t4b_eps) == 3
        assert len(self.t4s1_eps) == 5 and len(self.t4s2_eps) == 5

    def tearDown(self):
        self.history.delete(
            self.ffzy_eps + self.dytt_eps + self.t4a_eps + self.t4b_eps
            + self.t4s1_eps + self.t4s2_eps
            + ["og-ffzy", "og-dytt", "og-other", "og-var-a", "og-var-b",
               "og-mv-ffzy", "og-mv-dytt", "og-t4a", "og-t4b",
               "og-t4s1", "og-t4s2"], user="admin-guid")
        self.marks._fav = {}
        self.marks._watched = {}

    async def record(self, ig, ts=600, duration=3600, **extra):
        body = {"item_guid": ig, "ts": ts, "duration": duration}
        body.update(extra)
        r = await self.client.post("/v/api/v1/play/record", json=body,
                                   headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertTrue(r.json()["data"], r.json())
        return r

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        d = r.json().get("data") or {}
        return d.get("guid"), d.get("ts"), d.get("type")

    # ---- 对照组：同源续播基线已通过，修完不能坏 ----
    async def test_same_line_episode_resume(self):
        await self.record(self.ffzy_eps[0], ts=1097)
        guid, ts, _ = await self.play_info("og-ffzy")
        self.assertEqual((guid, ts), (self.ffzy_eps[0], 1097))

    async def test_same_line_movie_resume(self):
        await self.record("og-mv-ffzy", ts=600)
        guid, ts, typ = await self.play_info("og-mv-ffzy")
        self.assertEqual((guid, ts, typ), ("og-mv-ffzy", 600, "Movie"))

    # ---- 复现：跨线剧集续播（基线失败） ----
    async def test_cross_line_episode1_resumes(self):
        """用户原案：ffzy 第 1 集看到 1097 秒，换 dytt 线打开应续播同一位置。"""
        await self.record(self.ffzy_eps[0], ts=1097)
        guid, ts, _ = await self.play_info("og-dytt")
        self.assertEqual(guid, self.dytt_eps[0])
        self.assertEqual(ts, 1097)

    async def test_cross_line_episode_guid_direct_resumes(self):
        """直接点 dytt 第 1 集的 play/info，也应拿到 ffzy 的进度。"""
        await self.record(self.ffzy_eps[0], ts=1097)
        guid, ts, _ = await self.play_info(self.dytt_eps[0])
        self.assertEqual((guid, ts), (self.dytt_eps[0], 1097))

    async def test_cross_line_later_episode_resumes(self):
        """不是第 1 集也要跨线：第 4 集 ffzy 600 秒，dytt 应续播第 4 集 600 秒。"""
        await self.record(self.ffzy_eps[3], ts=600)
        guid, ts, _ = await self.play_info("og-dytt")
        self.assertEqual((guid, ts), (self.dytt_eps[3], 600))

    async def test_cross_line_bidirectional_and_max_wins(self):
        """双向：dytt 再报同一集更大进度后，ffzy 侧也应看到更大值。"""
        await self.record(self.ffzy_eps[0], ts=500)
        await self.record(self.dytt_eps[0], ts=900)
        guid, ts, _ = await self.play_info("og-ffzy")
        self.assertEqual((guid, ts), (self.ffzy_eps[0], 900))

    async def test_cross_line_title_suffix_variant_resumes(self):
        """标题只差括号年份后缀（归一化后同一标题）也要跨线。"""
        await self.record(episode_guid("og-var-a", "", 0), ts=600)
        guid, ts, _ = await self.play_info("og-var-b")
        self.assertEqual((guid, ts), (episode_guid("og-var-b", "", 0), 600))

    async def test_no_cross_title_leak(self):
        """不同标题绝不能串：另一部剧的 play/info 仍从头开始。"""
        await self.record(self.ffzy_eps[0], ts=1097)
        guid, ts, _ = await self.play_info("og-other")
        self.assertEqual((guid, ts), (episode_guid("og-other", "", 0), 0))

    async def test_cross_line_finished_episode_moves_on(self):
        """跨线看完第 1 集后，换线应播第 2 集 0 秒（与同源语义一致）。"""
        await self.record(self.ffzy_eps[0], ts=3600, duration=3600)
        guid, ts, _ = await self.play_info("og-dytt")
        self.assertEqual((guid, ts), (self.dytt_eps[1], 0))

    # ---- 复现：跨线电影（基线失败） ----
    async def test_cross_line_movie_resumes(self):
        await self.record("og-mv-ffzy", ts=600)
        guid, ts, typ = await self.play_info("og-mv-dytt")
        self.assertEqual((guid, ts, typ), ("og-mv-dytt", 600, "Movie"))

    # ---- 复现：跨 T4 源（基线失败） ----
    async def test_cross_line_t4_episode_resumes(self):
        await self.record(self.t4a_eps[1], ts=600)
        guid, ts, _ = await self.play_info("og-t4b")
        self.assertEqual((guid, ts), (self.t4b_eps[1], 600))

    async def test_cross_line_t4_multiseason_resumes(self):
        """多季：第 2 季第 3 集在 A 源看到 600 秒，B 源应续播第 2 季第 3 集。"""
        await self.record(self.t4s1_eps[4], ts=600)
        guid, ts, _ = await self.play_info("og-t4s2")
        self.assertEqual((guid, ts), (self.t4s2_eps[4], 600))
        # 且不能串到第 1 季第 3 集（不存在）或第 2 季第 1 集
        self.assertNotEqual(guid, self.t4s2_eps[2])

    # ---- 旧键回退：预先按旧分源 guid 存的记录必须仍能查到 ----
    async def test_legacy_old_key_record_found_cross_line(self):
        """模拟升级前已存的旧记录：直接以旧分源 guid 为键写入 bucket；
        换线 play/info 必须找到它，且读后应迁移到规范键下（旧键消失）。"""
        legacy = {"item_guid": self.ffzy_eps[0], "ts": 1097,
                  "duration": 3600, "updated_at": 1000.0,
                  "media_guid": "", "video_guid": "",
                  "audio_guid": "", "subtitle_guid": "", "resolution": ""}
        with as_history_user("admin-guid", admin=True):
            self.history._bucket(user="admin-guid")[self.ffzy_eps[0]] = legacy
        guid, ts, _ = await self.play_info("og-dytt")
        self.assertEqual((guid, ts), (self.dytt_eps[0], 1097))
        with as_history_user("admin-guid", admin=True):
            bucket = self.history._bucket(user="admin-guid")
            self.assertNotIn(self.ffzy_eps[0], bucket)
            canon = [k for k in bucket if k.startswith("canon:")]
            self.assertEqual(len(canon), 1)
            self.assertEqual(bucket[canon[0]]["ts"], 1097)
            # 旧 guid 直接查也还在（回退链），不丢记录
            self.assertEqual(self.history.get(self.ffzy_eps[0])["ts"], 1097)

    async def test_legacy_old_key_survives_restart(self):
        """旧记录落盘后再重启 History：不破坏性重写，换线仍能读到。"""
        legacy = {"item_guid": self.ffzy_eps[0], "ts": 1097,
                  "duration": 3600, "updated_at": 1000.0,
                  "media_guid": "", "video_guid": "",
                  "audio_guid": "", "subtitle_guid": "", "resolution": ""}
        with as_history_user("admin-guid", admin=True):
            self.history._bucket(user="admin-guid")[self.ffzy_eps[0]] = legacy
        # 直接插桶绕过了 record() 的脏标记，手动置脏再落盘（模拟旧版已落盘）
        self.history._dirty = True
        self.history.flush()
        h2 = histmod.History()
        with as_history_user("admin-guid", admin=True):
            rec = h2.get(self.dytt_eps[0])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["ts"], 1097)

    async def test_new_record_stored_under_canon_key(self):
        """新记录一律按规范键存：bucket 里只有 canon: 键，item_guid
        仍保留原始分源 guid（play/list 与 continue_watching 靠它回查 store）。"""
        await self.record(self.ffzy_eps[0], ts=1097)
        with as_history_user("admin-guid", admin=True):
            bucket = self.history._bucket(user="admin-guid")
            self.assertEqual(list(bucket), ["canon:tv:伟大的长征:s1:e1"])
            rec = next(iter(bucket.values()))
            self.assertEqual(rec["item_guid"], self.ffzy_eps[0])

    async def test_continue_watching_dedupes_cross_line(self):
        """两源各报同一集进度后，继续观看只应出现一次（不重复）。"""
        await self.record(self.ffzy_eps[0], ts=500)
        await self.record(self.dytt_eps[0], ts=900)
        with as_history_user("admin-guid", admin=True):
            rows = self.history.continue_watching()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], 900)

    async def test_delete_by_old_guid_removes_canon_record(self):
        """按旧分源 guid 删进度：规范键下的记录也要删掉，否则删完又复活。"""
        await self.record(self.ffzy_eps[0], ts=600)
        r = await self.client.request(
            "DELETE", "/v/api/v1/play/record",
            json={"item_guid": self.dytt_eps[0]}, headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        guid, ts, _ = await self.play_info("og-ffzy")
        self.assertEqual((guid, ts), (self.ffzy_eps[0], 0))

    # ---- marks 分源碎裂：只核实、不改（本测试断言现状，供另开任务） ----
    async def test_marks_fragmentation_verified_only(self):
        """收藏仍按分源 guid 存：ffzy 标题收藏后，dytt 标题查不到。
        本轮只核实并报告，不改 marks；此测试断言现状，通过即表示
        碎裂确实存在、另开任务再定。"""
        r = await self.client.request(
            "PUT", "/v/api/v1/item/favorite",
            json={"guid": "og-ffzy"}, headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertTrue(self.marks.is_favorite("og-ffzy",
                                                user="admin-guid"))
        self.assertFalse(self.marks.is_favorite("og-dytt",
                                                user="admin-guid"))
        r = await self.client.request(
            "PUT", "/v/api/v1/item/favorite",
            json={"guid": self.ffzy_eps[0]}, headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertFalse(self.marks.is_watched(self.dytt_eps[0],
                                                user="admin-guid")
                         or self.marks.is_favorite(self.dytt_eps[0],
                                                   user="admin-guid"))


class CanonicalKeyUnitTests(unittest.TestCase):
    def test_key_normalize_title(self):
        norm = histmod.key_normalize_title
        self.assertEqual(norm("伟大的长征"), norm("伟大的长征 (2024)"))
        self.assertEqual(norm("伟大的长征"), norm("伟大的长征（2024）"))
        self.assertEqual(norm("伟大的长征"), norm("伟大的长征 HD"))
        self.assertEqual(norm("伟大的长征"), norm(" 伟 大 的 长 征 "))
        self.assertEqual(norm("Test Show"), norm("test show"))
        self.assertEqual(norm("长征：伟大"), norm("长征伟大"))
        self.assertNotEqual(norm("伟大的长征"), norm("另一部剧"))
        self.assertEqual(norm(""), "")
        self.assertEqual(norm("第1集"), "")

    def test_canonical_episode_key_forms(self):
        key = histmod.canonical_episode_key
        self.assertEqual(key(TITLE, 1, 1), "canon:tv:伟大的长征:s1:e1")
        self.assertEqual(key(TITLE, 2, 3, "season:2:episode:3"),
                         "canon:tv:伟大的长征:s2:e3")
        self.assertEqual(key(TITLE, 1, 3, "episode:3"),
                         "canon:tv:伟大的长征:s1:e3")
        # T4 label 行：无集号时用 label 本身做集身份
        self.assertEqual(key(TITLE, 1, 0, "label:花絮"),
                         "canon:tv:伟大的长征:s1:label:花絮")
        # T4 variant 行（同一集两个编码）：去掉线路 hash，落到同一 label 键
        self.assertEqual(
            key(TITLE, 1, 0, "season:1:variant:0123456789abcdef:花絮"),
            "canon:tv:伟大的长征:s1:label:花絮")
        self.assertEqual(histmod.canonical_movie_key(TITLE),
                         "canon:movie:伟大的长征")
        self.assertEqual(histmod.canonical_title_key(TITLE),
                         "canon:title:伟大的长征")
        self.assertIsNone(key("", 1, 1))
        self.assertIsNone(key(TITLE, 1, 0))

    def test_canonical_key_for_unknown_guid_is_none(self):
        # store 里没有的 guid：无规范键，只走旧键（不破坏既有行为）
        self.assertIsNone(histmod.canonical_key_for_guid("og-nope"))
        self.assertIsNone(histmod.canonical_key_for_guid(""))
        self.assertIsNone(histmod.canonical_key_for_guid(None))


if __name__ == "__main__":
    unittest.main()
