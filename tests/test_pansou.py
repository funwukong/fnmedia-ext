"""PanSou 手动线状态机（2026-11-12 上游同步第 3 批）+ P3 网盘回归确认。

状态机本体见 proxy/pansou.py：上游公开仓只给存根契约，真实实现是
fork 自研；搜索一步默认空钩子（无可验证的公开 PanSou 端点），可用
核心是“手动贴分享链接 → resolve_share 转存 → 直链”。
本文件用假网盘驱动测状态机（不碰真实网络/飞牛），并回归确认 P3
三件套在去 guard 后仍在（识别注册表、清理循环、pan 播放路由、
hls_seg 网盘 Cookie/Referer 注入）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import pansou
from panmatch import _PAN_LINKSTART_RE, _PAN_PROVIDERS, _pan_match

_QUARK_URL = "https://pan.quark.cn/s/abc123def"
_UC_URL = "https://drive.uc.cn/s/xyz789"
_ALI_URL = "https://www.alipan.com/s/aaa111"


class _FakePan:
    """pan/panuc/panali 的替身：登录态、扫码、转存均可脚本化。"""

    def __init__(self, logged_in=True, items=None, fail_resolve=False):
        self._logged_in = logged_in
        self.items = items if items is not None else [
            {"name": "ep01.mp4", "size": 100, "fid": "fid1",
             "url": "/v/api/ext/pan/play?fid=fid1"},
            {"name": "ep02.mp4", "size": 200, "fid": "fid2",
             "url": "/v/api/ext/pan/play?fid=fid2"},
        ]
        self.fail_resolve = fail_resolve
        self.qr_started = 0
        self.resolve_calls: list[str] = []

    def logged_in(self):
        return self._logged_in

    def qr_start(self):
        self.qr_started += 1
        return {"qr_text": "https://example.invalid/qr", "token": "T1"}

    def qr_poll(self, session):
        self._logged_in = True
        return {"status": "success", "nickname": "fake"}

    def qr_png(self, text):
        return b"\x89PNG\r\n\x1a\nfake-png"

    def resolve_share(self, url, passcode_hint=""):
        self.resolve_calls.append(url)
        if self.fail_resolve:
            raise RuntimeError("转存失败（假驱动）")
        return {"title": "假影片", "items": list(self.items)}


class _PansouBase(unittest.TestCase):
    def setUp(self):
        self.mg = "test-pansou-mg-001"
        self.fakes = {"quark": _FakePan(logged_in=False),
                      "uc": _FakePan(logged_in=False),
                      "ali": _FakePan(logged_in=False)}
        self._patcher = patch.object(
            pansou, "_pan_module",
            side_effect=lambda key: self.fakes.get(key))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # 搜索适配器已接通线上实例：_pansou_run 测试默认不联网，
        # 搜索行为本身由 tests/test_pansou_search.py 用 MockTransport 覆盖。
        self._search_patcher = patch.object(
            pansou, "_pansou_search", return_value=[])
        self._search_patcher.start()
        self.addCleanup(self._search_patcher.stop)
        self.addCleanup(pansou._pansou_reset, self.mg)
        self.addCleanup(pansou._PANSOU_QR.clear)

    def _all_logged_in(self):
        for f in self.fakes.values():
            f._logged_in = True


class PansouContractTests(_PansouBase):
    def test_constants_and_line_name(self):
        self.assertEqual(pansou.PANSOU_LINE, "builtin-pansou::PanSou网盘高清")
        self.assertEqual(pansou.PANSOU_LABEL, "🎞️网盘高清（PanSou）")
        self.assertTrue(pansou._is_pansou_line_name(pansou.PANSOU_LINE))
        self.assertTrue(pansou._is_pansou_line_name("builtin-pansou::x"))
        self.assertFalse(pansou._is_pansou_line_name("ffzy::m3u8"))
        self.assertFalse(pansou._is_pansou_line_name(""))
        self.assertFalse(pansou._is_pansou_line_name(None))

    def test_manual_target_shape(self):
        t = pansou._pansou_manual_target(3)
        self.assertEqual(t["line"], pansou.PANSOU_LINE)
        self.assertEqual(t["index"], 3)
        self.assertTrue(t["_pansou_manual"])
        self.assertFalse(t["m3u8"])

    def test_search_hook_base_is_user_supplied_instance(self):
        # 搜索钩子已接通用户提供的自托管实例（行为见
        # tests/test_pansou_search.py，本文件 _pansou_run 测试默认
        # 把搜索打桩为空以保持离线）。
        # 2026-11-13：地址改为可配置（pansou_url），默认仍是该实例 ——
        # 该实例已不可达，用户可在管理台「网盘登录」换成自己的。
        self.assertEqual(pansou._pansou_base_url(),
                         "https://bus.wulii.eu.cc")


class PansouArmTests(_PansouBase):
    def test_arm_does_not_demand_login_upfront(self):
        """2026-11-13：选网盘线只是兜底意图，不该立刻判未登录（那会直接弹扫码）。

        登录检查推迟到真正转存前（_pansou_run 的候选循环里）。
        """
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        self.assertEqual(pansou._pansou_manual_state(self.mg), "armed")
        self.assertEqual(pansou._pansou_status(self.mg)["error"], "")

    def test_arm_armed_when_one_logged_in(self):
        self.fakes["uc"]._logged_in = True
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        self.assertEqual(pansou._pansou_manual_state(self.mg), "armed")

    def test_user_pick_flag(self):
        self.assertFalse(pansou._pansou_is_user_pick(self.mg))
        pansou._pansou_mark_user_pick(self.mg)
        self.assertTrue(pansou._pansou_is_user_pick(self.mg))

    def test_set_manual_share_rejects_unplayable(self):
        with self.assertRaises(ValueError):
            pansou._pansou_set_manual_share(
                self.mg, "https://pan.baidu.com/s/abc123")
        with self.assertRaises(ValueError):
            pansou._pansou_set_manual_share(
                self.mg, "https://example.com/video.mp4")

    def test_set_manual_share_accepts_quark(self):
        st = pansou._pansou_set_manual_share(self.mg, _QUARK_URL, 1)
        self.assertEqual(st["state"], "need_login")  # 三家均未登录
        self.assertIn(_QUARK_URL, st["manual_shares"])
        self.assertEqual(st["index"], 1)


class PansouRunTests(_PansouBase):
    def test_run_manual_share_ready(self):
        self._all_logged_in()
        pansou._pansou_manual_arm(self.mg, "假影片", 1)
        pansou._pansou_set_manual_share(self.mg, _QUARK_URL, 1)
        asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "ready")
        snap = pansou._PANSOU_MAN_SNAP.get(self.mg)
        self.assertIsNotNone(snap)
        # index=1 选第 2 个转存条目
        self.assertEqual(snap["url"], "/v/api/ext/pan/play?fid=fid2")
        self.assertEqual(snap["line"], pansou.PANSOU_LINE)
        self.assertEqual(snap["provider"], "quark")
        self.assertEqual(self.fakes["quark"].resolve_calls, [_QUARK_URL])

    def test_run_dead_when_no_candidates(self):
        self._all_logged_in()
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "dead")
        self.assertIn("PanSou", pansou._pansou_status(self.mg)["error"])

    def test_run_search_hook_path(self):
        self._all_logged_in()
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        with patch.object(pansou, "_pansou_search",
                          return_value=[_ALI_URL]):
            asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "ready")
        self.assertEqual(pansou._PANSOU_MAN_SNAP[self.mg]["provider"], "ali")

    def test_run_need_login_for_share_provider(self):
        self.fakes["quark"]._logged_in = True  # 夸克已登，UC 未登
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        self.assertEqual(pansou._pansou_manual_state(self.mg), "armed")
        pansou._pansou_set_manual_share(self.mg, _UC_URL, 0)
        asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "need_login")
        self.assertEqual(pansou._pansou_status(self.mg)["provider"], "uc")

    def test_run_dead_when_resolve_fails(self):
        self._all_logged_in()
        self.fakes["quark"].fail_resolve = True
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        pansou._pansou_set_manual_share(self.mg, _QUARK_URL, 0)
        asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "dead")

    def test_arm_keeps_ready(self):
        self._all_logged_in()
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        pansou._pansou_set_manual_share(self.mg, _QUARK_URL, 0)
        asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "ready")
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        self.assertEqual(pansou._pansou_manual_state(self.mg), "ready")

    def test_kick_singleflight_reaches_ready(self):
        async def _drive():
            self._all_logged_in()
            pansou._pansou_manual_arm(self.mg, "假影片", 0)
            pansou._pansou_set_manual_share(self.mg, _QUARK_URL, 0)
            pansou._pansou_manual_kick(self.mg)
            pansou._pansou_manual_kick(self.mg)  # 第二次不重复建任务
            for _ in range(50):
                if pansou._pansou_manual_state(self.mg) == "ready":
                    break
                await asyncio.sleep(0.05)

        asyncio.run(_drive())
        self.assertEqual(pansou._pansou_manual_state(self.mg), "ready")


class PansouQrTests(_PansouBase):
    def test_login_qr_png_first_not_logged_in(self):
        qr = asyncio.run(pansou._pansou_login_qr_png(self.mg))
        self.assertIsNotNone(qr)
        png, name = qr
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(name, "夸克网盘")
        # 120 秒内复用，不重复申请
        qr2 = asyncio.run(pansou._pansou_login_qr_png(self.mg))
        self.assertEqual(qr2[0], png)
        self.assertEqual(self.fakes["quark"].qr_started, 1)

    def test_login_qr_png_none_when_logged_in(self):
        self._all_logged_in()
        self.assertIsNone(asyncio.run(pansou._pansou_login_qr_png(self.mg)))

    def test_wait_login_polls_to_success(self):
        async def _drive():
            pansou._pansou_manual_arm(self.mg, "假影片", 0)
            ok = await pansou._pansou_wait_login(self.mg, "quark")
            self.assertTrue(ok)
            self.assertTrue(self.fakes["quark"].logged_in())

        asyncio.run(_drive())


class PansouWiringTests(_PansouBase):
    def test_line_choices_appends_pansou(self):
        import app as appmod
        target = {"online_guid": "no-such-guid", "index": 0,
                  "line": "ffzy::m3u8"}
        with patch.object(appmod, "_pansou_available", return_value=True):
            choices = appmod._line_choices(target)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["line"], pansou.PANSOU_LINE)
        self.assertEqual(choices[0]["label"], pansou.PANSOU_LABEL)
        with patch.object(appmod, "_pansou_available", return_value=True):
            self.assertEqual(appmod._line_choices(target, include_pansou=False),
                             [])
        with patch.object(appmod, "_pansou_available", return_value=False):
            self.assertEqual(appmod._line_choices(target), [])

    def test_pick_episode_pansou_needs_user_pick(self):
        import app as appmod
        from starlette.requests import Request
        qs = urllib.parse.urlencode(
            {"line": pansou.PANSOU_LINE, "index": "0"}).encode()
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "query_string": qs, "headers": []})
        # 无手切意图：不返回手动 target（走正常选线，未绑定故 None）
        self.assertIsNone(appmod._pick_episode(self.mg, req))
        # 有手切意图：返回手动 target
        pansou._pansou_mark_user_pick(self.mg)
        tgt = appmod._pick_episode(self.mg, req)
        self.assertIsNotNone(tgt)
        self.assertEqual(tgt["line"], pansou.PANSOU_LINE)
        self.assertTrue(tgt["_pansou_manual"])

    def test_pick_episode_pansou_ready_returns_snap(self):
        import app as appmod
        from starlette.requests import Request
        self._all_logged_in()
        pansou._pansou_manual_arm(self.mg, "假影片", 0)
        pansou._pansou_set_manual_share(self.mg, _QUARK_URL, 0)
        asyncio.run(pansou._pansou_run(self.mg))
        self.assertEqual(pansou._pansou_manual_state(self.mg), "ready")
        pansou._pansou_mark_user_pick(self.mg)
        qs = urllib.parse.urlencode(
            {"line": pansou.PANSOU_LINE, "index": "0"}).encode()
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "query_string": qs, "headers": []})
        tgt = appmod._pick_episode(self.mg, req)
        self.assertEqual(tgt["url"], "/v/api/ext/pan/play?fid=fid1")
        self.assertEqual(tgt["line"], pansou.PANSOU_LINE)

    def test_pansou_routes_mounted(self):
        import app as appmod
        routes = {getattr(r, "path", None) for r in appmod.app.routes}
        for p in ("/v/api/ext/pansou/share", "/v/api/ext/pansou/status",
                  "/v/api/ext/pan/play", "/v/api/ext/pan/uc/play",
                  "/v/api/ext/pan/ali/play"):
            self.assertIn(p, routes, p)


class PanP3RegressionTests(unittest.TestCase):
    """P3：只回归确认，不移植。去 guard 后三件套仍在。"""

    def test_panmatch_registry_intact(self):
        keys = [p["key"] for p in _PAN_PROVIDERS]
        self.assertEqual(keys, ["quark", "uc", "ali", "baidu", "xunlei",
                                "p115", "tianyi", "yidong"])
        self.assertEqual(_pan_match("https://pan.quark.cn/s/abc")["key"],
                         "quark")
        self.assertEqual(_pan_match("https://drive.uc.cn/s/abc")["key"], "uc")
        self.assertEqual(_pan_match("https://www.alipan.com/s/abc")["key"],
                         "ali")
        self.assertFalse(_pan_match("https://pan.baidu.com/s/abc")["playable"])
        self.assertIsNone(_pan_match("https://example.com/x"))
        m = _PAN_LINKSTART_RE.search(
            "link_start/abc?redirect_to=pan_id_12345")
        self.assertEqual(m.group(1), "12345")

    def test_pan_cleanup_covers_quark_uc_not_ali(self):
        # 与上游同款缺口：ali 不在清理循环内，本批不混进移植。
        import app as appmod
        src = inspect.getsource(appmod._pan_cache_cleanup_once)
        self.assertIn('"pan"', src)
        self.assertIn('"panuc"', src)
        self.assertNotIn("panali", src)

    @unittest.skipUnless(importlib.util.find_spec("pan") is not None,
                         "公开构建已剥离 pan 模块，注入块随之剥离")
    def test_hls_seg_pan_cookie_injection_present(self):
        # 仅完整构建断言：注入代码在私有剥离块内，公开构建按设计剥离。
        text = (Path(__file__).resolve().parents[1] / "proxy"
                / "hls_race.py").read_text(encoding="utf-8")
        self.assertIn("drive.quark.cn", text)
        self.assertIn(".uc.cn", text)
        self.assertIn("pan.quark.cn", text)


if __name__ == "__main__":
    unittest.main()
