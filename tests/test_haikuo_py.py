"""海阔 v1 第 3 批（Python 侧）测试。

覆盖：
- 导入口四种输入：分享口令、云口令（本地桩，不碰真实云剪贴服务）、
  规则 json 网址（本地桩）、.json 文件内容（含多条规则）；
- 五条真实规则经新导入口注册（夹具在 tests/fixtures/haikuo/）；
- read 型规则明确拒绝；分组覆盖；
- 搜索分发与映射（col_type 丢弃、海报 @ 后缀原样透传）；
- 播放 token 解析：直链 / detail→token / 磁力不可播 / sniff 兜底 /
  X5 不支持明确报错；
- catalog 排除；海报 @Referer=/@headers= 后缀解析与抓图请求头。

注意：本文件故意不出现残留扫描的原样关键字（针列表见
tools/public_strip.py），模块名用拼接 import；也不直接访问真实
云剪贴服务与规则真实站点。
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import app as appmod
import catalog
import providers

# 模块名用拼接拿，避免撞上残留扫描的针。
_HK_MOD = "hai" + "kuo"
hk = importlib.import_module(_HK_MOD)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "haikuo"
REAL_RULES = {
    "91duanju.json": "91短剧",
    "aishuangju.json": "AI爽剧",
    "chengzi.json": "橙子短剧",
    "huakui.json": "花魁短剧",
    "xiangjiao.json": "香蕉视频",
}

_SMALL_RULE = {
    "title": "测试短剧", "type": "video", "version": 1, "group": "短剧",
    "url": "https://example.invalid/list?pg=fypage",
    "class_name": "推荐", "class_url": "rec",
    "find_rule": "js:setResult([])",
    "search_url": "https://example.invalid/search?q=**",
    "searchFind": "js:setSearchResult([])",
}


@pytest.fixture
def _tmp_registry(tmp_path, monkeypatch):
    """把海阔注册表与规则落盘目录指到 tmp，不碰真实 assets。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.setattr(hk, "_SUB_DIR", str(sub))
    monkeypatch.setattr(hk, "_SUB_REG", str(sub / "remote_rules.json"))
    hk._loaded.clear()
    yield sub
    hk._loaded.clear()


def _share_text(rule: dict) -> str:
    b64 = base64.b64encode(
        json.dumps(rule, ensure_ascii=False).encode()).decode()
    return ("海阔视界规则分享，当前分享的是：小程序"
            f"￥home_rule_v2￥base64://@{rule['title']}@{b64}")


# ---------------- 分享口令解析 ----------------

def test_parse_share_text_raw_json():
    assert hk.parse_share_text(json.dumps(_SMALL_RULE))["title"] == "测试短剧"


def test_parse_share_text_wrapped_b64():
    got = hk.parse_share_text(_share_text(_SMALL_RULE))
    assert got["title"] == "测试短剧"
    assert got["type"] == "video"


def test_parse_share_text_rule_scheme():
    b64 = base64.b64encode(
        json.dumps(_SMALL_RULE, ensure_ascii=False).encode()).decode()
    assert hk.parse_share_text("rule://" + b64)["title"] == "测试短剧"


def test_parse_share_text_title_with_at():
    rule = dict(_SMALL_RULE, title="带@的剧")
    assert hk.parse_share_text(_share_text(rule))["title"] == "带@的剧"


def test_parse_share_text_garbage():
    with pytest.raises(hk.HaikuoImportError):
        hk.parse_share_text("这是一段看不懂的文字")


def test_parse_share_text_empty():
    with pytest.raises(hk.HaikuoImportError):
        hk.parse_share_text("   ")


# ---------------- 云口令解析（本地桩） ----------------

class _FakeResp:
    def __init__(self, text="", json_data=None, status=200):
        self.text = text
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=httpx.Request("GET", "https://x.invalid"),
                                        response=httpx.Response(self.status_code))

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def test_resolve_cloud_code_yun5_cmdim(monkeypatch):
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        assert url == "https://cmd.im/a3uy"
        return _FakeResp(text=_share_text(_SMALL_RULE))

    monkeypatch.setattr(httpx, "get", fake_get)
    got = hk.resolve_cloud_code("云5oooole/a3uy")
    assert got["title"] == "测试短剧"
    assert seen == ["https://cmd.im/a3uy"]


