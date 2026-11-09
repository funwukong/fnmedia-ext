#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电视直播（方案 L）：IPTV 频道聚合为飞牛虚拟媒体库。

- 订阅：M3U（#EXTINF tvg-logo/group-title、#EXTVLCOPT UA/Referer）与 DIYP TXT
  （# genre# 分组、"名字,url" 行）。内置公开聚合源 + 管理页自定义订阅。
- 合并：频道名归一后同名合并，多源地址成为同频道多线路；台标取首个非空。
- 缓存：解析结果落盘 state/live/channels.json（TTL 6h），浏览接口只读内存。
- 测活：后台轮转抽样，httpx 轻量探测，连续 3 败标死隐藏、恢复自动回。
- 代理：/v/media/online/live/<chash>/<idx>/index.m3u8 拉源清单并把分片改写为
  seg?u=<b64url>，seg 端点流式直通（带该线路 UA/Referer），支持 master/media
  清单与 EXT-X-KEY。
- guid 形态（全部自造前缀，可确定性反解，不入 store）：
    库   = md5("fnmedia-live-lib")                 （32 位 hex，与官方库同形态）
    分类 = fnmedia-live-cat:<md5(group)[:12]>
    频道 = fnmedia-live:<md5(norm_name)>
    媒体 = fnmedia-livemg:<chash>:<idx>（video = fnmedia-livevg:<chash>:<idx>）

模块名说明：live.py 已是实时播放诊断模块，故本模块取名 iptv。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time

import httpx
from fastapi.responses import StreamingResponse

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "state", "live")
CONF_PATH = os.path.join(STATE_DIR, "config.json")
CHANNELS_PATH = os.path.join(STATE_DIR, "channels.json")
CHECK_PATH = os.path.join(STATE_DIR, "check.json")

# 内置默认订阅（管理页可关；拉取失败自动跳过，不影响其余源）
DEFAULT_SUBS = [
    {"name": "泛盒子聚合(IPv6)", "url": "https://live.fanmingming.com/tv/m3u/ipv6.m3u", "enabled": True},
    {"name": "iptv-org 中国", "url": "https://iptv-org.github.io/iptv/countries/cn.m3u", "enabled": True},
]

CHANNELS_TTL = 6 * 3600          # 解析缓存有效期
CHECK_INTERVAL = 150             # 测活轮询间隔（秒）
CHECK_BATCH = 40                 # 每轮抽样条数
DEAD_AFTER = 3                   # 连续失败次数 -> 标死
DEAD_RETRY_HOURS = 24            # 死线恢复重试间隔（小时）

PROXY_BASE = "/v/media/online/live"

# ---------------------------------------------------------------- guid helpers


def lib_guid() -> str:
    return hashlib.md5(b"fnmedia-live-lib").hexdigest()


def cat_guid(group: str) -> str:
    return "fnmedia-live-cat:" + hashlib.md5(
        (group or "其他").encode()).hexdigest()[:12]


def ch_hash(norm_name: str) -> str:
    return hashlib.md5(norm_name.encode()).hexdigest()


def ch_guid(norm_name: str) -> str:
    return "fnmedia-live:" + ch_hash(norm_name)


def ch_guid_hash(guid: str) -> str:
    """fnmedia-live:<chash> -> chash；其余形态返回 ''。"""
    return guid[len("fnmedia-live:"):] if guid.startswith("fnmedia-live:") else ""


def cat_guid_group(guid: str) -> str | None:
    """分类 guid 反查原始分组名；非直播分类返回 None。

    分类 guid 是 md5(group)[:12]，只能查表反解：用当前缓存里的分组集合。"""
    if not guid or not guid.startswith("fnmedia-live-cat:"):
        return None
    tok = guid[len("fnmedia-live-cat:"):]
    for g in {ch.get("g") or "其他" for ch in channels_cached()}:
        if hashlib.md5(g.encode()).hexdigest()[:12] == tok:
            return g
    return None


MG_PREFIX = "fnmedia-livemg:"
VG_PREFIX = "fnmedia-livevg:"


def media_guid_of(chash: str, idx: int) -> str:
    return f"{MG_PREFIX}{chash}:{idx}"


def video_guid_of(chash: str, idx: int) -> str:
    return f"{VG_PREFIX}{chash}:{idx}"


