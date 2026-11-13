"""B 站库进入链路回归：点进库不能提示「此媒体不存在」。

三个真实根因：
  1. B 站库 guid 没有稳定库键 → 子账号鉴权判定失败 → -6「此媒体库不存在」；
  2. B 站影片是 Movie 形态 → season/list、episode/list 直接透传官方上游
     → 502 upstream ConnectError → App 显示「此媒体不存在」；
  3. detail["episodes"] 是 Episode dataclass 不是 dict → 用 .get() 崩掉，
     分P 永远取不到（被 except 吞成 0 集）。

锁住：库键映射齐全、season/episode 走本地分支、Episode 按属性取值、
play/info 认 media_guid/video_guid。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class BilibiliLibKeyTests(unittest.TestCase):
    """库 guid ↔ 稳定库键必须双向可映射。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import app as appmod
        import vlibs as vlibsmod
        self.app = appmod
        self.vlibs = vlibsmod

    def test_guid_constant_matches_browser(self):
        self.assertEqual(self.app.BILI_LIB_GUID,
                         self.vlibs._BILIBILI_GUID)

    def test_key_of_guid(self):
        self.assertEqual(
            self.vlibs.key_of_guid(self.app.BILI_LIB_GUID),
            self.vlibs.K_BILIBILI)

    def test_lib_guid_for_key(self):
        self.assertEqual(
            self.app._lib_guid_for_key(self.vlibs.K_BILIBILI),
            self.app.BILI_LIB_GUID)

    def test_vkey_for_guid(self):
        self.assertEqual(
            self.app._vkey_for_guid(self.app.BILI_LIB_GUID),
            self.vlibs.K_BILIBILI)

    def test_normalize_key_accepts_bilibili(self):
        self.assertEqual(
            self.vlibs.normalize_key(self.vlibs.K_BILIBILI),
            self.vlibs.K_BILIBILI)

    def test_in_virtual_lib_set(self):
        self.assertIn(self.app.BILI_LIB_GUID,
                      self.app._all_virtual_lib_guids())


class BilibiliBrowseRouteTests(unittest.TestCase):
    """item_list / season_list / episode_list 必须走本地 B 站分支。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import app as appmod
        self.app = appmod
        self.src = (PROXY / "app.py").read_text(encoding="utf-8")

    def test_item_list_has_bilibili_branch(self):
        self.assertIn("bili_browser.listing(", self.src)

    def test_season_list_has_bilibili_branch(self):
        self.assertIn("_bili_season_rows(", self.src)

    def test_episode_list_has_bilibili_branch(self):
        self.assertIn("_bili_episode_rows(", self.src)

    def test_bilibili_branch_comes_before_upstream_fallback(self):
        """B 站分支必须在 forward_upstream 之前，否则又会透传上游 502。"""
        i_ep = self.src.index("async def episode_list_route")
        seg = self.src[i_ep:i_ep + 4000]
        self.assertLess(seg.index("_bili_episode_rows("),
                        seg.index("forward_upstream(request)"))

    def test_play_info_accepts_media_guid(self):
        self.assertIn('"media_guid", "video_guid"', self.src)

    def test_episode_defs_reads_dataclass_attrs(self):
        """Episode 是 dataclass，必须用属性取值，不能 .get()。"""
        src = (PROXY / "app.py").read_text(encoding="utf-8")
        self.assertIn('getattr(e, "title"', src)
        self.assertIn('getattr(e, "url"', src)


class BilibiliEpisodeTests(unittest.TestCase):
    """分P 取值与写回 store。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import app as appmod
        self.app = appmod

    def test_episode_rows_use_store_consistent_guid(self):
        """集 guid 必须用 episode_guid(og, "", i)，与 store 索引一致。"""
        import inject
        import store as st
        og = "b" * 32
        self.assertEqual(inject.episode_guid(og, "", 0),
                         st.episode_guid(og, "", 0))

    def test_episode_rows_carry_media_guid(self):
        src = (PROXY / "app.py").read_text(encoding="utf-8")
        self.assertIn('ep_media_guid(ep_guid, "B站自动高清")', src)


if __name__ == "__main__":
    unittest.main()
