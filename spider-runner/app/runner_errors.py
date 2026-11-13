#!/usr/bin/env python3
"""spider-runner 侧结构化错误（guardfix-e）。

与 proxy/runner_errors.py 镜像：同一种 kind 语义、同一种分类规则。
本模块只用标准库（runner 容器无第三方依赖）。

kind：
  spider  —— 爬虫业务异常（源代码抛错、bridge 返回的 error）
  jar     —— jar/类缺失（ClassNotFound、DexPathList、dex 加载失败）
  timeout —— 调用超时（含 socket/读取超时）
  fatal   —— 其余（传输层失败、未知错误）

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

    与 proxy/runner_errors.py 同名镜像：两份文件须保持同一公开 API，
    测试进程会同时把 proxy/ 与 spider-runner/app/ 放上 sys.path。
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
    # 传输层异常（urllib / socket / OSError / httpx）归 fatal，不冒充业务错误。
    # 本模块只用标准库，httpx 用类名/模块名鸭子类型识别（与 proxy 版行为一致）。
    if type(exc).__module__.split(".")[0] in ("httpx", "httpcore", "urllib3"):
        return "fatal"
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
