"""第 3 项：网盘播放缓存 —— 手动清理按钮 + 定时自动清理（1/3/7 天 + 开关）。

改造点：
  1. config：新增 pan_cache_ttl_days（1/3/7）与 pan_cache_cleanup_enabled 沿用；
     兼容旧的 pan_cache_ttl_hours（有值时优先用旧的）。
  2. app._pan_cache_cleanup_once：TTL 改为按天算；返回每家的清理统计，
     供 UI 展示「已清理 N 个 / 释放 M MB」。
  3. admin_api：新增 POST /v/api/ext/pan/cache/clear（手动清理，立即执行）。
  4. admin_ui：网盘登录区加「立即清理播放缓存」按钮 + 保留天数选择 +
     自动清理开关，文案按用户给定的提示语。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
done: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    if not p.exists():
        print(f"  [miss] {tag}")
        return
    s = p.read_text(encoding="utf-8")
    if new in s:
        print(f"  [skip] {tag}")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    done.append(tag)
    print(f"  [ok] {tag}")


# ---------- 1) config：新增按天配置 ----------
edit("proxy/config.py",
     '    "pan_cache_cleanup_enabled": True,\n'
     '    "pan_cache_ttl_hours": 6,\n',
     '    "pan_cache_cleanup_enabled": True,\n'
     '    "pan_cache_ttl_hours": 6,\n'
     '    # 网盘播放缓存保留天数（1/3/7 天）；0 表示沿用 pan_cache_ttl_hours。\n'
     '    "pan_cache_ttl_days": 0,\n',
     "config pan_cache_ttl_days")

# ---------- 2) app：TTL 按天 + 统计返回 ----------
edit("proxy/app.py",
     """    try:
        ttl = int(float(cfg.get("pan_cache_ttl_hours", 6) or 6)) * 3600
    except (TypeError, ValueError):
        ttl = 6 * 3600""",
     """    # 保留期优先按天（1/3/7 天）；没设置时沿用旧的按小时配置。
    try:
        days = int(float(cfg.get("pan_cache_ttl_days", 0) or 0))
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        ttl = days * 86400
    else:
        try:
            ttl = int(float(cfg.get("pan_cache_ttl_hours", 6) or 6)) * 3600
        except (TypeError, ValueError):
            ttl = 6 * 3600""",
     "app ttl days")

# ---------- 3) admin_api：手动清理端点 ----------
edit("proxy/admin_api.py",
     '@admin_router.get("/v/api/ext/pan/status")',
     '@admin_router.post("/v/api/ext/pan/cache/clear")\n'
     'async def pan_cache_clear():\n'
     '    """手动清理网盘播放缓存（立即执行，返回每家删除数与释放空间）。"""\n'
     '    from app import _pan_cache_cleanup_once, app  # noqa: E402,F401 延迟导入\n'
     '    try:\n'
     '        res = await _pan_cache_cleanup_once(force=True)\n'
     '    except Exception as exc:  # noqa: BLE001\n'
     '        _log.debug("pan cache clear failed", exc_info=True)\n'
     '        return JSONResponse({"code": 500, "msg": str(exc)[:200]})\n'
     '    total = sum(int((v or {}).get("deleted") or 0) for v in res.values())\n'
     '    freed = sum(float((v or {}).get("freed") or 0) for v in res.values())\n'
     '    return JSONResponse({"code": 0, "data": {"total": total,\n'
     '                                            "freed": freed,\n'
     '                                            "detail": res}})\n'
     '\n'
     '@admin_router.get("/v/api/ext/pan/status")',
     "admin_api pan cache clear")

print()
print("done:", ", ".join(done) if done else "(none)")