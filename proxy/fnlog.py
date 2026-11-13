#!/usr/bin/env python3
"""fnmedia-ext 统一日志（P0）。

约定：
- 所有模块用 ``get_logger(__name__)``，日志名前缀统一为 ``fnmedia.``；
- 级别由环境变量 ``FNMEDIA_LOG_LEVEL`` 控制，默认 INFO；
- 默认只输出到 stderr（uvicorn / systemd journal 可直接收）；
  若 ``state/logs/`` 可写，再加一个按天轮转的文件 handler，
  文件写失败时静默回退到只输出 stderr，绝不因此影响启动；
- 禁止把 token / cookie / 完整播放地址记入日志，调用方只传
  脱敏后的源 id、域名、耗时、状态码 / 异常类型。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_PREFIX = "fnmedia."
_DEFAULT_LEVEL = "INFO"
_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """返回统一前缀的 logger；重复调用返回同一实例。"""
    name = name.removeprefix(_PREFIX)
    return logging.getLogger(f"{_PREFIX}{name}")


def setup_logging(level: str | None = None) -> None:
    """配置根 logger（幂等）。只在进程入口调用一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    lvl = (level or os.environ.get("FNMEDIA_LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    try:
        numeric = getattr(logging, lvl)
    except AttributeError:
        numeric = logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [fnmedia] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(numeric)
    if not root.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    # 文件日志：尽力而为，失败不影响启动。
    try:
        log_dir = Path(__file__).resolve().parent.parent / "state" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            log_dir / "fnmedia-ext.log", when="midnight", backupCount=7,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass
