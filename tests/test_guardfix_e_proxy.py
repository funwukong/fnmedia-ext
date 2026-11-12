"""guardfix-e：proxy 侧复现测试（runner_errors / 调用客户端 / guard /
drpy / catvod / spiders / providers）。

全部用假子进程/假 HTTP，不碰真实 runner、docker 与网络。
"""
import asyncio
import importlib
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import runner_errors  # noqa: E402
from runner_errors import RunnerError, RunnerTransportError, kind_of  # noqa: E402

PY = sys.executable
HANG_SCRIPT = "import sys,time; sys.stdin.readline(); time.sleep(60)"
ECHO_SCRIPT = (
    "import sys,json; "
    "line=sys.stdin.readline(); "
    "req=json.loads(line); "
    "sys.stdout.write(json.dumps({'id':req['id'],'result':{'ok':1}})+'\\n'); "
    "sys.stdout.flush()"
)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, json_raises=False):
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("No JSON object could be decoded")
        return self._payload


# ---------------- 5. 错误分类 ----------------

@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("x"), "timeout"),
    (RuntimeError("timed out after 30s"), "timeout"),
    (RuntimeError("ClassNotFoundException: com.x.Y"), "jar"),
    (RuntimeError("DexPathList empty"), "jar"),
    (RuntimeError("spider boom"), "spider"),
    (httpx.ConnectError("connect failed", request=None), "fatal"),
])
def test_kind_of(exc, expected):
    assert kind_of(exc) == expected


def test_runner_error_carries_kind_and_orig():
    e = RunnerError("boom", kind="jar", orig="ClassNotFoundException")
    assert e.kind == "jar" and e.orig == "ClassNotFoundException"
    assert isinstance(e, RuntimeError)


# ---------------- 5/9. rclient ----------------

def test_rclient_health_removed():
    rclient = importlib.import_module("runner" + "_client")
    assert not hasattr(rclient, "health"), \
        "health() 全仓无调用方，应删除（guard.health 自有更紧超时的实现）"


def test_rclient_non_json_is_runner_http_status(monkeypatch):
    rclient = importlib.import_module("runner" + "_client")
    monkeypatch.setattr(rclient, "_URL", "http://127.0.0.1:9100")
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _FakeResp(502, json_raises=True))
    with pytest.raises(RuntimeError, match=r"runner HTTP 502"):
        rclient.invoke({"runtime": "node", "method": "home"})


def test_rclient_business_error_not_truncated_and_kind_kept(monkeypatch):
    rclient = importlib.import_module("runner" + "_client")
    monkeypatch.setattr(rclient, "_URL", "http://127.0.0.1:9100")
    long_err = "x" * 1000
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _FakeResp(200, {"ok": False, "error": long_err,
                                        "kind": "jar",
                                        "error_class": "ClassNotFoundException"}))
    with pytest.raises(RunnerError) as ei:
        rclient.invoke({"runtime": "node", "method": "home"})
    assert len(str(ei.value)) >= 1000
    assert ei.value.kind == "jar"
    assert ei.value.orig == "ClassNotFoundException"


def test_rclient_transport_is_distinct_type(monkeypatch):
    rclient = importlib.import_module("runner" + "_client")
    monkeypatch.setattr(rclient, "_URL", "http://127.0.0.1:9100")

    def _boom(*a, **k):
        raise httpx.ConnectError("refused", request=None)

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(RunnerTransportError):
        rclient.invoke({"runtime": "node", "method": "home"})


# ---------------- 5. guard._runner_post / _call ----------------

@pytest.fixture()
def _guard(monkeypatch):
    guard = importlib.import_module("guard")
    monkeypatch.setattr(guard, "_mark_android_used", lambda: None)
    return guard


def test_guard_runner_post_non_json(monkeypatch, _guard):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResp(503, json_raises=True))
    with pytest.raises(RuntimeError, match=r"runner HTTP 503"):
        _guard._runner_post("/invoke", {"method": "home"})


def test_guard_runner_post_business_error_full_text_and_kind(monkeypatch, _guard):
    long_err = "y" * 900
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _FakeResp(200, {"ok": False, "error": long_err,
                                        "kind": "spider",
                                        "error_class": "SpiderException"}))
    with pytest.raises(RunnerError) as ei:
        _guard._runner_post("/invoke", {"method": "home"})
    assert len(str(ei.value)) >= 900
    assert ei.value.kind == "spider"


def test_guard_call_timeout_does_not_count_bridge_fail(monkeypatch, _guard):
    _guard._bridge_fail = 0

    def _boom(path, payload, timeout=None):
        raise RunnerError("spider timeout after 22s", kind="timeout")

    monkeypatch.setattr(_guard, "_runner_post", _boom)
    site = {"key": "K", "class": "com.x.Y", "ext": ""}
    with pytest.raises(RunnerError):
        _guard._call(site, "home", filter=True)
    assert _guard._bridge_fail == 0, "timeout 不应计入 _bridge_fail、不触发重启"


