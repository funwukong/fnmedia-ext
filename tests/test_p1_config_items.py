"""P1 配置项 10、11：main_ready_budget + CONFIG_VERSION 迁移。

- DEFAULTS 含 main_ready_budget=2.4；旧配置加载后自动出现该键；
  每次读取夹取 [0.8, 2.8]，非法值回落 2.4（POST 夹取见 test_slow_line_budget）。
- 第 11 项：版本号稳健解析；非数字不再误判为 v1 翻掉用户显式 True；
  降级警告；迁移跑与不跑都记日志。
"""
from __future__ import annotations

import json
import logging

import config as config_mod


def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


# ---------------- 第 10 项：main_ready_budget ----------------

def test_main_ready_budget_in_defaults():
    assert config_mod.DEFAULTS["main_ready_budget"] == 2.4
    assert config_mod.MAIN_READY_BUDGET_DEFAULT == 2.4


def test_old_config_gets_default_budget_on_load(monkeypatch, tmp_path):
    # 旧配置没有该键：加载是 DEFAULTS 深拷贝 + saved update，自动出现 2.4，无需迁移。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2, "hot_limit": 300}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["main_ready_budget"] == 2.4
    assert c.main_ready_budget() == 2.4


def test_main_ready_budget_clamp_matrix(monkeypatch, tmp_path):
    # 每次读取时夹取 [0.8, 2.8]，非法值回落 2.4。
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    for raw, want in ((99, 2.8), (0.1, 0.8), ("junk", 2.4), (1.5, 1.5),
                      (None, 2.4), (float("nan"), 2.4), (2.8, 2.8), (0.8, 0.8)):
        c.save({"main_ready_budget": raw})
        assert c.main_ready_budget() == want, raw


# ---------------- 第 11 项：CONFIG_VERSION 迁移 ----------------

def test_parse_config_version_matrix():
    parse = config_mod.parse_config_version
    assert parse(1) == 1
    assert parse(2) == 2
    assert parse(2.0) == 2
    assert parse("2") == 2
    assert parse("2.0") == 2
    assert parse(" 2 ") == 2
    assert parse(3) == 3
    assert parse("abc") is None
    assert parse(None) is None
    assert parse(2.5) is None
    assert parse("2.5") is None
    assert parse(True) is None
    assert parse("") is None
    assert parse([]) is None


def test_version_matrix_keeps_explicit_true(monkeypatch, tmp_path):
    # 缺失/v1/v2/>2/非数字：只有缺失与 v1 跑迁移翻 False；其余一律保留用户显式 True。
    cases = [
        # (存量 config_version, 是否跑迁移翻 False)
        (1, True),
        (2, False),
        (3, False),
        ("3", False),
        ("2.0", False),
        (2.0, False),
        ("2", False),
        ("abc", False),
        (None, False),
        (2.5, False),
        ("2.5", False),
    ]
    for raw, flipped in cases:
        state = _patch_state(monkeypatch, tmp_path)
        state.parent.mkdir(parents=True, exist_ok=True)
        if state.exists():
            state.unlink()
        state.write_text(json.dumps({"config_version": raw,
                                     "acl_default_visible": True,
                                     "acl_default_online": True}),
                         encoding="utf-8")
        c = config_mod.Config()
        assert c.get()["acl_default_visible"] is (not flipped), raw
        assert c.get()["acl_default_online"] is (not flipped), raw


def test_version_missing_still_migrates(monkeypatch, tmp_path):
    # 没有 config_version 键的更旧配置视为 v1，迁移照常跑。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is False
    assert c.get()["acl_default_online"] is False
    assert c.get()["config_version"] == 2


def test_version_unparsable_warns_and_normalizes(monkeypatch, tmp_path, caplog):
    # 非数字版本：记 warning、不翻用户值、版本号归一化为 2 并落盘。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": "abc",
                                 "acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fnmedia.config"):
        c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 2
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["config_version"] == 2
    assert saved["acl_default_visible"] is True
    assert "unparsable" in caplog.text


def test_version_downgrade_warns_and_keeps_values(monkeypatch, tmp_path, caplog):
    # 存量版本 > 当前 CONFIG_VERSION：记 warning，不迁移、不降版本号、值原样保留。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 3,
                                 "acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fnmedia.config"):
        c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3
    assert "newer than supported" in caplog.text


def test_migration_logs_when_run_and_when_skipped(monkeypatch, tmp_path, caplog):
    # 迁移跑与不跑都记一条日志。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 1,
                                 "acl_default_visible": True}),
                     encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="fnmedia.config"):
        config_mod.Config()
    assert "v1->v2" in caplog.text
    caplog.clear()
    state.write_text(json.dumps({"config_version": 2,
                                 "acl_default_visible": True}),
                     encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="fnmedia.config"):
        config_mod.Config()
    assert "no migration needed" in caplog.text


