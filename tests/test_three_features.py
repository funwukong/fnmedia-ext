"""三项改动回归测试。

1) 网盘播放对齐上游：PanSou 搜索结果映射为【真实的一集】出现在线路里
   （pansou_search.search() → OnlineTitle + Episode(resolve=True)），
   且地址/云类型/超时/条数全部走 config（不再硬编码）。
2) 导入订阅探活：失效源与风险源（网盘类）自动删除，报告里给出 deleted。
3) 资源站列表不分页：一次渲染全部 + 容器内自适应 + 展开/收起。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class PansouSearchLineTests(unittest.TestCase):
    """1) 网盘结果作为真实一集出现在线路里（上游同款）。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import pansou_search
        self.mod = pansou_search

    def test_source_def_registered(self):
        d = self.mod.source_def()
        self.assertEqual(d["id"], "builtin-pansou")
        self.assertTrue(d["pansou"])
        self.assertIn("name", d)

    def test_search_maps_to_onlinetitle_with_episode(self):
        """搜索结果 → OnlineTitle，其线路下是可播 Episode。"""
        rows = [("quark", "https://pan.quark.cn/s/abc123", "火影忍者 剧场版"),
                ("uc", "https://drive.uc.cn/s/def456", "火影忍者 剧场版")]

        orig = self.mod._query_sync
        self.mod._query_sync = lambda title: rows
        try:
            import asyncio
            titles = asyncio.run(
                self.mod.search(None, self.mod.source_def(), "火影忍者"))
        finally:
            self.mod._query_sync = orig

        self.assertTrue(titles, "应产出标题")
        t = titles[0]
        self.assertIn("PanSou网盘", t.sources)
        eps = t.sources["PanSou网盘"]
        self.assertEqual(len(eps), 2, "同名资源的两个分享应合并为同一标题下两集")
        for e in eps:
            # resolve=True：播放时走 providers.resolve_share 转存出直链
            self.assertTrue(e.resolve, "网盘集必须走转存解析")
            self.assertFalse(e.direct)

    def test_irrelevant_results_dropped(self):
        orig = self.mod._query_sync
        self.mod._query_sync = lambda title: [
            ("quark", "https://pan.quark.cn/s/x", "完全不相关的片名")]
        try:
            import asyncio
            titles = asyncio.run(
                self.mod.search(None, self.mod.source_def(), "火影忍者"))
        finally:
            self.mod._query_sync = orig
        self.assertEqual(titles, [], "不相关结果应被相关度过滤")

    def test_config_driven_not_hardcoded(self):
        """地址/云类型/超时/条数走 config（实例不可达时用户可自行更换）。"""
        pansou_src = (PROXY / "pansou.py").read_text(encoding="utf-8")
        self.assertNotIn('_PANSOU_SEARCH_BASE = "', pansou_src)
        self.assertIn("def _pansou_base_url()", pansou_src)
        self.assertIn("def _pansou_cloud_types()", pansou_src)
        self.assertIn("def _pansou_timeout()", pansou_src)
        cfg = (PROXY / "config.py").read_text(encoding="utf-8")
        for key in ("pansou_enabled", "pansou_url", "pansou_cloud_types",
                    "pansou_src", "pansou_timeout", "pansou_limit"):
            self.assertIn(f'"{key}"', cfg, key)

    def test_provider_dispatch_hooks_pansou(self):
        src = (PROXY / "providers.py").read_text(encoding="utf-8")
        self.assertIn('if source.get("pansou"):', src)
        self.assertIn("import pansou_search as _pss", src)


class ImportCleanupTests(unittest.TestCase):
    """2) 导入订阅：失效源与风险源自动删除。"""

    def test_remove_sources_exists(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import config as cfgmod
        self.assertTrue(hasattr(cfgmod.config, "remove_sources"))

    def test_import_endpoint_reports_deleted(self):
        src = (PROXY / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn('"deleted": len(deleted)', src)
        self.assertIn('"deleted_detail": deleted[:60]', src)

    def test_risky_sources_removed_on_import(self):
        src = (PROXY / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn("风险源（网盘类），已自动删除", src)
        self.assertIn("config.remove_sources(", src)


class SourceListNoPagingTests(unittest.TestCase):
    """3) 资源站列表不分页，自适应显示 + 展开全量。"""

    def setUp(self):
        self.ui = (PROXY / "assets" / "admin.html").read_text(encoding="utf-8")

    def test_no_paging_controls(self):
        self.assertNotIn("function srcPage(", self.ui)
        self.assertNotIn("SRC_PAGE_SIZE", self.ui)
        self.assertNotIn('onclick="srcPage(-1)"', self.ui)
        self.assertNotIn('onclick="srcPage(1)"', self.ui)

    def test_expand_toggle_exists(self):
        self.assertIn("function toggleSrcExpand()", self.ui)
        self.assertIn('onclick="toggleSrcExpand()"', self.ui)
        self.assertIn('id="srcExpandBtn"', self.ui)

    def test_container_scroll_and_fixed_layout(self):
        self.assertIn(".src-wrap{", self.ui)
        self.assertIn("table-layout:fixed", self.ui)
        self.assertIn(".src-wrap.expanded{", self.ui)

    def test_all_rows_rendered(self):
        """renderSources 全量渲染，不做分页截断。

        2026-11-14：渲染改为「首屏同步 + 剩余分帧追加」，因此允许出现
        ``slice(pos, pos+SRC_CHUNK)`` —— 那是把同一份全量列表分批铺进 DOM，
        不是页大小切分。这里改为断言「没有分页大小常量」且「分批循环的第
        三步是继续追加，直到列表取空」，从而保证最终行数等于筛选结果数。
        """
        i = self.ui.index("function renderSources()")
        seg = self.ui[i:i + 4000]
        self.assertNotIn("SRC_PAGE_SIZE", seg)
        # 全量来源：直接对整个筛选结果切片，无 page*size 偏移
        self.assertIn("srcsAll.slice(", seg)
        self.assertNotIn("SRC_PAGE_SIZE*", seg)
        # 分批推进：位置按批次递增，取空即止（不是取到某页上限就停）
        self.assertIn("pos+=SRC_CHUNK", seg)
        self.assertIn("if(!slice.length)", seg)


if __name__ == "__main__":
    unittest.main()
