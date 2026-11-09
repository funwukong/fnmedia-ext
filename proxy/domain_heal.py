#!/usr/bin/env python3
"""域名自愈（P2），与 TVBox 源的自愈规则一致。

候选顺序：上次成功的工作域名 → ext / 配置里给的域名 → 主域名 → 备用域名。
写法都认：纯域名（example.com）、完整 URL（https://example.com/...）、
JSON（{"host": "..."} / {"url": "..."}）。

采纳条件（只认返回正常内容的域名）：
- HTTP 200；
- 内容非空；
- 不是拦截页：Cloudflare / “正在验证” / ge_js_validator / JS 跳转壳；
- 对苹果 CMS：JSON 能解析且 code 为 1（或含 list）；对 HTML：含视频列表标记。

本模块是纯函数 + 一个小的“工作域名记忆”：不发请求，调用方传 fetch 回调。
"""
from __future__ import annotations

import json
import re
import urllib.parse

_CHALLENGE_MARKERS = (
    "正在验证", "ge_js_validator", "cloudflare", "cf-challenge",
    "Attention Required", "Just a moment", "请完成验证",
)
# 279 字节左右的 JS 跳转壳、只有一两行 script 的空壳页都算拦截。
_SHELL_RE = re.compile(r"^\s*(<script[^>]*>.*?</script>\s*){1,3}$", re.I | re.S)


def normalize_host(raw: str) -> str:
    """把 ext / 配置里的各种写法归一化为 https://host（无 path）。"""
    s = (raw or "").strip()
    if not s:
        return ""
    # JSON 写法
    if s.startswith("{"):
        try:
            j = json.loads(s)
            s = (j.get("host") or j.get("url") or "").strip()
        except Exception:
            return ""
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    try:
        p = urllib.parse.urlparse(s)
        if not p.netloc:
            return ""
        return f"{p.scheme or 'https'}://{p.netloc}"
    except Exception:
        return ""


def is_challenge_page(status: int, body: str) -> bool:
    if status != 200:
        return True
    b = (body or "").strip()
    if not b:
        return True
    # JSON 响应（苹果 CMS）本身可能很短，不能按长度判拦截。
    if b.startswith("{"):
        try:
            json.loads(b)
            return False
        except Exception:
            pass
    if len(b) < 200:
        return True
    low = b.lower()
    if any(m.lower() in low for m in _CHALLENGE_MARKERS):
        return True
    if _SHELL_RE.match(b):
        return True
    return False


def is_valid_cms_json(body: str) -> bool:
    try:
        j = json.loads(body)
    except Exception:
        return False
    if not isinstance(j, dict):
        return False
    return j.get("code") in (1, "1") and isinstance(j.get("list"), list)


def is_valid_list_page(status: int, body: str, expect_json: bool = False) -> bool:
    if is_challenge_page(status, body):
        return False
    if expect_json:
        return is_valid_cms_json(body)
    # HTML：含视频详情 / 列表链接即算正常页。
    return bool(re.search(r"/video/|/vod/|vod_id|vod_name", body or ""))


def candidates(working: str, ext: str, primary: str,
               backups: list[str] | None = None) -> list[str]:
    """按优先级去重后的候选域名列表。"""
    out: list[str] = []
    for raw in [working, ext, primary, *(backups or [])]:
        h = normalize_host(raw)
        if h and h not in out:
            out.append(h)
    return out


def heal(primary_api: str, ext: str = "", backups: list[str] | None = None,
         working: str = "", fetch=None, expect_json: bool = True) -> str | None:
    """依次试候选域名，返回第一个返回正常内容的完整 API 地址，全部失败返回 None。

    fetch(url) -> (status, body)，由调用方提供（便于测试注入假请求）。
    primary_api 是主域名的完整 API 地址（如 .../api.php/provide/vod/at/json），
    换域名时只换 host，path / query 保持不变。
    """
    if fetch is None:
        return None
    try:
        primary_host = normalize_host(primary_api)
    except Exception:
        return None
    if not primary_host:
        return None
    path = urllib.parse.urlparse(primary_api).path or ""
    query = urllib.parse.urlparse(primary_api).query
    for host in candidates(working, ext, primary_host, backups):
        url = host + path + (("?" + query) if query else "")
        try:
            status, body = fetch(url)
        except Exception:
            continue
        if is_valid_list_page(status, body, expect_json=expect_json):
            return url
    return None
