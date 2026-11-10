"""管理令牌加固：新令牌长度、老令牌兼容、轮换宽限、登录限流、端点。

只测本次引入的行为；配置一律打到 tmp_path，绝不碰仓库真实的 state/。
"""
import json

import pytest
from fastapi.testclient import TestClient

import admin_auth
import app as appmod
import config as configmod


def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(configmod, "_STATE", state)
    return state


def _write_state(state, data):
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(data), encoding="utf-8")


# ---------------- admin_auth：纯逻辑 ----------------

def test_new_token_is_256bit_hex():
    t = admin_auth.new_token()
    assert len(t) == 64
    int(t, 16)  # 纯 hex
    assert admin_auth.new_token() != t


def test_mask_and_fingerprint_never_leak_token():
    t = "a" * 64
    assert t not in admin_auth.mask_token(t)
    assert admin_auth.mask_token(t) == "aaaa…aaaa"
    fp = admin_auth.token_fingerprint(t)
    assert t not in fp and len(fp) == 16
    assert admin_auth.token_fingerprint(t) == fp  # 确定性
    assert admin_auth.token_fingerprint("b" * 64) != fp


def test_verify_current_and_wrong_and_empty():
    cfg = {"admin_token": "c" * 64, "admin_token_created_at": 1000}
    assert admin_auth.verify_token(cfg, "c" * 64, now=2000) == (True, "ok")
    assert admin_auth.verify_token(cfg, "d" * 64, now=2000) == (False, "invalid")
    assert admin_auth.verify_token(cfg, "", now=2000) == (False, "empty")
    assert admin_auth.verify_token({"admin_token": ""}, "x", now=2000) == (
        False, "no_token_configured")


def test_verify_legacy_16char_token_still_ok():
    # 老令牌 token_hex(8) = 16 hex 字符：不因长度被拒。
    cfg = {"admin_token": "ab12" * 4, "admin_token_created_at": 1000}
    assert admin_auth.verify_token(cfg, "ab12" * 4, now=2000) == (True, "ok")


def test_verify_expired_only_when_max_age_set_and_created_known():
    cfg = {"admin_token": "e" * 64, "admin_token_created_at": 1000,
           "admin_token_max_age_days": 1}
    assert admin_auth.verify_token(cfg, "e" * 64, now=1000 + 86399) == (True, "ok")
    assert admin_auth.verify_token(cfg, "e" * 64, now=1000 + 86400) == (
        False, "expired")
    # 默认 max_age_days=0：再老也不过期。
    cfg["admin_token_max_age_days"] = 0
    assert admin_auth.verify_token(cfg, "e" * 64, now=1000 + 365 * 86400) == (
        True, "ok")
    # 创建时间未知：即使配了 max_age 也不过期（老配置未打戳时不锁死）。
    cfg2 = {"admin_token": "e" * 64, "admin_token_created_at": 0,
            "admin_token_max_age_days": 1}
    assert admin_auth.verify_token(cfg2, "e" * 64, now=10 ** 9) == (True, "ok")


def test_token_info_age_and_expiry():
    cfg = {"admin_token": "f" * 64, "admin_token_created_at": 1000,
           "admin_token_max_age_days": 10, "admin_token_grace_hours": 24,
           "admin_token_previous": "a" * 16,
           "admin_token_previous_expires_at": 2000 + 3600}
    info = admin_auth.token_info(cfg, now=2000)
    assert info["length"] == 64 and info["bits"] == 256
    assert info["age_seconds"] == 1000
    assert info["expires_at"] == 1000 + 10 * 86400
    assert info["expired"] is False
    assert info["grace_remaining_seconds"] == 3600
    assert "f" * 64 not in json.dumps(info)


def test_grace_previous_token():
    cfg = {"admin_token": "a" * 64, "admin_token_created_at": 1000,
           "admin_token_previous": "b" * 16,
           "admin_token_previous_expires_at": 5000}
    assert admin_auth.verify_token(cfg, "b" * 16, now=4999) == (True, "grace")
    assert admin_auth.verify_token(cfg, "b" * 16, now=5001) == (False, "invalid")
    assert admin_auth.verify_token(cfg, "a" * 64, now=5001) == (True, "ok")


