"""B站独立播放模块：抽自 BiliLink 的核心思路，直接给飞牛输出可播地址。"""
from __future__ import annotations

import httpx
import re
import time
import os
from pathlib import Path

import bilibili

API = "https://api.bilibili.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REF = "https://www.bilibili.com/"
BV_RE = re.compile(r"BV[1-9A-HJ-NP-Za-km-z]{10}")

_QN_PRIORITY = [120, 116, 112, 80, 74, 64]
_QN_NAMES = {120: "4K", 116: "1080P60", 112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P"}

def _headers() -> dict:
    h = {"User-Agent": UA, "Referer": REF}
    ck = bilibili.current_cookie()
    if ck:
        h["Cookie"] = ck
    return h

def _post_json(url: str, data: dict, timeout: float = 15.0):
    r = httpx.post(url, data=data, headers=_headers(), timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    j = r.json()
    code = int(j.get("code") or 0)
    if code != 0:
        raise RuntimeError(f"B 站接口返回 code={code} msg={str(j.get('message'))[:120]}")
    return j.get("data") or {}

def _post_api(path: str, params: dict, timeout: float = 15.0):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return _post_json(f"{API}{path}?{q}", {}, timeout=timeout)

def get_bvid(url_or_id: str) -> str:
    s = str(url_or_id or "").strip()
    if s.startswith("BV"):
        m = BV_RE.match(s)
        if m:
            return m.group(0)
        raise ValueError("B 站 BV 号格式无效")
    m = BV_RE.search(s)
    if m:
        return m.group(0)
    raise ValueError("未识别 B 站视频 ID / 链接")

def video_info(bvid: str) -> dict:
    bvid = get_bvid(bvid)
    data = _post_api("/x/web-interface/wbi/view", {"bvid": bvid})
    data["bvid"] = bvid
    return data

def page_cid(bvid: str, page: int = 1) -> tuple[int, int]:
    bvid = get_bvid(bvid)
    data = _post_api("/x/player/pagelist", {"bvid": bvid})
    pages = data if isinstance(data, list) else []
    if not pages:
        raise ValueError("B 站分 P 列表为空")
    total = len(pages)
    page = int(page or 1)
    if page < 1 or page > total:
        raise ValueError(f"第 {page} P 不存在，共 {total} P")
    cid = int(pages[page - 1]["cid"])
    return cid, total

def _pick_dash(video: list, qn: int) -> dict | None:
    for item in video or []:
        if int(item.get("id") or 0) == int(qn):
            if item.get("baseUrl"):
                return item
    best = None
    for item in video or []:
        base = int(item.get("id") or 0)
        if not item.get("baseUrl"):
            continue
        if best is None or base > int(best.get("id") or 0):
            best = item
    return best

def play(bvid: str, page: int = 1, qn: int | None = None) -> dict:
    """自动选择最高可用清晰度，优先 4K/1080/720。返回 m3u8 或 MP4 直链。"""
    bvid = get_bvid(bvid)
    cid, total_pages = page_cid(bvid, page)
    last_err = None
    order = [qn] if qn else _QN_PRIORITY
    for want in order:
        want = int(want or 0)
        try:
            data = _post_api("/x/player/playurl", {
                "bvid": bvid, "cid": cid, "qn": want,
                "fnver": 0, "fnval": 16 | 4096 | 64, "fourk": 1,
            })
        except Exception as e:
            last_err = e
            continue
        dash = data.get("dash") or {}
        chosen = _pick_dash(dash.get("video"), want)
        if chosen and chosen.get("baseUrl"):
            audio = ""
            aus = dash.get("audio") or []
            if aus:
                aus = sorted(aus, key=lambda x: int(x.get("bandwidth") or 0), reverse=True)
                audio = str(aus[0].get("baseUrl") or "")
            return {
                "type": "dash" if audio else "mp4",
                "video": chosen["baseUrl"],
                "audio": audio,
                "qn": int(chosen.get("id") or want),
                "quality": _QN_NAMES.get(int(chosen.get("id") or want), _QN_NAMES.get(want, "auto")),
                "cid": cid, "page": page, "pages": total_pages,
                "headers": _headers(),
                "cookie_set": bool(bilibili.current_cookie()),
            }
        durl = data.get("durl") or []
        if durl and durl[0].get("url"):
            return {
                "type": "mp4",
                "video": durl[0]["url"],
                "audio": "",
                "qn": want,
                "quality": _QN_NAMES.get(want, "auto"),
                "cid": cid, "page": page, "pages": total_pages,
                "headers": _headers(),
                "cookie_set": bool(bilibili.current_cookie()),
            }
    if last_err:
        raise RuntimeError(f"B 站无可用清晰度: {last_err}")
    raise RuntimeError("B 站未返回可播放流")
