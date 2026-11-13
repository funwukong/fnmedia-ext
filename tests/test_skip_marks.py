"""跳过片头/片尾原生 play_config：逐标题标记 + 播放器写回路由。

移植自上游 yujian0815/fnmedia-ext tests/test_skip_marks.py
（SkipMarkTests 与 SkipConfigRouteTests 原样移植，只改 fork 差异处），
另加 fork 扩展的测试（PlayConfigForTests、SkipMarkGuidTests、
SkipConfigRouteTests 里的 HTTP 往返与删除）。

Fork 差异：
1. 上游路由测试直接 await 处理函数；fork 的处理函数签名相同，
   同样直接 await（不走 TestClient，避免跨线程 ContextVar 分区问题）。
2. 上游“line_season == season_guid”的归一断言在 fork 由
   _skip_mark_guid 实现（线路季 guid 直接按季号算季 guid，不依赖
   store 里还有没有这部剧），断言与上游一致。
3. 新增 PlayConfigForTests：play_config 与服务端剪裁共存、不双重跳过
   （skip_marks.play_config_for 的 fork 扩展）：无标记时恒为 0（全局
   设置已由服务端剪裁执行，不重复报给播放器）；有标记时已开剪裁的
   方向归 0，未开剪裁的方向按标记值给；全局关时（默认）标记原样生效。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import history
import inject
import skip_marks


class SkipMarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "skip_marks.json"
        item = patch.object(skip_marks, "_STATE", self.state)
        item.start()
        self.addCleanup(item.stop)
        self.tokens = history.set_current_user("user-1")

    def tearDown(self):
        history.reset_current_user(self.tokens)

    def test_native_config_set_get_and_disable(self):
        self.assertEqual(skip_marks.play_config("season-1"),
                         {"guid": "season-1", "skip_opening": 0,
                          "skip_ending": 0})
        skip_marks.set_mark("season-1", {"skip_opening": 90,
                                         "skip_ending": 120})
        self.assertEqual(skip_marks.play_config("season-1"),
                         {"guid": "season-1", "skip_opening": 90,
                          "skip_ending": 120})
        skip_marks.set_mark("season-1", {"enabled": False})
        self.assertEqual(skip_marks.play_config("season-1"),
                         {"guid": "season-1", "skip_opening": 0,
                          "skip_ending": 0})
        self.assertEqual(skip_marks.get("season-1")["skip_opening"], 90)

    def test_libretv_segment_validation_and_resolution(self):
        skip_marks.set_mark("season-1", {
            "skip_opening": 85,
            "segments": [
                {"type": "opening", "start": 0, "end": 85},
                {"type": "middle", "start": 300, "end": 330},
                {"type": "ending", "start": 1700, "end": -1},
                {"type": "middle", "start": 320, "end": 340},  # 过近，丢弃
                {"type": "middle", "start": -1, "end": 20},   # 非法，丢弃
            ]})
        segments = skip_marks.resolve_segments("season-1", 1800)
        self.assertEqual([(s["type"], s["start"], s["end"])
                          for s in segments],
                         [("opening", 0, 85), ("middle", 300, 330),
                          ("ending", 1700, 1800)])
        self.assertEqual(skip_marks.find_active_segment(
            "season-1", 310, 1800)["type"], "middle")
        self.assertIsNone(skip_marks.find_active_segment(
            "season-1", 1000, 1800))

    def test_custom_hls_skip_is_opt_in_and_middle_only(self):
        self.assertEqual(skip_marks.hls_ranges("season-1"), [])
        skip_marks.set_mark("season-1", {"custom_hls_skip": True,
                                         "segments": [
                                             {"type": "opening", "start": 0,
                                              "end": 80},
                                             {"type": "middle", "start": 300,
                                              "end": 330},
                                             {"type": "ending", "start": 1700,
                                              "end": -1},
                                         ]})
        self.assertEqual(skip_marks.hls_ranges("season-1"), [(300, 330)])

    def test_injected_play_info_uses_native_config(self):
        skip_marks.set_mark("season-1", {"skip_opening": 85,
                                         "skip_ending": 100})
        movie = inject.play_info({"guid": "movie-1", "title": "影片",
                                  "type_name": "电影", "lines": {}})
        self.assertEqual(movie["play_config"],
                         {"guid": "movie-1", "skip_opening": 0,
                          "skip_ending": 0})
        ep = {"ep_guid": "ep-1", "online_guid": "show-1",
              "season_guid": "season-1", "season": 1, "index": 0,
              "episode_number": 1, "ep_title": "第1集"}
        info = inject.episode_play_info(ep)
        self.assertEqual(info["play_config"],
                         {"guid": "season-1", "skip_opening": 85,
                          "skip_ending": 100})
        # 原生自动连播依赖的 Episode 身份必须完整且稳定，无需新增 next 路由。
        self.assertEqual(info["type"], "Episode")
        self.assertEqual(info["parent_guid"], "season-1")
        self.assertEqual(info["item"]["parent_guid"], "season-1")
        self.assertEqual(info["item"]["episode_number"], 1)
        self.assertEqual(info["item"]["guid"], "ep-1")


class PlayConfigForTests(unittest.TestCase):
    """play_config_for：逐标题标记 + 服务端剪裁共存，不双重跳过。

    服务端剪裁（hls_race._trim_playlist）按全局配置剪清单；同一秒数
    若再经 play_config 交播放器跳一次，就会跳两遍。故无标记时恒 0，
    有标记时已开剪裁的方向归 0。
    """

    class _CfgStub:
        def __init__(self, values):
            self._values = dict(values)

        def get(self):
            return dict(self._values)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        item = patch.object(skip_marks, "_STATE",
                            Path(self.temp.name) / "skip_marks.json")
        item.start()
        self.addCleanup(item.stop)
        self.tokens = history.set_current_user("user-1")

    def tearDown(self):
        history.reset_current_user(self.tokens)

    def _with_global(self, values):
        return patch.object(appmod, "config", self._CfgStub(values))

    def test_no_mark_is_zero_when_global_on(self):
        # 无标记：全局剪裁已在服务端执行，play_config 不再重复报秒数。
        with self._with_global({"skip_intro_enabled": True,
                                "skip_intro_seconds": 90,
                                "skip_outro_enabled": True,
                                "skip_outro_seconds": 120}):
            self.assertEqual(
                skip_marks.play_config_for("season-1", is_tv=True),
                {"guid": "season-1", "skip_opening": 0, "skip_ending": 0})

    def test_no_mark_tv_global_off_is_zero(self):
        with self._with_global({"skip_intro_enabled": False,
                                "skip_intro_seconds": 90,
                                "skip_outro_enabled": False,
                                "skip_outro_seconds": 120}):
            self.assertEqual(
                skip_marks.play_config_for("season-1", is_tv=True),
                {"guid": "season-1", "skip_opening": 0, "skip_ending": 0})

    def test_no_mark_movie_is_zero_even_when_global_on(self):
        with self._with_global({"skip_intro_enabled": True,
                                "skip_intro_seconds": 90,
                                "skip_outro_enabled": True,
                                "skip_outro_seconds": 120}):
            self.assertEqual(
                skip_marks.play_config_for("movie-1", is_tv=False),
                {"guid": "movie-1", "skip_opening": 0, "skip_ending": 0})

    def test_mark_zeroed_where_global_trims(self):
        # 全局片头/片尾剪裁都开着：标记的两个分量都归 0，避免跳两遍。
        skip_marks.set_mark("season-1", {"skip_opening": 45,
                                         "skip_ending": 30})
        with self._with_global({"skip_intro_enabled": True,
                                "skip_intro_seconds": 90,
                                "skip_outro_enabled": True,
                                "skip_outro_seconds": 120}):
            self.assertEqual(
                skip_marks.play_config_for("season-1", is_tv=True),
                {"guid": "season-1", "skip_opening": 0, "skip_ending": 0})

    def test_mark_partial_survives_untrimmed_field(self):
        # 只开片头剪裁：标记的片头归 0，片尾仍按标记给。
        skip_marks.set_mark("season-1", {"skip_opening": 45,
                                         "skip_ending": 30})
        with self._with_global({"skip_intro_enabled": True,
                                "skip_intro_seconds": 90,
                                "skip_outro_enabled": False,
                                "skip_outro_seconds": 0}):
            self.assertEqual(
                skip_marks.play_config_for("season-1", is_tv=True),
                {"guid": "season-1", "skip_opening": 0,
                 "skip_ending": 30})

    def test_mark_applies_verbatim_when_global_off(self):
        # 全局关时（默认）：标记原样生效。
        skip_marks.set_mark("season-1", {"skip_opening": 45,
                                         "skip_ending": 30})
        with self._with_global({"skip_intro_enabled": False,
                                "skip_intro_seconds": 0,
                                "skip_outro_enabled": False,
                                "skip_outro_seconds": 0}):
            self.assertEqual(
                skip_marks.play_config_for("season-1", is_tv=True),
                {"guid": "season-1", "skip_opening": 45,
                 "skip_ending": 30})

    def test_explicit_disable_mark_beats_global(self):
        skip_marks.set_mark("season-1", {"skip_opening": 0,
                                         "skip_ending": 0})
        with self._with_global({"skip_intro_enabled": True,
                                "skip_intro_seconds": 90,
                                "skip_outro_enabled": True,
                                "skip_outro_seconds": 120}):
            self.assertEqual(
                skip_marks.play_config_for("season-1", is_tv=True),
                {"guid": "season-1", "skip_opening": 0, "skip_ending": 0})


class SkipMarkGuidTests(unittest.TestCase):
    """_skip_mark_guid：写读两端统一归一到季 guid（剧集）/标题 guid（电影）。"""

    class _StoreStub:
        def get_episode(self, guid):
            if guid == "ep-hash-1":
                return {"season_guid": "season-hash-1",
                        "online_guid": "og-tv"}
            return None

        def get_title_by_guid(self, guid):
            if guid == "og-tv":
                return {"guid": "og-tv", "type_name": "电视剧",
                        "lines": {"line-a": [{"title": "第1集"}]}}
            if guid == "og-movie":
                return {"guid": "og-movie", "type_name": "电影"}
            return None

        def resolve_media_guid(self, guid):
            return None

    def setUp(self):
        item = patch.object(appmod, "store", self._StoreStub())
        item.start()
        self.addCleanup(item.stop)

    def test_line_season_guid_maps_to_season(self):
        from store import season_guid
        line_season = appmod._line_season_guid("og-tv", "line-a", 1)
        self.assertEqual(appmod._skip_mark_guid(line_season),
                         season_guid("og-tv", 1))

    def test_episode_guid_maps_to_season(self):
        self.assertEqual(appmod._skip_mark_guid("ep-hash-1"),
                         "season-hash-1")

    def test_tv_title_maps_to_season_guid(self):
        from store import season_guid
        self.assertEqual(appmod._skip_mark_guid("og-tv"),
                         season_guid("og-tv"))

    def test_movie_title_stays(self):
        self.assertEqual(appmod._skip_mark_guid("og-movie"), "og-movie")

    def test_unknown_and_empty_pass_through(self):
        self.assertEqual(appmod._skip_mark_guid("whatever"), "whatever")
        self.assertEqual(appmod._skip_mark_guid(""), "")


class _Request:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query_params = query or {}

    async def json(self):
        return self._body


class SkipConfigRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_config_normalizes_line_season_identity(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(skip_marks, "_STATE",
                             Path(tmp) / "skip_marks.json"):
            guid = appmod._line_season_guid("og", "lzi::lzm3u8", 1)
            response = await appmod.play_set_config_by_item(_Request({
                "guid": guid, "skip_opening": 90, "skip_ending": 110}))
            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.body)
            base = appmod.season_guid("og", 1)
            self.assertEqual(payload["data"]["guid"], base)
            self.assertEqual(payload["data"]["play_config"],
                             {"guid": base, "skip_opening": 90,
                              "skip_ending": 110})
            self.assertEqual(skip_marks.play_config(base),
                             {"guid": base, "skip_opening": 90,
                              "skip_ending": 110})
            # GET 与 DELETE 走同一归一，往返一致。
            got = await appmod.ext_skip_marks_get(
                _Request(query={"guid": guid}))
            self.assertEqual(got.status_code, 200)
            gdata = json.loads(got.body)["data"]
            self.assertEqual(gdata["guid"], base)
            self.assertEqual(gdata["mark"]["skip_opening"], 90)
            gone = await appmod.ext_skip_marks_delete(
                _Request(query={"guid": guid}))
            self.assertEqual(gone.status_code, 200)
            self.assertTrue(json.loads(gone.body)["data"])
            after = await appmod.ext_skip_marks_get(
                _Request(query={"guid": guid}))
            self.assertEqual(
                json.loads(after.body)["data"]["mark"]["skip_opening"], 0)

    async def test_set_config_route_rejects_missing_guid(self):
        response = await appmod.play_set_config_by_item(_Request({}))
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
