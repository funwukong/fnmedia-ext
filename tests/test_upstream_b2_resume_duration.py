"""B2：_ensure_resume_duration（_mirror_item_ts 完整语义）。

- duration=0 且 ts>0 时兜底 duration=ts+60，同步 item.ts/watched_ts/runtime；
- 真实时长绝不被兜底覆盖（只在 duration 仍为 0 时兜底）；
- ts=0 时不动。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod


def _info(ts: int, item_duration: int) -> dict:
    return {"ts": ts, "duration": 0,
            "item": {"guid": "g", "ts": 0, "watched_ts": 0,
                     "duration": item_duration, "runtime": 0}}


class EnsureResumeDurationTests(unittest.TestCase):
    def test_snapshot_branch_duration_zero_gets_ts_plus_60(self):
        # 快照分支 duration 恒 0：ts=706 不再被钳成 0
        info = _info(706, 0)
        out = appmod._ensure_resume_duration(info)
        self.assertEqual(out["item"]["duration"], 766)
        self.assertEqual(out["item"]["ts"], 706)
        self.assertEqual(out["item"]["watched_ts"], 706)
        self.assertEqual(out["item"]["runtime"], 12)

    def test_real_duration_is_never_overwritten(self):
        # fork 真实时长（CMS/HLS 探测）：2809 秒不动
        info = _info(706, 2809)
        out = appmod._ensure_resume_duration(info)
        self.assertEqual(out["item"]["duration"], 2809)
        self.assertEqual(out["item"]["ts"], 706)
        self.assertEqual(out["item"]["watched_ts"], 706)
        self.assertEqual(out["item"]["runtime"], 46)

    def test_nonzero_duration_below_ts_is_kept(self):
        # 兜底只在 duration 仍为 0 时生效：已有真实时长 500（< ts）也不覆盖
        info = _info(706, 500)
        out = appmod._ensure_resume_duration(info)
        self.assertEqual(out["item"]["duration"], 500)
        self.assertEqual(out["item"]["runtime"], 8)

    def test_zero_ts_leaves_item_alone(self):
        info = _info(0, 0)
        out = appmod._ensure_resume_duration(info)
        self.assertEqual(out["item"]["duration"], 0)
        self.assertEqual(out["item"]["ts"], 0)
        self.assertEqual(out["item"]["watched_ts"], 0)
        self.assertEqual(out["item"]["runtime"], 0)

    def test_mirror_alias_is_same_function(self):
        self.assertIs(appmod._mirror_item_ts, appmod._ensure_resume_duration)


if __name__ == "__main__":
    unittest.main()
