"""PanSou 自托管搜索适配器（2026-11-12 接通）。

``_pansou_search`` 向用户提供的自托管实例
``https://bus.wulii.eu.cc/``（fish2018/pansou）POST /api/search，
取 quark/uc/aliyun 三盘的分享链接；失败一律返回 [] 不抛错。
本文件用 httpx.MockTransport 测适配器，不碰真实网络。

关键点（开发指南学习结论，见 docs/插件开发指南.md 与
service/search_service.go）：merged_by_type 的键是服务端类型名，
aliyun 不是 ali——按 ("quark","uc","ali") 取会整个丢掉阿里桶。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import pansou

_QUARK1 = "https://pan.quark.cn/s/aaa111"
_QUARK2 = "https://pan.quark.cn/s/bbb222"
_UC1 = "https://drive.uc.cn/s/ccc333"
_ALI1 = "https://www.aliyundrive.com/s/ddd444"
_ALI2 = "https://www.alipan.com/s/eee555"
_BAIDU1 = "https://pan.baidu.com/s/fff666"
_XUNLEI1 = "https://pan.xunlei.com/ggg777"


def _item(url, password="", note="测试影片"):
    return {"url": url, "password": password, "note": note,
            "datetime": "2026-11-12T00:00:00Z", "source": "tg:test",
            "images": []}


def _payload(merged, total=None):
    if total is None:
        total = sum(len(v) for v in merged.values())
    return {"code": 0, "message": "success",
            "data": {"total": total, "merged_by_type": merged}}


def _transport(payload=None, status=200, raw=None, capture=None,
               exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if exc is not None:
            raise exc
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


class PansouSearchAdapterTests(unittest.TestCase):
    def test_base_constant_is_user_supplied_instance(self):
        # 2026-11-13：地址改为可配置（pansou_url），默认仍是该实例。
        self.assertEqual(pansou._pansou_base_url(),
                         "https://bus.wulii.eu.cc")

    def test_request_shape(self):
        capture: list = []
        tr = _transport(_payload({"quark": [_item(_QUARK1)]}),
                        capture=capture)
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        self.assertEqual(out, [_QUARK1])
        self.assertEqual(len(capture), 1)
        req = capture[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.url.path, "/api/search")
        self.assertEqual(json.loads(req.content),
                         {"kw": "流浪地球",
                          "cloud_types": ["quark", "uc", "aliyun"],
                          "res": "merge", "src": "all"})
        self.assertIsNone(req.headers.get("authorization"))

    def test_merge_shape_parsing_and_order(self):
        # 服务端字典顺序是 aliyun 在前，也必须按 quark → uc → aliyun 出。
        merged = {"aliyun": [_item(_ALI1)],
                  "quark": [_item(_QUARK1), _item(_QUARK2)],
                  "uc": [_item(_UC1)]}
        tr = _transport(_payload(merged))
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        self.assertEqual(out, [_QUARK1, _QUARK2, _UC1, _ALI1])

    def test_aliyun_bucket_not_dropped(self):
        # 回归：merged_by_type 的键是 aliyun 不是 ali；只取 ("quark",
        # "uc", "ali") 会整个丢掉阿里桶。
        merged = {"aliyun": [_item(_ALI1), _item(_ALI2)]}
        tr = _transport(_payload(merged))
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        self.assertEqual(out, [_ALI1, _ALI2])

    def test_filters_non_three_disk_magnet_and_empty(self):
        merged = {"quark": [_item(_QUARK1), _item(""),
                             _item("magnet:?xt=urn:btih:abc")],
                  "baidu": [_item(_BAIDU1)],
                  "xunlei": [_item(_XUNLEI1)],
                  "123": [_item("https://www.123pan.com/s/hhh888")],
                  "aliyun": [_item(_ALI1)]}
        tr = _transport(_payload(merged))
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        self.assertEqual(out, [_QUARK1, _ALI1])

    def test_password_appended(self):
        merged = {"quark": [_item(_QUARK1, "ZY4K")],
                  "uc": [_item(_UC1 + "?x=1", "ab12")]}
        tr = _transport(_payload(merged))
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        self.assertEqual(out, [_QUARK1 + "?pwd=ZY4K",
                               _UC1 + "?x=1&pwd=ab12"])

    def test_password_not_duplicated_when_present(self):
        merged = {"aliyun": [_item(_ALI1 + "?pwd=AAAA", "BBBB"),
                             _item(_ALI2 + "?code=CCCC", "DDDD")]}
        tr = _transport(_payload(merged))
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        self.assertEqual(out, [_ALI1 + "?pwd=AAAA",
                               _ALI2 + "?code=CCCC"])

    def test_dedupe_and_caps(self):
        merged = {"quark": [_item(_QUARK1, "AAAA"),
                             _item(_QUARK1 + "/", "BBBB"),
                             _item(_QUARK1, "CCCC")]
                  + [_item(f"https://pan.quark.cn/s/q{i:03d}")
                     for i in range(8)],
                  "aliyun": [_item(f"https://www.aliyundrive.com/s/a{i:03d}")
                             for i in range(8)]}
        tr = _transport(_payload(merged))
        out = pansou._pansou_search("流浪地球", 0, _transport=tr)
        # 同一分享去重后只留 1 条；每盘上限 6 条；总数上限 12 条。
        self.assertEqual(len(out), 12)
        self.assertEqual(out[0], _QUARK1 + "?pwd=AAAA")
        self.assertEqual(len([u for u in out if "quark" in u]), 6)
        self.assertEqual(len([u for u in out if "aliyun" in u]), 6)
        self.assertEqual(len(set(out)), 12)

    def test_total_zero_returns_empty(self):
        tr = _transport({"code": 0, "message": "success",
                         "data": {"total": 0}})
        self.assertEqual(pansou._pansou_search("归鹭", 0, _transport=tr), [])

    def test_code_nonzero_returns_empty(self):
        tr = _transport({"code": 400, "message": "bad kw",
                         "data": {"total": 0, "merged_by_type": {}}})
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])

    def test_http_error_returns_empty(self):
        tr = _transport(_payload({}), status=400)
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])
        tr = _transport(_payload({}), status=403)
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])

    def test_timeout_and_connect_error_return_empty(self):
        req = httpx.Request("POST", "https://bus.wulii.eu.cc/api/search")
        tr = _transport(exc=httpx.ConnectTimeout("timed out", request=req))
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])
        tr = _transport(exc=httpx.ConnectError("refused", request=req))
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])

    def test_non_json_returns_empty(self):
        tr = _transport(raw=b"<html>not json</html>")
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])

    def test_missing_data_returns_empty(self):
        tr = _transport({"code": 0, "message": "success"})
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])
        tr = _transport({"code": 0, "message": "success",
                         "data": {"total": 3}})
        self.assertEqual(pansou._pansou_search("流浪地球", 0, _transport=tr),
                         [])

    def test_empty_title_makes_no_request(self):
        capture: list = []
        tr = _transport(_payload({"quark": [_item(_QUARK1)]}),
                        capture=capture)
        self.assertEqual(pansou._pansou_search("", 0, _transport=tr), [])
        self.assertEqual(pansou._pansou_search("   ", 0, _transport=tr), [])
        self.assertEqual(capture, [])


if __name__ == "__main__":
    unittest.main()
