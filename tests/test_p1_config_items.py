"""P1 配置项：10 main_ready_budget、11 CONFIG_VERSION 迁移、12 死开关接线。

- 第 10 项：DEFAULTS 含 main_ready_budget=2.4；旧配置加载后自动出现该键；
  每次读取夹取 [0.8, 2.8]，非法值回落 2.4（POST 夹取见 test_slow_line_budget）。
- 第 11 项：版本号稳健解析；非数字不再误判为 v1 翻掉用户显式 True；
  降级警告；迁移跑与不跑都记日志。
- 第 12 项：source_health_enabled 门控搜索降级（不跳过/不记录），
  domain_heal_enabled 门控域名自愈端点（不试备用域名）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

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
    # v1→v2→v3 完整链：v1/v2 存量跑迁移，最终都翻回 True；v3 及更新保留
    # 用户显式值；无法解析的版本不跑迁移、原样保留。
    cases = [
        # (存量 config_version, 存量值, 迁移后期望值)
        (1, False, True),    # v1→v2 翻 False，再 v2→v3 翻回 True
        (1, True, True),
        (2, False, True),    # v2→v3 翻回 True
        (2, True, True),
        (3, False, False),   # 已是当前版本，不迁移，显式 False 保留
        (3, True, True),
        ("3", False, False),
        ("2.0", False, True),
        (2.0, False, True),
        ("2", False, True),
        ("abc", False, False),
        (None, False, False),
        (2.5, False, False),
        ("2.5", False, False),
    ]
    for raw, stored, expected in cases:
        state = _patch_state(monkeypatch, tmp_path)
        state.parent.mkdir(parents=True, exist_ok=True)
        if state.exists():
            state.unlink()
        state.write_text(json.dumps({"config_version": raw,
                                     "acl_default_visible": stored,
                                     "acl_default_online": stored}),
                         encoding="utf-8")
        c = config_mod.Config()
        assert c.get()["acl_default_visible"] is expected, raw
        assert c.get()["acl_default_online"] is expected, raw


def test_version_missing_still_migrates(monkeypatch, tmp_path):
    # 没有 config_version 键的更旧配置视为 v1，走完整链 v1→v2→v3，
    # 最终版本 3、两项值 True。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 3


def test_version_unparsable_warns_and_normalizes(monkeypatch, tmp_path, caplog):
    # 非数字版本：记 warning、不翻用户值、版本号归一化为 3 并落盘。
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
    assert c.get()["config_version"] == 3
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["config_version"] == 3
    assert saved["acl_default_visible"] is True
    assert "unparsable" in caplog.text


def test_version_downgrade_warns_and_keeps_values(monkeypatch, tmp_path, caplog):
    # 存量版本 > 当前 CONFIG_VERSION（3）：记 warning，不迁移、不降版本号、值原样保留。
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 99,
                                 "acl_default_visible": True,
                                 "acl_default_online": True}),
                     encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fnmedia.config"):
        c = config_mod.Config()
    assert c.get()["acl_default_visible"] is True
    assert c.get()["acl_default_online"] is True
    assert c.get()["config_version"] == 99
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
    assert "v2->v3" in caplog.text
    caplog.clear()
    state.write_text(json.dumps({"config_version": 3,
                                 "acl_default_visible": True}),
                     encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="fnmedia.config"):
        config_mod.Config()
    assert "no migration needed" in caplog.text


# ---------------- 第 12 项：源健康降级开关 ----------------

import providers as providers_mod
import sourcehealth


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"code": 1, "list": []}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.resp


_SRC = {"id": "p1-health-src",
        "api": "https://p1-health.test/api.php/provide/vod/at/json"}


def _make_dead(key):
    sourcehealth.reset(key)
    for _ in range(5):
        sourcehealth.record(key, False, 1.0)
    assert sourcehealth.usable(key) is False


def test_health_switch_off_does_not_skip_dead_source(monkeypatch):
    # 开关关：即使画像已死（5 次失败/冷却窗内），搜索也不跳过，照常请求。
    _make_dead(_SRC["id"])
    monkeypatch.setattr(providers_mod, "_source_health_enabled", lambda: False)
    client = _FakeClient(_FakeResp())
    try:
        out = asyncio.run(providers_mod.search_source(client, _SRC, "测试"))
        assert out == []
        assert len(client.calls) == 1
        # 开关关也不记录：成功结果未清零失败计数，画像仍死。
        assert sourcehealth.usable(_SRC["id"]) is False
    finally:
        sourcehealth.reset(_SRC["id"])


def test_health_switch_on_skips_dead_source(monkeypatch):
    # 开关开：行为与改前一致，死源在冷却窗内直接跳过，不发请求。
    _make_dead(_SRC["id"])
    monkeypatch.setattr(providers_mod, "_source_health_enabled", lambda: True)
    client = _FakeClient(_FakeResp())
    try:
        out = asyncio.run(providers_mod.search_source(client, _SRC, "测试"))
    finally:
        sourcehealth.reset(_SRC["id"])
    assert out == []
    assert client.calls == []


def test_health_switch_off_does_not_record(monkeypatch):
    # 开关关：搜索失败也不记入画像。
    key = "p1-health-norecord"
    sourcehealth.reset(key)
    monkeypatch.setattr(providers_mod, "_source_health_enabled", lambda: False)
    client = _FakeClient(exc=RuntimeError("boom"))
    try:
        out = asyncio.run(providers_mod.search_source(
            client, {"id": key, "api": "https://p1-norecord.test/api.php"}, "测试"))
        assert out == []
        assert sourcehealth.stats(key) == {}
    finally:
        sourcehealth.reset(key)


def test_health_switch_on_records_failure(monkeypatch):
    # 开关开：搜索失败记入画像（行为与改前一致）。
    key = "p1-health-record"
    sourcehealth.reset(key)
    monkeypatch.setattr(providers_mod, "_source_health_enabled", lambda: True)
    client = _FakeClient(exc=RuntimeError("boom"))
    try:
        out = asyncio.run(providers_mod.search_source(
            client, {"id": key, "api": "https://p1-record.test/api.php"}, "测试"))
        assert out == []
        assert sourcehealth.stats(key)["fails"] == 1
    finally:
        sourcehealth.reset(key)


# ---------------- 第 12 项：域名自愈开关 ----------------

import app as appmod
import domain_heal as domain_heal_mod


class HealEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        item = patch.object(config_mod, "_STATE",
                            Path(self._tmp.name) / "config.json")
        item.start()
        self.addCleanup(item.stop)
        self.cfg = config_mod.Config()
        item = patch.object(appmod, "config", self.cfg)
        item.start()
        self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_heal_disabled_returns_none_without_calling_heal(self):
        # 开关关：不试备用域名、不换域名，heal() 一次也不调用。
        self.cfg.save({"domain_heal_enabled": False})
        with patch.object(domain_heal_mod, "heal",
                          return_value="https://bak.test/api.php") as heal:
            resp = await self.client.post("/v/api/ext/source/heal",
                                          json={"id": "baofeng"})
        heal.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("code"), 0)
        self.assertIsNone(body.get("data"))
        self.assertIn("disabled", body.get("msg", ""))

    async def test_heal_enabled_calls_heal(self):
        # 开关开：行为与改前一致，调 heal() 并返回其结果。
        self.cfg.save({"domain_heal_enabled": True})
        with patch.object(domain_heal_mod, "heal",
                          return_value="https://bak.test/api.php/x") as heal:
            resp = await self.client.post("/v/api/ext/source/heal",
                                          json={"id": "baofeng"})
        heal.assert_called_once()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("code"), 0)
        self.assertEqual(body.get("data"), "https://bak.test/api.php/x")
