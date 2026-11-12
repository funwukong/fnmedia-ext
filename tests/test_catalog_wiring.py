"""资源分类接线（cat-wiring，工人 2）：过滤 / 虚拟库 / 管理页。

覆盖（只测接线，不动 catalog.py 的分类核心）：
- libfilter：5 个新键进 _KNOWN_CATALOG_CATS 与并集分支；
  catalog_cat_for_type 的新映射与 catalog.classify() 口径一致
  （新五类先于 anime/variety/documentary；“音乐纪录片”归 music，
  不被判回 documentary；旧四类 + 纪录片行为不变）。
- vlibs：catalog:<新键> 通过库键校验，GUID 与 app.py 一致且由
  md5("fnmedia-catalog-lib:<key>") 派生；TED 旧 GUID 不变、显示名改“教育”。
- 管理页：_virtual_lib_list() 动态循环带出 5 个新库；空分类 count 为 0。
- total()：空分类返回实际数量 0，不许用页数×20 估算（4K 修复的教训）。

与工人 1（cat-core）的约定：键 sports/live/kids/opera/music 固定；
分类注册、关键词表、classify() 在 catalog.py，改动须同步见
libfilter.py 顶部注释；本文件用探针断言两者对同一批类型名一致。
"""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import catalog as catalogmod
import config as configmod
import fnusers as fnusersmod
import libfilter as libfiltermod
import vlibs as vlibsmod

NEW_CATS = ("sports", "live", "kids", "opera", "music")
TED_GUID = "34883d4af44ca4a5539cb38827158294"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    """配置单例换成内存替身（同 test_library_visibility 的做法），不碰真实 state/。"""
    monkeypatch.setattr(configmod.config, "_cfg",
                        copy.deepcopy(configmod.DEFAULTS))

    def _fake_save(patch):
        cfg = configmod.config.get()
        for k, v in patch.items():
            if k in cfg:
                cfg[k] = v
        configmod.config._cfg = cfg

    monkeypatch.setattr(configmod.config, "save", _fake_save)
    fnusersmod._cache.clear()
    yield
    fnusersmod._cache.clear()


def _guid_for(cat: str) -> str:
    return hashlib.md5(f"fnmedia-catalog-lib:{cat}".encode()).hexdigest()


def _brief(guid, provider, title, type_name, route=None):
    b = {"guid": guid, "online_id": f"{provider}:123", "title": title,
         "type_name": type_name, "lines": {f"{provider}::线路1": []}}
    if route is not None:
        b["catalog_route"] = route
    return b


# ---------- libfilter：新键进已知分类集 ----------

def test_known_cats_include_new_five():
    for cat in NEW_CATS:
        assert cat in libfiltermod._KNOWN_CATALOG_CATS
    # 与 app.CATALOG_LIB_GUIDS 保持同一全集（既有 test_route_cats_match_
    # catalog_lib_guids 的同一约定；两边各加 5 个后仍应相等）。
    assert libfiltermod._KNOWN_CATALOG_CATS == set(appmod.CATALOG_LIB_GUIDS)


# ---------- libfilter：类型名 → 新分类 ----------

def test_catalog_cat_for_type_new_mappings():
    cases = {
        "体育": "sports", "体育赛事": "sports", "足球": "sports",
        "篮球联赛": "sports", "乒乓球": "sports", "赛事": "sports",
        "运动": "sports",
        "直播": "live", "现场直播": "live", "电视直播": "live",
        "少儿": "kids", "少儿节目": "kids", "儿童": "kids",
        "儿歌": "kids", "幼儿启蒙": "kids", "亲子": "kids",
        "戏曲": "opera", "京剧": "opera", "越剧": "opera",
        "黄梅戏": "opera", "豫剧": "opera", "昆曲": "opera",
        "音乐": "music", "MV": "music", "mv": "music",
        "演唱会": "music", "音乐会": "music", "音乐节": "music",
        "音综": "music", "打歌舞台": "music", "音乐晚会": "music",
    }
    for typ, want in cases.items():
        assert libfiltermod.catalog_cat_for_type(typ) == want, typ


