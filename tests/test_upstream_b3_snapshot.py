"""B3+B4：固定线路 play/info 快照回退 + 显式集序号受尊重。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
from store import episode_guid, season_guid


def _brief(og: str) -> dict:
    return {"guid": og, "title": "测试剧", "type_name": "电视剧",
            "poster": "", "duration": 1800,
            "lines": {"l1::m3u8": [
                {"title": f"第{i + 1}集", "url": f"https://x/{i}.m3u8",
                 "direct": True} for i in range(3)]}}


def _ep(og: str, index: int) -> dict:
    return {"ep_guid": episode_guid(og, "", index), "online_guid": og,
            "index": index, "episode_number": index + 1, "season": 1,
            "season_guid": season_guid(og, 1), "title": "测试剧",
            "ep_title": f"第{index + 1}集", "duration": 1800,
            "default_line": "l1::m3u8", "line": "l1::m3u8"}


class VodSnapshotTests(unittest.TestCase):
    def test_snapshot_fallback_rebuilds_episode_with_explicit_index(self):
        og = "og-snap-1"
        index = 1
        eg = episode_guid(og, "", index)
        rec = {"item_guid": eg, "ts": 706, "duration": 0,
               "snapshot": {"guid": og, "title": "测试剧",
                            "type_name": "电视剧",
                            "lines": {"l1::m3u8": [
                                {"title": "第2集",
                                 "url": "https://x/1.m3u8", "direct": True}]},
                            "_ep_index": 1, "_ep_guid": eg}}
        client = clientmod.parse_user_agent("")
        with patch.object(appmod.history, "get",
                          side_effect=lambda g: rec if g in (eg, og) else None), \
                patch.object(appmod.resume_diagmod, "note_info",
                             return_value=None):
            resp = appmod._vod_play_info_from_snapshot(
                og, "l1::m3u8", index, client)
        self.assertIsNotNone(resp)
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["type"], "Episode")
        self.assertEqual(data["guid"], eg)
        self.assertEqual(data["ts"], 706)
        # 续播兜底：快照 duration 恒 0 时 item.duration=ts+60，不再钳成 0
        self.assertEqual(data["item"]["duration"], 766)
        self.assertEqual(data["item"]["ts"], 706)
        self.assertEqual(data["item"]["watched_ts"], 706)
        self.assertEqual(data["item"]["parent_guid"],
                         appmod._line_season_guid(og, "l1::m3u8", 1))

    def test_snapshot_fallback_returns_none_without_record(self):
        client = clientmod.parse_user_agent("")
        with patch.object(appmod.history, "get", return_value=None), \
                patch.object(appmod.history, "latest_episode_record",
                             return_value=None):
            self.assertIsNone(appmod._vod_play_info_from_snapshot(
                "og-none", "l1::m3u8", 0, client))

    def test_explicit_index_is_not_overridden_by_latest_record(self):
        # 请求第 3 集（index=2），最近观看记录在第 1 集：不得退回第 1 集
        og = "og-idx-1"
        line = "l1::m3u8"
        brief = _brief(og)
        client = clientmod.parse_user_agent("")
        latest = {"snapshot": {"_ep_index": 0}, "ts": 100}
        with patch.object(appmod.store, "get_title_by_guid",
                          return_value=brief), \
                patch.object(appmod.store, "resolve_episode",
                             return_value={"url": "https://x/2.m3u8"}), \
                patch.object(appmod.store, "get_episode",
                             side_effect=lambda g: _ep(og, 2)), \
                patch.object(appmod.store, "list_episodes",
                             return_value=[0, 1, 2]), \
                patch.object(appmod.history, "latest_episode_record",
                             return_value=latest), \
                patch.object(appmod.history, "position", return_value=50), \
                patch.object(appmod.resume_diagmod, "note_info",
                             return_value=None):
            # 直接调 _vod_play_info：guid 需符合 _parse_line_item_guid
            parsed_guid = appmod._line_item_guid(og, line, 2)
            resp = appmod._vod_play_info(parsed_guid, client)
        self.assertIsNotNone(resp)
        data = json.loads(resp.body)["data"]
        self.assertEqual(data["guid"], episode_guid(og, "", 2))


if __name__ == "__main__":
    unittest.main()
