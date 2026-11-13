"""B 站独立内置站点：搜索、详情分P、站内浏览。

独立于海阔规则：自带一条「B站自动高清」播放线，播放由 bilibili_play
输出飞牛可直接播的合一 MP4（见该模块的选流说明）。
"""
from __future__ import annotations

import re

import httpx

from providers import Episode, OnlineTitle

import bilibili
import bilibili_play

SOURCE_ID = "bilibili"
SOURCE_NAME = "哔哩哔哩"
LINE_NAME = "B站自动高清"

_BILI_URI = "bilibili://video/"


def _headers() -> dict:
    return bilibili_play._headers()


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _search_raw(keyword: str, page: int = 1, page_size: int = 24) -> list[dict]:
    r = httpx.get(
        "https://api.bilibili.com/x/web-interface/search/type",
        params={"search_type": "video", "keyword": keyword,
                "page": int(page or 1), "page_size": int(page_size or 24)},
        headers=_headers(), timeout=12.0, follow_redirects=True)
    r.raise_for_status()
    j = r.json()
    if int(j.get("code") or 0) != 0:
        return []
    data = j.get("data") or {}
    return [x for x in (data.get("result") or []) if isinstance(x, dict)]


def _popular_raw(page: int = 1, page_size: int = 24) -> list[dict]:
    """首页：热门推荐（与搜索同一套字段形态）。"""
    r = httpx.get(
        "https://api.bilibili.com/x/web-interface/popular",
        params={"pn": int(page or 1), "ps": int(page_size or 24)},
        headers=_headers(), timeout=12.0, follow_redirects=True)
    r.raise_for_status()
    j = r.json()
    if int(j.get("code") or 0) != 0:
        return []
    data = j.get("data") or {}
    return [x for x in (data.get("list") or []) if isinstance(x, dict)]


def _map_item(raw: dict) -> OnlineTitle | None:
    bvid = str(raw.get("bvid") or "")
    if not bvid:
        return None
    title = _strip_tags(raw.get("title"))
    if not title:
        return None
    vid = _BILI_URI + bvid
    episodes = [Episode(
        title=LINE_NAME, url=vid, direct=True,
        headers={"User-Agent": "Mozilla/5.0",
                 "Referer": "https://www.bilibili.com/"},
        play_from=SOURCE_ID, xsrc=True,
    )]
    duration = raw.get("duration")
    remarks = ""
    if duration:
        # B 站 duration 有两种形态：秒数（123）或 "mm:ss"（362:0）。
        try:
            secs = int(duration)
            remarks = "%d:%02d" % (secs // 60, secs % 60)
        except (TypeError, ValueError):
            text = str(duration).strip()
            remarks = text if ":" in text else ""
    return OnlineTitle(
        provider=SOURCE_ID,
        vod_id=vid,
        title=title,
        type_name=str(raw.get("typename") or "B站"),
        year="", area="", updated="", remarks=remarks,
        score=str(raw.get("play") or ""),
        poster=str(raw.get("pic") or ""),
        overview=_strip_tags(raw.get("description"))[:200],
        actor=str(raw.get("author") or ""), director="",
        sources={LINE_NAME: episodes},
    )


async def search(client: httpx.AsyncClient, source: dict,
                 keyword: str) -> list[OnlineTitle]:
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    try:
        raw = await asyncio_to_thread(_search_raw, keyword)
    except Exception:
        return []
    out = []
    for item in raw[:24]:
        t = _map_item(item)
        if t is not None:
            out.append(t)
    return out


async def popular(client: httpx.AsyncClient, source: dict,
                  page: int = 1, page_size: int = 24) -> list[OnlineTitle]:
    try:
        raw = await asyncio_to_thread(_popular_raw, page, page_size)
    except Exception:
        return []
    out = []
    for item in raw[:page_size]:
        t = _map_item(item)
        if t is not None:
            out.append(t)
    return out


async def detail(ref: str) -> dict:
    """详情：元数据 + 真实分 P 列表（每 P 一集，指向同一 BV 的不同 P）。"""
    info = await asyncio_to_thread(bilibili_play.video_info, ref)
    pages = await asyncio_to_thread(bilibili_play.page_list, ref)
    bvid = info.get("bvid") or bilibili_play.get_bvid(ref)
    eps = []
    for idx, p in enumerate(pages or [], start=1):
        title = str(p.get("part") or "").strip() or "P%d" % idx
        eps.append(Episode(
            title=title, url="%s%s?p=%d" % (_BILI_URI, bvid, idx),
            direct=True,
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://www.bilibili.com/"},
            play_from=SOURCE_ID, xsrc=True,
        ))
    if not eps:
        eps = [Episode(title=LINE_NAME, url=_BILI_URI + bvid, direct=True,
                       headers={"User-Agent": "Mozilla/5.0",
                                "Referer": "https://www.bilibili.com/"},
                       play_from=SOURCE_ID, xsrc=True)]
    return {
        "title": str(info.get("title") or ""),
        "poster": str(info.get("pic") or ""),
        "overview": _strip_tags(info.get("desc"))[:400],
        "year": str(info.get("pubdate") or "")[:4],
        "actor": str((info.get("owner") or {}).get("name") or ""),
        "remarks": str(info.get("duration") or ""),
        "episodes": eps,
        "line": LINE_NAME,
        "bvid": bvid,
    }


def parse_page(ref: str) -> int:
    """从 bilibili://video/BVxxx?p=N 取分 P 序号。"""
    m = re.search(r"[?&]p=(\d+)", str(ref or ""))
    return int(m.group(1)) if m else 1


def asyncio_to_thread(fn, *args):
    import asyncio
    return asyncio.get_running_loop().run_in_executor(None, fn, *args)