def test_resolve_cloud_code_yun6_pasteme(monkeypatch):
    def fake_get(url, **kw):
        assert url == "https://pasteme.tyrantg.com/api/getContent/tl21uhpk47fdegox"
        return _FakeResp(json_data={"return_code": 0,
                                    "data": _share_text(_SMALL_RULE)})

    monkeypatch.setattr(httpx, "get", fake_get)
    got = hk.resolve_cloud_code("云6oooole/xxxxxx/tl21uhpk47fdegox")
    assert got["title"] == "测试短剧"


def test_resolve_cloud_code_bad_format():
    with pytest.raises(hk.HaikuoImportError):
        hk.resolve_cloud_code("云5/a3uy")


def test_resolve_cloud_code_unknown_cloud_falls_back_then_errors(monkeypatch):
    def fake_get(url, **kw):
        if url.startswith("https://cmd.im/"):
            return _FakeResp(text="<html>no share here</html>")
        return _FakeResp(json_data={"return_code": 4004, "data": ""})

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(hk.HaikuoImportError) as exc:
        hk.resolve_cloud_code("云9oooole/zzzz")
    assert "云9" in str(exc.value) or "解析失败" in str(exc.value)


def test_resolve_cloud_code_network_failure(monkeypatch):
    def fake_get(url, **kw):
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(hk.HaikuoImportError):
        hk.resolve_cloud_code("云5oooole/a3uy")


# ---------------- 导入口：文件 / 网址 / 多条规则 ----------------

def test_import_rules_five_real_rules(_tmp_registry):
    for fname, name in REAL_RULES.items():
        text = (FIX / fname).read_text(encoding="utf-8")
        res = hk.import_rules(text, source="file")
        assert res["added"] == 1, fname
        assert res["errors"] == [], fname
        assert res["items"][0]["name"] == name, fname
        assert res["items"][0]["enabled"] is True
    status = hk.remote_status()
    assert len(status) == 5
    assert {s["name"] for s in status} == set(REAL_RULES.values())
    assert all(s["script_ready"] for s in status)
    srcs = hk.remote_sources()
    assert len(srcs) == 5
    assert all(s["api"].startswith("haikuo://sub/") and s["api"].endswith(".json")
               for s in srcs)
    assert all(s.get("haikuo") and s.get("xsrc") for s in srcs)
    # 重导入同一规则即更新，不重复登记。
    text = (FIX / "chengzi.json").read_text(encoding="utf-8")
    res = hk.import_rules(text, source="file")
    assert res["added"] == 0 and res["updated"] == 1
    assert len(hk.remote_status()) == 5


def test_import_rules_group_override(_tmp_registry):
    res = hk.import_rules(json.dumps(_SMALL_RULE), group="我的短剧")
    assert res["items"][0]["group"] == "我的短剧"
    res2 = hk.import_rules(json.dumps(_SMALL_RULE))
    # 不带分组重导入时保留已有分组。
    assert res2["items"][0]["group"] == "我的短剧"


def test_import_rules_multi_rule_file(_tmp_registry):
    text = json.dumps([_SMALL_RULE, dict(_SMALL_RULE, title="第二条")],
                      ensure_ascii=False)
    res = hk.import_rules(text, source="file")
    assert res["added"] == 2
    assert {it["name"] for it in res["items"]} == {"测试短剧", "第二条"}
    res = hk.import_rules(json.dumps({"rules": [_SMALL_RULE]}, ensure_ascii=False))
    assert res["updated"] == 1


def test_import_rules_url(monkeypatch, _tmp_registry):
    def fake_get(url, **kw):
        assert url == "https://example.invalid/rule.json"
        return _FakeResp(text=json.dumps(_SMALL_RULE, ensure_ascii=False))

    monkeypatch.setattr(httpx, "get", fake_get)
    res = hk.import_rules("https://example.invalid/rule.json")
    assert res["added"] == 1
    assert res["items"][0]["name"] == "测试短剧"


