"""B18-prewarm：启动预热（startup_prewarm_hot）与按需空操作
（schedule_prewarm_title）。懒索引部分 fork 已用增量索引等价解决，不做。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


class PrewarmTests(unittest.IsolatedAsyncioTestCase):
    def test_schedule_prewarm_title_is_noop(self):
        # 上游当前版本同样是空操作：按需模式，不为单个标题建后台预热任务
        self.assertIsNone(appmod.schedule_prewarm_title("og-x", 0))
        self.assertIsNone(appmod.schedule_prewarm_title("og-x"))

    async def test_startup_prewarm_hot_warms_routes_and_home_windows(self):
        warmed = []

        async def fake_window(cat, offset, limit):
            warmed.append((cat, offset, limit))

        with patch("asyncio.sleep", new=AsyncMock()), \
                patch.object(appmod.config, "get",
                             return_value={"linepredict_enabled": True}), \
                patch.object(appmod.catalog, "routes_stale",
                             return_value=True), \
                patch.object(appmod.catalog, "refresh_routes",
                             new=AsyncMock(return_value={})), \
                patch.object(appmod.catalog, "available",
                             return_value=True), \
                patch.object(appmod.catalog, "get_window",
                             side_effect=fake_window), \
                patch.object(appmod, "standalone_sources_active",
                             return_value=[]):
            await appmod.startup_prewarm_hot()
        cats = [c for c, _t, _ in appmod.catalogmod.CATALOG_CATEGORIES]
        self.assertEqual(sorted(c for c, _o, _l in warmed), sorted(cats))
        self.assertTrue(all(o == 0 and n == 80 for _c, o, n in warmed))

    async def test_startup_prewarm_hot_skipped_when_linepredict_off(self):
        with patch("asyncio.sleep", new=AsyncMock()), \
                patch.object(appmod.config, "get",
                             return_value={"linepredict_enabled": False}), \
                patch.object(appmod.catalog, "routes_stale",
                             side_effect=AssertionError("must not be called")):
            await appmod.startup_prewarm_hot()


if __name__ == "__main__":
    unittest.main()
