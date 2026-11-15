"""端到端回归（真机时序）：A 播放中切 PanSou → 退出 → 点播 B。

用户 2026-11-14 现场：A 片播放中切到 PanSou 网盘线后退出，再点播 B 片，
B 显示网盘线路、放不出画面，切回其它线路也放不出，必须退出重进才行。

飞牛的「清晰度/线路」选择是**跨片全局记忆**的：播 B 时仍带 A 片切网盘线时的
那个 bitrate。这里按真机请求序列走完整 ASGI 路由，断言三件事：

* A 仍能正常切到网盘线（新增保护不能把正常能力做掉）；
* B 不被上一部片的档位拉进网盘占位画面，按真实线路起播；
* B 在菜单里切回真实线路立即可用。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import httpx

import app as appmod
import config as configmod
import history as histmod
import pansou
import providers
import store as storemod
import t4_media as t4_media_mod
import t4_service as t4_service_mod

LINE_A, LINE_B, LINE_C = "线路甲", "线路乙", "线路丙"


class FakePan:
    def __init__(self):
        self.items = [{"name": "ep01.mp4", "size": 100, "fid": "f1",
                       "url": "/v/api/ext/pan/play?fid=f1"}]

    def logged_in(self):
        return True

    def resolve_share(self, url, hint=""):
        return {"title": "x", "items": list(self.items)}


def _handle(request):
    if request.url.host == "localhost":
        return httpx.Response(200, json={"code": 0, "data": []})
    if request.url.path.endswith(".m3u8"):
        return httpx.Response(200, text=(
            "#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:6\n"
            "#EXTINF:6,\nseg1.ts\n#EXT-X-ENDLIST\n"),
            headers={"Content-Type": "application/octet-stream"})
    return httpx.Response(200, content=b"\x00" * 4096,
                          headers={"Content-Type": "video/mp4"})


def _seed(store, name, vod_id, lines):
    src = {"id": "probe-src", "name": "Probe",
           "api": "https://media.test/cms", "enabled": True}
    play_url = "$$$".join(
        "正片$https://media.test/%s_%s.m3u8" % (vod_id, ln) for ln in lines)
    title = providers._map_vod({
        "vod_id": vod_id, "vod_name": name, "type_name": "电影",
        "vod_year": "2024", "vod_play_from": "$$$".join(lines),
        "vod_play_url": play_url}, src)
    return store.remember_titles([title])[0]["guid"]


class CrossShowPanSouTests(unittest.TestCase):
    """真机时序的端到端回归。"""

    def test_switch_pansou_then_play_another_show(self):
        asyncio.run(self._run())

    async def _run(self):
        pansou._PANSOU_USER_PICK.clear()
        pansou._PANSOU_REAL_PLAY.clear()
        pansou._PANSOU_STATE.clear()
        temp = tempfile.TemporaryDirectory()
        patchers = []
        self._patchers = patchers

        def _patch(target, **kwargs):
            p = patch(target, **kwargs)
            patchers.append(p)
            return p.start()

        def _patch_obj(module, name, value):
            p = patch.object(module, name, value)
            patchers.append(p)
            return p.start()

        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            _patch_obj(module, "_STATE",
                      Path(temp.name) / (filename + ".json"))
        config = configmod.Config()
        config.save({"sources": [],
                     "removed_ids": [s["id"]
                                     for s in providers.DEFAULT_SOURCES]})
        store = storemod.Store()
        history = histmod.History()
        for module, name, value in [
                (appmod, "config", config), (appmod, "store", store),
                (appmod, "history", history), (storemod, "store", store),
                (appmod, "t4_service", t4_service_mod.T4Service(store, config)),
                (appmod, "t4_media", t4_media_mod.T4Media()),
                (appmod, "_schedule_routes_refresh", lambda: None)]:
            _patch_obj(module, name, value)
        _patch("app.fnusersmod.resolve", return_value={
            "known": True, "guid": "admin-guid", "name": "admin",
            "is_admin": True, "token": "tok-admin"})
        appmod._search_cache.clear()
        appmod._search_inflight.clear()

        real_client = httpx.AsyncClient
        net = real_client(transport=httpx.MockTransport(_handle))
        appmod.app.state.ext = net
        appmod.app.state.up = net
        _patch("providers.httpx.AsyncClient",
           side_effect=lambda *a, **k: real_client(
               *a, transport=httpx.MockTransport(_handle), **k))
        _patch("pansou._pan_module", side_effect=lambda key: FakePan())
        _patch("pansou._pansou_search", return_value=[])

        client = real_client(transport=httpx.ASGITransport(app=appmod.app),
                             base_url="http://fnmedia.test")

        og_a = _seed(store, "影片A", "a1", (LINE_A, LINE_B, LINE_C))
        og_b = _seed(store, "影片B", "b2", (LINE_A, LINE_B, LINE_C))

        def rel(link):
            return link.split("http://fnmedia.test")[-1]

        def line_of(link):
            qs = urllib.parse.parse_qs(
                urllib.parse.urlparse(link).query)
            return (qs.get("line") or [""])[0]

        async def info(og):
            r = await client.post(
                "/v/api/v1/play/info",
                json={"item_guid": appmod._line_item_guid(og, LINE_A, 0)})
            return r.json()["data"]

        async def stream(mg, bitrate=None):
            body = {"media_guid": mg}
            if bitrate is not None:
                body["bitrate"] = bitrate
            return (await client.post("/v/api/v1/stream", json=body)).json()

        async def play(mg, bitrate=None):
            body = {"media_guid": mg, "video_guid": ""}
            if bitrate is not None:
                body["bitrate"] = bitrate
            return (await client.post("/v/api/v1/play/play", json=body)).json()

        # ---------- A：真实线路起播 ----------
        info_a = await info(og_a)
        mg_a = info_a["media_guid"]
        quals_a = (await stream(mg_a))["data"]["qualities"]
        names_a = [q["resolution"] for q in quals_a]
        self.assertIn(pansou.PANSOU_LABEL, names_a,
                      "网盘线应出现在清晰度菜单里")
        pan_br = quals_a[names_a.index(pansou.PANSOU_LABEL)]["bitrate"]
        real_br_a = quals_a[0]["bitrate"]

        pp = await play(mg_a, real_br_a)
        self.assertEqual(line_of(pp["data"]["play_link"]), LINE_A,
                         "A 应先按真实线路起播")
        preset = await client.get(rel(pp["data"]["play_link"]))
        self.assertEqual(preset.status_code, 200)
        self.assertIn("EXTM3U", preset.text)

        # ---------- A：播放中切到网盘线 ----------
        pp = await play(mg_a, pan_br)
        self.assertTrue(pansou._pansou_is_user_pick(mg_a),
                        "A 有起播证据，菜单切网盘线必须被接受")
        preset = await client.get(rel(pp["data"]["play_link"]))
        self.assertEqual(preset.status_code, 200)
        self.assertIn("EXTM3U", preset.text)
        main = await client.get(
            rel(pp["data"]["play_link"]).replace("preset.m3u8", "main.m3u8"))
        self.assertEqual(main.status_code, 200)
        self.assertIn("EXTM3U", main.text)

        # ---------- 退出，点播 B（仍带 A 的网盘档位）----------
        info_b = await info(og_b)
        mg_b = info_b["media_guid"]
        quals_b = (await stream(mg_b))["data"]["qualities"]
        names_b = [q["resolution"] for q in quals_b]

        pp_b = await play(mg_b, pan_br)
        link_b = pp_b["data"]["play_link"]
        self.assertFalse(pansou._pansou_is_user_pick(mg_b),
                         "B 不该被上一部片的档位登记成手切网盘线")
        self.assertEqual(line_of(link_b), LINE_A,
                         "B 必须按默认真实线路起播，不能被拉进网盘占位")

        preset_b = await client.get(rel(link_b))
        self.assertEqual(preset_b.status_code, 200)
        self.assertIn("EXTM3U", preset_b.text)
        # 用户切线路走「清晰度」菜单（qualities），那里必须列出全部真实线路，
        # 否则切回其它线路无从下手。
        for line in (LINE_A, LINE_B, LINE_C):
            self.assertIn(line, names_b)
        # master 里网盘变体的带宽必须最低：否则自适应码率会主动升级到这个
        # 只有占位循环的变体（用户看到的「显示 pansou 线路、放不出来」）。
        rows = [ln for ln in preset_b.text.splitlines()
                if ln.startswith("#EXT-X-STREAM-INF")]
        self.assertGreaterEqual(len(rows), 2)
        bws = [int(r.split("BANDWIDTH=")[1].split(",")[0]) for r in rows]
        pan_row = [r for r in rows if "PanSou" in r or "网盘" in r][0]
        pan_bw = int(pan_row.split("BANDWIDTH=")[1].split(",")[0])
        self.assertEqual(pan_bw, min(bws),
                         "网盘变体带宽必须最低，避免被 ABR 自动选中")
        main_b = await client.get(
            rel(link_b).replace("preset.m3u8", "main.m3u8"))
        self.assertEqual(main_b.status_code, 200)

        # ---------- B：切回其它真实线路 ----------
        real_br_b = quals_b[names_b.index(LINE_B)]["bitrate"]
        pp_b2 = await play(mg_b, real_br_b)
        link_b2 = pp_b2["data"]["play_link"]
        self.assertEqual(line_of(link_b2), LINE_B,
                         "切回真实线路必须立刻生效")
        preset_b2 = await client.get(rel(link_b2))
        self.assertEqual(preset_b2.status_code, 200)
        self.assertIn("EXTM3U", preset_b2.text)

        await client.aclose()
        await net.aclose()
        for p in reversed(self._patchers):
            p.stop()
        self._patchers = []
        temp.cleanup()


if __name__ == "__main__":
    unittest.main()
