"""配置备份清理：_backup_config 只留最近 5 份、清理顺序、损坏回退。

先写失败测试再修实现。_STATE 是模块级路径，测试用 monkeypatch 把它
指到 tmp_path，绝不碰仓库真实的 state/。
"""
import json
import os

import config as config_mod


def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


def _backup_dir(tmp_path):
    return tmp_path / "state" / "backups"


def _seed_backup(backup_dir, name, marker, mtime):
    """写一份内容带 marker 的备份，并把 mtime 钉到给定值。"""
    p = backup_dir / name
    p.write_text(json.dumps({"marker": marker}), encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def _markers(backup_dir):
    out = {}
    for p in backup_dir.glob("config-*.json"):
        try:
            out[p.name] = json.loads(p.read_text(encoding="utf-8"))["marker"]
        except Exception:
            out[p.name] = "<corrupt>"
    return out


def test_backup_keeps_only_latest_five(monkeypatch, tmp_path):
    # 5 份旧备份（mtime 递增）+ 当前配置再备 1 份 = 6 份 → 只留最新 5 份。
    state = _patch_state(monkeypatch, tmp_path)
    bdir = _backup_dir(tmp_path)
    bdir.mkdir(parents=True)
    state.write_text(json.dumps({"marker": "current"}), encoding="utf-8")
    for i in range(1, 6):
        _seed_backup(bdir, f"config-20200101-00000{i}.json", f"old-{i}",
                     1700000000.0 + i)
    assert config_mod._backup_config() is not None
    kept = _markers(bdir)
    assert len(kept) == 5
    assert "old-1" not in kept.values()  # 最旧的一份被删
    assert "current" in kept.values()
    assert sorted(kept.values()) == ["current", "old-2", "old-3", "old-4", "old-5"]


def test_backup_cleanup_via_save_keeps_newest_five(monkeypatch, tmp_path):
    # 真实路径：Config.save() × 6 次 → 备份目录只剩最新 5 份，且是最新的 5 个值。
    # 注意首次 Config() 加载时迁移分支也会落一份备份（hot_limit=300），
    # 它出现最早，应最先被删。
    # mtime 钉法：新出现的文件按出现顺序钉到过去递增的时间（base+i），
    # 保证 save 内清理那一刻新文件的真实 mtime（现在）晚于所有已钉时间，
    # 新文件不会被当成最旧误删；也不依赖文件系统 mtime 粒度。
    import time

    _patch_state(monkeypatch, tmp_path)
    bdir = _backup_dir(tmp_path)
    c = config_mod.Config()
    base = time.time() - 3600.0
    seq = [0]

    def _pin_new():
        for p in sorted(bdir.glob("config-*.json"),
                        key=lambda q: q.stat().st_mtime):
            if p.name not in _pin_new.seen:
                _pin_new.seen.add(p.name)
                os.utime(p, (base + seq[0], base + seq[0]))
                seq[0] += 1
    _pin_new.seen = set()
    _pin_new()  # 首次加载产生的备份排最旧
    for i in range(1, 7):
        c.save({"hot_limit": 100 + i})
        _pin_new()
    backups = sorted(bdir.glob("config-*.json"),
                     key=lambda p: p.stat().st_mtime)
    assert len(backups) == 5
    kept = [json.loads(p.read_text(encoding="utf-8"))["hot_limit"]
            for p in backups]
    assert kept == [102, 103, 104, 105, 106]  # 最旧的 300 与 101 被删


def test_backup_cleanup_same_second_keeps_newest(monkeypatch, tmp_path):
    # 同秒多次备份：5 份旧备份 mtime 完全相同，名字按生成顺序是
    # config-TS.json（最旧）、config-TS-1.json … config-TS-4.json（最新）。
    # 再备 1 份后必须删最旧的 config-TS.json，而不是按名字排第一的 -1。
    state = _patch_state(monkeypatch, tmp_path)
    bdir = _backup_dir(tmp_path)
    bdir.mkdir(parents=True)
    state.write_text(json.dumps({"marker": "current"}), encoding="utf-8")
    same_mtime = 1700000000.0
    _seed_backup(bdir, "config-20200101-000000.json", "oldest", same_mtime)
    for i in range(1, 5):
        _seed_backup(bdir, f"config-20200101-000000-{i}.json", f"new-{i}",
                     same_mtime)
    assert config_mod._backup_config() is not None
    kept = _markers(bdir)
    assert len(kept) == 5
    assert "oldest" not in kept.values()  # 最旧的 plain 备份必须被删
    assert "new-1" in kept.values()  # 较新的 -1 备份必须留下
    assert "current" in kept.values()


def test_restore_prefers_newest_backup_same_mtime(monkeypatch, tmp_path):
    # 损坏回退：主配置写坏，两份备份 mtime 相同，名字 newer 的 -1 才是最新，
    # 恢复必须取它，而不是最旧的 plain 备份。
    state = _patch_state(monkeypatch, tmp_path)
    bdir = _backup_dir(tmp_path)
    bdir.mkdir(parents=True)
    same_mtime = 1700000000.0
    _seed_backup(bdir, "config-20200101-000000.json", "old", same_mtime)
    _seed_backup(bdir, "config-20200101-000000-1.json", "new", same_mtime)
    state.write_text("{not json", encoding="utf-8")
    c = config_mod.Config()
    assert c._restored_from_backup is True
    assert c.get()["marker"] == "new"


def test_restore_skips_corrupt_newest_backup(monkeypatch, tmp_path):
    # 损坏回退：最新的备份本身也是坏 JSON 时，跳过它恢复次新的有效备份。
    state = _patch_state(monkeypatch, tmp_path)
    bdir = _backup_dir(tmp_path)
    bdir.mkdir(parents=True)
    _seed_backup(bdir, "config-20200101-000001.json", "good", 1700000001.0)
    bad = bdir / "config-20200101-000002.json"
    bad.write_text("{broken", encoding="utf-8")
    os.utime(bad, (1700000002.0, 1700000002.0))
    state.write_text("{not json", encoding="utf-8")
    c = config_mod.Config()
    assert c._restored_from_backup is True
    assert c.get()["marker"] == "good"


def test_load_survives_missing_backup_dir(monkeypatch, tmp_path):
    # 备份目录缺失：load 不崩、走默认值；之后 save 能正常建备份。
    # 首次加载的迁移分支本身也会落一份备份，所以只断言“至少 1 份且含本次保存的值”。
    _patch_state(monkeypatch, tmp_path)
    assert not _backup_dir(tmp_path).exists()
    c = config_mod.Config()
    assert c.get()["config_version"] == config_mod.CONFIG_VERSION
    c.save({"hot_limit": 321})
    kept = [json.loads(p.read_text(encoding="utf-8")).get("hot_limit")
            for p in _backup_dir(tmp_path).glob("config-*.json")]
    assert 321 in kept


def test_load_and_save_survive_backup_dir_is_file(monkeypatch, tmp_path):
    # 备份目录损坏（同名位置是个普通文件）：load 不崩、回退默认值；
    # save 不抛错、配置本身照常落盘（备份失败只记 warning）。
    state = _patch_state(monkeypatch, tmp_path)
    bdir = _backup_dir(tmp_path)
    bdir.parent.mkdir(parents=True)
    bdir.write_text("not a dir", encoding="utf-8")
    state.write_text("{not json", encoding="utf-8")
    c = config_mod.Config()  # 不崩
    assert c.get()["hot_limit"] == config_mod.DEFAULTS["hot_limit"]
    assert c._restored_from_backup is False
    c.save({"hot_limit": 456})  # 不抛错
    assert c.get()["hot_limit"] == 456
    assert json.loads(state.read_text(encoding="utf-8"))["hot_limit"] == 456


def test_backup_config_no_state_file_returns_none(monkeypatch, tmp_path):
    # 主配置文件不存在时 _backup_config 返回 None，不建备份、不抛错。
    _patch_state(monkeypatch, tmp_path)
    assert config_mod._backup_config() is None
    assert not _backup_dir(tmp_path).exists()
