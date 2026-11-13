"""三项改动回归：扫码登录区 / B站列表加载 / 网盘高清兜底。

锁住的故障：
  1. 第三方网盘没有扫码入口 → 用户根本找不到；B 站扫码散在别处。
  2. B 站分区接口 dynamic/region 已下线（-404「啥都木有」）→ 登录后
     分区列表全空，看着像「登录后加载不出视频列表」。
  3. 网盘高清卡挂在海报下方（二级线路墙）→ 点进去提示无权访问。
  4. 直接选网盘线路就触发扫码 → 必须先播别的线再切才行。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class ScanLoginUiTests(unittest.TestCase):
    """扫码登录入口：聚合第三方网盘 + B 站，位于导入订阅下方。"""

    def setUp(self):
        self.ui = (PROXY / "admin_ui.py").read_text(encoding="utf-8")

    def test_nav_entry_exists_and_after_import(self):
        self.assertIn('data-page="scanlogin"', self.ui)
        self.assertLess(self.ui.index('data-page="importsub"'),
                        self.ui.index('data-page="scanlogin"'))

    def test_scan_page_has_pan_and_bilibili(self):
        self.assertIn('id="page-scanlogin"', self.ui)
        self.assertIn("panScanFrame", self.ui)   # 第三方网盘扫码
        self.assertIn("biliQr", self.ui)          # B 站扫码
        self.assertIn("startBiliQr", self.ui)
        self.assertIn("openPanScan", self.ui)

    def test_old_standalone_bilibili_page_removed(self):
        """B 站扫码归类到扫码区，不再单独占一页。"""
        self.assertNotIn('id="page-bilibili"', self.ui)
        self.assertNotIn('data-page="bilibili"', self.ui)

    def test_switch_page_loads_scan_state(self):
        self.assertIn('page==="scanlogin"', self.ui)
        self.assertIn("loadBiliStatus()", self.ui)
        self.assertIn("refreshPanScan()", self.ui)


class BilibiliListingTests(unittest.TestCase):
    """B 站列表加载：分区接口必须是仍可用的那个。"""

    def setUp(self):
        self.browser = (PROXY / "bilibili_browser.py").read_text(encoding="utf-8")

    def test_partition_uses_newlist_not_dead_region_api(self):
        # dynamic/region 已下线（实测 -404），实际请求必须走 newlist。
        self.assertIn("/x/web-interface/newlist", self.browser)
        # 旧接口不得再被真正调用（注释里提及允许）。
        body = self.browser.split("def _partition")[1].split("def _titles_cards")[0]
        self.assertNotIn('"https://api.bilibili.com/x/web-interface/dynamic/region"',
                        body)

    def test_partition_rids_are_official(self):
        self.assertIn('("181", "影视")', self.browser)
        self.assertIn('("23", "电影")', self.browser)
        self.assertIn('("1", "动画")', self.browser)

    def test_popular_is_awaited_not_to_thread(self):
        """popular 是 async，塞进 to_thread 会拿到 coroutine 对象。"""
        self.assertNotIn("to_thread(bilibili_site.popular", self.browser)


class PansouFallbackTests(unittest.TestCase):
    """网盘高清线：兜底而非主动；海报下方不再展示入口。"""

    def setUp(self):
        self.app = (PROXY / "app.py").read_text(encoding="utf-8")
        self.hls = (PROXY / "hls_race.py").read_text(encoding="utf-8")
        self.pansou = (PROXY / "pansou.py").read_text(encoding="utf-8")

    def test_no_pansou_card_in_line_wall(self):
        """海报下方的二级线路墙不再追加网盘卡（点进去无权访问）。"""
        self.assertNotIn("_vod_line_folder pansou card failed", self.app)

    def test_pansou_still_in_playback_menu(self):
        """播放页线路菜单保留网盘线（用户可手动切）。"""
        self.assertIn('"label": PANSOU_LABEL', self.app)

    def test_fallback_only_after_real_lines_fail(self):
        self.assertIn("async def _pansou_fallback_target", self.hls)
        i_find = self.hls.index(
            "good = await _find_playable(picked, request, skip_current=True)")
        i_fb = self.hls.index("fallback = await _pansou_fallback_target(picked)")
        self.assertGreater(i_fb, i_find)

    def test_fallback_skips_when_already_on_pansou(self):
        self.assertIn('if picked.get("line") == PANSOU_LINE:', self.hls)

    def test_arm_does_not_require_login_upfront(self):
        """选网盘线不应立刻判未登录（那会直接弹扫码）。"""
        arm = self.pansou.split("def _pansou_manual_arm")[1].split("\ndef ")[0]
        self.assertNotIn('rec["state"] = "need_login"', arm)

    def test_login_check_happens_at_transfer_time(self):
        """登录检查在真正转存前（run 的候选循环里），不在 arm。"""
        run = self.pansou.split("async def _pansou_run")[1]
        self.assertIn('if not _pan_logged_in(key):', run)
        self.assertNotIn('if rec.get("state") == "need_login":', run)


if __name__ == "__main__":
    unittest.main()
