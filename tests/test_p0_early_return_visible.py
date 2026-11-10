"""P0-1：搜索早返回按未过滤计数（受限账号搜索系统性变空）。

覆盖：
- providers.search_all：visible 回调传入时，早返回阈值按可见标题数计
  （快源 30 条全属隐藏库 → 可见 0 → 不触发早返回，慢源可见标题不被取消）；
  不传 visible 时沿用原始总数（管理员行为不变：30 条即触发早返回）。
- app.online_search：受限账号下慢源可见标题仍能拿到；管理员早返回不变；
  搜索/聚合缓存按查看者分键（受限账号先搜不污染管理员的聚合结果，反之亦然）。

只测进程内逻辑与假源，不碰真实网络与飞牛。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import store as storemod
import t4_media
import t4_service

FAST = [{"id": f"fast{i}", "name": f"Fast{i}",
         "api": f"https://fast{i}.test/api.php/provide/vod/at/json",
         "enabled": True} for i in range(3)]
SLOW = {"id": "slow-src", "name": "Slow",
        "api": "https://slow.test/api.php/provide/vod/at/json",
        "enabled": True}
BUDGET = 5.0

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


def _title(vod_id, name, src, play_url, type_name="电视剧", year="2024"):
    return providers._map_vod({
        "vod_id": vod_id, "vod_name": name, "type_name": type_name,
        "vod_year": year,
        "vod_play_from": "A", "vod_play_url": play_url}, src)


class EarlyReturnVisibleTests(unittest.IsolatedAsyncioTestCase):
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
        self.config.save({"sources": FAST + [SLOW],
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
        self.addCleanup(appmod._search_cache.clear)
        self.addCleanup(appmod._search_inflight.clear)
        self.addCleanup(appmod._AGG_CACHE.clear)
        self.addCleanup(appmod._AGG_INFLIGHT.clear)

    def _patch_obj(self, target, attr, *args, **kwargs):
        item = patch.object(target, attr, *args, **kwargs)
        item.start()
        self.addCleanup(item.stop)
        return item

    def _viewer(self, user, allow_map):
        self._patch_obj(appmod.fnusersmod, "resolve", return_value=user)
        self._patch_obj(appmod.vlibsmod, "account_can_see",
                        _can_see(allow_map))
        return SimpleNamespace(headers={})

    def _dispatch(self, slow_sleep, cancelled, slow_titles=2):
        """3 快源各回 10 条同名“快片”（原始 30 条、归并后 10 组）；
        慢源回 slow_titles 条“慢片”。快片属隐藏库（电视剧→catalog:tv），
        慢片属可见库（电影→catalog:movie）。"""
        async def fake_dispatch(client, source, keyword):
            sid = source["id"]
            if sid == "slow-src":
                try:
                    await asyncio.sleep(slow_sleep)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return [_title(f"s{i}", f"慢片{i}", source,
                               f"正片$https://slow.test/{i}.m3u8",
                               type_name="电影")
                        for i in range(slow_titles)]
            idx = int(sid[-1])
            return [_title(f"f{j}", f"快片{j}", source,
                           f"正片$https://fast{idx}.test/{j}.m3u8",
                           type_name="电视剧")
                    for j in range(10)]
        return fake_dispatch

    @staticmethod
    def _titles(cards):
        return [c["title"].replace("【在线】", "").split(" ·")[0]
                for c in cards]

    # ---------- providers.search_all：可见计数 ----------

    async def test_search_all_visible_predicate_keeps_slow_source(self):
        # 30 条快片全不可见 → 可见数 0 < 25，不触发早返回；慢源 1.5s 后
        # 回 2 条可见慢片，全部拿到，慢源不被取消。
        cancelled = asyncio.Event()
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(1.5, cancelled))):
            out = await providers.search_all(
                "可见片", FAST + [SLOW], budget=BUDGET,
                visible=lambda t: t.type_name == "电影")
        elapsed = time.monotonic() - t0
        self.assertFalse(cancelled.is_set())
        self.assertEqual(
            sorted(t.title for t in out if t.type_name == "电影"),
            ["慢片0", "慢片1"])
        self.assertEqual(len(out), 32)
        self.assertGreaterEqual(elapsed, 1.4)
        self.assertLess(elapsed, 4.5)

    async def test_search_all_without_visible_early_returns(self):
        # 不传 visible：原始总数 30 ≥ 25，0.8s 后即早返回，慢源被取消。
        cancelled = asyncio.Event()
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(30, cancelled))):
            out = await providers.search_all("原始片", FAST + [SLOW],
                                             budget=BUDGET)
        elapsed = time.monotonic() - t0
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(out), 30)
        self.assertFalse(any(t.title.startswith("慢片") for t in out))
        self.assertLess(elapsed, 4.0)

    async def test_search_all_visible_all_behaves_like_admin(self):
        # 可见回调全 True（管理员）：与不传 visible 一致，早返回照常触发。
        cancelled = asyncio.Event()
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(30, cancelled))):
            out = await providers.search_all(
                "全可见片", FAST + [SLOW], budget=BUDGET,
                visible=lambda t: True)
        elapsed = time.monotonic() - t0
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(out), 30)
        self.assertLess(elapsed, 4.0)

    async def test_search_all_visible_predicate_error_counts_invisible(self):
        # 可见回调抛异常的标题按不可见算（fail-closed），不炸掉早返回循环。
        cancelled = asyncio.Event()

        def flaky(title):
            if title.type_name == "电影":
                raise RuntimeError("boom")
            return False

        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(1.5, cancelled))):
            out = await providers.search_all(
                "异常片", FAST + [SLOW], budget=BUDGET, visible=flaky)
        elapsed = time.monotonic() - t0
        # 可见数恒为 0 → 不早返回，等到慢源完成（1.5s），结果仍全量返回。
        self.assertFalse(cancelled.is_set())
        self.assertEqual(len(out), 32)
        self.assertGreaterEqual(elapsed, 1.4)

    # ---------- app.online_search：端到端 ----------

    async def test_online_search_restricted_gets_slow_visible_title(self):
        # 受限账号只开 catalog:movie：快片（catalog:tv）全隐藏，慢片可见。
        # 早返回按可见数计 → 慢源不被取消，慢片进结果；快片被过滤掉。
        cancelled = asyncio.Event()
        req = self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(1.5, cancelled))):
            cards = await appmod.online_search("受限搜片", None, request=req)
        elapsed = time.monotonic() - t0
        self.assertFalse(cancelled.is_set())
        self.assertEqual(self._titles(cards), ["慢片0", "慢片1"])
        self.assertGreaterEqual(elapsed, 1.4)
        self.assertLess(elapsed, 4.5)

    async def test_online_search_admin_early_return_unchanged(self):
        # 管理员：快片 30 条全可见，0.8s 即早返回，慢源被取消、慢片不出。
        cancelled = asyncio.Event()
        req = self._viewer(ADMIN, {})
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(30, cancelled))):
            cards = await appmod.online_search("管理搜片", None, request=req)
        elapsed = time.monotonic() - t0
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(t.startswith("慢片") for t in self._titles(cards)))
        self.assertEqual(len(cards), 10)  # 10 组快片归并结果
        self.assertLess(elapsed, 4.0)

    async def test_online_search_restricted_visible_titles_still_early_return(self):
        # P1-23(5)：此前只测了受限账号“可见数不足→不截断”的方向。
        # 反方向：受限账号但快片全部可见（catalog:tv 也在白名单内），
        # 30 条可见即触发早返回，慢源被取消、慢片不出——早返回对受限账号
        # 不是失效，只是按可见数计；与管理员的截断行为一致。
        cancelled = asyncio.Event()
        req = self._viewer(_user("u1"),
                           {"u1": {"catalog:tv", "catalog:movie"}})
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(1.5, cancelled))):
            cards = await appmod.online_search("受限截断", None, request=req)
        elapsed = time.monotonic() - t0
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(t.startswith("慢片") for t in self._titles(cards)))
        self.assertEqual(len(cards), 10)  # 10 组快片归并结果
        self.assertLess(elapsed, 4.0)

    async def test_online_search_cache_isolated_per_viewer(self):
        # 同一关键词先后被受限账号与管理员搜：缓存按查看者分键，
        # 谁先搜都不污染对方的聚合结果（受限的截断/完整结果不漏给管理员，
        # 管理员的早返回截断结果也不漏给受限账号）。
        cancelled_a = asyncio.Event()
        cancelled_b = asyncio.Event()
        # 顺序一：受限先，管理员后
        req_u1 = self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(1.5, cancelled_a))):
            cards_u1 = await appmod.online_search("隔离片A", None, request=req_u1)
        self.assertEqual(self._titles(cards_u1), ["慢片0", "慢片1"])
        req_admin = self._viewer(ADMIN, {})
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(30, cancelled_b))):
            cards_admin = await appmod.online_search("隔离片A", None,
                                                     request=req_admin)
        self.assertTrue(cancelled_b.is_set())
        self.assertFalse(any(t.startswith("慢片")
                             for t in self._titles(cards_admin)))
        self.assertEqual(len(cards_admin), 10)
        # 顺序二：管理员先，受限后（同一关键词换新词，避免跨顺序干扰）
        cancelled_c = asyncio.Event()
        cancelled_d = asyncio.Event()
        req_admin2 = self._viewer(ADMIN, {})
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(30, cancelled_c))):
            cards_admin2 = await appmod.online_search("隔离片B", None,
                                                      request=req_admin2)
        self.assertTrue(cancelled_c.is_set())
        self.assertEqual(len(cards_admin2), 10)
        req_u1b = self._viewer(_user("u1"), {"u1": {"catalog:movie"}})
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(1.5, cancelled_d))):
            cards_u1b = await appmod.online_search("隔离片B", None,
                                                   request=req_u1b)
        self.assertFalse(cancelled_d.is_set())
        self.assertEqual(self._titles(cards_u1b), ["慢片0", "慢片1"])


if __name__ == "__main__":
    unittest.main()
