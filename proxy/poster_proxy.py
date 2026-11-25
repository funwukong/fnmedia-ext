#!/usr/bin/env python3
"""海报代理的纯逻辑层：token 解码、占位图、@ 后缀拆解、成功图内存 LRU、
成功图磁盘缓存、图片嗅探。

从 app.py 拆出（v1.2.8）：这一整块不依赖 app 的其它全局状态，只依赖
config 模块与标准库，放独立模块便于测试与阅读。路由（/online/...）仍留在
app.py —— 它们要 FastAPI 的 app 实例与请求级依赖。

注意：这里只做工具函数，**不发**上游请求；抓图（ext_get_retry 等）在 app.py，
以便测试继续 patch app.ext_get_retry。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path

import httpx
from fastapi import Response

import config as cfgmod

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("app")

def _decode_poster_tok(tok: str) -> tuple[str, str, int | None]:
    """解码海报 token，返回 (图床URL, 网盘名, 健康等级)。

    兼容旧版纯 URL token 与新版 JSON{u,p,h} token。"""
    from app import _decode_tok  # 延迟导入：app 在模块级 import 本模块，避免环
    raw = _decode_tok(tok)
    pan_label, grade = "", None
    url = raw
    if raw[:1] in ("{", "["):
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                url = j.get("u", "")
                pan_label = str(j.get("p", "") or "")
                if j.get("h") is not None:
                    grade = int(j["h"])
        except Exception:
            _log.debug("_decode_poster_tok failed", exc_info=True)
            url = raw
    return url, pan_label, grade


# 海报占位图（JPEG，150x225 渐变 + 播放标）：App/TV 原生图片加载器解不了 SVG，
# 旧的 SVG fallback 在 App 端会显示为空白；改成光栅 JPEG 后失败时至少能看到占位。
_POSTER_FALLBACK_JPEG_B64 = (
    "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"
)


def _poster_fallback() -> Response:
    """海报抓取失败时的占位图：返回 JPEG（非 SVG）。

    旧版返回 SVG；浏览器能渲染，但 App/TV 原生图片加载器解不了 SVG，
    失败时 App 端直接显示空白。JPEG 各端都能解，至少能看到占位图。
    占位图只给 60 秒缓存（之前是 3600 秒）：首屏并发打垮图床时失败
    占位图会被 App 缓存一小时、第一页一直蓝着；60 秒后重进就能重试。
    """
    try:
        import base64 as _b64
        data = _b64.b64decode(_POSTER_FALLBACK_JPEG_B64)
        if data[:2] == b"\xff\xd8":
            return Response(data, media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=60"})
    except Exception:  # noqa: BLE001
        _log.debug("_poster_fallback failed", exc_info=True)
        pass
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
  <rect width="300" height="450" fill="#1f6feb"/>
  <circle cx="150" cy="180" r="58" fill="rgba(255,255,255,.18)"/>
  <polygon points="132,145 132,215 192,180" fill="#fff" opacity=".92"/>
  <text x="150" y="285" text-anchor="middle" fill="#fff" font-family="sans-serif"
    font-size="26" font-weight="700">在线影视</text>
</svg>"""
    return Response(svg, media_type="image/svg+xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=60"})


def _poster_fetch_headers(url: str) -> dict:
    """海报上游抓取请求头：UA + 豆瓣 Referer（其余图床不带 Referer）。"""
    headers = {"User-Agent": "Mozilla/5.0"}
    host = urllib.parse.urlsplit(url).netloc.lower()
    # 豆瓣图床需要带豆瓣 Referer，否则可能 403
    if host.endswith("doubanio.com") or host.endswith("douban.com") or \
            host.endswith("doubanio.viki.moe"):
        headers["Referer"] = "https://movie.douban.com/"
    return headers


# 部分源规则海报常带 HikerView 后缀：url@Referer=https://站/@User-Agent=UA
# 或 url@headers={"Referer": "..."}。只认已知键开头的 @Key= 标记——图地址
# 本身也可能含 @（如 userinfo），不能按第一个 @ 一刀切。
_POSTER_SUFFIX_RE = re.compile(r"@(Referer|User-Agent|Cookie|headers)=")


