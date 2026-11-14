"""把 PanSou 的地址/参数从硬编码改为配置化（对齐上游 pansou.py）。

上游完整版（/var/minis/workspace/fnmedia-ext-upstream-full/proxy/pansou.py）
的配置键：
    pansou_enabled        开关（默认关）
    pansou_url            自托管实例地址
    pansou_cloud_types    网盘类型（逗号分隔）
    pansou_src            数据来源：all/tg/plugin
    pansou_timeout        单次搜索超时（默认 12s）
    pansou_limit          结果条数上限（默认 30）

我们之前把地址硬编码成 bus.wulii.eu.cc —— 该实例已不可达（连接被重置），
必须改成可配置，否则整个网盘搜索路线不可用。
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


# ---------- 1) config：新增 PanSou 配置项 ----------
edit("proxy/config.py",
     '    "pan_cache_ttl_days": 0,\n',
     '    "pan_cache_ttl_days": 0,\n'
     '    # PanSou 网盘搜索（自托管实例；上游同款配置键）\n'
     '    "pansou_enabled": True,\n'
     '    "pansou_url": "https://bus.wulii.eu.cc",\n'
     '    "pansou_cloud_types": "quark,uc,aliyun",\n'
     '    "pansou_src": "all",\n'
     '    "pansou_timeout": 30.0,\n'
     '    "pansou_limit": 30,\n',
     "config pansou keys")

# ---------- 2) pansou.py：地址等走配置 ----------
edit("proxy/pansou.py",
     '''# 自托管 PanSou 搜索实例（用户 2026-11-12 提供并令接线；默认值硬编码，
# 本批不新增配置项）。auth_enabled=false，无需 Authorization。
_PANSOU_SEARCH_BASE = "https://bus.wulii.eu.cc"
_PANSOU_SEARCH_TIMEOUT = 60.0   # 单次搜索超时（秒；实例冷启动曾 30 秒超时）
_PANSOU_SEARCH_PER_TYPE_MAX = 6  # 每盘最多取几条
_PANSOU_SEARCH_TOTAL_MAX = 12   # 搜索结果总数上限''',
     '''# 自托管 PanSou 搜索实例：地址/云类型/超时/条数全部走配置
# （上游 pansou.py 同款配置键）。默认地址仅作兜底，实例不可达时用户可在
# 管理页「网盘登录」里换成自己的实例。
_DEFAULT_PANSOU_URL = "https://bus.wulii.eu.cc"


def _pansou_cfg(key: str, default):
    try:
        import config as _cfg
        v = _cfg.config.get().get(key, default)
    except Exception:
        v = default
    return v


def _pansou_base_url() -> str:
    u = str(_pansou_cfg("pansou_url", _DEFAULT_PANSOU_URL) or "").strip()
    return u.rstrip("/")


def _pansou_timeout() -> float:
    try:
        return max(3.0, float(_pansou_cfg("pansou_timeout", 30.0)))
    except (TypeError, ValueError):
        return 30.0


def _pansou_limit() -> int:
    try:
        return max(1, min(80, int(_pansou_cfg("pansou_limit", 30))))
    except (TypeError, ValueError):
        return 30


def _pansou_cloud_types() -> list[str]:
    raw = str(_pansou_cfg("pansou_cloud_types", "quark,uc,aliyun") or "").strip()
    if not raw:
        return ["quark", "uc", "aliyun"]
    out: list[str] = []
    for t in raw.replace("，", ",").split(","):
        t = t.strip().lower()
        if t in ("quark", "uc", "aliyun") and t not in out:
            out.append(t)
    return out or ["quark", "uc", "aliyun"]


def _pansou_src() -> str:
    s = str(_pansou_cfg("pansou_src", "all") or "all").strip().lower()
    return s if s in ("all", "tg", "plugin") else "all"


_PANSOU_SEARCH_PER_TYPE_MAX = 6  # 每盘最多取几条
_PANSOU_SEARCH_TOTAL_MAX = 12   # 搜索结果总数上限''',
     "pansou config accessors")

print()
print("done:", ", ".join(done) if done else "(none)")