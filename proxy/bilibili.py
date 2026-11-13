"""B站登录态桥：扫码保存 Cookie，海阔 B 站规则读取并自动选高清。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

_STATE = Path(__file__).resolve().parent.parent / "state" / "bilibili_cookie.json"
_HAIKUO_DIR = Path(__file__).resolve().parent / "assets" / "haikuo" / "sub"
_COOKIE_FILE = _HAIKUO_DIR / "Bilibili" / "Cookie" / "bilibili_cookie.txt"

_QR_START = "https://api.bilibili.com/x/frontend/webnav"
_QR_LOGIN = "https://passport.bilibili.com/qrcode/generator"
_POLL = "https://api.bilibili.com/x/web-interface/nav"

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
        try: os.unlink(tmp)
        except OSError: pass

def qr_start() -> dict:
    """B站扫码入口：返回可生成二维码的 B 站扫码 URL，前端扫码后把 Cookie 回填。"""
    s = _load()
    qr_url = str(s.get("qr_url") or "https://passport.bilibili.com/qrcode/login")
    return {"state": "waiting", "qr_url": qr_url, "qr_text": qr_url,
            "poll": "/v/api/ext/bilibili/qr/poll"}

def qr_poll(payload: dict) -> dict:
    code = str(payload.get("code") or payload.get("cookie") or "").strip()
    if not code:
        return {"state": "waiting", "msg": "等待扫码完成"}
    if not any(x in code for x in ("SESSDATA", "bili_jct", "DedeUserID")):
        raise ValueError("二维码未返回 B 站登录态 Cookie")
    now = int(time.time())
    state = {"cookie": code, "mid": str(payload.get("mid") or ""),
             "vip": bool(payload.get("vip")), "updated_at": now}
    _save(state)
    _write_rule_cookie(code)
    return {"state": "done", "mid": state["mid"], "vip": state["vip"]}

def set_cookie(cookie: str) -> dict:
    code = str(cookie or "").strip().replace("\n", " ").replace("\r", "")
    if not any(x in code for x in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")):
        raise ValueError("Cookie 缺少 SESSDATA / bili_jct / buvid")
    now = int(time.time())
    state = {"cookie": code, "mid": "", "vip": False, "updated_at": now}
    _save(state)
    _write_rule_cookie(code)
    return {"state": "saved", "cookie_set": True}

def status() -> dict:
    s = _load()
    cookie = str(s.get("cookie") or "")
    out = {"cookie_set": bool(cookie), "mid": str(s.get("mid") or ""),
           "vip": bool(s.get("vip")), "updated_at": int(s.get("updated_at") or 0),
           "has_sessdata": "SESSDATA" in cookie, "has_jct": "bili_jct" in cookie}
    return out

def clear() -> None:
    try:
        _STATE.unlink()
    except OSError:
        pass
    _write_rule_cookie("")

def current_cookie() -> str:
    return str(_load().get("cookie") or "")

def _write_rule_cookie(cookie: str) -> None:
    _COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COOKIE_FILE.write_text(cookie, encoding="utf-8")
    os.chmod(_COOKIE_FILE, 0o600)
