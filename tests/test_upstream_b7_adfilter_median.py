"""B7：去广告片头组中位数防误判（_lead_group_looks_routine / _median）。

片头组分片数与后续组中位数接近时判为常规 DISCONTINUITY 封装而不删；
URL 强信号（广告关键字/广告投放域）除外。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter


def _playlist(blocks, host="cdn.test", ad_lead=False) -> str:
    """blocks: 每组分片数；每分片 6 秒；组间 DISCONTINUITY 分隔。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:8"]
    for bi, n in enumerate(blocks):
        if bi:
            lines.append("#EXT-X-DISCONTINUITY")
        for i in range(n):
            lines.append("#EXTINF:6.0,")
            seg_host = "ad.test/ads/x" if (ad_lead and bi == 0) else host
            lines.append(f"https://{seg_host}/seg{bi}_{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _seg_count(text: str) -> int:
    return text.count("#EXTINF")


class MedianGuardTests(unittest.TestCase):
    def test_median(self):
        self.assertEqual(adfilter._median([]), 0.0)
        self.assertEqual(adfilter._median([6, 8]), 8.0)
        self.assertEqual(adfilter._median([4, 6, 8]), 6.0)

    def test_lead_group_looks_routine(self):
        self.assertTrue(adfilter._lead_group_looks_routine(6, [8, 6]))
        self.assertTrue(adfilter._lead_group_looks_routine(6, [6, 6, 6]))
        self.assertFalse(adfilter._lead_group_looks_routine(2, [20, 20]))
        self.assertFalse(adfilter._lead_group_looks_routine(6, []))
        self.assertFalse(adfilter._lead_group_looks_routine(0, [6, 6]))

    def test_routine_lead_group_is_kept(self):
        # 片头组 6 片 ×6s=36s：无防误判时评分 2(片头)+1(同构)+1(典型时长)=4 会被删；
        # 与后续组中位数 8 接近（|6-8|<=max(3,4)），应保留。
        text = _playlist([6, 8, 6])
        out, removed = adfilter._detect_ad_blocks(text)
        self.assertEqual(_seg_count(out), 20)
        self.assertEqual(removed, 0)

    def test_lead_group_with_ad_keyword_is_still_dropped(self):
        # 同样规模的片头组，但 URL 带 /ads/ 强信号：防误判不适用，仍删除。
        text = _playlist([6, 8, 6], ad_lead=True)
        out, removed = adfilter._detect_ad_blocks(text)
        self.assertLess(_seg_count(out), 20)
        self.assertGreater(removed, 0)
        self.assertNotIn("seg0_", out)


if __name__ == "__main__":
    unittest.main()
