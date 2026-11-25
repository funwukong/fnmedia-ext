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
        self.ui = (PROXY / "assets" / "admin.html").read_text(encoding="utf-8")

    def test_nav_entry_exists_and_after_import(self):
        self.assertIn('data-page="scanlogin"', self.ui)
        self.assertLess(self.ui.index('data-page="importsub"'),
                        self.ui.index('data-page="scanlogin"'))

    def test_scan_page_has_pan(self):
        self.assertIn('id="page-scanlogin"', self.ui)
        self.assertIn("panScanFrame", self.ui)   # 第三方网盘扫码
        self.assertIn("openPanScan", self.ui)

    def test_bilibili_ui_removed(self):
        """B 站模块已整体删除，管理台不应再有 B 站登录区。"""
        self.assertNotIn("startBiliQr", self.ui)
        self.assertNotIn("biliQr", self.ui)
        self.assertNotIn("/v/api/ext/bilibili/", self.ui)

    def test_switch_page_loads_scan_state(self):
        self.assertIn('page==="scanlogin"', self.ui)
        self.assertIn("refreshPanScan()", self.ui)



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
