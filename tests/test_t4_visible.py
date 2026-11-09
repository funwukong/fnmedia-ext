"""T4 远程源：导入订阅时 type=4 不再静默丢弃，单独成组返回、管理页显示。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import tvbox


SITES = [
    {"name": "T4源A", "type": 4, "api": "http://t4.example/api", "ext": "x"},
    {"name": "T4源A重复", "type": 4, "api": "http://t4.example/api"},
    {"name": "T4坏地址", "type": 4, "api": "ftp://t4.example/api"},
    {"name": "普通CMS", "type": 1, "api": "https://cms.example/api.php/provide/vod/at/json"},
]


def test_type4_collected_not_skipped():
    groups = tvbox.classify_subscription(SITES)
    # T4 源A（http）与 T4坏地址（ftp）都收录展示，ftp 的标 invalid，不进 skipped
    assert len(groups["t4"]) == 2
    assert groups["t4"][0]["name"] == "T4源A"
    assert groups["t4"][0]["api"] == "http://t4.example/api"
    bad = [x for x in groups["t4"] if x["name"] == "T4坏地址"][0]
    assert bad["invalid"] is True
    # T4 不再进 skipped（之前是“不支持的类型 type=4”）
    assert not any("T4" in s.get("name", "") for s in groups["skipped"])
    # 普通 CMS 不受影响
    assert len(groups["cms"]) == 1


def test_admin_ui_renders_t4_section():
    ui = open(os.path.join(os.path.dirname(__file__), "..", "proxy", "admin_ui.py"),
              encoding="utf-8").read()
    assert "T4 远程源" in ui
    assert "d.t4" in ui
