"""P0-3：收藏夹与继续观看未按库过滤、播放链可绕过（fail-closed 的洞）。

覆盖：
- marks 按账号分区（history.py 同款模式）：分账号隔离、已看同样分区、
  未识别身份走全局分区、旧全局记录只由管理员首次访问继承一次（复制）。
- 收藏夹 favorite/list：分账号隔离 + 按库过滤；brief 淘汰后用收藏时存下
  的归属库键兜底，无键无 brief 时隐藏；ext/favorite/enrich 同口径。
- 继续观看 ext/continue 与播放列表 play/list：按库过滤（权限收回后旧记录
  不再出现）。
- 播放链 play/info、stream/list、stream、play/play、media/range、preset、
  main、seg、direct：无权返回 -6（JSON 接口 200 体内 code -6，字节流类
  路由 403 体内 code -6）；有权仍可播；季/集直达断点续播不受影响。

只测进程内逻辑与假数据，不碰真实网络与飞牛。
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import marks as marksmod
import store as storemod
import vlibs as vlibsmod
from marks import Marks
from store import media_guid, video_guid

ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
         "is_admin": True, "token": "tok-admin"}
UNKNOWN = {"known": False, "guid": "", "name": "", "is_admin": False,
           "token": "tok-x"}


def _user(guid="u1"):
    return {"known": True, "guid": guid, "name": guid,
            "is_admin": False, "token": "tok-" + guid}


def _can_see(allow_map, hidden=()):
    """目标 fail-closed 语义的 account_can_see 桩（与 test_libfilter 同款）。"""
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


@contextmanager
def as_marks_user(guid, admin=False):
    tokens = marksmod.set_current_user(guid, admin)
    try:
        yield
    finally:
        marksmod.reset_current_user(tokens)


def _brief(guid, provider, title, type_name, episodes):
    return {
        "guid": guid, "online_id": f"{provider}:vod-{guid}",
        "title": title, "type_name": type_name, "year": "2024",
        "poster": "http://x/p.jpg", "overview": "", "remarks": "",
        "score": "", "actor": "", "director": "", "area": "",
        "lines": {f"{provider}::线路1": [
            {"name": name, "title": name, "direct": True, "url": url}
            for name, url in episodes]},
        "locator": {},
    }


BRIEF_MOVIE = _brief("online:movie-a", "src1", "电影A", "电影",
                     [("正片", "https://x/movie-a.m3u8")])
BRIEF_TV = _brief("online:tv-b", "src2", "剧集B", "电视剧",
                  [("第1集", "https://x/tv-b-1.m3u8"),
                   ("第2集", "https://x/tv-b-2.m3u8")])
BRIEF_T4 = _brief("online:t4-c", "t4src", "T4剧", "连续剧",
                  [("第1集", "https://x/t4-c-1.m3u8")])
BRIEF_T4["locator"] = {"kind": "t4"}


# ---------- marks 分区（单元） ----------

class MarksPartitionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._p = patch.object(marksmod, "_STATE",
                               Path(self._tmp.name) / "marks.json")
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_partitions_isolated(self):
        m = Marks()
        m.add_favorite("g1", {"title": "片1"}, user="u1")
        m.add_favorite("g2", {"title": "片2"}, user="u2")
        m.set_watched("g1", user="u1")
        m.set_watched("g2", user="u2")
        assert m.favorite_guids(user="u1") == {"g1"}
        assert m.favorite_guids(user="u2") == {"g2"}
        assert m.is_favorite("g2", user="u1") is False
        assert m.is_watched("g1", user="u2") is False
        assert [r["guid"] for r in m.favorite_records(user="u1")] == ["g1"]
        # 删 u1 的不影响 u2
        assert m.remove_favorite("g1", user="u1") is True
        assert m.favorite_guids(user="u2") == {"g2"}

    def test_contextvar_partition(self):
        m = Marks()
        with as_marks_user("u1"):
            m.add_favorite("g1", {"title": "片1"})
            assert m.favorite_guids() == {"g1"}
        with as_marks_user("u2"):
            assert m.favorite_guids() == set()
            assert m.is_favorite("g1") is False
        with as_marks_user("u1"):
            assert m.favorite_guids() == {"g1"}

    def test_legacy_flat_loads_into_global(self):
        p = Path(self._tmp.name) / "marks.json"
        p.write_text(json.dumps({
            "favorites": {"gold": {"guid": "gold", "ts": 1, "title": "老片"}},
            "watched": {"gold": {"guid": "gold", "ts": 1}}}),
            encoding="utf-8")
        m = Marks()
        assert m.favorite_guids() == {"gold"}
        assert m.favorite_guids(user="u1") == set()
        assert m.is_watched("gold") is True

    def test_admin_adopts_global_once_kid_starts_empty(self):
        p = Path(self._tmp.name) / "marks.json"
        p.write_text(json.dumps({
            "favorites": {"gold": {"guid": "gold", "ts": 1, "title": "老片"}},
            "watched": {}}), encoding="utf-8")
        m = Marks()
        with as_marks_user("kid"):
            assert m.favorite_guids() == set()
        with as_marks_user("admin-guid", admin=True):
            assert m.favorite_guids() == {"gold"}
        # 全局分区仍保留（复制式继承），子账号始终看不到
        assert m.favorite_guids(user="") == {"gold"}
        with as_marks_user("kid"):
            assert m.favorite_guids() == set()
        # 继承标记已落盘：重载后管理员仍有，kid 仍没有
        m2 = Marks()
        assert m2.favorite_guids(user="admin-guid") == {"gold"}
        assert m2.favorite_guids(user="kid") == set()

    def test_persistence_roundtrip_new_format(self):
        m = Marks()
        m.add_favorite("g1", {"title": "片1", "libs": ["catalog:movie"]},
                       user="u1")
        m.add_favorite("g2", {"title": "片2"}, user="u2")
        m2 = Marks()
        assert m2.favorite_guids(user="u1") == {"g1"}
        assert m2.favorite_guids(user="u2") == {"g2"}
        assert m2.favorite_records(user="u1")[0].get("libs") == \
            ["catalog:movie"]
        assert m2.favorite_guids(user="") == set()


# ---------- app 接线：收藏 / 继续观看 / 播放链（ASGI） ----------

class PlaychainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.patches = []

        def _patch_obj(target, attr, *args, **kwargs):
            item = patch.object(target, attr, *args, **kwargs)
            item.start()
            self.patches.append(item)
            return item

        self._patch_obj = _patch_obj
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history"),
                                 (marksmod, "marks")]:
            _patch_obj(module, "_STATE",
                       Path(temp.name) / (filename + ".json"))
        self.config = configmod.Config()
        self.config.save({"sources": [],
                          "removed_ids": []})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        self.marks = Marks()
        for module, name, value in [
            (appmod, "config", self.config), (appmod, "store", self.store),
            (appmod, "history", self.history), (appmod, "marks", self.marks),
            (storemod, "store", self.store),
            (appmod, "_schedule_routes_refresh", lambda: None),
            (appmod, "_autofail_on", lambda: False),
        ]:
            _patch_obj(module, name, value)
        # T4 详情加载与上游客户端桩掉（不跑 lifespan、不联网）
        async def _noop_detail(client, guid, **kw):
            return None

        _patch_obj(appmod, "t4_service",
                   SimpleNamespace(ensure_detail=_noop_detail))
        _patch_obj(appmod, "ext_client", lambda request: None)
        _patch_obj(appmod, "chart_cache",
                   SimpleNamespace(items=lambda cat: [],
                                   is_chart_guid=lambda g: False,
                                   get_guid=lambda g: None))

        def official(request):
            path = request.url.path
            if path.endswith("/favorite/list"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "list": [], "total": 0}})
            return httpx.Response(200, json={"code": 0, "data": []})

        self.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(official))
        self.addAsyncCleanup(self.upstream.aclose)
        _patch_obj(appmod, "up_client", lambda request: self.upstream)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        # 种子标题：电影 A（src1/电影→catalog:movie）、剧集 B（src2/电视剧→catalog:tv）
        self.store.remember_merged([copy.deepcopy(BRIEF_MOVIE),
                                    copy.deepcopy(BRIEF_TV)])
        self.mg_movie = media_guid("online:movie-a")
        self.mg_tv = media_guid("online:tv-b")

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _viewer(self, user, allow_map):
        self._patch_obj(appmod.fnusersmod, "resolve", return_value=user)
        self._patch_obj(appmod.vlibsmod, "account_can_see",
                        _can_see(allow_map))

    @staticmethod
    def _fav_titles(body):
        return [row["title"].replace("【在线】", "").split(" ·")[0]
                for row in body["data"]["list"]]

    def _seed_favorites(self):
        self.marks.add_favorite("online:movie-a", {"title": "电影A",
                                                   "type": "Movie"},
                                user="u1")
        self.marks.add_favorite("online:tv-b", {"title": "剧集B",
                                                "type": "TV"}, user="u1")
        self.marks.add_favorite("online:movie-a", {"title": "电影A",
                                                   "type": "Movie"},
                                user="u2")
        # 管理员自己的分区同样各备一份（分区隔离：管理员看不到 u1 的记录）
        self.marks.add_favorite("online:movie-a", {"title": "电影A",
                                                   "type": "Movie"},
                                user="admin-guid")
        self.marks.add_favorite("online:tv-b", {"title": "剧集B",
                                                "type": "TV"},
                                user="admin-guid")

    def _seed_history(self):
        mg_a, mg_b = self.mg_movie, self.mg_tv
        self.history.record({"item_guid": "online:movie-a", "ts": 60,
                             "duration": 600, "media_guid": mg_a,
                             "video_guid": video_guid("online:movie-a")},
                            user="u1")
        self.history.record({"item_guid": "online:tv-b", "ts": 60,
                             "duration": 600, "media_guid": mg_b,
                             "video_guid": video_guid("online:tv-b")},
                            user="u1")

    # ---------- 收藏夹 ----------

    async def test_favorite_list_isolated_per_account(self):
        self._seed_favorites()
        self._viewer(_user("u1"), {"u1": {"catalog:movie", "catalog:tv"}})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert body["code"] == 0
        assert sorted(self._fav_titles(body)) == ["剧集B", "电影A"]
        self._viewer(_user("u2"), {"u2": {"catalog:movie", "catalog:tv"}})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert self._fav_titles(body) == ["电影A"]
        self._viewer(UNKNOWN, {})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert self._fav_titles(body) == []

    async def test_favorite_list_filtered_by_library(self):
        self._seed_favorites()
        # u1 两部都收藏过；收回 catalog:tv 后收藏夹只剩电影 A。
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert self._fav_titles(body) == ["电影A"]
        self._viewer(ADMIN, {})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert sorted(self._fav_titles(body)) == ["剧集B", "电影A"]

    async def test_favorite_add_route_stores_lib_keys(self):
        # 经 PUT 收藏路由写入的记录带归属库键，供 brief 淘汰后兜底过滤。
        self._viewer(_user("u1"), {"u1": {"catalog:movie", "catalog:tv"}})
        r = await self.client.put("/v/api/v1/item/favorite",
                                  json={"guid": "online:movie-a"})
        assert r.json()["code"] == 0
        recs = self.marks.favorite_records(user="u1")
        assert len(recs) == 1
        assert set(recs[0].get("libs") or []) == \
            {"standalone:src1", "catalog:movie"}

    async def test_favorite_list_libs_fallback_when_brief_evicted(self):
        # store 里已无该标题：有归属键的按键过滤，无键无 brief 的一律隐藏。
        self.marks.add_favorite("online:gone", {"title": "已下架片",
                                                "type": "Movie",
                                                "libs": ["catalog:movie"]},
                                user="u1")
        self.marks.add_favorite("online:gone2", {"title": "无键旧片",
                                                 "type": "Movie"}, user="u1")
        self.marks.add_favorite("online:gone", {"title": "已下架片",
                                                "type": "Movie",
                                                "libs": ["catalog:movie"]},
                                user="admin-guid")
        self.marks.add_favorite("online:gone2", {"title": "无键旧片",
                                                 "type": "Movie"},
                                user="admin-guid")
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert self._fav_titles(body) == ["已下架片"]
        self._viewer(_user("u1"), {"u1": {"catalog:tv"}})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        assert self._fav_titles(body) == []
        self._viewer(ADMIN, {})
        body = (await self.client.post("/v/api/v1/favorite/list",
                                       json={})).json()
        # 管理员：有键的可见，无键无 brief 的仍隐藏（fail-closed）
        assert self._fav_titles(body) == ["已下架片"]

    async def test_favorite_enrich_filtered_same_as_list(self):
        self._seed_favorites()
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        body = (await self.client.post("/v/api/ext/favorite/enrich")).json()
        assert body["code"] == 0
        assert [r["guid"] for r in body["data"]] == ["online:movie-a"]

    # ---------- 继续观看 / 播放列表 ----------

    async def test_play_list_and_continue_filtered_by_library(self):
        self._seed_history()
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        lst = (await self.client.get("/v/api/v1/play/list")).json()
        assert lst["code"] == 0
        assert [row["guid"] for row in lst["data"]] == ["online:movie-a"]
        cont = (await self.client.get("/v/api/ext/continue")).json()
        assert cont["code"] == 0
        assert [r["item_guid"] for r in cont["data"]] == ["online:movie-a"]
        # u1 全开权限时两条都可见；管理员看不到 u1 分区的记录（分区隔离）
        self._viewer(_user("u1"),
                     {"u1": {"catalog:movie", "catalog:tv"}})
        lst = (await self.client.get("/v/api/v1/play/list")).json()
        assert {row["guid"] for row in lst["data"]} == \
            {"online:movie-a", "online:tv-b"}
        cont = (await self.client.get("/v/api/ext/continue")).json()
        assert {r["item_guid"] for r in cont["data"]} == \
            {"online:movie-a", "online:tv-b"}
        self._viewer(ADMIN, {})
        lst = (await self.client.get("/v/api/v1/play/list")).json()
        assert lst["data"] == []
        cont = (await self.client.get("/v/api/ext/continue")).json()
        assert cont["data"] == []

    # ---------- 播放链：无权 -6 ----------

    async def test_play_info_denied_and_allowed(self):
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "online:tv-b"})
        assert r.json()["code"] == -6
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "online:movie-a"})
        assert r.json()["code"] == 0
        self._viewer(_user("u1"), {"u1": {"catalog:tv"}})
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "online:tv-b"})
        assert r.json()["code"] == 0

    async def test_play_chain_denied_all_routes(self):
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        mg = self.mg_tv
        r = await self.client.post("/v/api/v1/stream/list",
                                   json={"guid": "online:tv-b"})
        assert r.json()["code"] == -6
        r = await self.client.get("/v/api/v1/stream/list/online:tv-b")
        assert r.json()["code"] == -6
        r = await self.client.post("/v/api/v1/stream",
                                   json={"media_guid": mg})
        assert r.json()["code"] == -6
        r = await self.client.post("/v/api/v1/play/play",
                                   json={"media_guid": mg})
        assert r.json()["code"] == -6
        r = await self.client.get(f"/v/api/v1/media/range/{mg}")
        assert r.status_code == 403 and r.json()["code"] == -6
        r = await self.client.get(f"/v/media/online/{mg}/preset.m3u8")
        assert r.status_code == 403 and r.json()["code"] == -6
        r = await self.client.get(f"/v/media/online/{mg}/main.m3u8")
        assert r.status_code == 403 and r.json()["code"] == -6
        from app import b64e
        seg = f"/v/media/online/{mg}/seg?u={b64e('https://x/tv-b-1.ts')}"
        r = await self.client.get(seg)
        assert r.status_code == 403 and r.json()["code"] == -6
        r = await self.client.get(f"/v/media/online/{mg}/direct")
        assert r.status_code == 403 and r.json()["code"] == -6

    async def test_play_chain_allowed_paths(self):
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        mg = self.mg_movie
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "online:movie-a"})
        assert r.json()["code"] == 0
        r = await self.client.post("/v/api/v1/stream/list",
                                   json={"guid": "online:movie-a"})
        assert r.json()["code"] == 0
        r = await self.client.post("/v/api/v1/stream",
                                   json={"media_guid": mg})
        assert r.json()["code"] == 0
        r = await self.client.post("/v/api/v1/play/play",
                                   json={"media_guid": mg})
        assert r.json()["code"] == 0
        assert "preset.m3u8" in r.json()["data"]["play_link"]
        r = await self.client.get(f"/v/media/online/{mg}/preset.m3u8")
        assert r.status_code == 200
        assert "#EXTM3U" in r.text and "main.m3u8" in r.text
        with patch.object(appmod, "_get_playlist_cached",
                          new=AsyncMock(return_value=(
                              "#EXTM3U\n#EXT-X-VERSION:3\n"
                              "#EXTINF:6.0,\nhttps://x/seg0.ts\n"))):
            r = await self.client.get(f"/v/media/online/{mg}/main.m3u8")
        assert r.status_code == 200
        assert "#EXTM3U" in r.text
        from app import b64e
        from fastapi.responses import Response as StarletteResponse
        seg = f"/v/media/online/{mg}/seg?u={b64e('https://x/seg0.ts')}"
        fake_ts = StarletteResponse(content=b"fake-ts",
                                    media_type="video/mp2t")
        with patch.object(appmod, "_try_stream_segment",
                          new=AsyncMock(return_value=fake_ts)):
            r = await self.client.get(seg)
        assert r.status_code == 200 and r.content == b"fake-ts"
        r = await self.client.get(f"/v/media/online/{mg}/direct",
                                  follow_redirects=False)
        assert r.status_code == 302
        assert "preset.m3u8" in r.headers["location"]
        r = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                  follow_redirects=False)
        assert r.status_code == 302
        assert "preset.m3u8" in r.headers["location"]

    async def test_resume_episode_unaffected_for_allowed_viewer(self):
        # 季/集直达（断点续播）：有权限时 play/info 仍回到未看完的第 1 集 ts=60；
        # 同一记录对无权账号仍是 -6（续播不断点，但权限照常拦）。
        egs = self.store.tv_episode_guids("online:tv-b", regular_only=True)
        assert len(egs) == 2
        self.history.record({"item_guid": egs[0], "ts": 60, "duration": 600,
                             "media_guid": self.mg_tv,
                             "video_guid": video_guid(egs[0])}, user="u1")
        self._viewer(_user("u1"), {"u1": {"catalog:tv"}})
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "online:tv-b"})
        body = r.json()
        assert body["code"] == 0
        assert body["data"]["ts"] == 60
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "online:tv-b"})
        assert r.json()["code"] == -6

    # ---------- 守门 helper：边界 ----------

    def test_online_guid_viewable_none_when_evicted(self):
        req = SimpleNamespace(headers={})
        assert appmod._online_guid_viewable(req, "online:gone") is None
        assert appmod._online_guid_viewable(req, "") is None

    def test_online_guid_viewable_line_item_guid(self):
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        req = SimpleNamespace(headers={})
        line_guid = appmod._line_item_guid("online:tv-b", "src2::线路1", 0)
        assert appmod._online_guid_viewable(req, line_guid) is False
        line_guid_a = appmod._line_item_guid("online:movie-a",
                                             "src1::线路1", 0)
        assert appmod._online_guid_viewable(req, line_guid_a) is True

    def test_online_target_viewable_t4_key(self):
        self.store.remember_merged([copy.deepcopy(BRIEF_T4)])
        req = SimpleNamespace(headers={})
        target = {"online_guid": "online:t4-c", "t4": True}
        self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        assert appmod._online_target_viewable(req, target, "") is False
        self._viewer(_user("u1"), {"u1": {"t4"}})
        assert appmod._online_target_viewable(req, target, "") is True
        assert appmod._online_target_viewable(req, None, "") is None


if __name__ == "__main__":
    unittest.main()
