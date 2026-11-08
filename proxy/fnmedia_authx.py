#!/usr/bin/env python3
"""飞牛影视 trim.media 的 authx 请求签名客户端（逆向自前端 chunk）。

签名算法（见前端 14a77ac...js）：
  header 名: authx
  值: nonce=<6位随机>&timestamp=<毫秒>&sign=<md5>
  GET : data = 排序后的 canonical query（值 URL 编码，空格 %20）
        o = md5(decodeURIComponent(data))
  POST: data = JSON.stringify(body); o = md5(data)
  raw = [SECRET, path(含 /v), nonce, timestamp, o, apiKey].join("_")
  sign = md5(raw)
已实测：哈希为 MD5（chunk 内另含 sha256 实现但默认导出 md5），path 含 /v 前缀。
"""
from __future__ import annotations

import hashlib
import json
import random
import socket
import time
import urllib.parse
from typing import Any

SECRET = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
APIKEY = "16CCEB3D-AB42-077D-36A1-F355324E4237"
UPSTREAM = "/var/run/trim_media_upstream.sock"   # 接管态下直连官方
TARGET = "/var/run/trim_media.sock"               # 未接管时用


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _canon_query(params: dict[str, Any]) -> str:
    # 对齐前端 pu(): key 排序，append String(value)，URLSearchParams 编码，+ -> %20
    from urllib.parse import quote
    parts = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        parts.append(f"{k}=" + quote(str(v), safe=""))
    return "&".join(parts).replace("+", "%20")


def make_authx(method: str, path: str, params: dict | None = None,
               body: Any = None, api_key: str = APIKEY) -> tuple[str, str]:
    method = method.upper()
    nonce = str(random.randint(100000, 999999))
    ts = str(int(time.time() * 1000))
    if method == "GET":
        qs = _canon_query(params or {})
        o = _md5(urllib.parse.unquote(qs))
        data = qs
    else:
        data = json.dumps(body) if body is not None else ""
        o = _md5(data)
    raw = "_".join([SECRET, path, nonce, ts, o, api_key])
    sign = _md5(raw)
    return f"nonce={nonce}&timestamp={ts}&sign={sign}", data


def call(method: str, path: str, params: dict | None = None, body: Any = None,
         extra_headers: dict | None = None, sock: str = TARGET,
         raw: bool = False, timeout: float = 30) -> Any:
    method = method.upper()
    authx, data = make_authx(method, path, params, body)
    url = path
    if method == "GET" and params:
        # 用 canonical query 发，保证服务端复算一致
        url = f"{path}?{data}"
    headers = {
        "Host": "localhost",
        "Connection": "close",
        "authx": authx,
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    payload = b""
    if method != "GET" and body is not None:
        payload = data.encode("utf-8")
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(sock)
    req = f"{method} {url} HTTP/1.1\r\n"
    for k, v in headers.items():
        req += f"{k}: {v}\r\n"
    req += f"Content-Length: {len(payload)}\r\n\r\n"
    s.sendall(req.encode() + payload)
    chunks = b""
    try:
        while True:
            c = s.recv(65536)
            if not c:
                break
            chunks += c
    except socket.timeout:
        pass
    s.close()
    head, _, b = chunks.partition(b"\r\n\r\n")
    if b"chunked" in head.lower():
        out = b""
        rest = b
        while True:
            nl = rest.find(b"\r\n")
            if nl < 0:
                break
            try:
                n = int(rest[:nl].split(b";")[0], 16)
            except ValueError:
                break
            if n == 0:
                break
            out += rest[nl + 2:nl + 2 + n]
            rest = rest[nl + 2 + n + 2:]
        b = out
    if raw:
        return head.decode("latin1"), b
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return {"_raw": b[:500].decode("utf-8", "replace"), "_head": head.decode("latin1")[:300]}


if __name__ == "__main__":
    import os
    use = UPSTREAM if os.path.exists(UPSTREAM) else TARGET
    print("using socket:", use)
    print(json.dumps(call("GET", "/v/api/v1/server/info", sock=use),
                     ensure_ascii=False)[:300])
