"""搜索 / 详情按虚拟库过滤（decisions-2-libfilter）。

覆盖：
- libfilter 映射（纯函数）：T4 / 分类库 agg / 分类库单源 / 聚合搜索
  （独立库 + 分类库双门）/ 无法映射拒绝 / 与 inject.hot_category 一致。
- app 接线：_viewer_can_open_key（只复用 _viewer_can_open_lib 的身份链）、
  online_search 按查看者过滤（含缓存复用时按人重滤）、item_detail
  隐藏库 -6（标题 / 库根 / 榜单绑定 / 季 / 集 / 红果）、search_list 红果门。

身份语义与第 1 项（decisions-2-failclosed）一致，但本文件不依赖它
的合入顺序：account_can_see / fnusers.resolve 一律用桩代替——桩按
目标 fail-closed 语义实现（未知身份→隐藏，未配置账号→隐藏，
已配置账号→只看白名单，管理员→全看）。第 1 项合入前后本文件行为一致。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import catalog as catalogmod
import inject as injmod
import libfilter as libfiltermod

ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
         "is_admin": True, "token": "tok-admin"}
UNKNOWN = {"known": False, "guid": "", "name": "", "is_admin": False,
           "token": "tok-x"}


def _user(guid="u1"):
    return {"known": True, "guid": guid, "name": guid,
            "is_admin": False, "token": "tok-" + guid}


def _can_see(allow_map, hidden=()):
    """目标 fail-closed 语义的 account_can_see 桩（见模块文档）。"""
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


def _brief(guid, provider, title, type_name, lines=None, locator=None):
    return {
        "guid": guid, "online_id": f"{provider}:vod-{guid}", "title": title,
        "type_name": type_name, "year": "2024", "poster": "", "overview": "",
        "remarks": "", "score": "", "actor": "", "director": "", "area": "",
        "lines": lines if lines is not None else
        {f"{provider}::线路1": [{"name": "第1集", "url": "http://x/1.mp4"}]},
        "locator": locator if locator is not None else {},
    }


BRIEF_A = _brief("test-title-a", "baofeng", "动作大片", "动作片")
BRIEF_B = _brief("test-title-b", "ffzy", "国产剧集", "国产剧")
BRIEF_T4 = _brief("test-title-t4", "t4src", "T4剧集", "连续剧",
                  lines={"T4线路": [{"name": "第1集", "title": "第1集",
                                    "play_from": "T4线路",
                                    "url": "http://x/t4.mp4"}]},
                  locator={"kind": "t4", "source_id": "t4src"})


# ---------- libfilter 映射（纯函数） ----------

def test_t4_brief_maps_to_t4_only():
    assert libfiltermod.brief_lib_keys(BRIEF_T4) == {"t4"}


def test_search_brief_standalone_and_catalog_doors():
    # 动作片：独立库 baofeng + 聚合分类库 catalog:movie（ANY 语义见模块文档）
    assert libfiltermod.brief_lib_keys(BRIEF_A) == {
        "standalone:baofeng", "catalog:movie"}


def test_search_brief_multi_provider_lines():
    b = _brief("test-m", "baofeng", "多源剧", "韩剧", lines={
        "baofeng::线路1": [{"name": "第1集", "url": "http://x/1.mp4"}],
        "ffzy::线路2": [{"name": "第1集", "url": "http://x/2.mp4"}]})
    assert libfiltermod.brief_lib_keys(b) == {
        "standalone:baofeng", "standalone:ffzy", "catalog:tv"}


def test_search_brief_online_id_fallback_when_no_lines():
    b = _brief("test-nolines", "baofeng", "无线路片", "动作片", lines={})
    assert libfiltermod.brief_lib_keys(b) == {
        "standalone:baofeng", "catalog:movie"}


def test_catalog_agg_brief():
    b = dict(BRIEF_A, catalog_cat="movie", catalog_scope="__agg__",
             catalog_source="baofeng")
    assert libfiltermod.brief_lib_keys(b) == {
        "catalog:movie", "standalone:baofeng"}


def test_catalog_single_source_brief():
    b = dict(BRIEF_A, catalog_cat="tv", catalog_scope="ffzy",
             catalog_source="ffzy")
    # 单源作用域只在该源独立库出现，不进聚合分类库。
    assert libfiltermod.brief_lib_keys(b) == {"standalone:ffzy"}


def test_catalog_brief_missing_cat_and_source_unmappable():
    b = {"catalog_scope": "__agg__"}
    assert libfiltermod.brief_lib_keys(b) == set()
    assert libfiltermod.brief_visible(b, lambda k: True) is False


def test_gkey_mapping_via_callback():
    b = _brief("test-g", "guard-src", "加固片", "动作片")
    keys = libfiltermod.brief_lib_keys(
        b, gkey_of=lambda p: "zone1" if p == "guard-src" else None)
    assert keys == {"standalone:guard-src", "guard:zone1", "catalog:movie"}


def test_type_mapping_matches_hot_category():
    # 与 inject.hot_category 同规则（纪录片除外，见下）；类型缺失也按 tv（hot_category 默认）。
    for typ in ("动漫", "日本动漫", "综艺", "真人秀", "动作片",
                "韩剧", "国产剧", "连续剧", "", "  "):
        assert libfiltermod.catalog_cat_for_type(typ) == \
            injmod.hot_category({"type_name": typ})
    assert catalogmod.AGG_SCOPE == libfiltermod.AGG_SCOPE


def test_documentary_type_goes_to_documentary_not_movie():
    # P0-6 修正错放：类型名含“纪录片”归 catalog:documentary 而非 movie。
    # 与 hot_category 有意分歧：热门库只有四张，纪录片在热门侧仍归 movie。
    assert libfiltermod.catalog_cat_for_type("纪录片") == "documentary"
    assert injmod.hot_category({"type_name": "纪录片"}) == "movie"


def test_filter_briefs_keeps_order_and_needs_any_door():
    briefs = [BRIEF_A, BRIEF_B, BRIEF_T4]
    # 只开 catalog:movie：A 经分类库门可见；B、T4 全部门关闭。
    kept = libfiltermod.filter_briefs(
        briefs, lambda k: k == "catalog:movie")
    assert [b["guid"] for b in kept] == ["test-title-a"]
    # 只开 standalone:ffzy：B 经独立库门可见。
    kept = libfiltermod.filter_briefs(
        briefs, lambda k: k == "standalone:ffzy")
    assert [b["guid"] for b in kept] == ["test-title-b"]


def test_filter_briefs_no_viewer_context_denies_all():
    assert libfiltermod.filter_briefs([BRIEF_A], None) == []
    assert libfiltermod.brief_visible(BRIEF_A, None) is False



# ---------- app 接线：_viewer_can_open_key ----------

class ViewerKeyTests(unittest.TestCase):
    def setUp(self):
        self.patches = []
        def _patch_obj(target, attr, *args, **kwargs):
            _p = patch.object(target, attr, *args, **kwargs)
            _p.start()
            self.patches.append(_p)
        self._patch_obj = _patch_obj

    def tearDown(self):
        # 逆序停：同一属性多次打桩时，正序停会把中间值漏到测试外。
        for p in reversed(self.patches):
            p.stop()

    def _viewer(self, user, allow_map):
        self._patch_obj(appmod.fnusersmod, "resolve",
                           return_value=user)
        self._patch_obj(appmod.vlibsmod, "account_can_see",
                           _can_see(allow_map))
        return SimpleNamespace(headers={})

    def test_open_key_delegates_to_existing_chain(self):
        req = self._viewer(ADMIN, {})
        assert appmod._viewer_can_open_key(req, "catalog:movie") is True
        assert appmod._viewer_can_open_key(req, "standalone:baofeng") is True
        assert appmod._viewer_can_open_key(req, "t4") is True
        req = self._viewer(UNKNOWN, {})
        assert appmod._viewer_can_open_key(req, "catalog:movie") is False
        req = self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert appmod._viewer_can_open_key(req, "catalog:movie") is True
        assert appmod._viewer_can_open_key(req, "catalog:tv") is False
        assert appmod._viewer_can_open_key(req, "standalone:baofeng") is False

    def test_open_key_invalid_or_unmapped_denied(self):
        req = self._viewer(ADMIN, {})
        assert appmod._viewer_can_open_key(req, "") is False
        assert appmod._viewer_can_open_key(req, "bogus:x") is False
        assert appmod._viewer_can_open_key(req, "chart:nope") is False
        assert appmod._viewer_can_open_key(None, "catalog:movie") is False


# ---------- app 接线：online_search ----------

class OnlineSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        self.patches = []

        async def _fake_agg(keyword, *a, **kw):
            return [dict(b) for b in (BRIEF_A, BRIEF_B, BRIEF_T4)]

        def _patch_obj(target, attr, *args, **kwargs):
            _p = patch.object(target, attr, *args, **kwargs)
            _p.start()
            self.patches.append(_p)
        self._patch_obj = _patch_obj
        self._patch_obj(appmod, "_aggregate_briefs", _fake_agg)

    async def asyncTearDown(self):
        # 逆序停：同一属性多次打桩时，正序停会把中间值漏到测试外。
        for p in reversed(self.patches):
            p.stop()
        appmod._search_cache.clear()
        appmod._search_inflight.clear()

    def _viewer(self, user, allow_map):
        self._patch_obj(appmod.fnusersmod, "resolve",
                           return_value=user)
        self._patch_obj(appmod.vlibsmod, "account_can_see",
                           _can_see(allow_map))
        return SimpleNamespace(headers={})

    def _titles(self, cards):
        return [c["title"].replace("【在线】", "").split(" ·")[0]
                for c in cards]

    async def test_search_filters_by_library_and_recaches_per_viewer(self):
        # 同一关键词先后被不同账号搜：缓存的是未过滤 brief，每次按人重滤。
        kw = "libfilter-cache-kw"
        admin = await appmod.online_search(
            kw, None, request=self._viewer(ADMIN, {}))
        assert self._titles(admin) == ["动作大片", "国产剧集", "T4剧集"]
        unknown = await appmod.online_search(
            kw, None, request=self._viewer(UNKNOWN, {}))
        assert unknown == []
        u9 = await appmod.online_search(
            kw, None, request=self._viewer(_user("u9"), {}))
        assert u9 == []
        u1 = await appmod.online_search(
            kw, None, request=self._viewer(
                _user("u1"), {"u1": {"catalog:movie"}}))
        assert self._titles(u1) == ["动作大片"]
        u2 = await appmod.online_search(
            kw, None, request=self._viewer(
                _user("u2"), {"u2": {"standalone:ffzy", "t4"}}))
        assert self._titles(u2) == ["国产剧集", "T4剧集"]

    async def test_search_without_request_denies_all(self):
        cards = await appmod.online_search("libfilter-noreq-kw", None)
        assert cards == []

# ---------- app 接线：item_detail / search_list（ASGI） ----------

class DetailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.patches = []
        def _patch_obj(target, attr, *args, **kwargs):
            _p = patch.object(target, attr, *args, **kwargs)
            _p.start()
            self.patches.append(_p)
        self._patch_obj = _patch_obj

        async def _noop_detail(client, guid, **kw):
            return None

        self._patch_obj(appmod, "t4_service",
                           SimpleNamespace(ensure_detail=_noop_detail))
        # ASGI 测试不跑 lifespan，app.state.ext 不存在；_load_t4_detail
        # 的 ext_client(request) 调用须桩掉（ensure_detail 桩本身不用它）。
        self._patch_obj(appmod, "ext_client", lambda request: None)
        self._patch_obj(appmod, "chart_cache",
                           SimpleNamespace(items=lambda cat: [],
                                           is_chart_guid=lambda g: False,
                                           get_guid=lambda g: None))

    async def asyncTearDown(self):
        # 逆序停：同一属性多次打桩时，正序停会把中间值漏到测试外。
        for p in reversed(self.patches):
            p.stop()

    def _viewer(self, user, allow_map):
        self._patch_obj(appmod.fnusersmod, "resolve",
                           return_value=user)
        self._patch_obj(appmod.vlibsmod, "account_can_see",
                           _can_see(allow_map))

    async def _item(self, guid):
        r = await self.client.get(f"/v/api/v1/item/{guid}")
        assert r.status_code == 200
        return r.json()

    async def test_detail_title_allowed_and_denied(self):
        self._patch_obj(appmod.store, "get_title_by_guid",
                           lambda g: dict(BRIEF_A) if g == "test-title-a"
                           else None)
        self._viewer(ADMIN, {})
        body = await self._item("test-title-a")
        assert body["code"] == 0
        assert body["data"]["title"] == "动作大片"
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert (await self._item("test-title-a"))["code"] == 0
        self._viewer(_user("u1"), {"u1": {"catalog:tv"}})
        assert (await self._item("test-title-a"))["code"] == -6
        self._viewer(UNKNOWN, {})
        assert (await self._item("test-title-a"))["code"] == -6
        self._viewer(_user("u9"), {})
        assert (await self._item("test-title-a"))["code"] == -6

    async def test_detail_lib_root_denied_and_allowed(self):
        guid = appmod.CHART_LIB_GUIDS["movie"]
        self._viewer(UNKNOWN, {})
        assert (await self._item(guid))["code"] == -6
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert (await self._item(guid))["code"] == -6
        self._viewer(ADMIN, {})
        body = await self._item(guid)
        assert body["code"] == 0

    async def test_detail_chart_bound_title_uses_chart_door(self):
        guid = "test-chart-entry"
        meta = {"cat": "movie", "title": "榜单片", "cover": "",
                "rating": "", "desc": "", "year": "2024"}
        self._patch_obj(
            appmod, "chart_cache",
            SimpleNamespace(
                items=lambda cat: [],
                is_chart_guid=lambda g: g == guid,
                get_guid=lambda g: dict(meta) if g == guid else None))
        self._patch_obj(appmod.store, "get_title_by_guid",
                           lambda g: dict(BRIEF_A) if g == guid else None)
        # 只开 chart:movie：榜单库这扇门就够了（标题归属库全关也不挡）。
        self._viewer(_user("u1"), {"u1": {"chart:movie"}})
        assert (await self._item(guid))["code"] == 0
        # 全关：-6。
        self._viewer(_user("u1"), {"u1": set()})
        assert (await self._item(guid))["code"] == -6
        # 只开 catalog:movie：标题归属库门也放行。
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert (await self._item(guid))["code"] == 0

    async def test_detail_chart_unbound_skeleton_uses_chart_door(self):
        guid = "test-chart-unbound"
        meta = {"cat": "tv", "title": "未绑定榜单片", "cover": "",
                "rating": "", "desc": "", "year": "2024"}
        self._patch_obj(
            appmod, "chart_cache",
            SimpleNamespace(
                items=lambda cat: [],
                is_chart_guid=lambda g: g == guid,
                get_guid=lambda g: dict(meta) if g == guid else None))
        self._patch_obj(appmod, "start_resolve_chart",
                           lambda g: None)
        self._patch_obj(appmod.store, "get_title_by_guid",
                           lambda g: None)
        self._viewer(_user("u1"), {"u1": set()})
        assert (await self._item(guid))["code"] == -6
        self._viewer(_user("u1"), {"u1": {"chart:tv"}})
        body = await self._item(guid)
        assert body["code"] == 0
        assert body["data"]["title"] == "未绑定榜单片"

    async def test_detail_season_denied_and_allowed(self):
        parent, season_guid = "test-title-b", "test-season-b"
        season = {"guid": season_guid, "season": 1,
                  "episode_guids": ["test-ep-b1"], "online_guid": parent}
        self._patch_obj(appmod.store, "get_title_by_guid",
                           lambda g: dict(BRIEF_B) if g == parent else None)
        self._patch_obj(appmod.store, "get_season",
                           lambda g: dict(season) if g == season_guid else None)
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert (await self._item(season_guid))["code"] == -6
        self._viewer(_user("u1"), {"u1": {"catalog:tv"}})
        body = await self._item(season_guid)
        assert body["code"] == 0
        assert body["data"]["type"] == "Season"

    async def test_detail_episode_denied_and_allowed(self):
        parent, ep_guid = "test-title-b", "test-ep-b1"
        ep = {"ep_guid": ep_guid, "online_guid": parent,
              "season_guid": "test-season-b", "season": 1,
              "episode_number": 1, "index": 0, "ep_title": "第1集"}
        self._patch_obj(appmod.store, "get_title_by_guid",
                           lambda g: dict(BRIEF_B) if g == parent else None)
        self._patch_obj(appmod.store, "get_episode",
                           lambda g: dict(ep) if g == ep_guid else None)
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert (await self._item(ep_guid))["code"] == -6
        self._viewer(_user("u1"), {"u1": {"standalone:ffzy"}})
        body = await self._item(ep_guid)
        assert body["code"] == 0
        assert body["data"]["type"] == "Episode"

    async def test_search_list_hgdj_gate(self):
        def official(request):
            return httpx.Response(200, json={"code": 0, "data": []})

        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(official))
        self.addAsyncCleanup(upstream.aclose)
        self._patch_obj(appmod, "up_client", lambda request: upstream)

        async def _no_online(keyword, client, **kw):
            return []

        self._patch_obj(appmod, "online_search", _no_online)
        self._patch_obj(appmod, "hgdj_enabled", lambda: True)
        self._patch_obj(appmod.hgdjmod, "search_items",
                           lambda q: [{"guid": "hgdj-series:abc",
                                       "title": "红果剧", "type": "TV",
                                       "poster": "", "overview": ""}])
        self._patch_obj(appmod.vlibsmod, "account_online_allowed",
                           lambda user: True)
        self._viewer(ADMIN, {})
        r = await self.client.get("/v/api/v1/search/list",
                                  params={"q": "红果"})
        assert r.status_code == 200
        assert any(d.get("guid") == "hgdj-series:abc"
                   for d in r.json()["data"])
        self._viewer(_user("u1"), {"u1": set()})
        r = await self.client.get("/v/api/v1/search/list",
                                  params={"q": "红果"})
        assert r.status_code == 200
        assert all(d.get("guid") != "hgdj-series:abc"
                   for d in r.json()["data"])
