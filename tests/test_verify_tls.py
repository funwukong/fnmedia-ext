"""verify=False 审计（port/verify，基于 dc087de v0.2.42）：

恢复证书校验（必须校验；缺省即校验，不再显式传 verify=False）：
- proxy/pan.py / proxy/panuc.py 的 _ssl_ctx() 与 _pc() 内联上下文：目标是
  夸克/UC 官方域名，请求带登录 Cookie；防 401 靠 OP_NO_TICKET + 逐 client
  新建 + 禁 keep-alive，不是关校验。
- proxy/pan.py::resolve_linkstart：sidhub/seedog/seedhub 公开跳转站，不带凭据。
- proxy/panali.py::_client：阿里云盘开放平台官方域名，带 client_secret/token。
- proxy/app.py::_pan_stream：网盘 CDN 直链（官方 API 返回的地址），带网盘 Cookie。
- proxy/catalog.py::refresh_routes / _load_page：CMS 源抓取，与 providers.py
  已校验的 CMS 抓取走同一批主机、同一默认。

保留 verify=False（用户任意配置的主机，自签/IP/断链常见；不带凭据）：
- proxy/subfetch.py：订阅 URL / GitHub 镜像站。
- proxy/app.py ext_source_heal 的 _fetch：用户自建源 API。
- proxy/app.py 订阅代理连通探测：经用户自配代理发出，代理可能重签 TLS。

本文件只断言“客户端构造时传了什么 verify”，不碰真实网络。
"""
from __future__ import annotations

import asyncio
import ssl
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))


def _no_verify_false(kwargs: dict) -> bool:
    """httpx 缺省即校验：没传 verify，或传 True，都算校验开启。"""
    return kwargs.get("verify", True) is not False


# ---------------------------------------------------------- 夸克 / UC
def test_pan_ssl_ctx_verifies():
    """_ssl_ctx()：校验开启，只禁 Session Ticket。"""
    import pan
    import panuc

    for mod in (pan, panuc):
        ctx = mod._ssl_ctx()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
        assert ctx.options & ssl.OP_NO_TICKET


def test_resolve_linkstart_verifies():
    """resolve_linkstart：公开跳转站，不带凭据，恢复校验。"""
    import pan

    seen: dict = {}

    class _Resp:
        headers = {}
        text = "nothing here"

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            return _Resp()

    with mock.patch.object(pan.httpx, "Client", _FakeClient):
        assert pan.resolve_linkstart("abc123") == ""
    assert _no_verify_false(seen)


# ---------------------------------------------------------- 阿里云盘
def test_panali_client_verifies():
    """_client()：官方 openapi.alipan.com，带 secret/token，恢复校验。"""
    import panali

    assert panali._API == "https://openapi.alipan.com"
    seen: dict = {}

    class _Dummy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    with mock.patch.object(panali.httpx, "Client", _Dummy):
        panali._client()
    assert _no_verify_false(seen)


# ---------------------------------------------------------- 订阅抓取（保留）
def test_subfetch_keeps_verify_false():
    """subfetch：用户配置的任意订阅 URL，自签/IP/断链常见，保留并有注释。"""
    import subfetch

    seen: list[dict] = []

    class _Resp:
        status_code = 200
        content = b"hello"

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            return _Resp()

    class _FakeAsyncClient(_FakeClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    with (mock.patch.object(subfetch, "subscription_proxy", lambda: ""),
          mock.patch.object(subfetch.httpx, "Client", _FakeClient),
          mock.patch.object(subfetch.httpx, "AsyncClient", _FakeAsyncClient)):
        subfetch.sub_client()
        subfetch.async_sub_client()
        blob, hit = subfetch.fetch_first(["https://example.com/a.jar"])
    assert blob == b"hello" and hit == "https://example.com/a.jar"
    assert len(seen) == 3
    assert all(kw.get("verify") is False for kw in seen)


# ---------------------------------------------------------- 分类路由 / 深翻
def test_refresh_routes_verifies():
    """refresh_routes：两处 httpx.AsyncClient 均不再传 verify=False。"""
    import catalog

    seen: list[dict] = []

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _noop(self, client, source):
        return None

    cat = catalog.Catalog()
    with (mock.patch.object(catalog.httpx, "AsyncClient", _FakeClient),
          mock.patch.object(catalog.Catalog, "_load_source_classes", _noop),
          mock.patch.object(catalog.Catalog, "standalone_scopes",
                            lambda self: [])):
        asyncio.run(cat.refresh_routes(
            [{"id": "s1", "api": "https://cms.example.com/api"}]))
    assert len(seen) == 2
    assert all(_no_verify_false(kw) for kw in seen)


# ---------------------------------------------------------- 网盘播放代理
def test_pan_stream_verifies():
    """_pan_stream：m3u8 清单与 mp4 流式代理均不再传 verify=False。"""
    import app as appmod
    import pan as panmod
    from fastapi.testclient import TestClient

    seen: list[dict] = []

    class _Upstream:
        status_code = 200
        headers = {}

        async def aiter_raw(self):
            yield b"video-bytes"

        async def aclose(self):
            pass

    class _Resp:
        status_code = 200
        text = "#EXTM3U\n#EXT-X-VERSION:3\n"
        content = b"#EXTM3U\n"
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def build_request(self, method, url, headers=None):
            return (method, url, headers)

        async def send(self, request, stream=False):
            return _Upstream()

        async def get(self, url, headers=None):
            return _Resp()

        async def aclose(self):
            pass

    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        # m3u8 清单分支：取清单并绝对化后直接返回。
        with (mock.patch.object(panmod, "playable",
                                lambda fid: {"url": "https://cdn.example.com/x.m3u8",
                                             "resolution": "原画", "m3u8": True}),
              mock.patch.object(panmod, "auth_cookie_or_raise",
                                lambda: "test-cookie"),
              mock.patch.object(appmod.httpx, "AsyncClient", _FakeAsyncClient)):
            r = cli.get("/v/api/ext/pan/play?fid=f1")
            assert r.status_code == 200
            assert "#EXTM3U" in r.text
        # mp4 流式分支：透传上游字节流。
        with (mock.patch.object(panmod, "playable",
                                lambda fid: {"url": "https://cdn.example.com/x.mp4",
                                             "resolution": "", "m3u8": False}),
              mock.patch.object(panmod, "auth_cookie_or_raise",
                                lambda: "test-cookie"),
              mock.patch.object(appmod.httpx, "AsyncClient", _FakeAsyncClient)):
            r = cli.get("/v/api/ext/pan/play?fid=f2")
            assert r.status_code == 200
            assert r.content == b"video-bytes"
    assert len(seen) == 2
    assert all(_no_verify_false(kw) for kw in seen)
