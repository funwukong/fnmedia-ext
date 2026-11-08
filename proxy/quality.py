#!/usr/bin/env python3
"""播放线路实际分辨率探测（ffprobe）。

飞牛播放器在主 m3u8 的多变体菜单里只显示 NAME。各采集源的线路名
（如 zjm3u8、yun 等）本身常不含清晰度，这里在拿到**真实可播地址**后探测其
视频分辨率，给线路名追加 4K/1080P/720P 等标注。

策略：
  * mp4：Range 拉前 2MB，ffprobe 读 moov；
  * m3u8：媒体列表里前几片常是图片占位（CDN 鉴权/广告），跳过 image/* 与
    1×1，按几个候选下标找首个真实 TS 分片，ffprobe 读分辨率；
  * 结果按 站点+线路 粒度缓存（同一线路全剧分辨率基本一致），不随每集重探；
  * ffprobe 不存在、探测失败、超时一律返回 ""，绝不影响播放。

仅读取公开媒体字节用于读取分辨率，不做转码。
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import urllib.parse

import httpx

_FFPROBE: str | None = None
# key -> (ts, label)；缓存 6 小时，限 2000 条
_CACHE: dict[str, tuple[float, str]] = {}
_TTL = 6 * 3600
_MAX = 2000
_PROBE_TIMEOUT = 12.0
_SEG_TIMEOUT = 12.0


def _ffprobe_bin() -> str | None:
    global _FFPROBE
    if _FFPROBE is not None:
        return _FFPROBE or None
    from shutil import which
    _FFPROBE = which("ffprobe") or ""
    return _FFPROBE or None


def _label(height: int) -> str:
    if height >= 2000:
        return "4K"
    if height >= 1400:
        return "2K"
    if height >= 1000:
        return "1080P"
    if height >= 700:
        return "720P"
    if height >= 460:
        return "540P"
    if height > 0:
        return f"{height}P"
    return ""


async def _ffprobe_file(path: str) -> tuple[int, int] | None:
    binp = _ffprobe_bin()
    if not binp:
        return None
    try:
        p = await asyncio.create_subprocess_exec(
            binp, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=_PROBE_TIMEOUT)
    except Exception:
        return None
    # 可能有多行（广告/占位与正片分辨率不同），取最高（代表正片清晰度）
    wh = None
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if "," in line:
            try:
                w, h = (int(x) for x in line.split(",")[:2])
            except ValueError:
                continue
            # 排除 1×1 占位与 <240 的广告/缩略小片
            if w >= 320 and h >= 240 and (wh is None or h > wh[1]):
                wh = (w, h)
    return wh


def _unwrap_segment(data: bytes) -> tuple[bytes, str]:
    """部分源用「约 73 字节 1×1 PNG 头 + 原始 TS」伪装分片做防盗链。
    剥掉 PNG 外壳，返回 (真实负载, 文件后缀)。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        # IEND 结束位置；之后若是 TS（0x47 同步、188 对齐）即为伪装分片
        iend = data.find(b"IEND")
        if 0 < iend < 4096:
            cut = iend + 8
            payload = data[cut:]
            if payload and payload[0] == 0x47 and len(payload) % 188 == 0:
                return payload, ".ts"
    return data, ".ts"


