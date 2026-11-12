"""Guardfix C 组：分类库与加固源链路 + 配置同步（7 项修复的复现测试）。

每项先复现失败再修；全部用桩，不碰真实网络 / runner / 真实 state。
"""
import asyncio
import json
from pathlib import Path
from unittest import mock

import httpx
import pytest

import catalog as catalogmod
import config as config_mod
import guard as guard_mod
import guard_native as gn_mod
import providers as providers_mod
import sourcehealth

CMS_API = "https://cms.example.com/api.php"


# ---------------------------------------------------------------- fake client
class _CmsResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """记录所有 get；非 http(s) URL 按真实 httpx 抛 UnsupportedProtocol。"""

    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        url = str(url)
        self.calls.append(url)
        if not url.lower().startswith(("http://", "https://")):
            raise httpx.UnsupportedProtocol(
                f"Request URL has an unsupported protocol '{url.split(':')[0]}://'")
        if (params or {}).get("ac") == "list":
            return _CmsResp({"class": [
                {"type_id": 1, "type_name": "动作片", "type_pid": "0"},
                {"type_id": 2, "type_name": "国产剧", "type_pid": "0"},
            ]})
        return _CmsResp({"code": 1, "pagecount": 1,
                         "list": [{"vod_id": "v1", "vod_name": "测试片"}]})


class _RecordingFactory:
    def __init__(self, client):
        self.client = client

    def __call__(self, **kwargs):
        return self.client


class _NoCallClient:
    """get 一旦被调就失败：断言非 http 源根本不该发起请求。"""

    def __init__(self):
        self.calls = []

    async def get(self, *a, **k):
        self.calls.append(a)
        raise AssertionError("non-http source must not issue any request")


def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


def _cms_source():
    return {"id": "cms1", "name": "CMS", "api": CMS_API, "enabled": True}


def _guard_source(key="Fake", sid="guard-Fake"):
    return {"id": sid, "name": "Fake", "api": "guard://" + key,
            "enabled": True, "guard": True, "guard_key": key, "xsrc": True}


# ------------------------------------------------------------------ 1. catalog
def test_refresh_routes_skips_non_cms_sources():
    """分类库刷新不得把 guard/spider/drpy/catvod/xsrc 源当苹果 CMS 请求。"""
    cat = catalogmod.Catalog()
    fake = _FakeClient()
    sources = [
        _guard_source(),
        {"id": "spider-x", "name": "S", "api": "csprun://x", "enabled": True,
         "spider_src": True, "xsrc": True},
        {"id": "drpy-x", "name": "D", "api": "drpy://x.js", "enabled": True,
         "drpy": True, "xsrc": True},
        {"id": "catvod-x", "name": "C", "api": "catvod://x", "enabled": True,
         "catvod": True, "xsrc": True},
        {"id": "xsrc-x", "name": "X", "api": CMS_API, "enabled": True,
         "xsrc": True},
        {"id": "t4-x", "name": "T", "api": "http://t4.example/", "enabled": True,
         "kind": "t4"},
        _cms_source(),
    ]
    with mock.patch.object(catalogmod.netutil, "async_client",
                           _RecordingFactory(fake)):
        asyncio.run(cat.refresh_routes(sources))
    assert all(u.startswith("https://cms.example.com/")
               for u in fake.calls), fake.calls
    assert "guard-Fake" not in cat._classes
    assert "spider-x" not in cat._classes
    assert "drpy-x" not in cat._classes
    assert "catvod-x" not in cat._classes
    assert "xsrc-x" not in cat._classes
    assert "t4-x" not in cat._classes
    assert cat._classes.get("cms1"), "CMS 源仍应正常加载分类"


def test_load_source_classes_rejects_non_http_api():
    """_load_source_classes 对非 http(s) api 直接返回，不发起任何请求。"""
    cat = catalogmod.Catalog()
    client = _NoCallClient()
    asyncio.run(cat._load_source_classes(client, _guard_source()))
    assert client.calls == []
    assert "guard-Fake" not in cat._classes


