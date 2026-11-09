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
