"""清单级去广告增强：分片序号修正 / DATERANGE 收紧 / 广告域名拦截。

先写测试再改代码（TDD）。基线行为（改动前）下，标为“增强后才通过”的
用例应失败；控制用例（正常清单原样通过、不误伤）基线即通过。

覆盖：
  E1 剥离前导分片后 #EXT-X-MEDIA-SEQUENCE / #EXT-X-DISCONTINUITY-SEQUENCE
     修正（同步层 filter_hls 与 SPS 异步层 filter_hls_async）；
  E2 #EXT-X-DATERANGE 只有带 SCTE35-OUT/SCTE35-CMD/插播 CLASS 才算广告
     区间；SCTE35-IN 的 DATERANGE 结束区间；普通 DATERANGE（章节/元数据）
     保留；
  E3 广告投放域名（doubleclick/googlesyndication 等纯广告域）上的分片
     按 host 后缀拦截；形似域名不误伤。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter
import config as cfgmod


def _pl(blocks, media_seq=0, disc_seq=None, endlist=True):
    """blocks: 每块是 [(dur, url), ...]，块间插 #EXT-X-DISCONTINUITY。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10"]
    if media_seq is not None:
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_seq}")
    if disc_seq is not None:
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_seq}")
    for bi, blk in enumerate(blocks):
        if bi:
            lines.append("#EXT-X-DISCONTINUITY")
        for dur, url in blk:
            lines.append(f"#EXTINF:{dur},")
            lines.append(url)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _segs(text):
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")]


def _dur_sum(text):
    import re
    return sum(float(m.group(1)) for m in
               re.finditer(r"#EXTINF:([\d.]+)", text))


def _media_seq(text):
    import re
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _disc_seq(text):
    import re
    m = re.search(r"#EXT-X-DISCONTINUITY-SEQUENCE:\s*(\d+)", text)
    return int(m.group(1)) if m else None