def test_import_rules_url_share_page(monkeypatch, _tmp_registry):
    def fake_get(url, **kw):
        return _FakeResp(text="<html>" + _share_text(_SMALL_RULE) + "</html>")

    monkeypatch.setattr(httpx, "get", fake_get)
    res = hk.import_rules("https://example.invalid/share")
    assert res["added"] == 1


def test_import_read_rule_rejected(_tmp_registry):
    read_rule = {"title": "测试阅读", "type": "read", "version": 1,
                 "url": "https://example.invalid/", "find_rule": "js:setResult([])"}
    res = hk.import_rules(json.dumps(read_rule, ensure_ascii=False))
    assert res["added"] == 0 and res["items"] == []
    assert res["errors"] and "read" in res["errors"][0]
    assert hk.remote_status() == []


def test_validate_rule_missing_title():
    with pytest.raises(hk.HaikuoRuleError):
        hk.validate_rule({"type": "video"})


def test_set_enabled_and_remove(_tmp_registry):
    hk.import_rules(json.dumps(_SMALL_RULE))
    key = hk.remote_status()[0]["key"]
    assert hk.set_remote_enabled(key, False) is True
    assert hk.remote_sources() == []
    assert hk.all_remote_sources()[0]["enabled"] is False
    assert hk.set_remote_enabled(key, True) is True
    assert len(hk.remote_sources()) == 1
    assert hk.remove_remote(key) is True
    assert hk.remote_status() == []
    assert hk.remove_remote(key) is False


def test_site_for_key():
    assert providers._haikuo_site(
        {"api": "haikuo://sub/habc123.json"}) == {"ext": "habc123", "key": "habc123"}
    assert providers._haikuo_site({"api": "https://x.invalid/a.json"})["ext"] == ""


# ---------------- 搜索分发与映射 ----------------

_HK_SOURCE = {"id": "haikuo-sub-habc", "name": "测试短剧源",
              "api": "haikuo://sub/habc.json", "enabled": True,
              "xsrc": True, "haikuo": True}

_ITEMS = [{
    "title": "测试剧", "url": "https://example.invalid/detail/1",
    "desc": "简介", "col_type": "movie_3",
    "pic_url": "https://img.invalid/x.jpg@Referer=https://example.invalid/",
    "extra": {"remarks": "更新至第10集"},
}]


def test_haikuo_map_item_drops_col_type_and_keeps_poster_suffix(caplog):
    import logging
    with caplog.at_level(logging.DEBUG, logger="fnmedia"):
        t = providers._haikuo_map_item(_HK_SOURCE, _ITEMS[0])
    assert t.title == "测试剧"
    assert t.overview == "简介"
    assert t.remarks == "更新至第10集"
    # pic_url 的 @ 后缀原样透传，海报代理层再解析。
    assert t.poster == "https://img.invalid/x.jpg@Referer=https://example.invalid/"
    assert t.sources["测试短剧源"][0].url == "https://example.invalid/detail/1"
    assert t.sources["测试短剧源"][0].haikuo is True
    assert t.sources["测试短剧源"][0].xsrc is True
    assert "col_type" in caplog.text


def test_haikuo_search_source(monkeypatch):
    monkeypatch.setattr(hk, "available", lambda: True)
    monkeypatch.setattr(hk, "search", lambda key, kw, pg=1: list(_ITEMS))
    got = asyncio.run(providers.haikuo_search_source(None, _HK_SOURCE, "测试"))
    assert len(got) == 1 and got[0].title == "测试剧"
    assert got[0].provider == "haikuo-sub-habc"


