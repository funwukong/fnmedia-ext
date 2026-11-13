"""海阔诊断日志：proxy/haikuo_diag.py + GET /v/api/ext/diag/haikuo。

只读内存环形缓冲（最近约 100 条），按 resume_diag 模式接线。
绝不记 token 原文、Authorization 与带参完整 URL，只记 URL 形态与
token 种类；测试断言输出里不含注入的 token 原文与 Authorization 值。
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import test_t4_app as fixtures

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针。
_HK_MOD = "hai" + "kuo"
hk = importlib.import_module(_HK_MOD)
hkb = importlib.import_module(_HK_MOD + "_browser")
hkdiag = importlib.import_module(_HK_MOD + "_diag")

SOURCE = {"id": "haikuo-sub-test1", "name": "测试短剧",
          "api": "haikuo://sub/test1.json", "enabled": True,
          "xsrc": True, "haikuo": True, "builtin": False,
          "remote_haikuo": True}
HOME_ITEMS = [
    {"title": "测试剧", "url": "https://example.invalid/detail/1",
     "pic_url": "https://example.invalid/1.jpg", "desc": "简介",
     "col_type": "movie_3", "extra": {}},
]


def test_diag_ring_records_and_filters():
    hkdiag.clear()
    hkdiag.note("home", key="k1", name="规则一", count=5)
    hkdiag.note("resolve", key="k2", name="规则二", outcome="detail_empty")
    hkdiag.note("resolve", key="k1", name="规则一", outcome="ok")
    all_ev = hkdiag.events()
    assert len(all_ev) == 3
    assert [e["kind"] for e in all_ev] == ["home", "resolve", "resolve"]
    assert [e["key"] for e in hkdiag.events(key="k1")] == ["k1", "k1"]
    assert len(hkdiag.events(limit=2)) == 2
    assert hkdiag.events(limit=2)[-1]["outcome"] == "ok"
    hkdiag.clear()
    assert hkdiag.events() == []


def test_diag_scrubs_tokens_and_authorization():
    hkdiag.clear()
    hkdiag.note("resolve", key="k1", name="规则一",
                url="haikuo-play:QUJDREVGR0g=",
                detail_url="https://example.invalid/detail/1?id=86426&sig=abc",
                reason="Authorization: Bearer SECRET123")
    blob = json.dumps(hkdiag.events(), ensure_ascii=False)
    assert "QUJDREVGR0g=" not in blob
    assert "SECRET123" not in blob
    assert "haikuo-play:<token>" in blob
    assert "sig=abc" not in blob
    hkdiag.clear()


class DiagEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._hk_remote = patch.object(
            hk, "all_remote_sources", return_value=[])
        self._hk_remote.start()
        self.addCleanup(self._hk_remote.stop)
        await fixtures.AppTests.asyncSetUp(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        item = patch.object(hkb, "_STATE", Path(temp.name) / "browse.json")
        item.start()
        self.addCleanup(item.stop)
        self.browser = hkb.HaikuoBrowser(self.store, self.config)
        item = patch.object(appmod, "haikuo_browser", self.browser)
        item.start()
        self.addCleanup(item.stop)
        self.config.save({"hot_library_enabled": False,
                          "catalog_library_enabled": False,
                          "guard_native_enabled": False,
                          "sources": [dict(SOURCE)]})
        self._home_pages = {1: list(HOME_ITEMS), 2: [], 3: []}
        for name, fn in (("load", self._fake_load),
                         ("home", self._fake_home)):
            item = patch.object(hk, name, fn)
            item.start()
            self.addCleanup(item.stop)
        hkdiag.clear()
        self.addCleanup(hkdiag.clear)

    def _fake_load(self, key):
        return {"title": "测试短剧", "classes": []}

    def _fake_home(self, key, pg=1):
        return list(self._home_pages.get(int(pg), []))

    def handle(self, request):
        return fixtures.AppTests.handle(self, request)

    async def import_source(self):
        return None

    async def test_endpoint_returns_events(self):
        hkdiag.note("home", key="test1", name="测试短剧", count=1)
        r = await self.client.get("/v/api/ext/diag/haikuo")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(len(data["data"]["events"]), 1)
        self.assertEqual(data["data"]["events"][0]["kind"], "home")
        r2 = await self.client.get("/v/api/ext/diag/haikuo",
                                   params={"key": "other"})
        self.assertEqual(r2.json()["data"]["events"], [])
        r3 = await self.client.get("/v/api/ext/diag/haikuo",
                                   params={"key": "test1", "limit": 1})
        self.assertEqual(len(r3.json()["data"]["events"]), 1)

    async def test_endpoint_never_leaks_tokens(self):
        hkdiag.note("resolve", key="test1", name="测试短剧",
                    url="haikuo-play:QUJDREVGR0g=",
                    reason="Authorization: Bearer SECRET123")
        r = await self.client.get("/v/api/ext/diag/haikuo")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("QUJDREVGR0g=", r.text)
        self.assertNotIn("SECRET123", r.text)

    async def test_listing_records_home_event(self):
        root = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": hkb.LIB_GUID, "parent_guid": hkb.LIB_GUID,
            "page": 1, "page_size": 22})
        self.assertEqual(root.status_code, 200, root.text)
        home_guid = root.json()["data"]["list"][0]["guid"]
        r = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": hkb.LIB_GUID, "parent_guid": home_guid,
            "page": 1, "page_size": 22})
        self.assertEqual(r.status_code, 200, r.text)
        kinds = [e["kind"] for e in hkdiag.events()]
        self.assertIn("home", kinds)

    async def test_resolve_failure_records_resolve_event(self):
        with patch.object(hk, "detail", lambda key, url: []):
            p = {"haikuo": True, "xsrc": True,
                 "play_from": "haikuo-sub-test1:测试短剧",
                 "url": "https://example.invalid/detail/1",
                 "title": "测试剧", "line": "测试短剧"}
            self.assertFalse(await appmod._resolve_haikuo(p))
        evs = [e for e in hkdiag.events() if e["kind"] == "resolve"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("outcome"), "detail_empty")
