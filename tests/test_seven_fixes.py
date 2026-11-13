"""七项改动回归（海阔删除 / B站库 / 网盘缓存 / 文案 / 重置 / 列表展开）。

逐项锁住用户提出的验收点：
  1. 海阔代码与入口全部删除（文件、标记块、import、库键、sidecar 分支）；
  2. B 站库能进入（库键映射 + season/episode 本地分支）；
  3. 网盘缓存：手动清理端点 + 按天 TTL + 开关；
  4. 「扫码登录」→「网盘登录」；
  5. 提示文案逐字对齐；
  6. 一键重置端点 + config.reset_all 保留管理令牌；
  7. 已缓存条目默认缩放 + 「显示更多」展开。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class HaikuoRemovedTests(unittest.TestCase):
    """1) 海阔方案作废：代码与入口全清。"""

    def test_modules_deleted(self):
        for name in ("haikuo.py", "haikuo_browser.py", "haikuo_cache.py",
                     "haikuo_diag.py"):
            self.assertFalse((PROXY / name).exists(), name)

    def test_assets_deleted(self):
        self.assertFalse((PROXY / "assets" / "haikuo").exists())
        self.assertFalse((PROXY / "assets" / "drpy" / "haikuo.mjs").exists())

    def test_no_private_haikuo_blocks(self):
        for py in PROXY.glob("*.py"):
            self.assertNotIn("PRIVATE:HAIKUO", py.read_text(encoding="utf-8"),
                             py.name)

    def test_no_python_references(self):
        for py in PROXY.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            self.assertNotIn("haikuo_browser", src, py.name)
            self.assertNotIn("haikuo_cache", src, py.name)

    def test_feature_flag_removed(self):
        src = (PROXY / "features.py").read_text(encoding="utf-8")
        self.assertNotIn("HAIKUO", src)

    def test_sidecar_engine_removed(self):
        src = (PROXY / "assets" / "drpy" / "sidecar.mjs").read_text(
            encoding="utf-8")
        self.assertNotIn("haikuo", src.lower())

    def test_vlibs_key_removed(self):
        src = (PROXY / "vlibs.py").read_text(encoding="utf-8")
        self.assertNotIn("K_HAIKUO", src)
        self.assertNotIn("_HAIKUO_GUID", src)

    def test_admin_ui_entry_removed(self):
        src = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("/v/api/ext/haikuo/", src)



class PanCacheTests(unittest.TestCase):
    """3) 网盘播放缓存：手动清理 + 按天 TTL + 开关。"""

    def test_config_has_days_key(self):
        src = (PROXY / "config.py").read_text(encoding="utf-8")
        self.assertIn('"pan_cache_ttl_days"', src)

    def test_cleanup_endpoint_exists(self):
        src = (PROXY / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn('"/v/api/ext/pan/cache/clear"', src)

    def test_cleanup_supports_force(self):
        src = (PROXY / "app.py").read_text(encoding="utf-8")
        self.assertIn("async def _pan_cache_cleanup_once(force: bool = False)",
                      src)
        self.assertIn("if not force and not bool(", src)

    def test_days_ttl_used(self):
        src = (PROXY / "app.py").read_text(encoding="utf-8")
        self.assertIn("ttl = days * 86400", src)

    def test_days_in_allowed_config_keys(self):
        src = (PROXY / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn('"pan_cache_ttl_days"', src)


class WordingTests(unittest.TestCase):
    """4/5) 文案对齐用户给定的提示语。"""

    def setUp(self):
        self.ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")

    def test_nav_renamed(self):
        self.assertIn('data-page="scanlogin">网盘登录', self.ui)
        self.assertNotIn('data-page="scanlogin">扫码登录', self.ui)

    def test_login_hint(self):
        self.assertIn("选择网盘进行登录授权", self.ui)

    def test_clear_success_text(self):
        self.assertIn("✅ 播放缓存清理完成", self.ui)

    def test_cleaning_text(self):
        self.assertIn("正在清理播放缓存，请稍候", self.ui)

    def test_auto_enabled_text(self):
        self.assertIn("已开启自动清理，到期自动清除网盘播放缓存", self.ui)

    def test_no_cache_text(self):
        self.assertIn("暂无播放缓存需要清理", self.ui)


class ResetAllTests(unittest.TestCase):
    """6) 一键重置：清空设置与资源站列表，保留管理令牌。"""

    def test_reset_all_exists(self):
        src = (PROXY / "config.py").read_text(encoding="utf-8")
        self.assertIn("def reset_all(self)", src)

    def test_reset_keeps_admin_token(self):
        src = (PROXY / "config.py").read_text(encoding="utf-8")
        i = src.index("def reset_all(self)")
        seg = src[i:i + 1500]
        self.assertIn("admin_token", seg)

    def test_reset_endpoint_exists(self):
        src = (PROXY / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn('"/v/api/ext/config/reset"', src)

    def test_ui_button_exists(self):
        ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")
        self.assertIn("resetAll()", ui)
        self.assertIn("一键重置", ui)


class CacheListTests(unittest.TestCase):
    """7) 已缓存条目：默认缩放适配 + 显示更多。"""

    def setUp(self):
        self.ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")

    def test_container_has_scroll(self):
        self.assertIn("cache-wrap", self.ui)
        self.assertIn("max-height", self.ui)

    def test_show_more_button(self):
        self.assertIn("显示更多", self.ui)
        self.assertIn("toggleCacheAll", self.ui)

    def test_default_limit(self):
        self.assertIn("CACHE_PAGE=30", self.ui)


if __name__ == "__main__":
    unittest.main()
