"""海报镜像域 + 分类顺序 + 路由状态重校验回归。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import catalog  # noqa: E402


def test_overseas_anime_compound_names_go_to_anime():
    """Netflix动漫/海外动漫 这类复合类目必须进动漫库，不得串进海外剧集。"""
    for n in ("Netflix动漫", "Netflix动画", "海外动漫", "海外动画",
              "海外番剧", "奈飞动漫", "日韩动漫"):
        assert catalog.classify(n) == "anime", (n, catalog.classify(n))


def test_genuine_overseas_still_overseas():
    for n in ("海外剧", "美剧", "英剧", "韩剧", "Netflix剧", "美国剧"):
        assert catalog.classify(n) == "overseas", (n, catalog.classify(n))


def test_anime_plain_still_anime():
    for n in ("玄幻动漫", "国产动漫", "日本动漫", "动漫", "动画", "番剧"):
        assert catalog.classify(n) == "anime", (n, catalog.classify(n))


def test_shortdrama_theater_unchanged():
    assert catalog.classify("海外剧场") == "shortdrama"


def test_poster_mirror_rewrites_wsyzy_hosts():
    import adfilter
    assert adfilter.mirror_poster_url("https://img1.wsyzy.org/2026/a.jpg") == \
        "https://fan.cloudflare.182682.xyz/2026/a.jpg"
    assert adfilter.mirror_poster_url("https://fiizvfck.top/x.png") == \
        "https://fan.cloudflare.182682.xyz/x.png"
    # 非镜像图床原样返回
    assert adfilter.mirror_poster_url("https://douban.com/x.jpg") == \
        "https://douban.com/x.jpg"


def test_app_poster_fetch_applies_mirror_before_upstream():
    """app._poster_upstream_get 必须在抓取前替换镜像域（而不是 403 后补救）。"""
    src = (ROOT / "proxy" / "app.py").read_text(encoding="utf-8")
    i_upstream = src.index("async def _poster_upstream_get")
    body = src[i_upstream:i_upstream + 2500]
    assert "mirror_poster_url" in body
    # 镜像替换应在第一次 ext_get_retry 之前
    assert body.index("mirror_poster_url") < body.index("ext_get_retry")
