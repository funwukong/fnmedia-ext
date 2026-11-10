"""海报代理测试：App 端海报空白的回归覆盖。

背景：热门短剧（海量分类库）在网页端能出图、App 端全白（灰色胶片图标）。
与上游（yujian0815/fnmedia-ext main，v0.2.42+）逐项对比后发现本 fork
偏离了三处，上游格式是 App 端能拉到的唯一形式：
- 卡片海报地址：上游是 /online/<token>.jpg；本 fork 曾改成
  /v/api/v1/sys/img/online/（后又加 v2 前缀），App 端拉不到，改回上游格式；
- 海报服务端缓存：上游 v0.2.42 已移除磁盘缓存与全量后台预热，纯透传；
  本 fork 的 7 天磁盘缓存 + 预热循环随本次对齐一并移除；
- 图床镜像改写（HOSTS_MIRROR）：本 fork 独有，上游直接抓原地址，已移除。
保留的本 fork 改进：失败回退是 JPEG（不是 SVG，App 解不了 SVG）、
403 时带图床自身 origin 作 Referer 重试一次。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import inject

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
IMG_URL = "https://p.lztutu.com/upload/vod/20261109-1/c84dc96dbe69397adc85f677d2cb6e51.jpg"


class _Resp:
    def __init__(self, status=200, content=JPEG, ctype="image/jpeg"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}

    async def aclose(self):
        return None


class _FakeExt:
    """模拟上游图床：按预设剧本返回响应/抛异常，并记录每次请求头。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        step = self.script.pop(0) if self.script else _Resp()
        if isinstance(step, Exception):
            raise step
        return step


def _fake_request(fake_ext):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(ext=fake_ext)))


class _FakeNetutilClient:
    """模拟 netutil.async_client(timeout=...) 造出的客户端。"""

    def __init__(self, script, calls):
        self.script = list(script)
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        step = self.script.pop(0) if self.script else _Resp()
        if isinstance(step, Exception):
            raise step
        return step


def _netutil_factory(script, calls, timeouts):
    """替代 netutil.async_client：记录 timeout，返回假客户端。"""

    def _make(*, timeout=None, **kwargs):
        timeouts.append(timeout)
        return _FakeNetutilClient(script, calls)

    return _make


def _tok(url=IMG_URL):
    # inject._poster 生成的卡片海报 URL，取 token 部分当路由入参
    return inject._poster(url).split("/online/", 1)[1]


def test_poster_fallback_is_jpeg_not_svg():
    r = appmod._poster_fallback()
    assert r.media_type == "image/jpeg"
    assert r.body[:2] == b"\xff\xd8"
    assert len(r.body) > 1000
    assert b"<svg" not in r.body


