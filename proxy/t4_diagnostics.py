"""Bounded, redacted staged T4 diagnostics."""
from __future__ import annotations

import time
import asyncio

import t4


def _stage(name, started, ok, **extra):
    return {"stage": name, "ok": bool(ok),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1), **extra}


async def diagnose(service, client, source: dict, guid: str = "") -> dict:
    out = {"source_id": str(source.get("id") or "")[:96],
           "source_name": str(source.get("name") or "")[:120],
           "stages": []}
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(t4.probe(client, source), timeout=10)
        out["stages"].append(_stage("connect", started, True,
                                    classes=len(result.get("classes") or []),
                                    items=len(result.get("list") or [])))
    except (t4.T4Error, asyncio.TimeoutError, OSError, ValueError, TypeError) as exc:
        out["stages"].append(_stage("connect", started, False,
                                    error_type=type(exc).__name__))
        return out
    if guid:
        started = time.monotonic()
        try:
            brief = await asyncio.wait_for(service.ensure_detail(client, guid, force=True), timeout=15)
            lines = len((brief or {}).get("lines") or {})
            episodes = sum(len(x) for x in ((brief or {}).get("lines") or {}).values())
            out["stages"].append(_stage("detail", started, bool(brief),
                                        lines=lines, episodes=episodes))
            if brief and episodes:
                started = time.monotonic()
                out["stages"].append(_stage("episodes", started, True,
                                            episodes=episodes))
                target = next((e for vals in brief["lines"].values() for e in vals), None)
                if target:
                    started = time.monotonic()
                    try:
                        playback = await asyncio.wait_for(service.resolve(client, {
                            "online_guid": guid, "index": 0,
                            "line": target.get("play_from", ""),
                            "episode_key": target.get("episode_key", "")}), timeout=15)
                        out["stages"].append(_stage("play", started, bool(playback),
                                                    wrapper=getattr(playback, "wrapper", "")))
                    except (t4.T4Error, asyncio.TimeoutError, OSError, ValueError, TypeError) as exc:
                        out["stages"].append(_stage("play", started, False,
                                                    error_type=type(exc).__name__))
        except (t4.T4Error, asyncio.TimeoutError, OSError, ValueError, TypeError) as exc:
            out["stages"].append(_stage("detail", started, False,
                                        error_type=type(exc).__name__))
    return out
