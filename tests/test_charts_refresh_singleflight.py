"""榜单强制刷新防抖：并发强制刷新合并为一次（single-flight）。

ChartCache.refresh(force=True) 本应只在“已有在途刷新”时复用其
future；旧实现里 force=True 会绕过合并判断，两次并发强制刷新各拉
一次豆瓣。测试先行：test_forced_refresh_singleflight 在修实现前失败。
只测 charts 模块，不碰真实网络与仓库 state/。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import charts as charts_mod


def _fake_payload():
    return {"charts": {"movie": [{"guid": "g1", "cat": "movie"}],
                       "tv": [], "anime": [], "variety": []},
            "provider_ok": 1, "provider_total": 2, "updated_at": 123}


class ChartsForcedRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_state = charts_mod.STATE
        self._orig_collect = charts_mod.collect_charts
        charts_mod.STATE = Path(self._tmp.name) / "charts.json"
        self.cache = charts_mod.ChartCache()
        self.addCleanup(self._restore)

    def _restore(self):
        charts_mod.STATE = self._orig_state
        charts_mod.collect_charts = self._orig_collect

    def _counting_collect(self, calls, delay=0.05):
        async def fake():
            calls.append(1)
            await asyncio.sleep(delay)
            return _fake_payload()
        charts_mod.collect_charts = fake

    async def test_forced_refresh_singleflight(self):
        # 两次并发强制刷新只拉取一次，且两次拿到同一份结果。
        calls: list[int] = []
        self._counting_collect(calls)
        first, second = await asyncio.gather(
            self.cache.refresh(force=True),
            self.cache.refresh(force=True))
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(self.cache.items("movie")[0]["guid"], "g1")

    async def test_non_forced_refresh_singleflight(self):
        # 回归：普通刷新本就合并在途请求，语义不变。
        calls: list[int] = []
        self._counting_collect(calls)
        first, second = await asyncio.gather(
            self.cache.refresh(force=False),
            self.cache.refresh(force=False))
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)

    async def test_sequential_forced_refresh_fetches_twice(self):
        # force 语义不变：没有在途刷新时，每次强制刷新都重新拉取。
        calls: list[int] = []
        self._counting_collect(calls, delay=0)
        await self.cache.refresh(force=True)
        await self.cache.refresh(force=True)
        self.assertEqual(len(calls), 2)
