"""P1-15：夸克/UC 四个底层调用抽公共后的行为等价测试。

抽公共前四函数（_auth_cookie/_api/_fresh/_pc）在两边逐行全等
（含注释与风控文案）；抽公共后两边共用 pan._make_pan_helpers，
各注各的 _client / PanError / UA / _V4HTTPSConnection。
本文件用假客户端/假连接（无真实网络）在两边跑同一脚本，
断言登录路径与播放路径的结果、重试次数、错误文案完全一致，
并断言公开接口签名未变。
"""
from __future__ import annotations

import inspect
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import pan as panmod
import panuc as panucmod

_RAISE = object()


class _FakeResp:
    def __init__(self, status, payload=_RAISE):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is _RAISE:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """httpx.Client 替身：按脚本返回响应，记录请求。"""

    def __init__(self, script, auth=""):
        self._script = list(script)
        self._pan_auth = auth
        self.closed = False
        self.requests = []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return self._script.pop(0)

    def close(self):
        self.closed = True


class _FakeHTTPResp:
    def __init__(self, status, raw):
        self.status = status
        self._raw = raw

    def read(self):
        return self._raw


class _FakeConn:
    def __init__(self, factory, host):
        self._f = factory
        self.host = host

    def request(self, method, path, body=None, headers=None):
        self._f.calls.append({"host": self.host, "method": method,
                              "path": path, "body": body,
                              "headers": dict(headers or {})})

    def getresponse(self):
        status, raw = self._f.script.pop(0)
        if isinstance(raw, dict):
            raw = json.dumps(raw).encode()
        return _FakeHTTPResp(status, raw)

    def close(self):
        pass


