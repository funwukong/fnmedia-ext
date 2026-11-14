#!/usr/bin/env python3
"""CMS 源批量探活：受控并发、实时进度、可取消、结果保留。

借鉴 LibreTV use-source-probe：默认并发 4、最多 100 个源；命中任一热门词即判
有效。结果回写 sourcehealth，与起播/竞速共用同一套健康画像，不另建标准。
"""
from __future__ import annotations

import asyncio
import time

import providers
import sourcehealth

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("source_probe")

_state: dict = {"running": False, "total": 0, "done": 0, "alive_count": 0,
                "unknown_count": 0, "removed_count": 0,
                "results": [], "started_at": 0.0, "error": ""}
_task: asyncio.Task | None = None
_run_id = 0
# 失效源禁用回调：由调用方注入（避免本模块依赖 config）。
# 签名 (source_id: str) -> None。
_disable_sink = None
# 探活实现（可注入，测试/降级用）
_probe_impl = None


def _default_probe():
    """默认探活实现。

    延迟到调用时解析，这样测试里 patch 的
    ``providers.probe_source_alive`` / ``providers.probe_any_source``
    以及本模块的 ``_probe_impl`` 都仍然生效。
    """
    if _probe_impl is not None:
        return _probe_impl
    return providers.probe_any_source


def status() -> dict:
    return {**_state, "results": list(_state["results"])}


async def start(sources: list[dict], *, concurrency: int = 4,
                limit: int = 500, disable_sink=None) -> dict:
    global _task, _run_id, _disable_sink
    _disable_sink = disable_sink
    if _state["running"]:
        return status()
    _run_id += 1
    rid = _run_id
    # limit<=0 表示「全部探活」。旧实现硬顶 500，站点多于一屏时
    # 后半段永远轮不到 → 用户体感「探活探不全」。
    all_src = list(sources)
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 0
    targets = all_src if lim <= 0 else all_src[:lim]
    _state.update(running=True, total=len(targets), done=0, alive_count=0,
                  unknown_count=0, removed_count=0,
                  results=[], started_at=time.time(), error="")
    cursor = 0
    lock = asyncio.Lock()

    async def one(source: dict, index: int):
        removed = False
        try:
            result = await _default_probe()(source)
        except Exception as exc:  # noqa: BLE001
            _log.debug("source probe failed: id=%s", source.get("id"),
                       exc_info=True)
            # 地址非法/配置残缺属于确定性失效（重试也不会好），
            # TLS 证书链缺失/协议错误同理（站点侧问题，换网络不会好），
            # 记为 alive=False 交给下面的删除判据；其余网络类异常才算未知。
            _fatal = isinstance(exc, (ValueError, KeyError)) or \
                bool(getattr(providers, "_is_site_side_error", lambda e: False)(exc))
            result = {"alive": False if _fatal else None, "ok": False,
                      "elapsed_ms": 0,
                      "count": 0, "error": type(exc).__name__,
                      "hit": "", "tried": []}
        if rid != _run_id:
            return
        # 失效/风险自动处理：只有 probe 明确给出 JSON 判据（ok 且
        # alive is False）才动。alive=None（可访问但无基准命中、
        # 运行时不具备）以及探活本身抛异常（ok=False）绝不删除。
        # 风险源（json 型接口/可疑域）同样处理，避免残留死接口。
        risky = bool(source.get("risk"))
        alive_now = result.get("alive")
        dead = result.get("ok", True) and alive_now is False
        # risk=True 是风险源，按要求一律清掉；
        # json_api 只是接口形态（不一定有 CMS 分类页），探活判死才清，
        # 否则会把能出内容的 JSON 源误删。
        if ((dead or risky) and _disable_sink is not None):
            try:
                _disable_sink(source.get("id", ""))
            except Exception:  # noqa: BLE001
                _log.debug("probe disable sink failed: id=%s",
                           source.get("id"), exc_info=True)
            removed = True
        key = ""
        try:
            if sourcehealth is not None:
                key = (sourcehealth.source_key_of(source.get("id"), source)
                       or source.get("id", ""))
        except Exception:  # noqa: BLE001
            key = source.get("id", "") or ""
        if key and result.get("alive") is not None:
            try:
                if sourcehealth is not None:
                    sourcehealth.record(key, bool(result.get("alive")),
                                        (result.get("elapsed_ms") or 0) / 1000.0)
            except Exception:  # noqa: BLE001
                _log.debug("probe health record failed: id=%s",
                           source.get("id"), exc_info=True)
        row = {"id": source.get("id"), "name": source.get("name", ""),
               "index": index,
               "alive": bool(result.get("alive")), "hit": result.get("hit", ""),
               "tried": result.get("tried", []), "count": result.get("count", 0),
               "elapsed_ms": result.get("elapsed_ms", 0),
               "status": result.get("status", 0), "error": result.get("error", ""),
               "removed": removed, "reason": result.get("reason", ""),
               # alive=True/False 是结论，None 是「没测出结论」（网络层失败），
               # 前端必须区分，否则 healthy 源会被显示成失效。
               "state": ("alive" if alive_now is True
                         else "dead" if alive_now is False else "unknown"),
               "dead": sourcehealth.is_dead(key) if key else False}
        async with lock:
            _state["results"].append(row)
            _state["done"] += 1
            _state["alive_count"] += int(alive_now is True)
            if alive_now is None:
                _state["unknown_count"] += 1
            _state["removed_count"] += int(bool(removed))

    async def worker():
        nonlocal cursor
        while rid == _run_id:
            async with lock:
                if cursor >= len(targets):
                    return
                idx = cursor
                cursor += 1
                source = targets[idx]
            await one(source, idx + 1)

    async def job():
        try:
            await asyncio.gather(*[worker() for _ in range(
                max(1, min(int(concurrency), len(targets) or 1)))])
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.debug("source probe job failed", exc_info=True)
            _state["error"] = str(exc)[:200]
        finally:
            if rid == _run_id:
                _state["running"] = False

    _task = asyncio.create_task(job(), name=f"cms-source-probe-{rid}")
    return status()


def cancel() -> dict:
    global _run_id
    _run_id += 1
    if _task and not _task.done():
        _task.cancel()
    _state["running"] = False
    return status()


async def wait() -> dict:
    """等待本轮结束（测试/诊断用）；生产轮询 status 即可。"""
    task = _task
    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass
    return status()
