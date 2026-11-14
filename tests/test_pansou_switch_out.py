"""从网盘线手切到其他线后不得被残留意图弹回网盘线。

现象复现：播放页切到 PanSou 线 → 有手切意图 → 用户再切到别的线路，
若意图不清，后续不带 line 的 main/preset 请求会被塞回网盘线，
表现为「从网盘线切其他线路后放不出来」。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
sys.path.insert(0, os.path.dirname(__file__))

import pansou  # noqa: E402


class SwitchOutOfPansouTests(unittest.TestCase):
    MG = "media-guid-switch"

    def tearDown(self):
        pansou._pansou_reset(self.MG)

    def test_mark_then_reset_clears_intent(self):
        pansou._pansou_mark_user_pick(self.MG)
        self.assertTrue(pansou._pansou_is_user_pick(self.MG))
        pansou._pansou_reset(self.MG)
        self.assertFalse(pansou._pansou_is_user_pick(self.MG))

    def test_reset_drops_state_and_snapshot(self):
        pansou._PANSOU_STATE[self.MG] = {"state": "ready"}
        pansou._PANSOU_MAN_SNAP[self.MG] = {"url": "https://cdn/x.m3u8"}
        pansou._pansou_mark_user_pick(self.MG)
        pansou._pansou_reset(self.MG)
        self.assertNotIn(self.MG, pansou._PANSOU_STATE)
        self.assertNotIn(self.MG, pansou._PANSOU_MAN_SNAP)
        self.assertFalse(pansou._pansou_is_user_pick(self.MG))

    def test_reset_empty_mg_is_noop(self):
        pansou._pansou_reset("")
        self.assertFalse(pansou._pansou_is_user_pick(""))


if __name__ == "__main__":
    unittest.main()
