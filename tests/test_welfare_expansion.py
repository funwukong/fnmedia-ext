"""福利扩围（第 5 批 upstream-merge）：成人类目按真源分类表收编。

真源依据：2026-11-12 拉取 11 个内置 CMS 源的 ac=list 分类表
（baofeng/ffzy/lzi/wujin/youku/guangsu/ruyi/jisu/xinlang/dytt/s360，
11/11 拉通，共 113 个去重分类名），据此定稿：
- 并入 _ADULT_KW：“理论”（baofeng 理论片）、“三级”（wujin 港台三级）、
  “两性”（wujin 两性课堂）、“擦边”（guangsu/jisu/wujin/xinlang 擦边短剧）；
- 不加：“限制级”真源零命中；R18/18禁/激情/性感/美女/诱惑/床戏/
  大尺度/黄片/毛片真源亦零命中。
- 基线 7 词（福利/伦理/情色/午夜/写真/里番/成人）保留；
  其中情色/午夜/里番/成人在本次 11 源表里零命中，但属既有基线，不动。

另修一个真源暴露的路由缺口：改前 classify() 的“以‘片’结尾 ≤4 字 →
movie”规则把“伦理片”（10 个源有）判进电影库，“短剧”规则把
“擦边短剧”判进短剧库，classify_source_class() 里 _ADULT_KW 的第二段
永远看不到它们。改后福利开关开时成人词先于常规分类判定；开关关时
常规路由与改前完全一致；classify() 本身不动。

“改前”列是本次实测值（改前代码 + 同一张 113 名真源表跑出），
不是推断；“改后”列由本文件断言。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog
from catalog import classify, classify_source_class

WELFARE_GUID = "411515e17c85709cf1966697b7b2c58e"

# （分类名, 出处源, 改前[开关开/无逐源标记], 改后[开关开/无逐源标记]）
# 出处源只列含该分类名的源；改前/改后均为 classify_source_class(name,
# adult_allowed=False) 在 _welfare_on()==True 下的返回值。
BEFORE_AFTER = [
    # 基线 7 词已覆盖的（改前已进福利，改后不变）
    ("福利", ["baofeng"], "adult", "adult"),
    ("日本伦理", ["wujin"], "adult", "adult"),
    ("韩国伦理", ["wujin"], "adult", "adult"),
    ("西方伦理", ["wujin"], "adult", "adult"),
    ("写真热舞", ["wujin"], "adult", "adult"),
    # 本批新增 4 词覆盖的（改前丢弃或错进常规库，改后进福利）
    ("伦理片", ["dytt", "ffzy", "guangsu", "jisu", "lzi", "ruyi", "s360",
              "wujin", "xinlang", "youku"], "movie", "adult"),
    ("理论片", ["baofeng"], None, "adult"),
    ("港台三级", ["wujin"], None, "adult"),
    ("两性课堂", ["wujin"], None, "adult"),
    ("擦边短剧", ["guangsu", "jisu", "wujin", "xinlang"], "shortdrama", "adult"),
]

NEW_KW = ("理论", "三级", "两性", "擦边")
NOT_ADDED_KW = ("限制级", "R18", "18禁", "激情", "性感", "美女", "诱惑",
                "床戏", "大尺度", "黄片", "毛片")


def _welfare_on(monkeypatch):
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)


def _welfare_off(monkeypatch):
    monkeypatch.setattr(catalog, "_welfare_on", lambda: False)


def test_adult_keywords_expanded():
    assert catalog._ADULT_KW[:7] == ("福利", "伦理", "情色", "午夜", "写真", "里番", "成人")
    for kw in NEW_KW:
        assert kw in catalog._ADULT_KW
    for kw in NOT_ADDED_KW:
        assert kw not in catalog._ADULT_KW
    # 红线表绝不触碰。
    assert catalog._REDLINE_KW == ("萝莉", "童贞", "换脸")


def test_before_after_table(monkeypatch):
    """扩围前后覆盖对照表（开关开、无逐源标记）：改前 5 名进福利，改后 10 名。"""
    _welfare_on(monkeypatch)
    for name, _sources, _before, after in BEFORE_AFTER:
        assert classify_source_class(name, adult_allowed=False) == after, name


def test_welfare_off_routing_unchanged(monkeypatch):
    """开关关时常规路由与改前完全一致：成人词不吸走常规库内容。"""
    _welfare_off(monkeypatch)
    assert classify_source_class("伦理片", adult_allowed=False) == "movie"
    assert classify_source_class("擦边短剧", adult_allowed=False) == "shortdrama"
    assert classify_source_class("理论片", adult_allowed=False) is None
    assert classify_source_class("港台三级", adult_allowed=False) is None
    assert classify_source_class("两性课堂", adult_allowed=False) is None
    assert classify_source_class("福利", adult_allowed=False) is None


def test_marked_source_fallback_unchanged(monkeypatch):
    """逐源标记兜底轨不变：未归类非中性分类仍靠 adult_allowed 进福利；
    中性噪音仍被挡；成人词改后不再依赖该标记。"""
    _welfare_on(monkeypatch)
    # 兜底轨：改前改后一致进福利。
    assert classify_source_class("台湾剧", adult_allowed=True) == "adult"
    # 新需求：黑名单分类直接丢弃，即使源开启福利分类也不进入兜底福利库。
    assert classify_source_class("未分类", adult_allowed=True) is None
    assert classify_source_class("台湾剧", adult_allowed=False) is None
    # 中性噪音：即使源已标记也不进。
    assert classify_source_class("影视解说", adult_allowed=True) is None
    assert classify_source_class("预告片", adult_allowed=True) is None
    # 成人词：无标记也进（双轨制第一轨）。
    assert classify_source_class("伦理片", adult_allowed=False) == "adult"
    assert classify_source_class("擦边短剧", adult_allowed=False) == "adult"


def test_mainstream_routing_untouched(monkeypatch):
    """主流分类不受扩围影响（开关开/关一致）。"""
    for on in (True, False):
        monkeypatch.setattr(catalog, "_welfare_on", lambda on=on: on)
        assert classify_source_class("动作片", adult_allowed=False) == "movie"
        assert classify_source_class("国产剧", adult_allowed=False) == "tv"
        assert classify_source_class("动漫", adult_allowed=False) == "anime"
        assert classify_source_class("综艺", adult_allowed=False) == "variety"
        assert classify_source_class("纪录片", adult_allowed=False) == "documentary"
        assert classify_source_class("短剧", adult_allowed=False) == "shortdrama"
        assert classify_source_class("体育", adult_allowed=False) == "sports"
        assert classify_source_class("AI漫剧", adult_allowed=False) == "ai_comic"


def test_classify_itself_unchanged():
    """classify() 本身不动：修复只在 classify_source_class() 的顺序上，
    libfilter/tap 等 classify() 调用方行为不变。"""
    assert classify("伦理片") == "movie"
    assert classify("擦边短剧") == "shortdrama"
    assert classify("理论片") is None
    assert classify("港台三级") is None
    assert classify("两性课堂") is None
    assert classify("福利") is None


def test_redline_still_blocked(monkeypatch):
    """硬红线仍被拦截：单红线词、红线+成人词组合、红线+新词组合，开关开、
    源已标记也一律不放行；标题/备注侧的 is_redline 同样。"""
    _welfare_on(monkeypatch)
    for name in ("萝莉", "童贞", "换脸视频", "萝莉伦理", "换脸理论片",
                 "童贞写真", "萝莉三级", "换脸擦边短剧", "萝莉两性课堂"):
        assert classify_source_class(name, adult_allowed=True) is None, name
    assert catalog.is_redline("萝莉伦理片") is True
    assert catalog.is_redline("换脸理论片") is True
    assert catalog.is_redline("伦理片") is False


def test_welfare_guid_unchanged():
    """福利库 GUID 种子绝不能改：md5("fnmedia-welfare-lib")，
    app.py 与 vlibs.py 两处一致，反查一致。"""
    assert hashlib.md5(b"fnmedia-welfare-lib").hexdigest() == WELFARE_GUID
    import app as appmod
    import vlibs as vlibsmod

    assert appmod.CATALOG_LIB_GUIDS["adult"] == WELFARE_GUID
    assert vlibsmod.CATALOG_LIB_GUIDS["adult"] == WELFARE_GUID
    assert appmod.CATALOG_GUID_CAT[WELFARE_GUID] == "adult"
    assert vlibsmod.key_of_guid(WELFARE_GUID) == "catalog:adult"


def test_welfare_switch_default_off():
    """福利媒体库默认关闭，需在管理台手动开启。"""
    import config as configmod

    assert configmod.DEFAULTS["welfare_library_enabled"] is False
