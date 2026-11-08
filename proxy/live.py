#!/usr/bin/env python3
"""实时播放诊断（内存态，不落盘）。

用途：管理台「实时播放诊断」展示此刻有哪些端（TV/Android/iOS/Web）在访问/播放、
走的是本地构造还是透传官方、哪一步出错。用于在用户报“某端不能播”时第一时间定位。

设计原则：
  * 纯内存、有界环形缓冲，进程重启即清空，绝不持久化、不记录完整 UA 之外的隐私。
  * 只记录播放链路相关接口（库浏览/详情/选集/播放/分片），过滤高频管理类噪声。
  * 按“设备”聚合：同类端 + 来源 IP + UA 指纹归为同一设备，展示该设备最近请求与
    当前播放会话。
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from threading import Lock

import client as clientmod

# 每设备保留的最近请求条数 / 设备上限 / 会话空闲过期（秒）
MAX_EVENTS_PER_DEVICE = 60
MAX_DEVICES = 32
SESSION_TTL = 600

_LOCK = Lock()
_DEVICES: "OrderedDict[str, dict]" = OrderedDict()
_SEQ = 0


def _ua_fp(ua: str) -> str:
    """从 UA 取稳定短指纹（bundle/版本/机型关键片段），不做哈希碰撞安全用途。"""
    s = ua or ""
    m = re.search(r"(com\.trim\.[a-z.]+|trimemedia-web)", s, re.I)
    core = (m.group(1).lower() if m else s[:24])
    tail = re.sub(r"[^a-z0-9.]+", "", s.lower())[-12:]
    return f"{core}|{tail}"[:48]


def reset() -> None:
    with _LOCK:
        _DEVICES.clear()


def record(ua: str, ip: str, method: str, path: str, query: str,
           status: int, origin: str, title: str = "", stage: str = "",
           note: str = "") -> None:
    """记录一条播放链路请求。

    origin: local=本插件构造；upstream=透传官方；mixed=先查官方再合并；redirect=302。
    title/stage/note: 尽量由播放路由补充（片名、环节、错误简述）。
    """
    global _SEQ
    cl = clientmod.parse_user_agent(ua)
    dev_id = f"{cl.kind}:{(ip or '?')}:{_ua_fp(ua)}"
    now = time.time()
    is_err = status >= 400
    with _LOCK:
        dev = _DEVICES.get(dev_id)
        if dev is None:
            dev = {"id": dev_id, "kind": cl.kind, "label": cl.label,
                   "ip": ip or "?", "ua": (ua or "")[:160],
                   "seen": now, "events": deque(maxlen=MAX_EVENTS_PER_DEVICE),
                   "errors": 0, "reqs": 0, "session": None,
                   "last_status": 0, "last_path": ""}
            _DEVICES[dev_id] = dev
            if len(_DEVICES) > MAX_DEVICES:
                _DEVICES.popitem(last=False)
        else:
            _DEVICES.move_to_end(dev_id)
        dev["seen"] = now
        dev["reqs"] += 1
        dev["last_status"] = status
        dev["last_path"] = path
        if is_err:
            dev["errors"] += 1
        ev = {"t": round(now, 3), "seq": _SEQ, "m": method,
              "p": (path + ("?" + query if query else ""))[:160],
              "status": status, "origin": origin,
              "stage": stage, "title": title[:80], "note": note[:120],
              "err": is_err}
        _SEQ += 1
        dev["events"].append(ev)
        # 维护“当前播放会话”：出现播放/分片类请求即更新；记录片名与最后活跃时间。
        is_play_stage = stage in ("play", "media", "hls", "seg", "direct")
        if title and is_play_stage:
            sess = dev.get("session") or {}
            # 切换到不同片名：开新会话，清掉上一部的错误标记
            if sess.get("title") and sess["title"] != title:
                sess = {}
            sess["title"] = title
            sess["stage"] = stage
            sess["status"] = status
            sess["at"] = round(now, 3)
            sess["path"] = path
            if is_err:
                sess["error"] = f"{status} {note or path}"[:120]
            else:
                # 成功的起播/清单/分片，说明上一次错误已恢复
                sess.pop("error", None)
            dev["session"] = sess
        elif is_play_stage:
            # 无片名（如未绑定 mg）：仅刷新活跃时间，不覆盖已有会话的片名/错误
            sess = dev.get("session")
            if sess is not None:
                sess["at"] = round(now, 3)
                sess["status"] = status
                if is_err:
                    sess["error"] = f"{status} {note or path}"[:120]
                elif status < 400:
                    sess.pop("error", None)


def snapshot() -> dict:
    """返回给管理台的实时快照。"""
    now = time.time()
    out = []
    with _LOCK:
        for dev in list(_DEVICES.values()):
            if now - dev["seen"] > 1800:
                continue
            sess = dev.get("session")
            active = bool(sess and now - sess.get("at", 0) < SESSION_TTL)
            out.append({
                "id": dev["id"], "kind": dev["kind"], "label": dev["label"],
                "ip": dev["ip"], "ua": dev["ua"],
                "seen": round(dev["seen"], 1),
                "ago": round(now - dev["seen"], 1),
                "reqs": dev["reqs"], "errors": dev["errors"],
                "last_status": dev["last_status"], "last_path": dev["last_path"],
                "active": active, "session": sess if active else None,
                "events": list(dev["events"])[-30:][::-1],
            })
    out.sort(key=lambda d: d["seen"], reverse=True)
    return {"now": round(now, 1), "devices": out}
