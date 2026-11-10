# -*- coding: utf-8 -*-
"""飞牛影视账号识别（只读官方库）。

飞牛影视每个请求都带登录 token（``Authorization`` 头或 ``Trim-MC-token``
cookie）。官方媒体服务用它在
``trimactivity.db.user_token`` 里映射到 ``user_guid``，用户名/管理员标记在
``trimmedia.db.user``。我们**只读**这两个 SQLite 库（``mode=ro``，不写不锁），
并在内存里缓存映射，从而在不改 App、不重放签名的前提下知道“现在是哪个账号在看”。

任何一步失败（库路径变化/无权限/库被 WAL 占用）都安全降级为“未知账号”，
由调用方按默认开关处理（fail-open：未知账号默认可见、可在线搜索）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

# 官方媒体服务数据目录（trim-media 以 --root 指向；多卷兜底）
_DB_CANDIDATES = [
    "/usr/local/apps/@appdata/trim.media/database",
    "/vol2/@appdata/trim.media/database",
    "/vol1/@appdata/trim.media/database",
]


def _db_dir() -> str | None:
    for d in _DB_CANDIDATES:
        if os.path.isfile(os.path.join(d, "trimmedia.db")):
            return d
    return None


_lock = threading.Lock()
_cache: dict[str, tuple[float, dict | None]] = {}
_TTL = 30.0   # token→用户 缓存秒数（登录会话较长，30s 足够省 DB 压力）
_NEG_TTL = 10.0


def _ro_connect(path: str) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro&immutable=0"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def token_from_request(request) -> str:
    """从 Starlette 请求里取登录 token。"""
    try:
        tok = request.headers.get("authorization") or \
            request.headers.get("trim-mc-token")
        if tok:
            return tok.strip()
        ck = request.headers.get("cookie") or ""
        for part in ck.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k.strip().lower() == "trim-mc-token":
                    return v.strip()
    except Exception:
        return ""
    return ""


def _lookup(token: str) -> dict | None:
    d = _db_dir()
    if not d or not token:
        return None
    try:
        act = os.path.join(d, "trimactivity.db")
        med = os.path.join(d, "trimmedia.db")
        with _ro_connect(act) as c:
            row = c.execute(
                "SELECT user_guid FROM user_token WHERE token=? AND status=1 "
                "ORDER BY update_time DESC LIMIT 1", (token,)).fetchone()
        if not row:
            return None
        ug = row["user_guid"]
        with _ro_connect(med) as c:
            u = c.execute(
                "SELECT guid,username,is_admin FROM user WHERE guid=?",
                (ug,)).fetchone()
        if not u:
            return {"guid": ug, "name": "", "is_admin": False}
        return {"guid": u["guid"], "name": u["username"] or "",
                "is_admin": bool(u["is_admin"])}
    except Exception:
        return None


def _result(u: dict | None, token: str) -> dict:
    if not u:
        return {"known": False, "guid": "", "name": "", "is_admin": False, "token": token}
    return {"known": True, "guid": u["guid"], "name": u["name"],
            "is_admin": u["is_admin"], "token": token}


def resolve(request) -> dict:
    """返回当前账号：{known, guid, name, is_admin, token}。

    known=False 表示识别不了（本地/异常），调用方按默认策略处理。

    P1-21：锁外查库、锁内只换缓存。原先 ``with _lock`` 内调
    ``_lookup``（3 次 isfile + 两次 SQLite 查询），未命中时并发请求
    串行等 DB。现为双重检查：
    1) 锁外先查缓存（dict.get 是原子的），命中直接走快路径；
    2) 未命中时锁外查库（SQLite I/O 不持锁，并发请求不再互相堵）；
    3) 再加锁写入缓存，写前再查一次——并发请求可能已写入更新鲜的
       条目，别用本次查到的旧结果覆盖它。
    TTL 语义不变：正缓存 30s、负缓存 10s；查库失败仍返回未知身份，
    由调用方按 fail-closed 处理。
    """
    token = token_from_request(request)
    if not token:
        return {"known": False, "guid": "", "name": "", "is_admin": False, "token": ""}
    now = time.time()
    hit = _cache.get(token)
    if hit and now - hit[0] < (TTL_VALID if hit[1] else _NEG_TTL):
        return _result(hit[1], token)
    u = _lookup(token)
    write_now = time.time()
    with _lock:
        cur = _cache.get(token)
        if cur and write_now - cur[0] < (TTL_VALID if cur[1] else _NEG_TTL):
            # 并发请求已写入更新鲜的缓存：用它，别覆盖
            u = cur[1]
        else:
            _cache[token] = (write_now, u)
            if len(_cache) > 2000:
                # 简单淘汰最旧的一半
                for k in sorted(_cache, key=lambda x: _cache[x][0])[:1000]:
                    _cache.pop(k, None)
    return _result(u, token)


TTL_VALID = _TTL


def list_users() -> list[dict]:
    """列出官方媒体库账号，供管理页授权（只读）。"""
    d = _db_dir()
    if not d:
        return []
    try:
        with _ro_connect(os.path.join(d, "trimmedia.db")) as c:
            rows = c.execute(
                "SELECT guid,username,is_admin,status FROM user "
                "WHERE guid<>'default-user-template' ORDER BY is_admin DESC, username"
            ).fetchall()
        return [{"guid": r["guid"], "name": r["username"] or r["guid"][:8],
                 "is_admin": bool(r["is_admin"]), "status": r["status"]} for r in rows]
    except Exception:
        return []
