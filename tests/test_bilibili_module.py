"""B 站独立模块的真实链路回归（用 Mock 替掉网络，只验接线与契约）。

锁住的故障：
  1. bilibili://video/BV…?p=N 的分P号被 get_bvid 截掉 → 永远播第 1 集；
  2. DASH 双轨当播放源 → 飞牛播不了（必须优先合一 MP4）；
  3. B 站库没进 _all_virtual_lib_guids → 被上游库列表覆盖；
  4. popular 是 async，被塞进 to_thread → 返回 coroutine，列表崩溃；
  5. 搜索返回的 duration 是 "mm:ss" → int() 抛错，整批结果丢失。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class _FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.content = b""
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class BilibiliPlayContractTests(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import bilibili_play
        self.bp = bilibili_play

    def test_ref_page_parses_query(self):
        self.assertEqual(self.bp.ref_page("bilibili://video/BV1ex411J7GE?p=5"), 5)
        self.assertEqual(self.bp.ref_page("bilibili://video/BV1ex411J7GE"), 1)

    def test_get_bvid_strips_query(self):
        self.assertEqual(
            self.bp.get_bvid("bilibili://video/BV1GJ411x7h7?p=3"),
            "BV1GJ411x7h7")

    def test_play_uses_url_page_not_default(self):
        """分P号必须在 get_bvid 截断之前取，否则永远第 1 集。"""
        seen = {}

        def fake_page_cid(ref, page):
            seen["ref"] = ref
            seen["page"] = page
            return 1000 + page, 32

        def fake_durl(ref, cid, qn):
            return {"type": "mp4", "video": "https://x/v.mp4", "audio": "",
                    "qn": qn, "quality": "720P", "size": 1,
                    "headers": {}}

        orig_cid = self.bp.page_cid
        orig_durl = self.bp._durl_playurl
        self.bp.page_cid = fake_page_cid
        self.bp._durl_playurl = fake_durl
        try:
            r = self.bp.play("bilibili://video/BV1ex411J7GE?p=5")
        finally:
            self.bp.page_cid = orig_cid
            self.bp._durl_playurl = orig_durl
        self.assertEqual(seen["page"], 5)
        self.assertEqual(r["page"], 5)
        self.assertEqual(r["cid"], 1005)

    def test_explicit_page_wins_over_query(self):
        seen = {}

        def fake_page_cid(ref, page):
            seen["page"] = page
            return 1, 10

        def fake_durl(ref, cid, qn):
            return {"type": "mp4", "video": "https://x/v.mp4", "audio": "",
                    "qn": qn, "quality": "720P", "size": 1, "headers": {}}

        orig_cid = self.bp.page_cid
        orig_durl = self.bp._durl_playurl
        self.bp.page_cid = fake_page_cid
        self.bp._durl_playurl = fake_durl
        try:
            self.bp.play("bilibili://video/BV1ex411J7GE?p=2", 7)
        finally:
            self.bp.page_cid = orig_cid
            self.bp._durl_playurl = orig_durl
        self.assertEqual(seen["page"], 7)

    def test_progressive_mp4_preferred_over_dash(self):
        src = (PROXY / "bilibili_play.py").read_text(encoding="utf-8")
        # 飞牛不能播 DASH 音视频分离：合一 MP4 必须先行
        self.assertIn('"fnval": 1', src)
        self.assertIn("allow_dash", src)

    def test_quality_order_desc(self):
        self.assertEqual(self.bp._QN_PRIORITY, [120, 116, 112, 80, 74, 64])


class BilibiliSiteTests(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import bilibili_site
        self.bs = bilibili_site

    def test_map_item_handles_mm_ss_duration(self):
        raw = {"bvid": "BV1GJ411x7h7", "title": "标题", "author": "UP",
               "duration": "362:0", "pic": ""}
        t = self.bs._map_item(raw)
        self.assertIsNotNone(t)
        self.assertEqual(t.remarks, "362:0")

    def test_map_item_handles_int_duration(self):
        raw = {"bvid": "BV1GJ411x7h7", "title": "标题", "duration": 213}
        t = self.bs._map_item(raw)
        self.assertEqual(t.remarks, "3:33")

    def test_map_item_single_line(self):
        raw = {"bvid": "BV1GJ411x7h7", "title": "标题", "duration": 0}
        t = self.bs._map_item(raw)
        self.assertEqual(list(t.sources), [self.bs.LINE_NAME])
        self.assertEqual(len(t.sources[self.bs.LINE_NAME]), 1)

    def test_vod_id_scheme(self):
        raw = {"bvid": "BV1ex411J7GE", "title": "x", "duration": 0}
        self.assertTrue(self.bs._map_item(raw).vod_id
                        .startswith("bilibili://video/"))


class BilibiliWiringTests(unittest.TestCase):
    def test_bili_lib_in_virtual_lib_set(self):
        src = (PROXY / "lib_inject.py").read_text(encoding="utf-8")
        self.assertIn("BILI_LIB_GUID", src)

    def test_browser_does_not_use_to_thread_for_async_popular(self):
        src = (PROXY / "bilibili_browser.py").read_text(encoding="utf-8")
        self.assertNotIn("to_thread(bilibili_site.popular", src)

    def test_play_resolve_branch(self):
        src = (PROXY / "hls_race.py").read_text(encoding="utf-8")
        self.assertIn("bilibili_play.play", src)

    def test_guess_wrapper_mp4(self):
        src = (PROXY / "app.py").read_text(encoding="utf-8")
        self.assertIn('startswith("bilibili")', src)


if __name__ == "__main__":
    unittest.main()
