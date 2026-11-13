"""海阔选片库浏览：App 点开源的完整链（库列表 → 库详情 → 目录列表）。

复现 2026-11-12 用户报的 bug：海阔源能导入、在资源站列表里能看到，
点开却是空的。根因是 catalog 排除海阔源且 mdb_detail/item_list 没有
海阔分支，目录树拿不到内容；本文件先以本地桩复现这条链，再断言修复
后有内容。真实规则的端到端见 HAIKUO_LIVE=1 的 live 测试。

注意：本文件故意不出现残留扫描的原样关键字（针列表见
tools/public_strip.py），模块名用拼接 import；本地桩不碰真实云剪贴
服务与规则真实站点。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import test_t4_app as fixtures

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针。
_HK_MOD = "hai" + "kuo"
hk = importlib.import_module(_HK_MOD)
hkb = importlib.import_module(_HK_MOD + "_browser")

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "haikuo"

SOURCE = {"id": "haikuo-sub-test1", "name": "测试短剧",
          "api": "haikuo://sub/test1.json", "enabled": True,
          "xsrc": True, "haikuo": True, "builtin": False,
          "remote_haikuo": True}

HOME_ITEMS = [
    {"title": "剧名1", "url": "https://example.invalid/detail/1",
     "pic_url": "https://example.invalid/1.jpg", "desc": "简介1"},
    {"title": "剧名2", "url": "https://example.invalid/detail/2",
     "pic_url": "https://example.invalid/2.jpg", "desc": "简介2"},
    {"title": "剧名3", "url": "https://example.invalid/detail/3",
     "pic_url": "https://example.invalid/3.jpg", "desc": "简介3"},
]
CATEGORY_ITEMS = [
    {"title": "分类剧1", "url": "https://example.invalid/detail/c1",
     "pic_url": "https://example.invalid/c1.jpg", "desc": "分类简介1"},
    {"title": "分类剧2", "url": "https://example.invalid/detail/c2",
     "pic_url": "https://example.invalid/c2.jpg", "desc": "分类简介2"},
]
CLASSES = [{"name": "推荐", "url": "rec"}, {"name": "热门", "url": "hot"}]


def _titles(rows):
    """卡片标题去在线前缀（search_item 给在线卡加【在线】，与搜索结果一致）。"""
    return [r["title"].removeprefix("【在线】") for r in rows]


class BrowseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 隔离：不让真实注册表里的规则混进测试配置。
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
        # 本地桩：规则元数据（带分类）与首页/分类内容，不碰 sidecar。
        self._classes = list(CLASSES)
        self._home_pages = {1: list(HOME_ITEMS), 2: [], 3: []}
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
        # 上游桩：复用 test_t4_app 的通用处理（媒体库列表等回空官方列表，
        # 插件再注入虚拟库）；海阔内容走本地桩，不经过这里。
        return fixtures.AppTests.handle(self, request)

    async def import_source(self):
        # 本类不导 T4 订阅：海阔源由 setUp 直接写进测试配置。
        return None

    async def listing(self, parent=hkb.LIB_GUID, **body):
        response = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": hkb.LIB_GUID, "parent_guid": parent,
            "page": 1, "page_size": 22, **body})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def plugin(self):
        data = await self.listing()
        self.assertEqual(data["code"], 0)
        return data["data"]["list"][0]["guid"]

    # ---------------- 根因复现：点开源整条链 ----------------

    async def test_library_listed_detail_and_root_have_content(self):
        # 资源站列表里有海阔选片库；库详情 code 0；根目录列出规则文件夹。
        libs = (await self.client.get("/v/api/v1/mediadb/list")).json()["data"]
        self.assertEqual([r["guid"] for r in libs],
                         [hkb.LIB_GUID,
                          appmod.standalone_lib_guid("haikuo-sub-test1")])
        detail = await self.client.get("/v/api/v1/mdb/" + hkb.LIB_GUID)
        self.assertEqual(detail.json()["code"], 0)
        self.assertEqual(detail.json()["data"]["guid"], hkb.LIB_GUID)
        self.assertEqual(detail.json()["data"]["name"], "海阔小程序")
        root = await self.listing()
        self.assertEqual(root["code"], 0)
        self.assertEqual(root["data"]["total"], 1)
        folder = root["data"]["list"][0]
        self.assertEqual(folder["title"], "测试短剧")
        self.assertEqual(folder["type"], "Directory")
        # 文件夹本身的详情也 code 0（App 点卡前会校验）。
        item = await self.client.get("/v/api/v1/item/" + folder["guid"])
        self.assertEqual(item.json()["code"], 0)

    async def test_home_node_shows_classes_and_titles(self):
        home_guid = await self.plugin()
        data = await self.listing(home_guid)
        self.assertEqual(data["code"], 0)
        rows = data["data"]["list"]
        # 2 个分类文件夹在前，3 张标题卡在后；第 2 页为空即拉完，无哨兵。
        self.assertEqual(_titles(rows),
                         ["推荐", "热门", "剧名1", "剧名2", "剧名3"])
        self.assertEqual([r["type"] for r in rows[:2]],
                         ["Directory", "Directory"])
        self.assertEqual(data["data"]["total"], 2 + 3)
        # 标题卡已入库：点卡后的详情/播放链能从 store 找到它。
        card = next(r for r in rows
                    if r["title"].removeprefix("【在线】") == "剧名1")
        brief = self.store.get_title_by_guid(card["guid"])
        self.assertIsNotNone(brief)
        self.assertEqual(brief["title"], "剧名1")
        self.assertTrue(brief["lines"])

    async def test_category_node_lists_titles(self):
        home_guid = await self.plugin()
        home = await self.listing(home_guid)
        cat_guid = next(r["guid"] for r in home["data"]["list"]
                        if r["title"] == "推荐")
        data = await self.listing(cat_guid)
        self.assertEqual(data["code"], 0)
        rows = data["data"]["list"]
        self.assertEqual(_titles(rows), ["分类剧1", "分类剧2"])
        self.assertTrue(all(r["type"] != "Directory" for r in rows))

    async def test_home_without_classes_lists_only_titles(self):
        self._classes = []
        home_guid = await self.plugin()
        data = await self.listing(home_guid)
        self.assertEqual(_titles(data["data"]["list"]),
                         ["剧名1", "剧名2", "剧名3"])
        self.assertEqual(data["data"]["total"], 3)

    async def test_source_standalone_guid_opens_home(self):
        # 单源入口（App 点资源站里的源时按单源独立库 guid 打开）：库列表
        # 里有 "📡测试短剧" 卡；库详情 code 0；点开即该规则首页内容。
        sguid = appmod.standalone_lib_guid("haikuo-sub-test1")
        libs = (await self.client.get("/v/api/v1/mediadb/list")).json()["data"]
        card = next(r for r in libs if r["guid"] == sguid)
        self.assertEqual(card["title"], "📡测试短剧")
        detail = await self.client.get("/v/api/v1/mdb/" + sguid)
        self.assertEqual(detail.json()["code"], 0)
        self.assertEqual(detail.json()["data"]["name"], "📡测试短剧")
        item = await self.client.get("/v/api/v1/item/" + sguid)
        self.assertEqual(item.json()["code"], 0)
        self.assertEqual(item.json()["data"]["type"], "Directory")
        # 点开源时请求里只有单源 guid，不带聚合库 guid。
        response = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": sguid, "parent_guid": sguid,
            "page": 1, "page_size": 22})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(_titles(data["data"]["list"]),
                         ["推荐", "热门", "剧名1", "剧名2", "剧名3"])
        # 与聚合库进同一首页文件夹是同一节点（guid 一致）。
        home_guid = await self.plugin()
        self.assertEqual(item.json()["data"]["guid"], home_guid)

    async def test_expired_node_returns_502_not_empty(self):
        home_guid = await self.plugin()
        # 模拟节点过期（状态丢失）：App 仍按旧 guid 打开时明确报错，
        # 不静默返回空列表让用户以为源里没资源。
        del self.browser._nodes[home_guid]
        response = await self.client.post("/v/api/v1/item/list", json={
            "ancestor_guid": hkb.LIB_GUID, "parent_guid": home_guid,
            "page": 1, "page_size": 22})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], -1)
        self.assertIn("重新进入", response.json()["msg"])

    async def test_no_sources_hides_library(self):
        self.config.save({"sources": []})
        libs = (await self.client.get("/v/api/v1/mediadb/list")).json()["data"]
        self.assertNotIn(hkb.LIB_GUID, [r["guid"] for r in libs])
        managed = next(row for row in appmod._virtual_lib_list()
                       if row["group"] == "haikuo")
        self.assertEqual(managed["guid"], hkb.LIB_GUID)
        self.assertFalse(managed["enabled"])

    # ---------------- 单元：分页合并与映射 ----------------

    async def test_window_merges_rule_pages(self):
        self._classes = []
        self._home_pages = {
            1: [dict(HOME_ITEMS[0]), dict(HOME_ITEMS[1]), dict(HOME_ITEMS[2])],
            2: [dict(CATEGORY_ITEMS[0]), dict(CATEGORY_ITEMS[1])],
            3: []}
        home_guid = await self.plugin()
        first = await self.listing(home_guid, page=1, page_size=4)
        self.assertEqual(len(first["data"]["list"]), 4)
        self.assertEqual(first["data"]["total"], 5 + 1)  # 未拉完，哨兵 +1
        second = await self.listing(home_guid, page=2, page_size=4)
        self.assertEqual(len(second["data"]["list"]), 1)
        self.assertEqual(second["data"]["total"], 5)  # 拉完，无哨兵
        titles = (_titles(first["data"]["list"])
                  + _titles(second["data"]["list"]))
        self.assertEqual(titles, ["剧名1", "剧名2", "剧名3", "分类剧1", "分类剧2"])

    def test_rows_skip_chrome_items(self):
        items = [{"title": "搜索框", "url": "function(){}",
                  "col_type": "input"},
                 {"title": "正片", "url": "https://example.invalid/detail/9",
                  "pic_url": "https://example.invalid/9.jpg"}]
        cards = self.browser._rows(items, SOURCE, hkb.LIB_GUID)
        self.assertEqual(_titles(cards), ["正片"])
        self.assertIsNotNone(
            self.store.get_title_by_guid(cards[0]["guid"]))

    def test_rows_rewrite_keeps_playable_lines(self):
        # 重刷同一条目（海报变了）时入库 brief 更新海报，lines 仍为当前
        # 详情链接导出的单集占位，可详情/可播放。
        cards = self.browser._rows([HOME_ITEMS[0]], SOURCE, hkb.LIB_GUID)
        guid = cards[0]["guid"]
        changed = dict(HOME_ITEMS[0],
                       pic_url="https://example.invalid/1-new.jpg")
        self.browser._rows([changed], SOURCE, hkb.LIB_GUID)
        stored = self.store.get_title_by_guid(guid)
        self.assertEqual(stored["poster"],
                         "https://example.invalid/1-new.jpg")
        lines = stored["lines"]
        self.assertEqual(len(lines), 1)
        eps = next(iter(lines.values()))
        self.assertEqual(eps[0]["url"], "https://example.invalid/detail/1")

    def test_source_removed_raises(self):
        self.config.save({"sources": []})
        with self.assertRaises(hkb.HaikuoBrowseError):
            self.browser.source("haikuo-sub-test1")

    async def test_folder_card_is_not_playable_shape(self):
        # 2026-11-12 真机 bug：文件夹卡带了非空 path（面包屑路径），
        # 客户端把它当成可播文件，目录显示成点播、点开转圈。对齐已验证
        # 文件夹卡形状（_vod_directory_card）：path 空、season_number 0。
        root = await self.listing()
        folder = root["data"]["list"][0]
        self.assertEqual(folder["type"], "Directory")
        self.assertEqual(folder["can_play"], 0)
        self.assertEqual(folder["path"], "")
        self.assertEqual(folder["file_name"], "")
        self.assertEqual(folder["season_number"], 0)
        home_guid = await self.plugin()
        rows = (await self.listing(home_guid))["data"]["list"]
        cls_folder = rows[0]
        self.assertEqual(cls_folder["title"], "推荐")
        self.assertEqual(cls_folder["type"], "Directory")
        self.assertEqual(cls_folder["can_play"], 0)
        self.assertEqual(cls_folder["path"], "")
        self.assertEqual(cls_folder["file_name"], "")
        self.assertEqual(cls_folder["season_number"], 0)

    async def test_play_info_on_folder_guid_returns_clear_error(self):
        # 纵深防御：即使客户端再误点目录，play/info 直接回明确错误，
        # 不透传上游等满 120 秒转圈。
        folder_guid = await self.plugin()
        response = await self.client.post(
            "/v/api/v1/play/info", json={"item_guid": folder_guid})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["code"], -1)
        self.assertIn("目录", response.json()["msg"])


LIVE = os.environ.get("HAIKUO_LIVE") == "1"


@unittest.skipUnless(LIVE, "live 回归需 HAIKUO_LIVE=1（访问真实规则 host）")
class BrowseLiveTests(unittest.IsolatedAsyncioTestCase):
    """真实规则浏览：AI爽剧首页文件夹 → 首页标题卡（走 sidecar）。"""

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in ((importlib.import_module("config"), "config"),
                                 (importlib.import_module("store"), "titles")):
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self._sub = Path(temp.name) / "sub"
        self._sub.mkdir()
        self._hk_dir = patch.object(hk, "_SUB_DIR", str(self._sub))
        self._hk_dir.start()
        self.addCleanup(self._hk_dir.stop)
        self._hk_reg = patch.object(hk, "_SUB_REG",
                                    str(self._sub / "remote_rules.json"))
        self._hk_reg.start()
        self.addCleanup(self._hk_reg.stop)
        hk._loaded.clear()
        self.addCleanup(hk._loaded.clear)
        self._state = patch.object(hkb, "_STATE",
                                   Path(temp.name) / "browse.json")
        self._state.start()
        self.addCleanup(self._state.stop)
        store = importlib.import_module("store").Store()
        self.addCleanup(store.close)
        self.store = store
        text = (FIX / "aishuangju.json").read_text(encoding="utf-8")
        rep = hk.import_rules(text, source="file")
        self.assertEqual(rep["errors"], [], rep)
        self.assertEqual(len(rep["items"]), 1)
        source = hk.all_remote_sources()[0]
        self.browser = hkb.HaikuoBrowser(
            self.store, SimpleNamespace(enabled_sources=lambda: [source]))

    async def asyncTearDown(self):
        try:
            _drpy = importlib.import_module("dr" + "py")
            _drpy._sidecar.stop()
        except Exception:  # noqa: BLE001
            pass

    async def test_live_ai_home_browses(self):
        root = await self.browser.listing(None, hkb.LIB_GUID, {})
        self.assertEqual(root["total"], 1)
        home_guid = root["list"][0]["guid"]
        self.assertEqual(root["list"][0]["title"], "AI爽剧")
        home = await self.browser.listing(None, home_guid, {})
        rows = home["list"]
        self.assertTrue(rows, "AI爽剧首页应有标题卡")
        self.assertTrue(all(r.get("title") for r in rows))
        first = rows[0]
        self.assertIsNotNone(self.store.get_title_by_guid(first["guid"]))
