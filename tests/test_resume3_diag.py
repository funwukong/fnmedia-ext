"""Resume round 3：诊断端点增强（分支 resume3-diag）。

在 resume2-diag 端点与 resume3-duration / resume3-identity 两条线之上，
只补诊断字段，不动播放/续播逻辑：

- info 事件加服务端返回的 duration（秒，int，与 ts 并列；取 play/info
  返回体里的值，不另算）；
- record 事件加解析到的规范键 canon_key（取 history.record 存下的
  canon_key 字段；没有时用 history.canonical_key_for_guid 现算，
  算不到填 null）；
- GET /v/api/ext/diag/resume?guid=<g> 的 lookup：给定 guid 能解析出
  规范键时，也返回该规范键下的记录（跨线命中的那条）。

先写失败测试：在基线 37286b7 上，info 事件无 duration、record 事件
无 canon_key、跨线 lookup 查不到规范键记录，应失败；修完应全部通过。
"""
from __future__ import annotations

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
import config as configmod
import history as histmod
import marks as marksmod
import resume_diag as resume_diagmod
import store as storemod
import t4_service
from store import episode_guid

ADMIN = {"Authorization": "Bearer <redacted>"}
FFZY = "ffzy::ffzy"
DYTT = "dytt::dytt"
LZI = "lzi::lzm3u8"
TITLE = "测试剧"
CMS_DURATION = 2700

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
FAKE_M3U8_SECONDS = 30


def tv_brief(guid, title, line, duration=0, n=8):
    eps = [{"title": f"第{i + 1:02d}集",
            "url": f"https://{line.split('::')[0]}.test/v{i}.m3u8",
            "direct": True} for i in range(n)]
    b = {"guid": guid, "title": title, "year": "2026",
         "type_name": "电视剧", "poster": "https://img.test/p.jpg",
         "overview": "简介", "score": "8", "actor": "", "director": "",
         "area": "", "remarks": "",
         "lines": {line: eps}}
    if duration:
        b["duration"] = duration
    return b


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
    if tok == "Bearer <redacted>":
        return {"known": True, "guid": "admin-guid", "name": "admin",
                "is_admin": True, "token": tok}
    return {"known": False, "guid": "", "name": "", "is_admin": False,
            "token": ""}


