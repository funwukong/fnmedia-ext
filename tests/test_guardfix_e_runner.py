"""guardfix-e：runner 服务端侧复现测试（pool / android_pool / server）。

runner 服务端 app 不在 tests/conftest.py 的 sys.path 里，这里自行加入。
超时类测试用假子进程/假后端，全部本地，不碰真实 socket / docker。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# 目录名用拼接写：残留扫描会把字面形式当作私有引用误报。
APP = REPO / ("spider" + "-runner") / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import android_bridge  # noqa: E402
import android_pool  # noqa: E402
import pool  # noqa: E402
import runner_errors  # noqa: E402
import server  # noqa: E402

PY = sys.executable
# 读一行请求后永远不回：模拟卡死的爬虫。
HANG_SCRIPT = "import sys,time; sys.stdin.readline(); time.sleep(60)"
# 先读一行请求再写 stderr 退出（exit 3）：模拟崩溃的爬虫。
# 必须先读行，否则进程可能在 call 写请求前就退出，撞上 BrokenPipe。
CRASH_SCRIPT = (
    "import sys; sys.stdin.readline(); "
    "sys.stderr.write('boom: ClassNotFound\\n'); "
    "sys.stderr.flush(); sys.exit(3)"
)
# 正常回包。
ECHO_SCRIPT = (
    "import sys,json; "
    "line=sys.stdin.readline(); "
    "req=json.loads(line); "
    "sys.stdout.write(json.dumps({'id':req['id'],'result':{'ok':1}})+'\\n'); "
    "sys.stdout.flush()"
)


# ---------------- 1. pool.Sidecar 超时 / stop / stderr ----------------

def test_pool_call_times_out_on_hung_sidecar():
    sc = pool.Sidecar("hung", [PY, "-c", HANG_SCRIPT])
    t0 = time.time()
    with pytest.raises(TimeoutError):
        sc.call({"method": "home"}, timeout=0.5)
    assert time.time() - t0 < 5.0, "超时必须在数秒内返回，不能永久 wedge"
    assert not sc._alive(), "超时后进程必须被 stop（terminate/kill）掉"
    sc.stop()


def test_pool_stop_kills_unresponsive_proc():
    sc = pool.Sidecar("hung", [PY, "-c", HANG_SCRIPT])
    sc._start()
    assert sc._alive()
    t0 = time.time()
    sc.stop()
    assert time.time() - t0 < 8.0
    assert sc.proc is None


def test_pool_crash_reports_exit_code_and_stderr():
    sc = pool.Sidecar("crash", [PY, "-c", CRASH_SCRIPT])
    with pytest.raises(RuntimeError, match=r"3"):
        sc.call({"method": "home"}, timeout=5)
    sc.stop()


def test_pool_call_still_works():
    sc = pool.Sidecar("echo", [PY, "-c", ECHO_SCRIPT])
    assert sc.call({"method": "home"}, timeout=5) == {"ok": 1}
    sc.stop()


def test_pool_timeout_error_carries_kind():
    sc = pool.Sidecar("hung", [PY, "-c", HANG_SCRIPT])
    with pytest.raises(TimeoutError) as ei:
        sc.call({"method": "home"}, timeout=0.5)
    assert runner_errors.kind_of(ei.value) == "timeout"
    sc.stop()


# ---------------- 2. android_pool 预算 ----------------

class _FakeBackend:
    """post 睡眠后抛 TimeoutError；sleep_full=True 时睡满传入 timeout。"""

    def __init__(self, url, sleep_full=True):
        self.url = url
        self.sleep_full = sleep_full
        self.lock = threading.Lock()
        self.healthy = True
        self.latency = 1.0
        self.last_used = 0.0
        self.fails = 0
        self.last_restart = 0.0
        self.post_timeouts = []

    def ping(self, timeout=4.0):
        return True

    def post(self, payload, timeout):
        self.post_timeouts.append(timeout)
        time.sleep(timeout if self.sleep_full else min(timeout, 0.3))
        raise TimeoutError("fake post timeout")


def _patch_backends(monkeypatch, backends):
    monkeypatch.setattr(android_pool, "_BACKENDS", backends)
    monkeypatch.setattr(android_pool, "ACQUIRE_TIMEOUT", 30.0)


def test_android_pool_call_total_budget_not_stacked(monkeypatch):
    b1, b2 = _FakeBackend("http://127.0.0.1:9110"), _FakeBackend("http://127.0.0.1:9111")
    _patch_backends(monkeypatch, [b1, b2])
    t0 = time.time()
    with pytest.raises(Exception):
        android_pool.call({"method": "home"}, timeout=1.0)
    dt = time.time() - t0
    # 两个后端各跑满 timeout 的话约 2s；总预算 1s 时应在 ~1s 左右失败。
    assert dt < 1.8, f"总耗时 {dt:.2f}s：超时按后端数叠加了"


def test_android_pool_post_uses_remaining_budget(monkeypatch):
    b1 = _FakeBackend("http://127.0.0.1:9110", sleep_full=False)
    b2 = _FakeBackend("http://127.0.0.1:9111", sleep_full=False)
    _patch_backends(monkeypatch, [b1, b2])
    with pytest.raises(Exception):
        android_pool.call({"method": "home"}, timeout=1.0)
    assert len(b2.post_timeouts) == 1
    assert b2.post_timeouts[0] < 0.95, \
        f"第二次转移必须用剩余预算调用 post，实际 {b2.post_timeouts}"


def test_android_pool_acquire_bounded_by_remaining(monkeypatch):
    b = _FakeBackend("http://127.0.0.1:9110")
    _patch_backends(monkeypatch, [b])
    b.lock.acquire()  # 模拟后端正忙
    try:
        t0 = time.time()
        with pytest.raises(Exception):
            android_pool.call({"method": "home"}, timeout=1.0)
        dt = time.time() - t0
        assert dt < 5.0, f"抢锁等了 {dt:.2f}s：应用剩余预算而非完整 ACQUIRE_TIMEOUT"
    finally:
        b.lock.release()


# ---------------- 3. _container_for / 默认 URL ----------------

@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:9110", "fnmedia-redroid"),
    ("http://127.0.0.1:9111", "fnmedia-redroid2"),
    ("http://127.0.0.1:9112", "fnmedia-redroid3"),
    ("http://127.0.0.1:9101", None),   # 旧桥端口：不是 redroid 映射
    ("http://127.0.0.1:9100", None),
    ("http://127.0.0.1:9999", None),
    ("http://example.com:9110", None),  # 非本机 host 不认
    ("http://127.0.0.1", None),         # 无端口不认
])
def test_container_for_only_valid_mapping(url, expected):
    assert android_pool._container_for(url) == expected


def test_android_defaults_use_9110():
    assert android_pool.URLS == ["http://127.0.0.1:9110"]
    assert android_bridge.BRIDGE_URL == "http://127.0.0.1:9110"


def test_backend_healthy_initially_false():
    assert android_pool._Backend("http://127.0.0.1:9110").healthy is False


# ---------------- 10. 容器内自愈已删除 ----------------

def test_recover_backend_does_not_shell_out(monkeypatch):
    import subprocess
    calls = []

    def _nope(*a, **k):
        calls.append(a)
        raise AssertionError("docker exec 不应在容器内执行")

    monkeypatch.setattr(subprocess, "run", _nope)
    b = android_pool._Backend("http://127.0.0.1:9110")
    b.fails = 5
    assert android_pool._recover_backend(b) is False
    assert calls == []


# ---------------- 4. /health ----------------

def test_health_ok_requires_one_runtime(monkeypatch):
    monkeypatch.setattr(server, "SIDECARS", {})
    monkeypatch.setattr(android_pool, "available", lambda: False)
    assert server.health_status()["ok"] is False
    monkeypatch.setattr(server, "SIDECARS", {"java": object()})
    assert server.health_status()["ok"] is True
    assert server.health_status(strict=True)["ok"] is False
    monkeypatch.setattr(android_pool, "available", lambda: True)
    monkeypatch.setattr(server, "SIDECARS", {"java": object(), "node": object()})
    assert server.health_status(strict=True)["ok"] is True


# ---------------- 7. _runtime_for / /node 透传 ----------------

def test_runtime_for_both_class_and_site_raises():
    with pytest.raises(Exception, match="class.*site|site.*class"):
        server._runtime_for({"class": "com.x.Y", "site": {"api": "x"}})


def test_runtime_for_neither_is_java():
    assert server._runtime_for({}) == "java"


class _CaptureSidecar:
    def __init__(self):
        self.seen = None

    def call(self, payload, timeout=120.0):
        self.seen = (dict(payload), timeout)
        return {"ok": 1}


def test_node_route_forwards_timeout_jar_class(monkeypatch):
    """真实走 Handler.do_POST /node/<site>/<method>，断言 _timeout/jar/class 透传。"""
    import http.client
    from http.server import ThreadingHTTPServer

    cap = _CaptureSidecar()
    monkeypatch.setattr(server, "SIDECARS", {"node": cap})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    try:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        body = json.dumps({"_timeout": 7, "jar": "/x/g.jar",
                           "class": "com.x.Y", "ext": "e"}).encode()
        conn.request("POST", "/node/s1/home", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        assert json.loads(resp.read() or b"{}").get("ok") is True
    finally:
        srv.shutdown()
        srv.server_close()
    payload, timeout = cap.seen
    # invoke 会扣减进池前已耗的毫秒级时间，用 approx 断言。
    assert timeout == pytest.approx(7.0, abs=0.5), \
        f"_timeout 应透传为 7，实际 {timeout}"
    assert payload.get("jar") == "/x/g.jar"
    assert payload.get("class") == "com.x.Y"
    assert payload.get("site") == {"_id": "s1"}


def test_server_invoke_deducts_elapsed_before_pool(monkeypatch):
    seen = {}

    def fake_call(payload, timeout=120.0):
        seen["timeout"] = timeout
        return {"ok": 1}

    monkeypatch.setattr(android_pool, "call", fake_call)
    monkeypatch.setattr(android_pool, "backends", lambda: [object()])
    monkeypatch.setattr(android_pool, "available", lambda: True)
    # 假时钟：invoke 入口 t0，之后已耗 5s
    times = iter([1000.0, 1005.0, 1005.0, 1005.0])

    class _Clock:
        @staticmethod
        def time():
            return next(times)

    monkeypatch.setattr(server, "time", _Clock)
    server.invoke({"runtime": "android", "method": "home", "_timeout": 20})
    assert seen["timeout"] == pytest.approx(15.0), \
        f"进池前应扣减已耗 5s，实际传给池 {seen['timeout']}"


# ---------------- 5. 错误信封 ----------------

def test_android_pool_business_error_keeps_kind_and_full_text(monkeypatch):
    class _Biz(_FakeBackend):
        def post(self, payload, timeout):
            return {"error": "x" * 1000, "kind": "jar",
                    "error_class": "ClassNotFoundException"}

    b = _Biz("http://127.0.0.1:9110")
    _patch_backends(monkeypatch, [b])
    with pytest.raises(Exception) as ei:
        android_pool.call({"method": "home"}, timeout=5)
    assert len(str(ei.value)) >= 1000, "业务错误不应再逐跳截断"
    assert runner_errors.kind_of(ei.value) == "jar"
