"""P1-7：苹果 CMS at/xml 订阅解析 + 入库归一。

- providers.parse_cms_xml：XML（RSS）转成与 at/json 等价的 dict，
  与 JSON 路径共用 _map_vod / 同一返回结构 {code:1, list:[vod...]}。
- search_source / probe_source：/at/xml 源走 XML 解析，不再无条件 r.json()。
- config.add_source：新入库的 /at/xml 归一为 /at/json（与 TVBox 导入
  路径一致）；已入库源不动；JSON 路径不受影响。
"""
import asyncio
import json

import providers
import config as config_mod


XML_OK = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><list page="1" pagecount="1" pagesize="10" recordcount="2">
<video><id>101</id><last>2026-11-09 10:00:00</last><name><![CDATA[测试剧]]></name><type>国产剧</type><pic>https://cdn.example.com/101.jpg</pic><lang>国语</lang><area>大陆</area><year>2026</year><note>更新至第2集</note><actor>演员A</actor><director>导演B</director><des><![CDATA[简介文字]]></des><dl><dd flag="m3u8"><![CDATA[第1集$https://cdn.example.com/101-1.m3u8#第2集$https://cdn.example.com/101-2.m3u8]]></dd><dd flag="mp4"><![CDATA[第1集$https://cdn.example.com/101-1.mp4]]></dd></dl></video>
<video><id>102</id><name>无播放源</name><type>电影</type><year>2025</year><pic></pic></video>
</list></rss>
"""

XML_SRC = {"id": "xmltest", "api": "https://example.com/api.php/provide/vod/at/xml"}
JSON_SRC = {"id": "jsontest", "api": "https://example.com/api.php/provide/vod/at/json"}


class _Resp:
    def __init__(self, status=200, text="", json_data=None):
        self.status_code = status
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.resp


class _FakeNetutilClient:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        return self.resp


def test_parse_cms_xml_list_maps_to_json_shape():
    j = providers.parse_cms_xml(XML_OK)
    assert isinstance(j, dict) and j["code"] == 1
    assert len(j["list"]) == 2
    v = j["list"][0]
    assert v["vod_id"] == "101"
    assert v["vod_name"] == "测试剧"
    assert v["vod_area"] == "国语"  # lang 优先于 area
    assert v["vod_year"] == "2026"
    assert v["vod_remarks"] == "更新至第2集"
    assert v["vod_pic"] == "https://cdn.example.com/101.jpg"
    assert v["vod_content"] == "简介文字"
    assert v["vod_actor"] == "演员A"
    assert v["vod_play_from"] == "m3u8$$$mp4"
    assert "101-1.m3u8" in v["vod_play_url"]
    assert "$$$" in v["vod_play_url"]
    # 无播放源的条目也保留（与 JSON 路径一致，由 _map_vod 决定是否可播）
    assert j["list"][1]["vod_id"] == "102"
    assert j["list"][1]["vod_play_from"] == ""


def test_parse_cms_xml_bad_input():
    assert providers.parse_cms_xml("") is None
    assert providers.parse_cms_xml('{"code": 1}') is None  # JSON 不是 XML
    assert providers.parse_cms_xml("<rss><unclosed>") is None
    assert providers.parse_cms_xml("<rss><channel></channel></rss>") == {
        "code": 1, "list": []}  # 无 <list>：空列表，不报错


def test_is_xml_api_case_insensitive():
    assert providers._is_xml_api("https://x/api.php/provide/vod/at/xml")
    assert providers._is_xml_api("https://x/api.php/provide/vod/AT/XML?token=1")
    assert not providers._is_xml_api("https://x/api.php/provide/vod/at/json")
    assert not providers._is_xml_api("")


def test_search_source_xml(monkeypatch):
    monkeypatch.setattr(providers, "_health", None)
    out = asyncio.run(providers.search_source(
        _FakeClient(_Resp(text=XML_OK)), XML_SRC, "测试剧"))
    assert len(out) == 2  # 无播放源的条目也映射（与 JSON 路径一致）
    t = out[0]
    assert t.title == "测试剧"
    assert t.vod_id == "101"
    assert t.poster == "https://cdn.example.com/101.jpg"
    assert set(t.sources) == {"m3u8", "mp4"}
    assert len(t.sources["m3u8"]) == 2
    assert t.sources["m3u8"][0].direct is True
    assert t.sources["m3u8"][0].url == "https://cdn.example.com/101-1.m3u8"
    assert out[1].title == "无播放源"
    assert out[1].sources == {}


def test_search_source_json_unaffected(monkeypatch):
    monkeypatch.setattr(providers, "_health", None)
    j = {"code": 1, "list": [{
        "vod_id": 7, "vod_name": "JSON剧", "vod_year": 2024,
        "vod_play_from": "线路1",
        "vod_play_url": "第1集$https://cdn.example.com/7.m3u8"}]}
    out = asyncio.run(providers.search_source(
        _FakeClient(_Resp(text='{"code":1}', json_data=j)), JSON_SRC, "JSON剧"))
    assert len(out) == 1 and out[0].title == "JSON剧"
    assert out[0].sources["线路1"][0].url == "https://cdn.example.com/7.m3u8"


def test_probe_source_xml(monkeypatch):
    monkeypatch.setattr(providers.netutil, "async_client",
                        lambda timeout=None: _FakeNetutilClient(
                            _Resp(text=XML_OK)))
    r = asyncio.run(providers.probe_source(XML_SRC["api"], keyword="测试剧"))
    assert r["ok"] is True
    assert r["count"] == 2
    assert r["sample"] == "测试剧"
    assert r["direct"] == 1  # 按条目计：101 号含直链，102 号无播放源


def test_probe_source_xml_empty_list_falls_back(monkeypatch):
    # 关键词无结果但接口合法：ac=list 回退同样走 XML 解析，返回 ok。
    empty = ('<?xml version="1.0"?><rss><list page="1" pagecount="0"'
             ' pagesize="10" recordcount="0"></list></rss>')
    monkeypatch.setattr(providers.netutil, "async_client",
                        lambda timeout=None: _FakeNetutilClient(
                            _Resp(text=empty)))
    r = asyncio.run(providers.probe_source(XML_SRC["api"], keyword="不存在的词"))
    assert r["ok"] is True
    assert r["count"] == 0
    assert "接口正常" in r["error"]


def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


def test_add_source_normalizes_at_xml(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    ok, sid = c.add_source(
        "XML站", "https://xml.example.com/api.php/provide/vod/at/xml?token=abc")
    assert ok is True
    stored = next(s for s in c.get()["sources"] if s["id"] == sid)
    assert stored["api"] == (
        "https://xml.example.com/api.php/provide/vod/at/json?token=abc")
    assert sid == config_mod.Config.make_custom_id(stored["api"])


def test_add_source_json_unchanged_and_xml_json_dedup(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    ok, sid = c.add_source(
        "JSON站", "https://j.example.com/api.php/provide/vod/at/json")
    assert ok is True
    assert next(s for s in c.get()["sources"]
                if s["id"] == sid)["api"].endswith("/at/json")
    # 同一站点换 /at/xml 再加：归一后与已入库的 json 地址重复，拒绝
    ok2, msg = c.add_source(
        "XML站", "https://j.example.com/api.php/provide/vod/at/xml")
    assert ok2 is False
    assert "已存在" in msg


def test_add_source_existing_xml_source_untouched(monkeypatch, tmp_path):
    state = _patch_state(monkeypatch, tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "config_version": 2,
        "sources": [{"id": "custom_old", "name": "旧XML站",
                     "api": "https://old.example.com/api.php/provide/vod/at/xml",
                     "enabled": True, "builtin": False}],
    }), encoding="utf-8")
    c = config_mod.Config()
    stored = next(s for s in c.get()["sources"] if s["id"] == "custom_old")
    assert stored["api"].endswith("/at/xml")  # 已入库的不迁移
