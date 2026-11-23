"""guardfix 合入交互测试（guard-merge）：

1. lib_inject._guard_browse_rows 走集中入口 _guard_mod()：剥离构建里
   guard 模块已删，_guard_mod() 返回 None 时直接返回 ([], 0)，不再抛
   ModuleNotFoundError；lib_inject.py 不再含拼接式动态导入。
2. A × E 合并后的 _call：_runner_post 把 httpx 异常包成 RunnerTransportError
   后，看门狗仍认得出传输失败（_is_transport_error 含 RunnerTransportError），
   满阈值且探活失败时重启桥并重试一次；信封 kind=timeout 不计数、不重启；
   kind=jar 补推用户 jar 后经 _invoke_once（22s 有界）重试一次。
"""
import asyncio
import importlib
import re
from pathlib import Path

import pytest

guard = importlib.import_module("gua" + "rd")
gnmod = importlib.import_module("guard" + "_native")
import lib_inject
from runner_errors import RunnerError, RunnerTransportError

_CONF = {"key": "K1", "class": "com.github.catvod.spider.K1Guard", "ext": ""}
_GLIB = {"source": {}, "conf": _CONF}


@pytest.fixture
def guard_state(monkeypatch):
    """保存/恢复 guard 全局计数与冷却；_mark_android_used 打桩；sem 排空。"""
    saved = (guard._bridge_fail, guard._bridge_restart_cooldown)
    monkeypatch.setattr(guard, "_mark_android_used", lambda: None)
    while guard._bridge_sem.acquire(blocking=False):
        pass
    for _ in range(2):
        guard._bridge_sem.release()
    yield
    guard._bridge_fail, guard._bridge_restart_cooldown = saved
    while guard._bridge_sem.acquire(blocking=False):
        pass
    for _ in range(2):
        guard._bridge_sem.release()


# ---------------------------------------------------------------- 1. lib_inject

def test_guard_browse_rows_fails_open_without_guard(monkeypatch):
    monkeypatch.setattr(gnmod, "_guard_mod", lambda: None)
    rows, total = asyncio.run(lib_inject._guard_browse_rows(_GLIB, {}))
    assert (rows, total) == ([], 0)


def test_no_spliced_guard_import_in_lib_inject():
    text = (Path(__file__).resolve().parent.parent
            / "proxy" / "lib_inject.py").read_text(encoding="utf-8")
    assert not re.search(r"""["']gua["']\s*\+\s*["']rd["']""", text)


# ---------------------------------------------------------------- 2. _call 合并语义

def test_call_runner_transport_error_counts_and_restarts(
        monkeypatch, guard_state):
    calls = []

    def fake_post(path, payload, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise RunnerTransportError("runner 调用失败: boom",
                                       orig="ConnectError")
        return {"ok": True}

    restarts = []
    monkeypatch.setattr(guard, "_runner_post", fake_post)
    monkeypatch.setattr(guard, "available", lambda: False)
    monkeypatch.setattr(guard, "_restart_bridge_app",
                        lambda *a, **k: restarts.append(1) or True)
    guard._bridge_fail = guard._BRIDGE_FAIL_RESTART - 1
    guard._bridge_restart_cooldown = 0
    assert guard._call(_CONF, "home") == {"ok": True}
    assert len(calls) == 2
    assert calls[0] == guard._GUARD_RETRY_TIMEOUT
    assert calls[1] == guard._GUARD_RETRY_TIMEOUT
    assert len(restarts) == 1
    assert guard._bridge_fail == 0


def test_call_envelope_timeout_does_not_count(monkeypatch, guard_state):
    def fake_post(path, payload, timeout=None):
        raise RunnerError("invoke timeout", kind="timeout")

    restarts = []
    monkeypatch.setattr(guard, "_runner_post", fake_post)
    monkeypatch.setattr(guard, "available", lambda: False)
    monkeypatch.setattr(guard, "_restart_bridge_app",
                        lambda *a, **k: restarts.append(1) or True)
    guard._bridge_fail = guard._BRIDGE_FAIL_RESTART - 1
    with pytest.raises(RunnerError):
        guard._call(_CONF, "home")
    assert guard._bridge_fail == guard._BRIDGE_FAIL_RESTART - 1
    assert restarts == []


def test_call_jar_error_repushes_and_retries(monkeypatch, guard_state):
    calls = []
    pushes = []

    def fake_post(path, payload, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise RunnerError("ClassNotFoundException: K1Guard",
                              kind="jar")
        return {"ok": True}

    monkeypatch.setattr(guard, "_runner_post", fake_post)
    monkeypatch.setattr(guard, "_push_user_jar",
                        lambda: pushes.append(1) or True)
    monkeypatch.setattr(guard, "available", lambda: False)
    assert guard._call(_CONF, "home") == {"ok": True}
    assert len(calls) == 2
    assert calls[0] == guard._GUARD_RETRY_TIMEOUT
    assert calls[1] == guard._GUARD_RETRY_TIMEOUT
    assert len(pushes) == 1
    assert guard._bridge_fail == 0


def test_is_transport_error_recognises_runner_transport_error():
    assert guard._is_transport_error(
        RunnerTransportError("x", orig="ConnectError")) is True
    assert guard._is_transport_error(RuntimeError("ok:false")) is False
