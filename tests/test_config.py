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
    # fail-open 默认：未配置账号默认能看到虚拟库、能在线搜索
    assert cfg["acl_default_visible"] is True
    assert cfg["acl_default_online"] is True
    assert cfg["admin_token"]  # 首次加载自动生成
    assert len(cfg["admin_token"]) == 64  # token_hex(32) = 256 bit
    assert cfg["admin_token_created_at"] > 0
    assert any(s["id"] == "baofeng" for s in cfg["sources"])


def test_first_install_persists_default_config(monkeypatch, tmp_path):
    # 上游 v0.2.45 / issue #39：干净安装首次加载即把默认配置落盘，
    # 否则 status/管理页等依赖“文件存在”的路径会异常。
    state = _patch_state(monkeypatch, tmp_path)
    assert not state.exists()
    config_mod.Config()
    assert state.exists()
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["config_version"] == config_mod.CONFIG_VERSION
    assert saved["admin_token"]


def test_deprecated_keys_removed(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"public_base": "/x", "strm_root": "/y",
                                 "tmdb_api_key": "deadbeef",
                                 "t4_sources": [{"name": "旧T4", "api": "http://x/"}]}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert "public_base" not in c.get()
    assert "strm_root" not in c.get()
    # 遗留键 t4_sources：T4 源早已由 import_t4_sources() 写入 sources（kind="t4"），
    # 旧配置里残留的该键加载时丢弃、不报错，且不再出现在默认值里。
    assert "t4_sources" not in c.get()
    assert "t4_sources" not in config_mod.DEFAULTS
    # 遗留键 tmdb_api_key：从未被任何代码读取（全仓库仅 DEFAULTS 与注释引用），
    # 旧配置里残留的该键加载时丢弃、不报错，且不再出现在默认值里。
    assert "tmdb_api_key" not in c.get()
    assert "tmdb_api_key" not in config_mod.DEFAULTS


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


def test_acl_defaults_fail_open_migration_v2(monkeypatch, tmp_path):
    # v2 存量配置（两项 False，即真机跑过 v1→v2 的用户 NAS 现状）：
    # 首次加载经 v2→v3 一次性翻回 True，并写回版本 3。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2,
                                 "acl_default_visible": False,
                                 "acl_default_online": False}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["acl_default_visible"] is True
    assert saved["acl_default_online"] is True
    assert saved["config_version"] == 3
    # 迁移后手动改回 False：重载后保持 False（迁移只跑一次）。
    c.save({"acl_default_visible": False, "acl_default_online": False})
    c2 = config_mod.Config()
    assert c2.get()["acl_default_visible"] is False
    assert c2.get()["acl_default_online"] is False
    assert c2.get()["config_version"] == 3


def test_acl_defaults_migration_v1_full_chain(monkeypatch, tmp_path):
    # v1 旧配置（两项 True）：先走 v1→v2（翻 False），再走 v2→v3（翻回
    # True），完整链最终为 True，版本 3。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 1,
                                 "acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["acl_default_visible"] is True
    assert saved["acl_default_online"] is True
    assert saved["config_version"] == 3


def test_acl_defaults_migration_without_version_key(monkeypatch, tmp_path):
    # 没有 config_version 键的更旧配置同样视为 v1，走完整链最终 True。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3


def test_acl_defaults_v2_config_with_true_kept(monkeypatch, tmp_path):
    # 已是 v2 且两项是 True 的配置：v2→v3 仍翻为 True（幂等），版本 3。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2,
                                 "acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3


def test_parse_config_version():
    parse = config_mod.parse_config_version
    assert parse(2) == 2
    assert parse(3) == 3
    assert parse(2.0) == 2
    assert parse("2") == 2
    assert parse("2.0") == 2
    assert parse(" 3 ") == 3
    # 无法确定版本：一律 None，调用方不得误判为 v1。
    assert parse("abc") is None
    assert parse(None) is None
    assert parse(2.5) is None
    assert parse("2.5") is None
    assert parse(True) is None
    assert parse(False) is None
    assert parse("") is None
    assert parse("   ") is None
    assert parse([2]) is None


def test_unparsable_version_skips_destructive_migration(monkeypatch, tmp_path):
    # 版本号无法确定（"abc" / 2.5）：不重跑破坏性迁移，用户值原样保留，
    # 只把版本号归一化为当前版本并落盘。
    for bad in ("abc", 2.5, "2.5", None, True, ""):
        state = _patch_state(monkeypatch, tmp_path)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"config_version": bad,
                                     "acl_default_visible": False,
                                     "acl_default_online": False}),
                         encoding="utf-8")
        c = config_mod.Config()
        assert c.get()["acl_default_visible"] is False
        assert c.get()["acl_default_online"] is False
        assert c.get()["config_version"] == 3
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["acl_default_visible"] is False
        assert saved["acl_default_online"] is False
        assert saved["config_version"] == 3


def test_string_float_version_migrates(monkeypatch, tmp_path):
    # "2.0" 是合法的 v2：解析为 2，走 v2→v3 翻回 True（decisions-2 的
    # int(...) 写法会把它误判为 v1，翻成 False 再停在版本 2）。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": "2.0",
                                 "acl_default_visible": False,
                                 "acl_default_online": False}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3


def test_newer_version_skips_migration(monkeypatch, tmp_path):
    # 高于当前版本的配置：不迁移、不降版本号，用户值原样保留。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 99,
                                 "acl_default_visible": False,
                                 "acl_default_online": False}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is False
    assert c.get()["acl_default_online"] is False
    assert c.get()["config_version"] == 99


def test_add_source_rejects_bad_api(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    ok, msg = c.add_source("x", "ftp://example.com/api")
    assert ok is False
