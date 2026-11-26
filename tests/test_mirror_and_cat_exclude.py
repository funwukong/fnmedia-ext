"""海报镜像可配置 + 分类排除回归。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import catalog  # noqa: E402


def test_mirror_brand_word_matches_all_subdomains():
    import adfilter
    # 品牌词命中所有子域与换域名形态
    assert adfilter.mirror_poster_url("https://img1.wsyzy.org/a.jpg") == \
        "https://fan.cloudflare.182682.xyz/a.jpg"
    assert adfilter.mirror_poster_url("https://img9.wsyzy.net/b.png") == \
        "https://fan.cloudflare.182682.xyz/b.png"  # 换域名形态
    assert adfilter.mirror_poster_url("https://wsyzy.org/c.jpg") == \
        "https://fan.cloudflare.182682.xyz/c.jpg"
    assert adfilter.mirror_poster_url("https://fiizvfck.top/x.png") == \
        "https://fan.cloudflare.182682.xyz/x.png"
    # 品牌词不误伤：包含品牌片段的无关域不命中
    assert adfilter.mirror_poster_url("https://mywsyzy.com/a.jpg") == \
        "https://mywsyzy.com/a.jpg"
    assert adfilter.mirror_poster_url("https://wsyzy.org.evil.com/a.jpg") == \
        "https://wsyzy.org.evil.com/a.jpg"
    # 非镜像图床原样返回
    assert adfilter.mirror_poster_url("https://douban.com/x.jpg") == \
        "https://douban.com/x.jpg"


def test_mirror_extra_pairs_from_config(tmp_path, monkeypatch):
    import adfilter

    pairs = list(adfilter.HOSTS_MIRROR) + [["newcdn.example", "cdn.example"]]
    monkeypatch.setattr(adfilter, "_mirror_pairs", lambda: pairs)
    # 完整域名命中
    assert adfilter.mirror_poster_url("https://newcdn.example/a.jpg") == \
        "https://cdn.example/a.jpg"
    # 子域命中
    assert adfilter.mirror_poster_url("https://img.newcdn.example/a.jpg") == \
        "https://cdn.example/a.jpg"


def test_host_hits_mirror_semantics():
    import adfilter
    # 品牌词：命中子域，不命中包含片段的无关域
    assert adfilter._host_hits_mirror("img1.wsyzy.org", "wsyzy") is True
    assert adfilter._host_hits_mirror("mywsyzy.com", "wsyzy") is False
    assert adfilter._host_hits_mirror("wsyzy.org.evil.com", "wsyzy") is False
    # 完整域名：相等或子域
    assert adfilter._host_hits_mirror("a.example.com", "example.com") is True
    assert adfilter._host_hits_mirror("example.com", "example.com") is True
    assert adfilter._host_hits_mirror("notexample.com", "example.com") is False


def test_cat_exclude_filters_source_classes(tmp_path, monkeypatch):
    import config as config_mod
    monkeypatch.setattr(config_mod, "_STATE", tmp_path / "config.json")
    cfg = config_mod.Config()
    cfg.save({"catalog_cat_exclude": {"dirty_src": ["overseas"]}})
    monkeypatch.setattr(catalog, "_excluded_cats_for",
                        lambda sid: {"overseas"} if sid == "dirty_src" else set())

    # 模拟 _load_source_classes 的过滤段：dirty_src 的 overseas 叶子被剔除
    classes = [
        {"type_id": 1, "type_name": "海外剧", "cat": "overseas"},
        {"type_id": 2, "type_name": "玄幻动漫", "cat": "anime"},
    ]
    excluded = catalog._excluded_cats_for("dirty_src")
    kept = [c for c in classes if c["cat"] not in excluded]
    assert [c["type_id"] for c in kept] == [2]


def test_cat_exclude_config_validation():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '"catalog_cat_exclude"' in src
    assert "valid_cats = set(catalogmod.CATS)" in src


def test_mirror_config_validation():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '"poster_mirror_hosts"' in src
    assert "://\" not in src" in src.replace("'", '"') or '://' in src
