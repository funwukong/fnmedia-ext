"""netutil 接入剩余三处（移植来源：上游 yujian0815/fnmedia-ext main v1.0.0）：

1. 分类深翻 proxy/catalog.py::_load_page：深翻抓取经
   netutil.async_client(verify=False, timeout=HTTP_TIMEOUT)，与上游
   catalog.py 逐行一致；T4 分支不在深翻路径内（refresh_routes 入口已
   过滤 kind == "t4"），未动。
2. dduo 探测 proxy/providers.py::dduo_probe_source：经
   netutil.async_client(timeout=timeout)；上游无 DDUO 模块，搬的是上游
   通用 probe_source 的客户端构造方式。
3. 海报重试见 tests/test_poster.py（_poster_upstream_get 的 netutil 重试）。
4. 分类路由刷新 proxy/catalog.py::Catalog.refresh_routes：建库分类表与
   首选叶子探测的两处客户端均经 netutil.async_client(verify=False,
   timeout=HTTP_TIMEOUT)，与上游 catalog.py 同位置一致。

只断言“客户端走 netutil 构造”，不碰真实网络：netutil.async_client
与 dduo.search 均用假对象替换。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog
import providers

CMS_API = "https://cms.example.com/api.php/provide/vod/at/json"


def _vod(vod_id: str):
    return {
        "vod_id": vod_id,
        "vod_name": f"测试电影{vod_id}",
        "vod_play_from": "线路1",
        "vod_play_url": f"第1集$https://cdn.example.com/{vod_id}.m3u8",
    }


class _CmsResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeCmsClient:
    """模拟 CMS 站：按分类 type_id 返回不同 vod，记录每次请求。"""

    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((str(url), dict(params or {})))
        type_id = str((params or {}).get("t", "1"))
        return _CmsResp({"code": 1, "pagecount": 3,
                         "list": [_vod(type_id)]})


class _RecordingFactory:
    """替代 netutil.async_client：记录构造参数，返回给定的假客户端。"""

    def __init__(self, client):
        self.client = client
        self.kwargs: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        return self.client


def _catalog_with_routes(scope, routes):
    cat = catalog.Catalog()
    cat._routes[catalog.Catalog._scope_key(scope, "movie")] = routes
    # _source_by_id 平时读 config.all_sources()，测试里直接给假源。
    cat._source_by_id = lambda sid: {"id": sid, "api": CMS_API}
    return cat


def test_load_page_aggregate_uses_netutil_client():
    """跨源聚合串行回退：客户端由 netutil.async_client 构造。"""
    cat = _catalog_with_routes(
        catalog.AGG_SCOPE,
        [{"source_id": "s1", "type_id": "1", "type_name": "电影"}])
    fake = _FakeCmsClient()
    factory = _RecordingFactory(fake)
    with mock.patch.object(catalog.netutil, "async_client", factory):
        briefs = asyncio.run(cat._load_page(catalog.AGG_SCOPE, "movie", 1))
    assert len(briefs) == 1
    assert briefs[0]["title"] == "测试电影1"
    assert factory.kwargs == [{"verify": False,
                               "timeout": catalog.HTTP_TIMEOUT}]
    assert len(fake.calls) == 1
    assert fake.calls[0][0].startswith("https://cms.example.com/")


def test_load_page_standalone_multileaf_uses_netutil_client():
    """单源独立库多叶子合并：同一页并发抓取也走 netutil.async_client。"""
    cat = _catalog_with_routes(
        "s1",
        [{"source_id": "s1", "type_id": "1", "type_name": "电影"},
         {"source_id": "s1", "type_id": "2", "type_name": "剧集"}])
    fake = _FakeCmsClient()
    factory = _RecordingFactory(fake)
    with mock.patch.object(catalog.netutil, "async_client", factory):
        briefs = asyncio.run(cat._load_page("s1", "movie", 1))
    # 两叶子各返回一条不同 vod，合并去重后两条。
    assert sorted(b["title"] for b in briefs) == ["测试电影1", "测试电影2"]
    assert factory.kwargs == [{"verify": False,
                               "timeout": catalog.HTTP_TIMEOUT}]
    assert len(fake.calls) == 2


def test_dduo_probe_source_uses_netutil_client():
    """dduo 探测：搜索经 netutil.async_client(timeout=timeout) 发起。"""
    fake_client = _FakeCmsClient()
    factory = _RecordingFactory(fake_client)
    seen: dict = {}

    async def _fake_search(client, source, keyword):
        seen["client"] = client
        seen["keyword"] = keyword
        return [{"vod_name": "庆余年"}]

    mod = types.ModuleType("dduo")
    mod.search = _fake_search
    with (mock.patch.object(providers.netutil, "async_client", factory),
          mock.patch.dict(sys.modules, {"dduo": mod})):
        res = asyncio.run(providers.dduo_probe_source(
            {"id": "d1", "api": "https://dduo.example.com"},
            keyword="庆余年", timeout=10.0))
    assert res["ok"] is True
    assert res["status"] == 200
    assert res["count"] == 1
    assert res["sample"] == "庆余年"
    assert factory.kwargs == [{"timeout": 10.0}]
    # dduo.search 拿到的是 netutil 工厂造出的那个客户端。
    assert seen["client"] is fake_client
    assert seen["keyword"] == "庆余年"


def test_dduo_probe_source_empty_result_still_ok():
    """dduo 探测：接口正常但该词无结果时同样走 netutil 且判在线。"""
    fake_client = _FakeCmsClient()
    factory = _RecordingFactory(fake_client)

    async def _fake_search(client, source, keyword):
        return []

    mod = types.ModuleType("dduo")
    mod.search = _fake_search
    with (mock.patch.object(providers.netutil, "async_client", factory),
          mock.patch.dict(sys.modules, {"dduo": mod})):
        res = asyncio.run(providers.dduo_probe_source(
            {"id": "d1", "api": "https://dduo.example.com"}))
    assert res["ok"] is True
    assert res["count"] == 0
    assert res["error"] == "接口正常（该词无结果）"
    assert factory.kwargs == [{"timeout": 10.0}]


class _FakeCmsClientWithClasses(_FakeCmsClient):
    """refresh_routes 用：分类表（ac=list）与分类页（ac=detail）都服务。"""

    async def get(self, url, params=None, headers=None, timeout=None):
        # 分类表自己记一次；分类页交给父类记，避免同一请求记两次。
        if (params or {}).get("ac") == "list":
            self.calls.append((str(url), dict(params or {})))
            return _CmsResp({"code": 1, "class": [
                {"type_id": "1", "type_pid": "0", "type_name": "电影"}]})
        return await super().get(url, params=params, headers=headers,
                                 timeout=timeout)


def test_refresh_routes_uses_netutil_client():
    """分类路由刷新：建库分类表 + 首选叶子探测都走 netutil.async_client。"""
    cat = catalog.Catalog()
    cat._source_by_id = lambda sid: {"id": sid, "api": CMS_API}
    # 4K 派生预热只在 uhd 密集列表为空时触发；预置一条，测刷新本身不跑后台抓取。
    cat._dense[catalog.Catalog._scope_key(catalog.AGG_SCOPE, "uhd")] = [
        {"guid": "g1"}]
    fake = _FakeCmsClientWithClasses()
    factory = _RecordingFactory(fake)
    with mock.patch.object(catalog.netutil, "async_client", factory):
        status = asyncio.run(cat.refresh_routes(
            [{"id": "s1", "api": CMS_API}]))
    # 两处客户端（分类表 + 叶子探测）都由 netutil 构造，参数与 _load_page 一致。
    assert factory.kwargs == [{"verify": False,
                               "timeout": catalog.HTTP_TIMEOUT}] * 2
    routes = cat._routes[catalog.Catalog._scope_key(
        catalog.AGG_SCOPE, "movie")]
    assert routes and routes[0]["source_id"] == "s1"
    assert routes[0]["type_id"] == "1"
    assert status["categories"]["movie"]["routes"] >= 1
    assert len(fake.calls) == 2
    assert fake.calls[0][1].get("ac") == "list"
    assert fake.calls[1][1].get("ac") == "detail"