class _Clock:
    def __init__(self, t=10000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_limiter_locks_after_max_failures_then_unlocks():
    clock = _Clock()
    lim = admin_auth.LoginAttemptLimiter(max_failures=5, lockout_seconds=900,
                                         window_seconds=900, now_fn=clock)
    for _ in range(4):
        lim.record_failure("1.2.3.4", "wrong")
        assert lim.is_locked("1.2.3.4", "wrong") == (False, 0.0)
    lim.record_failure("1.2.3.4", "wrong")
    locked, rem = lim.is_locked("1.2.3.4", "wrong")
    assert locked and 0 < rem <= 900
    # 锁定中继续失败不延长锁定；成功登录也被挡在检查点之外（由调用方先查锁）。
    lim.record_failure("1.2.3.4", "wrong")
    locked2, rem2 = lim.is_locked("1.2.3.4", "wrong")
    assert locked2 and rem2 <= rem
    # 锁定期过后自动解锁，计数清零。
    clock.t += 901
    assert lim.is_locked("1.2.3.4", "wrong") == (False, 0.0)
    lim.record_failure("1.2.3.4", "wrong")
    assert lim.is_locked("1.2.3.4", "wrong") == (False, 0.0)


def test_limiter_success_resets_and_keys_are_independent():
    clock = _Clock()
    lim = admin_auth.LoginAttemptLimiter(max_failures=3, lockout_seconds=60,
                                         window_seconds=900, now_fn=clock)
    lim.record_failure("1.1.1.1", "tok-A")
    lim.record_failure("1.1.1.1", "tok-A")
    lim.record_success("1.1.1.1", "tok-A")
    lim.record_failure("1.1.1.1", "tok-A")
    assert lim.is_locked("1.1.1.1", "tok-A") == (False, 0.0)
    # 同一令牌从另一个 IP 猜也会被令牌键挡住（按 IP/令牌分别计数，任一命中即锁）；
    # 不同 IP + 不同令牌则完全不受影响。
    lim.record_failure("2.2.2.2", "tok-A")
    lim.record_failure("2.2.2.2", "tok-A")
    lim.record_failure("2.2.2.2", "tok-A")
    assert lim.is_locked("2.2.2.2", "tok-A")[0] is True
    assert lim.is_locked("1.1.1.1", "tok-A")[0] is True  # 同一令牌，令牌键已锁
    assert lim.is_locked("2.2.2.2", "tok-B")[0] is True  # 同一 IP，IP 键已锁
    assert lim.is_locked("3.3.3.3", "tok-B") == (False, 0.0)


def test_limiter_window_slides():
    clock = _Clock()
    lim = admin_auth.LoginAttemptLimiter(max_failures=3, lockout_seconds=60,
                                         window_seconds=100, now_fn=clock)
    lim.record_failure("9.9.9.9", "w")
    clock.t += 101  # 窗口外：计数重新起算，不直接锁。
    lim.record_failure("9.9.9.9", "w")
    assert lim.is_locked("9.9.9.9", "w") == (False, 0.0)


# ---------------- config：生成 / 兼容 / 轮换 ----------------

def test_fresh_config_generates_256bit_token_and_persists(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    c = configmod.Config()
    tok = c.get()["admin_token"]
    assert len(tok) == 64
    int(tok, 16)
    assert c.get()["admin_token_created_at"] > 0
    # 落盘了：重启（重新加载）后令牌不变。
    c2 = configmod.Config()
    assert c2.get()["admin_token"] == tok


def test_legacy_token_preserved_and_stamped(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    _write_state(state, {"admin_token": "ab12" * 4})
    c = configmod.Config()
    assert c.get()["admin_token"] == "ab12" * 4  # 原样保留，不重生成
    assert c.get()["admin_token_created_at"] > 0
    assert c.verify_admin_token("ab12" * 4) == (True, "ok")
    assert c.verify_admin_token("ab12" * 4 + "x") == (False, "invalid")


def test_legacy_created_at_not_overwritten(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    _write_state(state, {"admin_token": "cd34" * 4,
                         "admin_token_created_at": 1700000000})
    c = configmod.Config()
    assert c.get()["admin_token_created_at"] == 1700000000


def test_expired_previous_cleaned_on_load(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    _write_state(state, {"admin_token": "ef56" * 4,
                         "admin_token_created_at": 1700000000,
                         "admin_token_previous": "ab12" * 4,
                         "admin_token_previous_expires_at": 1700000001})
    c = configmod.Config()
    assert c.get()["admin_token_previous"] == ""
    assert c.get()["admin_token"] == "ef56" * 4  # 当前令牌不受影响


def test_rotate_grace_period(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = configmod.Config()
    old = c.get()["admin_token"]
    new, info = c.rotate_admin_token(now=2000000000)
    assert len(new) == 64 and new != old
    assert c.get()["admin_token"] == new
    assert c.get()["admin_token_previous"] == old
    assert c.get()["admin_token_previous_expires_at"] == 2000000000 + 24 * 3600
    assert c.verify_admin_token(old, now=2000000000 + 3600) == (True, "grace")
    assert c.verify_admin_token(new, now=2000000000 + 3600) == (True, "ok")
    # 宽限期过后旧令牌失效，新令牌仍可用。
    assert c.verify_admin_token(old, now=2000000000 + 25 * 3600) == (False, "invalid")
    assert c.verify_admin_token(new, now=2000000000 + 25 * 3600) == (True, "ok")
    assert info["grace_remaining_seconds"] == 24 * 3600
    assert new not in json.dumps(info)
    # 再轮换一次：上上个令牌彻底失效。
    new2, _ = c.rotate_admin_token(now=2000000000 + 3600)
    assert c.verify_admin_token(old, now=2000000000 + 7200) == (False, "invalid")
    assert c.verify_admin_token(new, now=2000000000 + 7200) == (True, "grace")
    assert c.verify_admin_token(new2, now=2000000000 + 7200) == (True, "ok")


def test_rotate_with_zero_grace_has_no_previous(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = configmod.Config()
    c.save({"admin_token_grace_hours": 0})
    old = c.get()["admin_token"]
    new, _ = c.rotate_admin_token(now=2000000000)
    assert c.get()["admin_token_previous"] == ""
    assert c.verify_admin_token(old, now=2000000000) == (False, "invalid")
    assert c.verify_admin_token(new, now=2000000000) == (True, "ok")


def test_max_age_expiry_via_config(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    _write_state(state, {"admin_token": "a1b2" * 4,
                         "admin_token_created_at": 1000000000,
                         "admin_token_max_age_days": 1})
    c = configmod.Config()
    assert c.verify_admin_token("a1b2" * 4, now=1000000000 + 86401) == (
        False, "expired")
    assert c.admin_token_info(now=1000000000 + 86401)["expired"] is True


# ---------------- app 端点 ----------------

def _fresh_env(tmp_path, monkeypatch):
    cfg_state = tmp_path / "config.json"
    monkeypatch.setattr(configmod, "_STATE", cfg_state)
    cfg = configmod.Config()
    monkeypatch.setattr(appmod, "config", cfg)
    monkeypatch.setattr(configmod, "config", cfg)
    monkeypatch.setattr(appmod, "_ADMIN_LOGIN_LIMITER",
                        admin_auth.LoginAttemptLimiter())
    monkeypatch.setattr(appmod, "_schedule_routes_refresh", lambda: None)
    return cfg


def test_admin_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    assert "/v/api/ext/admin/token" in paths
    assert "/v/api/ext/admin/login" in paths
    assert "/v/api/ext/admin/token/rotate" in paths


def test_admin_token_status_never_returns_raw_token(tmp_path, monkeypatch):
    cfg = _fresh_env(tmp_path, monkeypatch)
    raw = cfg.get()["admin_token"]
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.get("/v/api/ext/admin/token")
    assert r.status_code == 200
    assert r.json()["code"] == 0
    body = json.dumps(r.json())
    assert raw not in body
    assert r.json()["data"]["length"] == 64
    assert r.json()["data"]["masked"].startswith(raw[:4])


def test_admin_login_ok_wrong_and_lockout(tmp_path, monkeypatch):
    cfg = _fresh_env(tmp_path, monkeypatch)
    raw = cfg.get()["admin_token"]
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/admin/login", json={"token": raw})
        assert r.status_code == 200 and r.json()["code"] == 0
        assert r.json()["data"]["via"] == "ok"
        # 5 次失败：第 5 次已触发锁定（401 带锁定提示），第 6 次直接 429。
        for _ in range(5):
            bad = cli.post("/v/api/ext/admin/login", json={"token": "0" * 64})
            assert bad.status_code == 401
        locked = cli.post("/v/api/ext/admin/login", json={"token": "0" * 64})
        assert locked.status_code == 429
        assert locked.json()["code"] == 429
        # 锁定期间正确令牌也被挡住（检查点先查锁）。
        good_while_locked = cli.post("/v/api/ext/admin/login",
                                     json={"token": raw})
        assert good_while_locked.status_code == 429


def test_admin_login_success_resets_failures(tmp_path, monkeypatch):
    cfg = _fresh_env(tmp_path, monkeypatch)
    raw = cfg.get()["admin_token"]
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        for _ in range(4):
            assert cli.post("/v/api/ext/admin/login",
                            json={"token": "0" * 64}).status_code == 401
        assert cli.post("/v/api/ext/admin/login",
                        json={"token": raw}).status_code == 200
        # 成功清零：再错 4 次仍不锁定。
        for _ in range(4):
            assert cli.post("/v/api/ext/admin/login",
                            json={"token": "0" * 64}).status_code == 401
        assert cli.post("/v/api/ext/admin/login",
                        json={"token": raw}).status_code == 200


def test_admin_rotate_requires_confirm_and_old_token_in_grace(
        tmp_path, monkeypatch):
    cfg = _fresh_env(tmp_path, monkeypatch)
    old = cfg.get()["admin_token"]
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        assert cli.post("/v/api/ext/admin/token/rotate",
                        json={}).status_code == 400
        r = cli.post("/v/api/ext/admin/token/rotate", json={"confirm": True})
        assert r.status_code == 200 and r.json()["code"] == 0
        new = r.json()["data"]["new_token"]
        assert len(new) == 64 and new != old
        # 轮换后旧令牌（宽限期内）与新令牌都能登录。
        assert cli.post("/v/api/ext/admin/login",
                        json={"token": old}).status_code == 200
        assert cli.post("/v/api/ext/admin/login",
                        json={"token": new}).json()["data"]["via"] == "ok"
        # 状态接口不再返回新令牌原文。
        assert new not in json.dumps(cli.get("/v/api/ext/admin/token").json())
