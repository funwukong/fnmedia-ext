"""HTTP 层核心路径测试：真实本地假服务器 + 真实 app（ASGI）。

覆盖：搜索 /v/api/v1/search/list、详情 /v/api/v1/item/{guid}、
播放 /v/api/v1/play/info + /v/api/v1/play/play、流 /v/api/v1/stream/list
与分片代理、m3u8 代理（preset/main）、海报代理（sys/img）、
/v/api/v1/mediadb/list、/v/api/v1/item/list、/v/api/ext/status、
管理页 /v/online-admin。

假源分两路，均无真实网络：
- T4 假源 + 媒体文件跑在本地 127.0.0.1 HTTP 服务器上（ext 客户端直连它）；
- 官方后端（up 客户端）用 httpx.MockTransport 假装，默认返回本地库数据，
  也可切到“上游宕机”模式（抛连接错）验证错误路径。
CMS 直链片直接 seed 进 store（remember_titles），走 preset/main 真实取清单。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import inject as injmod
import providers
import store as storemod
import t4 as t4mod
import t4_media
import t4_service

CLIP = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 100
SEG = b"\x47" * 188


class _FakeUpstream:
    """本地假服务器：T4 源 + 媒体文件（mp4 / 海报 / m3u8 / ts 分片）。"""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02),
            daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.api = f"{self.base}/t4"

    def _handler(self):
        outer = self

        class Fake(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def do_GET(self):
                self._handle()

            def do_HEAD(self):
                self._handle()

            def _handle(self):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/media/clip.mp4":
                    self._send(200, CLIP, "video/mp4")
                    return
                if parts.path == "/media/poster.jpg":
                    self._send(200, JPEG, "image/jpeg")
                    return
                if parts.path == "/media/list.m3u8":
                    pl = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
                          "#EXTINF:6.0,\nseg.ts\n#EXT-X-ENDLIST\n")
                    self._send(200, pl.encode(), "application/vnd.apple.mpegurl")
                    return
                if parts.path == "/media/seg.ts":
                    self._send(200, SEG, "video/mp2t")
                    return
                if parts.path != "/t4":
                    self._send(404, {"error": "not found"})
                    return
                if "wd" in query:
                    rows = [{"vod_id": "core-1", "vod_name": "核心剧",
                             "type_name": "电视剧", "vod_year": "2026",
                             "vod_pic": f"{outer.base}/media/poster.jpg"}] \
                        if "核心" in query["wd"] else []
                    self._send(200, {"list": rows})
                elif query.get("ac") == "detail":
                    self._send(200, {"list": [{
                        "vod_id": "core-1", "vod_name": "核心剧",
                        "type_name": "电视剧", "vod_year": "2026",
                        "vod_content": "核心简介",
                        "vod_play_from": "A",
                        "vod_play_url": "第01集$ep1#第02集$ep2"}]})
                elif "play" in query:
                    self._send(200, {"url": f"{outer.base}/media/clip.mp4",
                                     "parse": 0})
                else:
                    self._send(200, {
                        "class": [{"type_id": "series", "type_name": "电视剧"}],
                        "list": [{"vod_id": "core-1", "vod_name": "核心剧",
                                  "type_name": "电视剧"}]})

        return Fake

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class HttpCorePathsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = _FakeUpstream()
        self.addCleanup(self.fake.stop)
        self.upstream_down = False
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [], "removed_ids": []})
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
        # fail-open：未识别会话默认可见；本类测的是核心路径全流程，
        # 以管理员身份请求。
        _admin = patch.object(appmod.fnusersmod, "resolve", return_value={
            "known": True, "guid": "admin-guid", "name": "admin",
            "is_admin": True, "token": "tok-admin"})
        _admin.start()
        self.addCleanup(_admin.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        appmod._AGG_CACHE.clear()
        appmod._AGG_INFLIGHT.clear()
        # 播放列表缓存是模块级全局、键为确定性 mg：不清会在下一个测试里
        # 命中上一个已关闭假服务器的旧端口（测试隔离问题，与源码无关）。
        appmod._PLAYLIST_CACHE.clear()
        # 榜单后台刷新会联网抓榜单：测试内抑制，只读已缓存的榜单数据。
        _refreshing = patch.object(appmod.chart_cache, "_refreshing", True)
        _refreshing.start()
        self.addCleanup(_refreshing.stop)
        self.addAsyncCleanup(appmod.t4_media.metadata.close)

        def upstream(request):
            if self.upstream_down:
                raise httpx.ConnectError("upstream down", request=request)
            path = request.url.path
            if path == "/v/api/v1/search/list":
                q = dict(parse_qs(urlsplit(str(request.url)).query)).get("q", [""])[0]
                rows = [{"guid": "local-1", "title": "本地片"}] if "本地" in q else []
                return httpx.Response(200, json={"code": 0, "data": rows})
            if path == "/v/api/v1/mediadb/list":
                return httpx.Response(
                    200, json={"code": 0, "data": [{"guid": "local-db",
                                                    "title": "本地库"}]})
            if path == "/v/api/v1/item/list":
                return httpx.Response(200, json={"code": 0, "data": {
                    "mdb_name": "本地库", "total": 1,
                    "list": [{"guid": "local-1", "title": "本地片"}]}})
            if path.startswith("/v/api/v1/"):
                return httpx.Response(404, json={"code": -1, "msg": "not found",
                                                 "data": None})
            return httpx.Response(200, json={"code": 0, "data": []})

        self.up = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.up.aclose)
        self.network = httpx.AsyncClient(trust_env=False, timeout=10)
        self.addAsyncCleanup(self.network.aclose)
        appmod.app.state.ext = self.network
        appmod.app.state.up = self.up
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    # ---------- 夹具 ----------

    async def _import_t4(self):
        r = await self.client.post("/v/api/ext/source/import-subscription", json={
            "text": json.dumps({"sites": [{"type": 4, "name": "核心T4",
                                           "api": self.fake.api}]})})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["t4"]["added"], 1)

    async def _t4_title_guid(self):
        await self._import_t4()
        rows = (await self.client.get("/v/api/v1/search/list",
                                      params={"q": "核心剧"})).json()["data"]
        self.assertEqual(len(rows), 1)
        return rows[0]["guid"]

    async def _t4_episode(self):
        guid = await self._t4_title_guid()
        detail = (await self.client.get(f"/v/api/v1/item/{guid}")).json()["data"]
        self.assertEqual(detail["type"], "TV")
        seasons = (await self.client.get(
            f"/v/api/v1/season/list/{guid}")).json()["data"]
        episodes = (await self.client.get(
            f"/v/api/v1/episode/list/{seasons[0]['guid']}")).json()["data"]
        self.assertEqual([e["title"] for e in episodes], ["第01集", "第02集"])
        return guid, episodes[0]["guid"]

    def _seed_cms_movie(self):
        src = {"id": "probe-src", "name": "Probe",
               "api": f"{self.fake.base}/cms", "enabled": True}
        title = providers._map_vod({
            "vod_id": "m1", "vod_name": "探针片", "type_name": "电影",
            "vod_year": "2024", "vod_play_from": "A",
            "vod_play_url": f"正片${self.fake.base}/media/list.m3u8"}, src)
        briefs = self.store.remember_titles([title])
        og = briefs[0]["guid"]
        return og, storemod.media_guid(og)

    async def _cms_preset_link(self):
        og, mg = self._seed_cms_movie()
        play = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "video_guid": storemod.video_guid(og)})).json()["data"]
        return og, mg, play["play_link"]

    # ---------- 搜索 ----------

    async def test_search_local_only_when_no_online_match(self):
        r = await self.client.get("/v/api/v1/search/list", params={"q": "本地片"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([row["guid"] for row in r.json()["data"]], ["local-1"])

    async def test_search_no_match_returns_empty_list(self):
        r = await self.client.get("/v/api/v1/search/list", params={"q": "不存在的片"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], [])

    async def test_search_t4_online_result(self):
        guid = await self._t4_title_guid()
        rows = (await self.client.get("/v/api/v1/search/list",
                                      params={"q": "核心剧"})).json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertIn("核心剧", rows[0]["title"])
        self.assertEqual(rows[0]["guid"], guid)

    async def test_search_upstream_down_still_returns_online(self):
        await self._import_t4()
        self.upstream_down = True
        r = await self.client.get("/v/api/v1/search/list", params={"q": "核心剧"})
        self.assertEqual(r.status_code, 200)
        rows = r.json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertIn("核心剧", rows[0]["title"])

    async def test_search_short_pinyin_does_not_trigger_online(self):
        with patch.object(appmod, "online_search",
                          new=AsyncMock(return_value=[])) as online:
            r = await self.client.get("/v/api/v1/search/list", params={"q": "abc"})
        self.assertEqual(r.status_code, 200)
        online.assert_not_awaited()

    # ---------- 详情 ----------

    async def test_detail_cms_title(self):
        og, _mg = self._seed_cms_movie()
        r = await self.client.get(f"/v/api/v1/item/{og}")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["title"], "探针片")
        self.assertEqual(data["type"], "Movie")

    async def test_detail_t4_title_seasons_episodes(self):
        guid, eg = await self._t4_episode()
        detail = (await self.client.get(f"/v/api/v1/item/{guid}")).json()["data"]
        self.assertEqual(detail["type"], "TV")
        self.assertIn("核心简介", detail["overview"])
        seasons = (await self.client.get(
            f"/v/api/v1/season/list/{guid}")).json()["data"]
        episodes = (await self.client.get(
            f"/v/api/v1/episode/list/{seasons[0]['guid']}")).json()["data"]
        self.assertEqual(episodes[0]["guid"], eg)

    async def test_detail_unknown_guid_forwards_upstream_404(self):
        r = await self.client.get("/v/api/v1/item/does-not-exist-guid")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], -1)

    async def test_detail_t4_source_failure_returns_502(self):
        guid = await self._t4_title_guid()
        with patch.object(appmod.t4_service, "ensure_detail",
                          new=AsyncMock(side_effect=t4mod.T4Error("源站 500"))):
            r = await self.client.get(f"/v/api/v1/item/{guid}")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["code"], -1)

    # ---------- 播放 ----------

    async def test_play_info_cms_title(self):
        og, mg = self._seed_cms_movie()
        r = await self.client.post("/v/api/v1/play/info", json={"item_guid": og})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["media_guid"], mg)
        self.assertEqual(data["type"], "Movie")

    async def test_play_info_unknown_guid_forwards_upstream_404(self):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "does-not-exist-guid"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], -1)

    async def test_play_play_cms_returns_preset_link(self):
        og, mg, link = await self._cms_preset_link()
        self.assertTrue(link.startswith(f"/v/media/online/{mg}/preset.m3u8"), link)
        r = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "video_guid": storemod.video_guid(og)})
        self.assertEqual(r.json()["data"]["wrapper"], "HLS")

    async def test_play_play_t4_returns_t4_link_with_real_bytes(self):
        _guid, eg = await self._t4_episode()
        mg = storemod.ep_media_guid(eg)
        play = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg,
            "video_guid": storemod.ep_video_guid(eg)})).json()["data"]
        link = play["play_link"]
        self.assertTrue(link.startswith(f"http://fnmedia.test/v/media/online/{mg}/t4"), link)
        parts = urlsplit(link)
        path = parts.path + ("?" + parts.query if parts.query else "")
        got = await self.client.get(path)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.content, CLIP)

    async def test_play_play_unknown_media_guid_forwards_upstream_404(self):
        r = await self.client.post("/v/api/v1/play/play",
                                   json={"media_guid": "no-such-mg"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], -1)

    async def test_play_play_t4_bad_line_returns_400(self):
        _guid, eg = await self._t4_episode()
        mg = storemod.ep_media_guid(eg)
        r = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "video_guid": storemod.ep_video_guid(eg),
            "line": "no-such-line"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], -1)

    # ---------- 流 ----------

    async def test_stream_list_cms_title(self):
        og, mg = self._seed_cms_movie()
        r = await self.client.get(f"/v/api/v1/stream/list/{og}")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["video_streams"])
        self.assertEqual(data["video_streams"][0]["media_guid"], mg)

    async def test_stream_list_post_cms_title(self):
        og, mg = self._seed_cms_movie()
        r = await self.client.post("/v/api/v1/stream/list", json={"guid": og})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["data"]["video_streams"])
        self.assertEqual(r.json()["data"]["video_streams"][0]["media_guid"], mg)

    async def test_stream_list_t4_episode(self):
        _guid, eg = await self._t4_episode()
        r = await self.client.get(f"/v/api/v1/stream/list/{eg}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["data"]["video_streams"])

    async def test_stream_list_unknown_guid_forwards_upstream_404(self):
        r = await self.client.get("/v/api/v1/stream/list/does-not-exist-guid")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], -1)

    async def test_stream_segment_proxy_serves_bytes(self):
        _og, mg, _link = await self._cms_preset_link()
        main = await self.client.get(f"/v/media/online/{mg}/main.m3u8",
                                     params={"index": 0, "line": "A"})
        self.assertEqual(main.status_code, 200)
        seg_line = next(ln for ln in main.text.splitlines()
                        if "/seg?u=" in ln)
        seg = await self.client.get(seg_line)
        self.assertEqual(seg.status_code, 200)
        self.assertIn("video/mp2t", seg.headers.get("content-type", ""))
        self.assertEqual(seg.content, SEG)

    # ---------- m3u8 代理 ----------

    async def test_preset_m3u8_cms_lists_main_variant(self):
        _og, mg, link = await self._cms_preset_link()
        r = await self.client.get(link)
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/vnd.apple.mpegurl",
                      r.headers.get("content-type", ""))
        self.assertIn("#EXTM3U", r.text)
        self.assertIn("main.m3u8?index=0&line=A", r.text)

    async def test_main_m3u8_proxies_remote_playlist_rewritten(self):
        _og, mg, _link = await self._cms_preset_link()
        r = await self.client.get(f"/v/media/online/{mg}/main.m3u8",
                                  params={"index": 0, "line": "A"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("#EXTM3U", r.text)
        self.assertIn("#EXTINF:", r.text)
        self.assertIn(f"/v/media/online/{mg}/seg?u=", r.text)
        self.assertNotIn("127.0.0.1", r.text)

    async def test_main_m3u8_unbound_returns_404(self):
        r = await self.client.get("/v/media/online/no-such-mg/main.m3u8",
                                  params={"index": 0})
        self.assertEqual(r.status_code, 404)

    # ---------- 海报代理 ----------

    async def test_poster_sys_img_proxies_fake_jpeg(self):
        tok = injmod._poster(f"{self.fake.base}/media/poster.jpg").split(
            "/online/", 1)[1]
        r = await self.client.get(f"/v/api/v1/sys/img/online/{tok}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/jpeg", r.headers.get("content-type", ""))
        self.assertEqual(r.content, JPEG)

    async def test_poster_upstream_404_falls_back_to_placeholder(self):
        tok = injmod._poster(f"{self.fake.base}/media/missing.jpg").split(
            "/online/", 1)[1]
        r = await self.client.get(f"/v/api/v1/sys/img/online/{tok}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/jpeg", r.headers.get("content-type", ""))
        self.assertNotEqual(r.content, JPEG)
        self.assertTrue(len(r.content) > 1000)

    async def test_poster_bad_token_falls_back_to_placeholder(self):
        r = await self.client.get("/v/api/v1/sys/img/online/not-a-token.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/jpeg", r.headers.get("content-type", ""))
        self.assertTrue(len(r.content) > 1000)

    # ---------- mediadb / item 列表 ----------

    async def test_mediadb_list_injects_virtual_libraries(self):
        r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        guids = {row["guid"] for row in r.json()["data"]}
        self.assertIn("local-db", guids)
        self.assertTrue(set(appmod.CHART_LIB_GUIDS.values()) <= guids)

    async def test_mediadb_list_all_virtual_libs_off_returns_upstream(self):
        self.config.save({"hot_library_enabled": False,
                          "catalog_library_enabled": False})
        with patch.object(appmod, "guard_native_enabled", lambda: False):
            r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"],
                         [{"guid": "local-db", "title": "本地库"}])

    async def test_item_list_chart_library_returns_empty_wall(self):
        tv_guid = appmod.CHART_LIB_GUIDS["tv"]
        r = await self.client.post("/v/api/v1/item/list",
                                   json={"ancestor_guid": tv_guid, "page": 1})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["mdb_name"], "📺热门剧集")
        self.assertEqual(data["list"], [])
        self.assertEqual(data["total"], 0)

    async def test_item_list_unknown_guid_forwards_upstream(self):
        r = await self.client.post("/v/api/v1/item/list",
                                   json={"ancestor_guid": "unknown-guid",
                                         "page": 1})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["mdb_name"], "本地库")
        self.assertEqual([row["guid"] for row in data["list"]], ["local-1"])

    # ---------- status / 管理页 ----------

    async def test_ext_status_reports_build_and_hides_admin_token(self):
        self._seed_cms_movie()
        self.config.save({"admin_token": "test-only-token-123"})
        r = await self.client.get("/v/api/ext/status")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["build"])
        self.assertEqual(payload["cached_titles"], 1)
        self.assertIsInstance(payload["source_priority"], dict)
        self.assertNotIn("admin_token", payload["config"])
        self.assertNotIn("test-only-token-123", r.text)

    async def test_admin_page_returns_html(self):
        r = await self.client.get("/v/online-admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("content-type", ""))
        self.assertIn("fnmedia-ext 管理台", r.text)
        self.assertIn("no-store", r.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