def parse_media_guid(mg: str) -> tuple[str, int] | None:
    """fnmedia-livemg:<chash>:<idx> -> (chash, idx)；非直播 mg 返回 None。"""
    if not mg or not mg.startswith(MG_PREFIX):
        return None
    rest = mg[len(MG_PREFIX):]
    chash, _, idx = rest.rpartition(":")
    try:
        return (chash, int(idx)) if chash else None
    except ValueError:
        return None


def is_live_mg(mg: str) -> bool:
    return bool(mg) and mg.startswith(MG_PREFIX)


def _norm_name(name: str) -> str:
    """频道名归一：全角转半角、去空白与常见分隔符、统一大写。"""
    s = (name or "").strip()
    out = []
    for chn in s:
        o = ord(chn)
        if o == 0x3000:
            continue
        if 0xFF01 <= o <= 0xFF5E:
            o -= 0xFEE0
        out.append(chr(o))
    s = "".join(out).upper()
    s = re.sub(r"[\s\-—–_·•・.()（）\[\]【】「」《》|:：,，、]", "", s)
    return s or (name or "").strip().upper()


def _b64u_dec(tok: str) -> bytes:
    pad = "=" * (-len(tok) % 4)
    return base64.urlsafe_b64decode(tok + pad)


def _b64u_enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# ---------------------------------------------------------------- 状态读写

_lock = asyncio.Lock()


def _ensure_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def load_conf() -> dict:
    try:
        with open(CONF_PATH, encoding="utf-8") as fh:
            conf = json.load(fh)
        if not isinstance(conf.get("subs"), list):
            conf["subs"] = []
        return conf
    except Exception:
        return {"enabled": True, "subs": []}


def save_conf(conf: dict) -> None:
    _ensure_dir()
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(conf, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF_PATH)


def enabled() -> bool:
    return bool(load_conf().get("enabled", True))


# ---------------------------------------------------------------- 解析


def _absolutize(base: str, url: str) -> str:
    if not url or url.startswith(("http://", "https://", "data:")):
        return url
    from urllib.parse import urljoin
    return urljoin(base, url)


def parse_m3u(text: str, src: str) -> list[dict]:
    """M3U/DIYP 混合 -> [{n,g,logo,urls:[{u,ua,ref,src}]}]（未合并）。"""
    out: list[dict] = []
    pend_name = pend_logo = pend_group = ""
    pend_ua = pend_ref = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("#extinf"):
            pend_ua = pend_ref = ""
            attrs, _, _comma, name = line.partition(",")
            name = name.strip()
            if not name:
                continue
            pend_name = name
            m = re.search(r'tvg-logo="([^"]+)"', attrs)
            pend_logo = m.group(1).strip() if m else ""
            m = re.search(r'group-title="([^"]*)"', attrs)
            pend_group = (m.group(1).strip() if m else "") or "其他"
        elif low.startswith("#extvlcopt:http-user-agent="):
            pend_ua = line.split("=", 1)[1].strip()
        elif low.startswith("#extvlcopt:http-referrer="):
            pend_ref = line.split("=", 1)[1].strip()
        elif line.startswith("#"):
            continue
        else:
            # 无 # 前缀行：M3U 的 URL 行 或 DIYP 的 "名字,url" 行
            name, sep, url = line.partition(",")
            if low.startswith(("http://", "https://")):
                if not pend_name:
                    continue
                url, name = line, pend_name
            elif sep and url.strip().startswith(("http://", "https://")):
                url, name = url.strip(), name.strip()
                if not name:
                    continue
                pend_group = pend_group or "其他"
            else:
                continue
            url = url.strip()
            if not url.startswith(("http://", "https://", "rtmp://", "rtsp://")):
                continue
            out.append({"n": name, "g": pend_group or "其他",
                        "logo": pend_logo,
                        "urls": [{"u": url, "ua": pend_ua,
                                  "ref": pend_ref, "src": src}]})
            pend_name = pend_logo = pend_group = ""
            pend_ua = pend_ref = ""
    return out


def parse_diyp(text: str, src: str) -> list[dict]:
    """DIYP TXT：`分组,#genre#` 分段 + `频道,地址` 行。"""
    out: list[dict] = []
    group = "其他"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, url = line.partition(",")
        name, url = name.strip(), url.strip()
        if not sep:
            continue
        if url == "#genre#":
            group = name or "其他"
            continue
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"n": name, "g": group, "logo": "",
                    "urls": [{"u": url, "ua": "", "ref": "", "src": src}]})
    return out


