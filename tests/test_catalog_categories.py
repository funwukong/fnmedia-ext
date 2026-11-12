"""新增 catalog 分类：体育 / 直播 / 少儿 / 戏曲 / 音乐MV；TED 改名“🎓教育”。

测试先行：覆盖每个新分类的典型类名归属与关键边界（判定顺序）。
"""
import hashlib

import catalog


def _cats():
    return {c: (title, mdb) for c, title, mdb in catalog.CATALOG_CATEGORIES}


def test_new_categories_registered():
    cats = _cats()
    assert cats["sports"] == ("⚽体育节目", "TV")
    assert cats["live"] == ("📡直播", "TV")
    assert cats["kids"] == ("🧒少儿", "Mix")
    assert cats["opera"] == ("🎭戏曲", "Mix")
    assert cats["music"] == ("🎵音乐MV", "Mix")
    for c in ("sports", "live", "kids", "opera", "music"):
        assert c in catalog.CATS
        assert catalog.catalog_mdb(c) == cats[c][1]
        assert catalog.catalog_title(c) == cats[c][0]


def test_sports_keywords():
    assert catalog.classify("体育赛事") == "sports"
    assert catalog.classify("体育") == "sports"
    assert catalog.classify("足球") == "sports"
    assert catalog.classify("篮球联赛") == "sports"
    assert catalog.classify("乒乓球") == "sports"
    assert catalog.classify("网球公开赛") == "sports"
    assert catalog.classify("羽毛球") == "sports"
    assert catalog.classify("斯诺克") == "sports"
    assert catalog.classify("赛车") == "sports"
    assert catalog.classify("格斗") == "sports"


def test_live_keywords_and_tv_boundary():
    assert catalog.classify("直播") == "live"
    assert catalog.classify("现场直播") == "live"
    # “电视直播”含“电视”，必须在 tv 判定之前截获，否则会被 _TV_KW 吸走。
    assert catalog.classify("电视直播") == "live"
    assert catalog.classify("电视") == "tv"


def test_kids_keywords_and_anime_boundary():
    assert catalog.classify("少儿") == "kids"
    assert catalog.classify("儿童节目") == "kids"
    assert catalog.classify("儿歌") == "kids"
    assert catalog.classify("幼儿启蒙") == "kids"
    assert catalog.classify("亲子") == "kids"
    assert catalog.classify("童话故事") == "kids"
    # “少儿动画”归少儿；不含少儿的“动画”仍归动漫（“卡通”亦然）。
    assert catalog.classify("少儿动画") == "kids"
    assert catalog.classify("动画") == "anime"
    assert catalog.classify("卡通") == "anime"


def test_opera_keywords():
    assert catalog.classify("戏曲") == "opera"
    assert catalog.classify("京剧") == "opera"
    assert catalog.classify("越剧") == "opera"
    assert catalog.classify("黄梅戏") == "opera"
    assert catalog.classify("豫剧") == "opera"
    assert catalog.classify("昆曲") == "opera"
    assert catalog.classify("粤剧") == "opera"
    assert catalog.classify("二人转") == "opera"


def test_music_keywords_and_exclude_boundary():
    # “音乐 / MV”曾在 _EXCLUDE_KW 里：音乐判定必须在排除判定之前。
    assert catalog.classify("音乐") == "music"
    assert catalog.classify("MV") == "music"
    assert catalog.classify("mv") == "music"
    assert catalog.classify("演唱会") == "music"
    assert catalog.classify("音乐会") == "music"
    assert catalog.classify("音乐节") == "music"
    assert catalog.classify("音综") == "music"
    assert catalog.classify("打歌舞台") == "music"
    # 演唱会录像归音乐，不归纪录片/电影。
    assert catalog.classify("音乐纪录片") == "music"
    assert catalog.classify("纪录片") == "documentary"


def test_music_excludes_plain_gala():
    # 裸“晚会”不进音乐（与综艺重叠）；只有含音乐类词的晚会才进。
    assert catalog.classify("春晚") != "music"
    assert catalog.classify("综艺晚会") == "variety"
    assert catalog.classify("音乐晚会") == "music"
    assert catalog.classify("跨年演唱会") == "music"


def test_new_categories_bypass_neutral_kw():
    # classify 成功即自然绕过 classify_source_class 的 _NEUTRAL_KW
    # 拦截（“体育/赛事/音乐/MV/直播”都在中性表里），无需改该函数。
    assert catalog.classify_source_class("体育赛事", adult_allowed=False) == "sports"
    assert catalog.classify_source_class("音乐", adult_allowed=False) == "music"
    assert catalog.classify_source_class("电视直播", adult_allowed=False) == "live"
    assert catalog.classify_source_class("少儿动画", adult_allowed=False) == "kids"
    assert catalog.classify_source_class("京剧", adult_allowed=False) == "opera"


def test_ted_renamed_but_key_and_guid_unchanged():
    # 显示名由 catalog_title() 从 CATALOG_CATEGORIES 派生，键 ted 不动。
    assert catalog.catalog_title("ted") == "🎓教育"
    assert catalog.catalog_mdb("ted") == "Mix"
    assert "ted" in catalog.CATS
    assert catalog.classify("TED演讲") == "ted"
    assert hashlib.md5("fnmedia-catalog-lib:ted".encode()).hexdigest() == (
        "34883d4af44ca4a5539cb38827158294"
    )
