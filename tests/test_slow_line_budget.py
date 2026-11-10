"""慢线单飞 + 预算（上游 v1.0.0 移植）：hls_main 慢线分支。

只测进程内路由，不碰真实网络与飞牛。
- 慢线（drpy- 前缀）：预算到点先回占位 200，后台继续跑完并写缓存，下次轮询转正片；
  并发轮询共用一个 _MAIN_SERVE_FUT，底层只取一次。
- 普通线：不受预算/单飞影响，慢速取流也会等到真清单。
- 预算可配：main_ready_budget 改值生效，import 时夹在 [0.8, 2.8]。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
import subprocess
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
    # create=True：普通线测试在未移植代码上也应通过（预算与它无关），
    # 此时 patch 临时建一个无读取的常量，测试按原路径行为断言。
    with patch.object(appmod, "_MAIN_SLOW_BUDGET", value, create=True):
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
        # 全新配置没有 main_ready_budget：import 时默认值 2.4。
        self.assertEqual(appmod._MAIN_SLOW_BUDGET, 2.4)

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
        # 白名单 + 落盘：键已在配置文件里时，POST 能改值（与上游 save() 语义一致：
        # 全新配置里没有该键时 save() 不会凭空建键，值靠 load() 的文件合并进入）。
        state_path = Path(configmod._STATE)
        cfg = json.loads(state_path.read_text(encoding="utf-8"))
        cfg["main_ready_budget"] = 1.5
        state_path.write_text(json.dumps(cfg, ensure_ascii=False))
        self.config.load()
        self.assertEqual(self.config.get()["main_ready_budget"], 1.5)
        resp = await self.client.post("/v/api/ext/config",
                                      json={"main_ready_budget": 1.7})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("code"), 0)
        self.assertEqual(self.config.get()["main_ready_budget"], 1.7)

    def test_budget_clamped_at_import(self):
        # import 时读取 + 夹取 [0.8, 2.8]：子进程里逐值重载 app，隔离主进程状态。
        proxy_dir = str(Path(__file__).resolve().parents[1] / "proxy")
        driver = (
            "import importlib, json, os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {proxy_dir!r})\n"
            "import config as configmod\n"
            "tmp = Path(sys.argv[1])\n"
            "configmod._STATE = tmp / 'config.json'\n"
            "import app as appmod\n"
            "out = {}\n"
            "for raw in (99, 0.1, 'junk', 1.5, None):\n"
            "    (tmp / 'config.json').write_text(\n"
            "        json.dumps({'main_ready_budget': raw}) if raw is not None else '{}')\n"
            "    configmod.config.load()\n"
            "    importlib.reload(appmod)\n"
            "    out[repr(raw)] = appmod._MAIN_SLOW_BUDGET\n"
            "print(json.dumps(out), flush=True)\n"
            "os._exit(0)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-c", driver, tmp],
                capture_output=True, text=True, timeout=180,
                cwd=str(Path(__file__).resolve().parents[1]))
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got, {"99": 2.8, "0.1": 0.8, "'junk'": 2.4,
                               "1.5": 1.5, "None": 2.4})


if __name__ == "__main__":
    unittest.main()