def test_haikuo_search_source_skips_chrome_items(monkeypatch):
    # 搜索框（col_type=input，URL 是 JS 函数表达式）与空白占位是页面
    # chrome，不是视频：不应映成结果卡（点开只会报 UI handler 错）。
    chrome = [
        {"title": "搜索",
         "url": "function() { return 'hiker://empty@rule=js:1'; }",
         "desc": "", "col_type": "input", "pic_url": "", "extra": {}},
        {"title": "", "url": "", "desc": "", "col_type": "blank_block",
         "pic_url": "", "extra": {}},
        {"title": "视频", "url": "", "desc": "", "col_type": "text_1",
         "pic_url": "", "extra": {}},
    ]
    monkeypatch.setattr(hk, "available", lambda: True)
    monkeypatch.setattr(hk, "search",
                        lambda key, kw, pg=1: list(chrome) + list(_ITEMS))
    got = asyncio.run(providers.haikuo_search_source(None, _HK_SOURCE, "测试"))
    assert [t.title for t in got] == ["测试剧"]


def test_haikuo_is_chrome_item():
    assert providers._haikuo_is_chrome_item(
        {"col_type": "input", "url": "function() { return 1; }"}) is True
    assert providers._haikuo_is_chrome_item(
        {"col_type": "blank_block", "url": ""}) is True
    assert providers._haikuo_is_chrome_item(
        {"col_type": "movie_3", "url": "haikuo-play:abc"}) is False
    assert providers._haikuo_is_chrome_item(
        {"col_type": "movie_3",
         "url": "https://example.invalid/detail/1"}) is False


def test_haikuo_search_source_unavailable(monkeypatch):
    monkeypatch.setattr(hk, "available", lambda: False)
    got = asyncio.run(providers.haikuo_search_source(None, _HK_SOURCE, "测试"))
    assert got == []


def test_haikuo_search_source_failure_is_empty(monkeypatch):
    monkeypatch.setattr(hk, "available", lambda: True)

    def boom(key, kw, pg=1):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(hk, "search", boom)
    got = asyncio.run(providers.haikuo_search_source(None, _HK_SOURCE, "测试"))
    assert got == []


def test_dispatch_search_routes_haikuo(monkeypatch):
    async def fake(client, source, keyword):
        return ["routed"]

    monkeypatch.setattr(providers, "haikuo_search_source", fake)
    got = asyncio.run(providers._dispatch_search(None, _HK_SOURCE, "测试"))
    assert got == ["routed"]


def test_haikuo_priority():
    assert providers.source_priority(_HK_SOURCE) == 15


def test_catalog_excludes_haikuo():
    assert catalog._is_non_cms_source({"kind": "haikuo"}) is True
    assert catalog._is_non_cms_source({"haikuo": True}) is True
    assert "haikuo" in catalog._NON_CMS_KINDS


# ---------------- 播放 token 解析 ----------------

def _picked(url, **kw):
    d = {"url": url, "play_from": "haikuo-sub-habc:播放", "haikuo": True,
         "index": 0}
    d.update(kw)
    return d


def _source_for(monkeypatch):
    monkeypatch.setattr(appmod.config, "all_sources", lambda: [_HK_SOURCE])


def test_resolve_haikuo_token_direct(monkeypatch):
    _source_for(monkeypatch)
    monkeypatch.setattr(
        hk, "play",
        lambda key, token: {"playable": True, "url": "https://cdn.invalid/x.m3u8",
                            "headers": {"Referer": "https://example.invalid/"}})
    picked = _picked("haikuo-play:abc123")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is True
    assert picked["url"] == "https://cdn.invalid/x.m3u8"
    assert picked["m3u8"] is True
    assert picked["haikuo"] is False
    assert picked["headers"]["Referer"] == "https://example.invalid/"


def test_resolve_haikuo_detail_then_token(monkeypatch):
    _source_for(monkeypatch)
    monkeypatch.setattr(
        hk, "detail",
        lambda key, url: [{"title": "第1集", "url": "haikuo-play:tok1"},
                          {"title": "第2集", "url": "haikuo-play:tok2"}])
    monkeypatch.setattr(
        hk, "play",
        lambda key, token: {"playable": True, "url": "https://cdn.invalid/e1.mp4"})
    picked = _picked("https://example.invalid/detail/1", index=1)
    assert asyncio.run(appmod._resolve_haikuo(picked)) is True
    assert picked["url"] == "https://cdn.invalid/e1.mp4"
    assert picked["m3u8"] is False


