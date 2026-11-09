"""详情页关联作品：同系列/同分类优先，排除自身，同名去重，最多 12 个。"""
from related import related_for


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
