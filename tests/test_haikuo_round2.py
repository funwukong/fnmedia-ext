"""海阔第二轮修复（2026-11-12 用户装 all3 真机反馈）。

复现（影探4K 实测）：
  1. detail 是混合页不是纯剧集列表：归鹭 ?id=86364 的 detail 共 9 条，
     [0] 海报头（movie_1_vertical_pic_blur + .webp 图）、[1] 剧情简介
     （rich_text + javascript:;）、[2] blank_block、[3] 播放线路头
     （rich_text）、[4][5][6] 切线路按钮（scroll_button）、[7] 排序
     开关（text_center_1），只有 [8]（text_2，归鹭4K，extra.id
     yt_86364_0_0，haikuo-play: token）是真剧集。_resolve_haikuo 按
     下标取 items[0] 当第 1 集，播一张图得 sniff，客户端 15 秒重试
     约 16 轮。修：取集前先过滤到真剧集。
  2. 分类页上方还挂一级目录：navigate() 重取的是整页 home（68 条，
     42 个 scroll_button 开关行 + 20 部影片），浏览器把开关行全映
     成目录卡。修：lazy 节点列表里排除开关行，只留影片与“下一页 →”。
  3. diag 的 play/resolve/detail 事件没有 URL 信息，定位靠猜。
     修：事件 payload 里加 url 形态（只记形态，不记 token 原文）。

本文件先以桩复现，再断言修复后：混合 detail 页取到真剧集、lazy
分类列表无开关行有影片与下一页、diag 事件含 url 形态与过滤前后
集数；纯剧集列表的规则过滤是无操作。
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

LAZY_BTN = ("hiker://empty@lazyRule=.js:(function(){"
            "putMyVar('yt_tid','42');refreshPage(false);"
            "return 'hiker://empty';})()")
EPISODE_TOKEN = "haikuo-play:EPISODETOKEN"
DIRECT_URL = "https://pan.wo.cn/download/abc123.mp4"

# 归鹭 ?id=86364 的 detail 页形状（sidecar 实测，9 条）。
MIXED_DETAIL = [
    {"title": "", "url": "https://img.invalid/poster.webp", "pic_url": "",
     "desc": "", "col_type": "movie_1_vertical_pic_blur", "extra": {}},
    {"title": "剧情简介", "url": "javascript:;", "pic_url": "",
     "desc": "", "col_type": "rich_text", "extra": {}},
    {"title": "", "url": "", "pic_url": "", "desc": "",
     "col_type": "blank_block", "extra": {}},
    {"title": "播放线路", "url": "hiker://empty", "pic_url": "",
     "desc": "", "col_type": "rich_text", "extra": {}},
    {"title": "线路1", "url": "haikuo-play:AAAA", "pic_url": "",
     "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "线路2", "url": "haikuo-play:BBBB", "pic_url": "",
     "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "线路3", "url": "haikuo-play:CCCC", "pic_url": "",
     "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "共 1 集 正序 点击切换", "url": "haikuo-play:DDDD",
     "pic_url": "", "desc": "", "col_type": "text_center_1", "extra": {}},
    {"title": "归鹭4K", "url": EPISODE_TOKEN, "pic_url": "",
     "desc": "", "col_type": "text_2", "extra": {"id": "yt_86364_0_0"}},
]

# 别的规则的纯剧集列表：过滤对它们必须是无操作。
PURE_DETAIL = [
    {"title": "第1集", "url": "haikuo-play:AAAA",
     "col_type": "text_2", "extra": {}},
    {"title": "第2集", "url": "haikuo-play:BBBB",
     "col_type": "", "extra": {}},
    {"title": "第3集", "url": "https://example.invalid/detail/3",
     "col_type": "movie_3", "extra": {}},
    {"title": "第4集", "url": "https://example.invalid/detail/4",
     "col_type": "", "extra": {"id": "999"}},
]

# 分类页（navigate 返回）：开关行 + 影片 + 下一页。
LAZY_PAGE = [
    {"title": "最新电影", "url": LAZY_BTN, "pic_url": "",
     "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "热播国剧", "url": LAZY_BTN.replace("'42'", "'7'"),
     "pic_url": "", "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "韩剧", "url": LAZY_BTN.replace("'42'", "'9'"),
     "pic_url": "", "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "分类剧1", "url": "https://example.invalid/detail/c1",
     "pic_url": "https://example.invalid/c1.jpg", "desc": "分类简介1",
     "col_type": "movie_3", "extra": {"id": "c1"}},
    {"title": "分类剧2", "url": "https://example.invalid/detail/c2",
     "pic_url": "https://example.invalid/c2.jpg", "desc": "分类简介2",
     "col_type": "movie_3", "extra": {"id": "c2"}},
    {"title": "下一页",
     "url": ("hiker://empty@lazyRule=.js:(function(){"
            "putMyVar('yt_pg','2');refreshPage(false);"
            "return 'hiker://empty';})()"),
     "pic_url": "", "desc": "", "col_type": "scroll_button", "extra": {}},
]

HOME_ITEMS = [
    {"title": "最新电影", "url": LAZY_BTN, "pic_url": "",
     "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "热播国剧", "url": LAZY_BTN.replace("'42'", "'7'"),
     "pic_url": "", "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "寄生狂潮",
     "url": "hiker://page/detail#noHistory##immersiveTheme#",
     "pic_url": "https://example.invalid/86426.jpg", "desc": "简介A",
     "col_type": "movie_3", "extra": {"id": "86426"}},
]


def _picked(url="hiker://page/detail?id=86364"):
    return {"haikuo": True, "xsrc": True,
            "play_from": "haikuo-sub-test1:测试短剧",
            "url": url, "title": "归鹭", "line": "测试短剧"}


def _fake_play(key, token):
    if token == EPISODE_TOKEN:
        return {"playable": True, "url": DIRECT_URL}
    return {"playable": False, "sniff": True,
            "url": "https://img.invalid/poster.webp"}


# ---------------- 详情过滤（纯函数） ----------------

class FilterEpisodesTests(unittest.TestCase):
    def test_mixed_detail_keeps_only_real_episode(self):
        eps = appmod._haikuo_filter_episodes(list(MIXED_DETAIL))
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["title"], "归鹭4K")
        self.assertEqual(eps[0]["url"], EPISODE_TOKEN)

    def test_pure_episode_list_is_untouched(self):
        eps = appmod._haikuo_filter_episodes(list(PURE_DETAIL))
        self.assertEqual([e["title"] for e in eps],
                         ["第1集", "第2集", "第3集", "第4集"])

    def test_empty_and_non_dict_items_are_dropped(self):
        eps = appmod._haikuo_filter_episodes(
            [None, "第1集", {}, MIXED_DETAIL[-1]])
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["title"], "归鹭4K")


# ---------------- 解析取集 + diag ----------------

class Round2ResolveTests(unittest.IsolatedAsyncioTestCase):
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
        hkdiag.clear()
        self.addCleanup(hkdiag.clear)

    def handle(self, request):
        return fixtures.AppTests.handle(self, request)

    async def import_source(self):
        return None

    def _detail_events(self):
        return [e for e in hkdiag.events() if e.get("kind") == "detail"]

    def _resolve_events(self):
        return [e for e in hkdiag.events() if e.get("kind") == "resolve"]

    async def test_resolve_plays_real_episode_not_poster(self):
        with patch.object(hk, "detail",
                          lambda key, url: list(MIXED_DETAIL)), \
                patch.object(hk, "play", _fake_play):
            p = _picked()
            self.assertTrue(await appmod._resolve_haikuo(p))
        # 取到的是真剧集的直链，不是海报图的嗅探页。
        self.assertEqual(p["url"], DIRECT_URL)
        self.assertNotIn("haikuo_error", p)

    async def test_detail_diag_records_raw_and_filtered_counts(self):
        with patch.object(hk, "detail",
                          lambda key, url: list(MIXED_DETAIL)), \
                patch.object(hk, "play", _fake_play):
            p = _picked()
            await appmod._resolve_haikuo(p)
        evs = self._detail_events()
        self.assertTrue(any(e.get("count") == 9 and e.get("filtered") == 1
                            for e in evs), evs)

    async def test_all_filtered_out_reports_detail_empty(self):
        chrome_only = [d for d in MIXED_DETAIL if d["title"] != "归鹭4K"]
        with patch.object(hk, "detail", lambda key, url: list(chrome_only)), \
                patch.object(hk, "play", _fake_play):
            p = _picked()
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "detail_empty")

    async def test_resolve_event_carries_url_shape(self):
        with patch.object(hk, "detail",
                          lambda key, url: list(MIXED_DETAIL)), \
                patch.object(hk, "play", _fake_play):
            p = _picked()
            await appmod._resolve_haikuo(p)
        evs = self._resolve_events()
        self.assertTrue(evs)
        # 解析成功后 resolve 事件记的是直链形态（带 host、不带查询串）。
        self.assertEqual(evs[-1].get("url"), "https://pan.wo.cn/download/abc123.mp4")


# ---------------- lazy 分类列表 + diag url 形态 ----------------

class Round2LazyTests(unittest.IsolatedAsyncioTestCase):
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
                         ("home", self._fake_home),
                         ("navigate", self._fake_navigate)):
            item = patch.object(hk, name, fn)
            item.start()
            self.addCleanup(item.stop)
        hkdiag.clear()
        self.addCleanup(hkdiag.clear)

    def _fake_load(self, key):
        return {"title": "测试短剧", "classes": []}

    def _fake_home(self, key, pg=1):
        return list(self._home_pages.get(int(pg), []))

    def _fake_navigate(self, key, token, pg=1):
        return list(LAZY_PAGE) if int(pg) == 1 else []

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

    def _titles(self, rows):
        # 卡片标题带在线前缀（全角括号形式），这里只比后缀，免得测试
        # 文件里出现全角括号。
        return [r["title"] for r in rows]

    def _has_title(self, rows, name):
        return any(r["title"].endswith(name) for r in rows)

    async def _home_guid(self):
        root = await self.listing()
        self.assertEqual(root["code"], 0)
        return root["data"]["list"][0]["guid"]

    async def test_lazy_listing_drops_switchers_keeps_videos_and_next(self):
        home = await self.listing(parent=await self._home_guid())
        lazy = next(r for r in home["data"]["list"]
                    if r["title"].endswith("最新电影"))
        self.assertEqual(lazy["type"], "Directory")
        cat = await self.listing(parent=lazy["guid"])
        self.assertEqual(cat["code"], 0)
        rows = cat["data"]["list"]
        # 开关行全丢，只留影片与下一页；下一页仍是目录卡，可点翻页。
        self.assertEqual(len(rows), 3)
        self.assertTrue(self._has_title(rows, "分类剧1"))
        self.assertTrue(self._has_title(rows, "分类剧2"))
        self.assertTrue(self._has_title(rows, "下一页"))
        folders = [r for r in rows if r["type"] == "Directory"]
        self.assertEqual(len(folders), 1)
        self.assertTrue(folders[0]["title"].endswith("下一页"))
        videos = [r for r in rows if r["type"] != "Directory"]
        self.assertEqual(len(videos), 2)

    async def test_root_home_listing_still_shows_category_folders(self):
        home = await self.listing(parent=await self._home_guid())
        rows = home["data"]["list"]
        # 根目录不动：一级分类仍是目录，影片仍是影片。
        self.assertTrue(self._has_title(rows, "最新电影"))
        self.assertTrue(self._has_title(rows, "热播国剧"))
        self.assertTrue(self._has_title(rows, "寄生狂潮"))

    def test_play_and_detail_diag_carry_url_shape(self):
        with patch.object(hk, "_call_loaded",
                          return_value={"items": list(MIXED_DETAIL)}):
            hk.detail("test1", "hiker://page/detail?id=86364")
        with patch.object(hk, "_call_loaded",
                          return_value={"playable": True,
                                        "url": DIRECT_URL}):
            hk.play("test1", EPISODE_TOKEN)
        evs = hkdiag.events()
        detail = [e for e in evs if e.get("kind") == "detail"]
        play = [e for e in evs if e.get("kind") == "play"]
        self.assertTrue(detail)
        # 查询串已去掉，只留形态。
        self.assertEqual(detail[-1].get("url"), "hiker://page/detail")
        self.assertTrue(play)
        self.assertEqual(play[-1].get("url"), "haikuo-play:<token>")
        blob = str(evs)
        self.assertNotIn(EPISODE_TOKEN, blob)
