"""Resume round 3：真实 duration——客户端每次 record 都报 duration=0，
服务端 play/info 又只给 duration=0，续播守门拿不到合法时长。

诊断定性（真机）：《伟大的长征》第 1 集 record 全部 outcome: history、
parsed: episode、分区一致，play/info 也返回了正确 ts（1097、941），
但下一条 record 就是 ts: 1，且每条 record 的 duration=0。

本文件先写失败测试：在基线 09f6fbc 上，带 CMS 时长 / 伪造 m3u8 探测的
用例应失败（duration 仍为 0），修完应全部通过。

时长来源优先级（实现约定）：
  1. CMS vod_duration（苹果 CMS V10 标准字段，模板标签 {$obj.vod_duration}=时长）
     ——标题级；电影=全片时长，剧集=单集时长；
  2. 已缓存的 HLS playlist 的 EXTINF 求和（_get_playlist_cached 已取到的，
     不新增上游请求；无缓存时 stream/list 上限 2s 等同一单飞任务）；
  3. history 里已存下的 record duration（客户端此前报过 >0 时复用）。
探测到的时长缓存到 brief["ep_durations"][str(index)]（电影直接写
brief["duration"]），随 titles.json 持久化，不再重复拉流。
"""
from __future__ import annotations

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
import inject as injmod
import marks as marksmod
import providers as provmod
import store as storemod
import t4_service
from store import episode_guid, ep_media_guid, ep_video_guid

ADMIN = {"Authorization": "Bearer admin-token"}
LINE = "lzi::lzm3u8"

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
# 10.0 + 9.5 + 10.5 = 30 秒
FAKE_M3U8_SECONDS = 30


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
    p = request.url.path
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


