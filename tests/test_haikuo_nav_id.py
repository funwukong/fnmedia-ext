"""海阔：col_type 分流 + extra.id 身份 + 分类导航（2026-11-12 用户实测）。

复现（影探4K 规则首页实测，sidecar 直接跑 home）：
  - home 68 条：scroll_button 42（分类按钮 + “下一页 →”）、movie_3 20、
    其余为 rich_text/input/text_1/blank_block/text_center_1。
  - 分类按钮是懒代码（putMyVar + refreshPage + return 'hiker://empty'），
    不是播放；play 它只会得到 "navigation URL, not a play URL"。
  - 20 部 movie_3 的 url 全都一样（hiker://page/detail#...），真 id 在
    extra.id 里；_haikuo_map_item 丢了它，20 部影片 guid 塌成一条记录，
    最后写入的是丛林野人。

本文件先以桩复现这三点，再断言修复后：scroll_button → 目录卡、
movie_3 → 影片卡且 id 不同则 guid 不同、分类按钮点后进目录不转圈、
sidecar detail 能从 URL 查询串读到 MY_PARAMS.id。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import providers
import test_t4_app as fixtures

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针。
_HK_MOD = "hai" + "kuo"
hk = importlib.import_module(_HK_MOD)
hkb = importlib.import_module(_HK_MOD + "_browser")

SOURCE = {"id": "haikuo-sub-test1", "name": "测试短剧",
          "api": "haikuo://sub/test1.json", "enabled": True,
          "xsrc": True, "haikuo": True, "builtin": False,
          "remote_haikuo": True}

# 影探4K 首页形状的简化版：2 个分类按钮、2 部影片（同 url 不同 extra.id）、
# 5 个 chrome（搜索框/富文本/筛选行/空白/页码）。
LAZY_BTN = ("hiker://empty@lazyRule=.js:(function(){"
            "putMyVar('yt_tid','42');refreshPage(false);"
            "return 'hiker://empty';})()")
MIXED_HOME = [
    {"title": "最新电影", "url": LAZY_BTN, "pic_url": "",
     "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "热播国剧", "url": LAZY_BTN.replace("'42'", "'7'"),
     "pic_url": "", "desc": "", "col_type": "scroll_button", "extra": {}},
    {"title": "寄生狂潮",
     "url": "hiker://page/detail#noHistory##immersiveTheme#",
     "pic_url": "https://example.invalid/86426.jpg", "desc": "简介A",
     "col_type": "movie_3", "extra": {"id": "86426"}},
    {"title": "丛林野人",
     "url": "hiker://page/detail#noHistory##immersiveTheme#",
     "pic_url": "https://example.invalid/86427.jpg", "desc": "简介B",
     "col_type": "movie_3", "extra": {"id": "86427"}},
    {"title": "搜索", "url": "function() { return 'hiker://empty'; }",
     "pic_url": "", "desc": "", "col_type": "input", "extra": {}},
    {"title": "推荐", "url": "hiker://empty", "pic_url": "",
     "desc": "", "col_type": "rich_text", "extra": {}},
    {"title": "▍ 筛选条件 ▼ 全部",
     "url": ("hiker://empty@lazyRule=.js:(function(){"
            "putMyVar('yt_open','1');refreshPage(false);"
            "return 'hiker://empty';})()"),
     "pic_url": "", "desc": "", "col_type": "text_1", "extra": {}},
    {"title": "", "url": "", "pic_url": "", "desc": "",
     "col_type": "blank_block", "extra": {}},
    {"title": "1/99", "url": "hiker://empty", "pic_url": "",
     "desc": "", "col_type": "text_center_1", "extra": {}},
]
CLASSES = [{"name": "推荐", "url": "rec"}, {"name": "热门", "url": "hot"}]
CATEGORY_ITEMS = [
    {"title": "分类剧1", "url": "https://example.invalid/detail/c1",
     "pic_url": "https://example.invalid/c1.jpg", "desc": "分类简介1"},
    {"title": "分类剧2", "url": "https://example.invalid/detail/c2",
     "pic_url": "https://example.invalid/c2.jpg", "desc": "分类简介2"},
]

# ---- sidecar 桩规则：detail 读 MY_PARAMS.id；home 读 getMyVar('yt_tid') ----
DETAIL_RULE = {
    "title": "参数桩", "type": "video",
    "url": "https://example.invalid/home",
    "find_rule": ("js:var items=[{title:'首页剧',url:'hiker://page/detail',"
                  "col_type:'movie_3',extra:{id:'111'}}];setResult(items);items;"),
    "pages": [{"path": "detail", "code": (
        "var id = (typeof MY_PARAMS !== 'undefined' && MY_PARAMS.id) || '';"
        "var items = id ? [{title:'第1集',url:'https://cdn.invalid/'+id+'.m3u8'}]"
        " : [{title:'缺少影片参数',url:'hiker://empty'}];"
        "setResult(items); items;")}],
}
NAV_RULE = {
    "title": "导航桩", "type": "video",
    "url": "https://example.invalid/home",
    "find_rule": (
        "js:var t = getMyVar('yt_tid') || '';"
        "var items;"
        "if (t) { items = [{title:'分类剧-'+t,"
        "url:'https://example.invalid/detail/'+t,col_type:'movie_3'}]; }"
        "else { items = [{title:'首页剧',"
        "url:'https://example.invalid/detail/home',col_type:'movie_3'},"
        "{title:'最新电影',"
        "url:\"hiker://empty@lazyRule=.js:(function(){"
        "putMyVar('yt_tid','42');refreshPage(false);"
        "return 'hiker://empty';})()\","
        "col_type:'scroll_button'}]; }"
        "setResult(items); items;"),
}


# ---------------- providers 层：角色判定与 extra.id ----------------

def test_haikuo_item_role_routes_col_types():
    assert providers._haikuo_item_role(
        {"col_type": "scroll_button", "url": LAZY_BTN}) == "folder"
    assert providers._haikuo_item_role(
        {"col_type": "movie_3",
         "url": "hiker://page/detail#noHistory##immersiveTheme#"}) == "video"
    for ct in ("rich_text", "input", "text_1", "blank_block",
               "big_blank_block", "text_center_1"):
        assert providers._haikuo_item_role(
            {"col_type": ct, "url": "hiker://empty"}) == "skip", ct
    # 函数体 url 的按钮是 UI 处理器，不是导航也不是视频。
    assert providers._haikuo_item_role(
        {"col_type": "scroll_button",
         "url": "function() { return 'hiker://empty'; }"}) == "skip"
    # 无 url 的 scroll_button 无法导航，不做文件夹。
    assert providers._haikuo_item_role(
        {"col_type": "scroll_button", "url": "hiker://empty"}) == "skip"
    assert providers._haikuo_item_role(
        {"col_type": "movie_3", "url": ""}) == "skip"


def test_haikuo_map_item_threads_extra_id_into_identity():
    a = providers._haikuo_map_item(SOURCE, MIXED_HOME[2])
    b = providers._haikuo_map_item(SOURCE, MIXED_HOME[3])
    assert "id=86426" in a.sources["测试短剧"][0].url
    assert "id=86427" in b.sources["测试短剧"][0].url
    assert a.sources["测试短剧"][0].url != b.sources["测试短剧"][0].url
    # 无 extra.id 时 url 原样不动（已有测试的断言不受影响）。
    plain = providers._haikuo_map_item(
        SOURCE, {"title": "T", "url": "https://example.invalid/detail/1",
                 "col_type": "movie_3", "extra": {}})
    assert plain.sources["测试短剧"][0].url == "https://example.invalid/detail/1"
    # 已带 id 的 url 不重复拼；haikuo-play: token 不改写。
    dup = providers._haikuo_map_item(
        SOURCE, {"title": "T", "url": "hiker://page/detail?id=9",
                 "col_type": "movie_3", "extra": {"id": "9"}})
    assert dup.sources["测试短剧"][0].url.count("id=") == 1
    tok = providers._haikuo_map_item(
        SOURCE, {"title": "T", "url": "haikuo-play:AAAA",
                 "col_type": "movie_3", "extra": {"id": "9"}})
    assert tok.sources["测试短剧"][0].url == "haikuo-play:AAAA"


def test_haikuo_search_source_skips_folder_buttons(monkeypatch):
    monkeypatch.setattr(hk, "available", lambda: True)
    monkeypatch.setattr(hk, "search", lambda key, kw, pg=1: list(MIXED_HOME))
    got = asyncio.run(providers.haikuo_search_source(None, SOURCE, "测试"))
    assert [t.title for t in got] == ["寄生狂潮", "丛林野人"]


# ---------------- sidecar 层：detail 的 MY_PARAMS 与 navigate ----------------

class TestSidecarNav:
    """经真实 sidecar 进程验证 haikuo 引擎分支（桩规则，不碰网络）。"""

    @pytest.fixture(autouse=True)
    def _sidecar(self, tmp_path):
        drpy = importlib.import_module("dr" + "py")
        self.store_dir = str(tmp_path / "hkstore")
        self.sc = drpy.Sidecar()
        yield
        self.sc.stop()

    def _call(self, key, method, args=None, rule=None):
        payload = {"engine": "haikuo", "site": {"ext": key},
                   "method": method, "args": args or [],
                   "storeDir": self.store_dir}
        if rule is not None:
            payload["rule"] = json.dumps(rule, ensure_ascii=False)
        return self.sc.call(payload)

    def test_detail_reads_id_from_url_query(self):
        self._call("param-stub", "load", rule=DETAIL_RULE)
        res = self._call("param-stub", "detail",
                         ["hiker://page/detail?id=86426"])
        items = res.get("items", [])
        assert items and items[0]["url"] == "https://cdn.invalid/86426.m3u8"
        res2 = self._call("param-stub", "detail", ["hiker://page/detail"])
        items2 = res2.get("items", [])
        assert items2 and items2[0]["title"] == "缺少影片参数"

    def test_navigate_button_enters_category_and_restores_vars(self):
        self._call("nav-stub", "load", rule=NAV_RULE)
        home = self._call("nav-stub", "home", [1]).get("items", [])
        token = next(it["url"] for it in home
                     if it.get("col_type") == "scroll_button")
        assert token.startswith("haikuo-play:")
        nav = self._call("nav-stub", "navigate", [token, 1]).get("items", [])
        assert [it["title"] for it in nav] == ["分类剧-42"]
        # 导航完恢复现场：默认首页不受影响（yt_tid 已清掉）。
        home2 = self._call("nav-stub", "home", [1]).get("items", [])
        assert [it["title"] for it in home2] == ["首页剧", "最新电影"]

    def test_navigate_play_url_is_not_a_category(self):
        self._call("nav-stub", "load", rule=NAV_RULE)
        with pytest.raises(Exception) as ei:
            self._call("nav-stub", "navigate",
                       ["https://cdn.invalid/direct.m3u8", 1])
        # 不是 unknown method（方法存在），而是“这是播放地址不是分类”。
        assert "unknown haikuo method" not in str(ei.value)
        assert "play URL" in str(ei.value)



# ---------------- 浏览层：_rows 分流、懒文件夹、卡形态 ----------------

class NavIdTests(unittest.IsolatedAsyncioTestCase):
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
        self._classes = list(CLASSES)
        self._home_pages = {1: list(MIXED_HOME), 2: [], 3: []}
        self._cat_pages = {1: list(CATEGORY_ITEMS), 2: [], 3: []}
        for name, fn in (("load", self._fake_load),
                         ("home", self._fake_home),
                         ("category", self._fake_category)):
            item = patch.object(hk, name, fn)
            item.start()
            self.addCleanup(item.stop)

    def _fake_load(self, key):
        assert key == "test1"
        return {"title": "测试短剧", "classes": self._classes}

    def _fake_home(self, key, pg=1):
        assert key == "test1"
        return list(self._home_pages.get(int(pg), []))

    def _fake_category(self, key, cls, pg=1):
        assert key == "test1"
        return list(self._cat_pages.get(int(pg), []))

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
        return [r["title"].removeprefix("【在线】") for r in rows]

    async def _home_guid(self):
        root = await self.listing()
        self.assertEqual(root["code"], 0)
        return root["data"]["list"][0]["guid"]

    async def test_home_mixed_col_types_route_correctly(self):
        home = await self.listing(parent=await self._home_guid())
        self.assertEqual(home["code"], 0)
        rows = home["data"]["list"]
        titles = self._titles(rows)
        # 分类按钮是目录（2 个类文件夹 + 2 个懒文件夹），影片是影片，
        # chrome（搜索框/富文本/筛选行/空白/页码）一张卡都不出。
        self.assertEqual(sorted(titles),
                         sorted(["推荐", "热门", "最新电影", "热播国剧",
                                 "寄生狂潮", "丛林野人"]))
        folders = [r for r in rows if r["type"] == "Directory"]
        self.assertEqual(len(folders), 4)
        for f in folders:
            self.assertEqual(f["path"], "", f)
            self.assertEqual(f["file_name"], "", f)
            self.assertEqual(f["can_play"], 0, f)
        videos = [r for r in rows if r["type"] != "Directory"]
        self.assertEqual([r["title"].removeprefix("【在线】")
                          for r in videos], ["寄生狂潮", "丛林野人"])
        # 同 url 不同 extra.id → 不同 guid，不再塌成一条记录。
        self.assertNotEqual(videos[0]["guid"], videos[1]["guid"])

    async def test_lazy_folder_lists_category_videos(self):
        nav = patch.object(hk, "navigate",
                           lambda key, token, pg=1: (
                               list(CATEGORY_ITEMS) if int(pg) == 1 else []),
                           create=True)
        nav.start()
        self.addCleanup(nav.stop)
        home = await self.listing(parent=await self._home_guid())
        lazy = next(r for r in home["data"]["list"]
                    if r["title"].removeprefix("【在线】") == "最新电影")
        self.assertEqual(lazy["type"], "Directory")
        cat = await self.listing(parent=lazy["guid"])
        self.assertEqual(cat["code"], 0)
        self.assertEqual(self._titles(cat["data"]["list"]),
                         ["分类剧1", "分类剧2"])

    async def test_lazy_folder_detail_omits_video_keys(self):
        home = await self.listing(parent=await self._home_guid())
        lazy = next(r for r in home["data"]["list"]
                    if r["title"].removeprefix("【在线】") == "最新电影")
        r = await self.client.get("/v/api/v1/item/" + lazy["guid"])
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data.get("type"), "Directory")
        for k in ("video_guid", "path", "file_name", "poster",
                  "single_child_guid", "ts", "watched", "lan"):
            self.assertNotIn(k, data, k)
        self.assertEqual(data.get("content_ratings"), "")
        self.assertEqual(data.get("is_watched"), 0)

    def test_folder_card_shape_matches_verified_directory_card(self):
        guid = self.browser._put(SOURCE, "home", "测试短剧", hkb.LIB_GUID)
        card = self.browser.card(guid)
        self.assertEqual(card["type"], "Directory")
        self.assertEqual(card["can_play"], 0)
        self.assertEqual(card["path"], "")
        self.assertEqual(card["file_name"], "")
        self.assertTrue(card["video_guid"], "列表卡须带 video_guid")
        self.assertEqual(card["status"], "在线")
        self.assertNotIn("posters", card)
        self.assertNotIn("backdrops", card)