def test_new_cats_win_over_documentary_anime_variety():
    # 新五类在 classify() 里先于 anime/variety/documentary 判定，
    # libfilter 侧顺序须一致；“音乐纪录片”归 music，不判回 documentary。
    assert libfiltermod.catalog_cat_for_type("音乐纪录片") == "music"
    assert libfiltermod.catalog_cat_for_type("体育纪录片") == "sports"
    assert libfiltermod.catalog_cat_for_type("少儿纪录片") == "kids"
    assert libfiltermod.catalog_cat_for_type("戏曲纪录片") == "opera"
    assert libfiltermod.catalog_cat_for_type("直播纪录片") == "live"
    assert libfiltermod.catalog_cat_for_type("少儿动画") == "kids"
    assert libfiltermod.catalog_cat_for_type("音乐动漫") == "music"
    assert libfiltermod.catalog_cat_for_type("体育综艺") == "sports"
    assert libfiltermod.catalog_cat_for_type("音乐综艺") == "music"


def test_old_type_mapping_unchanged():
    assert libfiltermod.catalog_cat_for_type("纪录片") == "documentary"
    assert libfiltermod.catalog_cat_for_type("纪录") == "documentary"
    assert libfiltermod.catalog_cat_for_type("动作片") == "movie"
    assert libfiltermod.catalog_cat_for_type("动漫") == "anime"
    assert libfiltermod.catalog_cat_for_type("综艺") == "variety"
    assert libfiltermod.catalog_cat_for_type("韩剧") == "tv"
    assert libfiltermod.catalog_cat_for_type("") == "tv"


def test_type_mapping_matches_classify_for_probes():
    # 与 catalog.classify() 的口径一致性探针：只取两边都有映射的类名
    # （uhd/shortdrama/overseas/ai_comic/ted 仍不从类型名推断，不在此列）。
    probes = ("体育赛事", "足球", "直播", "电视直播", "少儿动画", "京剧",
              "音乐纪录片", "纪录片", "动作片", "动漫", "综艺", "电视",
              "电影", "演唱会", "音乐晚会", "少儿", "儿童节目", "越剧")
    for typ in probes:
        assert libfiltermod.catalog_cat_for_type(typ) == \
            catalogmod.classify(typ), typ


# ---------- libfilter：并集分支 ----------

def test_brief_lib_keys_union_via_type_name():
    cases = {
        "sports": "体育赛事", "live": "电视直播", "kids": "少儿动画",
        "opera": "京剧", "music": "音乐纪录片",
    }
    for cat, typ in cases.items():
        keys = libfiltermod.brief_lib_keys(
            _brief(f"w-{cat}", "ffzy", "片名", typ))
        assert keys == {f"standalone:ffzy", f"catalog:{cat}"}, (typ, keys)
        # 只开该分类库也能看见（ANY 语义的分类库门）。
        kept = libfiltermod.filter_briefs(
            [_brief(f"w-{cat}", "ffzy", "片名", typ)],
            lambda k, _c=cat: k == f"catalog:{_c}")
        assert [b["guid"] for b in kept] == [f"w-{cat}"]


def test_brief_lib_keys_union_via_route():
    for cat in NEW_CATS:
        b = _brief(f"r-{cat}", "baofeng", "动作大片", "动作片",
                   route={"source": "baofeng", "type_id": "5", "cat": cat})
        assert libfiltermod.brief_lib_keys(b) == {
            "standalone:baofeng", "catalog:movie", f"catalog:{cat}"}, cat


def test_brief_lib_keys_catalog_agg_brief_new_cat():
    for cat in NEW_CATS:
        b = _brief(f"c-{cat}", "baofeng", "片名", "体育")
        b.update({"catalog_cat": cat, "catalog_scope": "__agg__",
                  "catalog_source": "baofeng"})
        assert libfiltermod.brief_lib_keys(b) == {
            f"catalog:{cat}", "standalone:baofeng"}, cat


# ---------- vlibs：键校验与 GUID ----------

def test_vlibs_normalize_new_keys():
    for cat in NEW_CATS:
        assert vlibsmod.normalize_key(f"catalog:{cat}") == f"catalog:{cat}"
    assert vlibsmod.normalize_key("catalog:bogus") == ""


