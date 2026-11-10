"""P1 配置项 10：main_ready_budget 进 DEFAULTS + 实时读取。

- DEFAULTS 含 main_ready_budget=2.4；旧配置加载后自动出现该键；
  每次读取夹取 [0.8, 2.8]，非法值回落 2.4（POST 夹取见 test_slow_line_budget）。
"""
from __future__ import annotations

import json

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


