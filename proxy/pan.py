#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三方网盘播放支持（MVP：夸克网盘）。【私有：公开构建整体删除本文件】

为什么独立于 guard/redroid：网盘"把分享链接变成可播视频"靠的是各网盘官方网页
接口 + 用户自己的登录 cookie（OK影视 里弹二维码扫码即取 cookie），与加固爬虫 jar
无关，纯 HTTP 即可，在 NAS 上直接跑、无需模拟器。

链路（与 OK影视 一致）：
  1) 扫码登录：getTokenForQrcodeLogin 拿二维码 token → 手机夸克扫码确认 →
     getServiceTicketByQrcodeToken 拿 service_ticket → account/info 换 cookie，
     持久化到 state/pan_quark.json。
  2) 分享转存：sharepage/token 拿 stoken（验证提取码/有效性）→ sharepage/detail
     列文件 → sharepage/save 转到自己网盘的固定目录。
  3) 取直链：file/download 拿原画 CDN 直链（302 后即 mp4/mkv 流），交给播放器。

接口依据：夸克网页端公开接口（drive-pc.quark.cn / drive.quark.cn / uop.quark.cn）。
接口可能随官方调整；失败时给出可读错误。
"""
from __future__ import annotations

import json
import os
import http.client
import re
import socket
import ssl
import sys
import time
import uuid

import httpx


def _ssl_ctx() -> ssl.SSLContext:
    # 每个 client 独立的 SSL 上下文，并禁用 TLS Session Ticket：
    # 夸克风控会标记复用同一会话票据的连接（访问带码分享后再打 drive-pc
    # 会持续 401）。证书校验保持开启——只禁 Session Ticket，不关校验：
    # 目标是官方域名（证书正常），且请求带登录 Cookie。
    ctx = ssl.create_default_context()
    try:
        ctx.options |= ssl.OP_NO_TICKET
    except Exception:
        pass
    return ctx


# 阿里系网盘域名 AAAA 解析到内网 IPv6，Python 默认优先 IPv6 会握手超时
# （实测 20s+），强制 IPv4 后 0.2s。夸克/UC 外联统一走 IPv4。
def _v4_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport(local_address="0.0.0.0")


class _V4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port or 443,
                                   socket.AF_INET, socket.SOCK_STREAM)
        sock = socket.create_connection(infos[0][4], self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

# 优先用内联在 proxy/_vendor 的 segno（fpk 现场无需联网安装），回退系统包
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
try:
    import segno  # type: ignore
except Exception:  # noqa: BLE001
    segno = None  # type: ignore


def qr_png(text: str, scale: int = 9) -> bytes:
    """把文本渲染成黑白 PNG 二维码（登录扫码用）。"""
    import io
    if segno is None:
        raise PanError("二维码组件未随包提供")
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=4,
                                     dark="black", light="white")
    return buf.getvalue()


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Origin": "https://pan.quark.cn",
    "Referer": "https://pan.quark.cn/",
    "Content-Type": "application/json;charset=UTF-8",
}
BASE = "https://drive-pc.quark.cn/1/clouddrive"
SHARE = "https://drive.quark.cn/1/clouddrive"
_UOP = "https://uop.quark.cn/cas/ajax"

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "pan_quark.json")
# 转存目标文件夹名（用户网盘内）
SAVE_DIR = "fnmedia转存"

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".m4v", ".flv", ".ts", ".webm",
             ".rmvb", ".rm", ".wmv", ".mpg", ".mpeg", ".m2ts", ".iso")


class PanError(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------- 持久化
def _load() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, _STATE_PATH)
    try:
        os.chmod(_STATE_PATH, 0o600)
    except Exception:
        pass


# __puus（取链/CDN checkplay 必需，约 3 小时过期）：网页扫码/复制的 cookie 常
# 只有 __pus。用 drive-pc/config 且“不带 __puus”请求，服务端会在 Set-Cookie
# 重新下发（与 UC/AList refreshPuus 同源机制）。
_PUUS_TTL = 80 * 60
_PC_BASE = "https://drive-pc.quark.cn/1/clouddrive"


def _cookie_map(cookie: str) -> dict:
    out = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _merge_cookie(cookie: str, pairs: dict) -> str:
    m = _cookie_map(cookie)
    m.update(pairs)
    return "; ".join(f"{k}={v}" for k, v in m.items() if v != "")


def _refresh_puus(cookie: str) -> str:
    import urllib.parse
    base_ck = "; ".join(f"{k}={v}" for k, v in _cookie_map(cookie).items()
                        if k != "__puus")
    u = urllib.parse.urlparse(f"{_PC_BASE}/config?pr=ucpro&fr=pc")
    conn = _V4HTTPSConnection(u.netloc, context=_ssl_ctx(), timeout=15)
    try:
        conn.request("GET", u.path + "?" + u.query, headers={
            "Cookie": base_ck, "Referer": "https://pan.quark.cn/",
            "User-Agent": _HEADERS["User-Agent"], "Connection": "close",
            "Accept": "application/json"})
        r = conn.getresponse()
        r.read()
        fresh = {}
        for (k, v) in r.getheaders():
            if k.lower() == "set-cookie":
                name = v.split("=", 1)[0].strip()
                if name in ("__puus", "__pus"):
                    fresh[name] = v.split(";", 1)[0].split("=", 1)[1]
    finally:
        conn.close()
    if not fresh.get("__puus"):
        raise PanError("刷新夸克会话票据(__puus)失败，请重新扫码登录")
    return _merge_cookie(cookie, fresh)


def _active_cookie(force: bool = False) -> str:
    st = _load()
    cookie = st.get("cookie") or ""
    if not cookie:
        raise PanError("尚未登录夸克")
    age = _now() - int(st.get("puus_ts") or 0)
    if (not force) and "__puus" in _cookie_map(cookie) and age < _PUUS_TTL:
        return cookie
    newck = _refresh_puus(cookie)
    st["cookie"] = newck
    st["puus_ts"] = _now()
    _save(st)
    return newck


def _client(cookie: str = "", keep_jar: bool = False) -> httpx.Client:
    h = dict(_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    # 网盘是外网，绝不走 Clash 环境变量；由系统直连（实测可达）。
    # 关键：禁用 keep-alive 连接复用。分享域(drive.quark.cn)与个人网盘域
    # (drive-pc.quark.cn)同 IP，访问带码分享后该持久连接会被风控标记，
    # httpx 若复用它打 drive-pc 会持续 401（手动 Connection:close 头无效，
    # 必须在 transport 层 max_keepalive_connections=0）。
    cli = httpx.Client(verify=_ssl_ctx(), trust_env=False, timeout=20, headers=h,
                       follow_redirects=False, transport=_v4_transport(),
                       limits=httpx.Limits(max_keepalive_connections=0,
                                           max_connections=8))
    if keep_jar:
        # 扫码登录：需要保留服务端下发的 _UP_* 会话 cookie
        cli._pan_auth = cookie  # type: ignore[attr-defined]
        return cli
    auth = cookie

    def _strip_jar(request):
        # 只保留我们显式传入的登录 Cookie，丢弃夸克各域 Set 的临时 cookie
        # （drive.quark.cn 下发的 __sdid 再带到 drive-pc.quark.cn 会触发风控 401）。
        if auth:
            request.headers["Cookie"] = auth
        else:
            request.headers.pop("cookie", None)

    cli.event_hooks["request"].append(_strip_jar)
    cli._pan_auth = auth  # type: ignore[attr-defined]
    return cli


def logged_in() -> bool:
    return bool(_load().get("cookie"))


def account_info_safe() -> dict:
    st = _load()
    return {"logged_in": bool(st.get("cookie")),
            "nickname": st.get("nickname", ""),
            "uid": st.get("uid", ""),
            "saved_ts": st.get("saved_ts", 0)}


# 进行中的扫码会话：token -> httpx.Client（保留申请二维码时下发的 _UP_* cookie）
_QR_SESSIONS: dict[str, "httpx.Client"] = {}


def _cookie_header(c: "httpx.Client") -> str:
    return "; ".join(f"{k}={v}" for k, v in c.cookies.items())


# --------------------------------------------------------------- 扫码登录
def qr_start() -> dict:
    """生成登录会话：返回二维码内容与轮询所需 token。"""
    rid = str(uuid.uuid4()).upper()
    c = _client(keep_jar=True)
    r = c.get(f"{_UOP}/getTokenForQrcodeLogin",
              params={"client_id": 532, "v": 1.2, "request_id": rid})
    j = r.json()
    if j.get("status") != 2000000:
        c.close()
        raise PanError(f"获取二维码失败：{j.get('message') or j.get('status')}")
    token = (j.get("data") or {}).get("members", {}).get("token")
    if not token:
        c.close()
        raise PanError("二维码 token 为空")
    _QR_SESSIONS[token] = c
    # 与夸克网页端一致：把 token 拼到官方扫码落地页（su.quark.cn 短链页），
    # 并对 uc_biz_str 完整 URL 编码（未编码的 | : @ 会让夸克 App 误判成下载页）。
    from urllib.parse import urlencode
    q = urlencode({
        "token": token,
        "client_id": "532",
        "ssb": "weblogin",
        "uc_param_str": "",
        "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
    })
    return {"token": token, "request_id": rid,
            "qr_text": f"https://su.quark.cn/4_eMHBJ?{q}"}


def qr_poll(session: dict) -> dict:
    """轮询扫码状态。返回 {status: waiting|scanned|success|expired, ...}。"""
    c = _QR_SESSIONS.get(session["token"]) or _client()
    try:
        r = c.get(f"{_UOP}/getServiceTicketByQrcodeToken", params={
            "client_id": 532, "v": 1.2, "token": session["token"],
            # 必须与申请二维码时同一个 request_id，否则返回 Token Not Found
            "request_id": session.get("request_id") or str(uuid.uuid4()).upper()})
        j = r.json()
        st = j.get("status")
        members = (j.get("data") or {}).get("members", {}) or {}
        ticket = members.get("service_ticket")
        if st == 2000000 and ticket:
            # 用同一会话（带 _UP_* cookie）的 ticket 换登录 cookie
            cookie, nickname, uid = _redeem_ticket(ticket, c)
            stt = _load()
            stt.update(cookie=cookie, nickname=nickname, uid=uid,
                       saved_ts=_now())
            _save(stt)
            _QR_SESSIONS.pop(session["token"], None)
            c.close()
            return {"status": "success", "nickname": nickname}
        if st in (50004002, 50004003, 50004004):
            return {"status": "expired", "message": j.get("message", "")}
        # 50004001 等待扫码；50003001 类为“已扫码待确认”
        if members or "scan" in str(j.get("message", "")).lower():
            return {"status": "scanned", "status_code": st,
                    "message": j.get("message", "")}
        return {"status": "waiting", "status_code": st,
                "message": j.get("message", "")}
    finally:
        if session["token"] not in _QR_SESSIONS:
            try:
                c.close()
            except Exception:
                pass


def _redeem_ticket(ticket: str, c: "httpx.Client|None" = None) -> tuple[str, str, str]:
    own = c is None
    c = c or _client()
    try:
        r = c.get("https://pan.quark.cn/account/info",
                  params={"st": ticket, "lw": "scan"})
        cookie = _cookie_header(c)
        try:
            j = r.json()
        except Exception:
            j = {}
        data = j.get("data") or {}
        nickname = data.get("nickname", "")
        uid = str(data.get("uid", ""))
    finally:
        if own:
            c.close()
    if not cookie or "__pus" not in cookie:
        raise PanError("扫码成功但未取得有效 cookie")
    try:
        cookie = _refresh_puus(cookie)
    except Exception:
        pass
    return cookie, nickname, uid


def set_cookie(raw: str) -> dict:
    """直接导入用户从 pan.quark.cn 复制的 Cookie，校验并持久化。"""
    raw = (raw or "").strip()
    # 兼容把 "cookie: xxx" / "Cookie：xxx" 整行粘进来
    raw = re.sub(r"^\s*cookie\s*[:：]\s*", "", raw, flags=re.I).strip()
    if "__pus" not in raw:
        raise PanError("Cookie 里缺少 __pus 字段，可能复制不完整，请重登后整行复制")
    nickname, uid = _whoami(raw)
    cookie = _refresh_puus(raw)
    st = _load()
    st.update(cookie=cookie, nickname=nickname, uid=uid,
              saved_ts=_now(), puus_ts=_now())
    _save(st)
    return {"nickname": nickname, "uid": uid}


def _whoami(cookie: str) -> tuple[str, str]:
    with _client(cookie) as c:
        r = c.get("https://pan.quark.cn/account/info",
                  params={"fr": "pc", "platform": "pc"})
        try:
            j = r.json()
        except Exception:
            j = {}
    data = j.get("data") or {}
    if not data:
        raise PanError("Cookie 校验失败（账号信息为空），可能已过期，请重新登录复制")
    return data.get("nickname", ""), str(data.get("uid", ""))


def logout() -> None:
    st = _load()
    for k in ("cookie", "nickname", "uid", "saved_ts", "puus_ts"):
        st.pop(k, None)
    _save(st)


# --------------------------------------------------------------- 分享解析
_SHARE_RE = re.compile(r"https?://pan\.quark\.cn/s/([A-Za-z0-9]+)")


def parse_share(url: str) -> tuple[str, str]:
    """从分享 URL 提取 (pwd_id, passcode)。"""
    m = _SHARE_RE.search(url or "")
    if not m:
        raise PanError("不是有效的夸克分享链接（应形如 https://pan.quark.cn/s/xxxx）")
    pwd_id = m.group(1)
    code = ""
    m2 = re.search(r"[?&](?:pwd|passcode)=([A-Za-z0-9]{1,6})", url)
    if m2:
        code = m2.group(1)
    return pwd_id, code


def _auth_cookie(c: httpx.Client) -> str:
    return getattr(c, "_pan_auth", "") or ""


def _api(c: httpx.Client, method: str, url: str, _retry: int = 8, **kw) -> dict:
    # 夸克在“访问带码分享”后会对 drive-pc 域返回短时风控（401/非JSON密文），
    # 间歇性、秒级~十几秒自行恢复。策略：指数退避 + 每次重建全新 client
    # （断开可能被标记的 keep-alive 连接）。
    last = ""
    cur = c
    for attempt in range(_retry):
        r = cur.request(method, url, **kw)
        bad_json = False
        try:
            j = r.json()
        except Exception:
            j = None
            bad_json = True
        risky = bad_json and r.status_code in (401, 403, 429, 503)
        biz_risk = (not bad_json and j is not None and
                    (j.get("code") in (32003, 32005, 41001)
                     or j.get("status") in (401, 429)))
        if (risky or biz_risk) and attempt < _retry - 1:
            last = f"夸克风控（HTTP {r.status_code}）"
            auth = _auth_cookie(c)
            if cur is not c:
                cur.close()
            cur = _client(auth) if auth else _fresh(cur)
            time.sleep(0.5 * (attempt + 1))
            continue
        if bad_json:
            raise PanError(f"接口返回非 JSON（HTTP {r.status_code}）")
        code = j.get("code")
        if code not in (0, None) and j.get("status") not in (2000000,):
            msg = j.get("message") or j.get("msg") or code
            raise PanError(f"夸克接口错误：{msg}")
        return j
    raise PanError(last or "夸克接口多次重试失败")


def _fresh(c: httpx.Client) -> httpx.Client:
    try:
        c.close()
    except Exception:
        pass
    return _client()


def share_token(c: httpx.Client, pwd_id: str, passcode: str) -> str:
    j = _api(c, "POST", f"{SHARE}/share/sharepage/token?pr=ucpro&fr=pc",
             json={"pwd_id": pwd_id, "passcode": passcode or "",
                   "support_visit_limit_private_share": True})
    stoken = (j.get("data") or {}).get("stoken")
    if not stoken:
        raise PanError("分享已失效或提取码错误")
    return stoken


def share_detail(c: httpx.Client, pwd_id: str, stoken: str,
                 pdir_fid: str = "0") -> list[dict]:
    j = _api(c, "GET", f"{SHARE}/share/sharepage/detail", params={
        "pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
        "pdir_fid": pdir_fid, "force": 0, "_page": 1, "_size": 100,
        "_fetch_total": 1, "_sort": "file_type:asc,file_name:asc", "ver": 2})
    return (j.get("data") or {}).get("list") or []


def _video_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if it.get("dir"):
            continue
        name = (it.get("file_name") or "").lower()
        if name.endswith(VIDEO_EXT):
            out.append(it)
    return out


def _pc(method: str, url: str, cookie: str, _tries: int = 5, **kw) -> dict:
    """个人网盘域(drive-pc)调用：stdlib + 每请求全新 SSL 连接（禁会话票据）。

    关键：访问过带码分享后，进程内复用 TLS 会话（httpx/h11 或 SSL ticket）
    会被夸克风控标记，导致 drive-pc 稳定 401。用 http.client 每次新建
    SSLContext(OP_NO_TICKET) 实测稳定；并对短时风控做退避重试。"""
    import http.client
    import urllib.parse
    u = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    for k, v in (kw.get("params") or {}).items():
        qs.append((k, str(v)))
    path = u.path + ("?" + urllib.parse.urlencode(qs) if qs else "")
    data = None
    headers = {"User-Agent": _HEADERS["User-Agent"],
               "Cookie": cookie, "Accept": "application/json",
               "Connection": "close"}
    if kw.get("json") is not None:
        data = json.dumps(kw["json"]).encode()
        headers["Content-Type"] = "application/json;charset=UTF-8"
    last = ""
    for attempt in range(_tries):
        # 证书校验保持开启（官方域名证书正常，请求带登录 Cookie）；
        # 只禁 Session Ticket 防风控 401。
        ctx = ssl.create_default_context()
        try:
            ctx.options |= ssl.OP_NO_TICKET
        except Exception:
            pass
        conn = _V4HTTPSConnection(u.netloc, context=ctx, timeout=20)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            code = resp.status
        finally:
            conn.close()
        try:
            j = json.loads(raw.decode())
        except Exception:
            j = None
        if j is not None and (j.get("code") in (0, None)
                              or j.get("status") == 2000000):
            return j
        last = f"HTTP {code} 非JSON" if j is None else (
            j.get("message") or j.get("msg") or j.get("code"))
        if attempt < _tries - 1 and (j is None or code in (401, 403, 429, 503)
                                     or j.get("code") in (32003, 32005)):
            time.sleep(0.6 * (attempt + 1))
            continue
        raise PanError(f"夸克网盘接口错误：{last}")
    raise PanError(f"夸克网盘接口多次重试失败：{last}")


def _ensure_save_dir(cookie: str) -> str:
    """找到/创建固定转存目录，返回其 fid。"""
    j = _pc("GET", f"{BASE}/file/sort?pr=ucpro&fr=pc", cookie,
            params={"pdir_fid": "0", "_page": 1, "_size": 200,
                    "_fetch_total": 1, "_sort": "file_name:asc"})
    for it in (j.get("data") or {}).get("list") or []:
        if it.get("dir") and it.get("file_name") == SAVE_DIR:
            return it["fid"]
    j = _pc("POST", f"{BASE}/file?pr=ucpro&fr=pc", cookie,
            json={"pdir_fid": "0", "file_name": SAVE_DIR, "dir_path": "",
                  "dir_init_lock": False})
    fid = (j.get("data") or {}).get("fid")
    if not fid:
        # 极少数情况下创建是异步的，回退到根目录转存
        return "0"
    return fid


def resolve_share(url: str, passcode_hint: str = "") -> dict:
    """完整解析一个夸克分享：转存→列视频→返回可播条目（含直链）。

    返回 {title, items:[{name,size,fid,url}]}。"""
    cookie = _active_cookie()
    pwd_id, passcode = parse_share(url)
    if passcode_hint and not passcode:
        passcode = passcode_hint
    # 关键：分享域(drive.quark.cn)与个人网盘域(drive-pc.quark.cn)在同进程内
    # 不能同时打开 client——底层会共享到被风控标记的 TLS/连接，使 drive-pc
    # 稳定 401。因此分两阶段：先用分享 client 完成“列分享+发起转存”，
    # 关闭后，再用一次性 client 访问个人网盘域。
    cs = _client(cookie)
    try:
        stoken = share_token(cs, pwd_id, passcode)
        root = share_detail(cs, pwd_id, stoken, "0")
        # 收集：根目录视频 + 单层子目录视频（影视仓常见“剧集在子文件夹”）
        items = list(root)
        for d in [x for x in root if x.get("dir")][:10]:
            try:
                items += share_detail(cs, pwd_id, stoken, d["fid"])
            except Exception:
                pass
        vids = _video_items(items)
        if not vids:
            raise PanError("分享内没有可播放的视频文件")
        # 转存目标目录需要先在个人网盘域确定；先关掉分享 client 再访问。
        cs.close()
        to_fid = _ensure_save_dir(cookie)
        # 重新打开分享 client 发起转存（拿到 to_fid 后），随后立即关闭。
        cs = _client(cookie)
        fid_list, tok_list = [], []
        for v in vids:
            fid_list.append(v["fid"])
            tok_list.append(v.get("share_fid_token", ""))
        try:
            _api(cs, "POST", f"{SHARE}/share/sharepage/save?pr=ucpro&fr=pc", json={
                "fid_list": fid_list, "fid_token_list": tok_list,
                "to_pdir_fid": to_fid, "pwd_id": pwd_id, "stoken": stoken,
                "pdir_fid": "0", "pdir_save_all": False, "exclude_fids": [],
                "scene": "link"})
        except PanError as exc:
            # “文件不可转存到此空间”多为：自己分享给自己 / 此前已转过。
            # 转存目录里可能已有同名文件，容错继续按文件名找回。
            if "不可转存" not in str(exc) and "已存在" not in str(exc):
                raise
    finally:
        try:
            cs.close()
        except Exception:
            pass
    # 分享 client 已彻底关闭；以下个人网盘域调用都用一次性 client。
    # 等待转存落地，按文件名在目标目录里找回真实 fid。
    saved = _wait_files(cookie, to_fid, {v["file_name"] for v in vids})
    out_items = []
    # 不在此预取直链：夸克播放/下载 URL 均为短时效签名，统一在点播放时由
    # playable_url(fid) 实时换取。播放优先走 /file/v2/play 的 super 原画档
    # （该端点无 PC download 的单文件 >2GB 限制，等价 App 客户端路径）。
    for v in vids:
        real = saved.get(v["file_name"])
        if not real:
            continue
        out_items.append({
            "name": v["file_name"], "size": int(v.get("size") or 0),
            "fid": real, "url": f"/v/api/ext/pan/play?fid={real}"})
    if not out_items:
        raise PanError("转存成功但暂未取到文件（可稍后重试）")
    out_items.sort(key=lambda x: x["name"])
    return {"title": vids[0]["file_name"], "items": out_items}


def _wait_files(cookie: str, pdir_fid: str,
                names: set[str], tries: int = 8) -> dict:
    found: dict[str, str] = {}
    for _ in range(tries):
        j = _pc("GET", f"{BASE}/file/sort?pr=ucpro&fr=pc", cookie,
                params={"pdir_fid": pdir_fid, "_page": 1, "_size": 200,
                        "_fetch_total": 1, "_sort": "file_name:asc"})
        for it in (j.get("data") or {}).get("list") or []:
            if it.get("file_name") in names and not it.get("dir"):
                found[it["file_name"]] = it["fid"]
        if len(found) >= len(names):
            break
        time.sleep(0.8)
    return found


def _download_url(cookie: str, fid: str) -> str:
    try:
        j = _pc("POST", f"{BASE}/file/download?pr=ucpro&fr=pc", cookie,
                json={"fids": [fid]})
        arr = (j.get("data") or [])
        if arr and arr[0].get("download_url"):
            return arr[0]["download_url"]
    except Exception:
        return ""


def save_dir_fid(cookie: str) -> str:
    """转存目录 fid（不存在则返回根 '0'）。"""
    try:
        j = _pc("GET", f"{BASE}/file/sort?pr=ucpro&fr=pc", cookie,
                params={"pdir_fid": "0", "_page": 1, "_size": 200,
                        "_fetch_total": 1, "_sort": "file_name:asc"})
        for it in (j.get("data") or {}).get("list") or []:
            if it.get("dir") and it.get("file_name") == SAVE_DIR:
                return it["fid"]
    except Exception:
        pass
    return "0"


def list_dir(fid: str = "") -> dict:
    """列出自己网盘某目录（默认转存目录），供网盘页“我的转存”浏览/直接播放。"""
    cookie = _active_cookie()
    fid = fid or save_dir_fid(cookie)
    j = _pc("GET", f"{BASE}/file/sort?pr=ucpro&fr=pc", cookie,
            params={"pdir_fid": fid, "_page": 1, "_size": 200,
                    "_fetch_total": 1, "_sort": "file_type:asc,file_name:asc"})
    items = []
    for it in (j.get("data") or {}).get("list") or []:
        name = (it.get("file_name") or "")
        is_dir = bool(it.get("dir"))
        low = name.lower()
        playable = (not is_dir) and low.endswith(VIDEO_EXT)
        items.append({"name": name, "fid": it.get("fid"), "dir": is_dir,
                      "playable": playable, "size": int(it.get("size") or 0),
                      "play": (f"/v/api/ext/pan/play?fid={it.get('fid')}"
                               if playable else "")})
    items.sort(key=lambda x: (not x["dir"], x["name"]))
    return {"fid": fid, "title": ("转存目录 · " + SAVE_DIR), "items": items}


# ------------------------------------------------- 过期转存清理（P1-8）
# 上游 app.py 只给了调用点（_pan_cache_cleanup_once：每小时调
# mod.cleanup_expired(ttl_seconds)，ttl 默认 6h），执行实现上游公开仓
# 也没有，fork 自己写。转存只落云端 SAVE_DIR 目录、不落本地：fork 没有
# 本地转存台账，文件年龄以云端 file/sort 条目的 created_at 为准；
# 取不到可信时间戳的条目永不删除（宁可多占空间，不误删用户文件）。
# 只删 SAVE_DIR 内的过期文件，不碰目录、不碰 SAVE_DIR 以外的位置。
_CLEANUP_PAGE_SIZE = 200
_CLEANUP_MAX_PAGES = 50  # 最多扫 10000 条，防异常大目录拖垮清理
_CLEANUP_DELETE_BATCH = 50
_CLEANUP_TS_FIELDS = ("created_at", "updated_at", "file_created_at")


def _created_ts(item: dict, now: int | None = None) -> int | None:
    """取云端文件条目的创建时间（epoch 秒）。毫秒时间戳自动折算；
    数字串与 ISO 8601 也接受；取不到或不在合理范围返回 None。"""
    import datetime as _dt
    now = _now() if now is None else int(now)
    for key in _CLEANUP_TS_FIELDS:
        v = item.get(key)
        if v is None or v == "":
            continue
        ts = None
        if isinstance(v, (int, float)):
            ts = float(v)
        elif isinstance(v, str):
            s = v.strip()
            if s.replace(".", "", 1).isdigit():
                ts = float(s)
            else:
                try:
                    ts = _dt.datetime.fromisoformat(
                        s.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
        else:
            continue
        if ts > 1e12:  # 毫秒时间戳
            ts /= 1000.0
        if 946684800 <= ts <= now + 3600:  # 2000-01-01 至今 +1h 容差
            return int(ts)
    return None


def _list_save_dir_files(cookie: str) -> list[dict]:
    """分页列出 SAVE_DIR 内的全部条目（原始 file/sort 记录）。"""
    fid = save_dir_fid(cookie)
    out: list[dict] = []
    for page in range(1, _CLEANUP_MAX_PAGES + 1):
        j = _pc("GET", f"{BASE}/file/sort?pr=ucpro&fr=pc", cookie,
                params={"pdir_fid": fid, "_page": page,
                        "_size": _CLEANUP_PAGE_SIZE, "_fetch_total": 1,
                        "_sort": "file_name:asc"})
        lst = (j.get("data") or {}).get("list") or []
        out.extend(lst)
        if len(lst) < _CLEANUP_PAGE_SIZE:
            break
    return out


def _delete_fids(cookie: str, fids: list[str]) -> None:
    """批量删除云端文件（夸克网页端 file/delete，action_type=2 移入回收站）。
    失败抛 PanError，由调用方按批计数、不中断其余批次。"""
    _pc("POST", f"{BASE}/file/delete?pr=ucpro&fr=pc", cookie,
        json={"action_type": 2, "fid_list": list(fids), "exclude_fids": []})


def cleanup_expired(ttl_seconds: int = 6 * 3600,
                    now: int | None = None) -> dict:
    """删除 SAVE_DIR 内超过 TTL 的转存文件。

    返回 {deleted, freed(字节), kept, unknown(无可信时间戳而保留),
    errors}；未登录返回全 0；内部失败不抛异常（只记 error 字段），
    绝不影响正常播放。过期判定：年龄 > TTL（年龄恰好等于 TTL 保留）。
    ttl_seconds 须 > 0，否则 ValueError。
    """
    result = {"deleted": 0, "freed": 0, "kept": 0, "unknown": 0, "errors": 0}
    if ttl_seconds is None or int(ttl_seconds) <= 0:
        raise ValueError("ttl_seconds 须 > 0")
    if not logged_in():
        return result
    now = _now() if now is None else int(now)
    cutoff = now - int(ttl_seconds)
    try:
        cookie = _active_cookie()
        items = _list_save_dir_files(cookie)
    except Exception as exc:  # noqa: BLE001
        result["errors"] += 1
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    expired: list[dict] = []
    for it in items:
        if it.get("dir"):
            continue  # 目录永不删除
        ts = _created_ts(it, now)
        if ts is None:
            result["unknown"] += 1
            result["kept"] += 1
            continue
        if ts < cutoff:
            expired.append(it)
        else:
            result["kept"] += 1
    for i in range(0, len(expired), _CLEANUP_DELETE_BATCH):
        batch = expired[i:i + _CLEANUP_DELETE_BATCH]
        try:
            _delete_fids(cookie, [it.get("fid") for it in batch
                                  if it.get("fid")])
        except Exception:  # noqa: BLE001
            result["errors"] += len(batch)
            result["kept"] += len(batch)
            continue
        result["deleted"] += len(batch)
        result["freed"] += sum(int(it.get("size") or 0) for it in batch)
    return result


# v2/play 各档优先级：super=原画（与源文件等大），其后按清晰度递减。
_PLAY_RES_ORDER = ("super", "2k", "4k", "high", "normal", "low")


def _play_url(cookie: str, fid: str) -> tuple[str, str]:
    """经在线播放接口取可播地址，返回 (url, 档位)。

    走 /file/v2/play（等价夸克 App 客户端的播放路径）：它的 super 档与源文件
    等大、为原画，且没有 PC /file/download “单文件 >2GB 拒绝”的限制，因此大
    电影也能原画播放。返回的是 video-play/pds CDN 短时效签名地址，需带 Cookie。
    """
    j = _pc("POST", f"{BASE}/file/v2/play?pr=ucpro&fr=pc", cookie,
            json={"fid": fid})
    vlist = ((j.get("data") or {}).get("video_list")) or []
    by_res = {}
    for v in vlist:
        vi = v.get("video_info") or {}
        u = vi.get("url") or ""
        if u and vi.get("finish", True) and vi.get("success", True):
            by_res[str(v.get("resolution") or "").lower()] = u
    for res in _PLAY_RES_ORDER:
        if by_res.get(res):
            return by_res[res], res
    # 无已转码完成的档位：取任意一个带 url 的
    for v in vlist:
        u = (v.get("video_info") or {}).get("url")
        if u:
            return u, str(v.get("resolution") or "")
    return "", ""


def playable(fid: str) -> dict:
    """供播放代理：按用户网盘内真实 fid 实时换原画可播地址（短时效）。

    返回 {url, resolution, m3u8}。优先 v2/play 的 super 原画（无大小限制），
    回退 PC download 原画（小文件）。"""
    cookie = _active_cookie()
    url, res = "", ""
    try:
        url, res = _play_url(cookie, fid)
    except Exception:
        url, res = "", ""
    m3u8 = ".m3u8" in url.split("?", 1)[0].lower()
    if not url:
        url = _download_url(cookie, fid)
        m3u8 = False
    if not url:
        raise PanError("取播放地址失败（文件不存在、仍在转码或风控，可稍后重试）")
    return {"url": url, "resolution": res, "m3u8": m3u8}


def playable_url(fid: str) -> str:
    """兼容旧调用：只取 URL。"""
    return playable(fid)["url"]


def auth_cookie_or_raise() -> str:
    return _active_cookie()


_LINK_HOSTS = ("sidhub.cc", "seedog.cc", "seedhub.pro", "seedhub.cc")


def resolve_linkstart(pan_id: str) -> str:
    """把 Seedhub 的 pan_id 占位解析成真实夸克分享链接。

    跳转链：sidhub.cc/link_start?redirect_to=pan_id_<id> → 302 →
    seedog.cc/link_start/... → 最终 pan.quark.cn/s/<id>。
    seedog.cc 前置 Cloudflare，纯 HTTP 可能被拦（返回“Just a moment”）；
    此时返回空串，由上层回退普通加固流程，不报错。"""
    if not pan_id:
        return ""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Linux; Android 13; Pixel 5) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Mobile Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    try:
        with httpx.Client(trust_env=False, timeout=12,
                          follow_redirects=False, transport=_v4_transport(),
                          headers=headers) as c:
            nxt = f"https://sidhub.cc/link_start/?redirect_to=pan_id_{pan_id}"
            for _ in range(6):
                host = httpx.URL(nxt).host
                r = c.get(nxt, headers={"Referer": "https://sidhub.cc/"})
                loc = r.headers.get("location", "")
                if loc:
                    nxt = _join(nxt, loc)
                    if "pan.quark.cn/s/" in nxt:
                        return _clean_share(nxt)
                    continue
                # 无跳转：可能最终页里嵌了夸克链接
                m = re.search(r"https?://pan\.quark\.cn/s/[A-Za-z0-9?=&_%-]+",
                              r.text or "")
                if m:
                    return _clean_share(m.group(0))
                # 落到 Cloudflare 质询或未知页：放弃
                return ""
    except Exception:
        return ""
    return ""


def _join(base: str, loc: str) -> str:
    import urllib.parse
    return urllib.parse.urljoin(base, loc)


def _clean_share(u: str) -> str:
    m = re.search(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+", u)
    if not m:
        return u.split("#", 1)[0]
    out = m.group(0)
    pm = re.search(r"[?&]pwd=([A-Za-z0-9]+)", u)
    if pm:
        out += "?pwd=" + pm.group(1)
    return out
