"""T4 远程源：导入订阅时 type=4 不再静默丢弃，单独成组返回、管理页显示。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import tvbox


SITES = [
    {"name": "T4源A", "type": 4, "api": "http://t4.example/api", "ext": "x"},
    {"name": "T4源A重复", "type": 4, "api": "http://t4.example/api", "ext": "x"},
    {"name": "T4坏地址", "type": 4, "api": "ftp://t4.example/api"},
    {"name": "普通CMS", "type": 1, "api": "https://cms.example/api.php/provide/vod/at/json"},
]


def test_type4_collected_not_skipped():
    groups = tvbox.classify_subscription(SITES)
    # T4 源A（http）归一化进 t4 组（重复的同 api+ext 去重）；ftp 地址无法接入，
    # 按上游行为进 skipped 并注明原因，不再以 invalid 占位收进 t4 组。
    assert len(groups["t4"]) == 1
    assert groups["t4"][0]["name"] == "T4源A"
    assert groups["t4"][0]["api"] == "http://t4.example/api"
    assert groups["t4"][0]["kind"] == "t4"
    bad = [s for s in groups["skipped"] if s.get("name") == "T4坏地址"]
    assert len(bad) == 1
    assert "http" in bad[0]["reason"]
    # 普通 CMS 不受影响
    assert len(groups["cms"]) == 1


def test_admin_ui_renders_t4_section():
    ui = open(os.path.join(os.path.dirname(__file__), "..", "proxy", "admin_ui.py"),
              encoding="utf-8").read()
    assert "T4 远程源" in ui
    assert "d.t4" in ui
