import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import adfilter


def test_upstream_rule_file_schema_and_load():
    rules = adfilter._load_builtin_rules()
    assert len(rules) == 18
    assert all(isinstance(g.get("hosts"), list) for g in rules)
    assert all(isinstance(g.get("regex"), list) for g in rules)


def test_builtin_rules_are_scoped_by_host():
    assert adfilter._rules_for("https://cdn.ffzy.example/a.m3u8")
    assert adfilter._rules_for("https://cdn.suonizy.example/a.m3u8")
    assert adfilter._rules_for("https://unrelated.example/a.m3u8") == []


def test_builtin_duration_rules_remove_only_matching_segment():
    text = "\n".join([
        "#EXTM3U", "#EXTINF:14.45,", "https://vip.ffzy.example/a.ts",
        "#EXTINF:8.00,", "https://vip.ffzy.example/b.ts", "#EXT-X-ENDLIST", ""
    ])
    out, removed = adfilter.filter_hls(text, "https://vip.ffzy.example/index.m3u8",
                                       use_heuristic=False)
    assert removed == 1
    assert "b.ts" in out and "a.ts" not in out


def test_rule_cleanup_guard_keeps_playlist_when_all_segments_match():
    text = "#EXTM3U\n#EXTINF:14.45,\nhttps://vip.ffzy.example/a.ts\n#EXT-X-ENDLIST\n"
    out, removed = adfilter.filter_hls(text, "https://vip.ffzy.example/index.m3u8",
                                       use_heuristic=False)
    assert out == text
    assert removed == 0
