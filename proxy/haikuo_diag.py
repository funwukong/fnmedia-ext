#!/usr/bin/env python3
"""海阔诊断：内存环形缓冲（最近 100 条事件），供只读端点用。

照 proxy/resume_diag.py 的模式：只记内存，不写盘；事件由调用方在
海阔浏览/映射/解析的关键点记入，GET /v/api/ext/diag/haikuo 只读返回。

绝不记敏感值：
  - haikuo-play: token 只记种类（haikuo-play:<token>），不记原文；
  - Authorization 只留键名，值记为 <redacted>；
  - URL 只记形态（scheme/host/路径前缀），查询串与 fragment 一律去掉；
  - 函数体 URL（搜索框等）只记 function-url。
公开构建整文件剥离（tools/public_strip.py DROP_FILES）。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from collections import deque

_MAX_EVENTS = 100
_events: "deque[dict]" = deque(maxlen=_MAX_EVENTS)
_lock = threading.Lock()

_TOKEN_RE = re.compile(r"haikuo-play:[A-Za-z0-9_=\-]+")
# Authorization 头的值常是两词（Bearer <token>），一次吃掉两词，免得
# token 残留在脱敏后的串里。
_AUTH_RE = re.compile(r"Authorization\s*[:=]\s*[^\s,;\"']+(?:\s+[^\s,;\"']+)?", re.I)
_URL_FIELDS = ("url", "detail_url", "play_url", "src", "href", "link",
               "input")


def url_shape(url: str) -> str:
    """URL → 只留形态：scheme://host/路径前缀（≤120 字），无查询串/fragment。

    haikuo-play: token 只留种类；函数体 URL 只记 function-url。
    """
    s = str(url or "")
    if not s:
        return ""
    if s.startswith("haikuo-play:"):
        return "haikuo-play:<token>"
    if "haikuo-play:" in s:
        s = _TOKEN_RE.sub("haikuo-play:<token>", s)
    if s.lstrip().startswith("function"):
        return "function-url"
    try:
        p = urllib.parse.urlparse(s)
    except ValueError:
        return ""
    if not p.scheme:
        return s[:120]
    path = (p.path or "")[:120]
    if p.netloc:
        return f"{p.scheme}://{p.netloc}{path}"
    return f"{p.scheme}:{path}"


def _scrub(value):
    """递归去敏感值：token 原文与 Authorization 值一律替换。"""
    if isinstance(value, str):
        s = _TOKEN_RE.sub("haikuo-play:<token>", value)
        s = _AUTH_RE.sub("Authorization: <redacted>", s)
        return s
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def note(kind: str, key: str = "", name: str = "", **fields) -> None:
    """记一条事件：kind + 时间 + 规则 key/名 + 摘要字段（已脱敏）。"""
    try:
        event: dict = {"kind": str(kind or ""),
                       "at": time.time(),
                       "key": str(key or ""),
                       "name": str(name or "")}
        for fk, fv in fields.items():
            if fk in _URL_FIELDS and isinstance(fv, str):
                event[fk] = url_shape(fv)
            else:
                event[fk] = _scrub(fv)
    except Exception:
        return
    with _lock:
        _events.append(event)


def events(key: str | None = None, limit: int = 50) -> list[dict]:
    """最近事件的快照（最旧在前），返回副本；key 过滤，limit 默认 50、上限 200。"""
    try:
        limit = int(limit or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    with _lock:
        out = [dict(e) for e in _events]
    if key:
        out = [e for e in out if e.get("key") == key]
    return out[-limit:]


def clear() -> None:
    """清空缓冲（测试隔离用）。"""
    with _lock:
        _events.clear()
