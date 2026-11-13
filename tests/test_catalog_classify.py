"""分类归并：纪录片 / 4K / 短剧 / 动漫 / 综艺 / 电影 / 剧集。"""
import catalog


def test_documentary_and_typo():
    assert catalog.classify("纪录片") == "documentary"
    # 错别字“记录片”同样归入纪录片（README 本次更新）。
    assert catalog.classify("记录片") == "documentary"
    assert catalog.classify("纪录") == "documentary"


def test_uhd_keywords():
    assert catalog.classify("4K电影") == "uhd"
    assert catalog.classify("杜比视界") == "uhd"
    assert catalog.classify("蓝光") == "uhd"


def test_shortdrama_anime_variety():
    assert catalog.classify("热门短剧") == "shortdrama"
    assert catalog.classify("微短剧") == "shortdrama"
    assert catalog.classify("动漫") == "anime"
    assert catalog.classify("综艺") == "variety"


def test_movie_tv_and_excluded():
    assert catalog.classify("电影") == "movie"
    assert catalog.classify("动作片") == "movie"
    assert catalog.classify("国产剧") == "tv"
    # 脏类 / 噪音不进任何库。
    assert catalog.classify("影视解说") is None
    assert catalog.classify("预告") is None
    assert catalog.classify("") is None
    assert catalog.classify("未知分类xyz") is None


def test_redline_never_classified():
    # 硬红线：任何开关都不放行。
    assert catalog.is_redline("萝莉") is True
    assert catalog.is_redline("换脸") is True
    assert catalog.classify_source_class("萝莉", adult_allowed=True) is None
    assert catalog.classify_source_class("换脸视频", adult_allowed=True) is None


def test_adult_cat_needs_ext_switch():
    # 扩展库默认关闭：未归入常规四类的成人向分类也不收编。
    assert catalog._welfare_on() is False
    assert catalog.classify_source_class("伦理", adult_allowed=True) is None
