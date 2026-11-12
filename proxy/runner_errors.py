#!/usr/bin/env python3
"""proxy 侧结构化 runner 错误（guardfix-e）。

与 spider-runner/app/runner_errors.py 镜像：同一种 kind 语义、同一种分类规则。

kind：spider（爬虫业务异常）| jar（jar/类缺失）| timeout（调用超时）
     | fatal（传输层失败与其余未知错误）

约定：各层透传 error 全文 + kind + 原始类名，不再逐跳截断；
截断只发生在最终日志处。
"""
from __future__ import annotations

KINDS = ("spider", "jar", "timeout", "fatal")

_TIMEOUT_WORDS = ("timeout", "timed out", "deadline exceeded", "timedout")
_JAR_WORDS = ("classnotfound", "dexpathlist", "noclassdef",
              "classloader", "dex", "no such method")


class RunnerError(RuntimeError):
    """带 kind 与原始类名的 runner 错误。"""

    def __init__(self, message: str, kind: str = "fatal",
                 orig: str | None = None):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "fatal"
        self.orig = orig or type(self).__name__


class RunnerTransportError(RunnerError):
    """传输层失败（连接被拒/HTTP 非 JSON/502 等），与业务错误区分。

    drpy 回退本地只针对此类；业务 RunnerError 直接上抛。
    """

    def __init__(self, message: str, orig: str | None = None):
        super().__init__(message, kind="fatal",
                         orig=orig or type(self).__name__)


class RunnerTimeoutError(TimeoutError):
    """超时（kind=timeout；同时是 TimeoutError，兼容旧 except）。"""

    def __init__(self, message: str, orig: str | None = None):
        super().__init__(message)
        self.kind = "timeout"
        self.orig = orig or type(self).__name__


def kind_of(exc: BaseException) -> str:
    """从异常对象推断 kind：显式 kind 优先，其次类型，最后文本匹配。"""
    kind = getattr(exc, "kind", None)
    if kind in KINDS:
        return kind
    if isinstance(exc, TimeoutError):
        return "timeout"
    try:
        import httpx
        if isinstance(exc, httpx.HTTPError):
            return "fatal"
    except Exception:  # noqa: BLE001
        pass
    import urllib.error
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return "fatal"
    msg = str(exc).lower()
    if any(w in msg for w in _TIMEOUT_WORDS):
        return "timeout"
    if any(w in msg for w in _JAR_WORDS):
        return "jar"
    return "spider"


def error_class_of(exc: BaseException) -> str:
    return getattr(exc, "orig", None) or type(exc).__name__


def to_envelope(exc: BaseException) -> dict:
    return {"ok": False, "error": str(exc), "kind": kind_of(exc),
            "error_class": error_class_of(exc)}


def from_envelope(payload: dict) -> RunnerError:
    """把对端信封还原为 RunnerError（kind/原始类名不丢失）。

    信封无 kind 时按错误文本分类（业务错误默认 spider，而非 fatal）。
    """
    kind = str(payload.get("kind") or "")
    if kind not in KINDS:
        kind = kind_of(RuntimeError(str(payload.get("error") or "")))
    return RunnerError(str(payload.get("error") or payload),
                       kind=kind,
                       orig=str(payload.get("error_class") or "RuntimeError"))