def _merge(parsed: list[dict]) -> list[dict]:
    """按归一名合并；urls 去重；logo 取首个非空；分组取首个。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for rec in parsed:
        key = _norm_name(rec["n"])
        if not key:
            continue
        cur = merged.get(key)
        if cur is None:
            cur = {"n": rec["n"], "key": key, "g": rec["g"] or "其他",
                   "logo": rec.get("logo") or "", "urls": []}
            merged[key] = cur
            order.append(key)
        if not cur["logo"] and rec.get("logo"):
            cur["logo"] = rec["logo"]
        seen = {u["u"] for u in cur["urls"]}
        for u in rec.get("urls") or []:
            if u["u"] not in seen:
                cur["urls"].append(u)
                seen.add(u["u"])
    return [merged[k] for k in order]


# ---------------------------------------------------------------- 缓存通道

_cache: dict | None = None          # {"ts":..., "channels":[...]}
_cache_loaded = False


def _read_cache() -> dict | None:
    try:
        with open(CHANNELS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("channels"), list):
            return data
    except Exception:
        pass
    return None


def _write_cache(data: dict) -> None:
    _ensure_dir()
    tmp = CHANNELS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, CHANNELS_PATH)


def channels_cached() -> list[dict]:
    """只读内存缓存（不联网、不触发解析）；无缓存返回 []。"""
    global _cache, _cache_loaded
    if not _cache_loaded:
        _cache = _read_cache()
        _cache_loaded = True
    return (_cache or {}).get("channels") or []


def cache_stale() -> bool:
    data = _cache if _cache_loaded else _read_cache()
    if not data:
        return True
    return (time.time() - float(data.get("ts") or 0)) > CHANNELS_TTL


def cache_meta() -> dict:
    data = _cache if _cache_loaded else _read_cache()
    if not data:
        return {"ts": 0, "channels": 0}
    return {"ts": float(data.get("ts") or 0),
            "channels": len(data.get("channels") or []),
            "subs_ok": data.get("subs_ok") or [],
            "subs_fail": data.get("subs_fail") or []}


def ch_by_hash(chash: str) -> dict | None:
    for ch in channels_cached():
        if ch.get("key") and ch_hash(ch["key"]) == chash:
            return ch
    return None


def ch_by_guid(guid: str) -> dict | None:
    chash = ch_guid_hash(guid or "")
    return ch_by_hash(chash) if chash else None


def groups_of(channels: list[dict]) -> dict[str, list[dict]]:
    gs: dict[str, list[dict]] = {}
    for ch in channels:
        gs.setdefault(ch.get("g") or "其他", []).append(ch)
    return gs


def total_channels() -> int:
    return len(channels_cached())


def lib_posters(n: int = 8) -> list[str]:
    """库/卡片封面：前 n 个有效台标。"""
    ps = []
    for ch in channels_cached():
        if ch.get("logo"):
            ps.append(ch["logo"])
            if len(ps) >= n:
                break
    return ps


# ---------------------------------------------------------------- 刷新


async def refresh(ext: httpx.AsyncClient, force: bool = False) -> dict:
    """拉全部启用订阅并重建频道缓存（串行拉取，单源 12s 超时）。"""
    global _cache
    async with _lock:
        if not force and not cache_stale():
            return {"ok": True, "skipped": True,
                    "channels": len(channels_cached())}
        conf = load_conf()
        subs = [s for s in conf.get("subs") or [] if s.get("enabled", True)]
        parsed: list[dict] = []
        ok_subs, fail_subs = [], []
        for s in subs:
            url = (s.get("url") or "").strip()
            if not url:
                continue
            try:
                r = await ext.get(url, timeout=12.0, follow_redirects=True)
                r.raise_for_status()
                text = r.text
                if not text.strip():
                    raise ValueError("empty body")
                src = s.get("name") or url[:40]
                got = parse_m3u(text, src)
                if not got:
                    got = parse_diyp(text, src)
                parsed.extend(got)
                ok_subs.append(src)
            except Exception as exc:  # noqa: BLE001
                fail_subs.append({"name": s.get("name") or url[:40],
                                  "error": str(exc)[:160]})
        channels = _merge(parsed)
        _cache = {"ts": time.time(), "channels": channels,
                  "subs_ok": ok_subs, "subs_fail": fail_subs}
        _cache_loaded = True
        try:
            _write_cache(_cache)
        except Exception:
            pass
        return {"ok": True, "channels": len(channels),
                "groups": len(groups_of(channels)),
                "subs_ok": ok_subs, "subs_fail": fail_subs}


# ---------------------------------------------------------------- 测活


def _read_check() -> dict:
    try:
        with open(CHECK_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("results"), dict):
            return data
    except Exception:
        pass
    return {"ts": 0, "results": {}}


def _write_check(data: dict) -> None:
    _ensure_dir()
    tmp = CHECK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, CHECK_PATH)


_check: dict | None = None


def check_state() -> dict:
    global _check
    if _check is None:
        _check = _read_check()
    return _check


def url_alive(url: str) -> bool:
    st = check_state()["results"].get(url)
    return bool(st and st.get("state") == "ok")


def alive_urls(ch: dict) -> list[dict]:
    """该频道当前可用线路（全部死/未知时退回全部，避免空壳频道）。"""
    urls = ch.get("urls") or []
    if not urls:
        return urls
    known = [u for u in urls if check_state()["results"].get(u["u"])]
    if not known:
        return urls
    alive = [u for u in urls if url_alive(u["u"])]
    return alive or urls


def _pick_batch(n: int) -> list[str]:
    """轮转抽样：未测/最久未测优先；死线按恢复周期重试。"""
    results = check_state()["results"]
    now = time.time()
    cand: list[tuple[float, str]] = []
    for ch in channels_cached():
        for u in ch.get("urls") or []:
            url = u["u"]
            st = results.get(url)
            if st is None:
                prio = 0.0
            elif st.get("state") == "dead":
                if now - float(st.get("ts") or 0) < DEAD_RETRY_HOURS * 3600:
                    continue
                prio = 2.0
            else:
                prio = 1.0 + (now - float(st.get("ts") or 0)) / 3600.0
            cand.append((prio, url))
    cand.sort(key=lambda t: t[0])
    seen, out = set(), []
    for _p, url in cand:
        if url not in seen:
            seen.add(url)
            out.append(url)
            if len(out) >= n:
                break
    return out


async def _probe_one(ext: httpx.AsyncClient, url: str) -> tuple[str, int, float]:
    t0 = time.time()
    try:
        r = await ext.get(url, timeout=6.0, follow_redirects=True)
        code = r.status_code
    except Exception:  # noqa: BLE001
        return url, 0, time.time() - t0
    return url, code, time.time() - t0


async def check_once(ext: httpx.AsyncClient, batch: int = CHECK_BATCH) -> dict:
    global _check
    batch_urls = _pick_batch(batch)
    if not batch_urls:
        return {"ok": True, "checked": 0}
    async with _lock:
        st = check_state()
        results = st.setdefault("results", {})
        tasks = [_probe_one(ext, u) for u in batch_urls]
        for coro in asyncio.as_completed(tasks):
            url, code, ms = await coro
            rec = results.get(url) or {"state": "unknown", "fails": 0}
            if 200 <= code < 400:
                rec.update({"state": "ok", "fails": 0, "code": code,
                            "ts": time.time(), "ms": round(ms * 1000)})
            else:
                fails = int(rec.get("fails") or 0) + 1
                rec.update({"fails": fails, "code": code,
                            "ts": time.time()})
                if fails >= DEAD_AFTER:
                    rec["state"] = "dead"
            results[url] = rec
        st["ts"] = time.time()
        _write_check(st)
    return {"ok": True, "checked": len(batch_urls)}


def check_summary() -> dict:
    st = check_state()
    ok = dead = 0
    for rec in st["results"].values():
        if rec.get("state") == "dead":
            dead += 1
        elif rec.get("state") == "ok":
            ok += 1
    return {"ts": st.get("ts") or 0, "alive": ok, "dead": dead,
            "known": len(st["results"]),
            "channels": total_channels()}


async def worker(ext: httpx.AsyncClient) -> None:
    """后台循环：缓存过期即重解析 + 周期测活（绝不阻塞浏览接口）。"""
    while True:
        try:
            if enabled():
                try:
                    await refresh(ext)
                except Exception:  # noqa: BLE001
                    pass
                await check_once(ext)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------- 播放代理


def _line_of(ch: dict, idx: int) -> dict | None:
    urls = alive_urls(ch)
    if 0 <= idx < len(urls):
        return urls[idx]
    return None


def _rewrite_playlist(base_url: str, text: str, route_base: str) -> str:
    """把清单里的子清单/分片/KEY 全部改写为本地代理端点。"""
    out = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        s = line.strip()
        low = s.lower()
        if low.startswith(("#ext-x-stream-inf", "#ext-x-i-frame-stream-inf")):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                absu = _absolutize(base_url, m.group(1))
                line = re.sub(r'URI="[^"]+"',
                              f'URI="{route_base}/variant?u={_b64u_enc(absu.encode())}"',
                              line)
            out.append(line)
        elif low.startswith(("#ext-x-key", "#ext-x-map")):
            m = re.search(r'URI="([^"]+)"', s)
            if m and not m.group(1).startswith("data:"):
                absu = _absolutize(base_url, m.group(1))
                line = re.sub(r'URI="[^"]+"',
                              f'URI="{route_base}/seg?u={_b64u_enc(absu.encode())}"',
                              line)
            out.append(line)
        elif s and not s.startswith("#"):
            absu = _absolutize(base_url, s)
            out.append(f"{route_base}/seg?u={_b64u_enc(absu.encode())}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _seg_headers(line: dict) -> dict:
    h = {}
    if line.get("ua"):
        h["User-Agent"] = line["ua"]
    if line.get("ref"):
        h["Referer"] = line["ref"]
    return h


async def proxy_playlist(ext: httpx.AsyncClient, chash: str, idx: int,
                         sub_url: str | None = None):
    """频道线路清单：master/media 通吃，改写为本地端点。"""
    ch = ch_by_hash(chash)
    if not ch:
        return None
    line = _line_of(ch, idx)
    if not line:
        return None
    url = sub_url or line["u"]
    try:
        r = await ext.get(url, timeout=12.0, follow_redirects=True,
                          headers=_seg_headers(line))
        text = r.text
    except Exception:  # noqa: BLE001
        return None
    if "#EXTM3U" not in text.upper():
        return None
    base = f"{PROXY_BASE}/{chash}/{idx}"
    body = _rewrite_playlist(url, text, base)
    return body.encode("utf-8")


async def proxy_segment(ext: httpx.AsyncClient, chash: str, idx: int,
                        url: str, range_header: str = ""):
    """分片/KEY/直连流：流式直通（不再解析内容）。"""
    ch = ch_by_hash(chash)
    if not ch:
        return None
    line = _line_of(ch, idx)
    if not line:
        return None
    headers = _seg_headers(line)
    if range_header:
        headers["Range"] = range_header
    try:
        req = ext.build_request("GET", url, headers=headers)
        resp = await ext.send(req, stream=True, timeout=30.0,
                              follow_redirects=True)
    except Exception:  # noqa: BLE001
        return None

    async def _iter():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()

    ctype = resp.headers.get("content-type", "application/octet-stream")
    pass_headers = {"Cache-Control": "no-store"}
    if resp.headers.get("content-length"):
        pass_headers["Content-Length"] = resp.headers["content-length"]
    if resp.headers.get("content-range"):
        pass_headers["Content-Range"] = resp.headers["content-range"]
    if resp.headers.get("accept-ranges"):
        pass_headers["Accept-Ranges"] = resp.headers["accept-ranges"]
    return StreamingResponse(_iter(), status_code=resp.status_code,
                             media_type=ctype, headers=pass_headers)


# ---------------------------------------------------------------- 管理接口数据


def conf_payload() -> dict:
    conf = load_conf()
    return {"enabled": bool(conf.get("enabled", True)),
            "subs": conf.get("subs") or [],
            "defaults": DEFAULT_SUBS,
            "cache": cache_meta(),
            "check": check_summary()}


def list_channels(keyword: str = "", group: str = "",
                  limit: int = 200) -> list[dict]:
    kw = (keyword or "").strip().upper()
    out = []
    for ch in channels_cached():
        if group and (ch.get("g") or "其他") != group:
            continue
        if kw and kw not in (ch.get("n") or "").upper():
            continue
        urls = ch.get("urls") or []
        out.append({
            "guid": ch_guid(ch["key"]), "name": ch["n"],
            "group": ch.get("g") or "其他",
            "logo": ch.get("logo") or "", "lines": len(urls),
            "alive": sum(1 for u in urls if url_alive(u["u"])),
            "sources": sorted({(u.get("src") or "")
                               for u in urls})[:6],
        })
        if len(out) >= max(1, min(limit, 1000)):
            break
    return out
