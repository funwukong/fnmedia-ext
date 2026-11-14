"""B 站模块已整体删除的守护测试。

用户要求：删除 B 站模块及关联代码（界面、功能、源订阅、库、路由、feature），
同时保留网盘、播放优化、竞速等通用增强。

这份测试锁住两件事：
  1. B 站不再以任何形式存在（文件、引用、路由、界面、库键、feature 开关）；
  2. 通用增强仍在（网盘转存与缓存清理、竞速、静默换线、一键重置、
     已缓存条目展开、福利库）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class BilibiliFullyRemovedTests(unittest.TestCase):
    """B 站：文件、引用、路由、界面、库键、开关全部清除。"""

    def test_modules_deleted(self):
        for name in ("bilibili.py", "bilibili_play.py", "bilibili_site.py",
                     "bilibili_browser.py", "bilibili_detail.py"):
            self.assertFalse((PROXY / name).exists(), name)

    def test_no_python_references(self):
        for py in PROXY.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            for token in ("bilibili", "BILIBILI", "BILI_", "bili_site",
                          "bili_play", "bili_audio"):
                self.assertNotIn(token, src, f"{py.name}: {token}")

    def test_no_admin_ui_references(self):
        src = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
        for token in ("startBiliQr", "biliQr", "/v/api/ext/bilibili/",
                      "BILI_POLL_TIMER", "loadBiliStatus"):
            self.assertNotIn(token, src, token)

    def test_no_private_blocks(self):
        # 关键字用拼接构造，避免本文件自身被公开构建的残留扫描命中
        needle = "PRIVATE" + ":BILIBILI"
        for py in PROXY.glob("*.py"):
            self.assertNotIn(needle, py.read_text(encoding="utf-8"), py.name)

    def test_feature_flag_removed(self):
        src = (PROXY / "features.py").read_text(encoding="utf-8")
        self.assertNotIn("BILIBILI", src)

    def test_vlibs_key_removed(self):
        src = (PROXY / "vlibs.py").read_text(encoding="utf-8")
        self.assertNotIn("K_BILIBILI", src)
        self.assertNotIn("_BILIBILI_GUID", src)

    def test_no_state_credentials(self):
        for name in ("bilibili_cookie.json", "bilibili_cookie.txt"):
            self.assertFalse((ROOT / "state" / name).exists(), name)


class CommonEnhancementsKeptTests(unittest.TestCase):
    """通用增强必须保留：网盘 / 播放优化 / 竞速 / 换线 / 重置 / 条目展开。"""

    def setUp(self):
        self.app = (PROXY / "app.py").read_text(encoding="utf-8")
        self.hls = (PROXY / "hls_race.py").read_text(encoding="utf-8")
        self.ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
        self.api = (PROXY / "admin_api.py").read_text(encoding="utf-8")
        self.cfg = (PROXY / "config.py").read_text(encoding="utf-8")

    # —— 网盘 ——
    def test_pan_modules_present(self):
        for name in ("pan.py", "panuc.py", "panali.py", "pansou.py"):
            self.assertTrue((PROXY / name).exists(), name)

    def test_pan_cache_cleanup_endpoint(self):
        self.assertIn('"/v/api/ext/pan/cache/clear"', self.api)
        self.assertIn("async def _pan_cache_cleanup_once(force: bool = False)",
                      self.app)

    def test_pan_cache_ttl_days(self):
        self.assertIn('"pan_cache_ttl_days"', self.cfg)
        self.assertIn("ttl = days * 86400", self.app)

    def test_pan_cache_ui(self):
        for token in ("clearPanCache", "savePanCache", "panCacheDays",
                      "panCacheAuto"):
            self.assertIn(token, self.ui, token)

    # —— 网盘线路兜底 ——
    def test_pansou_fallback_exists(self):
        self.assertIn("async def _pansou_fallback_target", self.hls)
        self.assertIn("_pansou_fallback_target(picked)", self.hls)

    def test_pansou_not_in_line_wall(self):
        self.assertNotIn("_vod_line_folder pansou card failed", self.app)

    def test_pansou_still_in_playback_menu(self):
        self.assertIn('"label": PANSOU_LABEL', self.app)

    # —— 播放优化 / 竞速 ——
    def test_race_and_switch_kept(self):
        for name in ("_race_fastest", "_find_playable", "_ordered_candidates",
                     "_ensure_playable", "_maybe_mid_switch",
                     "_pick_mid_switch_target"):
            self.assertIn(f"def {name}", self.app + self.hls, name)

    def test_headers_timing_fixed(self):
        """解析后再取 headers（海阔期间修好的通用能力，必须保留）。"""
        self.assertIn("headers = dict(picked.get(\"headers\") or {})", self.hls)

    def test_hls_key_formats(self):
        q = (PROXY / "quality.py").read_text(encoding="utf-8")
        self.assertIn("b64decode", q)
        self.assertIn("fromhex", q)

    def test_media_type_probe(self):
        self.assertIn("_probe_direct_media", self.hls)

    # —— 一键重置 ——
    def test_reset_all(self):
        self.assertIn("def reset_all(self)", self.cfg)
        self.assertIn('"/v/api/ext/config/reset"', self.api)
        self.assertIn("resetAll()", self.ui)

    def test_reset_keeps_admin_token(self):
        cfg = (PROXY / "config.py").read_text(encoding="utf-8")
        i = cfg.index("def reset_all(self)")
        self.assertIn("admin_token", cfg[i:i + 1500])

    # —— 已缓存条目展开 ——
    def test_cache_list_expand(self):
        for token in ("cache-wrap", "CACHE_PAGE", "toggleCacheAll",
                      "显示更多"):
            self.assertIn(token, self.ui, token)

    # —— 福利库 ——
    def test_welfare_kept(self):
        c = (PROXY / "catalog.py").read_text(encoding="utf-8")
        self.assertIn("ADULT_CAT", c)


if __name__ == "__main__":
    unittest.main()