def test_serve_online_poster_retries_then_succeeds():
    fake = _FakeExt([httpx.ConnectError("boom", request=None), _Resp()])
    r = asyncio.run(appmod._serve_online_poster(
        _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.media_type == "image/jpeg"
    assert r.body == JPEG
    assert len(fake.calls) == 2  # 第一次连不上，重试后成功
    # 直接抓原地址，不经过镜像改写
    assert fake.calls[0][0] == IMG_URL
    assert fake.calls[1][0] == IMG_URL


def test_serve_online_poster_403_retries_with_own_origin_referer():
    class _Gate(_FakeExt):
        async def get(self, url, headers=None, timeout=None):
            self.calls.append((url, dict(headers or {})))
            if "Referer" not in (headers or {}):
                return _Resp(status=403, content=b"forbidden",
                             ctype="text/html")
            return _Resp()

    fake = _Gate([])
    r = asyncio.run(appmod._serve_online_poster(
        _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.body == JPEG
    assert len(fake.calls) == 2
    assert fake.calls[1][1].get("Referer") == "https://p.lztutu.com/"


def test_serve_online_poster_failure_returns_jpeg_fallback():
    # 主客户端 3 次都连不上；netutil 重试也失败 → JPEG 占位。
    # netutil.async_client 必须打桩，否则会真的去联网抓图。
    fake = _FakeExt([httpx.ConnectError("down", request=None)] * 3)
    calls, timeouts = [], []
    with mock.patch.object(
            appmod.netutil, "async_client",
            _netutil_factory([httpx.ConnectError("down", request=None)],
                             calls, timeouts)):
        r = asyncio.run(appmod._serve_online_poster(
            _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.media_type == "image/jpeg"
    assert r.body[:2] == b"\xff\xd8"
    assert b"<svg" not in r.body
    assert len(calls) == 1  # netutil 按上游做法只再试一次
    assert calls[0][0] == IMG_URL
    assert timeouts == [12.0]


def test_serve_online_poster_netutil_retry_succeeds():
    """上游 _poster_fetch 做法：主客户端网络异常后，经 netutil 再取一次成功。"""
    fake = _FakeExt([httpx.ConnectError("boom", request=None)] * 3)
    calls, timeouts = [], []
    with mock.patch.object(
            appmod.netutil, "async_client",
            _netutil_factory([_Resp()], calls, timeouts)):
        r = asyncio.run(appmod._serve_online_poster(
            _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.media_type == "image/jpeg"
    assert r.body == JPEG
    assert len(fake.calls) == 3  # 主客户端内部重试 3 次都失败
    assert len(calls) == 1
    assert calls[0][0] == IMG_URL
    assert timeouts == [12.0]


def test_serve_online_poster_netutil_retry_not_used_on_http_error():
    """正常 HTTP 错误（非网络异常）不触发 netutil 重试：403 仍走 Referer 重试。"""
    class _Gate(_FakeExt):
        async def get(self, url, headers=None, timeout=None):
            self.calls.append((url, dict(headers or {})))
            if "Referer" not in (headers or {}):
                return _Resp(status=403, content=b"forbidden",
                             ctype="text/html")
            return _Resp()

    fake = _Gate([])
    calls, timeouts = [], []
    with mock.patch.object(
            appmod.netutil, "async_client",
            _netutil_factory([_Resp()], calls, timeouts)):
        r = asyncio.run(appmod._serve_online_poster(
            _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.body == JPEG
    assert calls == []  # 没走 netutil
    assert timeouts == []


def test_poster_url_is_upstream_online_format():
    """卡片海报地址与上游一致：/online/<token>.jpg，不带 sys/img 前缀、不带 v2。"""
    p = inject._poster(IMG_URL)
    assert p.startswith("/online/")
    assert not p.startswith("/v/api/v1/sys/img/")
    assert "/v2/" not in p
    assert p.endswith(".jpg")
    # /online/ 输入原样返回
    assert inject._poster("/online/abc123.jpg") == "/online/abc123.jpg"
    assert inject._poster("") == ""


def test_serve_poster_legacy_sysimg_and_v2_addresses_still_work():
    """旧版地址仍兼容：/online/ 路由、sys/img 路由、v2 sys/img 路由都能出图。"""
    tok = _tok()  # "<b64>.jpg"
    for tok_path in (tok, "online/" + tok, "online/v2/" + tok):
        fake = _FakeExt([_Resp()])
        r = asyncio.run(appmod._serve_online_poster(
            tok_path, _fake_request(fake)))
        assert r.status_code == 200, tok_path
        assert r.media_type == "image/jpeg"
        assert r.body == JPEG
        assert fake.calls[0][0] == IMG_URL


def test_poster_has_no_disk_cache_or_prefetch():
    """上游式纯透传：磁盘缓存与后台预热已移除，不应再有这些入口。"""
    for name in ("_poster_cache_read", "_poster_cache_write",
                 "_poster_cache_file", "_poster_cache_gc",
                 "_prefetch_posters", "_prefetch_poster_urls",
                 "_poster_prefetch_loop"):
        assert not hasattr(appmod, name), name