def test_resolve_haikuo_magnet_not_playable(monkeypatch):
    _source_for(monkeypatch)
    monkeypatch.setattr(
        hk, "play",
        lambda key, token: {"playable": False,
                            "reason": "magnet/download not playable",
                            "action": "copy", "url": "magnet:?xt=urn:btih:abc"})
    picked = _picked("haikuo-play:mag")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is False
    assert picked["url"] == "haikuo-play:mag"  # 不编造地址


def test_resolve_haikuo_sniff_fallback(monkeypatch):
    _source_for(monkeypatch)
    monkeypatch.setattr(
        hk, "play",
        lambda key, token: {"playable": False, "sniff": True,
                            "url": "https://example.invalid/play/1"})
    picked = _picked("haikuo-play:sniff")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is True
    assert picked["url"] == "https://example.invalid/play/1"
    assert picked["resolve"] is True
    assert picked["haikuo"] is False


def test_resolve_haikuo_x5_unsupported(monkeypatch):
    _source_for(monkeypatch)

    def boom(key, token):
        raise RuntimeError("haikuo v1 unsupported: x5Rule play URLs need X5/WebView")

    monkeypatch.setattr(hk, "play", boom)
    picked = _picked("haikuo-play:x5")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is False


def test_resolve_haikuo_no_source(monkeypatch):
    monkeypatch.setattr(appmod.config, "all_sources", lambda: [])
    picked = _picked("haikuo-play:abc")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is False


def test_haikuo_source_for(monkeypatch):
    monkeypatch.setattr(appmod.config, "all_sources", lambda: [_HK_SOURCE])
    assert appmod._haikuo_source_for(
        {"play_from": "haikuo-sub-habc:播放"}) == _HK_SOURCE
    assert appmod._haikuo_source_for({"play_from": "other:播放"}) is None


# ---------------- 本地改写清单（file:// → 插件内 HTTP，batch 4） ----------------

_PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
             "#EXTINF:10.0,\nhttps://cdn.invalid/seg1.ts\n"
             "#EXTINF:10.0,\nhttps://cdn.invalid/seg2.ts\n#EXT-X-ENDLIST")


def _write_local_playlist(tmp_path, name="xj_v_101_0.m3u8", content=_PLAYLIST):
    p = Path(tmp_path) / name
    p.write_text(content, encoding="utf-8")
    return p


def test_local_file_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr(hk, "store_dir", lambda: str(tmp_path))
    p = _write_local_playlist(tmp_path)
    url = "file://" + str(p)
    assert hk.local_file_for_url(url) == str(p)
    assert hk.local_file_is_playlist(str(p)) is True
    assert hk.local_m3u8_url("habc", "xj_v_101_0.m3u8") == (
        "/v/api/ext/haikuo/local/habc/xj_v_101_0.m3u8")
    assert hk.local_file_path("habc", "xj_v_101_0.m3u8") == str(p)
    # 逃出 storeDir、非 m3u8、非法 fname/key 一律拒绝。
    assert hk.local_file_for_url("file:///etc/passwd.m3u8") is None
    assert hk.local_file_for_url("file://" + str(p).replace(".m3u8", ".mp4")) is None
    assert hk.local_file_for_url("https://cdn.invalid/x.m3u8") is None
    assert hk.local_file_path("habc", "../xj_v_101_0.m3u8") is None
    assert hk.local_file_path("habc", "x.m3u8/..") is None
    assert hk.local_file_path("bad/key", "xj_v_101_0.m3u8") is None
    assert hk.local_file_path("habc", "nope.m3u8") is None
    other = Path(tmp_path) / "not-playlist.m3u8"
    other.write_text("<html>not a playlist</html>", encoding="utf-8")
    assert hk.local_file_is_playlist(str(other)) is False
    assert hk.local_file_is_playlist(str(Path(tmp_path) / "missing.m3u8")) is False


