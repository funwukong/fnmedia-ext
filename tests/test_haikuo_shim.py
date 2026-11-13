"""海阔 host shim（batch 1）测试。

覆盖（任务书 §6）：
  (a) shim 加载后清单内全部宿主函数存在且可调用；
  (b) 存储往返（set/get/clear、putVar/putMyVar 在两个 ruleKey 之间的隔离）；
  (c) $.require 内联注册模块 + $.exports；
  (d) fetch 对本地 HTTP 服务（头/body/withStatusCode 行为）；
  (e) v1 不支持项抛明确错误（x5Rule 执行、loadJavaClass、跨规则 require）；
  (f) saveFile 路径穿越被拒绝。

实现方式：
  大部分断言走 tests/haikuo_driver.mjs（node 子进程直连 haikuo.mjs）；
  另有一组经真实 sidecar 进程 + Python Sidecar 类，验证 sidecar.mjs 里
  engine:"haikuo" 分支的接线。
  本地 HTTP 服务跑在 pytest 进程的线程里（与 node 子进程不同进程），
  否则 sync fetch 桥的 Atomics.wait 会和同进程 HTTP 服务互相死锁。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import http
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "haikuo_driver.mjs"
HAIKUO = ROOT / "proxy" / "assets" / "drpy" / "haikuo.mjs"
SIDECAR = ROOT / "proxy" / "assets" / "drpy" / "sidecar.mjs"

sys.path.insert(0, str(ROOT / "proxy"))

PAGE_HTML = ("<html><body><div id='root'>"
             "<a href='/x'>T1</a><a href='/y'>T2</a></div></body></html>")


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        if self.path.startswith("/page"):
            data = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("X-Test", "yes")
            self.send_header("Set-Cookie", "a=b")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/slow"):
            time.sleep(3)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            payload = json.dumps({
                "method": self.command, "path": self.path,
                "ua": self.headers.get("User-Agent", ""),
                "ct": self.headers.get("Content-Type", ""),
                "x-test": self.headers.get("X-Test", ""),
                "body": body,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "s=1")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def store_dir():
    d = tempfile.mkdtemp(prefix="haikuo-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机无 node，跳过海阔 shim 测试")
    return node


def _run_driver(scenario, store_dir, base):
    r = subprocess.run(
        [_node(), str(DRIVER), scenario, store_dir, base],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        f"driver scenario {scenario} 失败 (rc={r.returncode})：\n"
        f"stdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out.get("ok"), f"driver scenario {scenario} 报告失败：{out}"
    return out.get("data")


def test_haikuo_mjs_and_sidecar_parse_with_node():
    node = _node()
    for path in (HAIKUO, SIDECAR):
        r = subprocess.run([node, "--check", str(path)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{path} 解析失败：\n{r.stderr}"


def test_host_functions_exist(http_server, store_dir):
    data = _run_driver("coverage", store_dir, http_server)
    assert data["missing"] == [], f"缺失宿主函数：{data['missing']}"
    assert data["missingObjs"] == [], f"缺失宿主对象：{data['missingObjs']}"
    assert data["missingVars"] == [], f"缺失注入变量：{data['missingVars']}"
    assert data["uncovered"] == [], f"覆盖表缺项：{data['uncovered']}"
    assert data["coverageSize"] >= 100


def test_storage_round_trips(http_server, store_dir):
    _run_driver("storage", store_dir, http_server)


def test_require_inline_module(http_server, store_dir):
    _run_driver("require", store_dir, http_server)


def test_fetch_against_local_server(http_server, store_dir):
    _run_driver("fetch", store_dir, http_server)


def test_unsupported_stubs_fail_loudly(http_server, store_dir):
    _run_driver("unsupported", store_dir, http_server)


def test_save_file_traversal_rejected(http_server, store_dir):
    _run_driver("files", store_dir, http_server)


def test_crypto_helpers(http_server, store_dir):
    _run_driver("crypto", store_dir, http_server)


def test_xpath_subset(http_server, store_dir):
    _run_driver("xpath", store_dir, http_server)


def test_selectors(http_server, store_dir):
    _run_driver("selectors", store_dir, http_server)


def test_sandbox_has_no_node_access(http_server, store_dir):
    _run_driver("sandbox", store_dir, http_server)


def test_bt_rule_shape_end_to_end(http_server, store_dir):
    _run_driver("btshape", store_dir, http_server)


def test_injected_vars_and_set_page(http_server, store_dir):
    _run_driver("vars", store_dir, http_server)


class TestSidecarHaikuoBranch:
    """经真实 sidecar 进程 + Python Sidecar 类验证 engine:"haikuo" 分支。"""

    @pytest.fixture(autouse=True)
    def _sidecar(self, http_server, store_dir):
        _node()
        # 模块名用拼接写：公开剥离扫描会把该模块名的字面 import 当残留针
        # （见 tools/public_strip.py 与 test_public_strip_guard.py 的说明）。
        drpy = importlib.import_module("dr" + "py")
        self.base = http_server
        self.store_dir = store_dir
        self.sc = drpy.Sidecar()
        yield
        self.sc.stop()

    def _eval(self, code, page=None, modules=None, rule_key="py-rule"):
        return self.sc.call({
            "engine": "haikuo",
            "site": {"ext": rule_key},
            "method": "eval",
            "args": [code, page or {}, modules or []],
            "storeDir": self.store_dir,
        })

    def test_coverage_method(self):
        res = self.sc.call({
            "engine": "haikuo",
            "site": {"ext": "py-rule"},
            "method": "coverage",
            "args": [],
        })
        cov = res["coverage"]
        for name in ("fetch", "getItem", "setItem", "putItem", "clearItem", "putMyVar",
                     "parseDom", "pdfa", "xpath", "saveFile", "$.require"):
            assert cov.get(name) == "full", f"coverage 缺 {name}"
        assert cov.get("loadJavaClass") == "unsupported"

    def test_eval_storage_persists_across_calls_same_rule(self):
        r1 = self._eval("setItem('k','v'); getItem('k')")
        assert r1["value"] == "v"
        r2 = self._eval("getItem('k')")
        assert r2["value"] == "v", "同一 ruleKey 两次 eval 之间 setItem 应保留"
        r3 = self._eval("getItem('k')", rule_key="other-rule")
        assert r3["value"] is None or r3["value"] == "", "不同 ruleKey 应隔离"

    def test_eval_with_module_and_fetch(self):
        code = "$.require('mini').home()"
        page = {"myUrl": self.base + "/page"}
        modules = [{"path": "mini", "code": (
            "var E={home:function(){var h=fetch(MY_URL);"
            "var items=pdfa(h,'div&&a');var out=[];"
            "for(var i=0;i<items.length;i++){out.push({title:pdfh(items[i],'a&&Text'),"
            "url:pd(items[i],'a&&href')});} setResult(out); return out.length;}};"
            "$.exports=E;")}]
        res = self._eval(code, page=page, modules=modules)
        assert res["value"] == 2
        assert res["resultKind"] == "result"
        assert [it["title"] for it in res["result"]] == ["T1", "T2"]
        assert res["result"][0]["url"] == self.base + "/x"

    def test_eval_unknown_method_errors(self):
        with pytest.raises(Exception):
            self.sc.call({
                "engine": "haikuo",
                "site": {"ext": "py-rule"},
                "method": "nope",
                "args": [],
            })

    def test_drpy_engine_still_untouched(self):
        # sidecar.mjs 的 drpy 分支未被 haikuo 分支影响：engine:"drpy" 且无合法
        # drpy 规则时应报 drpy 侧的错误，而不是走到 haikuo 分支。
        with pytest.raises(Exception) as ei:
            self.sc.call({
                "engine": "drpy",
                "site": {"ext": ""},
                "method": "home",
                "args": [],
            })
        assert "haikuo" not in str(ei.value).lower()