def test_vlibs_guid_roundtrip_new_cats():
    for cat in NEW_CATS:
        guid = _guid_for(cat)
        assert appmod.CATALOG_LIB_GUIDS[cat] == guid
        assert vlibsmod.CATALOG_LIB_GUIDS[cat] == guid
        assert appmod.CATALOG_GUID_CAT[guid] == cat
        assert vlibsmod.key_of_guid(guid) == f"catalog:{cat}"
    # 两份 CATALOG_LIB_GUIDS 全集一致（app 与 vlibs 的固定 guid 不许漂移）。
    assert vlibsmod.CATALOG_LIB_GUIDS == appmod.CATALOG_LIB_GUIDS


def test_vlibs_labels_new_cats_and_ted():
    assert vlibsmod.CATALOG_CAT_LABEL["sports"] == "体育节目"
    assert vlibsmod.CATALOG_CAT_LABEL["live"] == "直播"
    assert vlibsmod.CATALOG_CAT_LABEL["kids"] == "少儿"
    assert vlibsmod.CATALOG_CAT_LABEL["opera"] == "戏曲"
    assert vlibsmod.CATALOG_CAT_LABEL["music"] == "音乐MV"
    assert vlibsmod.CATALOG_CAT_LABEL["ted"] == "教育"


def test_ted_guid_unchanged_and_renamed():
    assert _guid_for("ted") == TED_GUID
    assert appmod.CATALOG_LIB_GUIDS["ted"] == TED_GUID
    assert vlibsmod.CATALOG_LIB_GUIDS["ted"] == TED_GUID
    assert appmod.CATALOG_GUID_CAT[TED_GUID] == "ted"
    assert vlibsmod.key_of_guid(TED_GUID) == "catalog:ted"
    assert catalogmod.catalog_title("ted") == "🎓教育"


# ---------- total()：空分类返回实际数量 0 ----------

def test_empty_category_total_is_zero():
    cat = catalogmod.Catalog()
    for c in NEW_CATS + ("ted", "uhd", "movie"):
        assert cat.total(c) == 0, c
        assert cat.dense_count(c) == 0, c


def test_total_not_inflated_by_other_routes():
    # 只有电影路由、没有新分类路由时，新分类 total 仍是 0——不许像旧 4K
    # 那样借电影页数×20 估算（空库报几十条、前端一直翻空页转圈）。
    cat = catalogmod.Catalog()
    cat._routes[catalogmod.Catalog._scope_key("__agg__", "movie")] = [
        {"source_id": "s1", "type_id": 1, "type_name": "电影",
         "pagecount": 250}]
    assert cat.total("movie") == 250 * catalogmod.PAGE_SIZE
    for c in NEW_CATS:
        assert cat.total(c) == 0, c
    # 4K 回归：有电影路由、无 4K 原生路由时 total 只报实际找到的数（0）。
    assert cat.total("uhd") == 0


# ---------- 管理页与注入池：动态生效 ----------

def test_admin_virtual_lib_list_shows_new_cats():
    libs = appmod._virtual_lib_list()
    by_key = {d["key"]: d for d in libs}
    titles = {c: t for c, t, _ in catalogmod.CATALOG_CATEGORIES}
    for cat in NEW_CATS:
        key = f"catalog:{cat}"
        assert key in by_key, key
        row = by_key[key]
        assert row["guid"] == _guid_for(cat)
        assert row["kind"] == "catalog"
        assert row["title"] == titles[cat]
        # 空分类：count 为实际数量 0，且 enabled 为 False（无路由）。
        assert row["count"] == 0
        assert row["enabled"] is False
    ted = by_key["catalog:ted"]
    assert ted["guid"] == TED_GUID
    assert ted["title"] == "🎓教育"


def test_injection_pools_include_new_cats():
    # app.py 各注入点（2414/2454/2767/2894/3866/4542/862）全是遍历
    # CATALOG_CATEGORIES / CATS / CATALOG_LIB_GUIDS：新分类只要进了
    # 这三处全集就自动进入媒体库注入、列表与搜索范围，无需逐点改代码。
    cats = {c for c, _, _ in catalogmod.CATALOG_CATEGORIES}
    assert set(NEW_CATS) <= cats
    assert set(NEW_CATS) <= set(catalogmod.CATS)
    assert set(NEW_CATS) <= set(appmod.CATALOG_LIB_GUIDS)
    assert set(NEW_CATS) <= set(vlibsmod.CATALOG_LIB_GUIDS)
