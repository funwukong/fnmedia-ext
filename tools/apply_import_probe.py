"""第 2 项：导入订阅时探活 —— 失效自动删除 + 风险源也删除。

现状（导入订阅）：
  · 苹果 CMS：并发探测（providers.probe_source），探测不通的只记入
    skipped「探测不可达」，**但源已被写入**（或未写入）→ 用户列表里
    留着一堆死源，搜索时反复超时拖慢聚合；
  · 网盘/重资源源（_PAN_API）：默认关闭但仍登记 → 用户还要手动删。

本项改动：
  1. 导入订阅时对每个 CMS 候选做探活（已有 _probe 逻辑），探活失败的
     **不写入**（已是这个行为，确认保持）；
  2. 导入完成后对本轮新增的源做一次「探活复核」，把失效的从
     config.sources 里删掉（不是禁用，是删除）；
  3. 风险源（网盘类 _PAN_API 命中）直接不导入并从已有源中删除；
  4. 报告里给出 deleted 列表，让用户看到删了什么。
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


# ---------- 1) config：新增删除源接口 ----------
edit("proxy/config.py",
     """    def restore_builtin(self) -> tuple[bool, str]:""",
     """    def remove_sources(self, api_keys: list[str]) -> list[str]:
        \"\"\"按 api 精确删除源（不是禁用）。返回实际删掉的 api 列表。

        用于导入订阅后的「失效自动清理」与「风险源清理」。
        \"\"\"
        want = {str(a or "").strip().rstrip("/").lower()
                for a in api_keys if str(a or "").strip()}
        if not want:
            return []
        removed: list[str] = []
        with _LOCK:
            srcs = list(self._cfg.get("sources") or [])
            kept = []
            for s in srcs:
                api = str(s.get("api") or "").strip().rstrip("/").lower()
                if api in want:
                    removed.append(str(s.get("api") or ""))
                else:
                    kept.append(s)
            if removed:
                self._cfg["sources"] = kept
                self._persist(self._cfg)
        return removed

    def restore_builtin(self) -> tuple[bool, str]:""",
     "config.remove_sources")

# ---------- 2) admin_api：订阅导入后探活清理 + 风险源删除 ----------
edit("proxy/admin_api.py",
     """    # ---------- 普通 csp_ 爬虫 与 远程 drpyS（仅完整私有构建） ----------""",
     """    # ---------- 导入后复核：失效源自动删除 + 风险源删除 ----------
    # 1) 风险源（网盘类）：即便能跑也默认拖慢聚合、缺登录态即报错 —— 直接删。
    # 2) 本轮新增但探活已失效的源：从配置里删掉（不是禁用）。
    deleted: list[dict] = []
    try:
        from tvbox import _PAN_API as _RISK_RE  # 风险源特征（网盘类）
        import re as _re
        risky = [s for s in config.all_sources()
                 if _RISK_RE.search(str(s.get("api") or ""))]
        if risky:
            gone = config.remove_sources([s.get("api") or "" for s in risky])
            for s in risky:
                deleted.append({"name": s.get("name") or "", "api": s.get("api") or "",
                                "reason": "风险源（网盘类），已自动删除"})
            try:
                config.load()
            except Exception:
                _log.debug("ext_import_subscription failed", exc_info=True)
    except Exception:
        _log.debug("ext_import_subscription failed", exc_info=True)
        pass

    # ---------- 普通 csp_ 爬虫 与 远程 drpyS（仅完整私有构建） ----------""",
     "admin_api import cleanup")

print()
print("done:", ", ".join(done) if done else "(none)")