class DurationTests(unittest.IsolatedAsyncioTestCase):
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
        # 默认入库：带 CMS 时长的剧（2700s/集）与电影（5400s），以及
        # 一部无时长剧（走 playlist 探测）与一部无时长电影。
        self.store.remember_merged([
            tv_brief(duration=2700),
            movie_brief(duration=5400),
            tv_brief(guid="og-tv-nodur", title="无时长剧"),
            movie_brief(guid="og-movie-nodur", title="无时长电影"),
        ])
        self.tv_eps = [episode_guid("og-tv", "", i) for i in range(8)]
        self.nodur_eps = [episode_guid("og-tv-nodur", "", i)
                          for i in range(8)]
        self.movie_eg = episode_guid("og-movie", "", 0)

    def tearDown(self):
        self.history.delete(
            self.tv_eps + self.nodur_eps + [self.movie_eg, "og-tv",
                                            "og-tv-nodur", "og-movie",
                                            "og-movie-nodur"],
            user="admin-guid")
        self.marks._fav = {}
        self.marks._watched = {}
        appmod._PLAYLIST_CACHE.clear()

    # ---- 小工具 ----
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

    # ---- 1. CMS 时长字段解析 ----
    def test_cms_vod_duration_seconds(self):
        v = {"vod_id": "1", "vod_name": "剧",
             "vod_play_from": "lzm3u8",
             "vod_play_url": "第1集$https://x.test/v.m3u8",
             "vod_duration": "2700"}
        t = provmod._map_vod(v, {"id": "lzi",
                                 "api": "https://x.test/api"})
        self.assertEqual(t.duration, 2700)

    def test_cms_vod_duration_minutes_text(self):
        v = {"vod_id": "1", "vod_name": "剧",
             "vod_play_from": "lzm3u8",
             "vod_play_url": "第1集$https://x.test/v.m3u8",
             "vod_duration": "45分钟"}
        t = provmod._map_vod(v, {"id": "lzi",
                                 "api": "https://x.test/api"})
        self.assertEqual(t.duration, 2700)

    def test_cms_vod_duration_hms_text(self):
        v = {"vod_id": "1", "vod_name": "剧",
             "vod_play_from": "lzm3u8",
             "vod_play_url": "第1集$https://x.test/v.m3u8",
             "vod_duration": "01:30:00"}
        t = provmod._map_vod(v, {"id": "lzi",
                                 "api": "https://x.test/api"})
        self.assertEqual(t.duration, 5400)

    def test_cms_vod_duration_absent_is_zero(self):
        v = {"vod_id": "1", "vod_name": "剧",
             "vod_play_from": "lzm3u8",
             "vod_play_url": "第1集$https://x.test/v.m3u8"}
        t = provmod._map_vod(v, {"id": "lzi",
                                 "api": "https://x.test/api"})
        self.assertEqual(t.duration, 0)

    def test_cms_xml_duration_mapped(self):
        xml = ("<rss><list><video><id>1</id><name>剧</name>"
               "<duration>2700</duration>"
               "<dl><dd flag=\"lzm3u8\"><![CDATA[第1集$https://x.test/v.m3u8]]></dd></dl>"
               "</video></list></rss>")
        j = provmod.parse_cms_xml(xml)
        self.assertEqual(j["list"][0].get("vod_duration"), "2700")

    def test_brief_of_carries_cms_duration(self):
        v = {"vod_id": "1", "vod_name": "剧",
             "vod_play_from": "lzm3u8",
             "vod_play_url": "第1集$https://x.test/v.m3u8",
             "vod_duration": "2700"}
        t = provmod._map_vod(v, {"id": "lzi",
                                 "api": "https://x.test/api"})
        self.assertEqual(injmod.brief_of(t)["duration"], 2700)

    # ---- 2. playlist EXTINF 求和 ----
    def test_playlist_extinf_sum(self):
        self.assertEqual(appmod._playlist_total_seconds(FAKE_M3U8),
                         FAKE_M3U8_SECONDS)

    def test_playlist_master_without_extinf_is_zero(self):
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
                  "low.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
                  "high.m3u8\n")
        self.assertEqual(appmod._playlist_total_seconds(master), 0)

    def test_playlist_empty_is_zero(self):
        self.assertEqual(appmod._playlist_total_seconds(""), 0)
        self.assertEqual(appmod._playlist_total_seconds(None), 0)

    # ---- 3. play/info 带上真实 duration（CMS 来源） ----
    async def test_play_info_tv_returns_cms_duration(self):
        info = await self.play_info(self.tv_eps[0])
        self.assertEqual(info.get("duration"), 2700)
        self.assertEqual((info.get("item") or {}).get("duration"), 2700)

    async def test_play_info_movie_returns_cms_duration(self):
        info = await self.play_info("og-movie")
        self.assertEqual(info.get("duration"), 5400)
        self.assertEqual((info.get("item") or {}).get("duration"), 5400)

    async def test_play_info_title_resume_still_returns_ts(self):
        """有 CMS 时长时续播位置不能丢：先记 941s 再查 play/info。"""
        await self.record(self.tv_eps[0], ts=941, duration=2700)
        info = await self.play_info("og-tv")
        self.assertEqual(info.get("guid"), self.tv_eps[0])
        self.assertEqual(info.get("ts"), 941)
        self.assertEqual(info.get("duration"), 2700)

    # ---- 4. stream/list video_streams 带上真实 duration ----
    async def test_stream_list_tv_cms_duration(self):
        sl = await self.stream_list(self.tv_eps[0])
        vs = sl.get("video_streams") or []
        self.assertTrue(vs, sl)
        self.assertEqual(vs[0].get("duration"), 2700)

    async def test_stream_list_movie_cms_duration(self):
        sl = await self.stream_list("og-movie")
        vs = sl.get("video_streams") or []
        self.assertTrue(vs, sl)
        self.assertEqual(vs[0].get("duration"), 5400)

    # ---- 5. 无 CMS 时长时走 playlist 探测，并缓存到集上 ----
    async def test_stream_list_playlist_probe_and_cache(self):
        key = appmod._playlist_cache_key(
            {"online_guid": "og-tv-nodur", "line": LINE, "index": 0})
        appmod._PLAYLIST_CACHE[key] = (time.time(), FAKE_M3U8)
        sl = await self.stream_list(self.nodur_eps[0])
        vs = sl.get("video_streams") or []
        self.assertTrue(vs, sl)
        self.assertEqual(vs[0].get("duration"), FAKE_M3U8_SECONDS)
        # 缓存到集记录上：清掉 playlist 缓存后 play/info 仍能给出时长
        appmod._PLAYLIST_CACHE.clear()
        info = await self.play_info(self.nodur_eps[0])
        self.assertEqual(info.get("duration"), FAKE_M3U8_SECONDS)

    async def test_stream_list_probe_failure_falls_back_zero(self):
        # 无 CMS 时长、无 playlist 缓存、且上游抓不到清单：
        # 不阻塞、不抛错，duration 回退 0。
        sl = await self.stream_list(self.nodur_eps[1])
        vs = sl.get("video_streams") or []
        self.assertTrue(vs, sl)
        self.assertEqual(vs[0].get("duration"), 0)

    # ---- 6. play/list 合并记录：无 record duration 时用 brief 时长兜底 ----
    async def test_play_list_uses_brief_duration_when_record_zero(self):
        # 模拟诊断现场：客户端只报了 ts，duration=0
        await self.record(self.tv_eps[0], ts=941, duration=0)
        items = await self.play_list()
        mine = [x for x in items if x.get("guid") == self.tv_eps[0]]
        self.assertTrue(mine, items)
        self.assertEqual(mine[0].get("duration"), 2700)
        self.assertEqual(mine[0].get("ts"), 941)
        self.assertEqual(mine[0].get("runtime"), 45)

    # ---- 7. 全链路：stream/list 给出时长 → 客户端报出 >0 → 续播 progress 不再为 0 ----
    async def test_full_chain_record_stores_duration_and_progress(self):
        sl = await self.stream_list(self.tv_eps[0])
        dur = (sl.get("video_streams") or [{}])[0].get("duration")
        self.assertEqual(dur, 2700)
        # 客户端按 stream/list 给的时长上报
        r = await self.record(self.tv_eps[0], ts=941, duration=dur)
        self.assertEqual(r.json()["code"], 0, r.json())
        rec = self.history.get(self.tv_eps[0], user="admin-guid")
        self.assertEqual(rec["duration"], 2700)
        cw = self.history.continue_watching(user="admin-guid")
        mine = [x for x in cw if x.get("item_guid") == self.tv_eps[0]]
        self.assertTrue(mine, cw)
        self.assertAlmostEqual(mine[0]["progress"], 941 / 2700, places=3)
        self.assertGreater(mine[0]["progress"], 0)


if __name__ == "__main__":
    unittest.main()
