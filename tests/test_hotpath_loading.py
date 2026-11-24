"""加载/配置热路径优化回归。

1. adfilter 本地规则按 mtime 缓存：文件不变时不重复 read+parse，
   写入（_save_rules）后立即失效；
2. config.maybe_reload 节流：1 秒窗口内最多 stat 一次，但显式 load()
   与 save() 仍即时生效；
3. 管理页 gzip/ETag 行为在 test_http_core_paths 覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import adfilter  # noqa: E402
import config as config_mod  # noqa: E402


# ---------------- adfilter 本地规则缓存 ----------------

def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(adfilter, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(adfilter, "_RULES_FILE", tmp_path / "ad_rules.json")
    monkeypatch.setattr(adfilter, "_LOCAL_RULES_CACHE", None)


def test_local_rules_cached_and_reused(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    f = tmp_path / "ad_rules.json"
    f.write_text(json.dumps([{"name": "a", "hosts": ["x"], "regex": ["1.0"]}]),
                 encoding="utf-8")
    first = adfilter._load_rules()
    assert first and first[0]["name"] == "a"

    # 篡改磁盘内容但保持 mtime/size 不变 -> 应命中缓存（证明没有重复读盘）
    st = f.stat()
    f.write_text(json.dumps([{"name": "b", "hosts": ["y"], "regex": ["2.0"]}]),
                 encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert f.stat().st_size == st.st_size, "两段内容需等长，测试前提"
    assert adfilter._load_rules()[0]["name"] == "a"


def test_local_rules_cache_invalidated_on_save(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    adfilter._save_rules([{"name": "new", "hosts": ["h"], "regex": ["3.0"]}])
    assert adfilter._load_rules()[0]["name"] == "new"
    # 写入新内容后缓存必须失效并重新读取
    adfilter._save_rules([{"name": "next", "hosts": ["h"], "regex": ["4.0"]}])
    assert adfilter._load_rules()[0]["name"] == "next"


def test_local_rules_cached_list_is_not_shared(tmp_path, monkeypatch):
    """调用方 append 不得污染缓存（merge_subscription_rules 会 append）。"""
    _isolate(tmp_path, monkeypatch)
    adfilter._save_rules([{"name": "keep", "hosts": ["h"], "regex": ["5.0"]}])
    got = adfilter._load_rules()
    got.append({"name": "polluted", "hosts": ["z"], "regex": ["9.9"]})
    assert len(adfilter._load_rules()) == 1


# ---------------- config.maybe_reload 节流 ----------------

def test_maybe_reload_throttles_stat(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", cfg_path)
    monkeypatch.setattr(config_mod, "_STAT_CHECK_INTERVAL", 60.0)
    cfg = config_mod.Config()
    cfg.save({"filter_junk": True})

    calls = {"n": 0}
    real_stat = Path.stat

    def counting_stat(self, *a, **k):
        if str(self) == str(cfg_path):
            calls["n"] += 1
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", counting_stat)
    for _ in range(5):
        cfg.maybe_reload()
    assert calls["n"] <= 1, f"节流失效，stat 了 {calls['n']} 次"


def test_maybe_reload_still_detects_change_after_window(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", cfg_path)
    monkeypatch.setattr(config_mod, "_STAT_CHECK_INTERVAL", 0.0)  # 关闭节流
    cfg = config_mod.Config()
    cfg.save({"filter_junk": True})
    # 外部直接改文件（模拟手工编辑）应被检测到
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["filter_junk"] = False
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(0.01)
    os.utime(cfg_path, None)
    cfg.maybe_reload()
    assert cfg.read("filter_junk") is False


def test_save_bypasses_throttle(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", cfg_path)
    monkeypatch.setattr(config_mod, "_STAT_CHECK_INTERVAL", 3600.0)
    cfg = config_mod.Config()
    cfg.save({"filter_junk": False})
    assert cfg.read("filter_junk") is False
    cfg.save({"filter_junk": True})
    assert cfg.read("filter_junk") is True
