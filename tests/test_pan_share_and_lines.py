"""网盘分享链接 → 转存 → 播放链路 + 线路切换检查。

覆盖：
1. app._resolve_pan_share：识别网盘家 → 调 mod.resolve_share 转存 →
   picked.url 换成本服务 /v/api/ext/pan/{key}/play?fid=... 并标 pan=True。
2. hls_race._resolve_if_needed：resolve=True 的集先走网盘分支，
   失败才退回网页嗅探（顺序不能反）。
3. 线路菜单：网盘线恒排最后、当前线排第一、其它线路相对顺序保留。
4. 手动切到网盘线：无手切意图时忽略该线走正常选线（网盘永不作默认）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"
sys.path.insert(0, str(PROXY))


class PanShareResolveTests(unittest.TestCase):
    """1/2) 网盘分享解析与接线。"""

    def setUp(self):
        import app as appmod
        import hls_race
        self.app = appmod
        self.hls = hls_race

    def test_resolve_pan_share_quark(self):
        """夸克分享 → 转存 → /v/api/ext/pan/play?fid=…"""
        calls = {}

        def _fake_resolve_share(share, hint=""):
            calls["share"] = share
            calls["hint"] = hint
            return {"items": [{"fid": "FID1", "url": "http://x"},
                               {"fid": "FID2", "url": "http://y"}]}

        fake_mod = type("M", (), {"resolve_share": staticmethod(_fake_resolve_share)})
        with patch.dict(sys.modules, {"pan": fake_mod}):
            picked = {"url": "https://pan.quark.cn/s/abc123?pwd=5678",
                      "index": 1}
            ok = asyncio.run(self.app._resolve_pan_share(picked))
        self.assertTrue(ok, "应识别为网盘分享并转存")
        self.assertEqual(picked["url"], "/v/api/ext/pan/play?fid=FID2")
        self.assertTrue(picked["pan"])
        self.assertEqual(picked["pan_provider"], "quark")
        self.assertFalse(picked["m3u8"])
        self.assertEqual(calls["hint"], "5678")

    def test_resolve_pan_share_uc(self):
        """UC 分享 → /v/api/ext/pan/uc/play?fid=…"""
        def _rs(share, hint=""):
            return {"items": [{"fid": "U1", "url": "http://x"}]}

        fake_mod = type("M", (), {"resolve_share": staticmethod(_rs)})
        with patch.dict(sys.modules, {"panuc": fake_mod}):
            picked = {"url": "https://drive.uc.cn/s/zzz", "index": 0}
            ok = asyncio.run(self.app._resolve_pan_share(picked))
        self.assertTrue(ok)
        self.assertEqual(picked["url"], "/v/api/ext/pan/uc/play?fid=U1")

    def test_resolve_pan_share_not_pan_url(self):
        """非网盘地址不误判。"""
        picked = {"url": "https://example.com/a.m3u8", "index": 0}
        self.assertFalse(asyncio.run(self.app._resolve_pan_share(picked)))

    def test_resolve_pan_share_error_reason_kept(self):
        """转存失败时把原因写进 _pan_error（供上层给准确提示）。"""
        def _rs(share, hint=""):
            raise RuntimeError("好友取消分享")

        fake_mod = type("M", (), {"resolve_share": staticmethod(_rs)})
        with patch.dict(sys.modules, {"pan": fake_mod}):
            picked = {"url": "https://pan.quark.cn/s/abc", "index": 0}
            ok = asyncio.run(self.app._resolve_pan_share(picked))
        self.assertFalse(ok)
        self.assertIn("好友取消分享", picked.get("_pan_error", ""))

    def test_resolve_if_needed_prefers_pan_branch(self):
        """resolve=True 的集：先走网盘分支，命中就不再走网页嗅探。"""
        hls = self.hls
        picked = {"url": "https://pan.quark.cn/s/abc", "index": 0,
                  "resolve": True, "headers": {}}

        async def _fake_pan(p):
            p["url"] = "/v/api/ext/pan/play?fid=F9"
            p["pan"] = True
            return True

        sniffed = {"hit": False}

        async def _fake_snf(client, url, referer=""):
            sniffed["hit"] = True
            return "http://should-not-be-used"

        with patch.object(self.app, "_resolve_pan_share", _fake_pan), \
                patch("providers.resolve_share", _fake_snf):
            asyncio.run(hls._resolve_if_needed(picked, None))

        self.assertEqual(picked["url"], "/v/api/ext/pan/play?fid=F9")
        self.assertFalse(sniffed["hit"], "网盘分支命中后不应再走网页嗅探")

    def test_resolve_if_needed_falls_back_to_sniff(self):
        """网盘分支不命中时才退回网页嗅探（普通播放页仍能播）。"""
        hls = self.hls
        picked = {"url": "https://example.com/play/1", "index": 0,
                  "resolve": True, "headers": {}}

        async def _fake_pan(p):
            return False

        async def _fake_snf(client, url, referer=""):
            return "http://real.m3u8"

        with patch.object(self.app, "_resolve_pan_share", _fake_pan), \
                patch("providers.resolve_share", _fake_snf), \
                patch.object(self.app, "ext_client", lambda req: None):
            asyncio.run(hls._resolve_if_needed(picked, None))
        self.assertEqual(picked["url"], "http://real.m3u8")


class LineSwitchTests(unittest.TestCase):
    """3/4) 线路菜单与手动切线。"""

    def setUp(self):
        import app as appmod
        self.app = appmod

    def _lines(self):
        return [
            {"line": "baofeng::bfzym3u8", "url": "https://a/a.m3u8",
             "m3u8": True, "resolve": False},
            {"line": "dytt::dyttm3u8", "url": "https://b/b.m3u8",
             "m3u8": True, "resolve": False},
            {"line": "lzi::lzm3u8", "url": "https://c/c.m3u8",
             "m3u8": True, "resolve": False},
        ]

    def test_pansou_line_is_last(self):
        target = {"online_guid": "og", "kind": "title",
                  "line": "baofeng::bfzym3u8", "index": 0}
        with patch.object(self.app.store, "target_lines", lambda t: self._lines()), \
                patch.object(self.app, "_pansou_available", lambda: True), \
                patch.object(self.app, "_pansou_manual_target",
                             lambda i=0: {"line": self.app.PANSOU_LINE,
                                           "url": "pansou://manual",
                                           "index": i, "_pansou_manual": True}):
            out = self.app._line_choices(target)
        self.assertEqual(out[0]["line"], "baofeng::bfzym3u8", "当前线应排第一")
        self.assertEqual(out[-1]["line"], self.app.PANSOU_LINE, "网盘线应恒排最后")
        self.assertEqual([c["line"] for c in out[1:-1]],
                         ["dytt::dyttm3u8", "lzi::lzm3u8"],
                         "其它线路相对顺序应保留")

    def test_pansou_line_not_in_menu_when_unavailable(self):
        """网盘模块不可用时菜单里不出现网盘线。"""
        target = {"online_guid": "og", "kind": "title",
                  "line": "baofeng::bfzym3u8", "index": 0}
        with patch.object(self.app.store, "target_lines", lambda t: self._lines()), \
                patch.object(self.app, "_pansou_available", lambda: False):
            out = self.app._line_choices(target)
        self.assertNotIn(self.app.PANSOU_LINE, [c["line"] for c in out])

    def test_manual_pick_without_intent_ignores_pansou_line(self):
        """无手切意图时选网盘线 → 忽略该线走正常选线（网盘永不作默认）。"""
        req = _FakeReq({"line": self.app.PANSOU_LINE, "index": "0"})
        with patch.object(self.app, "_pansou_is_user_pick", lambda mg: False):
            out = self.app._request_without_line(req)
        self.assertNotIn("line", out.url.query)

    def test_manual_pick_with_intent_uses_pansou_line(self):
        """有手切意图时返回手动 target（ready 时给转存快照）。"""
        with patch.object(self.app, "_pansou_is_user_pick", lambda mg: True), \
                patch.object(self.app, "_pansou_manual_state", lambda mg: "armed"), \
                patch.object(self.app, "_pansou_manual_target",
                             lambda i=0: {"line": self.app.PANSOU_LINE,
                                           "url": "pansou://manual", "index": i,
                                           "_pansou_manual": True}), \
                patch.object(self.app.store, "resolve_media_guid",
                             lambda mg: {"online_guid": "og1", "title": "T",
                                         "kind": "title"}):
            req = _FakeReq({"line": self.app.PANSOU_LINE, "index": "2"})
            picked = self.app._pick_episode("mg1", req)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["line"], self.app.PANSOU_LINE)
        self.assertEqual(picked["index"], 2)


class _FakeReq:
    """最小 Request 替身：url.query / query_params / scope / receive / app.state。"""

    class _App:
        class state:
            ext = None

    def __init__(self, query: dict):
        import urllib.parse as up
        self._q = up.urlencode(query)
        self.url = type("U", (), {"query": self._q})()
        self.app = self._App()
        self.scope = {"type": "http", "method": "GET", "path": "/",
                     "query_string": self._q.encode(), "headers": []}

    def receive(self):  # pragma: no cover - 仅为构造 Request 完整
        return None

    @property
    def query_params(self):
        import urllib.parse as up
        parsed = up.parse_qsl(self._q, keep_blank_values=True)
        # parse_qsl 返回 [(k, v)] —— v 已是字符串，不能再取 v[0]
        return {k: v for k, v in parsed}


if __name__ == "__main__":
    unittest.main()
