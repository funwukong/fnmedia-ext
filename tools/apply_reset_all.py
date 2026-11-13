"""第 6 项：一键重置 —— 清空所有设置 + 资源站列表（可恢复内置）。

config.reset_all()：
  * 配置回到 DEFAULTS（内置源列表 + 默认开关 + 默认关键词）；
  * removed_ids 清空（内置源不再被标记删除）；
  * 已导入的自定义源全部移除（sources 只剩内置）。

保留：管理令牌等安全字段不被清（否则管理员会被锁在门外）。
可在「资源站」用「恢复内置」找回内置源。
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


# ---------- config：reset_all ----------
edit("proxy/config.py",
     """    def restore_builtin(self) -> tuple[bool, str]:""",
     """    def reset_all(self) -> tuple[bool, str]:
        \"\"\"一键重置：所有设置回到默认，资源站列表清空（只剩内置源）。

        保留管理令牌相关字段（admin_token 等），否则重置后管理员进不来。
        \"\"\"
        with _LOCK:
            cfg = dict(DEFAULTS)
            cfg["sources"] = [dict(s) for s in DEFAULT_SOURCES]
            cfg["removed_ids"] = []
            old = self.get()
            for k in ("admin_token", "admin_token_created_at",
                      "admin_token_previous",
                      "admin_token_previous_expires_at"):
                if k in old:
                    cfg[k] = old[k]
            self._persist(cfg)
            self.load()
            return True, "ok"

    def restore_builtin(self) -> tuple[bool, str]:""",
     "config.reset_all")

# ---------- admin_api：重置端点 ----------
edit("proxy/admin_api.py",
     '@admin_router.post("/v/api/ext/config")',
     '''@admin_router.post("/v/api/ext/config/reset")
async def ext_reset_config():
    """一键重置：清空所有设置与资源站列表（内置源可用「恢复内置」找回）。"""
    from app import app, config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        ok, msg = config.reset_all()
    except Exception as exc:  # noqa: BLE001
        _log.debug("config reset failed", exc_info=True)
        return JSONResponse({"code": 500, "msg": str(exc)[:200]})
    if not ok:
        return JSONResponse({"code": 500, "msg": msg})
    return JSONResponse({"code": 0, "msg": "", "data": {"reset": True}})


@admin_router.post("/v/api/ext/config")''',
     "admin_api config reset")

# ---------- admin_api：允许写 pan_cache_ttl_days ----------
edit("proxy/admin_api.py",
     '        "pan_cache_cleanup_enabled", "pan_cache_ttl_hours",\n',
     '        "pan_cache_cleanup_enabled", "pan_cache_ttl_hours",\n'
     '        "pan_cache_ttl_days",\n',
     "admin_api allowed pan_cache_ttl_days")

print()
print("done:", ", ".join(done) if done else "(none)")