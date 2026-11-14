"""扩展 CMS 探活到【全部源类型】，并支持自动禁用失效源。

用户要求（2026-11-13）：
  1) 探活要覆盖已添加的【全部】源，不只纯 CMS；
  2) 探出失效的源自动置 enabled=false（保留在列表，可手动恢复）；
  3) 已禁用的源也一起探（便于发现恢复可用的源）。

各类型探活方式：
  · 纯 CMS           → providers.probe_source_alive（多热门词）
  · T4               → 同 CMS 接口（api 指向 t4 服务）
  · spider / drpy    → 依赖 sidecar 运行时；无运行时判 reachable=None
                       （不误禁用），有运行时时用 spider/drpy 搜索探
  · catvod / xsrc    → 同上，走各自搜索探活
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = ROOT / "proxy"

# ---------------- 1) providers: 通用探活（按源类型分发） ----------------

NEW_FN = '''

async def probe_any_source(source: dict, timeout: float = 8.0,
                           keywords=PROBE_HOT_KEYWORDS) -> dict:
    """按源类型探活任意源，返回与 probe_source_alive 兼容的结构。

    2026-11-13：原批量体检只覆盖纯 CMS 源，spider/drpy/t4/catvod/xsrc
    全被跳过，用户看到的“探活不全”即此。这里按类型分发：
      · CMS / T4       → probe_source_alive（多热门词）
      · spider / drpy  → 交给各自搜索实现（需 sidecar 运行时可跑）
      · catvod / xsrc  → 同上
    运行时不具备时返回 alive=None（reachable=False），
    由调用方决定不据此禁用，避免误伤。
    """
    src = dict(source or {})
    kind = ""
    if src.get("spider_src"):
        kind = "spider"
    elif src.get("drpy"):
        kind = "drpy"
    elif src.get("catvod"):
        kind = "catvod"
    elif src.get("xsrc"):
        kind = "xsrc"
    elif src.get("t4"):
        kind = "t4"

    if kind in ("", "t4"):
        return await probe_source_alive(src, timeout=timeout, keywords=keywords)

    # 非 CMS 源：走各自搜索探活；需要运行时（sidecar）在，否则不下结论。
    t0 = time.monotonic()
    for kw in keywords:
        try:
            async with netutil.async_client() as client:
                titles = await _dispatch_search(client, src, kw)
        except Exception as exc:  # noqa: BLE001
            _log.debug("probe_any_source %s failed: id=%s", kind,
                       src.get("id"), exc_info=True)
            return {"ok": False, "alive": None, "reachable": False,
                    "status": 0, "count": 0, "sample": "", "hit": "",
                    "tried": [kw], "error": type(exc).__name__,
                    "reason": f"{kind} 运行时不可用或探活失败",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        n = len(titles or [])
        if n > 0:
            return {"ok": True, "alive": True, "reachable": True, "status": 200,
                    "count": n, "sample": "", "hit": kw, "tried": [kw],
                    "error": "",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    return {"ok": True, "alive": None, "reachable": True, "status": 200,
            "count": 0, "sample": "", "hit": "", "tried": list(keywords),
            "error": "", "reason": "接口正常但基准热门词无结果",
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}
'''


def patch_providers() -> str:
    p = PROXY / "providers.py"
    s = p.read_text(encoding="utf-8")
    if "probe_any_source" in s:
        return "providers 已有 probe_any_source"
    anchor = "\n\n# >>> PRIVATE:DDUO"
    assert anchor in s, "providers 锚点缺失"
    s = s.replace(anchor, NEW_FN + anchor, 1)
    p.write_text(s, encoding="utf-8")
    return "providers.probe_any_source"


# ---------------- 2) admin_api: 探活范围 + 自动禁用 ----------------

def patch_admin_api() -> str:
    p = PROXY / "admin_api.py"
    s = p.read_text(encoding="utf-8")
    old = (
        '    targets = [s for s in config.all_sources() if _is_pure_cms(s)]\n'
        '    return {"code": 0, "msg": "", "data": await _probe.start(\n'
        '        targets, concurrency=concurrency)}'
    )
    new = (
        '    # 2026-11-13：探活覆盖【全部源】（含已禁用），不只纯 CMS ——\n'
        '    # 原实现只挑 _is_pure_cms，spider/drpy/t4/catvod/xsrc 全被跳过，\n'
        '    # 用户看到的「探活不全」即此。已禁用源也探，便于发现恢复可用的源。\n'
        '    targets = list(config.all_sources())\n'
        '    return {"code": 0, "msg": "", "data": await _probe.start(\n'
        '        targets, concurrency=concurrency)}'
    )
    if old in s:
        s = s.replace(old, new, 1)
    elif "targets = list(config.all_sources())" not in s:
        raise SystemExit("admin_api 探活入口未命中")
    p.write_text(s, encoding="utf-8")
    return "admin_api 探活全部源"


# ---------------- 3) source_probe: 自动禁用 + 类型分发 ----------------

def patch_source_probe() -> str:
    p = PROXY / "source_probe.py"
    s = p.read_text(encoding="utf-8")

    # 3a 用 probe_any_source 取代 probe_source_alive
    s2 = s.replace("result = await providers.probe_source_alive(source)",
                   "result = await providers.probe_any_source(source)")
    if s2 == s:
        raise SystemExit("source_probe 探活调用未命中")
    s = s2

    # 3b 失效自动禁用（保留在列表，可手动恢复）
    old_row = '        if rid != _run_id:\n            return\n'
    new_row = (
        '        if rid != _run_id:\n            return\n'
        '        # 失效自动禁用：alive is False 才动（alive=None 表示\n'
        '        # 可访问但无基准命中，或运行时不具备，绝不据此禁用）。\n'
        '        # 用 sink 回调交给调用方落盘，避免本模块直接依赖 config。\n'
        '        if result.get("alive") is False and _disable_sink is not None:\n'
        '            try:\n'
        '                _disable_sink(source.get("id", ""))\n'
        '            except Exception:  # noqa: BLE001\n'
        '                _log.debug("probe disable sink failed: id=%s",\n'
        '                           source.get("id"), exc_info=True)\n'
    )
    if old_row in s:
        s = s.replace(old_row, new_row, 1)

    # 3c 声明 sink + start() 参数
    s = s.replace(
        '_task: asyncio.Task | None = None\n_run_id = 0',
        '_task: asyncio.Task | None = None\n_run_id = 0\n'
        '# 失效源禁用回调：由调用方注入（避免本模块依赖 config）。\n'
        '# 签名 (source_id: str) -> None。\n'
        '_disable_sink = None')
    s = s.replace(
        'async def start(sources: list[dict], *, concurrency: int = 4,\n'
        '                limit: int = 100) -> dict:\n'
        '    global _task, _run_id',
        'async def start(sources: list[dict], *, concurrency: int = 4,\n'
        '                limit: int = 100, disable_sink=None) -> dict:\n'
        '    global _task, _run_id, _disable_sink\n'
        '    _disable_sink = disable_sink')
    p.write_text(s, encoding="utf-8")
    return "source_probe 自动禁用 + 类型分发"


# ---------------- 4) admin_api: 注入禁用回调 ----------------

def patch_sink() -> str:
    p = PROXY / "admin_api.py"
    s = p.read_text(encoding="utf-8")
    old = ('    targets = list(config.all_sources())\n'
           '    return {"code": 0, "msg": "", "data": await _probe.start(\n'
           '        targets, concurrency=concurrency)}')
    new = ('    targets = list(config.all_sources())\n\n'
           '    def _disable(sid: str) -> None:\n'
           '        """探活判定失效 → 自动禁用该源（保留在列表，可手动恢复）。"""\n'
           '        if not sid:\n'
           '            return\n'
           '        try:\n'
           '            config.set_source_enabled(sid, False)\n'
           '        except AttributeError:\n'
           '            # 老版本没有该方法时退回直接改配置\n'
           '            cfg = config.get()\n'
           '            for _s in (cfg.get("sources") or []):\n'
           '                if _s.get("id") == sid:\n'
           '                    _s["enabled"] = False\n'
           '            config.save(cfg)\n\n'
           '    return {"code": 0, "msg": "", "data": await _probe.start(\n'
           '        targets, concurrency=concurrency, disable_sink=_disable)}')
    if old not in s:
        raise SystemExit("admin_api sink 锚点未命中")
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    return "admin_api 禁用回调注入"


def main() -> None:
    for fn in (patch_providers, patch_admin_api, patch_source_probe,
               patch_sink):
        try:
            print("  [ok]", fn())
        except SystemExit as e:
            print("  [miss]", e)


if __name__ == "__main__":
    main()
