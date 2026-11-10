"""慢线单飞 + 预算（上游 v1.0.0 移植）：hls_main 慢线分支。

只测进程内路由，不碰真实网络与飞牛。
- 慢线（drpy- 前缀）：预算到点先回占位 200，后台继续跑完并写缓存，下次轮询转正片；
  并发轮询共用一个 _MAIN_SERVE_FUT，底层只取一次。
- 普通线：不受预算/单飞影响，慢速取流也会等到真清单。
- 预算可配：main_ready_budget 改值即时生效（每次取 main 时实时读取），
  读取时夹在 [0.8, 2.8]，非法值回落 2.4。
"""
from __future__ import annotations

import asyncio
import gc
from contextlib import contextmanager
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import sourcehealth
import store as storemod
import t4_media
import t4_service

GOOD = {"id": "good-src", "name": "Good",
        "api": "https://good.test/api.php/provide/vod/at/json", "enabled": True}
DEAD = {"id": "dead-src", "name": "Dead",
        "api": "https://dead.test/api.php/provide/vod/at/json", "enabled": True}

REAL_PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                 "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nseg0.ts\n")

SLOW_PICKED = {"online_guid": "og-slow", "line": "drpy-x::A", "index": 0,
               "url": "https://slow.test/m.m3u8"}
FAST_PICKED = {"online_guid": "og-fast", "line": "cms::A", "index": 0,
               "url": "https://fast.test/m.m3u8"}


async def _identity_playable(picked, request, auto=False):
    return picked


async def _verify_ok(body, request, picked=None):
    return True


@contextmanager
def _budget(value):
    # 预算每次取 main 时实时读 _main_slow_budget()：测试直接替身该函数，
    # 不依赖 import 时的模块常量（P1-10：常量已删，改为实时读配置）。
    with patch.object(appmod, "_main_slow_budget", return_value=value):
        yield


def _slow_fetch(delay, calls):
    async def _fetch(mg, picked, request):
        calls.append(1)
        await asyncio.sleep(delay)
        return REAL_PLAYLIST
    return _fetch


class SlowLineBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [GOOD, DEAD],
                          "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        for module, name, value in [
            (appmod, "config", self.config), (appmod, "store", self.store),
            (appmod, "history", self.history), (storemod, "store", self.store),
            (appmod, "t4_service", t4_service.T4Service(self.store, self.config)),
            (appmod, "t4_media", t4_media.T4Media()),
            (appmod, "_schedule_routes_refresh", lambda: None),
        ]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        appmod._AGG_CACHE.clear()
        appmod._AGG_INFLIGHT.clear()
        appmod._PLAYLIST_CACHE.clear()
        # _MAIN_SERVE_FUT 只在移植后存在；getattr 守卫让本文件在未移植代码上
        # 也能跑（stash 验证时普通线测试应通过、慢线测试应失败）。
        if getattr(appmod, "_MAIN_SERVE_FUT", None) is not None:
            appmod._MAIN_SERVE_FUT.clear()
        self.addCleanup(appmod._PLAYLIST_CACHE.clear)
        if getattr(appmod, "_MAIN_SERVE_FUT", None) is not None:
            self.addCleanup(appmod._MAIN_SERVE_FUT.clear)
        sourcehealth.reset("good-src")
        sourcehealth.reset("dead-src")
        self.addCleanup(lambda: sourcehealth.reset("good-src"))
        self.addCleanup(lambda: sourcehealth.reset("dead-src"))
        appmod.app.state.ext = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(404)))
        self.addAsyncCleanup(appmod.app.state.ext.aclose)
        appmod.app.state.up = appmod.app.state.ext
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    def _patches(self, picked, fetch=None):
        """hls_main 外围替身：选集固定、探活直通、首片验活直通。"""
        stack = [
            patch.object(appmod, "_pick_episode", return_value=picked),
            patch.object(appmod, "_ensure_playable", new=_identity_playable),
            patch.object(appmod, "_verify_rewritten_first_seg",
                         new=AsyncMock(return_value=True)),
        ]
        if fetch is not None:
            stack.append(patch.object(appmod, "_get_playlist_cached",
                                      new=fetch))
        return stack

    @staticmethod
    def _enter(stack):
        for item in stack:
            item.start()

    @staticmethod
    def _exit(stack):
        for item in reversed(stack):
            item.stop()

    async def _drain_serve_fut(self, timeout=5.0):
        """等后台 _serve_normal 跑完（_MAIN_SERVE_FUT 被 done 回调清空）。"""
        deadline = time.monotonic() + timeout
        futs = getattr(appmod, "_MAIN_SERVE_FUT", None) or {}
        while futs and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertEqual(getattr(appmod, "_MAIN_SERVE_FUT", None) or {}, {})

    # ---------- (a) 慢线超时回占位 ----------

    async def test_slow_line_times_out_to_placeholder(self):
        calls = []
        stack = self._patches(SLOW_PICKED, _slow_fetch(1.0, calls))
        self._enter(stack)
        try:
            with _budget(0.4):
                t0 = time.monotonic()
                resp = await self.client.get(
                    "/v/media/online/mg-slow-a/main.m3u8", params={"index": 0})
                elapsed = time.monotonic() - t0
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
            self.assertIn("_placeholder/placeholder.ts", resp.text)
            self.assertLess(elapsed, 1.0)  # 0.4s 预算到点即回，不等 1.0s 取流
            await self._drain_serve_fut()
            self.assertEqual(len(calls), 1)  # 后台只取了一次
        finally:
            self._exit(stack)

    # ---------- (b) 并发轮询共用一个 future ----------

    async def test_slow_line_concurrent_polls_share_one_future(self):
        calls = []
        stack = self._patches(SLOW_PICKED, _slow_fetch(1.5, calls))
        self._enter(stack)
        try:
            with _budget(0.5):
                t0 = time.monotonic()
                r1, r2 = await asyncio.gather(
                    self.client.get("/v/media/online/mg-slow-b/main.m3u8",
                                    params={"index": 0}),
                    self.client.get("/v/media/online/mg-slow-b/main.m3u8",
                                    params={"index": 0}))
                elapsed = time.monotonic() - t0
            for resp in (r1, r2):
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
            self.assertLess(elapsed, 1.2)  # 两请求都在预算内回占位
            self.assertEqual(len(calls), 1)  # 底层只取一次，不是两次
            await self._drain_serve_fut()
            self.assertEqual(len(calls), 1)
        finally:
            self._exit(stack)

    # ---------- (c) 后台跑完写缓存，下次轮询命中 ----------

    async def test_slow_line_background_result_served_on_next_poll(self):
        calls = []

        async def _slow_build(mg, picked, request):
            calls.append(1)
            await asyncio.sleep(1.0)
            return REAL_PLAYLIST, None

        stack = self._patches(SLOW_PICKED)  # 真实 _get_playlist_cached
        stack.append(patch.object(appmod, "_build_playlist_body",
                                  new=_slow_build))
        self._enter(stack)
        try:
            with _budget(0.4):
                resp1 = await self.client.get(
                    "/v/media/online/mg-slow-c/main.m3u8", params={"index": 0})
            self.assertEqual(resp1.status_code, 200)
            self.assertEqual(resp1.headers.get("X-fnmedia-Placeholder"), "1")
            await self._drain_serve_fut()
            self.assertEqual(len(calls), 1)  # 后台只建了一次清单
            # 下次轮询：新 _serve_normal 经 _PLAYLIST_CACHE 直接命中真清单
            with _budget(0.4):
                resp2 = await self.client.get(
                    "/v/media/online/mg-slow-c/main.m3u8", params={"index": 0})
            self.assertEqual(resp2.status_code, 200)
            self.assertIsNone(resp2.headers.get("X-fnmedia-Placeholder"))
            self.assertIn("seg0.ts", resp2.text)
            self.assertEqual(len(calls), 1)  # 未重复建清单，命中缓存
        finally:
            self._exit(stack)

    # ---------- (d) 普通线不受预算/单飞影响 ----------

    async def test_normal_line_waits_past_budget_for_real_playlist(self):
        calls = []
        stack = self._patches(FAST_PICKED, _slow_fetch(1.0, calls))
        self._enter(stack)
        try:
            with _budget(0.4):
                t0 = time.monotonic()
                resp = await self.client.get(
                    "/v/media/online/mg-fast-d/main.m3u8", params={"index": 0})
                elapsed = time.monotonic() - t0
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(resp.headers.get("X-fnmedia-Placeholder"))
            self.assertIn("seg0.ts", resp.text)
            self.assertGreaterEqual(elapsed, 0.8)  # 等完了 1.0s 取流，未被预算截断
            self.assertEqual(len(calls), 1)
        finally:
            self._exit(stack)

    # ---------- (e) 预算可配 ----------

    def test_default_budget_is_2_4(self):
        # 全新配置：DEFAULTS 自带 main_ready_budget=2.4，实时读即 2.4。
        self.assertEqual(configmod.DEFAULTS["main_ready_budget"], 2.4)
        self.assertEqual(self.config.get()["main_ready_budget"], 2.4)
        self.assertEqual(appmod._main_slow_budget(), 2.4)

    async def test_budget_value_is_honored_live(self):
        calls = []
        stack = self._patches(SLOW_PICKED, _slow_fetch(2.0, calls))
        self._enter(stack)
        try:
            with _budget(0.9):
                t0 = time.monotonic()
                resp = await self.client.get(
                    "/v/media/online/mg-slow-e/main.m3u8", params={"index": 0})
                elapsed = time.monotonic() - t0
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
            # 0.9s 预算：明显快于默认 2.4s，也明显慢于 floor 下限
            self.assertGreaterEqual(elapsed, 0.6)
            self.assertLess(elapsed, 1.6)
            await self._drain_serve_fut()
        finally:
            self._exit(stack)

    async def test_config_endpoint_accepts_main_ready_budget(self):
        # 白名单 + 落盘 + 即时生效：全新配置已自带该键（DEFAULTS），POST 改值后
        # _main_slow_budget() 立即读到新值，无需重启/重载模块。
        self.assertEqual(self.config.get()["main_ready_budget"], 2.4)
        resp = await self.client.post("/v/api/ext/config",
                                      json={"main_ready_budget": 1.7})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("code"), 0)
        self.assertEqual(self.config.get()["main_ready_budget"], 1.7)
        self.assertEqual(appmod._main_slow_budget(), 1.7)

    async def test_config_endpoint_clamps_main_ready_budget(self):
        # POST 越界/非法值：越界夹取 [0.8, 2.8]，非法回落 2.4。
        for raw, want in ((99, 2.8), (0.1, 0.8), ("junk", 2.4)):
            resp = await self.client.post("/v/api/ext/config",
                                          json={"main_ready_budget": raw})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self.config.get()["main_ready_budget"], want)
            self.assertEqual(appmod._main_slow_budget(), want)

    def test_budget_clamped_on_read(self):
        # 每次读取时夹取 [0.8, 2.8]：直接改配置（模拟手改文件/旧值），
        # 无需重载模块即可验证。
        for raw, want in ((99, 2.8), (0.1, 0.8), ("junk", 2.4),
                          (1.5, 1.5), (None, 2.4)):
            self.config.save({"main_ready_budget": raw})
            self.assertEqual(appmod._main_slow_budget(), want)


    # ---------- (f) 后台 future 异常被取回记日志，不留 "never retrieved" ----------

    async def test_slow_line_background_exception_retrieved_and_logged(self):
        async def _slow_fail(mg, picked, request):
            calls.append(1)
            await asyncio.sleep(1.0)
            raise RuntimeError("slow boom")

        calls = []
        stack = self._patches(SLOW_PICKED, _slow_fail)
        self._enter(stack)
        try:
            bk = "mg-slow-f|drpy-x::A|0"
            appmod._MAIN_SERVE_BG_FAILS.pop(bk, None)
            # 根 logger 捕获：既要收到 fnmedia.app 的取回日志，也要能看到
            # asyncio 在 GC 时可能打的 "Future exception was never retrieved"。
            with _budget(0.4), self.assertLogs(level="WARNING") as cm:
                t0 = time.monotonic()
                resp = await self.client.get(
                    "/v/media/online/mg-slow-f/main.m3u8", params={"index": 0})
                elapsed = time.monotonic() - t0
                # 预算到点先回占位；后台继续跑，1.0s 后抛错，由 done 回调取回记日志
                await self._drain_serve_fut()
                gc.collect()
                await asyncio.sleep(0.1)
                gc.collect()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
            self.assertLess(elapsed, 1.0)  # 超时语义不变：不等后台跑完
            self.assertEqual(len(calls), 1)
            self.assertEqual(appmod._MAIN_SERVE_BG_FAILS.get(bk), 1)
            out = "\n".join(cm.output)
            self.assertIn("慢线后台取流失败", out)
            self.assertIn("slow boom", out)
            self.assertNotIn("Future exception was never retrieved", out)
        finally:
            self._exit(stack)

    # ---------- (g) done 回调只删同键的同一个 future ----------

    async def test_done_callback_keeps_new_future_for_same_key(self):
        loop = asyncio.get_running_loop()
        bk = "mg-stale|drpy-x::A|0"
        old = loop.create_future()
        new = loop.create_future()
        appmod._MAIN_SERVE_FUT[bk] = new
        appmod._MAIN_SERVE_BG_FAILS.pop(bk, None)
        try:
            old.set_exception(RuntimeError("stale boom"))
            with self.assertLogs("fnmedia.app", level="WARNING") as cm:
                appmod._main_serve_fut_done(bk, old)(old)
            # 旧 fut 的异常已取回记日志/统计，但字典里的新 fut 必须保留
            self.assertIs(appmod._MAIN_SERVE_FUT.get(bk), new)
            self.assertEqual(appmod._MAIN_SERVE_BG_FAILS.get(bk), 1)
            self.assertIn("stale boom", "\n".join(cm.output))
            # 新 fut 正常完成后，自己的回调仍能清理
            new.set_result(None)
            appmod._main_serve_fut_done(bk, new)(new)
            self.assertNotIn(bk, appmod._MAIN_SERVE_FUT)
        finally:
            appmod._MAIN_SERVE_FUT.pop(bk, None)
            appmod._MAIN_SERVE_BG_FAILS.pop(bk, None)
            if not old.done():
                old.cancel()
            if not new.done():
                new.cancel()
            gc.collect()

    async def test_done_callback_removes_same_future_on_success(self):
        loop = asyncio.get_running_loop()
        bk = "mg-ok|drpy-x::A|0"
        fut = loop.create_future()
        appmod._MAIN_SERVE_FUT[bk] = fut
        appmod._MAIN_SERVE_BG_FAILS.pop(bk, None)
        try:
            fut.set_result(None)
            appmod._main_serve_fut_done(bk, fut)(fut)
            self.assertNotIn(bk, appmod._MAIN_SERVE_FUT)
            self.assertNotIn(bk, appmod._MAIN_SERVE_BG_FAILS)
            # 已取消的 fut：回调不取异常（不抛 CancelledError），仍正常清理
            fut2 = loop.create_future()
            appmod._MAIN_SERVE_FUT[bk] = fut2
            fut2.cancel()
            appmod._main_serve_fut_done(bk, fut2)(fut2)
            self.assertNotIn(bk, appmod._MAIN_SERVE_FUT)
        finally:
            appmod._MAIN_SERVE_FUT.pop(bk, None)
            appmod._MAIN_SERVE_BG_FAILS.pop(bk, None)
            gc.collect()


if __name__ == "__main__":
    unittest.main()
