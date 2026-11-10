#!/usr/bin/env python3
"""管理令牌（admin_token）校验、轮换宽限与登录限流。

现状说明（2026-11-09 摸底）：``admin_token`` 只在 ``config.py`` 里生成，
全仓库没有任何一处校验它——管理页 ``/v/online-admin`` 与全部
``/v/api/ext/*`` 写接口都是无鉴权的（只有 ``update/now`` 查一个可伪造的
自定义头 ``x-fnmedia-admin``）。本模块提供校验/轮换/限流的原语与登录
检查点。

用户决定（2026-11-09，仓库 owner 明确决定）：**不对 ``/v/api/ext/*``
写接口加鉴权门**——令牌此前从未向用户展示过，直接上锁会把用户锁在
管理页外面。后来人不要再“补”这个缺口：无鉴权写接口是刻意保留的现状，
不是待修的漏洞；如需改变，须由 owner 重新决定，勿擅自上锁。

设计要点：
- 新令牌 ``secrets.token_hex(32)``（64 hex 字符 = 256 bit）。
- 老令牌（16 hex 字符，``token_hex(8)``）升级后继续可用，永不因长度被拒；
  只有在 ``admin_token_max_age_days > 0`` 且创建时间已知时才按年龄过期，
  默认 0 = 不按年龄强制过期（避免用户久未登录被锁死）。
- 轮换：当前令牌移入 ``admin_token_previous``，在宽限期
  （``admin_token_grace_hours``，默认 24 小时）内仍可登录；宽限期过后失效。
- 限流：内存记录失败认证，按客户端 IP 与令牌指纹分别计数；连续失败
  ``admin_auth_max_failures`` 次（默认 5）后锁定 ``admin_auth_lockout_seconds``
  秒（默认 900）。成功登录清零。指纹是令牌的 SHA-256 截断，日志与键里
  永不出现令牌原文。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

# 新令牌字节数：token_hex(32) = 64 hex 字符 = 256 bit。
TOKEN_BYTES = 32
# 老令牌 token_hex(8) = 16 hex 字符；低于此长度的已存令牌视为异常但仍按
# 常量时间比较处理（不因长度单独放行或拒绝，是否有效只看是否相等）。
LEGACY_MIN_HEX_LEN = 16

DEFAULT_GRACE_HOURS = 24
DEFAULT_MAX_AGE_DAYS = 0  # 0 = 不按年龄强制过期
DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCKOUT_SECONDS = 900
DEFAULT_WINDOW_SECONDS = 900


def new_token() -> str:
    """生成一个新管理令牌（256 bit）。"""
    return secrets.token_hex(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """令牌指纹（SHA-256 前 16 hex 字符）：用于限流键与日志，永不还原原文。"""
    return hashlib.sha256((token or "").encode()).hexdigest()[:16]


def mask_token(token: str) -> str:
    """掩码展示：前 4 + … + 后 4；空令牌返回空串。"""
    t = token or ""
    if not t:
        return ""
    if len(t) <= 8:
        return t[:2] + "…" + t[-2:]
    return t[:4] + "…" + t[-4:]


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def _cfg_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def token_info(cfg: dict, now: float | None = None) -> dict:
    """令牌状态（不含原文，供管理页展示）。

    - ``age_seconds``：创建时间未知（老配置未打戳）时为 None，代表“未知”。
    - ``expires_at``：``admin_token_max_age_days <= 0`` 或创建时间未知时为 None。
    - ``grace_remaining_seconds``：无旧令牌或已过期时为 0。
    """
    now = time.time() if now is None else float(now)
    cfg = cfg or {}
    token = str(cfg.get("admin_token") or "")
    created_at = _cfg_int(cfg, "admin_token_created_at", 0)
    max_age_days = _cfg_int(cfg, "admin_token_max_age_days", DEFAULT_MAX_AGE_DAYS)
    grace_hours = _cfg_int(cfg, "admin_token_grace_hours", DEFAULT_GRACE_HOURS)
    prev = str(cfg.get("admin_token_previous") or "")
    prev_expires = _cfg_int(cfg, "admin_token_previous_expires_at", 0)
    age = int(now - created_at) if created_at > 0 else None
    expires_at = None
    expired = False
    if created_at > 0 and max_age_days > 0:
        expires_at = created_at + max_age_days * 86400
        expired = now >= expires_at
    grace_remaining = 0
    if prev and prev_expires > 0:
        grace_remaining = max(0, int(prev_expires - now))
    return {
        "masked": mask_token(token),
        "length": len(token),
        "bits": len(token) * 4,
        "created_at": created_at or None,
        "age_seconds": age,
        "max_age_days": max_age_days,
        "expires_at": expires_at,
        "expired": expired,
        "grace_hours": grace_hours,
        "has_previous": bool(prev),
        "grace_remaining_seconds": grace_remaining,
        "max_failures": _cfg_int(cfg, "admin_auth_max_failures", DEFAULT_MAX_FAILURES),
        "lockout_seconds": _cfg_int(cfg, "admin_auth_lockout_seconds", DEFAULT_LOCKOUT_SECONDS),
        "window_seconds": _cfg_int(cfg, "admin_auth_window_seconds", DEFAULT_WINDOW_SECONDS),
    }


def verify_token(cfg: dict, candidate: str, now: float | None = None) -> tuple[bool, str]:
    """校验候选令牌。返回 ``(是否通过, 原因)``。

    原因：``ok``（当前令牌）/ ``grace``（宽限期内旧令牌）/ ``empty`` /
    ``invalid`` / ``expired``（当前令牌超龄，且无宽限旧令牌可用时也一样）/
    ``no_token_configured``（配置里根本没有令牌，属异常状态）。
    老令牌只要相等就通过，不做长度校验；创建时间未知时不按年龄过期。
    比较用 ``hmac.compare_digest``（常量时间）。
    """
    now = time.time() if now is None else float(now)
    cfg = cfg or {}
    current = str(cfg.get("admin_token") or "")
    candidate = str(candidate or "")
    if not candidate:
        return False, "empty"
    if not current:
        return False, "no_token_configured"
    # 当前令牌：先看是否超龄（创建时间已知且配了 max_age_days 才过期）。
    info = token_info(cfg, now)
    if not info["expired"] and hmac.compare_digest(candidate, current):
        return True, "ok"
    # 宽限期旧令牌：只在宽限期内有效；超龄的当前令牌不影响旧令牌的宽限。
    prev = str(cfg.get("admin_token_previous") or "")
    prev_expires = _cfg_int(cfg, "admin_token_previous_expires_at", 0)
    if prev and prev_expires > 0 and now <= prev_expires \
            and hmac.compare_digest(candidate, prev):
        return True, "grace"
    if info["expired"]:
        return False, "expired"
    return False, "invalid"


class LoginAttemptLimiter:
    """内存登录限流：按 IP 与令牌指纹分别计数。

    每个键独立记 ``{失败次数, 窗口起始, 锁定截止}``；窗口
    （``window_seconds``）内失败满 ``max_failures`` 次即锁定
    ``lockout_seconds`` 秒；锁定到期后计数清零；成功登录清零该 IP 与
    该令牌指纹的计数。进程重启后计数丢失（Fail-open：重启不锁死用户，
    限流只防在线爆破）。
    """

    def __init__(self, max_failures: int = DEFAULT_MAX_FAILURES,
                 lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS,
                 window_seconds: int = DEFAULT_WINDOW_SECONDS,
                 now_fn=None):
        self._lock = threading.Lock()
        self._attempts: dict[str, dict] = {}
        # 每个 IP 试过的令牌指纹键：成功登录时一并清零（见 record_success）。
        self._ip_tokens: dict[str, set[str]] = {}
        self._now_fn = now_fn or time.time
        self.configure(max_failures, lockout_seconds, window_seconds)

    def configure(self, max_failures: int, lockout_seconds: int,
                  window_seconds: int) -> None:
        """按当前配置刷新阈值（不清除已有计数）。"""
        with self._lock:
            self.max_failures = max(1, int(max_failures or DEFAULT_MAX_FAILURES))
            self.lockout_seconds = max(0, int(lockout_seconds or 0))
            self.window_seconds = max(1, int(window_seconds or DEFAULT_WINDOW_SECONDS))

    def _now(self) -> float:
        return float(self._now_fn())

    @staticmethod
    def ip_key(ip: str) -> str:
        return "ip:" + (ip or "unknown")

    @staticmethod
    def token_key(candidate: str) -> str:
        return "tok:" + token_fingerprint(candidate)

    def _entry(self, key: str) -> dict:
        return self._attempts.setdefault(
            key, {"count": 0, "first_at": 0.0, "locked_until": 0.0})

    def _locked_remaining(self, key: str, now: float) -> float:
        e = self._attempts.get(key)
        if not e:
            return 0.0
        if e["locked_until"] > now:
            return e["locked_until"] - now
        if e["locked_until"]:
            # 锁定已到期：计数清零，给一次新的机会窗口。
            e["count"] = 0
            e["first_at"] = 0.0
            e["locked_until"] = 0.0
        return 0.0

    def is_locked(self, ip: str, candidate: str) -> tuple[bool, float]:
        """是否被锁定。返回 ``(锁定中, 剩余秒数)``（取 IP/令牌两键中较长者）。"""
        now = self._now()
        with self._lock:
            rem = max(self._locked_remaining(self.ip_key(ip), now),
                      self._locked_remaining(self.token_key(candidate), now))
        return rem > 0, rem

    def record_failure(self, ip: str, candidate: str) -> None:
        now = self._now()
        with self._lock:
            ipk, tokk = self.ip_key(ip), self.token_key(candidate)
            self._ip_tokens.setdefault(ipk, set()).add(tokk)
            for key in (ipk, tokk):
                e = self._entry(key)
                if e["locked_until"] > now:
                    continue
                if not e["first_at"] or now - e["first_at"] > self.window_seconds:
                    e["count"] = 1
                    e["first_at"] = now
                else:
                    e["count"] += 1
                if e["count"] >= self.max_failures:
                    e["locked_until"] = now + self.lockout_seconds

    def record_success(self, ip: str, candidate: str) -> None:
        """成功登录：清零该 IP 的计数，以及该 IP 试过的所有令牌指纹的计数
        （成功证明该 IP 是合法管理员，之前用错令牌的失败记录不再算数）
        和本次正确令牌的计数。"""
        with self._lock:
            ipk = self.ip_key(ip)
            for tk in self._ip_tokens.pop(ipk, ()):
                self._attempts.pop(tk, None)
            self._attempts.pop(ipk, None)
            self._attempts.pop(self.token_key(candidate), None)

    def status(self, ip: str, candidate: str = "") -> dict:
        locked, remaining = self.is_locked(ip, candidate)
        return {"locked": locked, "retry_after_seconds": int(remaining) + 1 if locked else 0,
                "max_failures": self.max_failures,
                "lockout_seconds": self.lockout_seconds,
                "window_seconds": self.window_seconds}
