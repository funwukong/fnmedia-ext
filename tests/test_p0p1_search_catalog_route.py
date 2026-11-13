"""P0 第 6 项：libfilter 扩展分类库缺口 → brief 补 catalog_route + 并集归属。

覆盖：
- providers._map_vod 保留 type_id（OnlineTitle 新字段，缺失为空）；
- catalog.cat_for_source_type：(source_id, type_id) → 路由 cat；
- catalog._briefs_from_vods 补 catalog_route（含 4K 派生抄本的 cat 改写）；
- app 聚合搜索 brief 补 catalog_route（路由命中 / 未命中回退）；
- libfilter：路由门并集（只开扩展分类库时搜索可见）、纪录片归
  documentary 而非 movie、旧缓存（无 catalog_route）回退现有行为、
  非法路由 cat 被忽略；路由 cat 全集与 CATALOG_LIB_GUIDS 一致。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import catalog as catalogmod
import config as configmod
import history as histmod
import libfilter as libfiltermod
import providers
import sourcehealth
import store as storemod
import t4_media
import t4_service

GOOD = {"id": "good-src", "name": "Good",
        "api": "https://good.test/api.php/provide/vod/at/json", "enabled": True}
ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
         "is_admin": True, "token": "tok-admin"}


def _user(guid="u1"):
    return {"known": True, "guid": guid, "name": guid,
            "is_admin": False, "token": "tok-" + guid}


def _can_see(allow_map, hidden=()):
    hidden = set(hidden)

    def fake(user, key):
        if not key:
            return True
        if key in hidden:
            return False
        if not user or not user.get("known"):
            return False
        if user.get("is_admin"):
            return True
        allow = allow_map.get(user.get("guid", ""))
        if allow is None:
            return False
        return key in allow

    return fake


def _brief(guid, provider, title, type_name, route=None, lines=None):
    b = {"guid": guid, "online_id": "%s:vod-%s" % (provider, guid),
         "title": title, "type_name": type_name, "year": "2024",
         "poster": "", "overview": "", "remarks": "", "score": "",
         "actor": "", "director": "", "area": "",
         "lines": lines if lines is not None else
         {"%s::线路1" % provider: [{"name": "第1集",
                                   "url": "http://x/1.mp4"}]},
         "locator": {}}
    if route is not None:
        b["catalog_route"] = route
    return b


class MapVodTypeIdTests(unittest.TestCase):
    def test_map_vod_keeps_type_id(self):
        t = providers._map_vod({
            "vod_id": "7", "vod_name": "纪录大片", "type_name": "纪录片",
            "type_id": 7, "vod_year": "2024",
            "vod_play_from": "A",
            "vod_play_url": "正片$https://x.test/1.m3u8"}, GOOD)
        self.assertEqual(t.type_id, "7")

    def test_map_vod_missing_type_id_defaults_empty(self):
        t = providers._map_vod({
            "vod_id": "8", "vod_name": "无类片", "type_name": "电影",
            "vod_play_from": "A",
            "vod_play_url": "正片$https://x.test/1.m3u8"}, GOOD)
        self.assertEqual(t.type_id, "")


class CatalogRouteLookupTests(unittest.TestCase):
    def test_cat_for_source_type(self):
        cat = catalogmod.Catalog()
        cat._classes = {
            "src1": [{"type_id": 5, "type_name": "纪录片", "cat": "documentary"},
                     {"type_id": "6", "type_name": "短剧", "cat": "shortdrama"}],
        }
        self.assertEqual(cat.cat_for_source_type("src1", 5), "documentary")
        self.assertEqual(cat.cat_for_source_type("src1", "5"), "documentary")
        self.assertEqual(cat.cat_for_source_type("src1", "6"), "shortdrama")
        self.assertIsNone(cat.cat_for_source_type("src1", 999))
        self.assertIsNone(cat.cat_for_source_type("nope", 5))
        self.assertIsNone(cat.cat_for_source_type("src1", ""))
        self.assertIsNone(cat.cat_for_source_type("src1", None))

    def test_briefs_from_vods_adds_catalog_route(self):
        cat = catalogmod.Catalog()
        vods = [{"vod_id": "9", "vod_name": "纪录大片", "type_name": "纪录片",
                 "vod_year": "2024",
                 "vod_play_from": "A",
                 "vod_play_url": "正片$https://x.test/1.m3u8"}]
        briefs = cat._briefs_from_vods(
            vods, GOOD, {"source_id": "good-src", "type_id": 5},
            catalogmod.AGG_SCOPE, "documentary")
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["catalog_route"],
                         {"source": "good-src", "type_id": 5,
                          "cat": "documentary"})
        self.assertEqual(briefs[0]["catalog_cat"], "documentary")


class LibfilterRouteTests(unittest.TestCase):
    def test_route_door_union_with_type_door(self):
        b = _brief("r1", "baofeng", "动作大片", "动作片",
                   route={"source": "baofeng", "type_id": "5",
                          "cat": "documentary"})
        self.assertEqual(libfiltermod.brief_lib_keys(b), {
            "standalone:baofeng", "catalog:movie", "catalog:documentary"})

    def test_extension_only_library_sees_routed_title(self):
        # 只开 catalog:shortdrama：类型名（连续剧→tv）不开门，路由开门。
        b = _brief("r2", "baofeng", "竖屏短剧", "连续剧",
                   route={"source": "baofeng", "type_id": "6",
                          "cat": "shortdrama"})
        kept = libfiltermod.filter_briefs(
            [b], lambda k: k == "catalog:shortdrama")
        self.assertEqual([x["guid"] for x in kept], ["r2"])
        kept = libfiltermod.filter_briefs(
            [b], lambda k: k == "catalog:tv")
        self.assertEqual([x["guid"] for x in kept], ["r2"])

    def test_documentary_type_without_route_goes_documentary(self):
        b = _brief("r3", "baofeng", "纪录大片", "纪录片")
        self.assertEqual(libfiltermod.brief_lib_keys(b), {
            "standalone:baofeng", "catalog:documentary"})
        self.assertNotIn("catalog:movie",
                         libfiltermod.brief_lib_keys(b))

    def test_old_cache_without_route_falls_back(self):
        # 无 catalog_route 字段（旧缓存）：与改前同一可见集。
        b = _brief("r4", "baofeng", "动作大片", "动作片")
        self.assertEqual(libfiltermod.brief_lib_keys(b), {
            "standalone:baofeng", "catalog:movie"})
        b = _brief("r5", "baofeng", "纪录大片", "纪录片")
        self.assertEqual(libfiltermod.brief_lib_keys(b), {
            "standalone:baofeng", "catalog:documentary"})

    def test_invalid_route_cat_ignored(self):
        for bad in ({"source": "baofeng", "type_id": "5", "cat": "bogus"},
                    {"source": "baofeng", "type_id": "5", "cat": ""},
                    {"source": "baofeng", "type_id": "5"},
                    "not-a-dict"):
            b = _brief("r6", "baofeng", "动作大片", "动作片", route=bad)
            self.assertEqual(libfiltermod.brief_lib_keys(b), {
                "standalone:baofeng", "catalog:movie"})

    def test_route_cats_match_catalog_lib_guids(self):
        self.assertEqual(libfiltermod._KNOWN_CATALOG_CATS,
                         set(appmod.CATALOG_LIB_GUIDS))

    def test_catalog_agg_brief_route_matches_cat(self):
        b = _brief("r7", "baofeng", "纪录大片", "纪录片",
                   route={"source": "baofeng", "type_id": "5",
                          "cat": "documentary"})
        b.update({"catalog_cat": "documentary",
                  "catalog_scope": "__agg__", "catalog_source": "baofeng"})
        self.assertEqual(libfiltermod.brief_lib_keys(b), {
            "catalog:documentary", "standalone:baofeng"})


class AggregateRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [GOOD],
                          "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
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
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        appmod._AGG_CACHE.clear()
        appmod._AGG_INFLIGHT.clear()
        sourcehealth.reset("good-src")
        self.addCleanup(lambda: sourcehealth.reset("good-src"))
        # 路由表种子：good-src 的 type_id 5 → documentary。
        catalogmod.catalog._classes = {
            "good-src": [{"type_id": 5, "type_name": "纪录片",
                          "cat": "documentary"}]}
        self.addCleanup(catalogmod.catalog.reset)

    def _doc_title(self, type_id="5"):
        return providers._map_vod({
            "vod_id": "doc1", "vod_name": "纪录大片", "type_name": "纪录片",
            "type_id": type_id, "vod_year": "2024",
            "vod_play_from": "A",
            "vod_play_url": "正片$https://good.test/1.m3u8"}, GOOD)

    async def test_aggregate_brief_carries_catalog_route(self):
        with patch.object(providers, "search_all",
                          return_value=[self._doc_title()]):
            briefs = await appmod._aggregate_briefs("纪录片", refresh=True)
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["catalog_route"],
                         {"source": "good-src", "type_id": "5",
                          "cat": "documentary"})

    async def test_aggregate_brief_route_miss_falls_back(self):
        with patch.object(providers, "search_all",
                          return_value=[self._doc_title(type_id="999")]):
            briefs = await appmod._aggregate_briefs("纪录片", refresh=True)
        self.assertEqual(briefs[0]["catalog_route"],
                         {"source": "good-src", "type_id": "999", "cat": ""})
        # 无路由时仍按类型名归 documentary（不回 movie）。
        self.assertEqual(libfiltermod.brief_lib_keys(briefs[0]), {
            "standalone:good-src", "catalog:documentary"})

    async def test_online_search_visible_via_extension_library_only(self):
        # 只开 catalog:documentary 的账号：聚合搜索能看到路由命中的纪录片。
        with patch.object(providers, "search_all",
                          return_value=[self._doc_title()]), \
                patch.object(appmod.fnusersmod, "resolve",
                             lambda r: _user("u1")), \
                patch.object(appmod.vlibsmod, "account_can_see",
                             _can_see({"u1": {"catalog:documentary"}})):
            req = SimpleNamespace(headers={}, state=SimpleNamespace())
            cards = await appmod.online_search("纪录片", None, request=req)
        self.assertEqual(len(cards), 1)


if __name__ == "__main__":
    unittest.main()
