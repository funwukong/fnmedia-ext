"""收藏夹为空的修复：收藏记录只剩 guid/元信息、store 缓存丢了、收藏的是集节点时，
收藏夹原来直接把整条丢掉（_favorite_card 返回 None）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import app as appmod


class _ChartStub:
    def is_chart_guid(self, guid):
        return False

    def get_guid(self, guid):
        return None


def _setup(monkeypatch):
    monkeypatch.setattr(appmod, "chart_cache", _ChartStub())
    appmod.store._titles = {}
    appmod.store._ep_index = {}
    appmod.store._rebuild_index()
    appmod.marks._fav = {}
    appmod.marks._watched = {}


def test_favorite_shows_from_record_meta_when_store_miss(monkeypatch):
    """store 里没这个标题，但收藏记录里有标题/海报：收藏夹必须显示出来。"""
    _setup(monkeypatch)
    rec = {"guid": "online:abc", "ts": 1, "title": "测试剧", "type": "TV",
           "poster": "http://x/p.jpg", "year": "2024", "remarks": "更新至10集"}
    appmod.marks._fav = {"online:abc": rec}
    row = appmod._favorite_card(rec)
    assert row is not None
    assert "测试剧" in row["title"]
    assert row["is_favorite"] == 1
    assert row["type"] == "TV"


def test_favorite_with_no_meta_anywhere_is_dropped(monkeypatch):
    """实在没有任何可显示信息（只有 guid）时才丢弃。"""
    _setup(monkeypatch)
    rec = {"guid": "online:xyz", "ts": 1}
    appmod.marks._fav = {"online:xyz": rec}
    assert appmod._favorite_card(rec) is None


def test_favorite_episode_uses_parent_series(monkeypatch):
    """收藏的是集节点：用所属剧集建卡，而不是丢掉。"""
    _setup(monkeypatch)
    parent = {"guid": "online:series", "title": "父剧", "type_name": "电视剧",
              "year": "2023", "poster": "http://x/s.jpg", "remarks": "",
              "overview": "", "score": "8", "actor": "", "director": "",
              "area": "", "lines": {"源A": []}}
    appmod.store._titles = {"online:series": parent}
    appmod.store._ep_index = {"online:series:ep0": {"online_guid": "online:series",
                                                     "index": 0, "_per": {}}}
    appmod.store._rebuild_index()
    rec = {"guid": "online:series:ep0", "ts": 1, "title": "父剧",
           "type": "TV", "poster": "http://x/s.jpg"}
    appmod.marks._fav = {"online:series:ep0": rec}
    row = appmod._favorite_card(rec)
    assert row is not None
    assert "父剧" in row["title"]


def test_favorite_still_works_from_store_brief(monkeypatch):
    """正常路径（store 里有标题）不受影响。"""
    _setup(monkeypatch)
    brief = {"guid": "online:ok", "title": "正常剧", "type_name": "电影",
             "year": "2024", "poster": "http://x/o.jpg", "remarks": "HD",
             "overview": "", "score": "9", "actor": "", "director": "",
             "area": "内地", "lines": {"源A": []}}
    appmod.store._titles = {"online:ok": brief}
    appmod.store._rebuild_index()
    rec = {"guid": "online:ok", "ts": 1}
    appmod.marks._fav = {"online:ok": rec}
    row = appmod._favorite_card(rec)
    assert row is not None and "正常剧" in row["title"]
