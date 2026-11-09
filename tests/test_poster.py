"""海报代理测试：App 端海报空白的回归覆盖。

背景：热门短剧（海量分类库）在网页端能出图、App 端全白。卡片本身给两端
的 poster 是同一个 sys/img token URL，差别在图片本身的抓取与回退：
- 旧 fallback 是 SVG：浏览器能渲染，App/TV 原生加载器解不了 → 空白；
- 上游抓取只有一次机会：图床抖动/限流就直接 fallback；
- 预热只覆盖榜单四库，海量分类从没预热过，App 一开库就几十张现抓。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

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
    written = {}
    with patch.object(appmod, "_poster_cache_read", return_value=None), \
            patch.object(appmod, "_poster_cache_write",
                         side_effect=lambda u, b: written.setdefault("u", u)):
        r = asyncio.run(appmod._serve_online_poster(
            _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.media_type == "image/jpeg"
    assert r.body == JPEG
    assert len(fake.calls) == 2  # 第一次连不上，重试后成功
    assert written.get("u") == IMG_URL


def test_serve_online_poster_403_retries_with_own_origin_referer():
    class _Gate(_FakeExt):
        async def get(self, url, headers=None, timeout=None):
            self.calls.append((url, dict(headers or {})))
            if "Referer" not in (headers or {}):
                return _Resp(status=403, content=b"forbidden",
                             ctype="text/html")
            return _Resp()

    fake = _Gate([])
    with patch.object(appmod, "_poster_cache_read", return_value=None), \
            patch.object(appmod, "_poster_cache_write",
                         side_effect=lambda u, b: None):
        r = asyncio.run(appmod._serve_online_poster(
            _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.body == JPEG
    assert len(fake.calls) == 2
    assert fake.calls[1][1].get("Referer") == "https://p.lztutu.com/"


def test_serve_online_poster_failure_returns_jpeg_fallback():
    fake = _FakeExt([httpx.ConnectError("down", request=None)] * 3)
    with patch.object(appmod, "_poster_cache_read", return_value=None), \
            patch.object(appmod, "_poster_cache_write",
                         side_effect=lambda u, b: None):
        r = asyncio.run(appmod._serve_online_poster(
            _tok(), _fake_request(fake)))
    assert r.status_code == 200
    assert r.media_type == "image/jpeg"
    assert r.body[:2] == b"\xff\xd8"
    assert b"<svg" not in r.body


def test_prefetch_poster_urls_includes_catalog():
    class _Charts:
        def items(self, cat):
            return [{"cover": "https://img.doubanio.com/view/photo/1.jpg"},
                    {"cover": "https://img.doubanio.com/view/photo/1.jpg"},
                    {"cover": "/relative/not-http.jpg"},
                    {"cover": ""}]

    class _Catalog:
        def available(self, cat):
            return True

        def cached_page_posters(self, cat, n):
            return [IMG_URL,
                    "https://img.doubanio.com/view/photo/1.jpg",
                    "not-a-url"]

    with patch.object(appmod, "chart_cache", _Charts()), \
            patch.object(appmod, "catalog", _Catalog()), \
            patch.object(appmod, "catalog_enabled", return_value=True):
        urls = asyncio.run(appmod._prefetch_poster_urls())
    assert "https://img.doubanio.com/view/photo/1.jpg" in urls
    assert IMG_URL in urls
    assert urls.count("https://img.doubanio.com/view/photo/1.jpg") == 1
    assert not any(u.startswith("/") for u in urls)
    assert all(u.startswith("http") for u in urls)
