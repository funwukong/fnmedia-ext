#!/usr/bin/env python3
"""AList HTTP credentials used by the xlib media library.

The CSP Jar/JVM runner has been removed. This module keeps only the HTTP API
needed by proxy.xlib and the admin credentials page.
"""
from __future__ import annotations
import json
import logging
import os
import time
import httpx

_log = logging.getLogger("fnmedia")
_PRIVATE_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catvod.local.json")

def _load_private() -> dict:
    try:
        with open(_PRIVATE_CONF, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        _log.debug("_load_private failed", exc_info=True)
        return {}

_priv = _load_private().get("xiaoya", {})
XIAOYA = {
    "server": os.environ.get("FNM_XIAOYA_SERVER", _priv.get("server", "")),
    "username": os.environ.get("FNM_XIAOYA_USER", _priv.get("username", "")),
    "password": os.environ.get("FNM_XIAOYA_PASSWORD", _priv.get("password", "")),
    "ext": _priv.get("ext"),
    "drives": [{"name": n, "startPage": p} for n, p in (
        ("电影", "/电影"), ("电视剧", "/电视剧"), ("综艺", "/综艺"),
        ("纪录片", "/纪录片"), ("动漫", "/动漫"))],
}

def _save_private(data: dict):
    tmp = _PRIVATE_CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _PRIVATE_CONF)

def private_status() -> dict:
    return {"available": available(), "server": XIAOYA["server"],
            "username": XIAOYA["username"], "has_password": bool(XIAOYA["password"]),
            "drives": len(XIAOYA.get("ext") or XIAOYA.get("drives") or []),
            "custom_ext": bool(XIAOYA.get("ext"))}

def save_credentials(server: str | None = None, username: str | None = None,
                     password: str | None = None) -> dict:
    data = _load_private(); xy = dict(data.get("xiaoya", {}))
    if server is not None and server.strip(): xy["server"] = server.strip().rstrip("/")
    if username is not None and username.strip(): xy["username"] = username.strip()
    if password: xy["password"] = password
    data["xiaoya"] = xy; _save_private(data)
    for key in ("server", "username", "password", "ext"):
        if key in xy and xy[key]: XIAOYA[key] = xy[key]
    _token_cache.clear(); return private_status()

def available() -> bool:
    return bool(str(XIAOYA.get("server") or "").strip())

def xiaoya_ext(server: str | None = None) -> list[dict]:
    return XIAOYA.get("ext") or XIAOYA.get("drives") or []

_token_cache: dict[str, tuple[float, str]] = {}
_TOKEN_TTL = 90 * 60

async def xiaoya_token(server: str | None = None, force: bool = False) -> str:
    base = (server or XIAOYA["server"]).rstrip("/")
    if not XIAOYA["password"]: return ""
    hit = _token_cache.get(base)
    if not force and hit and time.time() - hit[0] < _TOKEN_TTL: return hit[1]
    async with httpx.AsyncClient(trust_env=False, timeout=12) as client:
        r = await client.post(base + "/api/auth/login",
                              json={"username": XIAOYA["username"],
                                    "password": XIAOYA["password"]})
        j = r.json()
    if j.get("code") != 200: raise RuntimeError(f"小雅登录失败: {j.get('message')}")
    tok = j["data"]["token"]; _token_cache[base] = (time.time(), tok); return tok