class Diag3Tests(unittest.IsolatedAsyncioTestCase):
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
        # 同一部剧两条线（同标题、不同 og/集 guid）+ 一部无 CMS 时长剧
        #（走 playlist 探测），三部都有 CMS 时长的是 ffzy/dytt 线。
        self.store.remember_merged([
            tv_brief("og-ffzy", TITLE, FFZY, duration=CMS_DURATION),
            tv_brief("og-dytt", TITLE, DYTT, duration=CMS_DURATION),
            tv_brief("og-nodur", "无时长剧", LZI),
        ])
        self.ffzy_eps = [episode_guid("og-ffzy", "", i) for i in range(8)]
        self.dytt_eps = [episode_guid("og-dytt", "", i) for i in range(8)]
        self.nodur_eps = [episode_guid("og-nodur", "", i) for i in range(8)]
        resume_diagmod.clear()

    def tearDown(self):
        self.history.delete(
            self.ffzy_eps + self.dytt_eps + self.nodur_eps
            + ["og-ffzy", "og-dytt", "og-nodur"], user="admin-guid")
        resume_diagmod.clear()
        appmod._PLAYLIST_CACHE.clear()

    # ---- 小工具 ----
    async def diag(self, guid=""):
        q = f"?guid={guid}" if guid else ""
        r = await self.client.get("/v/api/ext/diag/resume" + q,
                                  headers=ADMIN)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def record(self, ig, ts=600, duration=0):
        return await self.client.post("/v/api/v1/play/record", json={
            "item_guid": ig, "media_guid": "mg", "ts": ts,
            "duration": duration}, headers=ADMIN)

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        return r.json().get("data") or {}

    async def events(self, kind=None, guid=""):
        body = await self.diag(guid)
        evs = body["data"]["events"]
        if kind:
            evs = [e for e in evs if e["kind"] == kind]
        return body, evs

    # ---- 1. info 事件含服务端 duration ----
    async def test_info_event_has_cms_duration(self):
        # 客户端仍报 duration=0（诊断现场），服务端应返回真实时长。
        await self.record(self.ffzy_eps[0], ts=941, duration=0)
        info = await self.play_info("og-ffzy")
        self.assertEqual(info.get("duration"), CMS_DURATION)
        self.assertEqual(info.get("ts"), 941)
        _, infos = await self.events("info")
        self.assertEqual(len(infos), 1)
        ev = infos[0]
        self.assertIn("duration", ev)
        self.assertIsInstance(ev["duration"], int)
        self.assertGreater(ev["duration"], 0)
        self.assertEqual(ev["duration"], CMS_DURATION)
        self.assertEqual(ev["ts"], 941)

    async def test_info_event_has_playlist_probe_duration(self):
        # 无 CMS 时长：playlist EXTINF 探测到的时长也应进 info 事件。
        key = appmod._playlist_cache_key(
            {"online_guid": "og-nodur", "line": LZI, "index": 0})
        appmod._PLAYLIST_CACHE[key] = (time.time(), FAKE_M3U8)
        info = await self.play_info(self.nodur_eps[0])
        self.assertEqual(info.get("duration"), FAKE_M3U8_SECONDS)
        _, infos = await self.events("info")
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0]["duration"], FAKE_M3U8_SECONDS)

    def test_note_info_duration_missing_is_none(self):
        # 服务端没给 duration 时记 None（与“给了 0”区分），不编造。
        resume_diagmod.note_info("g", {"guid": "g", "ts": 5})
        evs = resume_diagmod.events()
        self.assertEqual(len(evs), 1)
        self.assertIn("duration", evs[0])
        self.assertIsNone(evs[0]["duration"])
        self.assertEqual(evs[0]["ts"], 5)

    def test_note_info_duration_string_coerced_to_int(self):
        resume_diagmod.note_info("g", {"guid": "g", "ts": 5,
                                       "duration": "2700"})
        ev = resume_diagmod.events()[0]
        self.assertEqual(ev["duration"], 2700)
        self.assertIsInstance(ev["duration"], int)

    # ---- 2. record 事件含 canon_key，跨线同一集相同 ----
    async def test_record_events_share_canon_key_across_lines(self):
        await self.record(self.ffzy_eps[0], ts=600, duration=3600)
        await self.record(self.dytt_eps[0], ts=900, duration=3600)
        _, recs = await self.events("record")
        self.assertEqual(len(recs), 2)
        for ev in recs:
            self.assertIn("canon_key", ev)
        self.assertEqual(recs[0]["canon_key"], recs[1]["canon_key"])
        self.assertEqual(
            recs[0]["canon_key"],
            histmod.canonical_key_for_guid(self.ffzy_eps[0]))
        self.assertTrue(recs[0]["canon_key"].startswith("canon:tv:"))
        self.assertEqual(recs[0]["ts"], 600)
        self.assertEqual(recs[1]["ts"], 900)

    async def test_record_folder_guid_event_has_title_canon_key(self):
        # 文件夹卡 guid 上报：存下的标题级记录带 canon:title:…键。
        folder = appmod._line_folder_guid("og-ffzy")
        r = await self.record(folder, ts=600, duration=3600)
        self.assertEqual(r.json()["code"], 0, r.text)
        _, recs = await self.events("record")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["canon_key"],
                         histmod.canonical_title_key(TITLE))
        self.assertEqual(recs[0]["parsed"], "title")

    async def test_record_unresolved_event_canon_key_is_none(self):
        await self.record("no-such-guid")
        _, recs = await self.events("record")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "upstream")
        self.assertIn("canon_key", recs[0])
        self.assertIsNone(recs[0]["canon_key"])

    def test_note_record_explicit_canon_key_kept(self):
        resume_diagmod.note_record("raw", "og-ffzy",
                                   {"ts": 1, "duration": 0},
                                   outcome="history", parsed="title",
                                   canon_key="canon:tv:x:s1:e1")
        ev = resume_diagmod.events()[0]
        self.assertEqual(ev["canon_key"], "canon:tv:x:s1:e1")

    # ---- 3. lookup：给分源 guid 也能返回规范键下的记录 ----
    async def test_lookup_by_other_line_guid_returns_canon_record(self):
        await self.record(self.ffzy_eps[0], ts=600, duration=3600)
        body = await self.diag(self.dytt_eps[0])
        lookup = body["data"]["lookup"]
        self.assertEqual(set(lookup), {"admin-guid"})
        canon = histmod.canonical_key_for_guid(self.ffzy_eps[0])
        self.assertIn(canon, lookup["admin-guid"])
        self.assertEqual(lookup["admin-guid"][canon]["ts"], 600)
        self.assertEqual(lookup["admin-guid"][canon]["duration"], 3600)

    async def test_lookup_by_own_guid_still_works(self):
        await self.record(self.ffzy_eps[3], ts=600, duration=3600)
        body = await self.diag(self.ffzy_eps[3])
        lookup = body["data"]["lookup"]
        canon = histmod.canonical_key_for_guid(self.ffzy_eps[3])
        self.assertIn(canon, lookup["admin-guid"])
        self.assertEqual(lookup["admin-guid"][canon]["ts"], 600)

    async def test_lookup_unknown_guid_is_empty(self):
        body = await self.diag("no-such-guid")
        self.assertEqual(body["data"]["lookup"], {})

    # ---- 4. 端点形状与只读性 ----
    async def test_diag_shape(self):
        await self.record(self.ffzy_eps[0], ts=600, duration=3600)
        await self.play_info("og-ffzy")
        body = await self.diag(self.ffzy_eps[0])
        self.assertEqual(body["code"], 0)
        self.assertEqual(set(body["data"]), {"events", "lookup"})
        kinds = [e["kind"] for e in body["data"]["events"]]
        self.assertEqual(kinds, ["record", "info"])
        for ev in body["data"]["events"]:
            self.assertIn("at", ev)
            self.assertIn("partition", ev)

    async def test_diag_is_read_only(self):
        await self.record(self.ffzy_eps[0], ts=600, duration=3600)
        before = await self.diag(self.ffzy_eps[0])
        after = await self.diag(self.ffzy_eps[0])
        self.assertEqual(after["data"], before["data"])
        rec = self.history.get(self.ffzy_eps[0], user="admin-guid")
        self.assertEqual(rec["ts"], 600)
        self.assertEqual(rec["duration"], 3600)
        # 端点响应里不含任何凭证原文。
        self.assertNotIn("admin-token", json.dumps(after))
        self.assertNotIn("token", json.dumps(after).lower().replace(
            "admin-guid", ""))

    async def test_diag_does_not_change_history_file(self):
        await self.record(self.ffzy_eps[0], ts=600, duration=3600)
        state = Path(histmod._STATE)
        before = state.read_bytes() if state.exists() else b""
        await self.diag(self.dytt_eps[0])
        await self.diag()
        after = state.read_bytes() if state.exists() else b""
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
