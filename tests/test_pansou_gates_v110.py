"""PanSou 守门回归：抄上游 v1.1.0 正式版（a26abdc）的 4 处网盘守门 + 文案修正。

- #16 _stream_list_impl 菜单禁预热（上游 app.py:3480-3482）
- #17 _vod_play_info 网盘分支（上游 app.py:3847-3849、3893-3895）
- #18 _line_display_name 网盘分支（上游 app.py:6854-6855）
- #20 hls_seg record_seg 排除（上游 app.py:7404）
- 文案：_vod_pansou_card overview 不再承诺按片名搜索
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import hls_race
import linepredict
import providers
import store as storemod
import test_t4_app as fixtures
from pansou import PANSOU_LABEL, PANSOU_LINE, _pansou_mark_user_pick

SEG = b"\x47" * 188


class PanSouGatesV110Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.AppTests.asyncSetUp(self)

    def handle(self, request):
        return fixtures.AppTests.handle(self, request)

    async def import_source(self):
        return None

    def _seed_tv(self):
        src = {"id": "probe-src", "name": "Probe",
               "api": "https://media.test/cms", "enabled": True}
        title = providers._map_vod({
            "vod_id": "tv1", "vod_name": "探针剧", "type_name": "电视剧",
            "vod_year": "2024", "vod_play_from": "A",
            "vod_play_url": "第1集$https://media.test/e1.m3u8"
                            "#第2集$https://media.test/e2.m3u8"}, src)
        return self.store.remember_titles([title])[0]["guid"]

    def _seed_movie(self):
        src = {"id": "probe-src", "name": "Probe",
               "api": "https://media.test/cms", "enabled": True}
        title = providers._map_vod({
            "vod_id": "m1", "vod_name": "探针片", "type_name": "电影",
            "vod_year": "2024", "vod_play_from": "A",
            "vod_play_url": "正片$https://media.test/m1.m3u8"}, src)
        return self.store.remember_titles([title])[0]["guid"]

    # ---------- #16 菜单禁预热 ----------

    async def test_stream_list_pansou_line_does_not_prewarm(self):
        og = self._seed_tv()
        guid = appmod._line_item_guid(og, PANSOU_LINE, 0)
        with patch.object(appmod, "_warm_playlist") as warm:
            r = await self.client.get(f"/v/api/v1/stream/list/{guid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["video_streams"])
        warm.assert_not_called()

    async def test_stream_list_plain_line_still_prewarms(self):
        og = self._seed_tv()
        guid = appmod._line_item_guid(og, "A", 0)
        with patch.object(appmod, "_warm_playlist") as warm:
            r = await self.client.get(f"/v/api/v1/stream/list/{guid}")
        self.assertEqual(r.status_code, 200, r.text)
        warm.assert_called_once()

    # ---------- #17 play/info 网盘分支 ----------

    async def test_play_info_pansou_tv_uses_manual_target_and_label(self):
        og = self._seed_tv()
        guid = appmod._line_item_guid(og, PANSOU_LINE, 0)
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": guid})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["type"], "TV")
        item = data["item"]
        self.assertEqual(item["number_of_episodes"], 2)
        self.assertTrue(
            item["overview"].startswith(f"来源：{PANSOU_LABEL}"),
            item["overview"])
        self.assertIn("指定线路直接播放，不自动竞速。", item["overview"])

    async def test_play_info_pansou_movie_label(self):
        og = self._seed_movie()
        guid = appmod._line_item_guid(og, PANSOU_LINE, 0)
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": guid})
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()["data"]["item"]
        self.assertEqual(item["number_of_episodes"], 0)
        self.assertTrue(
            item["overview"].startswith(f"来源：{PANSOU_LABEL}"),
            item["overview"])

    async def test_play_info_plain_line_keeps_provider_label(self):
        og = self._seed_tv()
        guid = appmod._line_item_guid(og, "A", 0)
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": guid})
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()["data"]["item"]
        self.assertEqual(item["number_of_episodes"], 2)
        self.assertIn("以该线路为优先种子起播，仍走竞速选最快线。",
                      item["overview"])
        self.assertNotIn(PANSOU_LABEL, item["overview"])

    # ---------- #18 线路显示名 ----------

    def test_line_display_name_pansou_manual(self):
        self.assertEqual(
            hls_race._line_display_name({"line": PANSOU_LINE}), PANSOU_LABEL)
        self.assertEqual(
            hls_race._line_display_name(
                {"_pansou_manual": True, "line": "other"}), PANSOU_LABEL)

    def test_line_display_name_pan_url_keeps_cloud_prefix(self):
        name = hls_race._line_display_name(
            {"line": "A::线路1", "url": "https://pan.quark.cn/s/abc"})
        self.assertTrue(name.startswith("☁"), name)

    # ---------- #20 分片测速统计排除 ----------

    def _seg_url(self, mg, line=None):
        u = base64.urlsafe_b64encode(
            b"https://media.test/seg.ts").decode().rstrip("=")
        params = {"u": u}
        if line:
            params["line"] = line
        return f"/v/media/online/{mg}/seg", params

    async def test_hls_seg_pump_pansou_not_recorded(self):
        og = self._seed_movie()
        mg = storemod.media_guid(og)
        _pansou_mark_user_pick(mg)
        url, params = self._seg_url(mg, PANSOU_LINE)
        with patch.object(linepredict, "record_seg") as rec:
            r = await self.client.get(url, params=params)
            _ = r.content
        self.assertEqual(r.status_code, 200, r.text)
        rec.assert_not_called()

    async def test_hls_seg_pump_plain_line_recorded(self):
        og = self._seed_movie()
        mg = storemod.media_guid(og)
        url, params = self._seg_url(mg)
        with patch.object(linepredict, "record_seg") as rec:
            r = await self.client.get(url, params=params)
            _ = r.content
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(rec.call_count, 1)

    async def test_hls_seg_buffered_pansou_not_recorded(self):
        og = self._seed_movie()
        mg = storemod.media_guid(og)
        _pansou_mark_user_pick(mg)
        url, params = self._seg_url(mg, PANSOU_LINE)
        fake = httpx.Response(200, content=SEG,
                              headers={"Content-Type": "video/mp2t"})
        with patch.object(appmod, "_try_stream_segment",
                          new=AsyncMock(return_value=None)), \
                patch.object(appmod, "ext_get_retry",
                             new=AsyncMock(return_value=fake)), \
                patch.object(linepredict, "record_seg") as rec:
            r = await self.client.get(url, params=params)
            _ = r.content
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, SEG)
        rec.assert_not_called()

    async def test_hls_seg_buffered_plain_line_recorded(self):
        og = self._seed_movie()
        mg = storemod.media_guid(og)
        url, params = self._seg_url(mg)
        fake = httpx.Response(200, content=SEG,
                              headers={"Content-Type": "video/mp2t"})
        with patch.object(appmod, "_try_stream_segment",
                          new=AsyncMock(return_value=None)), \
                patch.object(appmod, "ext_get_retry",
                             new=AsyncMock(return_value=fake)), \
                patch.object(linepredict, "record_seg") as rec:
            r = await self.client.get(url, params=params)
            _ = r.content
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(rec.call_count, 1)

    # ---------- 文案修正 ----------

    def test_pansou_card_overview_promises_manual_share_only(self):
        brief = {"guid": "g", "title": "测试片", "overview": "简介"}
        row = appmod._vod_pansou_card(brief, PANSOU_LINE, 0, "")
        self.assertIn("手动粘贴网盘分享链接", row["overview"])
        self.assertNotIn("按片名搜索", row["overview"])
        self.assertNotIn("搜索网盘", row["overview"])
