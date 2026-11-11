#!/usr/bin/env python3
"""外挂 spider-runner 的 HTTP 客户端（完整版私有特性）。

主插件默认用进程内 sidecar（catvod.py 的 JVM、drpy.py 的 Node）。当爬虫运行时被
部署为独立容器（见仓库 spider-runner/，docker compose），设置环境变量
`FNM_RUNNER_URL`（如 http://127.0.0.1:9100）即可让调用改走统一 HTTP 服务：

    POST {FNM_RUNNER_URL}/invoke
    {"runtime":"java|node|android|auto", ...catvod/drpy 参数}

未设置该变量时本模块不启用（enabled() 为 False），一切维持本地 sidecar 路径，
零外部依赖。仅完整版存在（公开构建删除本文件）。
"""
from __future__ import annotations

import os

import httpx

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("runner_client")

_URL = os.environ.get("FNM_RUNNER_URL", "").rstrip("/")
_TIMEOUT = float(os.environ.get("FNM_RUNNER_TIMEOUT", "120"))


def enabled() -> bool:
    return bool(_URL)


def base_url() -> str:
    return _URL


def health() -> dict:
    if not _URL:
        return {"ok": False, "enabled": False}
    try:
        r = httpx.get(_URL + "/health", timeout=6, trust_env=False)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        _log.debug("health failed", exc_info=True)
        return {"ok": False, "enabled": True, "error": str(exc)[:160]}


def invoke(payload: dict, timeout: float | None = None):
    """同步调用统一 runner。失败抛 RuntimeError。"""
    if not _URL:
        raise RuntimeError("未配置 FNM_RUNNER_URL")
    body = dict(payload)
    body.setdefault("_timeout", timeout or _TIMEOUT)
    try:
        r = httpx.post(_URL + "/invoke", json=body,
                       timeout=timeout or _TIMEOUT, trust_env=False)
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        _log.debug("invoke failed", exc_info=True)
        raise RuntimeError(f"spider-runner 调用失败: {exc}") from exc
    if not j.get("ok"):
        raise RuntimeError(str(j.get("error") or j)[:300])
    return j.get("result")
