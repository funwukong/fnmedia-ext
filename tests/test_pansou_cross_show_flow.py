"""端到端回归：网盘线不出现在新片的默认路径上，且切回真实线路有效。

复现真机时序（2026-11-14 用户报障）：

1. A 片用真实线路起播（preset?line=A）；
2. A 片播放中从「清晰度」菜单切到网盘线（play/play 带网盘线 bitrate）；
3. 退出，点播 B 片——播放器仍带着第 2 步的 bitrate；
4. 期望：B 按默认真实线路播放，不被拉进网盘占位画面；
5. B 在菜单里切回真实线路，也期望立刻能播。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as appmod
import hls_race
import pansou
import store as storemod


class LineBitrateStabilityTests(unittest.TestCase):
    """档位码必须与菜单顺序无关（菜单里当前线路置顶，下标会整体漂移）。"""

    def test_same_line_same_code_any_order(self):
        a = appmod._line_bitrate("line-a", ["line-a", "line-b"])
        b = appmod._line_bitrate("line-a", ["line-b", "line-a"])
        self.assertEqual(a, b)

    def test_distinct_lines_distinct_codes(self):
        names = ["line-a", "line-b", "line-c", pansou.PANSOU_LINE]
        codes = [appmod._line_bitrate(n, names) for n in names]
        self.assertEqual(len(set(codes)), len(codes))

    def test_decode_roundtrip(self):
        names = ["line-a", "line-b", pansou.PANSOU_LINE]
        for n in names:
            code = appmod._line_bitrate(n, names)
            self.assertEqual(appmod._line_of_bitrate(code, names), n)

    def test_decode_ignores_unknown_code(self):
        names = ["line-a", "line-b"]
        self.assertIsNone(appmod._line_of_bitrate(12345, names))
        self.assertIsNone(appmod._line_of_bitrate(None, names))

    def test_legacy_index_code_still_decodes(self):
        """升级前播过的档位（下标编码）仍能解释，避免升级瞬间断播。"""
        names = ["line-a", "line-b"]
        self.assertEqual(
            appmod._line_of_bitrate(90_000_000 + 1 * 10001, names), "line-b")

    def test_qualities_use_stable_codes(self):
        """菜单项也必须用稳定编码，否则播放器记住的档位仍会漂移。"""
        from unittest.mock import patch
        choices = [{"line": "line-a", "label": "A"},
                   {"line": "line-b", "label": "B"},
                   {"line": pansou.PANSOU_LINE, "label": "PanSou"}]
        with patch.object(appmod, "_line_choices", return_value=choices):
            quals = appmod._qualities({"online_guid": "x", "kind": "title"})
        names = [c["line"] for c in choices]
        self.assertEqual(len(quals), 3)
        for q, c in zip(quals, choices):
            self.assertEqual(q["bitrate"],
                             appmod._line_bitrate(c["line"], names))


class PanSouMasterBandwidthTests(unittest.TestCase):
    """网盘变体必须是全场最低带宽，否则自适应码率会主动跳向占位循环。"""

    def test_pansou_variant_lowest(self):
        targets = [{"line": "line-a", "index": 0},
                   {"line": "line-b", "index": 0},
                   {"line": pansou.PANSOU_LINE, "index": 0,
                    "_pansou_manual": True}]
        rows = [hls_race._variant_stream_inf(t, i)
                for i, t in enumerate(targets)]
        bws = [int(r.split("BANDWIDTH=")[1].split(",")[0]) for r in rows]
        self.assertEqual(len(bws), 3)
        pansou_idx = [i for i, t in enumerate(targets)
                      if t.get("_pansou_manual")][0]
        self.assertEqual(bws[pansou_idx], min(bws))
        self.assertLess(bws[pansou_idx], min(
            b for i, b in enumerate(bws) if i != pansou_idx))

    def test_no_real_line_blocks_pansou(self):
        """网盘线是唯一线路时不能把它设为当前线（客户端将无从切回）。"""
        mg = "m-noreal"
        pansou._pansou_mark_real_play(mg)
        titles = {"t-noreal": {"guid": "t-noreal", "name": "只有网盘",
                               "lines": {}, "episodes": []}}
        fake = SimpleNamespace(
            get_title_by_guid=lambda g: titles.get(g),
            target_lines=lambda t: [{"line": pansou.PANSOU_LINE, "index": 0}])
        with patch.object(appmod, "store", fake):
            self.assertFalse(
                appmod._pansou_pick_usable(mg, {"online_guid": "t-noreal",
                                                "kind": "title"}))
            self.assertFalse(
                appmod._pansou_switch_allowed(mg, {"online_guid": "t-noreal",
                                                   "kind": "title"}))

    def test_real_variants_still_distinct(self):
        """差异化带宽不能因为网盘线改最低而被破坏（否则变体会被合并）。"""
        rows = [hls_race._variant_stream_inf({"line": "line-%d" % i}, i)
                for i in range(4)]
        bws = [int(r.split("BANDWIDTH=")[1].split(",")[0]) for r in rows]
        self.assertEqual(len(set(bws)), 4)


class RealLineSelectionTests(unittest.TestCase):
    """`_pansou_real_lines` 的边界：判不出来时不阻断下游。"""

    def test_no_target_returns_none(self):
        """未知（解析不出目标）用 None 表示，与「确定没有真实线路」的 [] 区分。"""
        self.assertIsNone(appmod._pansou_real_lines(None))

    def test_unknown_show_returns_none(self):
        """store 不认识的片（榜单骨架等）不给结论，交给下游。"""
        got = appmod._pansou_real_lines({"online_guid": "nope",
                                         "kind": "title"})
        self.assertIsNone(got)


class SwitchAllowedTests(unittest.TestCase):
    """`_pansou_switch_allowed`：跨片残留 vs 真的在切。"""

    def setUp(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def tearDown(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def test_new_show_without_evidence_blocked(self):
        """B 片没有任何本片起播证据 => 播放器带来的网盘 bitrate 是残留。"""
        self.assertFalse(appmod._pansou_switch_allowed(
            "mg-b", {"online_guid": "nope", "kind": "title"}))

    def test_show_with_evidence_allowed(self):
        pansou._pansou_mark_real_play("mg-a")
        self.assertTrue(appmod._pansou_switch_allowed(
            "mg-a", {"online_guid": "nope", "kind": "title"}))

    def test_evidence_does_not_leak_across_shows(self):
        pansou._pansou_mark_real_play("mg-a")
        self.assertFalse(appmod._pansou_switch_allowed(
            "mg-b", {"online_guid": "nope", "kind": "title"}))


class PickUsableTests(unittest.TestCase):
    """`_pansou_pick_usable`：已有意图的后续请求。"""

    def setUp(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def tearDown(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def test_no_intent_not_usable(self):
        self.assertFalse(appmod._pansou_pick_usable(
            "mg-a", {"online_guid": "nope", "kind": "title"}))

    def test_intent_without_real_lines_not_usable(self):
        """网盘线是唯一线路时必须回落到真实线路，否则用户切不回去。"""
        pansou._pansou_mark_user_pick("mg-a")
        fake = SimpleNamespace(
            get_title_by_guid=lambda g: {"guid": g},
            target_lines=lambda t: [{"line": pansou.PANSOU_LINE, "index": 0}])
        with patch.object(appmod, "store", fake):
            self.assertFalse(appmod._pansou_pick_usable(
                "mg-a", {"online_guid": "t1", "kind": "title"}))

    def test_intent_with_unknown_target_is_usable(self):
        pansou._pansou_mark_user_pick("mg-a")
        self.assertTrue(appmod._pansou_pick_usable(
            "mg-a", {"online_guid": "nope", "kind": "title"}))


if __name__ == "__main__":
    unittest.main()
