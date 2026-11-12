"""二级目录冷片按需绑定（上游 bf16a3b + 447b452 的 folder 部分，fork 适配）。

覆盖点（任务要求）：
- 快源软超时内返回、总预算内补搜命中、超时后后台兜底仍能绑定
- prefetch 非阻塞（详情请求不被拖慢）、配置预算生效（调小/关掉）
- 冷片二级目录不再空、片名匹配规则（年份/语言后缀接受、包含误绑拒绝）
- catalog.find_brief_by_guid、early-return 参数透传
- beta.2 对齐：_vod_line_folder 强制刷新（refresh=1）

fork 适配说明（与上游的差异，见实现注释）：
- 上游 _start_folder_bind 命中后调 schedule_prewarm_title：fork 无此函数，跳过
  （预热由解冻第 4 项 browse_probe 负责，二者互补不重复）。
- 上游 _aggregate_briefs 用 soft_timeout 参数名：fork 用 timeout，调用时按
  fork 签名传 timeout。
- 上游 _FOLDER_BIND_WAIT 是模块常量：fork 改为配置项 folder_bind_wait
  （默认 8.0，夹取 [0, 25]，0 = 不等待、只后台绑定），_FOLDER_BIND_WAIT
  保留为默认值常量。
- 上游 beta.2 把等待放宽到 25s：fork 不采纳，默认仍为 8.0（用户定）。
- fork 无 _CHART_FAIL_LEVEL：未命中只清 _CHART_FAILED_TS。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod

_ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
          "is_admin": True, "token": "tok-admin"}


def _brief(title="冷片", year="2026", guid="other"):
    return {"guid": guid, "title": title, "year": year,
            "lines": {"lzi::lzm3u8": [{"title": "正片",
                                       "url": "https://lzi.test/v.m3u8",
                                       "direct": True}]}}


def _target(line="lzi::lzm3u8", index=0):
    return {"url": "https://lzi.test/v.m3u8", "headers": {},
            "line": line, "index": index, "total": 1,
            "ep_title": "正片", "m3u8": True}


def _client():
    return clientmod.Client(kind="web")


class FolderBindTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        futs = getattr(appmod, "_FOLDER_BIND_FUT", None)
        if isinstance(futs, dict):
            futs.clear()
        fb = getattr(appmod, "_FOLDER_FULL_FB", None)
        if isinstance(fb, set):
            fb.clear()

    # ---------- 片名匹配规则 ----------

    def test_folder_picker_accepts_year_language_suffix_rejects_contains(self):
        year_brief = _brief(title="美人余2026", year="2026")
        lang_brief = _brief(title="璀璨之城粤语", year="2026")
        wrong_brief = _brief(title="突如其来的假期", year="")
        self.assertIsNotNone(
            appmod._pick_folder_brief("美人余", "", [year_brief]))
        self.assertIsNotNone(
            appmod._pick_folder_brief("璀璨之城", "", [lang_brief]))
        self.assertIsNone(
            appmod._pick_folder_brief("突如其来", "", [wrong_brief]))

    def test_folder_picker_rejects_year_mismatch(self):
        brief = _brief(title="冷片", year="2025")
        self.assertIsNone(appmod._pick_folder_brief("冷片", "2026", [brief]))
        self.assertIsNotNone(appmod._pick_folder_brief("冷片", "2026",
                                                        [_brief(year="2026")]))

    # ---------- catalog.find_brief_by_guid ----------

    def test_catalog_find_brief_by_guid_dense_and_page_cache(self):
        cat = appmod.catalog
        cat._dense["agg:movie"] = [{"guid": "dense-og", "title": "片A"}]
        cat._page_cache[("agg", "movie", 1)] = (
            time.time(), [{"guid": "page-og", "title": "片B"}])
        self.addCleanup(lambda: cat._dense.pop("agg:movie", None))
        self.addCleanup(lambda: cat._page_cache.pop(("agg", "movie", 1), None))
        self.assertEqual(cat.find_brief_by_guid("dense-og")["title"], "片A")
        self.assertEqual(cat.find_brief_by_guid("page-og")["title"], "片B")
        self.assertIsNone(cat.find_brief_by_guid("no-such-og"))
        self.assertIsNone(cat.find_brief_by_guid(""))

    # ---------- 等待预算配置项 ----------

    def test_folder_bind_wait_default_and_clamp(self):
        self.assertEqual(appmod._FOLDER_BIND_WAIT, 8.0)
        cases = ({}, 8.0), ({"folder_bind_wait": 99}, 25.0), \
            ({"folder_bind_wait": -5}, 0.0), ({"folder_bind_wait": 2.5}, 2.5), \
            ({"folder_bind_wait": "xx"}, 8.0), ({"folder_bind_wait": 0}, 0.0)
        for cfg, want in cases:
            with patch.object(appmod.config, "get", return_value=dict(cfg)):
                self.assertEqual(appmod._folder_bind_wait(), want, cfg)

    def test_folder_fast_constants(self):
        self.assertEqual(appmod._FOLDER_FAST_SOFT, 2.0)
        self.assertEqual(appmod._FOLDER_FAST_HARD, 4.0)
        self.assertEqual(appmod._FOLDER_EARLY_WAIT, 0.4)
        self.assertEqual(appmod._FOLDER_EARLY_RESULTS, 15)
        self.assertLess(appmod._FOLDER_FAST_SOFT, appmod._FOLDER_BIND_WAIT)
        self.assertLess(appmod._FOLDER_FAST_HARD, 25.0)

    # ---------- ensure_folder_bound ----------

    async def test_ensure_folder_bound_fast_hit_binds_guid(self):
        g = "cold-og"
        picked = _brief()
        calls = []

        async def fake_agg(title, maxn, **kw):
            calls.append(kw)
            return [picked]

        bound = {}

        def _remember(briefs):
            bound.update(briefs[0])
            return briefs

        def _get_title(guid):
            return dict(bound) if bound else None

        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": "2026"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"year": "2026"}), \
                patch.object(appmod.store, "get_title_by_guid",
                             side_effect=_get_title), \
                patch.object(appmod.store, "remember_merged",
                             side_effect=_remember):
            out = await appmod.ensure_folder_bound(g)
        self.assertIsNotNone(out)
        self.assertEqual(out["guid"], g)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("fast_only"))
        self.assertEqual(calls[0].get("early_min_results"), 15)
        self.assertEqual(calls[0].get("early_min_wait"), 0.4)

    async def test_ensure_folder_bound_returns_existing_without_search(self):
        existing = {"guid": "hot-og", "title": "热片", "lines": {}}
        with patch.object(appmod.store, "get_title_by_guid",
                          return_value=existing), \
                patch.object(appmod, "_aggregate_briefs") as agg:
            out = await appmod.ensure_folder_bound("hot-og")
        self.assertIs(out, existing)
        agg.assert_not_called()

    async def test_ensure_folder_bound_full_search_hit_within_budget(self):
        # 快源未命中 → 在总预算剩余窗口内补一次全源命中 → 本次请求即出线路。
        g = "cold-og"
        picked = _brief()
        calls = []

        async def fake_agg(title, maxn, **kw):
            calls.append(kw)
            return [] if kw.get("fast_only") else [picked]

        bound = {}

        def _remember(briefs):
            bound.update(briefs[0])
            return briefs

        def _get_title(guid):
            return dict(bound) if bound else None

        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": "2026"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"year": "2026"}), \
                patch.object(appmod.store, "get_title_by_guid",
                             side_effect=_get_title), \
                patch.object(appmod.store, "remember_merged",
                             side_effect=_remember):
            out = await appmod.ensure_folder_bound(g)
        self.assertIsNotNone(out)
        self.assertEqual(out["guid"], g)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].get("fast_only"))
        self.assertFalse(calls[1].get("fast_only"))

    async def test_ensure_folder_bound_timeout_then_background_fallback_binds(self):
        # 快源慢（2s）+ 预算 0.3s：本次请求不等（返回 None），后台快源跑完
        # 未命中 → 全源兜底命中 → 用户重进即有线路。
        g = "cold-og"
        picked = _brief()
        calls = {"n": 0}
        bound = {}

        async def fake_agg(title, maxn, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(2.0)
                return []
            return [picked]

        def _remember(briefs):
            bound.update(briefs[0])
            return briefs

        def _get_title(guid):
            return dict(bound) if bound else None

        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": "2026"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"year": "2026"}), \
                patch.object(appmod.store, "get_title_by_guid",
                             side_effect=_get_title), \
                patch.object(appmod.store, "remember_merged",
                             side_effect=_remember), \
                patch.object(appmod.config, "get",
                             return_value={"folder_bind_wait": 0.3}):
            started = time.monotonic()
            out = await appmod.ensure_folder_bound(g)
            waited = time.monotonic() - started
            self.assertIsNone(out)
            self.assertLess(waited, 1.0)
            # 后台：快源跑完（2s）→ 全源兜底命中落库（轮询必须在 with 内，
            # 否则 patch 撤销后后台任务会调到真实 _aggregate_briefs）。
            for _ in range(30):
                if bound:
                    break
                await asyncio.sleep(0.1)
        self.assertEqual(bound.get("guid"), g)
        self.assertEqual(bound.get("title"), "冷片")

    async def test_ensure_folder_bound_fast_miss_schedules_full_fallback(self):
        g = "fast-miss-og"
        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs",
                             new=AsyncMock(return_value=[])), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": ""}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=None), \
                patch.object(appmod.store, "get_title_by_guid",
                             return_value=None), \
                patch.object(appmod, "_schedule_folder_full_fallback") as fb:
            out = await appmod.ensure_folder_bound(g)
        self.assertIsNone(out)
        fb.assert_called_once()
        self.assertEqual(fb.call_args[0][0], g)

    async def test_schedule_folder_full_fallback_binds_in_background(self):
        g = "fb-og"
        picked = _brief()
        bound = {}

        async def fake_agg(title, maxn, **kw):
            return [picked]

        def _remember(briefs):
            bound.update(briefs[0])
            return briefs

        with patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod.store, "get_title_by_guid",
                             return_value=None), \
                patch.object(appmod.store, "remember_merged",
                             side_effect=_remember):
            appmod._schedule_folder_full_fallback(g, "冷片", "2026")
            # 去重：第二次调度不重复入队
            appmod._schedule_folder_full_fallback(g, "冷片", "2026")
            for _ in range(30):
                if bound:
                    break
                await asyncio.sleep(0.1)
        self.assertEqual(bound.get("guid"), g)

    # ---------- prefetch 非阻塞 ----------

    async def test_prefetch_folder_binding_is_nonblocking(self):
        g = "prefetch-og"

        async def slow_agg(title, maxn, **kw):
            await asyncio.sleep(0.6)
            return [_brief()]

        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=slow_agg), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": "2026"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"year": "2026"}), \
                patch.object(appmod.store, "get_title_by_guid",
                             return_value=None), \
                patch.object(appmod.store, "remember_merged",
                             return_value=[]):
            started = time.monotonic()
            appmod.prefetch_folder_binding(g)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.3)
            # 单飞：重复预取不重复扇出
            appmod.prefetch_folder_binding(g)
            self.assertEqual(len(appmod._FOLDER_BIND_FUT), 1)
            await asyncio.sleep(0.8)  # 放行后台任务，避免循环关闭时挂起
        self.assertNotIn(g, appmod._FOLDER_BIND_FUT)

    async def test_prefetch_folder_binding_skips_already_bound(self):
        with patch.object(appmod.store, "get_title_by_guid",
                          return_value={"guid": "hot-og", "title": "热片"}), \
                patch.object(appmod, "_start_folder_bind") as start:
            appmod.prefetch_folder_binding("hot-og")
        start.assert_not_called()

    # ---------- 配置预算生效：关掉等待 ----------

    async def test_folder_bind_wait_zero_returns_immediately_but_binds_later(self):
        # folder_bind_wait=0：本次请求不等，但后台绑定仍跑，重进即有。
        g = "cold-og"
        picked = _brief()
        bound = {}

        async def slow_agg(title, maxn, **kw):
            await asyncio.sleep(0.5)
            return [picked]

        def _remember(briefs):
            bound.update(briefs[0])
            return briefs

        def _get_title(guid):
            return dict(bound) if bound else None

        with patch.object(appmod, "_folder_title_hint", return_value="冷片"), \
                patch.object(appmod, "_aggregate_briefs", side_effect=slow_agg), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "冷片", "year": "2026"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"year": "2026"}), \
                patch.object(appmod.store, "get_title_by_guid",
                             side_effect=_get_title), \
                patch.object(appmod.store, "remember_merged",
                             side_effect=_remember), \
                patch.object(appmod.config, "get",
                             return_value={"folder_bind_wait": 0}):
            started = time.monotonic()
            out = await appmod.ensure_folder_bound(g)
            waited = time.monotonic() - started
            self.assertIsNone(out)
            self.assertLess(waited, 0.3)
            # 后台绑定仍跑（轮询必须在 with 内，否则 patch 撤销后后台任务
            # 会调到真实 _aggregate_briefs）。
            for _ in range(30):
                if bound:
                    break
                await asyncio.sleep(0.1)
        self.assertEqual(bound.get("guid"), g)

    # ---------- early-return 参数透传 ----------

    async def test_aggregate_briefs_passes_early_params_to_search_all(self):
        with patch.object(appmod.config, "enabled_sources", return_value=[]), \
                patch.object(appmod.providers, "search_all",
                             new=AsyncMock(return_value=[])) as search_all:
            out = await appmod._aggregate_briefs(
                "__folder_bind_probe_kw__", 12, fast_only=True, timeout=2.0,
                include_t4=False, refresh=True,
                early_min_wait=0.4, early_min_results=15)
        self.assertEqual(out, [])
        search_all.assert_awaited_once()
        _, kwargs = search_all.call_args
        self.assertEqual(kwargs.get("early_min_wait"), 0.4)
        self.assertEqual(kwargs.get("early_min_results"), 15)

    # ---------- 冷片二级目录不再空 ----------

    async def test_cold_catalog_folder_lists_lines_after_binding(self):
        b = _brief(guid="catalog-cold-og")
        picked = _brief(guid="other")
        calls = {"n": 0}
        bound = {}

        def _get_title(guid):
            calls["n"] += 1
            return dict(bound) if bound else None

        def _remember(briefs):
            bound.update(briefs[0])
            return briefs

        async def fake_agg(title, maxn, **kw):
            return [picked]

        with patch.object(appmod.store, "get_title_by_guid",
                          side_effect=_get_title), \
                patch.object(appmod.store, "remember_merged",
                             side_effect=_remember), \
                patch.object(appmod.store, "target_lines",
                             return_value=[_target()]), \
                patch.object(appmod.store, "list_episodes",
                             return_value=["ep1"]), \
                patch.object(appmod.history, "resume_episode",
                             return_value=(None, 0)), \
                patch.object(appmod, "_aggregate_briefs", side_effect=fake_agg), \
                patch.object(appmod.chart_cache, "get_guid", return_value=None), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value={"title": "冷片", "year": "2026"}):
            response = await appmod._vod_line_folder(
                "catalog-cold-og", {}, _client())
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(bound.get("guid"), "catalog-cold-og")

    async def test_cold_folder_miss_returns_empty_and_clears_cooldown(self):
        called = {}

        async def fake_ensure(guid):
            called["guid"] = guid
            return None

        with patch.object(appmod.store, "get_title_by_guid",
                          return_value=None), \
                patch.object(appmod, "ensure_folder_bound",
                             side_effect=fake_ensure), \
                patch.object(appmod, "_CHART_FAILED_TS",
                             {"chart-og": 1.0}), \
                patch.object(appmod.chart_cache, "get_guid",
                             return_value={"title": "小城良方"}), \
                patch.object(appmod.catalog, "find_brief_by_guid",
                             return_value=None):
            response = await appmod._vod_line_folder(
                "chart-og", {}, clientmod.Client(kind="android"))
        data = json.loads(response.body)["data"]
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["list"], [])
        self.assertEqual(called["guid"], "chart-og")
        self.assertNotIn("chart-og", appmod._CHART_FAILED_TS)

    async def test_folder_force_refresh_clears_binding_and_rebinds(self):
        b = _brief(guid="cold-og")
        store_mock = MagicMock()
        store_mock._titles = {"cold-og": {"guid": "cold-og", "title": "旧绑定"}}
        store_mock._orig = {"cold-og": "cold-og"}
        calls = {"n": 0}

        def _get_title(guid):
            calls["n"] += 1
            return None if calls["n"] == 1 else b

        store_mock.get_title_by_guid.side_effect = _get_title
        store_mock.target_lines.return_value = [_target()]
        store_mock.list_episodes.return_value = ["ep1"]
        with patch.object(appmod, "store", store_mock), \
                patch.object(appmod, "ensure_folder_bound",
                             new=AsyncMock(return_value=b)), \
                patch.object(appmod.history, "resume_episode",
                             return_value=(None, 0)):
            response = await appmod._vod_line_folder(
                "cold-og", {"refresh": "1"}, _client())
        data = json.loads(response.body)["data"]
        self.assertNotIn("cold-og", store_mock._titles)
        self.assertNotIn("cold-og", store_mock._orig)
        store_mock._rebuild_index.assert_called_once()
        self.assertEqual(data["total"], 1)


if __name__ == "__main__":
    unittest.main()


def test_ext_config_accepts_folder_bind_wait_and_clamps(tmp_path):
    """POST /v/api/ext/config 白名单含 folder_bind_wait，管理页能改；
    99 夹到 25、0 能存住（0 = 不等待，只后台绑定）、非法值不改动原值。"""
    import config as configmod

    with patch.object(configmod, "_STATE", tmp_path / "config.json"):
        fresh = configmod.Config()
        fresh.save({"sources": [], "removed_ids": []})
        with patch.object(appmod, "config", fresh):
            from fastapi.testclient import TestClient
            with TestClient(appmod.app,
                            raise_server_exceptions=False) as cli:
                r = cli.post("/v/api/ext/config",
                             json={"folder_bind_wait": 2.5})
                assert r.status_code == 200, r.text
                assert fresh.get().get("folder_bind_wait") == 2.5
                r = cli.post("/v/api/ext/config",
                             json={"folder_bind_wait": 99})
                assert r.status_code == 200, r.text
                assert fresh.get().get("folder_bind_wait") == 25.0
                r = cli.post("/v/api/ext/config",
                             json={"folder_bind_wait": 0})
                assert r.status_code == 200, r.text
                assert fresh.get().get("folder_bind_wait") == 0.0
                r = cli.post("/v/api/ext/config",
                             json={"folder_bind_wait": "xx"})
                assert r.status_code == 200, r.text
                assert fresh.get().get("folder_bind_wait") == 0.0
