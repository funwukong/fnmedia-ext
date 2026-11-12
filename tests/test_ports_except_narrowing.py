"""移植批宽捕获收窄的回归测试（P2 门禁 total ≤ 639）。

上游移植三项新增的 8 处 `except Exception` 已收窄为具体异常元组。
约定：元组内的异常仍被吞掉（不影响主流程）；元组外的异常应上抛、
而非静默吞掉——本文件锁住这个约定，防止以后有人随手改回宽捕获。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import history as histmod
import store as storemod
from history import History


class _BadBrief(dict):
    """get() 按指定异常类型抛错的 brief，用于触发收窄守卫。"""

    def __init__(self, exc_type):
        super().__init__(title="坏 brief", guid="og-bad")
        self._exc_type = exc_type

    def get(self, key, default=None):
        raise self._exc_type("bad brief")


class NarrowingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_state = histmod._STATE
        histmod._STATE = Path(self._tmp.name) / "history.json"
        self.addCleanup(setattr, histmod, "_STATE", self._orig_state)
        self.h = History()

    def test_history_snapshot_swallows_data_errors(self):
        """_history_snapshot：build_snapshot 抛数据类异常时返回 None。"""
        brief = {"guid": "og-x", "title": "剧"}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid",
                             return_value=brief):
            for exc in (TypeError, ValueError, KeyError, AttributeError,
                        IndexError, OverflowError):
                with patch.object(appmod.histmod, "build_snapshot",
                                  side_effect=exc("bad")):
                    self.assertIsNone(
                        appmod._history_snapshot("raw", "og-x"),
                        "应吞掉 %s" % exc.__name__)

    def test_history_snapshot_propagates_unknown_errors(self):
        """_history_snapshot：元组外的异常应上抛，不静默吞掉。"""
        brief = {"guid": "og-x", "title": "剧"}
        with patch.object(appmod.store, "get_episode", return_value=None), \
                patch.object(appmod.store, "get_title_by_guid",
                             return_value=brief), \
                patch.object(appmod.histmod, "build_snapshot",
                             side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                appmod._history_snapshot("raw", "og-x")

    def test_prefetch_swallows_runtime_error(self):
        """prefetch_folder_binding：store 抛 RuntimeError 时静默返回。"""
        with patch.object(appmod.store, "get_title_by_guid",
                          side_effect=RuntimeError("no loop")):
            self.assertIsNone(appmod.prefetch_folder_binding("og-x"))

    def test_latest_episode_record_swallows_bad_brief(self):
        """latest_episode_record：标题归一化抛数据类异常时不中断，返回 None。"""
        with patch.object(storemod.store, "get_title_by_guid",
                          return_value=_BadBrief(TypeError)):
            self.assertIsNone(self.h.latest_episode_record("og-x"))

    def test_latest_episode_record_propagates_unknown_errors(self):
        """latest_episode_record：元组外的异常应上抛。"""
        with patch.object(storemod.store, "get_title_by_guid",
                          return_value=_BadBrief(RuntimeError)):
            with self.assertRaises(RuntimeError):
                self.h.latest_episode_record("og-x")


if __name__ == "__main__":
    unittest.main()
