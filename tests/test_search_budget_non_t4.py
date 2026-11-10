"""非 T4 聚合搜索总预算 / 早返回（p0-rest-search）。

覆盖缺口：移植批的 early-return（25 结果 / 0.8s）只活在
``providers.search_all`` 的 budget 分支里，而
``app.__aggregate_briefs_inner`` 无 T4 源时传 ``budget=None``，
于是非 T4 聚合搜索等全部源完成，最慢源拖死全局搜索。
修复后：无显式 timeout 时一律给 5.0s 兜底（与 T4 路径同值），
显式 timeout（如媒体库搜索的 6.0s）仍优先。

只测进程内逻辑与假源，不碰真实网络与飞牛。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import sourcehealth
import store as storemod
import t4_media
import t4_service

FAST = [{"id": f"fast{i}", "name": f"Fast{i}",
         "api": f"https://fast{i}.test/api.php/provide/vod/at/json",
         "enabled": True} for i in range(3)]
SLOW = {"id": "slow-src", "name": "Slow",
        "api": "https://slow.test/api.php/provide/vod/at/json",
        "enabled": True}
T4SRC = {"id": "t4-1", "name": "T4",
         "api": "https://t4.test/api", "kind": "t4", "enabled": True}
BUDGET = 5.0  # 与 app._AGGREGATE_SEARCH_BUDGET / T4 路径同值


def _title(vod_id, name, src, play_url, year="2024"):
    return providers._map_vod({
        "vod_id": vod_id, "vod_name": name, "type_name": "电影",
        "vod_year": year,
        "vod_play_from": "A", "vod_play_url": play_url}, src)


class NonT4SearchBudgetTests(unittest.IsolatedAsyncioTestCase):
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

    def _set_sources(self, sources):
        self.config.save({"sources": sources,
                          "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})

    # ---------- 接线：预算真的传到了 search_all ----------

    async def test_non_t4_aggregate_passes_default_budget(self):
        with patch.object(providers, "search_all",
                          return_value=[]) as search:
            await appmod._aggregate_briefs("预算片A", refresh=True)
        search.assert_awaited_once()
        self.assertIsNone(search.call_args.kwargs.get("timeout"))
        self.assertEqual(search.call_args.kwargs.get("budget"), BUDGET)

    async def test_fast_only_non_t4_also_gets_budget(self):
        with patch.object(providers, "search_all",
                          return_value=[]) as search:
            await appmod._aggregate_briefs("预算片B", fast_only=True,
                                           refresh=True)
        self.assertEqual(search.call_args.kwargs.get("budget"), BUDGET)

    async def test_t4_aggregate_budget_unchanged(self):
        self._set_sources(FAST + [T4SRC])
        with patch.object(providers, "search_all",
                          return_value=[]) as search:
            await appmod._aggregate_briefs("预算片C", refresh=True)
        # T4 路径此前已是 5.0s：改动前后行为一致
        self.assertEqual(search.call_args.kwargs.get("budget"), BUDGET)

    async def test_explicit_timeout_still_drives_budget(self):
        # 媒体库搜索传 timeout=6.0：不被默认预算覆盖，search_all 内仍由
        # timeout 派生预算（6.0s）。
        with patch.object(providers, "search_all",
                          return_value=[]) as search:
            await appmod._aggregate_briefs("预算片D", timeout=6.0,
                                           refresh=True)
        self.assertEqual(search.call_args.kwargs.get("timeout"), 6.0)
        self.assertIsNone(search.call_args.kwargs.get("budget"))

    # ---------- 端到端：假源 + 慢源 ----------

    def _dispatch(self, n_fast_titles, slow_sleep, cancelled):
        async def fake_dispatch(client, source, keyword):
            sid = source["id"]
            if sid == "slow-src":
                try:
                    await asyncio.sleep(slow_sleep)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return []
            idx = int(sid[-1])
            return [_title(f"{idx}-{i}", f"快片{idx}-{i}", source,
                           f"正片$https://fast{idx}.test/{i}.m3u8")
                    for i in range(n_fast_titles)]
        return fake_dispatch

    async def test_slow_source_early_return_end_to_end(self):
        # 3 快源各 10 条 = 30 ≥ 25，0.8s 后即早返回，不等 5s 慢源
        cancelled = asyncio.Event()
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(10, 30, cancelled))):
            briefs = await appmod._aggregate_briefs("早返回片", refresh=True)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(briefs), 12)  # max_per_keyword 默认 12
        self.assertLess(elapsed, 4.0)
        self.assertTrue(cancelled.is_set())

    async def test_budget_exhaustion_returns_partial_results(self):
        # 只有 5 条（<25，不触发早返回）：等满 5.0s 预算即返回，不空等慢源
        self._set_sources([FAST[0], SLOW])
        cancelled = asyncio.Event()
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(
                              side_effect=self._dispatch(5, 30, cancelled))):
            briefs = await appmod._aggregate_briefs("冷门片", refresh=True)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(briefs), 5)
        self.assertGreaterEqual(elapsed, 4.5)
        self.assertLess(elapsed, 9.0)
        self.assertTrue(cancelled.is_set())

    # ---------- 退化路径：search_all(budget=None) 仍等全部完成 ----------

    async def test_no_budget_path_still_waits_for_all(self):
        def titles(n):
            return [providers.OnlineTitle(
                provider="t", vod_id=f"v{i}", title=f"片{i}",
                type_name="", year="", area="", updated="", remarks="",
                score="", poster="", overview="", actor="", director="",
                sources={}) for i in range(n)]

        async def fake_dispatch(client, source, keyword):
            if source["id"] == "slow":
                await asyncio.sleep(0.5)
                return titles(5)
            await asyncio.sleep(0.1)
            return titles(5)

        sources = [{"id": "fast", "enabled": True},
                   {"id": "slow", "enabled": True}]
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(side_effect=fake_dispatch)):
            out = await providers.search_all("退化片", sources)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(out), 10)
        self.assertGreaterEqual(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
