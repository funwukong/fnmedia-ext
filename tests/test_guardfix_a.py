"""guardfix-a：guard 调用链超时与重启。

复现测试（先写、跑通失败，再改代码）：
1. _call 重试显式 timeout=22（FNM_GUARD_RETRY_TIMEOUT），重启移出 _bridge_sem，
   _bridge_sem.acquire 有界超时。
2. _restart_bridge_app 无论成败都写冷却，失败用 120s 长冷却。
3. _bridge_fail 只对传输层失败计数（available() 探活失败时），业务 ok:false 不计数；
   映射无效时跳过重启、不 docker exec。
4. _resolve_guard_once detail/play 包 wait_for(30)，失败写 picked["guard_error"]。
5. msearch parse_episodes 移入 try；as_completed 单分支异常不毒死竞速。
6. guard_native wait_for 用 shield 包共享 inflight 任务，超时不取消它。
7. 专用 ThreadPoolExecutor：刻意不做（见文件末尾注释）。
8. 传输失败日志 warning，业务失败保持 debug。
"""
import asyncio
import importlib
import threading
import time

import httpx
import pytest

guard = importlib.import_module("gua" + "rd")
gnmod = importlib.import_module("guard" + "_native")
import app as appmod

_CONF = {"key": "K1", "class": "com.github.catvod.spider.K1Guard", "ext": ""}


def _conn_err():
    return httpx.ConnectError("boom", request=httpx.Request("POST", "http://127.0.0.1:9100/invoke"))


@pytest.fixture
def guard_state(monkeypatch):
    """保存/恢复 guard 全局计数与冷却；_mark_android_used 打桩；sem 排空。"""
    saved = (guard._bridge_fail, guard._bridge_restart_cooldown)
    monkeypatch.setattr(guard, "_mark_android_used", lambda: None)
    # 排空信号量到满许可，避免别的测试残留占用影响本测试
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


