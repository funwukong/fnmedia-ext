"""B13：updater 预发布检测——拉 Release 列表（含 prerelease）按版本号取最高，
同版本优先正式版；不再只用会跳过 prerelease 的 /releases/latest。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import updater


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload, seen):
        self._payload = payload
        self._seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        self._seen.append(url)
        return _Resp(200, self._payload)


class VersionKeyTests(unittest.TestCase):
    def test_version_key_orders_major_minor_patch(self):
        # _version_key 只取前 3 个数字段：beta.3/beta.4 同键 (1,0,1)，
        # 同版本内的先后靠 GitHub 列表 newest-first 的原顺序（稳定排序）决定。
        self.assertEqual(updater._version_key("v1.0.1-beta.3"),
                         updater._version_key("v1.0.1-beta.4"))
        self.assertLess(updater._version_key("v1.0.0"),
                        updater._version_key("v1.0.1-beta.1"))
        self.assertEqual(updater._version_key("v1.2.0"), (1, 2, 0))


class FetchReleaseTests(unittest.TestCase):
    def _fetch(self, releases):
        seen = []
        with patch.object(updater, "_proxy_candidates", return_value=[None]), \
                patch.object(updater.httpx, "Client",
                             side_effect=lambda **kw: _Client(releases, seen)):
            return updater._fetch_release_sync(), seen

    def test_picks_highest_including_prerelease(self):
        # GitHub 列表按 newest-first 返回；同版本键稳定排序保留原顺序。
        releases = [
            {"tag_name": "v1.0.1-beta.4", "prerelease": True, "draft": False},
            {"tag_name": "v1.0.1-beta.2", "prerelease": True, "draft": False},
            {"tag_name": "v1.0.0", "prerelease": False, "draft": False},
        ]
        got, seen = self._fetch(releases)
        self.assertEqual(got["tag_name"], "v1.0.1-beta.4")
        self.assertIn("releases?per_page=20", seen[0])
        self.assertNotIn("/latest", seen[0])

    def test_same_version_prefers_stable_over_prerelease(self):
        releases = [
            {"tag_name": "v1.0.1", "prerelease": True, "draft": False},
            {"tag_name": "v1.0.1", "prerelease": False, "draft": False},
        ]
        got, _ = self._fetch(releases)
        self.assertFalse(got["prerelease"])

    def test_drafts_are_filtered(self):
        releases = [
            {"tag_name": "v9.9.9", "prerelease": False, "draft": True},
            {"tag_name": "v1.0.1-beta.4", "prerelease": True, "draft": False},
        ]
        got, _ = self._fetch(releases)
        self.assertEqual(got["tag_name"], "v1.0.1-beta.4")

    def test_empty_list_raises(self):
        with self.assertRaises(RuntimeError):
            self._fetch([])


if __name__ == "__main__":
    unittest.main()
