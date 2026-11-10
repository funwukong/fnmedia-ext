"""P1-14：高频日志降级——中间件、mediadb/list、死源跳过改为采样 info。

约定：
- 正常请求：每 100 次采样 1 条 info，其余记 debug（仍可观测、不刷屏）；
- 慢请求（>=1s）与错误（>=400）：每次都记 info；
- 错误与警告日志不动。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import providers as providersmod


class _DeadHealth:
    def usable(self, source_id: str) -> bool:
        return False


class LogNoiseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        def official(request):
            return httpx.Response(200, json={"code": 0, "data": []})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(official))
        self.addAsyncCleanup(self.upstream.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="https://media.example.com")
        self.addAsyncCleanup(self.client.aclose)
        for name, value in (
            ("up_client", lambda request: self.upstream),
            ("charts_enabled", lambda: True),
            ("catalog_enabled", lambda: False),
            ("standalone_sources_active", lambda: []),
            ("chart_cache", SimpleNamespace(items=lambda cat: [])),
            ("guard_native_enabled", lambda: False),
        ):
            item = patch.object(appmod, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(appmod.fnusersmod, "resolve", return_value={
            "known": True, "guid": "admin-guid", "name": "admin",
            "is_admin": True, "token": "tok-admin"})
        item.start()
        self.addCleanup(item.stop)
        appmod._LOG_SAMPLE.clear()
        providersmod._LOG_SAMPLE.clear()

    # ---------- 采样器本身 ----------

    def test_sampled_log_every_100(self):
        for mod in (appmod, providersmod):
            with self.subTest(mod=mod.__name__):
                self.assertEqual([mod._sampled_log("k") for _ in range(99)],
                                 [False] * 99)
                self.assertIs(mod._sampled_log("k"), True)
                self.assertIs(mod._sampled_log("k"), False)

    # ---------- 中间件访问日志 ----------

    def _access_records(self, captured):
        return [r for r in captured.records
                if r.getMessage().startswith(("GET ", "POST "))]

    async def test_middleware_normal_request_is_debug_not_info(self):
        with self.assertLogs("fnmedia.app", level="DEBUG") as captured:
            r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        access = self._access_records(captured)
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0].levelname, "DEBUG")

    async def test_middleware_100th_request_is_info(self):
        appmod._LOG_SAMPLE["access"] = 99
        with self.assertLogs("fnmedia.app", level="DEBUG") as captured:
            r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        access = self._access_records(captured)
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0].levelname, "INFO")

    async def test_middleware_error_request_is_info_immediately(self):
        # 无路由会稳定返回 404（兜底透传上游 200），直接调中间件：
        # call_next 返回 404 时，第一条访问日志就必须是 info。
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        scope = {"type": "http", "method": "GET",
                 "path": "/v/api/v1/search/list", "query_string": b"q=x",
                 "headers": [(b"user-agent", b"test"),
                             (b"host", b"media.example.com")],
                 "client": ("127.0.0.1", 1234), "server": ("t", 80),
                 "scheme": "http"}
        req = Request(scope)

        async def call_next(request):
            return JSONResponse({"code": -1, "msg": "not found"},
                                status_code=404)

        with self.assertLogs("fnmedia.app", level="DEBUG") as captured:
            resp = await appmod._access(req, call_next)
        self.assertEqual(resp.status_code, 404)
        access = self._access_records(captured)
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0].levelname, "INFO")
        self.assertIn("404", access[0].getMessage())

    async def test_middleware_slow_request_is_info_immediately(self):
        with patch.object(appmod, "_ACCESS_SLOW_MS", 0):
            with self.assertLogs("fnmedia.app", level="DEBUG") as captured:
                r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        access = self._access_records(captured)
        self.assertEqual(access[0].levelname, "INFO")

    # ---------- mediadb/list ----------

    def _mediadb_records(self, captured):
        return [r for r in captured.records
                if r.getMessage().startswith("mediadb/list ")]

    async def test_mediadb_list_normal_request_is_debug(self):
        with self.assertLogs("fnmedia.app", level="DEBUG") as captured:
            r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        recs = self._mediadb_records(captured)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].levelname, "DEBUG")

    async def test_mediadb_list_100th_request_is_info(self):
        appmod._LOG_SAMPLE["mediadb_list"] = 99
        with self.assertLogs("fnmedia.app", level="DEBUG") as captured:
            r = await self.client.get("/v/api/v1/mediadb/list")
        self.assertEqual(r.status_code, 200)
        recs = self._mediadb_records(captured)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].levelname, "INFO")

    # ---------- 死源跳过 ----------

    async def test_source_skip_normal_is_debug(self):
        with patch.object(providersmod, "_health", _DeadHealth()):
            with self.assertLogs("fnmedia.providers",
                                 level="DEBUG") as captured:
                out = await providersmod.search_source(
                    None, {"id": "dead-src"}, "kw")
        self.assertEqual(out, [])
        recs = [r for r in captured.records
                if "source skipped" in r.getMessage()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].levelname, "DEBUG")

    async def test_source_skip_100th_is_info(self):
        providersmod._LOG_SAMPLE["source_skip"] = 99
        with patch.object(providersmod, "_health", _DeadHealth()):
            with self.assertLogs("fnmedia.providers",
                                 level="DEBUG") as captured:
                out = await providersmod.search_source(
                    None, {"id": "dead-src"}, "kw")
        self.assertEqual(out, [])
        recs = [r for r in captured.records
                if "source skipped" in r.getMessage()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].levelname, "INFO")


if __name__ == "__main__":
    unittest.main()