def _split_poster_suffix(url: str) -> tuple[str, dict]:
    """拆海报地址的 @ 后缀 → (干净图地址, 抓图请求头)。

    @ 之前是真实图地址，@ 之后是抓图时要带的请求头；未知/解析失败的后缀
    直接丢弃并记日志，不让它污染图地址。无后缀时原样返回。
    """
    m = _POSTER_SUFFIX_RE.search(url)
    if not m:
        return url, {}
    base, rest = url[:m.start()], url[m.start():]
    headers: dict[str, str] = {}
    for key, val in re.findall(r"@(Referer|User-Agent|Cookie|headers)=([^@]*)", rest):
        if key == "headers":
            try:
                obj = json.loads(val)
            except ValueError:
                _log.debug("_split_poster_suffix bad headers json: %s", val[:60])
                continue
            if isinstance(obj, dict):
                for hk, hv in obj.items():
                    headers[str(hk)] = str(hv)
        else:
            headers[key] = val
    return base, headers


# 上游 v1.0.0 _poster_fetch 的网络错误集：直连出现连接级异常时才重试，
# 正常 HTTP 错误（4xx/5xx）不重试。
_POSTER_NETWORK_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


# 成功海报的内存 LRU 缓存（只缓存 200 且为图片的抓取结果；失败永不入缓存）。
# 首屏 9–22 张海报并发现抓常把图床打 403/429/超时，失败占位图又被 App 缓存
# 1 小时；有界内存缓存让重进/换页不再重复抓同一张图。
_POSTER_OK_CACHE: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()
_POSTER_OK_BYTES = 0
_POSTER_OK_MAX_ITEMS = 512
_POSTER_OK_MAX_BYTES = 64 * 1024 * 1024
_POSTER_OK_TTL = 6 * 3600

# ---- 海报磁盘缓存（v1.2.7 重引入，与 v0.2.42 移除的旧实现不同）----
# 旧实现（7 天全量落盘 + 后台预热循环）被移除的原因：缓存可能长期 serving
# 陈旧/错误内容、后台预热把海报墙变慢。本实现针对性避开：
#   1) 只缓存成功抓到的真图（200 + 图片嗅探，失败/占位永不落盘）；
#   2) 每个文件自带 mtime TTL（默认 7 天，过期即删不回旧内容）；
#   3) 无任何后台预热循环——只在内存 LRU 未命中时读一次磁盘；
#   4) 容量有界（poster_disk_cache_mb，默认 256MB，0=关闭），超限删最旧；
#   5) 原子写（tmp + replace），断电不产生半截图。
# 重启后海报不再冷启动打图床（首屏 9–22 张并发出图床 403/429 的主因）。
_POSTER_DISK_DIR = Path(__file__).resolve().parent.parent / "state" / "poster_disk"
_POSTER_DISK_TTL = 7 * 24 * 3600.0
_POSTER_DISK_MAX_BYTES_DEFAULT = 256 * 1024 * 1024
# 磁盘写/清理都丢线程池；同 URL 并发写只允许一个（防止重复写盘）。
_poster_disk_writing: set[str] = set()
_poster_disk_lock = threading.Lock()
# 清理节流：每次触发清理之间至少隔 60s，避免首屏并发写连续触发全目录扫描
_poster_disk_gc_last = 0.0


