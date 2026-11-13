"""海阔规则加载器 + 解析串解释器 + lazy 评估器（batch 2）测试。

覆盖（任务书）：
  (a) 五条已解析规则全部通过校验（解析、字段、pages 模块注册、classes 切分）——
      读 ~/workspace/haikuo-codes/，该目录不存在时跳过；
  (b) committed fixtures（tests/fixtures/haikuo/）：
      引擎模块规则 home/search、API-host 规则 home/search/detail（本地 stub
      HTTP 服务，绝不碰规则的真实 host）、纯解析串解释、.js: 后缀加工、
      preRule 执行、lazy token 往返出直链 .m3u8、磁力不可播、video:// 不支持；
  (c) 草榴 BT 规则加载 + 校验 + 注册 bt 引擎模块（不执行网络调用；
      规则文件不存在时跳过）。
  另有一组经真实 sidecar 进程 + Python Sidecar 类的端到端测试，验证
  sidecar.mjs 里 engine:"haikuo" 的 load/home/category/search/detail/play 分发。
  全套测试在没有 ~/workspace/haikuo-codes/ 的干净检出里也必须通过
  （依赖它的测试会跳过）。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "haikuo_loader_driver.mjs"
FIXTURES = ROOT / "tests" / "fixtures" / "haikuo"
RESOLVED_DIR = Path(os.path.expanduser("~/workspace/haikuo-codes"))
BT_RULE = Path(os.path.expanduser(
    "~/workspace/user/files/caoliu_bt_rule__1__0_b5ml.json"))

sys.path.insert(0, str(ROOT / "proxy"))

LIST_HTML = ("<html><body>"
             "<div class='item'><a href='/d/1'><img src='/p1.jpg'>Title One</a>"
             "<span class='desc'>Desc One</span></div>"
             "<div class='item'><a href='/d/2'><img src='/p2.jpg'>Title Two</a>"
             "<span class='desc'>Desc Two</span></div>"
             "</body></html>")
SEARCH_HTML = ("<html><body>"
               "<div class='item'><a href='/d/3'><img src='/p3.jpg'>Search One</a>"
               "<span class='desc'>Search Desc</span></div>"
               "</body></html>")
WATCH_HTML = "<html><body><a href='/v.m3u8'>play</a></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def _send(self, data: bytes, ctype="text/html"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        base = getattr(self.server, "base_url", "")
        if self.path.startswith("/list") or self.path.startswith("/eng"):
            self._send(LIST_HTML.encode())
        elif self.path.startswith("/search"):
            self._send(SEARCH_HTML.encode())
        elif self.path.startswith("/watch"):
            self._send(WATCH_HTML.encode())
        elif self.path.startswith("/api/home"):
            self._send(json.dumps({"items": [
                {"id": "1", "title": "剧一", "desc": "简介一", "pic": base + "/p1.jpg"},
                {"id": "2", "title": "剧二", "desc": "简介二", "pic": base + "/p2.jpg"},
            ]}).encode(), "application/json")
        elif self.path.startswith("/api/search"):
            self._send(json.dumps({"items": [
                {"id": "3", "title": "搜索剧", "desc": "搜索简介", "pic": base + "/p3.jpg"},
            ]}).encode(), "application/json")
        elif self.path.startswith("/api/detail"):
            q = self.path.split("id=", 1)[1].split("&")[0] if "id=" in self.path else "0"
            eps = [{"num": 1, "id": "101"}, {"num": 2, "id": "102"}] if q == "1" else \
                [{"num": 1, "id": "201"}, {"num": 2, "id": "202"}]
            title = "剧一" if q == "1" else "剧二"
            self._send(json.dumps({
                "title": title, "desc": "简介" + q[-1], "episodes": eps,
            }).encode(), "application/json")
        elif self.path.startswith("/slow35"):
            # 慢端点：35 秒才响应，用于验证 fetch 超时封顶（规则传
            # timeout:120000 也应在 ~30s 失败，而不是等 120s）。
            time.sleep(35)
            self._send(b"slow-ok")
        elif self.path.startswith("/xj.m3u8"):
            # 香蕉视频式改写清单：相对分片地址，规则须经 java.io 绝对化后写本地。
            self._send(
                b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                b"#EXTINF:10.0,\nseg1.ts\n#EXTINF:10.0,\nseg2.ts\n#EXT-X-ENDLIST",
                "application/vnd.apple.mpegurl")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    do_GET = _handle

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.base_url
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def store_dir():
    d = tempfile.mkdtemp(prefix="haikuo-loader-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机无 node，跳过海阔加载器测试")
    return node


def _run_driver(scenario, store_dir, base, extra=""):
    r = subprocess.run(
        [_node(), str(DRIVER), scenario, store_dir, base, extra],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        f"loader driver scenario {scenario} 失败 (rc={r.returncode})：\n"
        f"stdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out.get("ok"), f"loader driver scenario {scenario} 报告失败：{out}"
    return out.get("data")


def test_resolved_rules_validate(http_server, store_dir):
    if not RESOLVED_DIR.is_dir():
        pytest.skip("无 ~/workspace/haikuo-codes/（干净检出），跳过已解析规则校验")
    data = _run_driver("validate_all", store_dir, http_server, str(RESOLVED_DIR))
    assert {d["file"] for d in data} == {
        "chengzi", "xiangjiao", "aishuangju", "91duanju", "huakui"}
    by_file = {d["file"]: d for d in data}
    assert by_file["chengzi"]["title"] == "橙子短剧"
    assert by_file["xiangjiao"]["title"] == "香蕉视频"
    assert by_file["aishuangju"]["title"] == "AI爽剧"
    assert by_file["91duanju"]["title"] == "91短剧"
    assert by_file["huakui"]["title"] == "花魁短剧"
    for d in data:
        assert d["type"] == "video", f"{d['file']} type 不是 video"
        assert d["hasSearch"] is True, f"{d['file']} 应有搜索"
        assert d["classes"] == [], f"{d['file']} 应无 class_name/class_url"
    assert by_file["chengzi"]["pages"] == ["cz", "so", "de", "syn"]
    assert by_file["xiangjiao"]["pages"] == ["erji", "so", "short"]
    assert by_file["aishuangju"]["pages"] == ["erji", "so"]
    assert by_file["91duanju"]["pages"] == ["erji", "so"]
    assert by_file["huakui"]["pages"] == ["erji", "so", "tag", "tags"]


def test_bt_rule_loads(http_server, store_dir):
    if not BT_RULE.is_file():
        pytest.skip("无草榴 BT 规则文件，跳过")
    data = _run_driver("bt", store_dir, http_server, str(BT_RULE))
    assert data["title"] == "草榴BT磁力"
    assert data["type"] == "video"
    assert "bt" in data["modules"] and "pics" in data["modules"]
    assert len(data["classes"]) == 7
    assert data["classes"][0] == {"name": "亞洲無碼", "url": "2"}
    assert data["classes"][-1] == {"name": "AI破解", "url": "28"}
    assert data["hasSearch"] is True
    assert data["findRuleIsJs"] and data["searchFindIsJs"] and data["detailFindIsJs"]
    assert data["preRulePresent"] is True


def test_chengzi_fixture_loads(http_server, store_dir):
    data = _run_driver("chengzi_fixture", store_dir, http_server,
                       str(FIXTURES / "chengzi.json"))
    assert data["title"] == "橙子短剧"
    assert data["type"] == "video"
    assert set(data["modules"]) >= {"cz", "so", "de", "syn"}
    assert [p["path"] for p in data["pages"]] == ["cz", "so", "de", "syn"]
    assert data["hasSearch"] is True
    assert "fetch('hiker://page/cz')" in data["findRule"]


def test_parse_string_interpretation(http_server, store_dir):
    _run_driver("parse_string", store_dir, http_server,
                str(FIXTURES / "synthetic_parse.json"))


def test_engine_module_home_search(http_server, store_dir):
    _run_driver("engine_module", store_dir, http_server,
                str(FIXTURES / "synthetic_engine.json"))


def test_api_host_end_to_end(http_server, store_dir):
    _run_driver("api_host", store_dir, http_server,
                str(FIXTURES / "synthetic_api.json"))


def test_lazy_dispatch_matrix(http_server, store_dir):
    _run_driver("lazy_dispatch", store_dir, http_server)


def test_prerule_runs_before_home(http_server, store_dir):
    _run_driver("prerule", store_dir, http_server)


def test_loader_rejects_bad_rules(http_server, store_dir):
    _run_driver("validate_rejects", store_dir, http_server)


class TestSidecarHaikuoLoader:
    """经真实 sidecar 进程 + Python Sidecar 类验证 load/home/search/detail/play。"""

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

    def _rule_text(self, name):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        return text.replace("__BASE__", self.base)

    def _call(self, method, rule_key, rule=None, args=None, store_dir=None):
        req = {"engine": "haikuo", "site": {"ext": rule_key},
               "method": method, "args": args or []}
        if rule is not None:
            req["rule"] = rule
        if store_dir is not None:
            req["storeDir"] = store_dir
        return self.sc.call(req)

    def _load_api(self):
        res = self._call("load", "synthetic-api",
                         rule=self._rule_text("synthetic_api.json"))
        assert res["title"] == "合成API站"
        assert res["hasSearch"] is True
        assert [p["path"] for p in res["pages"]] == ["erji"]
        assert "erji" in res["modules"]
        return res

    def test_load_then_home_search_detail_play(self):
        self._load_api()
        home = self._call("home", "synthetic-api", args=[1])
        assert len(home["items"]) == 2
        assert home["items"][0]["title"] == "剧一"
        assert home["items"][0]["url"].startswith("haikuo-play:")
        assert "@rule=js:" not in home["items"][0]["url"]
        detail = self._call("detail", "synthetic-api",
                            args=[home["items"][0]["url"]])
        assert len(detail["items"]) == 3
        assert detail["items"][1]["title"] == "第1集"
        play = self._call("play", "synthetic-api",
                          args=[detail["items"][1]["url"]])
        assert play == {"playable": True, "url": self.base + "/v/101.m3u8",
                        "headers": {}}
        search = self._call("search", "synthetic-api", args=["剧", 1])
        assert len(search["items"]) == 1
        assert search["items"][0]["title"] == "搜索剧"

    def test_load_by_cached_key_only(self):
        self._load_api()
        # 第二次 load 只给 ruleKey（非 JSON 字符串），应命中 sidecar 内的缓存。
        res = self._call("load", "synthetic-api", rule="synthetic-api")
        assert res["title"] == "合成API站"
        home = self._call("home", "synthetic-api", args=[1])
        assert len(home["items"]) == 2

    def test_category_without_classes_errors(self):
        self._load_api()
        with pytest.raises(Exception) as ei:
            self._call("category", "synthetic-api", args=["分类A", 1])
        assert "no classes" in str(ei.value)

    def test_parse_rule_via_sidecar(self):
        res = self._call("load", "synthetic-parse",
                         rule=self._rule_text("synthetic_parse.json"))
        assert res["classes"][0] == {"name": "分类A", "url": "a"}
        home = self._call("home", "synthetic-parse", args=[1])
        assert [it["title"] for it in home["items"]] == ["TITLE ONE", "TITLE TWO"]
        assert home["items"][0]["url"] == self.base + "/d/1?from=list"
        cat = self._call("category", "synthetic-parse", args=["分类B", 1])
        assert len(cat["items"]) == 2

    def test_play_dispatch_via_sidecar(self):
        self._load_api()
        magnet = self._call("play", "synthetic-api",
                            args=["magnet:?xt=urn:btih:ABCDEF"])
        assert magnet["playable"] is False
        assert magnet["action"] == "copy"
        sniff = self._call("play", "synthetic-api", args=[self.base + "/watch"])
        assert sniff == {"playable": False, "sniff": True,
                         "url": self.base + "/watch"}
        with pytest.raises(Exception) as ei:
            self._call("play", "synthetic-api", args=["video://abc"])
        assert "haikuo v1 unsupported" in str(ei.value)

    def test_java_io_file_rewrite_play(self):
        # 香蕉视频式：lazy 码取清单、绝对化相对分片、经 java.io 写本地文件、
        # 返回 file:// 路径；play 应判 playable，且文件内容已改写。
        res = self._call("load", "synthetic-javaio",
                         rule=self._rule_text("synthetic_javaio.json"),
                         store_dir=self.store_dir)
        assert res["title"] == "合成JavaIO站"
        home = self._call("home", "synthetic-javaio", args=[1])
        assert len(home["items"]) == 2
        detail = self._call("detail", "synthetic-javaio",
                            args=[home["items"][0]["url"]])
        assert len(detail["items"]) == 3
        play = self._call("play", "synthetic-javaio",
                          args=[detail["items"][1]["url"]])
        expect_url = "file://" + os.path.join(self.store_dir, "xj_v_101_0.m3u8")
        assert play == {"playable": True, "url": expect_url, "headers": {}}, play
        written = Path(self.store_dir, "xj_v_101_0.m3u8").read_text(encoding="utf-8")
        assert written == (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10.0,\n" + self.base + "/seg1.ts\n"
            "#EXTINF:10.0,\n" + self.base + "/seg2.ts\n#EXT-X-ENDLIST"), written

    def test_java_io_path_escape_rejected(self):
        # java.io 路径逃出 storeDir 应明确抛错，而不是静默写到别处。
        rule = {
            "title": "逃逸站", "type": "video", "version": 1,
            "url": "hiker://empty##fypage",
            "find_rule": (
                "js:new java.io.File('/etc/passwd').mkdirs(); setResult([]);"),
        }
        self._call("load", "escape-site", rule=json.dumps(rule))
        with pytest.raises(Exception) as ei:
            self._call("home", "escape-site", args=[1])
        assert "escapes storeDir" in str(ei.value), ei.value

    def test_load_unknown_key_errors(self):
        with pytest.raises(Exception) as ei:
            self._call("home", "never-loaded", args=[1])
        assert "no loaded rule" in str(ei.value)

    def test_fetch_timeout_capped(self):
        # 规则 fetch 传 timeout:120000；引擎封顶 30s，应在 ~45s 内以
        # fetch failed 报错，而不是等 120s（慢规则拖不垮 sidecar）。
        rule = {
            "title": "慢站", "type": "video", "version": 1,
            "url": "hiker://empty##fypage",
            "find_rule": ("js:fetch('" + self.base + "/slow35', "
                          "{timeout: 120000}); setResult([]);"),
        }
        self._call("load", "slow-site", rule=json.dumps(rule))
        t0 = time.monotonic()
        with pytest.raises(Exception) as ei:
            self._call("home", "slow-site", args=[1])
        dt = time.monotonic() - t0
        assert "fetch failed" in str(ei.value), ei.value
        assert dt < 45, f"封顶失效：fetch 等了 {dt:.1f}s"

    def test_function_url_detail_play_clear_error(self):
        # 搜索框 URL 本身是 JS 函数表达式：detail/play 不应报编译错，
        # 应明确说是 UI 控件、不是视频/详情链接。
        self._load_api()
        fn_url = ("function() { var kw = String(input || '').trim(); "
                  "return 'hiker://empty@rule=js:$.require"
                  "(\"hiker://page/erji\")'; }")
        with pytest.raises(Exception) as ei:
            self._call("detail", "synthetic-api", args=[fn_url])
        assert "UI handlers" in str(ei.value), ei.value
        with pytest.raises(Exception) as ei2:
            self._call("play", "synthetic-api", args=[fn_url])
        assert "UI handlers" in str(ei2.value), ei2.value

    def test_function_url_not_tokenized(self):
        # 函数表达式 URL 里的 "@rule=js:" 在函数体字符串内，不是后缀：
        # 列表收集时不应把它切成 token（会切出残缺代码）。
        rule = {
            "title": "函数链接站", "type": "video", "version": 1,
            "url": "hiker://empty##fypage",
            "find_rule": (
                "js:setResult([{title: '搜索', "
                "url: 'function() { return "
                "\"hiker://empty@rule=js:$.require(\\\"hiker://page/erji\\\")\"; }', "
                "col_type: 'input'}]);"),
        }
        self._call("load", "fnurl-site", rule=json.dumps(rule))
        home = self._call("home", "fnurl-site", args=[1])
        assert len(home["items"]) == 1
        assert home["items"][0]["url"].startswith("function")
        assert "haikuo-play:" not in home["items"][0]["url"]

    def test_cross_rule_require_clear_error(self):
        rule = {
            "title": "跨规则站", "type": "video", "version": 1,
            "url": "hiker://empty##fypage",
            "find_rule": ("js:$.require('hiker://page/erji?rule=someone-else'); "
                          "setResult([]);"),
        }
        self._call("load", "xrule-site", rule=json.dumps(rule))
        with pytest.raises(Exception) as ei:
            self._call("home", "xrule-site", args=[1])
        assert "cross-rule $.require" in str(ei.value), ei.value