def test_set_standalone_rejects_guard(monkeypatch, tmp_path):
    """guard 源不许设独立库，提示走加固专区/聚合库。"""
    _patch_state(monkeypatch, tmp_path)
    _write_guard_files(monkeypatch, tmp_path, json.dumps({"sites": [
        {"key": "Fake", "name": "Fake", "api": "csp_FakeGuard",
         "class": "com.github.catvod.spider.FakeGuard", "enabled": True,
         "searchable": True}]}))
    monkeypatch.setattr(config_mod, "DEFAULT_SOURCES",
                        [_guard_source(), _cms_source()])
    c = config_mod.Config()
    ok, msg = c.set_standalone("guard-Fake", True)
    assert ok is False
    assert "加固专区" in msg and "🛡加固影视聚合库" in msg
    ok, _msg = c.set_standalone("cms1", True)
    assert ok is True


# ------------------------------------------------------- 2. _sync_guard_defaults
def _seed_defaults(monkeypatch, entries):
    monkeypatch.setattr(config_mod, "DEFAULT_SOURCES", list(entries))


def _write_guard_files(monkeypatch, tmp_path, sites_text, imported_text=None):
    sites = tmp_path / "sites.json"
    imported = tmp_path / "imported.json"
    if sites_text is not None:
        sites.write_text(sites_text, encoding="utf-8")
    if imported_text is not None:
        imported.write_text(imported_text, encoding="utf-8")
    monkeypatch.setattr(guard_mod, "_SITES", sites)
    monkeypatch.setattr(guard_mod, "_IMPORTED", imported)
    monkeypatch.setattr(guard_mod, "_sites", None)
    return sites, imported


def test_sync_guard_defaults_keeps_table_on_corrupt_sites(monkeypatch, tmp_path):
    """sites.json 损坏（live 为空）时不得删掉全部 guard/spider 底表项。"""
    _patch_state(monkeypatch, tmp_path)
    _write_guard_files(monkeypatch, tmp_path, "{ not json !!!")
    _seed_defaults(monkeypatch, [
        {"id": "guard-Keep", "name": "Keep", "api": "guard://Keep",
         "enabled": True, "guard": True, "guard_key": "Keep"},
        {"id": "spider-Keep", "name": "SK", "api": "csprun://Keep",
         "enabled": True, "spider_src": True, "spider_key": "Keep"},
        _cms_source(),
    ])
    c = config_mod.Config()
    c._sync_guard_defaults()
    ids = {s["id"] for s in config_mod.DEFAULT_SOURCES}
    assert "guard-Keep" in ids
    assert "spider-Keep" in ids


def test_sync_guard_defaults_removes_only_truly_gone(monkeypatch, tmp_path):
    """sites.json 正常时：已不存在的源才删，存活源跟随定义更新；死条件已删。"""
    _patch_state(monkeypatch, tmp_path)
    _write_guard_files(monkeypatch, tmp_path, json.dumps({"sites": [
        {"key": "Live1", "name": "Live One", "api": "csp_Live1Guard",
         "class": "com.github.catvod.spider.Live1Guard", "enabled": True,
         "searchable": True}]}))
    _seed_defaults(monkeypatch, [
        {"id": "guard-Live1", "name": "Old Name", "api": "guard://Live1",
         "enabled": True, "guard": True, "guard_key": "Live1"},
        {"id": "guard-Gone", "name": "Gone", "api": "guard://Gone",
         "enabled": True, "guard": True, "guard_key": "Gone"},
        {"id": "guard-Locked", "name": "Locked", "api": "guard://Locked",
         "enabled": True, "guard": True, "guard_key": "Locked",
         "builtin_locked": True},
        _cms_source(),
    ])
    c = config_mod.Config()
    c._sync_guard_defaults()
    by_id = {s["id"]: s for s in config_mod.DEFAULT_SOURCES}
    assert "guard-Gone" not in by_id
    assert "guard-Locked" not in by_id  # builtin_locked 是死条件，不再拦删除
    assert by_id["guard-Live1"]["name"] == "Live One"
    assert "cms1" in by_id


