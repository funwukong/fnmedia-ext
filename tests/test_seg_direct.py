"""分片 CDN 直连绕行（v1.2.8）决策逻辑回归。

只测判定，不发请求：满足白名单与"不需要服务端补头"才 302，其余一律照旧代理。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import hls_race as racemod


def _req(dec="0"):
    return SimpleNamespace(query_params={"dec": dec})


def _cfg(**kw):
    def _read(key, default=None):
        return kw.get(key, default)
    return SimpleNamespace(read=_read)


def test_disabled_by_default():
    with patch("app.config", _cfg(seg_direct_enabled=False,
                                  seg_direct_hosts=["cdn.test"])):
        assert racemod._seg_direct_ok("https://cdn.test/a.ts", {}, _req()) is False


def test_empty_hosts_never_directs():
    with patch("app.config", _cfg(seg_direct_enabled=True,
                                  seg_direct_hosts=[])):
        assert racemod._seg_direct_ok("https://cdn.test/a.ts", {}, _req()) is False


def test_host_suffix_match_and_non_match():
    with patch("app.config", _cfg(seg_direct_enabled=True,
                                  seg_direct_hosts=["example.com"])):
        assert racemod._seg_direct_ok("https://a.example.com/x.ts", {}, _req())
        assert racemod._seg_direct_ok("https://example.com/x.ts", {}, _req())
        # 形似域不得命中
        assert not racemod._seg_direct_ok("https://example.com.evil.test/x.ts", {},
                                          _req())
        assert not racemod._seg_direct_ok("https://notexample.com/x.ts", {}, _req())


def test_requires_no_server_side_headers():
    with patch("app.config", _cfg(seg_direct_enabled=True,
                                  seg_direct_hosts=["cdn.test"])):
        assert racemod._seg_direct_ok("https://cdn.test/a.ts", {}, _req())
        # Range 由播放器自己带，不算服务端补头
        assert racemod._seg_direct_ok("https://cdn.test/a.ts",
                                      {"Range": "bytes=0-1"}, _req())
        assert not racemod._seg_direct_ok("https://cdn.test/a.ts",
                                          {"Referer": "https://src.test/"}, _req())
        assert not racemod._seg_direct_ok("https://cdn.test/a.ts",
                                          {"Cookie": "a=b"}, _req())


def test_encrypted_segments_never_direct():
    with patch("app.config", _cfg(seg_direct_enabled=True,
                                  seg_direct_hosts=["cdn.test"])):
        assert not racemod._seg_direct_ok("https://cdn.test/a.ts", {},
                                          _req(dec="1"))


def test_hosts_parse_from_string_and_list():
    with patch("app.config", _cfg(seg_direct_enabled=True,
                                  seg_direct_hosts="a.test, .b.test ,, /x")):
        hosts = racemod._seg_direct_hosts()
        assert hosts == ("a.test", "b.test")
    with patch("app.config", _cfg(seg_direct_enabled=True,
                                  seg_direct_hosts=["A.Test", "a.test"])):
        assert racemod._seg_direct_hosts() == ("a.test",)


def test_hosts_cache_reused_and_invalidated_by_revision(tmp_path, monkeypatch):
    """白名单 tuple 按配置代际缓存：每分片都要问一次，不能每次都 deepcopy+清洗。"""
    import config as cfgmod
    import app as appmod

    monkeypatch.setattr(cfgmod, "_STATE", tmp_path / "config.json")
    cfg = cfgmod.Config()
    monkeypatch.setattr(appmod, "config", cfg)

    cfg.save({"seg_direct_hosts": ["A.Example.com", "a.example.com"]})
    assert racemod._seg_direct_hosts() == ("a.example.com",)
    rev, cached = racemod._SEG_DIRECT_HOSTS_CACHE
    assert rev == cfg.revision
    assert racemod._seg_direct_hosts() is cached

    cfg.save({"seg_direct_hosts": ["b.example.com"]})
    assert racemod._seg_direct_hosts() == ("b.example.com",)


def test_hosts_cache_bypassed_when_config_read_patched():
    """测试/插件打了 config.read 补丁时必须不吃缓存，保持旧语义。"""
    with patch("app.config", _cfg(seg_direct_hosts=["x.test"])):
        assert racemod._seg_direct_hosts() == ("x.test",)
    with patch("app.config", _cfg(seg_direct_hosts=["y.test"])):
        assert racemod._seg_direct_hosts() == ("y.test",)