class _BW:
    """最小 SPS NAL 构造器（与 test_source_proxy_sps 同款，用于 SPS 层测试）。"""

    def __init__(self):
        self.bits = []

    def u(self, v, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((v >> i) & 1)

    def ue(self, v):
        c = v + 1
        n = c.bit_length() - 1
        self.u(0, n)
        self.u(c, n + 1)

    def bytes(self):
        b = self.bits + [1]
        while len(b) % 8:
            b.append(0)
        out = bytearray()
        for i in range(0, len(b), 8):
            x = 0
            for bit in b[i:i + 8]:
                x = x * 2 + bit
            out.append(x)
        return bytes(out)


def make_sps_annexb(w, h):
    bw = _BW()
    bw.u(66, 8)
    bw.u(0, 8)
    bw.u(30, 8)
    bw.ue(0)
    bw.ue(0)
    bw.ue(0)
    bw.ue(0)
    bw.ue(1)
    bw.u(0, 1)
    bw.ue(w // 16 - 1)
    mh = (h + 15) // 16
    bw.ue(mh - 1)
    bw.u(1, 1)
    bw.u(1, 1)
    crop = mh * 16 - h
    if crop:
        bw.u(1, 1)
        bw.ue(0)
        bw.ue(0)
        bw.ue(0)
        bw.ue(crop // 2)
    else:
        bw.u(0, 1)
    bw.u(0, 1)
    return b"\x00\x00\x00\x01\x67" + bw.bytes()


class AdblockPlaylistTests(unittest.TestCase):
    def setUp(self):
        adfilter.reset_sps_cache()
        self._cfg = patch.object(
            cfgmod.config, "get",
            return_value={"ad_filter_enabled": True,
                          "ad_filter_dynamic": True,
                          "ad_filter_sps": True})
        self._cfg.start()
        self._rules = patch.object(adfilter, "_load_rules",
                                   return_value=[])
        self._rules.start()
        self.addCleanup(self._cfg.stop)
        self.addCleanup(self._rules.stop)
        self.addCleanup(adfilter.reset_sps_cache)

    def _main(self, n=10, prefix="main", dur=10.0):
        return [(dur, f"https://cdn.example.com/{prefix}{i}.ts")
                for i in range(n)]

    def _ad(self, n=3, prefix="ad", dur=10.0, base="https://cdn.example.com"):
        return [(dur, f"{base}/{prefix}{i}.ts") for i in range(n)]

    # ---- E1：MEDIA-SEQUENCE 修正 --------------------------------------
    def test_e1_media_sequence_bumped_after_leading_ad_removed(self):
        ad = [(10.0, f"https://cdn.example.com/ads/a{i}.ts")
              for i in range(3)]
        text = _pl([ad, self._main(10)], media_seq=100)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(_media_seq(new), 103)
        self.assertEqual(len(_segs(new)), 10)
        self.assertAlmostEqual(_dur_sum(new), 100.0)

    def test_e1_media_sequence_unchanged_for_midroll_removal(self):
        ad = [(10.0, f"https://cdn.example.com/ads/a{i}.ts")
              for i in range(3)]
        text = _pl([self._main(5, "m0"), ad, self._main(5, "m1")],
                   media_seq=7)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(_media_seq(new), 7)
        self.assertEqual(len(_segs(new)), 10)

    def test_e1_media_sequence_inserted_when_absent_and_leading_removed(self):
        ad = [(10.0, f"https://cdn.example.com/ads/a{i}.ts")
              for i in range(3)]
        text = _pl([ad, self._main(10)], media_seq=None)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(_media_seq(new), 3)

    def test_e1_discontinuity_sequence_adjusted(self):
        ad = [(10.0, f"https://cdn.example.com/ads/a{i}.ts")
              for i in range(3)]
        # 原表：DISCONTINUITY-SEQUENCE:2（窗外已滑出 2 个间断标记）。
        # 剥离前导广告块后，第一分片变为原主块首片，其间断序号 = 2（窗外）
        # + 1（窗内保留的广告/主块之间的间断标记）= 3；MEDIA-SEQUENCE 同步 +3。
        text = _pl([ad, self._main(10)], media_seq=50, disc_seq=2)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(_media_seq(new), 53)
        self.assertEqual(_disc_seq(new), 3)

    def test_e1_sequence_fix_skipped_when_regex_rewrites_urls(self):
        # 订阅规则把分片 URL 改写成原文没有的串：对齐失败时不碰序号标签
        ad = [(10.0, f"https://cdn.example.com/ads/a{i}.ts")
              for i in range(3)]
        main = [(10.0, f"https://cdn.example.com/seg-main-{i}.ts")
                for i in range(10)]
        text = _pl([ad, main], media_seq=100)
        with patch.object(adfilter, "_rules_for",
                          return_value=[r"-main-"]):
            new, removed = adfilter.filter_hls(
                text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(_media_seq(new), 100)  # 对齐失败：不修正

    def test_e1_normal_playlist_untouched(self):
        text = _pl([self._main(10)], media_seq=0)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 0)
        # 内容原样（启发式层既有行为：末尾换行不保留，只比内容）
        self.assertEqual(new.rstrip("\n"), text.rstrip("\n"))
        self.assertEqual(_media_seq(new), 0)

    def test_e1_live_playlist_leading_ad_bumps_sequence(self):
        # 直播表（无 ENDLIST）：前导广告剥离后序号同样要跟上
        ad = [(6.0, f"https://cdn.example.com/ads/a{i}.ts")
              for i in range(2)]
        text = _pl([ad, self._main(10)], media_seq=500, endlist=False)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 2)
        self.assertEqual(_media_seq(new), 502)

    # ---- E2：DATERANGE 收紧 --------------------------------------------
    def test_e2_plain_daterange_kept(self):
        # DATERANGE 前后各 6 片：基线会把 DATERANGE 当广告区间吞掉后 6 片
        #（不触发“删光回退”护栏）；收紧后 12 片全保留。
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 "#EXT-X-TARGETDURATION:10", "#EXT-X-MEDIA-SEQUENCE:0"]
        for i in range(6):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/m{i}.ts"]
        lines.append("#EXT-X-DATERANGE:ID=\"chapter1\","
                     "START-DATE=\"2026-01-01T00:00:00Z\",DURATION=60")
        for i in range(6, 12):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/m{i}.ts"]
        lines.append("#EXT-X-ENDLIST")
        text = "\n".join(lines) + "\n"
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 0)
        self.assertEqual(len(_segs(new)), 12)
        self.assertIn("#EXT-X-DATERANGE", new)

    def test_e2_scte35_out_daterange_starts_cue(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 "#EXT-X-TARGETDURATION:10", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-DATERANGE:ID=\"ad1\",START-DATE=\"2026-01-01T00:00:00Z\","
                 "DURATION=30,SCTE35-OUT=0xFC002F000000000000FF00000A0500",
                 "#EXT-X-DISCONTINUITY"]
        for i in range(3):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/ads/a{i}.ts"]
        lines += ["#EXT-X-DISCONTINUITY", "#EXT-X-CUE-IN"]
        for i in range(6):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/m{i}.ts"]
        lines.append("#EXT-X-ENDLIST")
        text = "\n".join(lines) + "\n"
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(len(_segs(new)), 6)
        self.assertEqual(_media_seq(new), 3)

    def test_e2_scte35_in_daterange_ends_cue(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 "#EXT-X-TARGETDURATION:10", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-CUE-OUT:30", "#EXT-X-DISCONTINUITY"]
        for i in range(3):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/ads/a{i}.ts"]
        lines += ["#EXT-X-DISCONTINUITY",
                  "#EXT-X-DATERANGE:ID=\"ad1\",SCTE35-IN=0xFC002F000000000000FF00000A0500"]
        for i in range(6):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/m{i}.ts"]
        lines.append("#EXT-X-ENDLIST")
        text = "\n".join(lines) + "\n"
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 3)
        self.assertEqual(len(_segs(new)), 6)

    def test_e2_interstitial_class_daterange_starts_cue(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 "#EXT-X-TARGETDURATION:10", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-DATERANGE:ID=\"it1\","
                 "CLASS=\"com.apple.hls.interstitial\",DURATION=20",
                 "#EXT-X-DISCONTINUITY"]
        for i in range(2):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/ads/a{i}.ts"]
        lines += ["#EXT-X-DISCONTINUITY", "#EXT-X-CUE-IN"]
        for i in range(6):
            lines += [f"#EXTINF:10.0,", f"https://cdn.example.com/m{i}.ts"]
        lines.append("#EXT-X-ENDLIST")
        text = "\n".join(lines) + "\n"
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 2)
        self.assertEqual(len(_segs(new)), 6)

    # ---- E3：广告域名拦截 ----------------------------------------------
    def test_e3_ad_host_segment_dropped(self):
        # 路径无任何关键字，仅 host 是广告投放域
        segs = ([(10.0, "https://googleads.g.doubleclick.net/video/s0.ts"),
                 (10.0, "https://googleads.g.doubleclick.net/video/s1.ts")]
                + self._main(10))
        text = _pl([segs], media_seq=0)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 2)
        self.assertEqual(len(_segs(new)), 10)
        self.assertEqual(_media_seq(new), 2)

    def test_e3_ad_host_block_scores_in_dynamic_detection(self):
        # 动态检测直测：广告块 4 片、时长参差（45/55/48/52s，总 200s，
        # 不命中“广告典型时长”与“高度同构”），基线评分只有位置+1、
        # 域名切换+2 = 3 分 < 4 不删；广告 host 信号 +3 后 6 分才删。
        ad = [(d, f"https://ads.pubmatic.com/v/a{i}.ts")
              for i, d in enumerate((45.0, 55.0, 48.0, 52.0))]
        text = _pl([self._main(10), ad], media_seq=0)
        new, removed = adfilter._detect_ad_blocks(text)
        self.assertEqual(removed, 4)
        self.assertEqual(len(_segs(new)), 10)

    def test_e3_ad_host_block_end_to_end(self):
        # 端到端：同一广告块经启发式 host 检查即剥离
        ad = [(d, f"https://ads.pubmatic.com/v/a{i}.ts")
              for i, d in enumerate((45.0, 55.0, 48.0, 52.0))]
        text = _pl([self._main(10), ad], media_seq=0)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 4)
        self.assertEqual(len(_segs(new)), 10)

    def test_e3_lookalike_hosts_not_dropped(self):
        segs = ([(10.0, "https://ads.example.com/v/s0.ts"),
                 (10.0, "https://mydoubleclick.net/v/s1.ts"),
                 (10.0, "https://doubleclick.net.evil.com/v/s2.ts")]
                + self._main(10))
        text = _pl([segs], media_seq=0)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 0)
        self.assertEqual(len(_segs(new)), 13)

    def test_e3_ad_host_midroll_keeps_sequence(self):
        ad = [(10.0, f"https://adsrvr.org/v/a{i}.ts") for i in range(2)]
        text = _pl([self._main(5, "m0"), ad, self._main(5, "m1")],
                   media_seq=20)
        new, removed = adfilter.filter_hls(
            text, "https://media.test/list.m3u8")
        self.assertEqual(removed, 2)
        self.assertEqual(_media_seq(new), 20)


class AdblockSpsSequenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        adfilter.reset_sps_cache()
        self._cfg = patch.object(
            cfgmod.config, "get",
            return_value={"ad_filter_enabled": True,
                          "ad_filter_dynamic": True,
                          "ad_filter_sps": True})
        self._cfg.start()
        self._rules = patch.object(adfilter, "_load_rules",
                                   return_value=[])
        self._rules.start()
        self.addCleanup(self._cfg.stop)
        self.addCleanup(self._rules.stop)
        self.addCleanup(adfilter.reset_sps_cache)

    async def _fetch_head(self, url, limit, timeout):
        if "/ad" in url or "ad0_" in url:
            return make_sps_annexb(1280, 720)
        return make_sps_annexb(1920, 1080)

    async def test_sps_leading_ad_block_bumps_media_sequence(self):
        blocks = [("ad0_", 3, 10.0), ("main0_", 10, 10.0),
                  ("main1_", 10, 10.0), ("main2_", 10, 10.0)]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
                 "#EXT-X-MEDIA-SEQUENCE:0"]
        for bi, (prefix, n, dur) in enumerate(blocks):
            if bi:
                lines.append("#EXT-X-DISCONTINUITY")
            for i in range(n):
                lines.append(f"#EXTINF:{dur},")
                lines.append(f"{prefix}{i}.ts")
        lines.append("#EXT-X-ENDLIST")
        text = "\n".join(lines) + "\n"
        new_text, removed = await adfilter.filter_hls_async(
            text, "https://media.test/list.m3u8", self._fetch_head)
        self.assertEqual(removed, 3)
        self.assertNotIn("ad0_0.ts", new_text)
        self.assertEqual(_media_seq(new_text), 3)
        self.assertAlmostEqual(_dur_sum(new_text), 300.0)


if __name__ == "__main__":
    unittest.main()
