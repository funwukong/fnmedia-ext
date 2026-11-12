"""直播分类可行性 + 边界核查（工人 3：cat-content）。

只测 classify / classify_source_class / cat_for_source_type 与 libfilter
路由门，不碰网络、不改业务代码。约定与工人 1（cat-core）一致：
- 新分类 sports / live / kids / opera / music 由 classify 在
  _EXCLUDE_KW 判定之前截获（“体育/赛事”“音乐/MV”曾在排除表里）；
- 五类判定顺序 sports > live > kids > opera > music，五类之间互不重叠；
- live 在 tv 之前（“电视直播”含“电视”）、music 在 documentary 之前
  （“音乐纪录片”含“纪录”）；
- classify 成功即绕过 classify_source_class 的 _NEUTRAL_KW 拦截
  （该检查只在 classify 返回 None 时才走到）。
"""
import pytest

import catalog
import libfilter
from catalog import Catalog, classify, classify_source_class, is_redline

# 工人 1 的实现已提交在 cat-core（8aeac92），尚未合入 main：
# 以下 6 项断言依赖 classify() 的新分类，在干净的 cat-content
#（main@d5a0f62）上会失败，故标 xfail（非严格）：合入 cat-core 后
# 它们应转为 xpass/通过，届时可去掉标记。本文件 02:33 在共享树里
#（工人 1 的 catalog.py + 工人 2 的 libfilter.py 同时在场时）已全量
# 跑通过一次，断言本身与工人 1 的实现一致，不是推测。
_NEEDS_CAT_CORE = (
    "待 cat-core（工人 1，8aeac92）合入：classify() 新分类未在 main 上"
)


@pytest.mark.xfail(strict=False, reason=_NEEDS_CAT_CORE)
def test_live_names_classify_live():
    # 任务 1：直播是纯分类归类——分类名命中即收录，不需要订阅/探活/HLS 代理。
    assert classify("直播") == "live"
    assert classify("电视直播") == "live"
    assert classify("现场直播") == "live"
    # 对照：不含直播的“电视”仍归剧集，live 判定没有把 tv 吸走。
    assert classify("电视") == "tv"


@pytest.mark.xfail(strict=False, reason=_NEEDS_CAT_CORE)
def test_sports_checked_before_live():
    # “体育直播 / 赛事直播”同时含体育词与直播词：按工人 1 的顺序
    # sports 在 live 之前，归体育节目库，不归直播库（顺序约定，见报告）。
    assert classify("体育直播") == "sports"
    assert classify("赛事直播") == "sports"
    assert classify("体育") == "sports"


@pytest.mark.xfail(strict=False, reason=_NEEDS_CAT_CORE)
def test_sports_music_escape_exclude_kw():
    # 任务 3：体育/音乐类名不再被 _EXCLUDE_KW 丢弃（判定在排除之前）。
    assert classify("体育") == "sports"
    assert classify("赛事") == "sports"
    assert classify("音乐") == "music"
    assert classify("MV") == "music"
    assert classify("mv") == "music"
    # 对照：排除表对没有新分类的噪音名仍生效。
    assert classify("影视解说") is None
    assert classify("预告") is None


@pytest.mark.xfail(strict=False, reason=_NEEDS_CAT_CORE)
def test_music_before_documentary():
    # 按工人 1 的约定：music 判定先于 documentary。
    assert classify("音乐纪录片") == "music"
    assert classify("演唱会") == "music"
    # 对照：纯纪录片仍归纪录片。
    assert classify("纪录片") == "documentary"


@pytest.mark.xfail(strict=False, reason=_NEEDS_CAT_CORE)
def test_classified_names_bypass_neutral_kw(monkeypatch):
    # 任务 3：classify 返回新分类后，classify_source_class 里的
    # _NEUTRAL_KW 检查不再走到——即使“直播/体育/音乐/MV”都在中性表里。
    # 福利开关开 + 源显式勾选成人也不应把它们送进成人库。
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)
    assert classify_source_class("直播", adult_allowed=True) == "live"
    assert classify_source_class("电视直播", adult_allowed=True) == "live"
    assert classify_source_class("现场直播", adult_allowed=True) == "live"
    assert classify_source_class("体育赛事", adult_allowed=True) == "sports"
    assert classify_source_class("音乐", adult_allowed=True) == "music"
    assert classify_source_class("音乐纪录片", adult_allowed=True) == "music"
    # 开关关时同样直通（classify 成功在福利检查之前返回）。
    monkeypatch.setattr(catalog, "_welfare_on", lambda: False)
    assert classify_source_class("电视直播", adult_allowed=False) == "live"
    assert classify_source_class("音乐", adult_allowed=False) == "music"