# ------------------------------------------------------------- 3. load() 去重
def test_load_dedups_runtime_imported_guard_source(monkeypatch, tmp_path):
    """运行中导入的 guard 源 save + reload 后只出现 1 次。"""
    _patch_state(monkeypatch, tmp_path)
    _write_guard_files(monkeypatch, tmp_path, json.dumps({"sites": [
        {"key": "NewSrc", "name": "New", "api": "csp_NewSrcGuard",
         "class": "com.github.catvod.spider.NewSrcGuard", "enabled": True,
         "searchable": True}]}))
    base = [dict(s) for s in config_mod.DEFAULT_SOURCES]
    _seed_defaults(monkeypatch, base)
    c = config_mod.Config()
    assert any(s["id"] == "guard-NewSrc" for s in c.get()["sources"])
    c.save({})  # 运行中导入的源落盘
    c2 = config_mod.Config()
    ids = [s["id"] for s in c2.get()["sources"]]
    assert ids.count("guard-NewSrc") == 1, ids


# ---------------------------------------------------------- 4. _msearch_targets
def _fake_guard_sites(monkeypatch, sites):
    monkeypatch.setattr(guard_mod, "_load_sites", lambda: dict(sites))


def _site(key, enabled=True, searchable=True, name=None):
    return {"key": key, "name": name or key, "api": "csp_" + key + "Guard",
            "class": "com.github.catvod.spider." + key + "Guard",
            "enabled": enabled, "searchable": searchable}


def test_msearch_targets_include_newly_imported_guard(monkeypatch):
    """新导入的 guard 源进入扇出候选，不再只认硬编码 PREF。"""
    import app as appmod
    _fake_guard_sites(monkeypatch, {
        "Bttwoo": _site("Bttwoo"),
        "Jpys": _site("Jpys"),
        "NewOne": _site("NewOne", name="新源·多线"),
    })
    targets = appmod._msearch_targets()
    keys = [t["key"] for t in targets]
    assert "NewOne" in keys, keys
    assert len(keys) == 3


def test_msearch_targets_replace_dead_pref_source(monkeypatch):
    """PREF 源失效（健康死亡冷却）后由健康的新源自动替补。"""
    import app as appmod
    _fake_guard_sites(monkeypatch, {
        "Bttwoo": _site("Bttwoo"),
        "Jpys": _site("Jpys"),
        "Hmys": _site("Hmys"),
        "NewOne": _site("NewOne", name="新源·多线"),
    })
    for _ in range(5):  # 连续失败 5 次 → 搜索级死亡冷却
        sourcehealth.record("guard-Jpys", False, level="search")
    try:
        targets = appmod._msearch_targets()
        keys = [t["key"] for t in targets]
        assert "Jpys" not in keys, keys
        assert "NewOne" in keys, keys
        assert len(keys) == 3
    finally:
        sourcehealth.reset("guard-Jpys")


def test_msearch_targets_pref_seed_tiebreak(monkeypatch):
    """无健康画像、无优先级差异时，PREF 顺序仍是并列打破种子。"""
    import app as appmod
    _fake_guard_sites(monkeypatch, {
        "Bttwoo": _site("Bttwoo"),
        "Jpys": _site("Jpys"),
        "Hmys": _site("Hmys"),
        "JPJ": _site("JPJ"),
    })
    targets = appmod._msearch_targets()
    assert [t["key"] for t in targets] == ["Bttwoo", "Jpys", "Hmys"]


def test_msearch_targets_skip_disabled_and_unsearchable(monkeypatch):
    """停用 / 不可搜索的 guard 源不参与扇出。"""
    import app as appmod
    _fake_guard_sites(monkeypatch, {
        "Bttwoo": _site("Bttwoo", enabled=False),
        "Jpys": _site("Jpys", searchable=False),
        "Hmys": _site("Hmys"),
        "JPJ": _site("JPJ"),
        "NewOne": _site("NewOne"),
    })
    targets = appmod._msearch_targets()
    keys = [t["key"] for t in targets]
    assert "Bttwoo" not in keys and "Jpys" not in keys
    assert keys == ["Hmys", "JPJ", "NewOne"], keys


# ------------------------------------------------------- 5. guard_native 深预拉
def test_deep_prefetch_is_constant_off_without_config():
    """深预拉恒关闭：无 config 读取、无“手动开”误导注释。"""
    assert gn_mod._gn_deep_enabled() is False
    src = Path(gn_mod.__file__).read_text(encoding="utf-8")
    assert "guard_native_deep" not in src
    assert "手动开" not in src
    assert gn_mod._GN_DEEP_ENABLED is False