async def _probe_bytes(data: bytes, suffix: str = ".ts") -> tuple[int, int] | None:
    data, suffix = _unwrap_segment(data)
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return await _ffprobe_file(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _probe_mp4(client: httpx.AsyncClient, url: str,
                     headers: dict | None) -> tuple[int, int] | None:
    try:
        # 取前 8MB：moov 在前/后两种 mp4 布局下尽量覆盖到
        r = await client.get(url, headers={**(headers or {}),
                                           "Range": "bytes=0-8388607"},
                             timeout=20.0)
        if r.status_code not in (200, 206) or not r.content:
            return None
        return await _probe_bytes(r.content, ".mp4")
    except Exception:
        return None


# ----------------------- AES-128-CBC（HLS EXT-X-KEY 解密） --------------------
_OPENSSL: str | None = None


def _openssl_bin() -> str | None:
    global _OPENSSL
    if _OPENSSL is not None:
        return _OPENSSL or None
    from shutil import which
    _OPENSSL = which("openssl") or ""
    return _OPENSSL or None


async def _aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes | None:
    """用系统 openssl（C 实现，毫秒级）解密 AES-128-CBC；不可用时返回 None。"""
    binp = _openssl_bin()
    if not binp or len(data) % 16:
        return None
    try:
        p = await asyncio.create_subprocess_exec(
            binp, "enc", "-d", "-aes-128-cbc",
            "-K", key.hex(), "-iv", iv.hex(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(
            p.communicate(data), timeout=_PROBE_TIMEOUT)
        return out if p.returncode == 0 and out else None
    except Exception:
        return None


async def _parse_hls_key(client, text: str, media_url: str,
                         headers) -> tuple[bytes, bytes] | None:
    """从媒体列表解析 AES-128 EXT-X-KEY，返回 (key, 默认IV全0标志)。"""
    m = None
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY") and "METHOD=AES-128" in line:
            m = line
            break
    if not m:
        return None
    mu = re.search(r'URI="([^"]+)"', m)
    if not mu:
        return None
    key_url = urllib.parse.urljoin(media_url, mu.group(1))
    try:
        kr = await client.get(key_url, headers=headers or {},
                              timeout=_SEG_TIMEOUT)
        if kr.status_code != 200 or len(kr.content) != 16:
            return None
        key = kr.content
        mi = re.search(r"IV=0x([0-9a-fA-F]+)", m)
        iv = bytes.fromhex(mi.group(1)) if mi else b"\0" * 16
        if len(iv) != 16:
            return None
        return key, iv
    except Exception:
        return None


async def _probe_hls(client: httpx.AsyncClient, media_url: str,
                     headers: dict | None) -> tuple[int, int] | None:
    try:
        r = await client.get(media_url, headers=headers or {},
                             timeout=_SEG_TIMEOUT)
        if r.status_code != 200:
            return None
        text = r.text
        keyinfo = await _parse_hls_key(client, text, media_url, headers)
        segs = [l.strip() for l in text.splitlines()
                if l.strip() and not l.strip().startswith("#")]
        mm = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
        media_seq = int(mm.group(1)) if mm else 0
    except Exception:
        return None
    if not segs:
        return None
    n = len(segs)
    if keyinfo:
        # AES 加密 HLS 不可能嵌占位图片，首片即正片；纯 Python 解密较贵，只试前 2 片
        cand = [i for i in (0, 1) if i < n]
        max_checked = 1
    else:
        # 明文流：开头若干片可能是真·1×1 PNG 占位，之后才是「PNG头+TS」伪装片，
        # 前中部分散取候选、上限 ~12 片控制冷启动开销
        picks = (2, 3, 5, 8, 12, 20, 30, n // 10, n // 6, n // 4, n // 3, n // 2)
        cand = sorted({i for i in picks if 0 < i < n})[:12]
        max_checked = 3
    best = None
    checked = 0
    for idx in cand:
        seg = urllib.parse.urljoin(media_url, segs[idx])
        try:
            sr = await client.get(seg, headers=headers or {},
                                  timeout=_SEG_TIMEOUT)
        except Exception:
            continue
        if sr.status_code != 200 or not sr.content:
            continue
        data = sr.content
        # AES-128 加密分片：openssl 解密后再探测（IV 缺省=媒体序列号）
        if keyinfo:
            key, explicit_iv = keyinfo
            iv = explicit_iv if explicit_iv != b"\0" * 16 \
                else (media_seq + idx).to_bytes(16, "big")
            dec = await _aes_cbc_decrypt(data, key, iv)
            if not dec:
                continue
            data = dec
        # _probe_bytes 会剥掉「PNG头+TS」伪装；真 1×1 占位片则返回 None 跳过
        wh = await _probe_bytes(data)
        if wh:
            checked += 1
            if best is None or wh[1] > best[1]:
                best = wh
        if checked >= max_checked:
            break
    return best


def cache_key(title_guid: str, line: str) -> str:
    # 按「标题+线路」缓存：同一剧各集分辨率一致，但不同影片可能不同；
    # 不绑具体 URL（带时效签名，每次解码都变）
    return f"{title_guid}|{line}"


async def resolve(client: httpx.AsyncClient, media_url: str, *,
                  title_guid: str, line: str,
                  headers: dict | None = None, kind: str = "") -> str:
    """返回 '1080P' 这样的标注；探测不到返回 ''。带缓存。

    kind 可显式传 "m3u8"/"mp4"（有些鉴权 URL 是哈希路径、无扩展名）。"""
    if not media_url:
        return ""
    key = cache_key(title_guid, line)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    if not _ffprobe_bin():
        return ""
    path = media_url.split("?", 1)[0].lower()
    if kind == "mp4" or (not kind and
                         (path.endswith(".mp4") or ".mp4" in os.path.basename(path))):
        wh = await _probe_mp4(client, media_url, headers)
    elif kind == "m3u8" or ".m3u8" in path:
        wh = await _probe_hls(client, media_url, headers)
    else:
        # 无扩展名的鉴权地址：先按 mp4 Range 试，失败再按 m3u8
        wh = await _probe_mp4(client, media_url, headers)
        if not wh:
            wh = await _probe_hls(client, media_url, headers)
    label = _label(wh[1]) if wh else ""
    _CACHE[key] = (now, label)
    if len(_CACHE) > _MAX:
        for k in list(_CACHE)[:_MAX // 2]:
            _CACHE.pop(k, None)
    return label


def cached_label(title_guid: str, line: str) -> str:
    """不触发探测，只取已有缓存（用于先快速出菜单、后台探测后刷新）。"""
    hit = _CACHE.get(cache_key(title_guid, line))
    return hit[1] if hit else ""
