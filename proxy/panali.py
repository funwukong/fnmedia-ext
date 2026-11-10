#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云盘（阿里云盘开放平台 OpenAPI）分享转存 + 原画播放。【私有：公开构建整体删除本文件】

与夸克/UC 走网页私有接口不同，阿里云盘的网页端接口需要复杂的 x-signature 设备签名，
私有方案随时失效。本模块改用阿里云盘【官方开放平台 OpenAPI】：
  - OAuth2 扫码授权 → access_token（约 2 小时）+ refresh_token（长效，可反复刷新）；
  - 分享：get_share_token（验提取码）→ get_share_file/list → 转存到自己盘；
  - 取链：get_video_play_info（原画/转码）或 get_file_download_url（普通下载直链）。
开放平台凭据（client_id/client_secret）需要用户在 https://open.alipan.com 免费创建
一个"应用"后填入（回调地址可填 https://127.0.0.1，不需要真正部署服务端）。凭据仅存
本机 NAS。也可在管理页直接粘贴已有的 refresh_token（alist 等工具可导出）。

接口依据：阿里云盘开放平台 https://www.alipan.com/announce/api-doc（openapi.alipan.com）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import httpx

# 复用内联 segno 渲染二维码
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
try:
    import segno  # type: ignore
except Exception:  # noqa: BLE001
    segno = None  # type: ignore

_API = "https://openapi.alipan.com"
_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "pan_ali.json")
# 转存目标目录名（在用户盘根下自建，避免污染）
_SAVE_DIR = "fnmedia转存"
_SCOPES = ["user:base", "file:all:read", "file:all:write"]


class PanError(RuntimeError):
    pass


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


def qr_png(text: str, scale: int = 9) -> bytes:
    if segno is None:
        raise PanError("二维码组件未随包提供")
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=4,
                                     dark="black", light="white")
    return buf.getvalue()


# --------------------------------------------------------------- 凭据
def _cred(st: dict | None = None) -> tuple[str, str]:
    st = st if st is not None else _load()
    cid = (st.get("client_id") or "").strip()
    csec = (st.get("client_secret") or "").strip()
    if not cid:
        raise PanError(
            "尚未配置阿里云盘开放平台凭据：请在网盘页填入 client_id/client_secret"
            "（open.alipan.com 免费创建应用），或直接粘贴 refresh_token")
    return cid, csec


def set_credentials(client_id: str, client_secret: str) -> None:
    st = _load()
    st["client_id"] = (client_id or "").strip()
    st["client_secret"] = (client_secret or "").strip()
    _save(st)


def logged_in() -> bool:
    st = _load()
    return bool(st.get("refresh_token") or st.get("access_token"))


def account_info_safe() -> dict:
    st = _load()
    return {"logged_in": logged_in(),
            "nickname": st.get("nickname", ""),
            "uid": st.get("user_id", ""),
            "saved_ts": st.get("saved_ts", 0),
            "has_cred": bool(st.get("client_id"))}


# --------------------------------------------------------------- HTTP
def _client() -> httpx.Client:
    # 证书校验开启：目标是阿里云盘开放平台官方域名
    # （openapi.alipan.com，证书正常），且请求带 client_secret 与 token。
    return httpx.Client(timeout=30, trust_env=False,
                        headers={"Content-Type": "application/json"})


