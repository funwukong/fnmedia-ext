"""TV 起播清单 8s 硬顶预算（单飞 future，对齐上游 v0.2.69）。

只测进程内路由，不碰真实网络与飞牛。预算用 monkeypatch 缩短验证，
测试本身不等满 8 秒。
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod

REAL_BODY = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
             "#EXTINF:6.0,\nhttps://cdn.test/seg1.ts\n")
MG = "mg-tv-budget"


def _picked(line="CMS1", index=0):
    return {"online_guid": "og-tv-budget", "line": line, "index": index,
            "url": "https://up.test/play.m3u8", "kind": "title"}


class _Req:
    """只满足 hls_main 的最小请求替身：query_params + 可写 state。"""

    def __init__(self, params=None):
        self.query_params = params or {}
        self.state = SimpleNamespace()


class TvMainBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.picked = _picked()
        self._patches = [
            patch.object(appmod, "_await_pending_chart",
                         new=AsyncMock(return_value="")),
            patch.object(appmod, "_pick_episode",
                         return_value=self.picked),
            patch.object(appmod, "_dodge_known_bad",
                         side_effect=lambda p, r: p),
            patch.object(appmod, "_autofail_on", return_value=False),
            patch.object(appmod, "_verify_rewritten_first_seg",
                         new=AsyncMock(return_value=True)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        appmod._MAIN_SERVE_FUT.clear()
        appmod._PLAYLIST_CACHE.clear()
        self.fetch_calls = 0

    async def asyncTearDown(self):
        # 后台 _serve 任务若未跑完，先取消，避免跨测试泄漏到已关闭的 loop。
        for fut, _dl in list(appmod._MAIN_SERVE_FUT.values()):
            if not fut.done():
                fut.cancel()
                try:
                    await fut
                except Exception:
                    pass
        appmod._MAIN_SERVE_FUT.clear()
        appmod._PLAYLIST_CACHE.clear()

    def _stub_fetch(self, delay=0.0, body=REAL_BODY):
        async def _fetch(mg, picked, request):
            self.fetch_calls += 1
            if delay:
                await asyncio.sleep(delay)
            return body
        return patch.object(appmod, "_get_playlist_cached",
                            side_effect=_fetch)

    async def _drain_serve(self, timeout=3.0):
        t0 = time.monotonic()
        while appmod._MAIN_SERVE_FUT and time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.05)

    def _body(self, resp):
        return resp.body.decode("utf-8", "replace")

    async def test_normal_fast_returns_full_playlist(self):
        """正常情况不受影响：预算内返回完整结果，底层只取一次。"""
        with self._stub_fetch():
            resp = await appmod.hls_main(MG, _Req())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._body(resp), REAL_BODY)
        self.assertEqual(self.fetch_calls, 1)
        self.assertEqual(appmod._MAIN_SERVE_FUT, {})

    async def test_budget_expiry_normal_line_returns_503_fast(self):
        """预算耗尽必有返回（普通线）：不等底层跑完，直接回明确 503。"""
        with (patch.object(appmod, "_TV_MAIN_BUDGET", 0.05),
              self._stub_fetch(delay=1.0)):
            t0 = time.monotonic()
            resp = await appmod.hls_main(MG, _Req())
            dt = time.monotonic() - t0
        self.assertLess(dt, 0.5)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers.get("Retry-After"), "3")
        self.assertIn("起播清单超时", self._body(resp))
        self.assertEqual(self.fetch_calls, 1)
        # 超时只打断等待、不取消后台任务：任务仍在跑，跑完后键被清掉。
        self.assertIn(f"{MG}|CMS1|0", appmod._MAIN_SERVE_FUT)
        await self._drain_serve()
        self.assertEqual(appmod._MAIN_SERVE_FUT, {})

    async def test_budget_expiry_slow_line_returns_placeholder(self):
        """预算耗尽必有返回（慢线）：回直播式占位，播放器可继续轮询转正。"""
        self.picked.update({"line": "drpy-test"})
        with (patch.object(appmod, "_TV_MAIN_BUDGET", 0.05),
              self._stub_fetch(delay=1.0)):
            t0 = time.monotonic()
            resp = await appmod.hls_main(MG, _Req())
            dt = time.monotonic() - t0
        self.assertLess(dt, 0.5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
        self.assertIn("_placeholder/", self._body(resp))
        await self._drain_serve()

    async def test_budget_expiry_returns_cached_partial(self):
        """预算耗尽时优先回已有的部分结果：缓存里的新鲜真实清单。"""
        key = appmod._playlist_cache_key(self.picked)
        appmod._PLAYLIST_CACHE[key] = (time.time(), REAL_BODY)
        with (patch.object(appmod, "_TV_MAIN_BUDGET", 0.05),
              self._stub_fetch(delay=1.0)):
            resp = await appmod.hls_main(MG, _Req())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._body(resp), REAL_BODY)
        await self._drain_serve()

    async def test_single_flight_dedup(self):
        """单飞去重：5 个并发请求只产生一次底层取数，都拿到同一份结果。"""
        with self._stub_fetch(delay=0.3):
            resps = await asyncio.gather(*[
                appmod.hls_main(MG, _Req()) for _ in range(5)])
        self.assertEqual(self.fetch_calls, 1)
        for resp in resps:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self._body(resp), REAL_BODY)
        self.assertEqual(appmod._MAIN_SERVE_FUT, {})

    async def test_late_joiner_shares_job_deadline(self):
        """迟到的轮询共用同一 deadline：只等剩余预算，不再各等满一份预算。"""
        with (patch.object(appmod, "_TV_MAIN_BUDGET", 0.3),
              self._stub_fetch(delay=1.0)):
            first = asyncio.ensure_future(appmod.hls_main(MG, _Req()))
            await asyncio.sleep(0.2)  # 首个请求已消耗约 0.2s 预算
            t0 = time.monotonic()
            resp2 = await appmod.hls_main(MG, _Req())
            dt2 = time.monotonic() - t0
            resp1 = await first
        self.assertEqual(resp2.status_code, 503)
        self.assertEqual(resp1.status_code, 503)
        self.assertLess(dt2, 0.25)  # 只剩约 0.1s，而非重新等满 0.3s
        self.assertEqual(self.fetch_calls, 1)
        await self._drain_serve()

    def test_cached_partial_rejects_stale_and_placeholder(self):
        """部分结果只认新鲜真实清单：过期、占位、无分片一律不要。"""
        key = appmod._playlist_cache_key(self.picked)
        appmod._PLAYLIST_CACHE[key] = (time.time() - 200, REAL_BODY)
        self.assertIsNone(appmod._cached_real_playlist(self.picked))
        ph = appmod._placeholder_playlist_response(MG).body.decode()
        appmod._PLAYLIST_CACHE[key] = (time.time(), ph)
        self.assertIsNone(appmod._cached_real_playlist(self.picked))
        appmod._PLAYLIST_CACHE[key] = (time.time(), "#EXTM3U\n")
        self.assertIsNone(appmod._cached_real_playlist(self.picked))
        appmod._PLAYLIST_CACHE[key] = (time.time(), REAL_BODY)
        self.assertEqual(appmod._cached_real_playlist(self.picked), REAL_BODY)


if __name__ == "__main__":
    unittest.main()
