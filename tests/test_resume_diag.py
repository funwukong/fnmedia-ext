"""续播诊断端点（分支 resume2-diag）。

GET /v/api/ext/diag/resume：最近 20 条续播事件（内存环形缓冲）+
指定 guid 在各分区里的 history 记录；只读，不写盘，不含任何凭证原文。

脚手架复用 tests/test_p0p1_resume_favorite.py 的 fake-store/管理员 viewer
模式：上游官方后端用 MockTransport 假装，admin-token 解析为 admin-guid。
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
import marks as marksmod
import resume_diag as resume_diagmod
import store as storemod
import t4_service
from store import episode_guid

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


class DiagTests(unittest.IsolatedAsyncioTestCase):
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
        self.store.remember_merged([tv_brief()])
        self.tv_eps = [episode_guid("og-tv", "", i) for i in range(8)]
        resume_diagmod.clear()

    def tearDown(self):
        self.history.delete(self.tv_eps + ["og-tv"], user="admin-guid")
        resume_diagmod.clear()

    # ---- 小工具 ----
    async def diag(self, guid=""):
        q = f"?guid={guid}" if guid else ""
        r = await self.client.get("/v/api/ext/diag/resume" + q,
                                  headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def record(self, ig, ts=600, duration=3600):
        return await self.client.post("/v/api/v1/play/record", json={
            "item_guid": ig, "media_guid": "mg", "ts": ts,
            "duration": duration}, headers=ADMIN)

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        d = r.json().get("data") or {}
        return d.get("guid"), d.get("ts"), d.get("type")

    # ---- 端点形状 ----
    async def test_diag_empty_shape(self):
        body = await self.diag()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["events"], [])
        self.assertEqual(body["data"]["lookup"], {})

    async def test_diag_lookup_without_guid_is_empty(self):
        await self.record(self.tv_eps[0])
        body = await self.diag()
        self.assertEqual(body["data"]["lookup"], {})

    async def test_diag_lookup_unknown_guid_is_empty(self):
        body = await self.diag("no-such-guid")
        self.assertEqual(body["data"]["lookup"], {})

    # ---- record 钩子 ----
    async def test_record_adds_event_with_raw_guid_and_partition(self):
        folder = appmod._line_folder_guid("og-tv")
        r = await self.record(folder)
        self.assertEqual(r.json()["code"], 0)
        body = await self.diag()
        recs = [e for e in body["data"]["events"]
                if e["kind"] == "record"]
        self.assertEqual(len(recs), 1)
        ev = recs[0]
        self.assertEqual(ev["raw_guid"], folder)
        self.assertEqual(ev["guid"], "og-tv")
        self.assertEqual(ev["parsed"], "title")
        self.assertEqual(ev["partition"], "admin-guid")
        self.assertEqual(ev["outcome"], "history")
        self.assertEqual(ev["ts"], 600)
        self.assertEqual(ev["duration"], 3600)
        self.assertIn("at", ev)

    async def test_record_unresolved_goes_upstream(self):
        r = await self.record("no-such-guid")
        self.assertEqual(r.json()["code"], 0)
        body = await self.diag()
        recs = [e for e in body["data"]["events"]
                if e["kind"] == "record"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["parsed"], "unresolved")
        self.assertEqual(recs[0]["outcome"], "upstream")

    # ---- play/info 钩子 ----
    async def test_play_info_adds_event_with_returned_ts(self):
        await self.record(self.tv_eps[3])
        guid, ts, _ = await self.play_info("og-tv")
        self.assertEqual(guid, self.tv_eps[3])
        self.assertEqual(ts, 600)
        body = await self.diag()
        infos = [e for e in body["data"]["events"]
                 if e["kind"] == "info"]
        self.assertEqual(len(infos), 1)
        ev = infos[0]
        self.assertEqual(ev["guid"], "og-tv")
        self.assertEqual(ev["resolved_guid"], self.tv_eps[3])
        self.assertEqual(ev["ts"], 600)
        self.assertEqual(ev["partition"], "admin-guid")

    async def test_play_info_direct_episode_adds_event(self):
        await self.record("og-tv")
        guid, ts, _ = await self.play_info(self.tv_eps[0])
        self.assertEqual(guid, self.tv_eps[0])
        self.assertEqual(ts, 600)
        body = await self.diag()
        infos = [e for e in body["data"]["events"]
                 if e["kind"] == "info"]
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0]["guid"], self.tv_eps[0])
        self.assertEqual(infos[0]["ts"], 600)

    # ---- lookup ----
    async def test_lookup_by_guid_finds_record(self):
        await self.record(self.tv_eps[3], ts=600, duration=3600)
        body = await self.diag(self.tv_eps[3])
        lookup = body["data"]["lookup"]
        self.assertEqual(set(lookup), {"admin-guid"})
        rec = lookup["admin-guid"][self.tv_eps[3]]
        self.assertEqual(set(rec), {"ts", "duration", "updated_at"})
        self.assertEqual(rec["ts"], 600)
        self.assertEqual(rec["duration"], 3600)
        self.assertIsInstance(rec["updated_at"], float)

    async def test_lookup_by_folder_guid_finds_normalized_record(self):
        """客户端拿文件夹卡 guid 查，也能看到落在标题键上的记录。"""
        folder = appmod._line_folder_guid("og-tv")
        await self.record(folder, ts=600, duration=3600)
        body = await self.diag(folder)
        lookup = body["data"]["lookup"]
        self.assertEqual(set(lookup), {"admin-guid"})
        self.assertEqual(lookup["admin-guid"]["og-tv"]["ts"], 600)


if __name__ == "__main__":
    unittest.main()
