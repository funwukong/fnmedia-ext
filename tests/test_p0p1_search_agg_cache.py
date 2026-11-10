"""P1 第 19 项：搜索聚合层重复计算 → 单层缓存 + 单次遍历 + 集合去重。

覆盖：
- 缓存层数减半：_search_cache/_search_inflight 只是 _AGG_CACHE/_AGG_INFLIGHT
  的别名（同一对象）；TTL 统一为 AGG_CACHE_TTL；online_search 复用聚合层缓存，
  不同账号仍按人重滤（不漏可见范围）；
- 缓存键仍区分请求参数（query/fast_only/include_t4）与 TTL 失效；
- 预算只走 budget 一个参数（死三元已拆）；
- T4 交错调度与 fast_only 源过滤不变；
- 早返回后回收去重按任务身份：字段完全相等的两份结果不再被误删。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import sourcehealth
import store as storemod
import t4_media
import t4_service

GOOD = {"id": "good-src", "name": "Good",
        "api": "https://good.test/api.php/provide/vod/at/json", "enabled": True}
DEAD = {"id": "dead-src", "name": "Dead",
        "api": "https://dead.test/api.php/provide/vod/at/json", "enabled": True}
T4SRC = {"id": "t4-src", "name": "T4", "api": "t4://test",
         "enabled": True, "kind": "t4"}
GUARD = {"id": "guard-src", "name": "Guard",
         "api": "https://guard.test/api.php/provide/vod/at/json",
         "enabled": True, "guard": True}
SPIDER = {"id": "spider-x", "name": "Spider",
          "api": "https://spider.test/api.php/provide/vod/at/json",
          "enabled": True, "spider_src": True}
BUDGET = 5.0

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


def _title(vod_id, name, src, play_url, type_name="电影", year="2024"):
    return providers._map_vod({
        "vod_id": vod_id, "vod_name": name, "type_name": type_name,
        "vod_year": year,
        "vod_play_from": "A", "vod_play_url": play_url}, src)


class AggCacheTests(unittest.IsolatedAsyncioTestCase):
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
        self.config.save({"sources": [GOOD, DEAD],
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
        sourcehealth.reset("dead-src")
        self.addCleanup(lambda: sourcehealth.reset("good-src"))
        self.addCleanup(lambda: sourcehealth.reset("dead-src"))

    def _two_titles(self):
        good = _title("1", "好片", GOOD, "正片$https://good.test/m.m3u8")
        dead = _title("2", "差片", DEAD,
                      "第1集$https://dead.test/1.m3u8#第2集$https://dead.test/2.m3u8",
                      type_name="连续剧")
        return [good, dead]

    def test_cache_is_single_layer(self):
        self.assertIs(appmod._search_cache, appmod._AGG_CACHE)
        self.assertIs(appmod._search_inflight, appmod._AGG_INFLIGHT)
        self.assertEqual(appmod.AGG_CACHE_TTL, 180)
        self.assertFalse(hasattr(appmod, "CACHE_TTL"))

    async def test_online_search_shares_agg_cache_across_viewers(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search, \
                patch.object(appmod.fnusersmod, "resolve",
                             lambda r: ADMIN), \
                patch.object(appmod.vlibsmod, "account_can_see",
                             _can_see({})):
            admin_req = SimpleNamespace(headers={}, state=SimpleNamespace())
            admin = await appmod.online_search("共享片", None, request=admin_req)
            self.assertEqual(search.call_count, 1)
            titles = [c["title"].replace("【在线】", "").split(" ·")[0]
                      for c in admin]
            self.assertEqual(sorted(titles), ["好片", "差片"])
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search, \
                patch.object(appmod.fnusersmod, "resolve",
                             lambda r: _user("u1")), \
                patch.object(appmod.vlibsmod, "account_can_see",
                             _can_see({"u1": {"catalog:movie"}})):
            u1_req = SimpleNamespace(headers={}, state=SimpleNamespace())
            u1 = await appmod.online_search("共享片", None, request=u1_req)
            # P0-1 合入后：聚合缓存按查看者分键（带 visible 回调的聚合可能
            # 被该查看者的早返回截断），u1 不复用管理员的缓存条目，需重新
            # 拉取一次；过滤仍在每请求做，u1 只看到自己可见的「好片」。
            self.assertEqual(search.call_count, 1)
            titles = [c["title"].replace("【在线】", "").split(" ·")[0]
                      for c in u1]
            self.assertEqual(titles, ["好片"])

    async def test_cache_key_splits_query_fast_and_t4(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search:
            await appmod._aggregate_briefs("键片", fast_only=True)
            await appmod._aggregate_briefs("键片", fast_only=False)
            await appmod._aggregate_briefs("键片", fast_only=True,
                                           include_t4=False)
            await appmod._aggregate_briefs("键片", fast_only=True)
            await appmod._aggregate_briefs("另一片", fast_only=True)
        self.assertEqual(search.call_count, 4)

    async def test_cache_expires_after_ttl(self):
        with patch.object(providers, "search_all",
                          return_value=self._two_titles()) as search:
            await appmod._aggregate_briefs("过期片")
            key = "过期片\x000\x001"
            ts, val = appmod._AGG_CACHE[key]
            appmod._AGG_CACHE[key] = (ts - appmod.AGG_CACHE_TTL - 1, val)
            await appmod._aggregate_briefs("过期片")
        self.assertEqual(search.call_count, 2)

    async def test_budget_is_single_param(self):
        with patch.object(providers, "search_all",
                          return_value=[]) as search:
            await appmod._aggregate_briefs("预算片", refresh=True)
        self.assertEqual(search.call_args.kwargs.get("budget"), BUDGET)
        self.assertIsNone(search.call_args.kwargs.get("timeout"))
        with patch.object(providers, "search_all",
                          return_value=[]) as search:
            await appmod._aggregate_briefs("预算片2", timeout=6.0,
                                           refresh=True)
        self.assertEqual(search.call_args.kwargs.get("budget"), 6.0)
        self.assertIsNone(search.call_args.kwargs.get("timeout"))

    async def test_t4_interleave_unchanged(self):
        t4a = dict(T4SRC, id="t4-a")
        t4b = dict(T4SRC, id="t4-b")
        cms1 = {"id": "cms-1", "api": "https://a.test/x", "enabled": True}
        cms2 = {"id": "cms-2", "api": "https://b.test/x", "enabled": True}
        seen = []

        async def fake_dispatch(client, source, keyword):
            seen.append(source["id"])
            await asyncio.sleep(0.01)
            return []

        with patch.object(providers, "_dispatch_search",
                          side_effect=fake_dispatch):
            await providers.search_all("交错片", [t4a, cms1, t4b, cms2])
        self.assertEqual(seen, ["t4-a", "cms-1", "t4-b", "cms-2"])

    async def test_fast_only_source_filter_unchanged(self):
        self.config.save({
            "sources": [GOOD, DEAD, GUARD, SPIDER,
                        {"id": "drpy-x", "name": "D",
                         "api": "drpy://x.js", "enabled": True},
                        {"id": "dduo", "name": "DD",
                         "api": "https://d.test", "enabled": True},
                        {"id": "xiaoya", "name": "XY",
                         "api": "catvod://xiaoya", "enabled": True}],
            "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
        seen_calls = []

        async def fake_search(keyword, sources, **kw):
            seen_calls.append([s["id"] for s in sources])
            return []

        with patch.object(providers, "search_all",
                          side_effect=fake_search):
            await appmod._aggregate_briefs("快源片", fast_only=True,
                                           refresh=True)
            await appmod._aggregate_briefs("快源片", fast_only=False,
                                           refresh=True)
        self.assertEqual(sorted(seen_calls[0]), ["dead-src", "good-src"])
        self.assertEqual(sorted(seen_calls[1]),
                         ["dduo", "dead-src", "drpy-x", "good-src",
                          "guard-src", "spider-x", "xiaoya"])

    async def test_search_all_recovery_keeps_equal_lists(self):
        fast_src = {"id": "fast-src", "api": "https://f.test/x",
                    "enabled": True}
        slow_src = {"id": "slow-src", "api": "https://s.test/x",
                    "enabled": True}
        titles = [_title(str(i), "同名片%d" % i, fast_src,
                         "正片$https://f.test/%d.m3u8" % i)
                  for i in range(30)]

        async def fake_dispatch(client, source, keyword):
            if source["id"] == "slow-src":
                await asyncio.sleep(0.05)
            return list(titles)

        real_wait = asyncio.wait
        reported = set()
        first = {"done": False}

        async def hiding_wait(fs, *, timeout=None, return_when=None):
            done, pending = await real_wait(
                fs, timeout=timeout, return_when=return_when)
            fresh = [t for t in done if id(t) not in reported]
            if not first["done"] and fresh:
                keep = fresh[0]
                first["done"] = True
                reported.add(id(keep))
                return {keep}, set(pending) | set(fresh[1:])
            return set(), set(pending) | set(fresh)

        with patch.object(providers, "_dispatch_search",
                          side_effect=fake_dispatch), \
                patch.object(asyncio, "wait", hiding_wait):
            got = await providers.search_all(
                "去重片", [fast_src, slow_src], budget=2.0)
        self.assertEqual(len(got), 60)

    async def test_search_all_keeps_distinct_lists_once(self):
        fast_src = {"id": "fast-src", "api": "https://f.test/x",
                    "enabled": True}
        slow_src = {"id": "slow-src", "api": "https://s.test/x",
                    "enabled": True}

        async def fake_dispatch(client, source, keyword):
            if source["id"] == "slow-src":
                await asyncio.sleep(0.05)
                return [_title("s1", "慢片", slow_src,
                               "正片$https://s.test/1.m3u8")]
            return [_title("f1", "快片", fast_src,
                           "正片$https://f.test/1.m3u8")]

        with patch.object(providers, "_dispatch_search",
                          side_effect=fake_dispatch):
            got = await providers.search_all(
                "各一片", [fast_src, slow_src], budget=2.0)
        self.assertEqual(sorted(t.title for t in got), ["快片", "慢片"])


if __name__ == "__main__":
    unittest.main()
