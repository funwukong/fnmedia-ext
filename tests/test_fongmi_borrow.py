"""FongMi/TV 借鉴三项 + 全源探活 回归测试（2026-11-13）。

1) sniff._is_media 判定顺序（排除优先 → 后缀/特殊协议 → CDN 路径）
2) 补 mpd/rtmp/抖音 video/tos 识别
3) HLS 代理错误响应带 X-Fnmedia-Error 标记头
4) 探活覆盖全部源类型 + 失效自动禁用 + 已禁用源也探
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

import sniff  # noqa: E402


class SniffMediaOrderTests(unittest.TestCase):
    """_is_media 判定顺序：排除优先，避免把页面/跳转当直链。"""

    def test_plain_media_ok(self):
        for u in ("https://a.com/x.m3u8", "https://a.com/y.mp4?k=1",
                  "https://a.com/z.ts"):
            self.assertTrue(sniff._is_media(u), u)

    def test_exclusions_win_over_suffix(self):
        """带 .mp4 后缀但实为跳转/页面的，必须先否掉（FongMi 同思路）。"""
        for u in (
            "https://a.com/play?url=https://b.com/x.mp4",   # 嵌套跳转
            "https://a.com/jump?v=http://c.com/y.m3u8",     # 嵌套跳转
            "https://a.com/page.html?f=1.mp4",              # 页面
            "https://a.com/api?ac=videodiag&id=1",          # 需再解析
            "https://a.com/go.php?m=play",                  # 跳转中间件
        ):
            self.assertFalse(sniff._is_media(u), u)

    def test_new_formats_from_fongmi(self):
        """FongMi SNIFFER 里我们原先缺的几类。"""
        for u in (
            "https://a.com/live.mpd",
            "rtmp://live.example.com/app/stream",
            "https://v3-web.douyinvod.com/video/tos/cn/tos-cn-ve-15/abc/",
            "https://a.com/live/room1.m3u8",
            "https://a.com/index.m3u8?sign=xyz",
            "https://a.com/song.m4a",
            "https://a.com/aud.aac",
        ):
            self.assertTrue(sniff._is_media(u), u)

    def test_cdn_path_still_ok(self):
        self.assertTrue(sniff._is_media(
            "https://cdn.example.com/hls/9f8a7b6c5d4e/video"))

    def test_empty_and_junk(self):
        for u in ("", "   ", "not-a-url", "https://a.com/index.html"):
            self.assertFalse(sniff._is_media(u), u)


class ErrHeaderTests(unittest.TestCase):
    """HLS 代理错误响应带 X-Fnmedia-Error（借 FongMi X-TV-Proxy-Error）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "proxy" / "hls_race.py").read_text(encoding="utf-8")

    def test_helper_exists(self):
        self.assertIn("def _err_response(", self.src)
        self.assertIn("X-Fnmedia-Error", self.src)

    def test_all_error_points_use_helper(self):
        for code in ("no_hls_line", "fetch_fail", "no_line_alive", "seg_fail"):
            self.assertIn(f'"{code}"', self.src, code)

    def test_no_bare_error_response_left(self):
        for bare in ('Response("no hls line"',
                     'Response("fetch fail upstream"',
                     'Response(f"seg fail'):
            self.assertNotIn(bare, self.src, bare)

    def test_builds_response_with_header(self):
        import hls_race
        r = hls_race._err_response("boom", 502, "seg_fail")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.headers.get("X-Fnmedia-Error"), "seg_fail")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")


class ProbeAllSourcesTests(unittest.TestCase):
    """探活覆盖全部源类型 + 失效自动禁用。"""

    @classmethod
    def setUpClass(cls):
        cls.api = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
        cls.probe = (ROOT / "proxy" / "source_probe.py").read_text(
            encoding="utf-8")
        cls.prov = (ROOT / "proxy" / "providers.py").read_text(
            encoding="utf-8")

    def test_probe_covers_all_sources_not_only_cms(self):
        self.assertIn("targets = list(config.all_sources())", self.api)
        self.assertNotIn(
            "targets = [s for s in config.all_sources() if _is_pure_cms(s)]",
            self.api)

    def test_probe_dispatches_by_source_type(self):
        self.assertIn("async def probe_any_source(", self.prov)
        for kind in ('"spider"', '"drpy"', '"catvod"', '"xsrc"', '"t4"'):
            self.assertIn(kind, self.prov, kind)

    def test_disabled_sources_are_also_probed(self):
        """已禁用源也进探活列表（用户选择「一起探」）。"""
        self.assertNotIn("if s.get(\"enabled\")", self.api.split(
            "targets = list(config.all_sources())")[1][:400])

    def test_dead_source_auto_removed(self):
        """失效源/风险源直接从列表删除（用户要求删而非禁）。"""
        self.assertIn("_disable_sink", self.probe)
        self.assertIn("risky", self.probe)
        self.assertIn("def _disable(sid", self.api)
        self.assertIn("remove_source", self.api)

    def test_alive_none_never_disables(self):
        """alive=None（可访问无命中 / 运行时不具备）绝不触发删除。"""
        # 放开为区间断言：判定段允许插入注释与中间变量，但必须仍是
        # `alive is False` 才判死，且必须落到 _disable_sink。
        seg = self.probe.split("risky = bool(")[1][:800]
        self.assertIn('alive_now is False', seg)
        self.assertIn("_disable_sink", seg)

    def test_config_has_set_source_enabled(self):
        cfg = (ROOT / "proxy" / "config.py").read_text(encoding="utf-8")
        self.assertIn("def set_source_enabled(", cfg)


class ProbeAnySourceBehaviourTests(unittest.TestCase):
    """probe_any_source：CMS 走原路径，非 CMS 走分发。"""

    def test_cms_delegates_to_probe_source_alive(self):
        import providers
        called = {}

        async def _fake(src, timeout=8.0, keywords=None):
            called["kw"] = keywords
            return {"alive": True, "ok": True, "count": 3, "hit": "庆余年"}

        with patch.object(providers, "probe_source_alive", _fake):
            out = asyncio.run(providers.probe_any_source(
                {"id": "cms1", "api": "https://x.com/api.php/provide/vod"}))
        self.assertTrue(out["alive"])
        self.assertIn("kw", called)

    def test_non_cms_dispatches_search(self):
        import providers

        async def _fake_dispatch(client, src, kw):
            return [object(), object()]

        with patch.object(providers, "_dispatch_search", _fake_dispatch):
            out = asyncio.run(providers.probe_any_source(
                {"id": "spider-x", "spider_src": True,
                 "api": "csp_X", "name": "爬虫"}))
        self.assertTrue(out["alive"])
        self.assertEqual(out["count"], 2)

    def test_non_cms_runtime_failure_is_none_not_dead(self):
        import providers

        async def _boom(client, src, kw):
            raise RuntimeError("sidecar down")

        with patch.object(providers, "_dispatch_search", _boom):
            out = asyncio.run(providers.probe_any_source(
                {"id": "drpy-x", "drpy": True, "api": "drpy://x"}))
        self.assertIsNone(out["alive"])          # 不下"失效"结论
        self.assertFalse(out["reachable"])


if __name__ == "__main__":
    unittest.main()
