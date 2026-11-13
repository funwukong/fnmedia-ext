"""4K 分类派生：源分类名里没有“4K”时，4K 库以前永远是空的（没有路由就不显示）。

现在 4K 还按片名画质信号派生：浏览电影/剧集时带 4K/2160/杜比/HDR/蓝光
信号的片抄进 uhd 列表；源没有 4K 分类时，打开 4K 库借电影分类翻页过滤。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import catalog as catalogmod
from catalog import Catalog

SOURCE = {"id": "s1", "api": "https://cms.example/api.php/provide/vod/at/json"}


def _vod(name, remarks):
    return {"vod_id": name, "vod_name": name, "type_name": "电影",
            "vod_year": "2024", "vod_area": "", "vod_remarks": remarks,
            "vod_score": "", "vod_pic": "", "vod_content": "",
            "vod_play_from": "线路1",
            "vod_play_url": "正片$http://a.example/1.m3u8"}


def test_is_uhd_title():
    for title, remarks in [("片名", "4K"), ("片名", "2160P"), ("片名", "杜比视界"),
                           ("片名", "蓝光"), ("片名", "HDR"), ("4K大片", ""),
                           ("片名", "超高清"), ("片名", "UHD")]:
        assert catalogmod.is_uhd_title(title, remarks), (title, remarks)
    for title, remarks in [("片名", "HD"), ("片名", "超清"), ("片名", "正片"),
                           ("片名", ""), ("", "")]:
        assert not catalogmod.is_uhd_title(title, remarks), (title, remarks)


def test_briefs_uhd_cat_keeps_only_4k():
    cat = Catalog()
    route = {"source_id": "s1", "type_id": 1}
    vods = [_vod("A4K", "4K"), _vod("B普", "HD"), _vod("C蓝光", "蓝光")]
    got = cat._briefs_from_vods(vods, SOURCE, route, "__agg__", "uhd")
    assert [b["title"] for b in got] == ["A4K", "C蓝光"]


def test_briefs_movie_feeds_uhd_dense():
    cat = Catalog()
    route = {"source_id": "s1", "type_id": 1}
    vods = [_vod("A4K", "4K"), _vod("B普", "HD"), _vod("C杜比", "杜比视界")]
    got = cat._briefs_from_vods(vods, SOURCE, route, "__agg__", "movie")
    assert len(got) == 3
    uhd = cat._dense.get("__agg__:uhd") or []
    assert [b["title"] for b in uhd] == ["A4K", "C杜比"]
    assert all(b["catalog_cat"] == "uhd" for b in uhd)
    # 再跑一遍不重复
    cat._briefs_from_vods(vods, SOURCE, route, "__agg__", "movie")
    assert len(cat._dense.get("__agg__:uhd") or []) == 2


def test_uhd_available_from_movie_routes():
    cat = Catalog()
    assert cat.available("uhd") is False
    cat._routes["__agg__:movie"] = [{"source_id": "s1", "type_id": 1,
                                     "pagecount": 5}]
    assert cat.available("uhd") is True
    assert cat.pagecount("uhd") == 5
    assert cat._uhd_fallback_routes("__agg__")[0]["type_id"] == 1


def test_classify_uhd_keywords():
    assert catalogmod.classify("4K电影") == "uhd"
    assert catalogmod.classify("蓝光") == "uhd"
    assert catalogmod.classify("杜比视界") == "uhd"


def test_uhd_total_is_honest_without_native_routes():
    """派生 4K 没有原生路由时，total 只报实际找到的数，不能拿电影页数充数
    （以前报 60 条、实际 0 条，前端会一直翻空页转圈、图片永远刷不出来）。"""
    cat = Catalog()
    cat._routes["__agg__:movie"] = [{"source_id": "s1", "type_id": 1,
                                     "pagecount": 3}]
    assert cat.total("uhd") == 0
    cat._dense["__agg__:uhd"] = [{"guid": "g1"}, {"guid": "g2"}]
    assert cat.total("uhd") == 2
    # 有原生 4K 路由时仍按页数估算
    cat._routes["__agg__:uhd"] = [{"source_id": "s1", "type_id": 9,
                                   "pagecount": 5}]
    assert cat.total("uhd") == 5 * catalogmod.PAGE_SIZE


# ---- 假 CMS 源端到端：refresh_routes -> 打开 4K 库 ----
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _make_handler(mode):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            ac = q.get("ac", [""])[0]
            if ac == "list":
                return self._send({"code": 1, "class": [
                    {"type_id": 1, "type_name": "电影"}]})
            if ac == "detail":
                if mode == "A":
                    vods = [
                        {"vod_id": "101", "vod_name": "星际穿越",
                         "vod_remarks": "4K",
                         "vod_pic": "http://img.example.com/101.jpg",
                         "vod_play_from": "线路1",
                         "vod_play_url": "正片$http://v.example.com/101.m3u8"},
                        {"vod_id": "102", "vod_name": "普通喜剧",
                         "vod_remarks": "HD中字",
                         "vod_pic": "http://img.example.com/102.jpg",
                         "vod_play_from": "线路1",
                         "vod_play_url": "正片$http://v.example.com/102.m3u8"},
                    ]
                else:
                    vods = [
                        {"vod_id": "201", "vod_name": "普通喜剧",
                         "vod_remarks": "HD中字",
                         "vod_pic": "http://img.example.com/201.jpg",
                         "vod_play_from": "线路1",
                         "vod_play_url": "正片$http://v.example.com/201.m3u8"},
                    ]
                return self._send({"code": 1, "pagecount": 3, "list": vods})
            return self._send({"code": 0, "list": []})
    return H


def _run_uhd_window(mode):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(mode))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cat = Catalog()
        src = {"id": "s1",
               "api": f"http://127.0.0.1:{port}/api.php/provide/vod/at/json"}
        cat._source_by_id = lambda sid: src if sid == "s1" else None
        asyncio.run(cat.refresh_routes([src]))
        win = asyncio.run(cat.get_window("uhd", 0, 22))
        return cat, win
    finally:
        srv.shutdown()


def test_uhd_window_with_4k_titles():
    cat, win = _run_uhd_window("A")
    assert [b["title"] for b in win] == ["星际穿越"]
    assert win[0]["poster"] == "http://img.example.com/101.jpg"
    assert cat.total("uhd") == 1
    assert cat.cached_page_posters("uhd", 8) == ["http://img.example.com/101.jpg"]


def test_uhd_window_without_4k_titles_reports_zero():
    cat, win = _run_uhd_window("B")
    assert win == []
    # 关键：没找到就是 0，不能报电影页数 × 20，否则前端一直翻空页
    assert cat.total("uhd") == 0
    assert cat.cached_page_posters("uhd", 8) == []
