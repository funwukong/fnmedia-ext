"""guardfix-B：spider/guard 导入与解析正确性（复现测试先行）。

覆盖清单：
1. _resolve_spider 缺 guard 模块导入（普通 spider 非直出路径静默失败）
2. _resolve_guard_once import 在 try 之外；_resolve_guard/_resolve_guard_pan
   的 int(index) 在 try 之外（_safe_index）
3. guard.py 模块级 env 解析非法值导致 import 失败
4. direct_ep 的 `"/" not in vod_id[:1]` 恒真析支 + 断言日志
5. providers._dispatch_search spider 分支缺 GUARD 门控
6. Config.load() 第二循环不恢复 spider_src/spider_key
7. parse_tvbox 字符串 type 与 classify_subscription guard 去重键
"""
import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

guard = importlib.import_module("gua" + "rd")
spiders = importlib.import_module("spi" + "ders")

PROXY = Path(__file__).resolve().parent.parent / "proxy"

_M3U8 = "http://cdn.example/x.m3u8"


def _detail_l1():
    return {"vod_play_from": "L1",
            "vod_play_url": "第1集$ep1#第2集$ep2"}


def _play_ep1_only(conf, flag, vid):
    if vid == "ep1":
        return {"url": _M3U8, "parse": 0}
    return {"url": ""}


# ---------------------------------------------------------------- item 1 + 4

def test_resolve_spider_non_direct_uses_real_parse_episodes(monkeypatch, caplog):
    """裸 vod_id（无 playUrl=/{}）必须走 detail + 真实 guard.parse_episodes，
    而不是被恒真析支当成直出集。"""
    import app as appmod
    monkeypatch.setattr(appmod.config, "all_sources",
                        lambda: [{"id": "s1", "spider_src": True}])
    monkeypatch.setattr(appmod.providers, "_spider_conf",
                        lambda src: {"key": "s1"})
    monkeypatch.setattr(spiders, "detail", lambda *a, **k: _detail_l1())
    monkeypatch.setattr(spiders, "play", _play_ep1_only)
    picked = {"play_from": "s1:L1", "url": "12345", "index": 0}
    with caplog.at_level(logging.WARNING, logger="fnmedia"):
        assert asyncio.run(appmod._resolve_spider(picked)) is True
    assert picked["url"] == _M3U8
    assert "no episode shape" in caplog.text


def test_resolve_guard_non_direct_bare_vod_id(monkeypatch, caplog):
    """guard 侧同款：裸 vod_id + 有线路名时走 detail，不直接调 play。"""
    import app as appmod
    monkeypatch.setattr(appmod.config, "all_sources",
                        lambda: [{"id": "g1", "guard": True}])
    monkeypatch.setattr(appmod.providers, "_guard_conf",
                        lambda src: {"key": "g1"})
    monkeypatch.setattr(guard, "detail", lambda *a, **k: _detail_l1())
    monkeypatch.setattr(guard, "play", _play_ep1_only)
    picked = {"play_from": "g1:L1", "url": "98765", "index": 0}
    with caplog.at_level(logging.WARNING, logger="fnmedia"):
        assert asyncio.run(appmod._resolve_guard_once(picked)) is True
    assert picked["url"] == _M3U8
    assert "no episode shape" in caplog.text


# ---------------------------------------------------------------- item 2

def test_resolve_guard_once_guard_import_failure_returns_false(monkeypatch):
    """guard 模块导入失败时播放解析返回 False，而不是把 ImportError
    抛到播放接口（500）。"""
    import app as appmod
    monkeypatch.setattr(appmod.config, "all_sources",
                        lambda: [{"id": "g1", "guard": True}])
    monkeypatch.setattr(appmod.providers, "_guard_conf",
                        lambda src: {"key": "g1"})
    monkeypatch.setitem(sys.modules, "guard", None)
    picked = {"play_from": "g1:L1", "url": "12345", "index": 0}
    assert asyncio.run(appmod._resolve_guard_once(picked)) is False


def test_safe_index_helper():
    import app as appmod
    assert appmod._safe_index({}) == 0
    assert appmod._safe_index({"index": 0}) == 0
    assert appmod._safe_index({"index": 2}) == 2
    assert appmod._safe_index({"index": "2"}) == 2
    assert appmod._safe_index({"index": "not-a-number"}) == 0
    assert appmod._safe_index({"index": None}) == 0
    assert appmod._safe_index({"index": -3}) == 0


def test_resolve_guard_bad_index_still_resolves(monkeypatch):
    """index 为非数字字符串时 _resolve_guard 不应抛 ValueError，
    应按 0 集正常解析。"""
    import app as appmod
    monkeypatch.setattr(appmod.config, "all_sources",
                        lambda: [{"id": "g1", "guard": True}])
    monkeypatch.setattr(appmod.providers, "_guard_conf",
                        lambda src: {"key": "g1"})
    monkeypatch.setattr(guard, "detail", lambda *a, **k: _detail_l1())
    monkeypatch.setattr(guard, "play", _play_ep1_only)
    picked = {"play_from": "g1:L1", "url": "vod-badidx-1",
              "index": "not-a-number"}
    assert asyncio.run(appmod._resolve_guard(picked)) is True
    assert picked["url"] == _M3U8