# --------------------------------------------------------- 6. guard 详情缓存失效
def _prefill_guard_caches(monkeypatch):
    guard_mod._DETAIL_CACHE.clear()
    guard_mod._BROWSE_CACHE.clear()
    guard_mod._DETAIL_CACHE["Foo|vid1"] = (0.0, {"vod_id": "vid1"})
    guard_mod._DETAIL_CACHE["Bar|vid1"] = (0.0, {"vod_id": "vid1"})
    guard_mod._BROWSE_CACHE["Foo|1|1"] = (0.0, [{"vod_id": "v"}])
    guard_mod._BROWSE_CACHE["Bar|1|1"] = (0.0, [{"vod_id": "v"}])


def _tvbox_cfg(key="Foo"):
    return json.dumps({"sites": [
        {"key": key, "name": key, "api": "csp_" + key + "Guard", "type": 3,
         "ext": "", "searchable": 1, "quickSearch": 0}], "spider": ""})


def test_import_tvbox_invalidates_detail_and_browse_cache(monkeypatch, tmp_path):
    _write_guard_files(monkeypatch, tmp_path, json.dumps({"sites": []}),
                       json.dumps({"sites": []}))
    _prefill_guard_caches(monkeypatch)
    guard_mod.import_tvbox(_tvbox_cfg("Foo"))
    assert "Foo|vid1" not in guard_mod._DETAIL_CACHE
    assert "Foo|1|1" not in guard_mod._BROWSE_CACHE
    assert "Bar|vid1" in guard_mod._DETAIL_CACHE  # 其它源缓存不受影响
    assert "Bar|1|1" in guard_mod._BROWSE_CACHE


def test_remove_imported_invalidates_cache(monkeypatch, tmp_path):
    _write_guard_files(monkeypatch, tmp_path, json.dumps({"sites": []}),
                       json.dumps({"sites": [
                           {"key": "Foo", "name": "Foo",
                            "api": "csp_FooGuard", "enabled": True}]}))
    _prefill_guard_caches(monkeypatch)
    assert guard_mod.remove_imported("Foo") is True
    assert "Foo|vid1" not in guard_mod._DETAIL_CACHE
    assert "Foo|1|1" not in guard_mod._BROWSE_CACHE
    assert "Bar|vid1" in guard_mod._DETAIL_CACHE


def test_set_enabled_invalidates_cache(monkeypatch, tmp_path):
    _write_guard_files(monkeypatch, tmp_path, json.dumps({"sites": [
        {"key": "Foo", "name": "Foo", "api": "csp_FooGuard",
         "class": "com.github.catvod.spider.FooGuard", "enabled": True}]}),
        json.dumps({"sites": []}))
    _prefill_guard_caches(monkeypatch)
    assert guard_mod.set_enabled("Foo", False) is True
    assert "Foo|vid1" not in guard_mod._DETAIL_CACHE
    assert "Foo|1|1" not in guard_mod._BROWSE_CACHE
    assert "Bar|vid1" in guard_mod._DETAIL_CACHE


# ------------------------------------------------------------- 7. guard.status
def test_status_no_sites(monkeypatch):
    """干净检出零 guard 源：status 明确报 no_sites，而不是静默无源。"""
    monkeypatch.setattr(guard_mod, "_load_sites", lambda: {})
    monkeypatch.setattr(guard_mod, "health", lambda force=False: {"ok": False})
    st = guard_mod.status()
    assert st.get("state") == "no_sites"
    assert st.get("no_sites") is True
    assert st.get("runner_down") is False


def test_status_runner_down(monkeypatch):
    """有源但 runner 不通：status 报 runner_down，与 no_sites 区分。"""
    monkeypatch.setattr(guard_mod, "_load_sites", lambda: {
        "Foo": {"key": "Foo", "name": "Foo", "enabled": True}})
    monkeypatch.setattr(guard_mod, "health",
                        lambda force=False: {"ok": False, "reason": "conn refused"})
    st = guard_mod.status()
    assert st.get("state") == "runner_down"
    assert st.get("no_sites") is False
    assert st.get("runner_down") is True


def test_readme_documents_sites_json_injection():
    """README 写明 sites.json 不随包分发、注入来源（构建哪一步放进去）。"""
    readme = (Path(__file__).resolve().parent.parent / "README.md"
              ).read_text(encoding="utf-8")
    assert "sites.json" in readme
    assert "导入 TVBox 配置" in readme
    assert "build.sh" in readme
