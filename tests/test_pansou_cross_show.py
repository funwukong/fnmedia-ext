"""PanSou 网盘线在真机上造成的三个问题（用户 2026-11-14 复现）。

现场：A 片播放中切到 PanSou 网盘线 → 退出 → 点播 B 片，B 显示网盘线路、
放不出画面；在菜单里切回其它线路也放不出，必须退出重进才行。

三个独立根因，各自的回归断言都在这里：

1. **档位码跨片污染**：飞牛播放器把「清晰度/线路」选择全局记忆，A 片切到
   网盘线后点播 B 片仍带同一个 bitrate。旧实现只凭 bitrate 就认定用户要网盘
   线，于是 B 一进就被拉到网盘占位画面。
2. **档位码按菜单下标编码**：菜单里「当前线路置顶」，网盘线成为当前线后所有
   真实线路的编码整体漂移一格，播放器记着的档位指向另一条线。必须按线路名
   稳定编码。
3. **网盘变体带宽最高**：自适应码率会主动跳到只有占位循环的网盘变体。必须给
   全场最低带宽。
"""
import time
import unittest

import pansou


class IntentTtlTests(unittest.TestCase):
    """`_pansou_is_user_pick` 的时效语义。"""

    def setUp(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def tearDown(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def test_mark_then_is_pick(self):
        pansou._pansou_mark_user_pick("mg-1")
        self.assertTrue(pansou._pansou_is_user_pick("mg-1"))

    def test_empty_mg_never_pick(self):
        pansou._pansou_mark_user_pick("")
        self.assertFalse(pansou._pansou_is_user_pick(""))

    def test_expired_pick_is_dropped(self):
        pansou._pansou_mark_user_pick("mg-1")
        # 把登记时间人工挪到 TTL 之前
        pansou._PANSOU_USER_PICK["mg-1"]["ts"] = (
            time.time() - pansou._PANSOU_PICK_TTL - 1)
        self.assertFalse(pansou._pansou_is_user_pick("mg-1"))
        self.assertNotIn("mg-1", pansou._PANSOU_USER_PICK)

    def test_pick_renewed_while_in_use(self):
        """播放中每次取 main 都续期：2 小时的片子不会播到一半掉回真实线路。"""
        pansou._pansou_mark_user_pick("mg-1")
        near = time.time() - pansou._PANSOU_PICK_TTL + 5
        pansou._PANSOU_USER_PICK["mg-1"]["ts"] = near
        self.assertTrue(pansou._pansou_is_user_pick("mg-1"))
        self.assertGreater(pansou._PANSOU_USER_PICK["mg-1"]["ts"], near)

    def test_other_show_untouched(self):
        """A 片的手切意图不会让 B 片被认为也在网盘线。"""
        pansou._pansou_mark_user_pick("mg-a")
        self.assertTrue(pansou._pansou_is_user_pick("mg-a"))
        self.assertFalse(pansou._pansou_is_user_pick("mg-b"))


class PlayEvidenceTests(unittest.TestCase):
    """`_PANSOU_REAL_PLAY`：只有真实线路起播过的片子才允许切网盘线。"""

    def setUp(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def tearDown(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()

    def test_evidence_recorded_and_expires(self):
        pansou._pansou_mark_real_play("mg-1")
        self.assertTrue(pansou._pansou_real_played("mg-1"))
        pansou._PANSOU_REAL_PLAY["mg-1"] = (
            time.time() - pansou._PANSOU_REAL_PLAY_TTL - 1)
        self.assertFalse(pansou._pansou_real_played("mg-1"))

    def test_reset_keeps_play_evidence(self):
        """``_pansou_reset`` 不能抹掉起播证据。

        切到真实线路本身就是「真实起播」，若 reset 顺手清掉它，紧接着的菜单
        切网盘线就会失去证据、被判成跨片残留，用户永远切不到网盘线。
        """
        pansou._pansou_mark_real_play("mg-1")
        pansou._pansou_mark_user_pick("mg-1")
        pansou._pansou_reset("mg-1")
        self.assertFalse(pansou._pansou_is_user_pick("mg-1"))
        self.assertTrue(pansou._pansou_real_played("mg-1"))

    def test_clear_user_pick_keeps_state(self):
        """只清意图的接口不能动状态机。"""
        pansou._PANSOU_STATE["mg-1"] = {"state": "ready", "title": "T"}
        pansou._pansou_mark_user_pick("mg-1")
        pansou._pansou_clear_user_pick("mg-1")
        self.assertFalse(pansou._pansou_is_user_pick("mg-1"))
        self.assertEqual(pansou._PANSOU_STATE["mg-1"]["state"], "ready")

    def test_maps_are_bounded(self):
        """意图表有上限，长期挂机不会无界增长。"""
        for i in range(pansou._PANSOU_MAX + 40):
            pansou._PANSOU_REAL_PLAY["mg-%d" % i] = float(i)
        pansou._pansou_trim_maps()
        self.assertLessEqual(len(pansou._PANSOU_REAL_PLAY),
                             pansou._PANSOU_MAX)


if __name__ == "__main__":
    unittest.main()


class ResetCancelsInflightTests(unittest.TestCase):
    """切回真实线路时，在途的转存任务必须被取消。

    否则它跑完会把状态写回 ready、把手动快照重新填上，用户明明已经切回
    真实线路却又被悄悄拉回网盘线（真机上表现为「切回其它线路也不能播放」）。
    """

    def test_reset_cancels_running_task(self):
        mg = "m-reset-cancel"

        class FakeTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        task = FakeTask()
        pansou._PANSOU_TASKS[mg] = task
        pansou._PANSOU_STATE[mg] = {"phase": "running"}
        pansou._PANSOU_MAN_SNAP[mg] = {"url": "x"}
        pansou._pansou_mark_user_pick(mg)
        try:
            pansou._pansou_reset(mg)
            self.assertTrue(task.cancelled, "在途任务必须被取消")
            self.assertNotIn(mg, pansou._PANSOU_TASKS)
            self.assertNotIn(mg, pansou._PANSOU_STATE)
            self.assertNotIn(mg, pansou._PANSOU_MAN_SNAP)
            self.assertNotIn(mg, pansou._PANSOU_USER_PICK)
        finally:
            pansou._PANSOU_TASKS.pop(mg, None)
            pansou._PANSOU_STATE.pop(mg, None)
            pansou._PANSOU_MAN_SNAP.pop(mg, None)
            pansou._PANSOU_USER_PICK.pop(mg, None)
            pansou._PANSOU_REAL_PLAY.pop(mg, None)
