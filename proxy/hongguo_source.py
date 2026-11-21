"""HTTP hongguo-web source adapter; independent from the local HGDJ companion."""
from __future__ import annotations

import hashlib
import urllib.parse
import httpx
import netutil
from providers import Episode, OnlineTitle

KIND = "hongguo"
STREAM_MARK = "/api/stream/"

def enabled(source: dict) -> bool:
    return str(source.get("kind") or "").lower() == KIND

def base_url(source: dict) -> str:
    return str(source.get("api") or "").strip().rstrip("/")

def new_id(url: str) -> str:
    return "hg_" + hashlib.md5(base_url({"api": url}).encode()).hexdigest()[:10]

def _headers(source: dict) -> dict:
    token = str(source.get("token") or "").strip()
    return {"User-Agent": "fnmedia-ext", "Accept": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {})}

def stream_url(source: dict, episode_id: str, quality: str = "auto") -> str:
    q = urllib.parse.urlencode({"q": quality or "auto"})
    token = str(source.get("token") or "").strip()
    if token:
        q += "&at=" + urllib.parse.quote(token, safe="")
    return f"{base_url(source)}{STREAM_MARK}{urllib.parse.quote(str(episode_id), safe='')}?{q}"

async def _get(client, source, path, params=None):
    url = base_url(source) + path
    if not url.startswith(("http://", "https://")):
        return None
    try:
        r = await client.get(url, params=params or {}, headers=_headers(source), timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError):
        return None

async def probe(source: dict) -> dict:
    async with netutil.async_client() as client:
        data = await _get(client, source, "/health")
    return {"ok": bool(data and data.get("service") == "hongguo-web"),
            "kind": KIND, "base_url": base_url(source)}

def _title(source: dict, row: dict) -> OnlineTitle | None:
    vid = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not vid or not title:
        return None
    return OnlineTitle(provider=str(source.get("id") or ""), vod_id=vid,
        title=title, type_name="短剧", year=str(row.get("year") or ""),
        area=str(row.get("area") or ""), updated="",
        remarks=str(row.get("remarks") or ""), score=str(row.get("score") or ""),
        poster=str(row.get("cover") or ""), overview=str(row.get("desc") or ""),
        actor="", director="", sources={})

def _titles(source: dict, rows) -> list[OnlineTitle]:
    return [t for row in (rows or []) if isinstance(row, dict)
            for t in [_title(source, row)] if t is not None]

async def search(client, source: dict, keyword: str) -> list[OnlineTitle]:
    data = await _get(client, source, "/api/search", {"q": keyword, "page": 1})
    return _titles(source, (data or {}).get("items"))

async def category(client, source: dict, tid: str, page: int = 1) -> list[OnlineTitle]:
    data = await _get(client, source, "/api/category", {"tid": tid, "page": page})
    return _titles(source, (data or {}).get("items"))

async def detail(client, source: dict, vod_id: str) -> OnlineTitle | None:
    data = await _get(client, source, "/api/detail", {"id": vod_id})
    if not data:
        return None
    title = _title(source, data)
    if title is None:
        return None
    lines = {}
    for line in data.get("lines") or []:
        if not isinstance(line, dict):
            continue
        name = str(line.get("name") or line.get("flag") or "红果").strip()
        eps = []
        for ep in line.get("episodes") or []:
            if not isinstance(ep, dict) or not ep.get("id"):
                continue
            url = stream_url(source, ep["id"])
            eps.append(Episode(title=str(ep.get("name") or f"第{len(eps)+1}集"),
                               url=url, direct=True,
                               headers={"Referer": base_url(source) + "/"}))
        if eps:
            lines[name] = eps
    title.sources = lines
    return title