class _FakeConnFactory:
    """_V4HTTPSConnection 替身：按脚本返回响应，记录每次连接。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, host, context=None, timeout=20):
        return _FakeConn(self, host)


def _ok_stoken(stoken):
    return {"code": 0, "data": {"stoken": stoken}}


class PanSharedTests(unittest.TestCase):
    def setUp(self):
        item = patch.object(time, "sleep", lambda s: None)
        item.start()
        self.addCleanup(item.stop)

    # ---------- 单一来源 ----------

    def test_both_modules_share_the_same_factory(self):
        self.assertIs(panucmod._make_pan_helpers,
                      panmod._make_pan_helpers)
        # 两边都是该工厂的产物（同一模块内定义，故 __module__ 均为 pan）
        self.assertEqual(panucmod._api.__module__, "pan")
        self.assertEqual(panmod._api.__module__, "pan")

    def test_private_and_public_signatures_unchanged(self):
        # 内层函数签名与抽出前一致（参数名、顺序、默认值），且两边相同。
        # 注：from __future__ import annotations 使注解以字符串形式呈现，
        # 故只断言参数结构，不逐字比注解文本。
        for name, params, defaults in (
                ("_api", ["c", "method", "url", "_retry", "kw"], {"_retry": 8}),
                ("_pc", ["method", "url", "cookie", "_tries", "kw"],
                 {"_tries": 5}),
                ("_fresh", ["c"], {}),
                ("_auth_cookie", ["c"], {})):
            sig_q = inspect.signature(getattr(panmod, name))
            sig_u = inspect.signature(getattr(panucmod, name))
            self.assertEqual(sig_q, sig_u, name)
            self.assertEqual(list(sig_q.parameters), params, name)
            for k, v in defaults.items():
                self.assertEqual(sig_q.parameters[k].default, v, name)
        for name in ("parse_share", "resolve_share", "playable",
                     "playable_url", "qr_start", "qr_poll", "logged_in",
                     "set_cookie", "logout", "save_dir_fid", "list_dir"):
            self.assertTrue(hasattr(panmod, name), f"pan.{name}")
            self.assertTrue(hasattr(panucmod, name), f"panuc.{name}")

    # ---------- _api：分享 token 路径（登录态入口），风控重试 ----------

    def _run_share_token(self, mod):
        made = []

        def fake_client(cookie=""):
            made.append(cookie)
            return _FakeClient([_FakeResp(200, _ok_stoken("STOKEN-X"))])

        first = _FakeClient([
            _FakeResp(401),  # 风控：非 JSON + 401，触发重建 client 重试
            _FakeResp(200, _ok_stoken("STOKEN-X")),
        ], auth="cookie-auth")
        # 第一次 401 后 _api 用 first 的 auth 重建 client；让重建走 fake_client
        with patch.object(mod, "_client", fake_client):
            out = mod.share_token(first, "pwdid", "1234")
        return out, made, first

    def test_share_token_retry_identical_quark_and_uc(self):
        # 首个 client 先返回 401 非 JSON（风控），_api 用其 auth 重建
        # client 后重试成功；两边应返回同一 stoken。
        for mod in (panmod, panucmod):
            with self.subTest(mod=mod.__name__):
                out, made, first = self._run_share_token(mod)
                self.assertEqual(out, "STOKEN-X")
                self.assertEqual(made, ["cookie-auth"])

    def test_api_retry_rebuilds_client_with_auth(self):
        for mod in (panmod, panucmod):
            with self.subTest(mod=mod.__name__):
                made = []

                def fake_client(cookie=""):
                    made.append(cookie)
                    return _FakeClient(
                        [_FakeResp(200, _ok_stoken("STOKEN-R"))])

                first = _FakeClient([_FakeResp(401)], auth="cookie-auth")
                with patch.object(mod, "_client", fake_client):
                    out = mod._api(first, "GET", "https://example.invalid/x")
                self.assertEqual(out, _ok_stoken("STOKEN-R"))
                self.assertEqual(made, ["cookie-auth"])
                self.assertTrue(first.closed is False)  # 首个 client 未被关

    def test_api_biz_risk_code_retries_identically(self):
        for mod in (panmod, panucmod):
            with self.subTest(mod=mod.__name__):
                first = _FakeClient([
                    _FakeResp(200, {"code": 32003, "message": "risk"}),
                    _FakeResp(200, _ok_stoken("STOKEN-B")),
                ], auth="")
                with patch.object(mod, "_client",
                                  lambda cookie="": _FakeClient(
                                      [_FakeResp(200, _ok_stoken("STOKEN-B"))])):
                    out = mod.share_token(first, "pwdid", "")
                self.assertEqual(out, "STOKEN-B")

    def test_api_persistent_non_json_raises_same_error(self):
        for mod in (panmod, panucmod):
            with self.subTest(mod=mod.__name__):
                first = _FakeClient([_FakeResp(401), _FakeResp(401)])
                with patch.object(mod, "_client",
                                  lambda cookie="": _FakeClient(
                                      [_FakeResp(401)])):
                    with self.assertRaises(mod.PanError) as cm:
                        mod._api(first, "GET", "https://example.invalid/x",
                                 _retry=2)
                self.assertEqual(str(cm.exception),
                                 "接口返回非 JSON（HTTP 401）")

    # ---------- _pc：登录校验与播放路径 ----------

    def test_uc_whoami_login_check_via_pc(self):
        factory = _FakeConnFactory([(200, {"code": 0, "data": {
            "nickname": "UC君", "uid": 123}})])
        with patch.object(panucmod, "_V4HTTPSConnection", factory):
            out = panucmod._whoami("cookie-uc")
        self.assertEqual(out, ("UC君", "123"))
        self.assertEqual(len(factory.calls), 1)
        call = factory.calls[0]
        self.assertEqual(call["host"], "pc-api.uc.cn")
        self.assertEqual(call["headers"]["Cookie"], "cookie-uc")
        self.assertIn("member", call["path"])

    def test_play_url_identical_quark_and_uc(self):
        play = {"code": 0, "data": {"video_list": [
            {"resolution": "super", "video_info": {
                "url": "https://cdn.example/x.mp4",
                "finish": True, "success": True}}]}}
        for mod in (panmod, panucmod):
            with self.subTest(mod=mod.__name__):
                factory = _FakeConnFactory([(200, play)])
                with patch.object(mod, "_V4HTTPSConnection", factory):
                    out = mod._play_url("cookie-x", "fid-1")
                self.assertEqual(out, ("https://cdn.example/x.mp4", "super"))
                self.assertEqual(len(factory.calls), 1)
                self.assertEqual(factory.calls[0]["method"], "POST")
                self.assertIn("/file/v2/play", factory.calls[0]["path"])

    def test_pc_retries_on_401_non_json_identically(self):
        for mod, host in ((panmod, "drive-pc.quark.cn"),
                          (panucmod, "pc-api.uc.cn")):
            with self.subTest(mod=mod.__name__):
                factory = _FakeConnFactory([
                    (401, b"not json"),
                    (200, {"code": 0, "data": {"list": []}}),
                ])
                with patch.object(mod, "_V4HTTPSConnection", factory):
                    out = mod._pc(
                        "GET", f"https://{host}/1/clouddrive/file/sort",
                        "cookie-x", params={"pdir_fid": "0"})
                self.assertEqual(out, {"code": 0, "data": {"list": []}})
                self.assertEqual(len(factory.calls), 2)
                self.assertEqual(factory.calls[0]["headers"]["Cookie"],
                                 "cookie-x")


if __name__ == "__main__":
    unittest.main()
