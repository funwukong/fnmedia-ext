import asyncio

import catalog
import libfilter


def test_explicit_catalog_route_does_not_union_generic_tv():
    brief = {
        "online_id": "ffzy:1", "type_name": "国产剧",
        "lines": {"ffzy::线路1": []},
        "catalog_route": {"source": "ffzy", "type_id": "9", "cat": "shortdrama"},
    }
    assert libfilter.brief_lib_keys(brief) == {
        "standalone:ffzy", "catalog:shortdrama"}


def test_route_authority_preserves_explicit_orthogonal_uhd():
    brief = {
        "online_id": "ffzy:1", "type_name": "国产剧",
        "lines": {"ffzy::线路1": []},
        "catalog_route": {"source": "ffzy", "type_id": "9", "cat": "uhd"},
    }
    assert libfilter.brief_lib_keys(brief) == {
        "standalone:ffzy", "catalog:tv", "catalog:uhd"}


def test_normal_type_fallback_stays_tv():
    assert libfilter.catalog_cat_for_type("国产剧") == "tv"
    assert libfilter.brief_lib_keys({
        "online_id": "ffzy:1", "type_name": "国产剧",
        "lines": {"ffzy::线路1": []},
    }) == {"standalone:ffzy", "catalog:tv"}


def test_explicit_type_boundaries():
    cases = {
        "热门短剧": "shortdrama", "美剧": "overseas", "动漫": "anime",
        "综艺": "variety", "纪录片": "documentary", "4K电影": "uhd",
        "AI漫剧": "ai_comic", "体育": "sports", "直播": "live",
        "少儿": "kids", "京剧": "opera", "音乐": "music", "TED": "ted",
    }
    for type_name, expected in cases.items():
        assert catalog.classify(type_name) == expected
        assert libfilter.catalog_cat_for_type(type_name) == expected


def test_shortdrama_parent_is_inherited_by_leaf():
    cat = catalog.Catalog()
    cat._classes = {"ffzy": [
        {"type_id": 1, "type_name": "短剧", "type_pid": "0", "cat": "shortdrama"},
        {"type_id": 2, "type_name": "都市", "type_pid": "1", "cat": "shortdrama"},
        {"type_id": 3, "type_name": "国产剧", "type_pid": "0", "cat": "tv"},
    ]}
    assert cat.cat_for_source_type("ffzy", 2) == "shortdrama"
    assert cat.cat_for_source_type("ffzy", 3) == "tv"


def test_loader_inherits_shortdrama_parent(monkeypatch):
    cat = catalog.Catalog()
    classes = [
        {"type_id": 1, "type_name": "短剧", "type_pid": "0"},
        {"type_id": 2, "type_name": "都市", "type_pid": "1"},
        {"type_id": 3, "type_name": "国产剧", "type_pid": "0"},
    ]
    async def fake_fetch(client, source):
        return classes
    monkeypatch.setattr(catalog, "_fetch_class_list", fake_fetch)
    asyncio.run(cat._load_source_classes(object(), {
        "id": "ffzy", "api": "https://ffzy.test/api.php", "adult": False}))
    got = {x["type_id"]: x["cat"] for x in cat._classes["ffzy"]}
    assert got == {2: "shortdrama", 3: "tv"}