def test_guard_call_jar_error_repushes_and_retries_once(monkeypatch, _guard):
    pushed = []
    monkeypatch.setattr(_guard, "_push_user_jar",
                        lambda: pushed.append(1) or True)
    calls = {"n": 0}

    def _flaky(path, payload, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RunnerError("ClassNotFoundException", kind="jar")
        return {"ok": 1}

    monkeypatch.setattr(_guard, "_runner_post", _flaky)
    _guard._bridge_fail = 0
    site = {"key": "K", "class": "com.x.Y", "ext": ""}
    assert _guard._call(site, "home", filter=True) == {"ok": 1}
    assert pushed == [1] and calls["n"] == 2


# ---------------- 1. catvod / drpy Sidecar ----------------

def test_catvod_sidecar_call_times_out():
    catvod = importlib.import_module("catvod")
    sc = catvod.Sidecar()
    sc._proc = None
    # 用挂起脚本直接替换启动命令：借 _start 的 Popen 点不可注入，改为手工建进程
    import subprocess
    sc._proc = subprocess.Popen(
        [PY, "-c", HANG_SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    # catvod.Sidecar.call 会调 _start（进程活着即返回），随后写请求读响应
    import threading as _th
    t0 = time.time()
    with pytest.raises(TimeoutError):
        sc.call({"method": "home"}, timeout=0.5)
    assert time.time() - t0 < 5.0
    assert sc._proc is None, "超时后进程必须被 stop 掉"
    sc.stop()


def test_drpy_sidecar_call_times_out(monkeypatch):
    drpy = importlib.import_module("drpy")
    import subprocess
    sc = drpy.Sidecar()
    sc._proc = subprocess.Popen(
        [PY, "-c", HANG_SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    sc._engine = drpy.drpy_engine()
    t0 = time.time()
    with pytest.raises(TimeoutError):
        sc.call({"method": "home"}, timeout=0.5)
    assert time.time() - t0 < 5.0
    assert sc._proc is None
    sc.stop()


# ---------------- 6. drpy 回退只针对传输层 ----------------

def test_drpy_call_falls_back_only_on_transport(monkeypatch):
    drpy = importlib.import_module("drpy")
    rclient = importlib.import_module("runner" + "_client")
    local = []

    monkeypatch.setattr(rclient, "enabled", lambda: True)
    monkeypatch.setattr(drpy, "_sidecar", type("S", (), {
        "call": staticmethod(lambda payload, timeout=90.0: local.append(1) or {"ok": 1})})())

    # 传输层异常 → 回退本地
    monkeypatch.setattr(rclient, "invoke",
                        lambda payload: (_ for _ in ()).throw(
                            RunnerTransportError("runner HTTP 502")))
    assert drpy._call({"api": "x"}, "home") == {"ok": 1}
    assert local == [1]

    # 业务错误 → 直接上抛，不回退、不叠加耗时
    monkeypatch.setattr(rclient, "invoke",
                        lambda payload: (_ for _ in ()).throw(
                            RunnerError("spider boom", kind="spider")))
    with pytest.raises(RunnerError, match="spider boom"):
        drpy._call({"api": "x"}, "home")
    assert local == [1], "业务错误不应再回退本地"


# ---------------- 8. spiders._norm_class ----------------

def test_norm_class_keeps_trailing_digit_class_name():
    spiders = importlib.import_module("spiders")
    assert spiders._norm_class("", "Spider2") == \
        "com.github.catvod.spider.Spider2"
    assert spiders._norm_class("com.github.catvod.spider.Spider2", "Spider2") == \
        "com.github.catvod.spider.Spider2"


def test_norm_class_uses_explicit_base_key():
    spiders = importlib.import_module("spiders")
    assert spiders._norm_class("", "XBPQ2", base_key="XBPQ") == \
        "com.github.catvod.spider.XBPQ"


def test_import_sites_stores_base_key_and_keeps_digit_class(tmp_path, monkeypatch):
    spiders = importlib.import_module("spiders")
    monkeypatch.setattr(spiders, "_SITES_FILE", tmp_path / "spider_sites.json")
    monkeypatch.setattr(spiders, "_JAR_DIR", tmp_path / "jars")
    monkeypatch.setattr(spiders, "_sites", None)
    items = [
        {"key": "Spider2", "name": "S2",
         "cls": "com.github.catvod.spider.Spider2",
         "ext": "", "jar": "http://x/y.jar"},
        {"key": "XBPQ2", "name": "X2",
         "cls": "com.github.catvod.spider.XBPQ",
         "ext": "", "jar": "http://x/y.jar"},
    ]
    spiders.import_sites(items, enable=False)
    sites = spiders.load_sites()
    assert sites["Spider2"]["class"] == "com.github.catvod.spider.Spider2"
    assert sites["XBPQ2"]["class"] == "com.github.catvod.spider.XBPQ"
    assert sites["XBPQ2"]["base_key"] == "XBPQ"
    assert sites["Spider2"]["base_key"] == "Spider2"


# ---------------- 6. providers drpy_search_source 8s 上限 ----------------

def test_drpy_search_source_bounded_by_8s(monkeypatch):
    drpy = importlib.import_module("drpy")
    import providers
    monkeypatch.setattr(drpy, "search",
                        lambda site, kw, quick, pg: time.sleep(30) or [])
    monkeypatch.setattr(providers, "_drpy_site", lambda s: {"api": "x"})
    # 不用 asyncio.run：它退出时会等 to_thread 的 30s 线程跑完；
    # 手动建 loop 只量 coroutine 本身的返回耗时。
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        t0 = time.time()
        out = loop.run_until_complete(
            providers.drpy_search_source(None, {"id": "d"}, "庆余年"))
        dt = time.time() - t0
    finally:
        # 8s 超时后 shield 的 to_thread 任务仍在后台跑：清掉挂起任务，
        # 免得 loop.close() 报 "Task was destroyed but it is pending"。
        for t in asyncio.all_tasks(loop):
            t.cancel()
        try:
            loop.run_until_complete(
                asyncio.gather(*asyncio.all_tasks(loop),
                               return_exceptions=True))
        except Exception:  # noqa: BLE001
            pass
        loop.close()
        asyncio.set_event_loop(None)
    assert out == []
    assert dt < 15.0, f"drpy 搜索应 8s 内返回，实际 {dt:.1f}s"
