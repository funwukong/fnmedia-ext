"""盘上路由分类“就地修正”（v1.2.10）回归。

旧实现：只要有一个叶子的分类与当前关键词表/福利开关不一致，就把整份盘态
作废 → 触发全量重探；而 shortdrama 叶子因为父类信息没有落盘，复算必然
判不一致，等于**每次启动都要重探一轮**。

新实现：逐叶子复算，只就地改写不一致的条目（其余路由原样保留），短剧叶子
与无法归类的叶子保留原判。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog as catalogmod


def _by_src(adult: bool = False):
    return lambda sid: {"id": sid, "adult": adult}


def test_anime_compound_reclassified_in_place():
    classes = {"s1": [{"type_id": 1, "type_name": "Netflix动漫",
                       "cat": "overseas"}]}
    assert catalogmod.revalidate_route_classes(classes, _by_src()) == (1, 0)
    assert classes["s1"][0]["cat"] == "anime"


def test_genuine_overseas_untouched():
    classes = {"s1": [{"type_id": 1, "type_name": "美剧", "cat": "overseas"}]}
    assert catalogmod.revalidate_route_classes(classes, _by_src()) == (0, 0)
    assert classes["s1"][0]["cat"] == "overseas"


def test_shortdrama_leaf_preserved():
    # 「都市」这类短剧子分类的归属靠父类信息判定，父类没落盘 → 不能复算，
    # 必须保持原判（否则就是旧实现那种“每次都整份作废”）。
    classes = {"s1": [{"type_id": 1, "type_name": "都市", "cat": "shortdrama"}]}
    assert catalogmod.revalidate_route_classes(classes, _by_src()) == (0, 0)
    assert classes["s1"][0]["cat"] == "shortdrama"


def test_other_leaves_kept_not_discarded():
    classes = {"s1": [{"type_id": 1, "type_name": "Netflix动漫",
                       "cat": "overseas"},
                      {"type_id": 2, "type_name": "美剧", "cat": "overseas"}]}
    fixed, dropped = catalogmod.revalidate_route_classes(classes, _by_src())
    assert (fixed, dropped) == (1, 0)
    # 其余叶子原样保留：不是整份作废，路由还在
    assert [c["cat"] for c in classes["s1"]] == ["anime", "overseas"]
    assert len(classes["s1"]) == 2


def test_missing_cat_filled_in():
    classes = {"s1": [{"type_id": 2, "type_name": "美剧", "cat": None}]}
    fixed, dropped = catalogmod.revalidate_route_classes(classes, _by_src())
    assert (fixed, dropped) == (1, 0)
    assert classes["s1"][0]["cat"] == "overseas"


def test_non_dict_leaf_dropped_and_counted():
    classes = {"s1": [{"type_id": 2, "type_name": "美剧", "cat": "overseas"},
                      "junk"]}
    fixed, dropped = catalogmod.revalidate_route_classes(classes, _by_src())
    assert (fixed, dropped) == (0, 1)
    assert len(classes["s1"]) == 1


def test_empty_state_is_noop():
    assert catalogmod.revalidate_route_classes({}, _by_src()) == (0, 0)
