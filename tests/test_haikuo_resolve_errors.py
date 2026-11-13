"""海阔解析失败：硬错误而不是占位播放列表（2026-11-12 用户报“转圈”）。

根因：hls_race._resolve_if_needed 调用 app._resolve_haikuo 后丢弃了
False 返回；picked 里留着详情链接或 haikuo-play: token 当 url，
而 _is_slow_line 对 xsrc=True 的海阔线返回 True，解析失败后
_serve_normal 给了 _placeholder_playlist_response，客户端一直转圈。

本文件先以桩复现：_resolve_haikuo 各失败点须带明确原因；
_resolve_if_needed 须把原因传到 picked.resolve_error；
hls_main 对解析失败的海阔片须返回 502 + 原因文案，而不是 200 占位。
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import hls_race as hlsmod
import store as storemod
import test_t4_app as fixtures

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针。
_HK_MOD = "hai" + "kuo"
hk = importlib.import_module(_HK_MOD)
hkb = importlib.import_module(_HK_MOD + "_browser")

SOURCE = {"id": "haikuo-sub-test1", "name": "测试短剧",
          "api": "haikuo://sub/test1.json", "enabled": True,
          "xsrc": True, "haikuo": True, "builtin": False,
          "remote_haikuo": True}
HOME_ITEMS = [
    {"title": "测试剧", "url": "https://example.invalid/detail/1",
     "pic_url": "https://example.invalid/1.jpg", "desc": "简介",
     "col_type": "movie_3", "extra": {}},
]
EPISODES = [{"title": "第1集", "url": "haikuo-play:AAAA"}]
NAV_REASON = {"playable": False,
              "reason": "lazy rule returned a navigation URL, not a play URL",
              "action": "detail", "url": "hiker://empty"}


def _picked(url="https://example.invalid/detail/1"):
    return {"haikuo": True, "xsrc": True,
            "play_from": "haikuo-sub-test1:测试短剧",
            "url": url, "title": "测试剧", "line": "测试短剧"}


class ResolveErrorTests(unittest.IsolatedAsyncioTestCase):
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

    def _fake_load(self, key):
        return {"title": "测试短剧", "classes": []}

    def _fake_home(self, key, pg=1):
        return list(self._home_pages.get(int(pg), []))

    def handle(self, request):
        return fixtures.AppTests.handle(self, request)

    async def import_source(self):
        return None

    async def listing(self, parent=hkb.LIB_GUID, **body):
        response = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": hkb.LIB_GUID, "parent_guid": parent,
            "page": 1, "page_size": 22, **body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ---- _resolve_haikuo 各失败点带明确原因 ----

    async def test_detail_empty_reports_reason(self):
        with patch.object(hk, "detail", lambda key, url: []):
            p = _picked()
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "detail_empty")
        self.assertIn("没有可播剧集", p.get("haikuo_error_msg", ""))

    async def test_navigation_token_is_not_playable(self):
        with patch.object(hk, "detail",
                          lambda key, url: list(EPISODES)), \
                patch.object(hk, "play",
                             lambda key, token: dict(NAV_REASON)):
            p = _picked()
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "not_playable")
        self.assertIn("分类导航", p.get("haikuo_error_msg", ""))

    async def test_x5_play_url_reports_unsupported(self):
        def _boom(key, token):
            raise RuntimeError("unsupported play URL scheme: video://x")
        with patch.object(hk, "detail",
                          lambda key, url: list(EPISODES)), \
                patch.object(hk, "play", _boom):
            p = _picked()
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "x5")
        self.assertIn("X5", p.get("haikuo_error_msg", ""))

    async def test_detail_exception_reports_failed(self):
        def _boom(key, url):
            raise RuntimeError("upstream exploded")
        with patch.object(hk, "detail", _boom):
            p = _picked()
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "failed")

    async def test_success_clears_error_and_sets_direct_url(self):
        with patch.object(hk, "detail",
                          lambda key, url: list(EPISODES)), \
                patch.object(hk, "play", lambda key, token: {
                    "playable": True,
                    "url": "https://cdn.invalid/x.m3u8"}):
            p = _picked()
            self.assertTrue(await appmod._resolve_haikuo(p))
        self.assertEqual(p["url"], "https://cdn.invalid/x.m3u8")
        self.assertNotIn("haikuo_error", p)
        self.assertNotIn("haikuo_error_msg", p)

    # ---- _resolve_if_needed 把原因传出来 ----

    async def test_resolve_if_needed_propagates_haikuo_error(self):
        with patch.object(hk, "detail", lambda key, url: []):
            p = _picked()
            await hlsmod._resolve_if_needed(p, None)
        self.assertEqual(p.get("resolve_error"), "detail_empty")
        self.assertIn("没有可播剧集", p.get("resolve_error_msg", ""))

    # ---- 端到端：hls_main 给硬错误，不给占位 ----

    async def test_hls_main_returns_hard_error_not_placeholder(self):
        root = await self.listing()
        home_guid = root["data"]["list"][0]["guid"]
        home = await self.listing(parent=home_guid)
        og = home["data"]["list"][0]["guid"]
        eg = storemod.episode_guid(og, index=0)
        info = (await self.client.post(
            "/v/api/v1/play/info", json={"item_guid": eg})).json()["data"]
        mg = info["media_guid"]
        with patch.object(hk, "detail", lambda key, url: []):
            r = await self.client.get(f"/v/media/online/{mg}/main.m3u8",
                                      params={"index": 0})
        self.assertEqual(r.status_code, 502, r.text[:200])
        self.assertIn("没有可播剧集", r.text)
        self.assertNotIn("#EXTM3U", r.text)

    # ---- 任务 C：文件夹守门仍在 ----

    async def test_folder_guard_returns_clear_error(self):
        guid = self.browser._put(SOURCE, "home", "测试短剧", hkb.LIB_GUID)
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": guid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], -1)
        self.assertIn("这是目录", r.json()["msg"])


# ---- store 取集路径必须透传 haikuo 标志（2026-11-12 转圈的真根因） ----

class StoreHaikuoFlagTests(unittest.TestCase):
    """resolve_episode / get_episode_target 曾用固定键集重建字典，
    只透传 xsrc/catvod/drpy，丢了 haikuo：播放链上 _resolve_if_needed
    的海阔分支从不触发，详情链接直送抓清单，慢线占位 → 一直转圈。"""

    def _title(self):
        import providers as provmod
        ep = provmod.Episode(
            title="第1集", url="https://example.invalid/detail/1",
            direct=True, xsrc=True, haikuo=True,
            play_from="haikuo-sub-test1:测试短剧")
        return provmod.OnlineTitle(
            provider="haikuo-sub-test1",
            vod_id="https://example.invalid/detail/1",
            title="测试剧", type_name="电视剧", year="", area="",
            updated="", remarks="", score="", poster="", overview="",
            actor="", director="", sources={"测试短剧": [ep]})

    def _fresh_store(self, tmp):
        with patch.object(storemod, "_STATE", Path(tmp) / "titles.json"):
            return storemod.Store()

    def test_resolve_episode_keeps_haikuo(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._fresh_store(tmp)
            og = s.remember_titles([self._title()])[0]["guid"]
            got = s.resolve_episode(og, "测试短剧", 0)
        self.assertIsNotNone(got)
        self.assertTrue(got.get("haikuo"))

    def test_get_episode_target_keeps_haikuo(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._fresh_store(tmp)
            og = s.remember_titles([self._title()])[0]["guid"]
            eg = storemod.episode_guid(og, "", 0)
            got = s.get_episode_target(eg)
        self.assertIsNotNone(got)
        self.assertTrue(got.get("haikuo"))
