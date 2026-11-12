#!/usr/bin/env python3
"""外挂 spider-runner 的 HTTP 客户端（完整版私有特性）。

主插件默认用进程内 sidecar（catvod.py 的 JVM、drpy.py 的 Node）。当爬虫运行时被
部署为独立容器（见仓库 spider-runner/，docker compose），设置环境变量
`FNM_RUNNER_URL`（如 http://127.0.0.1:9100）即可让调用改走统一 HTTP 服务：

    POST {FNM_RUNNER_URL}/invoke
    {"runtime":"java|node|android|auto", ...catvod/drpy 参数}

未设置该变量时本模块不启用（enabled() 为 False），一切维持本地 sidecar 路径，
零外部依赖。仅完整版存在（公开构建删除本文件）。

错误约定（guardfix-e）：业务错误透传 runner 信封的 kind/原始类名/全文，
不截断；传输层失败（连接失败、超时、非 JSON 响应）抛 RunnerTransportError，
供 drpy 区分“回退本地”与“业务错误直接上抛”。
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

from runner_errors import RunnerError, RunnerTransportError, from_envelope

_log = get_logger("runner_client")

_URL = os.environ.get("FNM_RUNNER_URL", "").rstrip("/")
_TIMEOUT = float(os.environ.get("FNM_RUNNER_TIMEOUT", "120"))


def enabled() -> bool:
    return bool(_URL)


def base_url() -> str:
    return _URL


# 注意：旧 health() 已删除（guardfix-e）。全仓无调用方；guard.health()
# 自有 /health 检查且用更紧的超时（connect 0.4s / 总 2s），不要再留第二条
# 语义不一致的健康路径。


def invoke(payload: dict, timeout: float | None = None):
    """同步调用统一 runner。失败抛 RunnerError（业务）或 RunnerTransportError（传输层）。"""
    if not _URL:
        raise RuntimeError("未配置 FNM_RUNNER_URL")
    body = dict(payload)
    body.setdefault("_timeout", timeout or _TIMEOUT)
    try:
        r = httpx.post(_URL + "/invoke", json=body,
                       timeout=timeout or _TIMEOUT, trust_env=False)
    except httpx.HTTPError as exc:
        _log.debug("invoke failed", exc_info=True)
        raise RunnerTransportError(
            f"spider-runner 调用失败: {exc}", orig=type(exc).__name__) from exc
    try:
        j = r.json()
    except ValueError:
        # runner 返回非 JSON（如 502/空体）：按 HTTP 状态码合成传输层错误。
        raise RunnerTransportError(f"runner HTTP {r.status_code}") from None
    if not j.get("ok"):
        raise from_envelope(j)
    return j.get("result")
