import libfilter


def test_normal_content_type_overrides_stale_welfare_route():
    cases = {
        "电视剧": "tv", "电影": "movie", "动漫": "anime",
        "综艺": "variety", "纪录片": "documentary", "AI漫剧": "ai_comic",
        "短剧": "shortdrama",
    }
    for typ, expected in cases.items():
        brief = {"type_name": typ,
                 "catalog_route": {"cat": "adult", "source": "cms"},
                 "catalog_cat": "adult", "catalog_scope": "__agg__",
                 "catalog_source": "cms"}
        keys = libfilter.brief_lib_keys(brief)
        assert f"catalog:{expected}" in keys, (typ, keys)
        assert "catalog:adult" not in keys, (typ, keys)


def test_explicit_adult_type_keeps_adult_route():
    brief = {"type_name": "伦理片",
             "catalog_route": {"cat": "adult", "source": "cms"},
             "catalog_cat": "adult", "catalog_scope": "__agg__",
             "catalog_source": "cms"}
    assert "catalog:adult" in libfilter.brief_lib_keys(brief)
