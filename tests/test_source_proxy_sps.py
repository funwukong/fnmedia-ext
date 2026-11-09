"""移植回归：上游 v0.2.46/55 取源代理 netutil、v0.2.70 搜索够快即返回、
v0.2.56 SPS 去广告指纹。

只测本移植引入的行为；不碰真实 socket/网络/飞牛，不碰播放竞速
（_race_fastest/_line_score/_LINE_FAST）——起播路径另有守卫测试，
断言 _build_playlist_body 不调用 filter_hls_async（与上游 v0.2.42 一致：
起播只跑同步规则层）。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter
import config as cfgmod
import netutil
import providers
import sps


# ---- 构造最小 SPS NAL（Baseline profile，无 VUI）--------------------------
class _BW:
    def __init__(self):
        self.bits = []

    def u(self, v, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((v >> i) & 1)

    def ue(self, v):
        c = v + 1
        n = c.bit_length() - 1
        self.u(0, n)
        self.u(c, n + 1)

    def bytes(self):
        b = self.bits + [1]  # rbsp stop bit
        while len(b) % 8:
            b.append(0)
        out = bytearray()
        for i in range(0, len(b), 8):
            x = 0
            for bit in b[i:i + 8]:
                x = x * 2 + bit
            out.append(x)
        return bytes(out)


def make_sps_annexb(w, h):
    """给定宽高的 SPS，包成 Annex-B 分片头字节。"""
    bw = _BW()
    bw.u(66, 8)
    bw.u(0, 8)
    bw.u(30, 8)
    bw.ue(0)  # sps id
    bw.ue(0)  # log2_max_frame_num_minus4
    bw.ue(0)  # poc type
    bw.ue(0)  # log2_max_pic_order_cnt_lsb_minus4
    bw.ue(1)  # max_num_ref_frames
    bw.u(0, 1)  # gaps flag
    bw.ue(w // 16 - 1)
    mh = (h + 15) // 16
    bw.ue(mh - 1)
    bw.u(1, 1)  # frame_mbs_only
    bw.u(1, 1)  # direct_8x8
    crop = mh * 16 - h
    if crop:
        bw.u(1, 1)
        bw.ue(0)
        bw.ue(0)
        bw.ue(0)
        bw.ue(crop // 2)
    else:
        bw.u(0, 1)
    bw.u(0, 1)  # vui absent
    return b"\x00\x00\x00\x01\x67" + bw.bytes()


def _cfg_with(**kw):
    """让 netutil 读到指定的 source_proxy/source_proxy_mode。"""
    base = {"source_proxy": "", "source_proxy_mode": "auto"}
    base.update(kw)
    return patch.object(cfgmod.config, "get", return_value=base)


class NetutilConfigTests(unittest.TestCase):
    def test_defaults_are_direct_auto(self):
        with _cfg_with():
            self.assertEqual(netutil.source_proxy(), "")
            self.assertEqual(netutil.proxy_mode(), "auto")

    def test_bad_proxy_url_rejected_without_write(self):
        ok, _msg = cfgmod.config.set_source_proxy("not-a-url")
        self.assertFalse(ok)
        ok, _msg = cfgmod.config.set_source_proxy("ftp://host:21")
        self.assertFalse(ok)

    def test_bad_mode_rejected_without_write(self):
        ok, _msg = cfgmod.config.set_source_proxy_mode("sometimes")
        self.assertFalse(ok)

    def test_config_defaults_carry_new_keys(self):
        self.assertIn("source_proxy", cfgmod.DEFAULTS)
        self.assertIn("source_proxy_mode", cfgmod.DEFAULTS)
        self.assertEqual(cfgmod.DEFAULTS["source_proxy_mode"], "auto")
        self.assertIn("ad_filter_sps", cfgmod.DEFAULTS)
        self.assertTrue(cfgmod.DEFAULTS["ad_filter_sps"])

    def test_off_mode_builds_plain_client(self):
        with _cfg_with(source_proxy="http://127.0.0.1:9",
                       source_proxy_mode="off"):
            c = netutil.async_client()
            self.assertIsInstance(c, httpx.AsyncClient)
            self.assertNotIsInstance(c, netutil._AdaptiveAsyncClient)
            asyncio.run(c.aclose())

    def test_on_mode_builds_proxied_client(self):
        with _cfg_with(source_proxy="http://127.0.0.1:9",
                       source_proxy_mode="on"):
            c = netutil.async_client()
            self.assertIsInstance(c, httpx.AsyncClient)
            self.assertNotIsInstance(c, netutil._AdaptiveAsyncClient)
            # 显式代理时 httpx 为代理挂一个 mount；直连客户端为空
            self.assertGreater(len(c._mounts), 0)
            asyncio.run(c.aclose())

    def test_auto_without_proxy_builds_plain_client(self):
        with _cfg_with():
            c = netutil.async_client()
            self.assertIsInstance(c, httpx.AsyncClient)
            self.assertNotIsInstance(c, netutil._AdaptiveAsyncClient)
            self.assertEqual(len(c._mounts), 0)
            asyncio.run(c.aclose())

    def test_auto_with_proxy_builds_adaptive_client(self):
        with _cfg_with(source_proxy="http://127.0.0.1:9",
                       source_proxy_mode="auto"):
            c = netutil.async_client()
            self.assertIsInstance(c, netutil._AdaptiveAsyncClient)
            asyncio.run(c.aclose())


class AdaptiveClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        netutil.clear_proxy_hosts()

    def tearDown(self):
        netutil.clear_proxy_hosts()

    def _clients(self):
        direct_calls, proxy_calls = [], []

        def direct_handler(request):
            direct_calls.append(str(request.url))
            raise httpx.ConnectError("boom", request=request)

        def proxy_handler(request):
            proxy_calls.append(str(request.url))
            return httpx.Response(200, text="ok")

        direct = httpx.AsyncClient(
            transport=httpx.MockTransport(direct_handler))
        proxied = httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler))
        return (netutil._AdaptiveAsyncClient(direct, proxied),
                direct_calls, proxy_calls)

    async def test_conn_error_fails_over_to_proxy_and_learns_host(self):
        client, direct_calls, proxy_calls = self._clients()
        try:
            r = await client.get("https://blocked.example/x")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.text, "ok")
            self.assertTrue(netutil.host_needs_proxy("blocked.example"))
            # 第二次直接走代理，不再试直连
            r2 = await client.get("https://blocked.example/y")
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(len(direct_calls), 1)
            self.assertEqual(len(proxy_calls), 2)
        finally:
            await client.aclose()

    async def test_http_error_does_not_fail_over(self):
        def err_handler(request):
            return httpx.Response(500, text="nope")

        direct = httpx.AsyncClient(
            transport=httpx.MockTransport(err_handler))
        proxied = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text="ok")))
        client = netutil._AdaptiveAsyncClient(direct, proxied)
        try:
            r = await client.get("https://fine.example/x")
            self.assertEqual(r.status_code, 500)
            self.assertFalse(netutil.host_needs_proxy("fine.example"))
        finally:
            await client.aclose()

    def test_recheck_expiry_reopens_direct(self):
        netutil.mark_host_proxy("stale.example")
        self.assertTrue(netutil.host_needs_proxy("stale.example"))
        netutil._proxy_hosts["stale.example"] = (
            time.time() - netutil._RECHECK_SECONDS - 1)
        self.assertFalse(netutil.host_needs_proxy("stale.example"))
        netutil.clear_proxy_hosts()
        self.assertFalse(netutil.host_needs_proxy("stale.example"))

    async def test_blocking_source_uses_dedicated_executor(self):
        name = await netutil.run_blocking_source(
            lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("blocking-source"), name)


class SpsParseTests(unittest.TestCase):
    def test_parse_resolutions(self):
        self.assertEqual(sps.parse_media_profile_head(
            make_sps_annexb(1920, 1080)),
            {"width": 1920, "height": 1080})
        self.assertEqual(sps.parse_media_profile_head(
            make_sps_annexb(1280, 720)),
            {"width": 1280, "height": 720})
        self.assertEqual(sps.parse_media_profile_head(
            make_sps_annexb(640, 360)),
            {"width": 640, "height": 360})

    def test_parse_garbage_returns_none(self):
        self.assertIsNone(sps.parse_media_profile_head(b""))
        self.assertIsNone(sps.parse_media_profile_head(b"not a segment"))
        self.assertIsNone(sps.parse_media_profile_head(None))

    def test_profile_key_and_same_profile(self):
        self.assertEqual(
            sps.profile_key({"width": 1920, "height": 1080, "fps": 25.0}),
            "1920x1080@25.0fps")
        self.assertTrue(sps.same_profile(
            {"width": 1920, "height": 1080},
            {"width": 1920, "height": 1080}))
        self.assertFalse(sps.same_profile(
            {"width": 1920, "height": 1080},
            {"width": 1280, "height": 720}))
        # 帧率差 < 1.5 视为一致，差得多则不一致
        self.assertTrue(sps.same_profile(
            {"width": 1920, "height": 1080, "fps": 25.0},
            {"width": 1920, "height": 1080, "fps": 26.0}))
        self.assertFalse(sps.same_profile(
            {"width": 1920, "height": 1080, "fps": 25.0},
            {"width": 1920, "height": 1080, "fps": 30.0}))


def _playlist(blocks):
    """blocks: [(prefix, n_segments, dur_each)]，块间以 DISCONTINUITY 分隔。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for bi, (prefix, n, dur) in enumerate(blocks):
        if bi:
            lines.append("#EXT-X-DISCONTINUITY")
        for i in range(n):
            lines.append(f"#EXTINF:{dur},")
            lines.append(f"{prefix}{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


class AdfilterSpsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        adfilter.reset_sps_cache()

    def tearDown(self):
        adfilter.reset_sps_cache()

    async def _fetch_head(self, url, limit, timeout):
        if "/ad" in url or "ad3_" in url:
            return make_sps_annexb(1280, 720)
        return make_sps_annexb(1920, 1080)

    async def test_sps_drops_recoded_ad_block(self):
        text = _playlist([("main0_", 10, 10.0), ("main1_", 10, 10.0),
                          ("main2_", 10, 10.0), ("ad3_", 3, 10.0)])
        new_text, removed = await adfilter.filter_hls_async(
            text, "https://media.test/list.m3u8", self._fetch_head)
        self.assertEqual(removed, 3)
        self.assertNotIn("ad3_0.ts", new_text)
        self.assertNotIn("ad3_2.ts", new_text)
        self.assertIn("main0_0.ts", new_text)
        self.assertIn("main2_9.ts", new_text)

    async def test_sps_disabled_keeps_everything(self):
        text = _playlist([("main0_", 10, 10.0), ("main1_", 10, 10.0),
                          ("main2_", 10, 10.0), ("ad3_", 3, 10.0)])
        calls = []

        async def counting_fetch(url, limit, timeout):
            calls.append(url)
            return await self._fetch_head(url, limit, timeout)

        with patch.object(adfilter, "sps_enabled", return_value=False):
            new_text, removed = await adfilter.filter_hls_async(
                text, "https://media.test/list.m3u8", counting_fetch)
        self.assertEqual(calls, [])
        self.assertIn("ad3_0.ts", new_text)
        self.assertEqual(removed, 0)

    async def test_sps_probe_failure_keeps_playlist(self):
        text = _playlist([("main0_", 10, 10.0), ("main1_", 10, 10.0),
                          ("main2_", 10, 10.0), ("ad3_", 3, 10.0)])

        async def failing_fetch(url, limit, timeout):
            return None

        new_text, removed = await adfilter.filter_hls_async(
            text, "https://media.test/list.m3u8", failing_fetch)
        self.assertIn("ad3_0.ts", new_text)
        self.assertEqual(removed, 0)

    def test_sps_enabled_defaults_on(self):
        self.assertTrue(adfilter.sps_enabled())


class EarlyReturnTests(unittest.IsolatedAsyncioTestCase):
    def _titles(self, n):
        return [providers.OnlineTitle(
            provider="t", vod_id=f"v{i}", title=f"片{i}",
            type_name="", year="", area="", updated="", remarks="",
            score="", poster="", overview="", actor="", director="",
            sources={}) for i in range(n)]

    async def test_fast_sources_cancel_slow_source(self):
        cancelled = asyncio.Event()

        async def fake_dispatch(client, source, keyword):
            sid = source["id"]
            if sid == "slow":
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return []
            return self._titles(10)

        sources = [{"id": f"fast{i}", "enabled": True} for i in range(3)]
        sources.append({"id": "slow", "enabled": True})
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(side_effect=fake_dispatch)):
            out = await providers.search_all("测试", sources, soft_timeout=5.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(out), 30)
        self.assertLess(elapsed, 4.0)
        self.assertTrue(cancelled.is_set())

    async def test_few_results_wait_for_budget(self):
        cancelled = asyncio.Event()

        async def fake_dispatch(client, source, keyword):
            if source["id"] == "slow":
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return []
            await asyncio.sleep(0.2)
            return self._titles(5)

        sources = [{"id": "only", "enabled": True},
                   {"id": "slow", "enabled": True}]
        t0 = time.monotonic()
        with patch.object(providers, "_dispatch_search",
                          new=AsyncMock(side_effect=fake_dispatch)):
            out = await providers.search_all("冷门词", sources, soft_timeout=1.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(out), 5)
        # 结果不够铺满首屏且仍有源在途：不提前收手，等满预算才取消慢源返回
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertTrue(cancelled.is_set())


class PlaybackGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_playlist_body_does_not_probe_sps(self):
        import app as appmod

        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
                "#EXTINF:4,\na.ts\n#EXTINF:4,\nb.ts\n#EXT-X-ENDLIST\n")
        picked = {"online_guid": "og", "line": "lzi::lzm3u8", "index": 0,
                  "url": "https://media.test/list.m3u8", "headers": {}}
        req = Request({"type": "http", "method": "GET",
                       "path": "/online/mg/main.m3u8",
                       "headers": [], "app": appmod.app})
        req.state._play_mg = "mg"
        async_filter = AsyncMock(
            side_effect=AssertionError("起播不应读取SPS分片"))
        with patch.object(appmod, "_resolve_if_needed",
                          new=AsyncMock()), \
                patch.object(appmod, "_fetch_playlist", new=AsyncMock(
                    return_value=(text, picked["url"]))), \
                patch.object(adfilter, "enabled", return_value=True), \
                patch.object(adfilter, "filter_hls",
                             new=Mock(return_value=(text, 0))), \
                patch.object(adfilter, "filter_hls_async",
                             new=async_filter):
            body, _ = await appmod._build_playlist_body("mg", picked, req)
        self.assertIn("/v/media/online/mg/seg?", body)
        async_filter.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