def _post(cli: httpx.Client, path: str, body: dict,
          token: str = "", params: dict | None = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = cli.post(_API + path, json=body, headers=headers, params=params or {})
    try:
        j = r.json()
    except Exception:
        raise PanError(f"阿里云盘接口返回非 JSON（HTTP {r.status_code}）")
    if isinstance(j, dict) and j.get("code") and j["code"] not in (
            "0", "200", "Success", "AccessTokenInvalid"):
        msg = j.get("message") or j.get("code")
        raise PanError(f"阿里云盘：{msg}")
    return j


# --------------------------------------------------------------- token
def _token_post(cli: httpx.Client, data: dict) -> dict:
    # token 端点用 form，且带 Basic 认证
    st = _load()
    cid, csec = _cred(st)
    r = cli.post(_API + "/oauth/access_token",
                 data={**data, "client_id": cid, "client_secret": csec},
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        j = r.json()
    except Exception:
        raise PanError(f"换取 token 失败（HTTP {r.status_code}）")
    if j.get("error") or j.get("code") in ("InvalidClient",):
        raise PanError(j.get("error_description") or j.get("message")
                       or "授权失败，请检查 client_id/client_secret")
    return j


def _store_tokens(j: dict) -> None:
    if not j.get("access_token"):
        raise PanError("授权返回缺少 access_token")
    st = _load()
    st["access_token"] = j["access_token"]
    st["refresh_token"] = j.get("refresh_token", st.get("refresh_token", ""))
    st["expires_at"] = int(time.time()) + int(j.get("expires_in", 7200) or 7200) - 120
    st["saved_ts"] = int(time.time())
    _save(st)


def access_token(force: bool = False) -> str:
    """返回有效 access_token，过期用 refresh_token 自动续期。"""
    st = _load()
    at = st.get("access_token", "")
    if at and not force and time.time() < st.get("expires_at", 0):
        return at
    rt = st.get("refresh_token", "")
    if not rt:
        raise PanError("阿里云盘未登录（缺少 refresh_token）")
    with _client() as cli:
        j = _token_post(cli, {"grant_type": "refresh_token",
                              "refresh_token": rt})
    _store_tokens(j)
    return j["access_token"]


def auth_cookie_or_raise() -> str:
    """与夸克接口同名；阿里 CDN 直链自带签名，无需鉴权头，返回空串。"""
    access_token()  # 触发一次登录态校验/续期
    return ""


def set_cookie(raw: str) -> dict:
    """兼容网盘页"粘贴凭据"：这里接受 refresh_token（或含 refresh_token 的 JSON）。"""
    raw = (raw or "").strip()
    rt = ""
    if raw.startswith("{"):
        try:
            j = json.loads(raw)
            rt = j.get("refresh_token", "")
            if j.get("client_id"):
                set_credentials(j.get("client_id", ""),
                                j.get("client_secret", ""))
        except Exception:
            pass
    if not rt:
        rt = raw
    if not rt:
        raise PanError("无法识别，请粘贴 refresh_token")
    st = _load()
    st["refresh_token"] = rt
    st["saved_ts"] = int(time.time())
    _save(st)
    _store_tokens(_refresh_with(rt))  # 立即换取并校验
    return {"ok": True}


def _refresh_with(rt: str) -> dict:
    with _client() as cli:
        return _token_post(cli, {"grant_type": "refresh_token",
                                 "refresh_token": rt})


def logout() -> None:
    st = _load()
    for k in ("access_token", "refresh_token", "expires_at",
              "nickname", "user_id", "saved_ts"):
        st.pop(k, None)
    _save(st)


# --------------------------------------------------------------- 扫码授权
def qr_start() -> dict:
    """申请扫码登录二维码。返回二维码内容（qrContent/扫码链接）与会话 sid。"""
    st = _load()
    cid, _ = _cred(st)
    with _client() as cli:
        j = _post(cli, "/oauth/authorize/qrcode", {
            "client_id": cid,
            "client_secret": _cred(st)[1],
            "scopes": _SCOPES,
            "width": 300, "height": 300,
            "redirectUri": "https://127.0.0.1"})
    sid = j.get("sid") or j.get("qrCodeSessionId")
    qr_text = j.get("qrCodeUrl") or j.get("qrContent") or ""
    if not sid or not qr_text:
        raise PanError(f"获取二维码失败：{json.dumps(j, ensure_ascii=False)[:200]}")
    return {"sid": sid, "qr_text": qr_text}


def qr_poll(session: dict) -> dict:
    """轮询扫码结果：waiting / scanned / success。success 时换 token 并落盘。"""
    st = _load()
    sid = session.get("sid", "")
    if not sid:
        return {"status": "error", "msg": "会话已失效，请重新生成二维码"}
    with _client() as cli:
        j = _post(cli, "/oauth/qrcode/" + sid + "/status", {},
                  params={"client_id": _cred(st)[0]})
    state = (j.get("status") or "").lower()
    # 阿里返回 authCode/code 表示已确认授权
    done = state in ("loginsuccess", "success", "confirmed")
    code = (j.get("authCode") or j.get("code")) if done else None
    if code:
        with _client() as cli2:
            tj = _token_post(cli2, {"grant_type": "authorization_code",
                                    "code": code})
        _store_tokens(tj)
        try:
            _fill_profile()
        except Exception:
            pass
        info = account_info_safe()
        return {"status": "success", "nickname": info.get("nickname", "")}
    if state in ("new", "waiting", "scanned", ""):
        return {"status": "waiting", "raw": state}
    if state in ("expired", "qrscanpagestatusexpired", "canceled", "cancelled"):
        return {"status": "expired"}
    return {"status": "waiting", "raw": state}


def _fill_profile() -> None:
    at = access_token()
    with _client() as cli:
        j = _post(cli, "/adrive/v1.0/user/getDriveInfo", {}, token=at)
    st = _load()
    st["user_id"] = j.get("user_id", "")
    st["nickname"] = j.get("name") or j.get("nickname") or ""
    _save(st)


# --------------------------------------------------------------- 分享解析
_SHARE_RE_PATHS = ("/s/", "/share/")


def parse_share(url: str) -> tuple[str, str]:
    """从分享链接取 share_id 与提取码（?pwd= 或路径末段）。"""
    import re
    import urllib.parse as up
    m = re.search(r"https?://[^\s\"']*(?:alipan|aliyundrive)\.com/s/([A-Za-z0-9]+)",
                  url)
    if not m:
        raise PanError("无法识别阿里云盘分享链接")
    share_id = m.group(1)
    q = up.parse_qs(up.urlparse(url).query)
    pwd = (q.get("pwd") or q.get("code") or [""])[0]
    return share_id, pwd


def _api_token(cli: httpx.Client, path: str, body: dict) -> dict:
    return _post(cli, path, body, token=access_token())


def _api_token_retry(cli: httpx.Client, path: str, body: dict) -> dict:
    try:
        return _post(cli, path, body, token=access_token())
    except PanError as e:
        if "AccessTokenInvalid" in str(e) or "token" in str(e).lower():
            return _post(cli, path, body, token=access_token(force=True))
        raise


def resolve_share(url: str, passcode_hint: str = "") -> dict:
    """转存分享里的视频，返回 {title, items:[{name,size,fid,url}]}。

    url 为本服务播放代理 /v/api/ext/pan/ali/play?fid=…，原画/转码实时换取。"""
    share_id, pwd = parse_share(url)
    if passcode_hint and not pwd:
        pwd = passcode_hint
    with _client() as cli:
        # 1) 分享 token（带提取码）
        st_body = {"share_id": share_id}
        if pwd:
            st_body["share_pwd"] = pwd
        stok = _api_token_retry(cli, "/adrive/v2.0/share_link/get_share_by_anonymous",
                                st_body)
        share_token = stok.get("share_token")
        if not share_token:
            # 兼容 v1
            stok = _api_token(cli, "/adrive/v1.0/share_link/get_share_by_anonymous",
                              st_body)
            share_token = stok.get("share_token")
        if not share_token:
            raise PanError("无法获取分享 token（链接失效或需要提取码）")

        def slist(parent="root"):
            return _share_list(cli, share_id, share_token, parent)

        vids, folder_title = _collect_videos(cli, share_id, share_token)
        if not vids:
            raise PanError("该阿里云盘分享里没有可播放的视频文件")

        # 2) 确保目标目录
        to_fid = _ensure_save_dir(cli)
        # 3) 转存
        fid_list = [{"file_id": v["file_id"]} for v in vids[:100]]
        sv = _api_token_retry(cli, "/adrive/v1.0/share_link/save", {
            "share_id": share_id,
            "share_token": share_token,
            "file_id_list": fid_list,
            "to_parent_file_id": to_fid,
            "policy": "auto_rename",
        })
        saved = sv.get("file_save_list") or []
        # 4) 按名称找回真实 fid
        name2fid = {x.get("name"): x.get("file_id") for x in saved if x.get("file_id")}
        items = []
        for v in vids[:100]:
            fid = name2fid.get(v["name"])
            if not fid:
                continue
            items.append({"name": v["name"], "size": int(v.get("size") or 0),
                          "fid": fid,
                          "url": f"/v/api/ext/pan/ali/play?fid={fid}"})
        if not items:
            raise PanError("阿里云盘转存后未能取回文件")
        items.sort(key=lambda x: x["name"])
        title = folder_title or items[0]["name"]
        return {"title": title, "items": items}


def _share_list(cli: httpx.Client, share_id: str, share_token: str,
                parent: str) -> dict:
    # 分享匿名接口：header 带 x-share-token
    r = cli.post(
        _API + "/adrive/v1.0/share_link/list",
        json={"share_id": share_id, "parent_file_id": parent,
              "limit": 200, "order_by": "name", "order_direction": "ASC"},
        headers={"Content-Type": "application/json",
                 "x-share-token": share_token})
    j = r.json()
    if j.get("code") and j["code"] not in ("0", "200"):
        raise PanError(j.get("message") or "列分享文件失败")
    return j


_VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".flv",
              ".wmv", ".rmvb", ".rm", ".mpg", ".mpeg", ".m4v", ".webm", ".3gp")


def _collect_videos(cli: httpx.Client, share_id: str, share_token: str,
                    parent="root", depth=0):
    """递归收集分享内视频（最多 3 层）。返回 (视频列表, 首个目录/文件名)。"""
    vids: list[dict] = []
    title = ""
    if depth > 3:
        return vids, title
    marker = ""
    while True:
        body = {"share_id": share_id, "parent_file_id": parent,
                "limit": 200, "order_by": "name", "order_direction": "ASC"}
        if marker:
            body["marker"] = marker
        r = cli.post(_API + "/adrive/v1.0/share_link/list", json=body,
                     headers={"Content-Type": "application/json",
                              "x-share-token": share_token})
        j = r.json()
        if j.get("code") and j["code"] not in ("0", "200"):
            raise PanError(j.get("message") or "列分享文件失败")
        entries = j.get("file_list") or j.get("records") or []
        for e in entries:
            nm = e.get("name", "")
            if not title and e.get("type") == "folder":
                title = nm
            if e.get("type") == "folder":
                if depth < 3:
                    sub, _ = _collect_videos(cli, share_id, share_token,
                                             e.get("file_id"), depth + 1)
                    vids += sub
            elif nm.lower().endswith(_VIDEO_EXT) and e.get("category") in \
                    ("video", None, ""):
                vids.append(e)
        marker = j.get("next_marker") or ""
        if not marker:
            break
    if not title and vids:
        title = vids[0]["name"]
    return vids, title


def _ensure_save_dir(cli: httpx.Client) -> str:
    """在盘根下确保存在转存目录，返回其 file_id。"""
    j = _api_token_retry(cli, "/adrive/v1.0/user/getDriveInfo", {})
    drive = j.get("resource_drive_id") or ""
    lj = _api_token_retry(cli, "/adrive/v1.0/file/list", {
        "drive_id": drive, "parent_file_id": "root", "limit": 100,
        "marker": "", "order_by": "name", "order_direction": "ASC"})
    for f in lj.get("items") or []:
        if f.get("type") == "folder" and f.get("name") == _SAVE_DIR:
            return f["file_id"]
    cj = _api_token_retry(cli, "/adrive/v1.0/file/create", {
        "drive_id": drive, "parent_file_id": "root",
        "name": _SAVE_DIR, "check_name_mode": "refuse", "type": "folder"})
    fid = cj.get("file_id")
    if not fid:
        raise PanError("创建转存目录失败")
    return fid


# --------------------------------------------------------------- 取播放直链
def playable(fid: str) -> dict:
    """实时取播放地址。优先原画下载直链（mp4 等），其次视频转码 m3u8。"""
    with _client() as cli:
        info = _api_token_retry(cli, "/adrive/v1.0/user/getDriveInfo", {})
        drive = info.get("resource_drive_id") or ""
        try:
            v = _api_token_retry(cli, "/adrive/v1.0/file/get_video_play_info", {
                "drive_id": drive, "file_id": fid,
                "category": "live_transcoding",
                "template_id": "",
                "url_expire_sec": 7200})
            # 原画
            url = v.get("web_content_url") or ""
            if not url and (v.get("video_media") or {}).get("media_list"):
                url = (v["video_media"]["media_list"][0] or {}).get("url", "")
            meta = v.get("video_media") or {}
            # 转码播放列表
            play = (v.get("live_transcoding_task_list")
                    or meta.get("live_transcoding_task_list") or [])
            m3u8 = next((t.get("url") for t in play
                         if t.get("status") == "finished" and t.get("url")), "")
            if m3u8:
                return {"url": m3u8, "m3u8": True}
            if url:
                return {"url": url, "m3u8": False}
        except PanError:
            pass
        # 回退：普通下载直链（302，客户端跟随）
        d = _api_token_retry(cli, "/adrive/v1.0/file/get_download_url", {
            "drive_id": drive, "file_id": fid, "expire_sec": 7200})
        url = d.get("url") or ""
        if not url:
            # streams 形式
            url = (d.get("streams") or {}).get("url", "")
        if not url:
            raise PanError("无法获取阿里云盘播放地址")
        return {"url": url, "m3u8": False}


def list_dir(fid: str) -> list[dict]:
    """网盘页"我的文件"浏览（可选）。"""
    with _client() as cli:
        info = _api_token_retry(cli, "/adrive/v1.0/user/getDriveInfo", {})
        drive = info.get("resource_drive_id") or ""
        j = _api_token_retry(cli, "/adrive/v1.0/file/list", {
            "drive_id": drive, "parent_file_id": fid or "root",
            "limit": 100, "order_by": "name", "order_direction": "ASC"})
        out = []
        for f in j.get("items") or []:
            out.append({"name": f.get("name"), "fid": f.get("file_id"),
                        "type": f.get("type"),
                        "size": int(f.get("size") or 0)})
        return out
