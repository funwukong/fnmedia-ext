"""P2 元数据补全 + 源健康自动降级。"""
import metadata as meta
import sourcehealth


def test_normalize_title():
    assert meta.normalize_title("测试剧第12集") == "测试剧"
    assert meta.normalize_title("测试剧(2024)") == "测试剧"
    assert meta.normalize_title("测试剧预告") == "测试剧"


def test_enrich_fills_empty_only():
    brief = {"title": "测试剧", "year": "2024", "area": "", "overview": "", "poster": ""}
    cands = [{"title": "测试剧", "year": "1999", "area": "大陆",
              "overview": "很长的简介", "poster": "https://x/cover.jpg"}]
    out = meta.enrich_brief(brief, cands)
    assert out["year"] == "2024"  # 已有值不被覆盖
    assert out["area"] == "大陆"
    assert out["overview"] == "很长的简介"
    assert out["poster"] == "https://x/cover.jpg"


def test_enrich_ignores_other_titles():
    brief = {"title": "测试剧", "area": ""}
    out = meta.enrich_brief(brief, [{"title": "别的剧", "area": "香港"}])
    assert out["area"] == ""


def test_dedupe_titles():
    titles = [
        {"title": "测试剧", "year": "", "overview": "短"},
        {"title": "测试剧第1集", "year": "2024", "overview": "很长很长的简介"},
        {"title": "别的剧", "year": "2023", "overview": ""},
    ]
    out = meta.dedupe_titles(titles)
    assert len(out) == 2
    main = next(t for t in out if "测试" in t["title"])
    assert main["year"] == "2024"
    assert main["overview"] == "很长很长的简介"


def test_source_health_dead_after_5_fails():
    sourcehealth.reset("p2-test-src")
    for _ in range(4):
        sourcehealth.record("p2-test-src", False, 1.0)
    assert sourcehealth.usable("p2-test-src") is True
    sourcehealth.record("p2-test-src", False, 1.0)
    assert sourcehealth.usable("p2-test-src") is False
    assert sourcehealth.is_dead("p2-test-src") is True
    # 成功一次即复活
    sourcehealth.record("p2-test-src", True, 0.5)
    assert sourcehealth.usable("p2-test-src") is True
    sourcehealth.reset("p2-test-src")


def test_source_health_ordered_dead_last():
    sourcehealth.reset("p2-a")
    sourcehealth.reset("p2-b")
    for _ in range(5):
        sourcehealth.record("p2-a", False, 1.0)
    sourcehealth.record("p2-b", True, 0.5)
    ordered = sourcehealth.ordered(["p2-a", "p2-b"])
    assert ordered == ["p2-b", "p2-a"]
    sourcehealth.reset("p2-a")
    sourcehealth.reset("p2-b")
