import pansou


def test_share_identity_dedup_ignores_tracking_but_keeps_access_code():
    base = "https://pan.quark.cn/s/abc"
    assert pansou._pansou_share_identity(base + "?utm_source=x") == \
           pansou._pansou_share_identity(base + "/?from=channel")
    assert pansou._pansou_share_identity(base + "?pwd=1111") != \
           pansou._pansou_share_identity(base + "?pwd=2222")


def test_pansou_search_cache_key_includes_endpoint_clouds_and_source(monkeypatch):
    monkeypatch.setattr(pansou, "_pansou_base_url", lambda: "https://pansou-a")
    monkeypatch.setattr(pansou, "_pansou_cloud_types", lambda: ["quark"])
    monkeypatch.setattr(pansou, "_pansou_src", lambda: "all")
    a = pansou._pansou_search_cache_key("测试 第01集")
    assert a == pansou._pansou_search_cache_key("测试")
    monkeypatch.setattr(pansou, "_pansou_base_url", lambda: "https://pansou-b")
    assert a != pansou._pansou_search_cache_key("测试")


def test_manual_share_add_dedupes_tracking_variants(monkeypatch):
    monkeypatch.setattr(pansou, "_pan_match", lambda u: {"key": "quark"})
    mg = "test-manual-share-dedupe"
    pansou._pansou_reset(mg)
    pansou._pansou_set_manual_share(mg, "https://pan.quark.cn/s/abc?utm_source=a")
    pansou._pansou_set_manual_share(mg, "https://pan.quark.cn/s/abc?from=b")
    assert len(pansou._PANSOU_STATE[mg]["manual_shares"]) == 1
    pansou._pansou_reset(mg)
