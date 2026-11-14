#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PanSou 手动线状态机（fork 自研，上游公开仓只有存根契约）。

背景（见 ~/workspace/upstream-diff/DIFF.md §4）：上游公开仓的 PanSou
真实实现在私有 GUARD 块里，不在 git 里；公开仓只给了存根契约——常量
``PANSOU_LINE`` / ``PANSOU_LABEL`` 与 11 个同名函数/方法的签名与语义。
本模块按该契约实现真正的（但精简的）状态机，接在 fork 现有的播放链路
（``app._line_choices`` / ``hls_preset`` / ``hls_main`` /
``placeholder_ts`` / ``stream_post``）上，不另起一套网盘播放链路。

状态（每部片按 media_guid ``mg`` 一条记录）：

- ``""`` —— 从未 arm，无记录；
- ``"need_login"`` —— 需要的网盘家尚未登录，播放画面出扫码二维码
  （``placeholder_ts`` 经 ``panqr`` 烧进 TS 分片）；
- ``"armed"`` —— 已登记片名/集序，待触发；
- ``"pending"`` —— 搜索/转存进行中；
- ``"ready"`` —— 已转存出直链，快照在 ``_PANSOU_MAN_SNAP[mg]``；
- ``"dead"`` —— 本轮无可用分享（无候选或全部转存失败），可重试。

搜索一步（2026-11-12 接通，用户提供的自托管实例）：
``_pansou_search`` 向 ``https://bus.wulii.eu.cc``（PanSou 盘搜，
fish2018/pansou 实例，auth_enabled=false，无需认证）``POST /api/search``，
JSON ``{"kw": title, "cloud_types": ["quark","uc","aliyun"],
"res": "merge", "src": "all"}``，取 merged_by_type 里三盘的分享链接
（服务端键名是 aliyun 不是 ali）；每盘前 6 条、总数上限 12 条；
失败（超时 60 秒/HTTP 错误/非 JSON/业务码非 0）一律返回 [] 不抛错。
fork 不新增配置项，地址作为默认值硬编码（用户明确给的）。
手动贴分享链接仍可用：管理端 ``POST /v/api/ext/pansou/share``（或直接调
``_pansou_set_manual_share``）贴夸克/UC/阿里分享链接，``kick`` 后
经 fork 现有 ``pan`` / ``panuc`` / ``panali`` 的 ``resolve_share``
转存，成功即 ``ready``。

