"""F 组第 5 项：config 白名单全量分类 + 布尔归一化覆盖。

- 每个 config DEFAULTS 键（74 个）必须恰好属于一类：
  WHITELIST（ext_set_config 的 allowed 集，可写）、
  DEDICATED（有专用端点/专用方法写）、
  NOT_WRITABLE（故意不可写：内部版本/元数据/无管理端点的只读默认）。
- 白名单里 DEFAULTS 值为 bool 的键，在 ext_set_config 内必须有
  ``patch["k"] = bool(...)`` 归一化（否则 "false"/0/"" 等原样落盘）。
- 白名单集合本身从 admin_api.py 源码 AST 提取，不在测试里手抄一份
  假集合对照——改了源码白名单却不改测试分类，测试必须失败。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import admin_auth
import app as appmod
import config as config_mod

ADMIN_API = Path(__file__).resolve().parent.parent / "proxy" / "admin_api.py"

# ---------------- 分类表（调查组核实，74 键全量） ----------------

WHITELIST = {
    "max_per_keyword", "filter_junk", "junk_keywords", "online_first",
    "hot_library_enabled", "hot_library_title", "hot_limit",
    "hot_per_source", "hot_pages_per_source", "hot_refresh_interval",
    "catalog_library_enabled", "t4_library_enabled",
    "welfare_library_enabled", "prefer_quality_source",
    "line_fastest", "line_fastest_max", "line_fastest_timeout",
    "line_fastest_prefer", "line_autofail", "fixed_line_direct",
    "linepredict_zero_race", "mid_playback_switch", "browse_probe",
    "poster_speed_badge", "main_ready_budget", "prewarm_mode",
    "pan_cache_cleanup_enabled", "pan_cache_ttl_hours", "update_proxy",
    "pan_lib_enabled", "guard_zone_enabled", "guard_native_enabled",
}

DEDICATED = {
    # ext_set_config 内 body["sources"] 专用分支处理
    "sources",
    # remove_source / restore_builtin 内部维护
    "removed_ids",
    # POST /v/api/ext/linepredict/config
    "linepredict_enabled", "linepredict_max_lines", "linepredict_ttl_min",
    # POST /v/api/ext/source/priority
    "source_priority",
    # POST /v/api/ext/lib/pin
    "pinned_libs",
    # POST /v/api/ext/lib/visibility（vlibs.set_hidden）
    "lib_hidden",
    # POST /v/api/ext/lib/order（vlibs.set_order）
    "lib_order", "lib_placement", "lib_anchor",
    # POST /v/api/ext/account/acl（vlibs.set_account_acl）
    "lib_acl",
    # POST /v/api/ext/account/defaults
    "acl_default_visible", "acl_default_online",
    # POST /v/api/ext/subscription-proxy
    "subscription_proxy",
    # POST /v/api/ext/source-proxy
    "source_proxy", "source_proxy_mode",
    # POST /v/api/ext/adfilter/toggle（ad_filter_enabled 经 adfilter.set_enabled）
    "ad_filter_enabled", "ad_filter_dynamic", "ad_filter_sps",
    # POST /v/api/ext/admin/token/rotate（令牌原文只走轮换路径）
    "admin_token",
}

NOT_WRITABLE = {
    # 内部迁移版本，不可由管理端改写
    "config_version",
    # 令牌元数据：轮换/校验逻辑内部维护，无管理端写入口
    "admin_token_created_at", "admin_token_previous",
    "admin_token_previous_expires_at", "admin_token_grace_hours",
    "admin_token_max_age_days",
    # 登录限流阈值：无管理端写入口，改动需改代码/默认值
    "admin_auth_max_failures", "admin_auth_lockout_seconds",
    "admin_auth_window_seconds",
    # P2 功能开关：只有读端门控，无管理端写入口（默认即策略）
    "source_health_enabled", "domain_heal_enabled", "auto_best_source",
    "follow_enabled", "metadata_enabled", "related_enabled",
    # 域名自愈备用表：只有 heal 端点读取，无管理端写入口
    "source_domain_backups",
    # 红果短剧本地服务开关/地址：无管理端写入口
    "hgdj_enabled", "hgdj_url",
    # 标题缓存性能调参：无管理端写入口，改动需改代码/默认值
    "titles_cache_max_entries", "titles_cache_max_mb",
    "titles_flush_debounce",
}


def _extract_allowed_and_boolnorm() -> tuple[set[str], set[str]]:
    """从 ext_set_config 源码 AST 提取 allowed 集与 bool() 归一化键。"""
    tree = ast.parse(ADMIN_API.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "ext_set_config")
    allowed: set[str] = set()
    boolnorm: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if (isinstance(tgt, ast.Name) and tgt.id == "allowed"
                    and isinstance(node.value, ast.Set)):
                allowed |= {e.value for e in node.value.elts
                            if isinstance(e, ast.Constant)}
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "patch"
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "bool"):
                boolnorm.add(tgt.slice.value)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "allowed"):
                if call.func.attr == "add" and call.args \
                        and isinstance(call.args[0], ast.Constant):
                    allowed.add(call.args[0].value)
                elif call.func.attr == "update" and call.args \
                        and isinstance(call.args[0], ast.Set):
                    allowed |= {e.value for e in call.args[0].elts
                                if isinstance(e, ast.Constant)}
    return allowed, boolnorm


def test_defaults_fully_classified():
    keys = set(config_mod.DEFAULTS)
    assert len(keys) == 74
    assert WHITELIST & DEDICATED == set()
    assert WHITELIST & NOT_WRITABLE == set()
    assert DEDICATED & NOT_WRITABLE == set()
    assert WHITELIST | DEDICATED | NOT_WRITABLE == keys, (
        "未分类的键: %s；多余的键: %s"
        % (sorted(keys - WHITELIST - DEDICATED - NOT_WRITABLE),
           sorted(WHITELIST | DEDICATED | NOT_WRITABLE - keys)))


def test_whitelist_matches_source():
    allowed, _ = _extract_allowed_and_boolnorm()
    assert allowed == WHITELIST, (
        "源码白名单与分类表不一致：源码多 %s，分类表多 %s"
        % (sorted(allowed - WHITELIST), sorted(WHITELIST - allowed)))


def test_every_whitelist_bool_key_is_normalized():
    _, boolnorm = _extract_allowed_and_boolnorm()
    bool_keys = {k for k in WHITELIST
                 if isinstance(config_mod.DEFAULTS[k], bool)}
    assert bool_keys, "白名单里没有布尔键？分类表可能错了"
    assert bool_keys <= boolnorm, (
        "缺少 bool() 归一化的白名单布尔键: %s" % sorted(bool_keys - boolnorm))


# ---------------- 功能：布尔归一化真实落盘 ----------------

def _fresh_env(tmp_path, monkeypatch):
    cfg_state = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", cfg_state)
    cfg = config_mod.Config()
    monkeypatch.setattr(appmod, "config", cfg)
    monkeypatch.setattr(config_mod, "config", cfg)
    monkeypatch.setattr(appmod, "_ADMIN_LOGIN_LIMITER",
                        admin_auth.LoginAttemptLimiter())
    monkeypatch.setattr(appmod, "_schedule_routes_refresh", lambda: None)
    return cfg


@pytest.mark.parametrize("key", sorted(
    k for k in WHITELIST if isinstance(config_mod.DEFAULTS[k], bool)))
def test_whitelist_bool_key_normalized_end_to_end(
        tmp_path, monkeypatch, key):
    """0 / "" / 1 等非 bool 输入经 POST /v/api/ext/config 后必须存为严格 bool。"""
    cfg = _fresh_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        for raw, want in ((0, False), ("", False), (1, True)):
            r = cli.post("/v/api/ext/config", json={key: raw})
            assert r.status_code == 200 and r.json()["code"] == 0, (key, raw)
            got = cfg.get()[key]
            assert got is want, (key, raw, got)


def test_guard_zone_keys_bool_normalized(tmp_path, monkeypatch):
    """第 4 项点名：guard_zone_enabled / guard_native_enabled 两键。"""
    cfg = _fresh_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/config",
                     json={"guard_zone_enabled": 0,
                           "guard_native_enabled": ""})
        assert r.status_code == 200 and r.json()["code"] == 0
    assert cfg.get()["guard_zone_enabled"] is False
    assert cfg.get()["guard_native_enabled"] is False


def test_non_whitelist_key_is_ignored(tmp_path, monkeypatch):
    """白名单外的键即使 POST 也不落盘（分类表 NOT_WRITABLE/DEDICATED 的约束）。"""
    cfg = _fresh_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/config",
                     json={"source_health_enabled": False,
                           "config_version": 99})
        assert r.status_code == 200 and r.json()["code"] == 0
    assert cfg.get()["source_health_enabled"] is True
    assert cfg.get()["config_version"] == config_mod.CONFIG_VERSION