# ---------------------------------------------------------------- item 3

def _run_guard_subprocess(env_extra, code):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True,
                          cwd=str(PROXY), env=env, timeout=60)


def test_guard_env_invalid_timeout_uses_default():
    r = _run_guard_subprocess(
        {"FNM_GUARD_TIMEOUT": "not-a-number"},
        "import importlib as _i; _g = _i.import_module('gua' + 'rd'); "
        "print(repr(_g.TIMEOUT))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "150.0"


@pytest.mark.parametrize("raw", ["xx", "0", "-3"])
def test_guard_env_concurrency_invalid_or_zero_still_imports(raw):
    r = _run_guard_subprocess(
        {"FNM_GUARD_CONCURRENCY": raw},
        "import importlib as _i; _g = _i.import_module('gua' + 'rd'); "
        "print(_g._bridge_sem.acquire(blocking=False))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "True"


def test_env_helpers(monkeypatch):
    monkeypatch.setenv("FNM_GUARD_X_FLOAT", "abc")
    assert guard._env_float("FNM_GUARD_X_FLOAT", 150.0) == 150.0
    monkeypatch.setenv("FNM_GUARD_X_FLOAT", "12.5")
    assert guard._env_float("FNM_GUARD_X_FLOAT", 150.0) == 12.5
    monkeypatch.setenv("FNM_GUARD_X_INT", "xx")
    assert guard._env_int("FNM_GUARD_X_INT", 2, minimum=1) == 2
    monkeypatch.setenv("FNM_GUARD_X_INT", "0")
    assert guard._env_int("FNM_GUARD_X_INT", 2, minimum=1) == 1
    monkeypatch.setenv("FNM_GUARD_X_INT", "-5")
    assert guard._env_int("FNM_GUARD_X_INT", 2, minimum=1) == 1


# ---------------------------------------------------------------- item 5

def test_dispatch_search_spider_branch_gated_by_guard(monkeypatch):
    import features
    import providers

    async def fake_spider(*a, **k):
        return ["spider"]

    async def fake_generic(*a, **k):
        return ["generic"]

    monkeypatch.setattr(providers, "spider_search_source", fake_spider)
    monkeypatch.setattr(providers, "search_source", fake_generic)
    monkeypatch.setattr(features, "GUARD", False)
    assert asyncio.run(providers._dispatch_search(
        None, {"spider_src": True}, "kw")) == ["generic"]
    monkeypatch.setattr(features, "GUARD", True)
    assert asyncio.run(providers._dispatch_search(
        None, {"spider_src": True}, "kw")) == ["spider"]


# ---------------------------------------------------------------- item 6

def test_config_load_restores_spider_fields_for_custom_source(monkeypatch,
                                                             tmp_path):
    import config as config_mod
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "sources": [{"id": "custom-sp1", "name": "MySpider",
                     "api": "http://x/", "enabled": True,
                     "spider_src": True, "spider_key": "MySpider"}]}),
        encoding="utf-8")
    c = config_mod.Config()
    srcs = [s for s in c.get()["sources"] if s.get("id") == "custom-sp1"]
    assert len(srcs) == 1
    assert srcs[0].get("spider_src") is True
    assert srcs[0].get("spider_key") == "MySpider"


# ---------------------------------------------------------------- item 7

def test_parse_tvbox_accepts_string_type_3():
    sites = [{"name": "A", "type": "3", "api": "csp_AppSxGuard",
              "ext": "https://x/e", "searchable": 1, "quickSearch": 0}]
    out = guard.parse_tvbox({"sites": sites})
    assert len(out["sites"]) == 1
    assert out["sites"][0]["key"] == "AppSx"
    # int 型 type 仍与之前一致
    sites_int = [{"name": "A", "type": 3, "api": "csp_AppSxGuard",
                  "ext": ""}]
    assert len(guard.parse_tvbox({"sites": sites_int})["sites"]) == 1
    # 非 Guard / 非 type=3 仍跳过
    assert guard.parse_tvbox({"sites": [
        {"name": "C", "type": "1", "api": "csp_AppSxGuard", "ext": ""}]})["sites"] == []


def test_classify_subscription_guard_dedup_by_api_and_ext():
    import tvbox
    sites = [
        {"name": "A1", "type": 3, "api": "csp_AppSxGuard", "ext": "cfg-one"},
        {"name": "A2", "type": 3, "api": "csp_AppSxGuard", "ext": "cfg-two"},
        {"name": "A3", "type": 3, "api": "csp_AppSxGuard", "ext": "cfg-one"},
        {"name": "B1", "type": 3, "api": "csp_BttwooGuard",
         "ext": {"token": "one"}},
        {"name": "B2", "type": 3, "api": "csp_BttwooGuard",
         "ext": {"token": "two"}},
    ]
    groups = tvbox.classify_subscription(sites)
    assert len(groups["guard"]) == 4
    # 下游 parse_tvbox 的 count 序号规则给同 api 多配置分配唯一 key
    parsed = guard.parse_tvbox({"sites": groups["guard"]})
    assert [s["key"] for s in parsed["sites"]] == [
        "AppSx", "AppSx2", "Bttwoo", "Bttwoo2"]