登录只接 fork 现有官方扫码/令牌流程（各 ``pan*.py`` 的 ``qr_start`` /
``qr_poll`` / ``logged_in``），不发明登录接口。公开构建已剥离
``pan*.py`` 时本模块优雅降级：``_pansou_available()`` 为 False，
状态机不再 arm，播放链路按无网盘线走（与上游 lite 存根行为一致）。
"""
from __future__ import annotations

import asyncio
import importlib
import time
import urllib.parse

import httpx

from fnlog import get_logger
from panmatch import _pan_match

_log = get_logger(__name__)

PANSOU_LINE = "builtin-pansou::PanSou网盘高清"
PANSOU_LABEL = "🎞️网盘高清（PanSou）"

# 自托管 PanSou 搜索实例：地址/云类型/超时/条数全部走配置
# （上游 pansou.py 同款配置键）。默认地址仅作兜底，实例不可达时用户可在
# 管理页「网盘登录」里换成自己的实例。
_DEFAULT_PANSOU_URL = "https://bus.wulii.eu.cc"


def _pansou_cfg(key: str, default):
    try:
        import config as _cfg
        v = _cfg.config.get().get(key, default)
    except Exception:
        v = default
    return v


def _pansou_base_url() -> str:
    u = str(_pansou_cfg("pansou_url", _DEFAULT_PANSOU_URL) or "").strip()
    return u.rstrip("/")


def _pansou_timeout() -> float:
    try:
        return max(3.0, float(_pansou_cfg("pansou_timeout", 30.0)))
    except (TypeError, ValueError):
        return 30.0


def _pansou_limit() -> int:
    try:
        return max(1, min(80, int(_pansou_cfg("pansou_limit", 30))))
    except (TypeError, ValueError):
        return 30


def _pansou_cloud_types() -> list[str]:
    raw = str(_pansou_cfg("pansou_cloud_types", "quark,uc,aliyun") or "").strip()
    if not raw:
        return ["quark", "uc", "aliyun"]
    out: list[str] = []
    for t in raw.replace("，", ",").split(","):
        t = t.strip().lower()
        if t in ("quark", "uc", "aliyun") and t not in out:
            out.append(t)
    return out or ["quark", "uc", "aliyun"]


def _pansou_src() -> str:
    s = str(_pansou_cfg("pansou_src", "all") or "all").strip().lower()
    return s if s in ("all", "tg", "plugin") else "all"


_PANSOU_SEARCH_PER_TYPE_MAX = 6  # 每盘最多取几条
_PANSOU_SEARCH_TOTAL_MAX = 12   # 搜索结果总数上限
# merged_by_type 的遍历顺序：键是服务端类型名，aliyun 不是 ali。
_PANSOU_SEARCH_ORDER = ("quark", "uc", "aliyun")
# 服务端类型名 -> fork 驱动 key（aliyun 桶的链接由 panali.py 转存）。
_PANSOU_SERVER_KEY = {"quark": "quark", "uc": "uc", "aliyun": "ali"}

# 状态取值（与上游存根契约一致）。
_PANSOU_STATES = ("", "need_login", "armed", "pending", "ready", "dead")

# 网盘家 -> （实现模块名，播放画面二维码标题）。顺序即登录提示/转存优先级。
_PAN_MODULES: dict[str, tuple[str, str]] = {
    "quark": ("pan", "夸克网盘"),
    "uc": ("panuc", "UC网盘"),
    "ali": ("panali", "阿里云盘"),
}
_PAN_ORDER = ("quark", "uc", "ali")

# 小阈值硬编码（本批不新增配置项，见任务约束）。
_QR_TTL = 120.0            # 二维码 PNG 复用时长（秒）
_QR_POLL_INTERVAL = 2.0    # 扫码轮询间隔（秒）
_QR_POLL_BUDGET = 150.0    # 单次扫码等待总预算（秒）
_MANUAL_SHARE_MAX = 8      # 每部片暂存的手动分享链接上限

# mg -> {"title","index","state","provider","share_url","error",
#        "manual_shares":[url,...],"updated":ts}
_PANSOU_STATE: dict[str, dict] = {}
# mg -> ready 后可直接播放的 target（与上游 _PANSOU_MAN_SNAP 同名同义）。
_PANSOU_MAN_SNAP: dict[str, dict] = {}
# mg -> True：用户亲手切过 PanSou 线（preset?line=PanSou / play/play
# bitrate / 线路墙卡）。无此意图时 PanSou 永不作默认线、不自动触发。
_PANSOU_USER_PICK: dict[str, bool] = {}
# provider key -> {"session","png","ts"}：进行中的扫码会话（按网盘家共享）。
_PANSOU_QR: dict[str, dict] = {}
# mg -> 在途 _pansou_run 任务（单飞）。
_PANSOU_TASKS: dict[str, "asyncio.Task"] = {}


def _pan_module(key: str):
    """取网盘驱动模块；公开构建已剥离时返回 None（不抛错）。"""
    spec = _PAN_MODULES.get(key)
    if spec is None:
        return None
    try:
        return importlib.import_module(spec[0])
    except Exception:
        _log.debug("_pan_module failed: key=%s", key, exc_info=True)
        return None


def _pan_logged_in(key: str) -> bool:
    mod = _pan_module(key)
    if mod is None:
        return False
    try:
        return bool(mod.logged_in())
    except Exception:
        _log.debug("_pan_logged_in failed: key=%s", key, exc_info=True)
        return False


def _first_not_logged_in() -> str | None:
    for key in _PAN_ORDER:
        if _pan_module(key) is not None and not _pan_logged_in(key):
            return key
    return None


def _any_pan_logged_in() -> bool:
    """任一已接入的网盘家已登录（转存至少有一家可用即 armed）。"""
    for key in _PAN_ORDER:
        if _pan_module(key) is not None and _pan_logged_in(key):
            return True
    return False


def _is_pansou_line_name(line: str) -> bool:
    try:
        return bool(line) and str(line).startswith("builtin-pansou::")
    except Exception:
        _log.debug("_is_pansou_line_name failed", exc_info=True)
        return False


def _pansou_available() -> bool:
    """手动线是否可提供：任一网盘驱动模块在即可（公开构建剥离后为 False）。"""
    for key in _PAN_ORDER:
        if _pan_module(key) is not None:
            return True
    return False


def _pansou_manual_target(index: int = 0) -> dict:
    """构造一条 PanSou 手动线 target（不在 store、不进搜索、不参与测速）。"""
    try:
        idx = int(index or 0)
    except (TypeError, ValueError):
        idx = 0
    return {"line": PANSOU_LINE, "index": idx, "_pansou_manual": True,
            "url": "", "m3u8": False, "resolve": False, "redirect": False,
            "headers": {}}


def _pansou_manual_state(mg: str) -> str:
    rec = _PANSOU_STATE.get(mg or "")
    if not rec:
        return ""
    return rec.get("state") or ""


def _pansou_manual_arm(mg: str, title: str, index: int = 0) -> None:
    """登记片名/集序，进入待命。已 ready 且同片同集时保持 ready 不降级；
    进行中（pending）时只更新片名/集序，不打断在途转存。

    未登录时**不立刻判 need_login**：网盘线是兜底线，选中它不代表用户想扫码。
    这里一律进 armed，等真正要转存时（_pansou_run）再判断登录态——那时才需要
    提示扫码。这样平时选网盘线不会立刻弹二维码；只有该线真要出流、而网盘
    尚未登录时才提示，且提示发生在播放画面里而不是选线瞬间。
    """
    if not mg:
        return
    try:
        idx = int(index or 0)
    except (TypeError, ValueError):
        idx = 0
    title = (title or "").strip()
    rec = _PANSOU_STATE.get(mg)
    if rec is not None and rec.get("state") == "ready" \
            and rec.get("title") == title and int(rec.get("index") or 0) == idx \
            and mg in _PANSOU_MAN_SNAP:
        return
    if rec is not None and rec.get("state") == "pending":
        rec["title"] = title
        rec["index"] = idx
        rec["updated"] = time.time()
        return
    if rec is None:
        rec = {"title": title, "index": idx, "state": "armed",
               "provider": "", "share_url": "", "error": "",
               "manual_shares": [], "updated": time.time()}
        _PANSOU_STATE[mg] = rec
    else:
        rec.update({"title": title, "index": idx, "updated": time.time()})
        _PANSOU_MAN_SNAP.pop(mg, None)
    if not _pansou_available():
        rec["state"] = "dead"
        rec["error"] = "当前构建不含网盘驱动模块"
        return
    # 不在这里判登录态：armed 即可，真正的登录检查在 _pansou_run 转存前。
    rec["state"] = "armed"
    rec["provider"] = ""
    rec["error"] = ""


def _pansou_manual_kick(mg: str) -> None:
    """触发一次后台搜索/转存（单飞）。无记录、ready 且快照在时不做事；
    need_login 时先走扫码等待，登录成功后自动继续转存。"""
    rec = _PANSOU_STATE.get(mg or "")
    if not rec:
        return
    if rec.get("state") == "ready" and mg in _PANSOU_MAN_SNAP:
        return
    if rec.get("state") not in ("need_login", "armed", "pending", "dead"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _PANSOU_TASKS.get(mg)
    if task is not None and not task.done():
        return
    task = loop.create_task(_pansou_run(mg))

    def _done(t: "asyncio.Task", _mg: str = mg) -> None:
        if _PANSOU_TASKS.get(_mg) is t:
            _PANSOU_TASKS.pop(_mg, None)

    task.add_done_callback(_done)
    _PANSOU_TASKS[mg] = task


def _pansou_mark_user_pick(mg: str) -> None:
    if mg:
        _PANSOU_USER_PICK[mg] = True


def _pansou_is_user_pick(mg: str) -> bool:
    return bool(_PANSOU_USER_PICK.get(mg or ""))


def _pansou_url_has_pwd(url: str) -> bool:
    """分享链接是否已带提取码参数（pwd/code/passcode 任一）。"""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:
        _log.debug("_pansou_url_has_pwd failed", exc_info=True)
        return False
    return any(k.lower() in ("pwd", "code", "passcode") for k in qs)


def _pansou_with_password(url: str, password: str) -> str:
    """提取码拼进分享链接查询串；已带提取码时原样返回，不重复拼。"""
    pw = (password or "").strip()
    if not pw or _pansou_url_has_pwd(url):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}pwd={urllib.parse.quote(pw, safe='')}"


def _pansou_dedupe_key(url: str) -> str:
    """去重键：归一化 URL（去尾部 /，比较 ?pwd= 之前的部分）。"""
    base = url.split("?pwd=")[0].split("&pwd=")[0]
    return base.rstrip("/")


def _pansou_search(title: str, index: int = 0, _transport=None) -> list[str]:
    """PanSou 自托管搜索适配器（2026-11-12 接通）。

    向配置的自托管实例 POST /api/search，取配置网盘类型
    三盘的分享链接字符串列表（``_pansou_run`` 按序逐条转存）。
    失败（超时/HTTP 错误/非 JSON/业务码非 0）一律返回 []，绝不抛错；
    不做标题精确过滤（note 可能是合集标题），转存按序试、首条可播
    即停。``_transport`` 仅供测试注入 httpx.MockTransport。
    """
    title = (title or "").strip()
    if not title:
        return []
    try:
        with httpx.Client(timeout=_pansou_timeout(),
                          transport=_transport) as client:
            resp = client.post(
                _pansou_base_url() + "/api/search",
                json={"kw": title,
                      "cloud_types": _pansou_cloud_types(),
                      "res": "merge", "src": _pansou_src()})
    except Exception:
        _log.debug("_pansou_search request failed: title=%s", title,
                   exc_info=True)
        return []
    if resp.status_code >= 400:
        _log.debug("_pansou_search http error: title=%s status=%s",
                   title, resp.status_code)
        return []
    try:
        payload = resp.json()
    except Exception:
        _log.debug("_pansou_search non-json response: title=%s", title,
                   exc_info=True)
        return []
    if not isinstance(payload, dict) or payload.get("code", 0) != 0:
        _log.debug("_pansou_search bad code: title=%s code=%s", title,
                   payload.get("code") if isinstance(payload, dict) else None)
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    merged = data.get("merged_by_type")
    if not isinstance(merged, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for server_key in _pansou_cloud_types():
        items = merged.get(server_key) or []
        if not isinstance(items, list):
            continue
        taken = 0
        for item in items:
            if len(out) >= _PANSOU_SEARCH_TOTAL_MAX:
                break
            if taken >= _PANSOU_SEARCH_PER_TYPE_MAX:
                break
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").strip()
            if not url or url.lower().startswith("magnet:"):
                continue
            pv = _pan_match(url)
            fork_key = _PANSOU_SERVER_KEY.get(server_key)
            if pv is None or pv.get("key") not in _PAN_MODULES \
                    or pv.get("key") != fork_key:
                continue
            key = _pansou_dedupe_key(url)
            if key in seen:
                continue
            seen.add(key)
            out.append(_pansou_with_password(
                url, (item.get("password") or "").strip()))
            taken += 1
        if len(out) >= _PANSOU_SEARCH_TOTAL_MAX:
            break
    _log.debug("_pansou_search ok: title=%s total=%s", title, len(out))
    return out


def _pansou_search_rows(title: str, _transport=None) -> list[tuple[str, str, str]]:
    """同 _pansou_search，但保留 (云类型, url, 备注)。

    供 pansou_search.search() 把网盘结果映射成真实的一集（上游行为）：
    上游用 merged_by_type 的键作云类型、用 note 作标题；这里保持一致。
    """
    title = (title or "").strip()
    if not title:
        return []
    try:
        with httpx.Client(timeout=_pansou_timeout(),
                          transport=_transport) as client:
            resp = client.post(
                _pansou_base_url() + "/api/search",
                json={"kw": title,
                      "cloud_types": _pansou_cloud_types(),
                      "res": "merge", "src": _pansou_src()})
    except Exception:
        _log.debug("_pansou_search_rows request failed", exc_info=True)
        return []
    if resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    if not isinstance(payload, dict) or payload.get("code", 0) != 0:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    merged = data.get("merged_by_type")
    if not isinstance(merged, dict):
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for server_key in _pansou_cloud_types():
        items = merged.get(server_key) or []
        if not isinstance(items, list):
            continue
        taken = 0
        for item in items:
            if len(out) >= _PANSOU_SEARCH_TOTAL_MAX:
                break
            if taken >= _PANSOU_SEARCH_PER_TYPE_MAX:
                break
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").strip()
            if not url or url.lower().startswith("magnet:"):
                continue
            pv = _pan_match(url)
            fork_key = _PANSOU_SERVER_KEY.get(server_key)
            if pv is None or pv.get("key") not in _PAN_MODULES \
                    or pv.get("key") != fork_key:
                continue
            key = _pansou_dedupe_key(url)
            if key in seen:
                continue
            seen.add(key)
            note = (item.get("note") or "").strip()
            out.append((server_key,
                        _pansou_with_password(url, (item.get("password") or "").strip()),
                        note))
            taken += 1
        if len(out) >= _PANSOU_SEARCH_TOTAL_MAX:
            break
    return out


def _pansou_set_manual_share(mg: str, url: str, index: int = 0) -> dict:
    """手动贴一条分享链接（搜索未接通时的可用核心）。

    校验：必须是夸克/UC/阿里三家可转存的分享链接（``_pan_match`` 识别）。
    成功后登记进该 mg 的候选列表（去重，最多 8 条），并按登录情况置
    ``armed`` / ``need_login``；调用方再 ``_pansou_manual_kick`` 触发转存。
    """
    if not mg or not url:
        raise ValueError("缺少 mg 或 url")
    pv = _pan_match(url)
    if pv is None or pv.get("key") not in _PAN_MODULES:
        raise ValueError("不是可转存的网盘分享链接（仅支持夸克/UC/阿里）")
    try:
        idx = int(index or 0)
    except (TypeError, ValueError):
        idx = 0
    rec = _PANSOU_STATE.get(mg)
    if rec is None:
        rec = {"title": "", "index": idx, "state": "armed",
               "provider": "", "share_url": "", "error": "",
               "manual_shares": [], "updated": time.time()}
        _PANSOU_STATE[mg] = rec
    shares = rec.setdefault("manual_shares", [])
    if url not in shares:
        shares.append(url)
        del shares[:max(0, len(shares) - _MANUAL_SHARE_MAX)]
    rec["index"] = idx
    rec["updated"] = time.time()
    if rec.get("state") in ("", "armed", "dead"):
        # 按当前登录态重算待命/需登录（ready/pending 不降级）。
        if not _pansou_available():
            rec["state"] = "dead"
            rec["error"] = "当前构建不含网盘驱动模块"
        elif _any_pan_logged_in():
            rec["state"] = "armed"
            rec["provider"] = ""
        else:
            need = _first_not_logged_in() or _PAN_ORDER[0]
            rec["state"] = "need_login"
            rec["provider"] = need
    return _pansou_status(mg)


def _pansou_status(mg: str) -> dict:
    rec = _PANSOU_STATE.get(mg or "") or {}
    return {"mg": mg or "", "state": rec.get("state") or "",
            "title": rec.get("title") or "",
            "index": int(rec.get("index") or 0),
            "provider": rec.get("provider") or "",
            "error": rec.get("error") or "",
            "user_pick": _pansou_is_user_pick(mg),
            "has_snap": bool(mg and mg in _PANSOU_MAN_SNAP),
            "manual_shares": list(rec.get("manual_shares") or [])}


def _pansou_reset(mg: str) -> None:
    """清掉一部片的 PanSou 状态（测试/重试用）。"""
    if not mg:
        return
    _PANSOU_STATE.pop(mg, None)
    _PANSOU_MAN_SNAP.pop(mg, None)
    _PANSOU_USER_PICK.pop(mg, None)


async def _pansou_login_qr_png(mg: str):
    """取当前需登录网盘家的扫码二维码 PNG。

    返回 ``(png_bytes, 网盘名)``；全部已登录或取不到时返回 None（调用方
    回退普通占位分片）。同一家的二维码在 120 秒内复用，不重复申请。
    只调各 ``pan*.py`` 的 ``qr_start`` / ``qr_png``，不发明登录接口。
    """
    rec = _PANSOU_STATE.get(mg or "") or {}
    provider = rec.get("provider") or ""
    if provider not in _PAN_MODULES or _pan_logged_in(provider):
        provider = _first_not_logged_in()
    if provider is None:
        return None
    mod = _pan_module(provider)
    if mod is None:
        return None
    name = _PAN_MODULES[provider][1]
    now = time.time()
    ent = _PANSOU_QR.get(provider)
    if ent is not None and ent.get("png") and now - float(ent.get("ts") or 0) < _QR_TTL:
        if rec:
            rec["provider"] = provider
        return ent["png"], name
    try:
        session = await asyncio.to_thread(mod.qr_start)
        qr_text = (session or {}).get("qr_text") or ""
        if not qr_text:
            return None
        png = await asyncio.to_thread(mod.qr_png, qr_text)
    except Exception:
        _log.debug("_pansou_login_qr_png failed: provider=%s", provider,
                   exc_info=True)
        return None
    if not png:
        return None
    _PANSOU_QR[provider] = {"session": session, "png": png, "ts": now}
    if rec:
        rec["provider"] = provider
    return png, name


async def _pansou_wait_login(mg: str, provider: str) -> bool:
    """轮询扫码登录直至成功/超时。成功返回 True；过期自动重建二维码继续等。"""
    mod = _pan_module(provider)
    if mod is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    deadline = loop.time() + _QR_POLL_BUDGET
    while loop.time() < deadline:
        if _pan_logged_in(provider):
            return True
        ent = _PANSOU_QR.get(provider)
        if ent is None or not ent.get("session"):
            await _pansou_login_qr_png(mg)
            ent = _PANSOU_QR.get(provider)
            if ent is None:
                await asyncio.sleep(_QR_POLL_INTERVAL)
                continue
        try:
            res = await asyncio.to_thread(mod.qr_poll, ent["session"])
        except Exception:
            _log.debug("_pansou_wait_login poll failed: provider=%s",
                       provider, exc_info=True)
            await asyncio.sleep(_QR_POLL_INTERVAL)
            continue
        status = (res or {}).get("status") or ""
        if status == "success":
            return _pan_logged_in(provider)
        if status == "expired":
            _PANSOU_QR.pop(provider, None)
            continue
        await asyncio.sleep(_QR_POLL_INTERVAL)
    return _pan_logged_in(provider)


async def _pansou_run(mg: str) -> None:
    """后台搜索 → 选分享 → resolve_share 转存 → 出直链快照。"""
    rec = _PANSOU_STATE.get(mg or "")
    if not rec:
        return
    try:
        # 登录检查放在【真正要转存前】（下面循环里），不在这里提前判：
        # 选网盘线只是兜底意图，不代表用户要立刻扫码。搜索无候选时直接
        # dead，不弹码。
        if rec.get("state") not in ("armed", "pending", "dead"):
            return
        rec["state"] = "pending"
        rec["error"] = ""
        rec["updated"] = time.time()
        title = rec.get("title") or ""
        try:
            index = int(rec.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        candidates: list[str] = []
        for url in list(rec.get("manual_shares") or []):
            if isinstance(url, str) and url and url not in candidates:
                candidates.append(url)
        if title:
            try:
                # 同步 httpx 搜索约 10 秒：放 to_thread，不堵事件循环
                #（同函数内 resolve_share 也是 to_thread）。
                for url in await asyncio.to_thread(
                        _pansou_search, title, index) or []:
                    if isinstance(url, str) and url and url not in candidates:
                        candidates.append(url)
            except Exception:
                _log.debug("_pansou_run search failed: mg=%s", mg, exc_info=True)
        if not candidates:
            rec["state"] = "dead"
            rec["error"] = ("未找到网盘分享（PanSou 搜索无结果），"
                            "可从管理页贴分享链接后重试"
                            if title else "缺少片名，无法搜索网盘")
            return
        last_err = ""
        for url in candidates:
            pv = _pan_match(url)
            if pv is None:
                continue
            key = pv.get("key")
            if key not in _PAN_MODULES:
                continue
            mod = _pan_module(key)
            if mod is None:
                continue
            if not _pan_logged_in(key):
                rec["state"] = "need_login"
                rec["provider"] = key
                rec["error"] = f"{_PAN_MODULES[key][1]}未登录，播放画面扫码登录后自动继续"
                return
            try:
                res = await asyncio.to_thread(mod.resolve_share, url)
            except Exception as exc:
                last_err = str(exc)[:200]
                _log.debug("_pansou_run resolve failed: mg=%s key=%s",
                           mg, key, exc_info=True)
                continue
            items = (res or {}).get("items") or []
            if not items:
                last_err = "分享内没有可播放的视频文件"
                continue
            pick = items[min(max(index, 0), len(items) - 1)]
            play_url = pick.get("url") or ""
            if not play_url:
                last_err = "转存成功但未取到播放地址"
                continue
            snap = dict(_pansou_manual_target(index),
                        url=play_url, m3u8=True,
                        title=title or (res or {}).get("title") or "",
                        provider=key, share_url=url,
                        fid=pick.get("fid") or "")
            _PANSOU_MAN_SNAP[mg] = snap
            rec["state"] = "ready"
            rec["provider"] = key
            rec["share_url"] = url
            rec["error"] = ""
            rec["updated"] = time.time()
            return
        rec["state"] = "dead"
        rec["error"] = last_err or "网盘分享均无法转存"
        rec["updated"] = time.time()
    except Exception:
        _log.debug("_pansou_run failed: mg=%s", mg, exc_info=True)
        if rec.get("state") != "ready":
            rec["state"] = "dead"
            rec["error"] = "网盘转存内部异常，可重试"