def _run_in_thread(fn):
    """在线程里跑可能阻塞的同步调用，带超时，返回 (exc, result)。"""
    box = {}

    def _t():
        try:
            box["res"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(6.0)
    return box, th


# ---------------------------------------------------------------- 1. _call

def test_call_retry_uses_explicit_timeout(monkeypatch, guard_state):
    calls = []

    def fake_post(path, payload, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise _conn_err()
        return {"ok": True}

    monkeypatch.setattr(guard, "_runner_post", fake_post)
    monkeypatch.setattr(guard, "available", lambda: False)

    def _fake_restart(container=None):
        # 模拟真实 _restart_bridge_app 成功时写冷却（30s）
        guard._bridge_restart_cooldown = time.time() + 30.0
        return True

    monkeypatch.setattr(guard, "_restart_bridge_app", _fake_restart)
    guard._bridge_fail = 2
    assert guard._call(_CONF, "home") == {"ok": True}
    assert calls == [guard._GUARD_RETRY_TIMEOUT, guard._GUARD_RETRY_TIMEOUT]
    assert guard._GUARD_RETRY_TIMEOUT == 22.0


def test_call_sem_acquire_bounded(monkeypatch, guard_state):
    monkeypatch.setattr(guard, "_BRIDGE_SEM_TIMEOUT", 0.2, raising=False)
    calls = []
    monkeypatch.setattr(guard, "_runner_post",
                        lambda *a, **k: calls.append(1) or {"ok": True})
    # 占满两个许可：_call 再拿不到就应在有界时间内失败，而不是无限等
    guard._bridge_sem.acquire()
    guard._bridge_sem.acquire()
    try:
        box, th = _run_in_thread(lambda: guard._call(_CONF, "home"))
    finally:
        guard._bridge_sem.release()
        guard._bridge_sem.release()
    assert not th.is_alive(), "sem 拿不到时 _call 不应无限阻塞"
    assert isinstance(box.get("exc"), TimeoutError)
    assert calls == []


def test_call_restart_outside_sem(monkeypatch, guard_state):
    """重启期间信号量必须是空闲的（别的调用能拿到许可）。"""
    seen = {}

    def fake_restart(container=None):
        seen["acquired"] = guard._bridge_sem.acquire(timeout=2)
        if seen["acquired"]:
            guard._bridge_sem.release()
        return True

    calls = {"n": 0}

    def fake_post(path, payload, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _conn_err()
        return {"ok": True}

    monkeypatch.setattr(guard, "_runner_post", fake_post)
    monkeypatch.setattr(guard, "available", lambda: False)
    monkeypatch.setattr(guard, "_restart_bridge_app", fake_restart)
    guard._bridge_fail = 2
    # 占掉另一个许可：_call 持有最后一个许可时重启；重启若还在 sem 内，
    # fake_restart 的 acquire 必然拿不到（0 空闲）；移出 sem 后有 1 空闲。
    guard._bridge_sem.acquire()
    try:
        assert guard._call(_CONF, "home") == {"ok": True}
    finally:
        guard._bridge_sem.release()
    assert seen.get("acquired") is True


# ---------------------------------------------------------------- 2. 重启冷却

def test_restart_cooldown_on_failure(monkeypatch, guard_state):
    monkeypatch.setattr(guard, "_docker_exec",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker down")))
    monkeypatch.setattr(guard, "_redroid_bridge_up", lambda: False)
    monkeypatch.setattr(guard, "_container_exists", lambda name: True)
    t0 = time.time()
    assert guard._restart_bridge_app() is False
    assert guard._bridge_restart_cooldown >= t0 + 120 - 1


def test_restart_cooldown_on_success(monkeypatch, guard_state):
    monkeypatch.setattr(guard, "_docker_exec",
                        lambda *a, **k: None)
    monkeypatch.setattr(guard, "_redroid_bridge_up", lambda: True)
    monkeypatch.setattr(guard, "_container_exists", lambda name: True)
    t0 = time.time()
    assert guard._restart_bridge_app() is True
    assert t0 + 30 - 1 <= guard._bridge_restart_cooldown <= t0 + 30 + 5


def test_retry_failure_keeps_fail_count(monkeypatch, guard_state):
    """重试也失败：_bridge_fail 不清零，但长冷却阻止立即再触发重启。"""
    def fake_post(path, payload, timeout=None):
        raise _conn_err()

    def fake_restart(container=None):
        # 模拟真实 _restart_bridge_app 成功时写冷却（30s）
        guard._bridge_restart_cooldown = time.time() + 30.0
        return True

    monkeypatch.setattr(guard, "_runner_post", fake_post)
    monkeypatch.setattr(guard, "available", lambda: False)
    monkeypatch.setattr(guard, "_restart_bridge_app", fake_restart)
    guard._bridge_fail = 2
    with pytest.raises(httpx.ConnectError):
        guard._call(_CONF, "home")
    assert guard._bridge_fail == 3
    # 立即再调一次：冷却期内不应再重启（restart 未被再次调用即证明）
    restarts = []
    monkeypatch.setattr(guard, "_restart_bridge_app",
                        lambda container=None: restarts.append(1) or True)
    with pytest.raises(httpx.ConnectError):
        guard._call(_CONF, "home")
    assert restarts == []


# ---------------------------------------------------------------- 3. 只计传输层失败

def test_business_error_not_counted(monkeypatch, guard_state):
    def fake_post(path, payload, timeout=None):
        raise RuntimeError("ok:false boom")

    monkeypatch.setattr(guard, "_runner_post", fake_post)
    restarts = []
    monkeypatch.setattr(guard, "_restart_bridge_app",
                        lambda container=None: restarts.append(1) or True)
    with pytest.raises(RuntimeError, match="ok:false"):
        guard._call(_CONF, "home")
    assert guard._bridge_fail == 0
    assert restarts == []


def test_transport_error_not_counted_when_runner_alive(monkeypatch, guard_state):
    monkeypatch.setattr(guard, "_runner_post",
                        lambda *a, **k: (_ for _ in ()).throw(_conn_err()))
    monkeypatch.setattr(guard, "available", lambda: True)
    with pytest.raises(httpx.ConnectError):
        guard._call(_CONF, "home")
    assert guard._bridge_fail == 0


def test_transport_error_counted_when_runner_down(monkeypatch, guard_state):
    monkeypatch.setattr(guard, "_runner_post",
                        lambda *a, **k: (_ for _ in ()).throw(_conn_err()))
    monkeypatch.setattr(guard, "available", lambda: False)
    with pytest.raises(httpx.ConnectError):
        guard._call(_CONF, "home")
    assert guard._bridge_fail == 1


def test_restart_skipped_on_bad_container_mapping(monkeypatch, guard_state, caplog):
    monkeypatch.setattr(guard, "_container_for",
                        lambda url: "no-such-container", raising=False)
    monkeypatch.setattr(guard, "_container_exists", lambda name: False)
    exec_calls = []
    monkeypatch.setattr(guard, "_docker_exec",
                        lambda *a, **k: exec_calls.append(a) or None)
    import logging
    with caplog.at_level(logging.WARNING, logger="fnmedia"):
        assert guard._restart_bridge_app() is False
    assert exec_calls == []
    assert any("no-such-container" in r.getMessage() for r in caplog.records)
    assert guard._bridge_restart_cooldown > time.time()


def test_restart_uses_mapped_container(monkeypatch, guard_state):
    monkeypatch.setattr(guard, "_container_for",
                        lambda url: "fnmedia-redroid2", raising=False)
    monkeypatch.setattr(guard, "_container_exists", lambda name: True)
    exec_calls = []
    monkeypatch.setattr(guard, "_docker_exec",
                        lambda *a, container=None, **k: exec_calls.append(container))
    monkeypatch.setattr(guard, "_redroid_bridge_up", lambda: True)
    assert guard._restart_bridge_app() is True
    assert exec_calls and all(c == "fnmedia-redroid2" for c in exec_calls)


# ---------------------------------------------------------------- 8. 日志级别

def test_transport_failure_logs_warning(monkeypatch, guard_state, caplog):
    import logging
    monkeypatch.setattr(guard, "_runner_post",
                        lambda *a, **k: (_ for _ in ()).throw(_conn_err()))
    monkeypatch.setattr(guard, "available", lambda: False)
    with caplog.at_level(logging.WARNING, logger="fnmedia"):
        with pytest.raises(httpx.ConnectError):
            guard._call(_CONF, "home")
    assert any(r.levelno >= logging.WARNING and "_call" in r.message
               for r in caplog.records)


def test_business_failure_stays_debug(monkeypatch, guard_state, caplog):
    import logging
    monkeypatch.setattr(guard, "_runner_post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ok:false")))
    with caplog.at_level(logging.WARNING, logger="fnmedia"):
        with pytest.raises(RuntimeError):
            guard._call(_CONF, "home")
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------- 4. app._resolve_guard_once

def _guard_picked(**over):
    p = {"play_from": "g1", "url": "vid123", "index": 0}
    p.update(over)
    return p


@pytest.fixture
def guard_once_env(monkeypatch):
    monkeypatch.setattr(appmod.config, "all_sources",
                        lambda: [{"id": "g1", "guard": True}])
    monkeypatch.setattr(appmod.providers, "_guard_conf",
                        lambda src: dict(_CONF))
    monkeypatch.setattr(appmod, "_GUARD_RESOLVE_TIMEOUT", 0.3, raising=False)
    return guard


def test_resolve_guard_once_detail_timeout_sets_guard_error(monkeypatch, guard_once_env):
    def slow_detail(conf, vod_id):
        time.sleep(2)
        return {}

    monkeypatch.setattr(guard_once_env, "detail", slow_detail)
    picked = _guard_picked()

    async def _run():
        # 在协程内计时：asyncio.run 收尾会等默认执行器的残留线程，
        # 外层计时会被慢线程拖到 2s，量不到 wait_for 的 0.3s。
        t0 = time.time()
        r = await appmod._resolve_guard_once(picked)
        return r, time.time() - t0

    ret, el = asyncio.run(_run())
    assert ret is False
    assert el < 1.5
    assert picked.get("guard_error"), "detail 超时应写 guard_error"


def test_resolve_guard_once_play_error_sets_guard_error(monkeypatch, guard_once_env):
    det = {"vod_play_from": "L1", "vod_play_url": "E1$ep1"}
    monkeypatch.setattr(guard_once_env, "detail", lambda conf, vid: det)
    monkeypatch.setattr(guard_once_env, "play",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("play boom")))
    picked = _guard_picked()
    assert asyncio.run(appmod._resolve_guard_once(picked)) is False
    assert "play boom" in str(picked.get("guard_error", ""))


def test_resolve_guard_once_success_clears_guard_error(monkeypatch, guard_once_env):
    det = {"vod_play_from": "L1", "vod_play_url": "E1$ep1"}
    monkeypatch.setattr(guard_once_env, "detail", lambda conf, vid: det)
    monkeypatch.setattr(guard_once_env, "play",
                        lambda conf, line, vid: {"url": "http://cdn/x.m3u8"})
    picked = _guard_picked(guard_error="stale")
    assert asyncio.run(appmod._resolve_guard_once(picked)) is True
    assert "guard_error" not in picked
    assert picked["url"] == "http://cdn/x.m3u8"


# ---------------------------------------------------------------- 5. msearch

def test_msearch_bad_detail_does_not_raise(monkeypatch):
    monkeypatch.setattr(guard, "site", lambda key: dict(_CONF))
    monkeypatch.setattr(guard, "detail", lambda conf, vid: {"vod_play_from": "L1"})
    monkeypatch.setattr(guard, "parse_episodes",
                        lambda det: (_ for _ in ()).throw(RuntimeError("bad det")))
    assert asyncio.run(appmod._msearch_resolve_one("K1", "vid1", 0)) is None


def test_msearch_one_bad_branch_does_not_kill_race(monkeypatch):
    monkeypatch.setattr(appmod, "_MSEARCH_CACHE", {}, raising=False)
    monkeypatch.setattr(guard, "search",
                        lambda conf, kw, quick, pg: [{"vod_id": "vid"}])
    monkeypatch.setattr(appmod, "_msearch_targets",
                        lambda: [{"key": "K1", "conf": {}},
                                 {"key": "K2", "conf": {}}])

    async def fake_resolve_one(key, vid, want_idx, pref_line=""):
        if key == "K1":
            raise RuntimeError("K1 boom")
        await asyncio.sleep(0.3)
        return key, "L2", "ep2", {"url": "http://cdn/x.m3u8", "parse": 0}

    monkeypatch.setattr(appmod, "_msearch_resolve_one", fake_resolve_one)
    picked = {"url": "msearch:###片名###海报", "index": 0}
    assert asyncio.run(appmod._resolve_guard_msearch(picked)) is True
    assert picked["url"] == "http://cdn/x.m3u8"
    assert picked["play_from"] == "guard-K2:L2"


# ---------------------------------------------------------------- 6. guard_native shield

def _fake_wait_for_factory(seen):
    async def fake_wait_for(awaitable, timeout=None):
        seen.append(awaitable)
        raise asyncio.TimeoutError()
    return fake_wait_for


def test_gn_category_page_timeout_does_not_cancel_inflight(monkeypatch):
    async def slow():
        await asyncio.sleep(30)
        return [{"vod_id": "v1"}], True

    async def _run():
        task = asyncio.ensure_future(slow())
        seen = []
        monkeypatch.setattr(gnmod.asyncio, "wait_for", _fake_wait_for_factory(seen))
        monkeypatch.setattr(gnmod, "_gn_category_task", lambda *a, **k: task)
        monkeypatch.setattr(gnmod, "_gn_category_rows", lambda *a, **k: ([], False))
        monkeypatch.setattr(gnmod, "_gn_catalog_max_page", lambda *a, **k: 0)
        monkeypatch.setattr(gnmod, "_gn_class_is_tv", lambda *a, **k: False)
        out = await gnmod._gn_category_page("K1", "t1", "电影", 1)
        # D 组修复：失败返回 handled=False（原 True 会让客户端无尽翻页）。
        assert out == ([], False, True)
        assert seen and seen[0] is not task, "wait_for 必须包 shield，不能直接等共享任务"
        assert not task.done() and not task.cancelled(), "超时不能取消共享 inflight 任务"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_gn_warm_worker_timeout_does_not_cancel_inflight(monkeypatch):
    async def slow():
        await asyncio.sleep(30)
        return [{"vod_id": "v1"}], True

    async def _run():
        task = asyncio.ensure_future(slow())
        seen = []
        monkeypatch.setattr(gnmod.asyncio, "wait_for", _fake_wait_for_factory(seen))
        monkeypatch.setattr(gnmod, "_gn_category_task", lambda *a, **k: task)
        monkeypatch.setattr(gnmod, "_gn_category_rows", lambda *a, **k: ([], False))
        monkeypatch.setattr(gnmod, "_gn_classes_meta",
                            lambda: {"K1": {"classes": [{"name": "电影", "tid": "t1"}]}})
        monkeypatch.setattr(gnmod, "_gn_is_film_class", lambda *a, **k: True)
        monkeypatch.setattr(gnmod, "_gn_class_is_tv", lambda *a, **k: False)
        monkeypatch.setattr(gnmod, "_GN_WARM_GAP", 0)
        await gnmod._gn_warm_worker()
        assert seen and seen[0] is not task, "warm worker 也必须 shield 共享任务"
        assert not task.done() and not task.cancelled()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------- 7. 专用执行器：刻意不做
# guard 的同步调用经 app.py / guard_native.py 十余处 asyncio.to_thread 进入，
# 各处共享默认执行器；单建一个 2~4 worker 的 guard 专用池要改全部调用点，
# 且 _bridge_sem 本来已把 guard 并发限在 2，专用池几乎不带来额外隔离收益，
# 反而改动面大、易改错并发语义。故本批不做，只修上面 1~6、8。
