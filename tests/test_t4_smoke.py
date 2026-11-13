"""T4 端到端冒烟：真实本地 HTTP 假服务端 + 真实 app。

覆盖：订阅导入 → 全局搜索出片 → 详情/季/集 → play/info → stream → play/play
取到直链。只验证链路与身份，不解码媒体。
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
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import store as storemod
import t4_media
import t4_service

CLIP = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


class FakeT4(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 安静
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
        if parts.path != "/t4":
            self._send(404, {"error": "not found"})
            return
        base = f"http://127.0.0.1:{self.server.server_port}"
        if "wd" in query:
            rows = [{"vod_id": "smoke-1", "vod_name": "冒烟剧",
                     "type_name": "电视剧", "vod_year": "2026",
                     "vod_pic": f"{base}/media/poster.jpg"}] \
                if "冒烟" in query["wd"] else []
            self._send(200, {"list": rows})
        elif query.get("ac") == "detail":
            self._send(200, {"list": [{
                "vod_id": "smoke-1", "vod_name": "冒烟剧", "type_name": "电视剧",
                "vod_year": "2026", "vod_content": "冒烟简介",
                "vod_play_from": "A", "vod_play_url": "第01集$ep1#第02集$ep2"}]})
        elif "play" in query:
            self._send(200, {"url": f"{base}/media/clip.mp4", "parse": 0})
        else:
            self._send(200, {"class": [{"type_id": "series", "type_name": "电视剧"}],
                            "list": [{"vod_id": "smoke-1", "vod_name": "冒烟剧",
                                      "type_name": "电视剧"}]})


class T4SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeT4)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.api = f"http://127.0.0.1:{self.port}/t4"
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE", Path(temp.name) / (filename + ".json"))
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
        # fail-open：未识别会话默认可见；本类测的是全流程搜索/播放，
        # 以管理员身份请求。
        item = patch.object(appmod.fnusersmod, "resolve", return_value={
            "known": True, "guid": "admin-guid", "name": "admin",
            "is_admin": True, "token": "tok-admin"})
        item.start()
        self.addCleanup(item.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        self.network = httpx.AsyncClient(trust_env=False, timeout=10)
        self.addAsyncCleanup(self.network.aclose)
        appmod.app.state.ext = self.network
        appmod.app.state.up = self.network
        self.addAsyncCleanup(appmod.t4_media.metadata.close)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                        base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    async def test_full_flow_search_detail_stream_play(self):
        # 1) 订阅导入 T4 源
        r = await self.client.post("/v/api/ext/source/import-subscription", json={
            "text": json.dumps({"sites": [{"type": 4, "name": "冒烟T4",
                                           "api": self.api}]})})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["t4"]["added"], 1)

        # 2) 全局搜索出片
        rows = (await self.client.get("/v/api/v1/search/list",
                                      params={"q": "冒烟剧"})).json()["data"]
        self.assertEqual(len(rows), 1)
        # fork 的搜索卡片标题带【在线】前缀
        self.assertIn("冒烟剧", rows[0]["title"])
        guid = rows[0]["guid"]

        # 3) 详情 + 季 + 集
        detail = (await self.client.get(f"/v/api/v1/item/{guid}")).json()["data"]
        self.assertEqual(detail["type"], "TV")
        self.assertIn("冒烟简介", detail["overview"])
        seasons = (await self.client.get(f"/v/api/v1/season/list/{guid}")).json()["data"]
        self.assertEqual(len(seasons), 1)
        episodes = (await self.client.get(
            f"/v/api/v1/episode/list/{seasons[0]['guid']}")).json()["data"]
        self.assertEqual([e["title"] for e in episodes], ["第01集", "第02集"])
        eg = episodes[0]["guid"]

        # 4) play/info + stream 菜单
        info = (await self.client.post("/v/api/v1/play/info",
                                       json={"item_guid": eg})).json()["data"]
        self.assertEqual(info["item"]["tv_title"], "冒烟剧")
        mg = storemod.ep_media_guid(eg)
        stream = (await self.client.get(f"/v/api/v1/stream/list/{eg}")).json()["data"]
        self.assertTrue(stream["video_streams"])

        # 5) play/play 取到播放链：媒体 host 是私网地址（127.0.0.1），按设计
        # 走服务端流式代理（/v/media/online/<mg>/t4），再沿该地址真实取到字节。
        play = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "video_guid": storemod.ep_video_guid(eg)})).json()["data"]
        link = play["play_link"]
        self.assertTrue(link.startswith(
            f"http://fnmedia.test/v/media/online/{mg}/t4"), link)
        from urllib.parse import urlsplit as _split
        path = _split(link).path + ("?" + _split(link).query if _split(link).query else "")
        got = await self.client.get(path)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.content, CLIP)


if __name__ == "__main__":
    unittest.main()
