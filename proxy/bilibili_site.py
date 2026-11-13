"""B 站独立内置站点：搜索、详情分P、自动最高清晰度播放。"""
from __future__ import annotations

import asyncio
import re

from providers import Episode, OnlineTitle
import bilibili_play
from features import features

BILI_SOURCE = {
    "id": "bilibili", "name": "哔哩哔哩", "api": "bilibili://home",
    "enabled": True, "builtin": True, "bili_site": True, "xsrc": True,
}
_BILI_KW_RE = re.compile(r"\s+")

async def search(client, source: dict, keyword: str) -> list[OnlineTitle]:
    keyword = _BILI_KW_RE.sub(" ", str(keyword or "").strip())
    if not keyword:
        return []
    import httpx
    try:
        r = await client.post(
            "https://api.bilibili.com/x/web-interface/search/type",
            data={"search_type": "video", "keyword": keyword, "page": 1,
                  "page_size": 24},
            headers=bilibili_play._headers(), timeout=12.0,
        )
        j = r.json()
    except Exception:
        return []
    if int(j.get("code") or 0) != 0:
        return []
    data = (j.get("data") or {}).get("result") or []
    out = []
    for item in data[:24]:
        bvid = str(item.get("bvid") or "")
        if not bvid:
            continue
        title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
        if not title:
            continue
        out.append(OnlineTitle(
            provider="bilibili",
            vod_id=f"bilibili://video/{bvid}",
            title=title,
            type_name="B站",
            year=str(item.get("pubdate") or "")[:4] or "",
            area="", updated="", remarks=f"播放量 {item.get('play', '')}",
            score="", poster=str(item.get("pic") or ""),
            overview=re.sub(r"<[^>]+>", "", str(item.get("description") or ""))[:200],
            actor=str(item.get("author") or ""), director="",
            sources={"哔哩哔哩·自动高清": [
                Episode(title="B站自动高清",
                        url=f"bilibili://video/{bvid}", direct=True,
                        headers={"User-Agent": "Mozilla/5.0",
                                 "Referer": "https://www.bilibili.com/"},
                        play_from="bilibili", xsrc=True)
            ]},
        ))
    return out

async def play(bvid_or_url: str, page: int = 1) -> dict:
    return await asyncio.to_thread(bilibili_play.play, bvid_or_url, page)
