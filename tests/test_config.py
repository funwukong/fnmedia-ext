"""配置：默认值、废弃键、备份与损坏恢复。

Config 的 _STATE 是模块级路径，测试用 monkeypatch 把它指到 tmp_path，
绝不碰仓库真实的 state/。
"""
import json

import config as config_mod


def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


def test_defaults_load_without_file(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    cfg = c.get()
    assert cfg["config_version"] == config_mod.CONFIG_VERSION
    assert cfg["hot_library_enabled"] is True
    assert cfg["admin_token"]  # 首次加载自动生成
    assert len(cfg["admin_token"]) >= 16
    assert any(s["id"] == "baofeng" for s in cfg["sources"])


def test_deprecated_keys_removed(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"public_base": "/x", "strm_root": "/y",
                                 "t4_sources": [{"name": "旧T4", "api": "http://x/"}]}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert "public_base" not in c.get()
    assert "strm_root" not in c.get()
    # 遗留键 t4_sources：T4 源早已由 import_t4_sources() 写入 sources（kind="t4"），
    # 旧配置里残留的该键加载时丢弃、不报错，且不再出现在默认值里。
    assert "t4_sources" not in c.get()
    assert "t4_sources" not in config_mod.DEFAULTS


def test_save_backups_old_config(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    c.save({"hot_limit": 111})
    c.save({"hot_limit": 222})
    backups = sorted((tmp_path / "state" / "backups").glob("config-*.json"))
    assert len(backups) >= 1
    assert json.loads(backups[-1].read_text(encoding="utf-8"))["hot_limit"] in (111, 222, 300)
    assert c.get()["hot_limit"] == 222


def test_corrupt_config_restored_from_backup(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    c.save({"hot_limit": 123})
    # 主配置写坏，备份里有 hot_limit=123。
    state.write_text("{not json", encoding="utf-8")
    c2 = config_mod.Config()
    assert c2.get()["hot_limit"] == 123
    assert c2._restored_from_backup is True


def test_corrupt_config_no_backup_falls_back_to_defaults(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text("{not json", encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["hot_limit"] == config_mod.DEFAULTS["hot_limit"]
    assert c._restored_from_backup is False


def test_add_source_rejects_bad_api(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    ok, msg = c.add_source("x", "ftp://example.com/api")
    assert ok is False
