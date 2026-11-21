"""Bounded, redacted staged source diagnostics."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx
import netutil

try:
    from fnlog import get_logger
except ImportError:
    import logging
    def get_logger(name): return logging.getLogger(name)
_log = get_logger("source_diagnostics")


def _safe_url(url: str) -> str:
    p = urlsplit(str(url or ""))
    return f"{p.scheme}://{p.netloc}{p.path}"[:240]


def _stage(name, ok, started, **extra):
    return {"stage": name, "ok": bool(ok),
            "elapsed_ms": round((time.perf_counter()-started)*1000, 1), **extra}


async def diagnose_cms(source: dict, keyword: str = "庆余年") -> dict:
    """Run short CMS stages; never return query, cookies, or full URLs."""
    api = str(source.get("api") or "")
    out = {"source_id": str(source.get("id") or "")[:96],
           "source_name": str(source.get("name") or "")[:120],
           "api": _safe_url(api), "stages": []}
    import netutil
    async with netutil.async_client() as client:
        started = time.perf_counter()
        try:
            r = await client.get(api, params={"ac": "list", "pg": 1}, timeout=8)
            j = r.json()
            classes = len(j.get("class") or []) if isinstance(j, dict) else 0
            items = len(j.get("list") or []) if isinstance(j, dict) else 0
            out["stages"].append(_stage("list", r.status_code < 400,
                                        started, status=r.status_code,
                                        classes=classes, items=items))
        except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError) as exc:
            out["stages"].append(_stage("list", False, started,
                                        error_type=type(exc).__name__))
            return out
        started = time.perf_counter()
        try:
            r = await client.get(api, params={"wd": keyword, "pg": 1}, timeout=8)
            j = r.json(); rows = j.get("list") if isinstance(j, dict) else []
            out["stages"].append(_stage("search", r.status_code < 400,
                                        started, status=r.status_code,
                                        results=len(rows or [])))
            first = rows[0] if rows else None
        except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError) as exc:
            _log.debug("source diagnose search failed", exc_info=True)
            out["stages"].append(_stage("search", False, started,
                                        error_type=type(exc).__name__))
            return out
        if first and first.get("vod_id"):
            started = time.perf_counter()
            try:
                r = await client.get(api, params={"ac": "detail", "ids": first["vod_id"]}, timeout=8)
                j = r.json(); rows = j.get("list") if isinstance(j, dict) else []
                out["stages"].append(_stage("detail", r.status_code < 400,
                                            started, status=r.status_code,
                                            episodes=len(rows or [])))
            except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError) as exc:
                _log.debug("source diagnose detail failed", exc_info=True)
                out["stages"].append(_stage("detail", False, started,
                                            error_type=type(exc).__name__))
    return out
