"""B站独立播放模块：输出飞牛可直接播放的地址。

选流策略（2026-11-13 实测）：
  * 飞牛播放器不能播 DASH（视频/音频分离），因此**优先 fnval=1 的合一 MP4**；
    实测未登录时 qn=120/80/64 返回的 durl 都是 1080P/720P 合一 MP4。
  * DASH（fnval=16/4048）只作为兜底：登录后拿不到合一 MP4 时才用，
    但双轨在飞牛侧无法直接播，需要合流，因此只在能拿到 durl 时才用。
  * 清晰度自动从高到低尝试：4K(120) → 1080P60(116) → 1080P+(112)
    → 1080P(80) → 720P60(74) → 720P(64)，取第一个真实可播的。

登录态：扫码保存的 Cookie（含 SESSDATA/bili_jct）由 bilibili 模块提供，
未登录时最高 720P，登录后按账号权限上探 4K/1080P。
"""
from __future__ import annotations

import re

import httpx

import bilibili

API = "https://api.bilibili.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REF = "https://www.bilibili.com/"
BV_RE = re.compile(r"BV[1-9A-HJ-NP-Za-km-z]{10}")

# 清晰度优先级（高 → 低）
_QN_PRIORITY = [120, 116, 112, 80, 74, 64]
_QN_NAMES = {127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K",
             116: "1080P60", 112: "1080P+", 80: "1080P", 74: "720P60",
             64: "720P", 32: "480P", 16: "360P"}


def _headers() -> dict:
    h = {"User-Agent": UA, "Referer": REF}
    ck = bilibili.current_cookie()
    if ck:
        h["Cookie"] = ck
    return h


def _get_json(url: str, params: dict, timeout: float = 15.0) -> dict:
    r = httpx.get(url, params=params, headers=_headers(), timeout=timeout,
                  follow_redirects=True)
    r.raise_for_status()
    j = r.json()
    code = int(j.get("code") or 0)
    if code != 0:
        raise RuntimeError("B 站接口 code=%s msg=%s"
                           % (code, str(j.get("message"))[:120]))
    return j.get("data") or {}


def get_bvid(url_or_id: str) -> str:
    s = str(url_or_id or "").strip()
    if s.startswith("bilibili://"):
        s = s.split("bilibili://", 1)[1].lstrip("/")
        s = s.split("?", 1)[0]
    m = BV_RE.search(s)
    if m:
        return m.group(0)
    if s.startswith("av"):
        return s
    raise ValueError("未识别 B 站视频 ID / 链接")


def ref_page(url_or_id: str) -> int:
    """从 bilibili://video/BVxxx?p=N 取分 P 序号（缺省 1）。"""
    m = re.search(r"[?&]p=(\d+)", str(url_or_id or ""))
    return int(m.group(1)) if m else 1


def video_info(ref: str) -> dict:
    """视频元数据（含分 P 列表）。"""
    bvid = get_bvid(ref)
    if bvid.startswith("av"):
        data = _get_json(f"{API}/x/web-interface/view", {"aid": bvid[2:]})
    else:
        data = _get_json(f"{API}/x/web-interface/wbi/view", {"bvid": bvid})
    data["bvid"] = bvid
    return data


def page_list(ref: str) -> list[dict]:
    """分 P 列表（含 cid）。"""
    bvid = get_bvid(ref)
    if bvid.startswith("av"):
        data = _get_json(f"{API}/x/player/pagelist", {"aid": bvid[2:]})
    else:
        data = _get_json(f"{API}/x/player/pagelist", {"bvid": bvid})
    return data if isinstance(data, list) else []


def page_cid(ref: str, page: int = 1) -> tuple[int, int]:
    pages = page_list(ref)
    if not pages:
        raise ValueError("B 站分 P 列表为空")
    total = len(pages)
    page = int(page or 1)
    if page < 1 or page > total:
        raise ValueError("第 %d P 不存在，共 %d P" % (page, total))
    return int(pages[page - 1]["cid"]), total


