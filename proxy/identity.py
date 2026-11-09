#!/usr/bin/env python3
"""当前飞牛用户识别：借客户端自己的会话向官方 user/info 查询。

依据（两个独立的公开客户端实现一致）：
- 飞牛影视 API（/v/api/v1/...）每次请求都带 Authorization 会话令牌；
- GET /v/api/v1/user/info 返回当前用户 {guid, username, is_admin}；
- GET /v/api/v1/manager/user/list 返回管理员可见的用户清单
  [{guid, username, is_admin}]。

本模块绝不伪造身份、绝不自己算签名、绝不存密码：只把本次请求自带
的 Authorization（以及客户端自己算好的 Authx，如有）原样转发给官方
user/info。官方认不出时返回 None，调用方按“身份未知”处理——默认
放行，不锁死任何库。

未在真机上验证的一步：user/info 是否接受回放的 Authx（签名里绑了
原请求路径），或只凭 Authorization 就够。真机上若识别不到，日志里
会有 identity_lookup_failed 警告，库保持全部可见。
"""
from __future__ import annotations

import hashlib
import time

from fnlog import get_logger

_log = get_logger("identity")

USER_INFO_PATH = "/v/api/v1/user/info"
USER_LIST_PATH = "/v/api/v1/manager/user/list"

_CACHE_TTL = 600          # 同一令牌 10 分钟内只查一次身份
_CACHE_MAX = 256
_WARN_MIN_INTERVAL = 60.0

# sha256(令牌) -> (用户名或 None, 过期时间）。键是摘要，令牌本身不落盘、不进日志。
_cache: dict[str, tuple[str | None, float]] = {}
_warn_until = 0.0


def _warn_throttled(msg: str, *args) -> None:
    global _warn_until
    now = time.time()
    if now < _warn_until:
        return
    _warn_until = now + _WARN_MIN_INTERVAL
    _log.warning(msg, *args)


def _cache_get(token: str) -> tuple[bool, str | None]:
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[1] > time.time():
        return True, hit[0]
    return False, None


def _cache_put(token: str, username: str | None) -> None:
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    if len(_cache) >= _CACHE_MAX:
        for k, (_u, exp) in list(_cache.items()):
            if exp <= now:
                del _cache[k]
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
    _cache[key] = (username, now + _CACHE_TTL)


def _auth_headers(request, token: str) -> dict:
    """只转发客户端自己的凭证：Authorization + 原样回放的 Authx。"""
    headers = {"Authorization": token, "Host": "localhost"}
    authx = request.headers.get("authx")
    if authx:
        headers["Authx"] = authx
    return headers


async def _lookup(client, request, token: str, path: str):
    """经上游 socket 调一个官方接口，返回解析后的 JSON dict，失败返回 None。"""
    try:
        r = await client.get("http://localhost" + path,
                             headers=_auth_headers(request, token), timeout=5)
    except Exception as exc:  # noqa: BLE001
        _warn_throttled("identity_lookup_failed path=%s err=%s",
                        path, type(exc).__name__)
        return None
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200 or not isinstance(j, dict) or j.get("code") != 0:
        return None
    data = j.get("data")
    return data if isinstance(data, (dict, list)) else None


async def current_username(request) -> str | None:
    """返回当前请求的飞牛用户名；识别不到返回 None（调用方按放行处理）。"""
    token = (request.headers.get("authorization") or "").strip()
    if not token:
        return None
    hit, username = _cache_get(token)
    if hit:
        return username
    try:
        client = request.app.state.up
    except Exception:  # noqa: BLE001
        return None
    data = await _lookup(client, request, token, USER_INFO_PATH)
    username = None
    if isinstance(data, dict):
        username = (data.get("username") or "").strip() or None
    if username is None:
        _warn_throttled("identity_lookup_failed path=%s err=%s",
                        USER_INFO_PATH, "no username in response")
    _cache_put(token, username)
    return username


async def list_users(request) -> list[dict] | None:
    """管理员可见的用户清单 [{guid, username, is_admin}]。

    失败（无权限/接口不通）返回 None，调用方回退到手动填用户名。
    不走缓存：管理员改完用户后立刻能在管理页看到新清单。
    """
    token = (request.headers.get("authorization") or "").strip()
    if not token:
        return None
    try:
        client = request.app.state.up
    except Exception:  # noqa: BLE001
        return None
    data = await _lookup(client, request, token, USER_LIST_PATH)
    if not isinstance(data, list):
        return None
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        name = (it.get("username") or "").strip()
        if not name:
            continue
        out.append({"guid": str(it.get("guid") or ""),
                    "username": name,
                    "is_admin": 1 if it.get("is_admin") else 0})
    return out