def test_neutral_kw_still_blocks_uncategorized(monkeypatch):
    # 对照：没有新分类可归的中性噪音名，仍被 _NEUTRAL_KW 拦在成人库之外。
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)
    for name in ("电台", "新闻", "解说"):
        assert classify(name) is None
        assert classify_source_class(name, adult_allowed=True) is None


def test_redline_first_even_for_live_music_names(monkeypatch):
    # 任务 3：红线不受新关键词影响。红线在 classify_source_class 开头
    # 拦截，且先于一切分类。
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)
    assert is_redline("换脸直播") is True
    assert classify_source_class("换脸直播", adult_allowed=True) is None
    assert classify_source_class("萝莉音乐", adult_allowed=True) is None
    assert classify_source_class("换脸视频", adult_allowed=True) is None


@pytest.mark.xfail(strict=False, reason=_NEEDS_CAT_CORE)
def test_classify_itself_ignores_redline():
    # classify 本身只做分类名归并，不管红线（红线由
    # classify_source_class 与 _briefs_from_vods 的标题/备注检查执行）。
    assert classify("换脸直播") == "live"
    assert classify("萝莉音乐") == "music"


def test_adult_routing_unaffected(monkeypatch):
    # 任务 3：纯成人向分类名仍走原成人路由，与新分类互不干扰。
    assert classify("福利") is None  # 成人向名从不进常规分类
    assert classify("伦理") is None
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)
    assert classify_source_class("福利", adult_allowed=False) == catalog.ADULT_CAT
    assert classify_source_class("伦理", adult_allowed=False) == catalog.ADULT_CAT
    assert classify_source_class("写真", adult_allowed=False) == catalog.ADULT_CAT
    # 开关关时成人路由仍关闭（与新分类无关的既有行为）。
    monkeypatch.setattr(catalog, "_welfare_on", lambda: False)
    assert classify_source_class("福利", adult_allowed=True) is None
    assert classify_source_class("伦理", adult_allowed=True) is None


def test_cat_for_source_type_covers_new_cats():
    # 任务 4：搜索/详情链的路由表映射（cat_for_source_type）是通用的——
    # 只要建库时 _classes 里记了新 cat，搜索 brief 补的 catalog_route
    # 就能带出 live / sports / music，无需按分类逐个加分支。
    cat = Catalog()
    cat._classes = {"ffzy": [
        {"type_id": 11, "type_name": "直播", "type_pid": "0", "cat": "live"},
        {"type_id": 12, "type_name": "电视直播", "type_pid": "0", "cat": "live"},
        {"type_id": 13, "type_name": "体育", "type_pid": "0", "cat": "sports"},
        {"type_id": 14, "type_name": "音乐纪录片", "type_pid": "0", "cat": "music"},
        {"type_id": 15, "type_name": "电影", "type_pid": "0", "cat": "movie"},
    ]}
    assert cat.cat_for_source_type("ffzy", 11) == "live"
    assert cat.cat_for_source_type("ffzy", "12") == "live"
    assert cat.cat_for_source_type("ffzy", 13) == "sports"
    assert cat.cat_for_source_type("ffzy", 14) == "music"
    assert cat.cat_for_source_type("ffzy", 15) == "movie"
    assert cat.cat_for_source_type("ffzy", 99) is None
    assert cat.cat_for_source_type("unknown", 11) is None
    assert cat.cat_for_source_type("ffzy", "") is None


@pytest.mark.xfail(
    strict=False,
    reason="cat-wiring 缺口：libfilter._KNOWN_CATALOG_CATS 与 "
           "app.CATALOG_LIB_GUIDS 未含 sports/live/kids/opera/music，"
           "路由门会丢弃新分类的 catalog_route；只核查不改动，见报告任务 4。",
)
def test_libfilter_route_gate_covers_new_cats():
    # 任务 4 缺口：搜索 brief 即使带 catalog_route={"cat": "live"}，
    # libfilter 路由门（_KNOWN_CATALOG_CATS 校验）目前也会丢弃它，
    # 且类型名映射 catalog_cat_for_type("直播") 会回退到 "tv"。
    assert "live" in libfilter._KNOWN_CATALOG_CATS
    assert "sports" in libfilter._KNOWN_CATALOG_CATS
    assert "music" in libfilter._KNOWN_CATALOG_CATS
    brief = {"catalog_route": {"source": "ffzy", "type_id": "11", "cat": "live"},
             "type_name": "直播", "lines": {}}
    assert "catalog:live" in libfilter.brief_lib_keys(brief)