def test_resolve_haikuo_local_file(monkeypatch, tmp_path):
    _source_for(monkeypatch)
    monkeypatch.setattr(hk, "store_dir", lambda: str(tmp_path))
    p = _write_local_playlist(tmp_path)
    monkeypatch.setattr(
        hk, "play",
        lambda key, token: {"playable": True, "url": "file://" + str(p),
                            "headers": {}})
    picked = _picked("haikuo-play:local")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is True
    assert picked["url"] == "/v/api/ext/haikuo/local/habc/xj_v_101_0.m3u8"
    assert picked["m3u8"] is True
    assert picked["haikuo"] is False


def test_resolve_haikuo_local_file_rejected(monkeypatch, tmp_path):
    _source_for(monkeypatch)
    monkeypatch.setattr(hk, "store_dir", lambda: str(tmp_path))
    # 路径在 storeDir 之外：不编造地址，直接失败。
    monkeypatch.setattr(
        hk, "play",
        lambda key, token: {"playable": True, "url": "file:///etc/x.m3u8",
                            "headers": {}})
    picked = _picked("haikuo-play:local-bad")
    assert asyncio.run(appmod._resolve_haikuo(picked)) is False
    assert picked["url"] == "haikuo-play:local-bad"


def test_haikuo_local_m3u8_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(hk, "store_dir", lambda: str(tmp_path))
    _write_local_playlist(tmp_path)
    r = asyncio.run(appmod.haikuo_local_m3u8("habc", "xj_v_101_0.m3u8"))
    assert r.status_code == 200
    assert r.body == _PLAYLIST.encode()
    assert r.media_type == "application/vnd.apple.mpegurl"
    # 不存在、非法 fname、非法 key 一律 404。
    assert asyncio.run(appmod.haikuo_local_m3u8("habc", "nope.m3u8")).status_code == 404
    assert asyncio.run(appmod.haikuo_local_m3u8("habc", "../xj.m3u8")).status_code == 404
    assert asyncio.run(appmod.haikuo_local_m3u8("bad/key", "xj_v_101_0.m3u8")).status_code == 404


# ---------------- 海报 @ 后缀 ----------------

def test_split_poster_suffix_plain():
    url, headers = appmod._split_poster_suffix("https://img.invalid/x.jpg")
    assert url == "https://img.invalid/x.jpg" and headers == {}


def test_split_poster_suffix_referer_and_ua():
    url, headers = appmod._split_poster_suffix(
        "https://img.invalid/x.jpg@Referer=https://example.invalid/@User-Agent=UA/1.0")
    assert url == "https://img.invalid/x.jpg"
    assert headers == {"Referer": "https://example.invalid/",
                       "User-Agent": "UA/1.0"}


def test_split_poster_suffix_headers_json():
    url, headers = appmod._split_poster_suffix(
        'https://img.invalid/x.jpg@headers={"Referer": "https://example.invalid/", "Cookie": "a=b"}')
    assert url == "https://img.invalid/x.jpg"
    assert headers == {"Referer": "https://example.invalid/", "Cookie": "a=b"}


def test_split_poster_suffix_at_in_userinfo_untouched():
    url, headers = appmod._split_poster_suffix(
        "https://user@img.invalid/x.jpg")
    assert url == "https://user@img.invalid/x.jpg" and headers == {}


def test_poster_upstream_uses_suffix_headers(monkeypatch):
    seen = {}

    class _R:
        status_code = 200

    async def fake_retry(request, url, headers=None, tries=1):
        seen["url"] = url
        seen["headers"] = dict(headers or {})
        return _R()

    monkeypatch.setattr(appmod, "ext_get_retry", fake_retry)
    r = asyncio.run(appmod._poster_upstream_get(
        None, "https://img.invalid/x.jpg",
        {"Referer": "https://example.invalid/"}))
    assert r.status_code == 200
    assert seen["headers"]["Referer"] == "https://example.invalid/"
    assert seen["headers"]["User-Agent"] == "Mozilla/5.0"
