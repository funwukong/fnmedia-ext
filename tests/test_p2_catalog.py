"""P2 新分类：海外电视剧、AI漫剧。"""
import catalog
from config import JUNK_KEYWORDS


def test_new_categories_registered():
    cats = dict((c, t) for c, t, _ in catalog.CATALOG_CATEGORIES)
    assert cats["overseas"] == "🌍海外剧集"
    assert cats["ai_comic"] == "🤖AI漫剧"
    assert cats["ted"] == "🎓教育"
    assert catalog.catalog_title("overseas") == "🌍海外剧集"
    assert catalog.catalog_title("ai_comic") == "🤖AI漫剧"
    assert catalog.catalog_title("ted") == "🎓教育"


def test_ted_classify():
    assert catalog.classify("TED演讲") == "ted"
    assert catalog.classify("TED") == "ted"
    assert catalog.classify("Ted演讲") == "ted"
    assert catalog.classify("TEDx") == "ted"
    assert catalog.classify("TED-Ed") == "ted"
    # 普通演讲不进 TED（只认 TED 关键字）。
    assert catalog.classify("演讲") is None


def test_overseas_classify():
    assert catalog.classify("美剧") == "overseas"
    assert catalog.classify("韩剧") == "overseas"
    assert catalog.classify("日剧") == "overseas"
    assert catalog.classify("海外剧") == "overseas"
    assert catalog.classify("英剧") == "overseas"
    # 国产剧仍是普通剧集，不进海外。
    assert catalog.classify("国产剧") == "tv"


def test_ai_comic_classify():
    assert catalog.classify("AI漫剧") == "ai_comic"
    assert catalog.classify("漫剧") == "ai_comic"
    assert catalog.classify("动态漫画") == "ai_comic"
    assert catalog.classify("AI动漫") == "ai_comic"


def test_ai_comic_not_excluded_as_junk():
    assert "AI漫剧" not in JUNK_KEYWORDS
    assert "漫剧" not in JUNK_KEYWORDS
    assert "动态漫画" not in JUNK_KEYWORDS
    # 解说 / 预告仍是噪音。
    assert "解说" in JUNK_KEYWORDS
    assert catalog.classify("影视解说") is None


def test_old_categories_unchanged():
    assert catalog.classify("纪录片") == "documentary"
    assert catalog.classify("记录片") == "documentary"
    assert catalog.classify("4K电影") == "uhd"
    assert catalog.classify("热门短剧") == "shortdrama"