def _durl_playurl(ref: str, cid: int, qn: int) -> dict | None:
    """取合一 MP4（fnval=1）：飞牛播放器能直接播。"""
    data = _get_json(f"{API}/x/player/playurl", {
        "bvid": get_bvid(ref), "cid": cid, "qn": int(qn),
        "fnver": 0, "fnval": 1, "fourk": 1, "high_quality": 1,
    })
    durl = data.get("durl") or []
    if durl and durl[0].get("url"):
        return {
            "type": "mp4",
            "video": str(durl[0]["url"]),
            "audio": "",
            "qn": int(data.get("quality") or qn),
            "quality": _QN_NAMES.get(int(data.get("quality") or qn), "auto"),
            "size": int(durl[0].get("size") or 0),
            "headers": _headers(),
        }
    return None


def _dash_playurl(ref: str, cid: int, qn: int) -> dict | None:
    """DASH 双轨：仅在明确要 DASH 时用（飞牛侧需合流）。"""
    data = _get_json(f"{API}/x/player/playurl", {
        "bvid": get_bvid(ref), "cid": cid, "qn": int(qn),
        "fnver": 0, "fnval": 16 | 4048 | 64, "fourk": 1,
    })
    dash = data.get("dash") or {}
    videos = dash.get("video") or []
    if not videos:
        return None
    chosen = None
    for item in videos:
        if int(item.get("id") or 0) == int(qn) and item.get("baseUrl"):
            chosen = item
            break
    if chosen is None:
        cands = [v for v in videos if v.get("baseUrl")]
        if not cands:
            return None
        cands.sort(key=lambda v: int(v.get("id") or 0), reverse=True)
        chosen = cands[0]
    audio = ""
    aus = dash.get("audio") or []
    if aus:
        aus = sorted(aus, key=lambda x: int(x.get("bandwidth") or 0),
                     reverse=True)
        audio = str(aus[0].get("baseUrl") or "")
    return {
        "type": "dash",
        "video": str(chosen.get("baseUrl") or ""),
        "audio": audio,
        "qn": int(chosen.get("id") or qn),
        "quality": _QN_NAMES.get(int(chosen.get("id") or qn), "auto"),
        "size": 0,
        "headers": _headers(),
    }


def play(ref: str, page: int = 0, qn: int | None = None,
         allow_dash: bool = False) -> dict:
    """自动选最高可用清晰度，返回飞牛可直接播放的地址。

    优先合一 MP4（fnval=1）；登录后账号权限决定实际拿到的画质。
    """
    ref_raw = str(ref or "")
    # 分P号优先取显式 page 参数；没给时从原始 ref 的 ?p=N 取
    # （get_bvid 会把 ?p=N 截掉，顺序反了会永远取第 1 集）。
    explicit = int(page or 0)
    page = ref_page(ref_raw) if explicit < 1 else explicit
    ref = get_bvid(ref_raw)
    if not page or int(page) < 1:
        page = 1
    page = int(page)
    cid, total_pages = page_cid(ref, page)
    order = [int(qn)] if qn else list(_QN_PRIORITY)
    last_err = None
    for want in order:
        try:
            got = _durl_playurl(ref, cid, want)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            got = None
        if got:
            got.update({"cid": cid, "page": page, "pages": total_pages,
                        "bvid": ref,
                        "cookie_set": bool(bilibili.current_cookie())})
            return got
    if allow_dash:
        for want in order:
            try:
                got = _dash_playurl(ref, cid, want)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                got = None
            if got:
                got.update({"cid": cid, "page": page, "pages": total_pages,
                            "bvid": ref,
                            "cookie_set": bool(bilibili.current_cookie())})
                return got
    if last_err:
        raise RuntimeError("B 站无可用清晰度: %s" % last_err)
    raise RuntimeError("B 站未返回可播放流")
