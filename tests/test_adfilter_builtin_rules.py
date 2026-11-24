"""内置片源规则：原 fnmedia-ad-rules.json 已并入 adfilter 源码。

背景：仓库根目录曾放一份 `fnmedia-ad-rules.json`（18 组 / 93 条 host 规则），
需要手动维护、且构建/分发容易漏带。现改为源码常量 `BUILTIN_SOURCE_RULES`，
运行时不依赖任何外部 JSON；本地订阅规则仍可叠加。

覆盖点：
1. 内置规则完整装载（组数 / 规则数 / 关键 host 命中）；
2. `effective_rules()` = 内置 + 本地，按 (hosts, regex) 去重；
3. `_rules_for()` 对命中 host 返回规则、对未知 host 返回空；
4. 内置规则真正参与 m3u8 清理（DURATION 型广告分片被删）；
5. 管理页 `replace_rules()` 只替换本地规则，内置规则不受影响；
6. 仓库根目录不再需要外部规则文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import adfilter  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """把本地规则文件指到临时目录，避免读写真实 state/。"""
    monkeypatch.setattr(adfilter, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(adfilter, "_RULES_FILE", tmp_path / "ad_rules.json")
    monkeypatch.setattr(adfilter, "enabled", lambda: True)
    return tmp_path


def test_builtin_rules_are_complete():
    rules = adfilter.builtin_rules()
    assert len(rules) >= 18, len(rules)
    assert sum(len(r["regex"]) for r in rules) >= 90
    for g in rules:
        assert g["hosts"], g
        assert g["regex"], g
        # durations 已折算成纯时长 regex，不应残留 durations 字段依赖
        assert all(isinstance(x, str) and x for x in g["regex"])


def test_builtin_rules_cover_known_sources():
    hosts = {h for g in adfilter.builtin_rules() for h in g["hosts"]}
    for expected in ("ffzy", "magnet", "bfzy", "haiwaikan", "hmrvideo"):
        assert any(expected in h for h in hosts), (expected, sorted(hosts)[:10])


def test_rules_for_matches_known_host_and_ignores_unknown(isolated_state):
    hit = adfilter._rules_for("https://vip.ffzy.com/x/main.m3u8")
    assert hit, "ffzy host 应命中内置规则"
    assert adfilter._rules_for("https://no-such-cdn.example.com/a.m3u8") == []


def test_effective_rules_merge_local_without_duplicates(isolated_state):
    base = len(adfilter.effective_rules())
    adfilter.merge_subscription_rules([
        {"name": "自定义", "hosts": ["custom-cdn"], "regex": ["#EXTINF:9.5"]},
    ])
    merged = adfilter.effective_rules()
    assert len(merged) == base + 1
    # 重复导入同一组不应增长
    adfilter.merge_subscription_rules([
        {"name": "自定义", "hosts": ["custom-cdn"], "regex": ["#EXTINF:9.5"]},
    ])
    assert len(adfilter.effective_rules()) == base + 1
    assert "custom-cdn" in {h for g in merged for h in g["hosts"]}


def test_replace_rules_keeps_builtin(isolated_state):
    base = len(adfilter.builtin_rules())
    adfilter.replace_rules([{"name": "only", "hosts": ["x"], "regex": ["#EXTINF:1"]}])
    assert len(adfilter._load_rules()) == 1
    # 内置规则仍是全量，不会被管理页替换清空
    assert len(adfilter.builtin_rules()) == base
    assert len(adfilter.effective_rules()) == base + 1
    assert adfilter._rules_for("https://vip.ffzy.com/a.m3u8"), "内置规则必须继续生效"


def test_builtin_rules_actually_strip_ad_segments(isolated_state):
    """内置 duration 规则要真的删掉命中时长的广告分片。"""
    # 看影网(fentvoss) 内置 durations 含 24.88
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXTINF:24.88,\n"
        "https://fentvoss.example/ad1.ts\n"
        "#EXTINF:23.0,\n"
        "https://fentvoss.example/main1.ts\n"
        "#EXTINF:23.0,\n"
        "https://fentvoss.example/main2.ts\n"
    )
    out, removed = adfilter.filter_hls(
        playlist, "https://fentvoss.example/x/main.m3u8", use_heuristic=False)
    assert "ad1.ts" not in out
    assert "main1.ts" in out and "main2.ts" in out
    assert removed >= 1


def test_no_external_rules_file_needed():
    """规则已内置，仓库根目录不应再依赖 fnmedia-ad-rules.json。"""
    assert not (ROOT / "fnmedia-ad-rules.json").exists()
    assert adfilter.builtin_rules(), "内置规则必须常驻源码"