def _poster_disk_enabled() -> bool:
    try:
        return int(cfgmod.config.read("poster_disk_cache_mb",
                               _POSTER_DISK_MAX_BYTES_DEFAULT
                               // (1024 * 1024)) or 0) > 0
    except Exception:
        _log.debug("_poster_disk_enabled failed", exc_info=True)
        return False


def _poster_disk_max_bytes() -> int:
    try:
        mb = int(cfgmod.config.read("poster_disk_cache_mb",
                             _POSTER_DISK_MAX_BYTES_DEFAULT
                             // (1024 * 1024)) or 0)
        return max(0, min(4096, mb)) * 1024 * 1024
    except Exception:
        _log.debug("_poster_disk_max_bytes failed", exc_info=True)
        return _POSTER_DISK_MAX_BYTES_DEFAULT


def _poster_disk_path(url: str) -> Path:
    """URL → 磁盘文件。sha256 命名避免路径注入/超长文件名；扩展名只影响
    部分图床 Content-Type 猜测，回包 media_type 由调用方给，不依赖后缀。"""
    return _POSTER_DISK_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".img")


def _poster_disk_read(url: str):
    """磁盘命中返回 (content, ctype)；未命中/过期/损坏返回 None。
    过期与损坏文件顺手删除。ctype 从文件头嗅探（写入时校验过是图片）。"""
    if not _poster_disk_enabled():
        return None
    p = _poster_disk_path(url)
    try:
        st = p.stat()
    except OSError:
        return None
    if time.time() - st.st_mtime > _POSTER_DISK_TTL:
        try:
            p.unlink()
        except OSError:
            _log.debug("_poster_disk_read unlink failed", exc_info=True)
        return None
    try:
        content = p.read_bytes()
    except OSError:
        _log.debug("_poster_disk_read read failed", exc_info=True)
        return None
    ctype = _sniff_image(content[:12])
    if not ctype:
        # 半截/损坏文件：删掉，当作未命中
        try:
            p.unlink()
        except OSError:
            _log.debug("_poster_disk_read unlink corrupt failed", exc_info=True)
        return None
    return content, ctype


def _poster_disk_gc(force: bool = False) -> None:
    """容量/过期双维度清理：删过期文件；仍超限时按 mtime 删最旧。
    节流 60s（首屏并发写一批海报只触发一次全目录扫描）。"""
    global _poster_disk_gc_last
    now = time.monotonic()
    if not force and now - _poster_disk_gc_last < 60.0:
        return
    _poster_disk_gc_last = now
    try:
        _POSTER_DISK_DIR.mkdir(parents=True, exist_ok=True)
        entries = []
        total = 0
        for p in _POSTER_DISK_DIR.iterdir():
            try:
                st = p.stat()
                if p.is_file() and st.st_size > 0:
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            except OSError:
                continue
        budget = _poster_disk_max_bytes()
        # 先删过期
        keep = []
        for mt, sz, p in entries:
            if time.time() - mt > _POSTER_DISK_TTL:
                try:
                    p.unlink()
                except OSError:
                    _log.debug("_poster_disk_gc unlink expired failed",
                               exc_info=True)
                    keep.append((mt, sz, p))
                else:
                    total -= sz
            else:
                keep.append((mt, sz, p))
        # 仍超限：按 mtime 从旧到新删
        if total > budget:
            keep.sort(key=lambda e: e[0])
            for mt, sz, p in keep:
                if total <= budget:
                    break
                try:
                    p.unlink()
                    total -= sz
                except OSError:
                    _log.debug("_poster_disk_gc unlink evict failed",
                               exc_info=True)
    except Exception:
        _log.debug("_poster_disk_gc failed", exc_info=True)


def _poster_disk_write(url: str, content: bytes) -> None:
    """异步落盘（线程池执行）。同一 URL 并发只写一次；写入后触发节流清理。"""
    if not _poster_disk_enabled() or not content:
        return
    with _poster_disk_lock:
        if url in _poster_disk_writing:
            return
        _poster_disk_writing.add(url)

    def _job():
        try:
            if _poster_disk_read(url) is not None:
                return  # 已有（并发或重启前写过）
            p = _poster_disk_path(url)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(p)
            _poster_disk_gc()
        except Exception:
            _log.debug("_poster_disk_write failed", exc_info=True)
        finally:
            with _poster_disk_lock:
                _poster_disk_writing.discard(url)

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _job)
    except RuntimeError:
        _job()


def reset_ok_cache() -> None:
    """清空成功图内存 LRU（测试/管理端用；字节计数一并归零）。"""
    global _POSTER_OK_BYTES
    _POSTER_OK_CACHE.clear()
    _POSTER_OK_BYTES = 0


def _poster_ok_get(url: str):
    """命中返回 (内容, content-type)，未命中/过期返回 None。"""
    ent = _POSTER_OK_CACHE.get(url)
    if ent is None:
        return None
    expires, content, ctype = ent
    if time.monotonic() >= expires:
        _poster_ok_drop(url)
        return None
    _POSTER_OK_CACHE.move_to_end(url)
    return content, ctype


def _poster_ok_drop(url: str):
    global _POSTER_OK_BYTES
    ent = _POSTER_OK_CACHE.pop(url, None)
    if ent is not None:
        _POSTER_OK_BYTES -= len(ent[1])


def _poster_ok_put(url: str, content: bytes, ctype: str):
    """只缓存成功图：调用方保证 200 且为图片、内容非空。超限时从最久未用
    起淘汰；单张超总字节上限时放完即被淘汰，等于不缓存。"""
    global _POSTER_OK_BYTES
    if not content:
        return
    _poster_ok_drop(url)
    _POSTER_OK_CACHE[url] = (time.monotonic() + _POSTER_OK_TTL,
                             content, ctype)
    _POSTER_OK_BYTES += len(content)
    budget = cfgmod.cache_budget_bytes("poster_cache_mb")
    while (_POSTER_OK_CACHE
           and (len(_POSTER_OK_CACHE) > _POSTER_OK_MAX_ITEMS
                or _POSTER_OK_BYTES > budget)):
        _old_url, (_e, _c, _t) = _POSTER_OK_CACHE.popitem(last=False)
        _POSTER_OK_BYTES -= len(_c)


def _sniff_image(head: bytes) -> str:
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:8] == bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)):
        return "image/png"
    if head[:3] == bytes((0xFF, 0xD8, 0xFF)):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""
