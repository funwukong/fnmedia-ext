"""B 站登录态：真扫码协议（passport 二维码）+ Cookie 保存。

协议（2026-11-13 实测 passport.bilibili.com，无需登录即可调用）：
  POST/GET /x/passport-login/web/qrcode/generate
      → {data:{url, qrcode_key}}
  GET /x/passport-login/web/qrcode/poll?qrcode_key=…
      → {data:{code, message, url, refresh_token}}
         code 86101 未扫码 / 86090 已扫码待确认 / 86038 已过期 / 0 成功
成功时响应带 Set-Cookie（SESSDATA、bili_jct、DedeUserID…），
从 Cookie 头取回并落盘；凭据只写本地 state（0600），不回显、不进日志。

会话状态在内存（ttl 120s、单会话、并发锁），扫码由用户在手机 B 站完成。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx

_STATE = Path(__file__).resolve().parent.parent / "state" / "bilibili_cookie.json"
# Cookie 只在本模块 state 内保存（不再写海阔规则目录，那套已删除）
_COOKIE_FILE = Path(__file__).resolve().parent.parent / "state" /"bilibili_cookie.txt"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_REF = "https://www.bilibili.com/"
_GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

_SESSION_TTL = 120.0
_LOCK = threading.Lock()
_SESSION: dict = {}

# B 站扫码状态码
_QC_WAIT = 86101        # 未扫码
_QC_SCANNED = 86090      # 已扫码，待手机确认
_QC_EXPIRED = 86038      # 二维码过期
_QC_CONFIRM = 86033      # 已被确认（部分版本）
_QC_OK = 0               # 成功


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8")) if _STATE.exists() else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bilibili-cookie-", dir=str(_STATE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _STATE)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _headers() -> dict:
    return {"User-Agent": _UA, "Referer": _REF, "Origin": "https://www.bilibili.com"}


def _extract_cookie(resp) -> str:
    """从 Set-Cookie 里取出拼好的 Cookie 串（只留登录相关字段）。"""
    raw = []
    try:
        raw = resp.headers.get_list("set-cookie")
    except Exception:
        one = resp.headers.get("set-cookie")
        raw = [one] if one else []
    keep = []
    for line in raw:
        if not line:
            continue
        pair = line.split(";", 1)[0].strip()
        if not pair or "=" not in pair:
            continue
        name = pair.split("=", 1)[0].strip()
        if name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
                    "sid", "buvid3", "buvid4", "b_nut", "b_lsid", "bili_ticket",
                    "bili_ticket_r"):
            keep.append(pair)
    return "; ".join(keep)


def _write_rule_cookie(cookie: str) -> None:
    """同时写一份到海阔规则约定的位置（规则自己会读 bilibili_cookie.txt）。"""
    try:
        _COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIE_FILE.write_text(cookie, encoding="utf-8")
        os.chmod(_COOKIE_FILE, 0o600)
    except OSError:
        pass


def qr_png(text: str, scale: int = 9) -> bytes:
    """把扫码 URL 渲染成黑白 PNG（segno 随包提供）。"""
    import io
    from _vendor import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=4,
                                     dark="black", light="white")
    return buf.getvalue()


def _client() -> httpx.Client:
    """B 站 passport 客户端。

    直连优先（trust_env=False）；本机若只有代理才能出网（DNS 解析失败），
    退回带环境代理的客户端，避免扫码在代理环境下不可用。
    """
    try:
        c = httpx.Client(headers=_headers(), timeout=15.0, trust_env=False,
                         follow_redirects=True)
        c.get(_GEN_URL)
        c.close()
        return httpx.Client(headers=_headers(), timeout=15.0, trust_env=True,
                           follow_redirects=True)
    except Exception:
        return httpx.Client(headers=_headers(), timeout=15.0, trust_env=True,
                           follow_redirects=True)


def _qr_data_url(text: str) -> str:
    """二维码 PNG 的 data: URL（内联，管理页直接 <img src=>）。"""
    try:
        import base64
        return "data:image/png;base64," + base64.b64encode(
            qr_png(text)).decode()
    except Exception:
        return ""


def qr_start() -> dict:
    """生成扫码二维码，返回二维码 URL 与不透明会话 ID（不回显 cookie）。"""
    global _SESSION
    with _client() as c:
        r = c.get(_GEN_URL)
        r.raise_for_status()
        j = r.json()
    if int(j.get("code") or 0) != 0:
        raise RuntimeError("B 站扫码初始化失败：%s" % str(j.get("message"))[:120])
    data = j.get("data") or {}
    key = str(data.get("qrcode_key") or "")
    url = str(data.get("url") or "")
    if not key or not url:
        raise RuntimeError("B 站扫码初始化未返回二维码")
    sid = key[:16]
    with _LOCK:
        _SESSION[sid] = {"key": key, "created": time.monotonic()}
        # 只保留最近的会话，避免内存里堆过期 key
        for k in [x for x in _SESSION
                  if time.monotonic() - _SESSION[x]["created"] > _SESSION_TTL * 3]:
            _SESSION.pop(k, None)
    return {"state": "waiting", "qr_url": url, "qr_text": url,
            "session": sid, "expires_in": int(_SESSION_TTL),
            "qr_png": _qr_data_url(url)}


def qr_poll(payload: dict) -> dict:
    """轮询扫码状态；成功后保存 Cookie 并返回 done（不含凭据）。"""
    sid = str(payload.get("session") or "").strip()
    with _LOCK:
        sess = _SESSION.get(sid)
        if sess is None:
            raise ValueError("扫码会话已过期，请重新生成二维码")
        if time.monotonic() - sess["created"] > _SESSION_TTL:
            _SESSION.pop(sid, None)
            raise ValueError("二维码已过期，请重新生成")
        key = sess["key"]
    with _client() as c:
        r = c.get(_POLL_URL, params={"qrcode_key": key})
        r.raise_for_status()
        j = r.json()
    if int(j.get("code") or 0) != 0:
        raise RuntimeError("B 站扫码轮询失败：%s" % str(j.get("message"))[:120])
    data = j.get("data") or {}
    code = int(data.get("code") or 0)
    if code == _QC_WAIT:
        return {"state": "waiting", "msg": str(data.get("message") or "未扫码")}
    if code == _QC_SCANNED or code == _QC_CONFIRM:
        return {"state": "scanned", "msg": "已扫码，请在手机上确认登录"}
    if code == _QC_EXPIRED:
        with _LOCK:
            _SESSION.pop(sid, None)
        return {"state": "expired", "msg": "二维码已过期，请重新生成"}
    if code != _QC_OK:
        return {"state": "error", "msg": str(data.get("message") or "扫码失败")}
    cookie = _extract_cookie(r)
    if not cookie or "SESSDATA" not in cookie:
        raise RuntimeError("扫码成功但未取得登录 Cookie")
    now = int(time.time())
    _save({"cookie": cookie, "updated_at": now})
    _write_rule_cookie(cookie)
    with _LOCK:
        _SESSION.pop(sid, None)
    return {"state": "done", "cookie_set": True}


def set_cookie(cookie: str) -> dict:
    """手工填入 Cookie（扫码之外的备用路径）。"""
    code = str(cookie or "").strip().replace("\n", " ").replace("\r", "")
    if not any(x in code for x in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")):
        raise ValueError("Cookie 缺少 SESSDATA / bili_jct / DedeUserID")
    _save({"cookie": code, "updated_at": int(time.time())})
    _write_rule_cookie(code)
    return {"state": "saved", "cookie_set": True}


def status() -> dict:
    s = _load()
    cookie = str(s.get("cookie") or "")
    return {"cookie_set": bool(cookie),
            "has_sessdata": "SESSDATA" in cookie,
            "has_jct": "bili_jct" in cookie,
            "updated_at": int(s.get("updated_at") or 0)}


def clear() -> None:
    try:
        _STATE.unlink()
    except OSError:
        pass
    _write_rule_cookie("")


def current_cookie() -> str:
    return str(_load().get("cookie") or "")
