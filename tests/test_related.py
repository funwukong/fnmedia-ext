"""详情页关联作品：同系列/同分类优先，排除自身，同名去重，最多 12 个。"""
from related import DEFAULT_LIMIT, related_for, related_for_with_dense


def _b(guid, title, **kw):
    d = {"guid": guid, "title": title, "poster": "http://x/p.jpg",
         "year": "2024", "area": "内地", "actor": "", "director": "",
         "type_name": "国产剧", "catalog_cat": "tv", "score": "8.0",
         "lines": {"线路1": [{"direct": True}, {"direct": True}]}}
    d.update(kw)
    return d


CUR = _b("g0", "狂飙", actor="张译/张颂文", director="徐纪周")


def test_excludes_self_and_prefers_same_series():
    pool = [
        _b("g0", "狂飙"),  # 自身，必须排除
        _b("g1", "狂飙2", catalog_cat="tv", type_name="国产剧",
           actor="张译", year="2024", score="9.0"),
        _b("g2", "漫长的季节", catalog_cat="tv", type_name="国产剧",
           year="2023", score="9.5"),
        _b("g3", "流浪地球", catalog_cat="movie", type_name="科幻片",
           year="2023", score="9.0"),
    ]
    out = related_for(CUR, pool)
    guids = [b["guid"] for b in out]
    assert "g0" not in guids
    # 同系列“狂飙2”排第一；同分类同类型名的剧集排第二。
    assert guids[0] == "g1"
    assert guids[1] == "g2"
    # 毫不相关的电影（仅同地区/邻近年份）达不到入选门槛，不入选。
    assert "g3" not in guids


def test_dedupe_same_title_keeps_best():
    pool = [
        _b("g1", "狂飙2", catalog_cat="movie", score="7.0"),
        _b("g2", "狂飙2", catalog_cat="tv", type_name="国产剧",
           actor="张译", score="9.0"),
    ]
    out = related_for(CUR, pool)
    assert [b["guid"] for b in out] == ["g2"]


def test_cap_and_empty_pool():
    pool = [_b(f"g{i}", f"剧名{i}", catalog_cat="tv") for i in range(30)]
    assert len(related_for(CUR, pool)) == 12
    assert related_for(CUR, []) == []
    assert related_for(CUR, [_b("g9", "无关片", catalog_cat="movie",
                                  type_name="科幻片", area="美国",
                                  year="1999", score="5.0")]) == []


def test_no_poster_excluded():
    pool = [_b("g1", "狂飙2", poster="", catalog_cat="tv")]
    assert related_for(CUR, pool) == []


def test_family_key_sequel_suffix():
    from related import _family_key
    assert _family_key("狂飙2") == _family_key("狂飙第二部") == "狂飙"
    assert _family_key("狂飙") == "狂飙"


# ---------------- 冷启动兜底：池子为空/过小或入选不足时，用分类 dense 补候选 ----------------

def _dense_pool():
    return [
        _b("d1", "狂飙2", catalog_cat="tv", type_name="国产剧",
           actor="张译", year="2024", score="9.0"),
        _b("d2", "漫长的季节", catalog_cat="tv", type_name="国产剧",
           year="2023", score="9.5"),
        _b("d3", "流浪地球", catalog_cat="movie", type_name="科幻片",
           year="2023", score="9.0"),
    ]


def test_cold_start_empty_pool_uses_dense():
    # 已浏览池为空：只靠 dense 也能出同系列候选；毫不相关的仍被门槛拦住。
    out = related_for_with_dense(CUR, [], _dense_pool())
    guids = [b["guid"] for b in out]
    assert guids[0] == "d1"
    assert guids[1] == "d2"
    assert "d3" not in guids


def test_cold_start_small_pool_supplemented():
    # 已浏览池只有 1 条入选：dense 补足，统一排序（同系列仍排第一）。
    pool = [_b("g1", "狂飙2", catalog_cat="tv", type_name="国产剧",
               actor="张译", year="2024", score="9.0")]
    out = related_for_with_dense(CUR, pool, _dense_pool())
    assert [b["guid"] for b in out] == ["g1", "d2"]


def test_cold_start_dense_still_filtered():
    # dense 里的候选同样要过 MIN_SCORE / 必须有海报这两道关。
    junk = [
        _b("d9", "无关片", catalog_cat="movie", type_name="科幻片",
           area="美国", year="1999", score="5.0"),
        _b("d8", "狂飙2", poster="", catalog_cat="tv"),
    ]
    assert related_for_with_dense(CUR, [], junk) == []


def test_cold_start_no_dup_across_pools():
    # 同一 guid 同时在 pool 与 dense 里：只算一次。
    pool = [_b("g1", "狂飙2", catalog_cat="tv", type_name="国产剧",
               actor="张译", year="2024", score="9.0")]
    dense = [_b("g1", "狂飙2", catalog_cat="tv", type_name="国产剧",
                actor="张译", year="2024", score="9.0"),
             _b("d2", "漫长的季节", catalog_cat="tv", type_name="国产剧",
                year="2023", score="9.5")]
    out = related_for_with_dense(CUR, pool, dense)
    assert [b["guid"] for b in out] == ["g1", "d2"]


def test_cold_start_cap_twelve():
    dense = [_b(f"d{i}", f"剧名{i}", catalog_cat="tv") for i in range(30)]
    out = related_for_with_dense(CUR, [], dense)
    assert len(out) == DEFAULT_LIMIT


def test_cold_start_full_pool_needs_no_dense():
    # 主池已满 12 条入选：不再并入 dense（dense 即便有更高分也不掺和）。
    pool = [_b(f"g{i}", f"剧名{i}", catalog_cat="tv") for i in range(30)]
    dense = [_b("d1", "狂飙2", catalog_cat="tv", type_name="国产剧",
                actor="张译", year="2024", score="9.9")]
    out = related_for_with_dense(CUR, pool, dense)
    assert len(out) == DEFAULT_LIMIT
    assert "d1" not in [b["guid"] for b in out]


def test_cached_dense_returns_materialized_only():
    # 同步取已物化的 dense：只读内存，不触发网络；未物化的分类返回 []。
    import catalog as catalogmod
    cat = catalogmod.Catalog()
    key = catalogmod.Catalog._scope_key(catalogmod.AGG_SCOPE, "tv")
    cat._dense[key] = [_b(f"d{i}", f"剧名{i}", catalog_cat="tv")
                       for i in range(5)]
    assert [b["guid"] for b in cat.cached_dense("tv", 3)] == ["d0", "d1", "d2"]
    assert len(cat.cached_dense("tv", 40)) == 5
    assert cat.cached_dense("tv", 0) == []
    assert cat.cached_dense("movie") == []
