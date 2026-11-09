"""媒体库可见权限：默认全放行、名单允许/拒绝、身份未知时隐藏受限库。"""
import asyncio
import hashlib
import types

import pytest

import config as config_mod
import identity as identitymod
import visibility as vis


LIB = "e91dd7347a49dbd874fb8a0e78ad7ad7"  # 海量电影库 guid


# ---------- visibility 纯逻辑 ----------

def test_default_allows_everyone():
    assert vis.is_visible({}, LIB, "alice") is True
    assert vis.is_visible({"library_visible_users": {}}, LIB, "alice") is True
    assert vis.is_visible({"library_visible_users": {LIB: []}}, LIB, "alice") is True
    assert vis.is_visible({"library_visible_users": {LIB: None}}, LIB, "alice") is True
    # 别的库有限制，不影响本库
    assert vis.is_visible({"library_visible_users": {"other": ["bob"]}},
                           LIB, "alice") is True


def test_allow_list_allow_and_deny():
    cfg = {"library_visible_users": {LIB: ["alice", "bob"]}}
    assert vis.is_visible(cfg, LIB, "alice") is True
    assert vis.is_visible(cfg, LIB, "bob") is True
    assert vis.is_visible(cfg, LIB, "carol") is False
    # 用户名精确匹配，不做大小写折叠
    assert vis.is_visible(cfg, LIB, "Alice") is False


def test_unknown_identity_fails_closed():
    cfg = {"library_visible_users": {LIB: ["alice"]}}
    # 身份未知时隐藏受限库（fail-closed）；未配限制的库仍全部可见。
    assert vis.is_visible(cfg, LIB, None) is False
    assert vis.is_visible({"library_visible_users": {}}, LIB, None) is True


def test_allowed_users_normalization():
    assert vis.allowed_users_for({"library_visible_users": {LIB: "alice"}}, LIB) == ["alice"]
    cfg = {"library_visible_users": {LIB: [" alice ", "bob", "alice", "", "  "]}}
    assert vis.allowed_users_for(cfg, LIB) == ["alice", "bob"]
    assert vis.allowed_users_for({"library_visible_users": "nope"}, LIB) is None


def test_restrictions_configured():
    assert vis.restrictions_configured({}) is False
    assert vis.restrictions_configured({"library_visible_users": {}}) is False
    assert vis.restrictions_configured({"library_visible_users": {LIB: []}}) is False
    assert vis.restrictions_configured({"library_visible_users": {LIB: ["alice"]}}) is True
    assert vis.restrictions_configured({"library_visible_users": {LIB: "alice"}}) is True


def test_normalize_users():
    assert vis.normalize_users("alice，bob, alice , ") == ["alice", "bob"]
    assert vis.normalize_users(["alice", " ", "bob"]) == ["alice", "bob"]
    assert vis.normalize_users(None) == []
    assert vis.normalize_users(123) == []
    assert len(vis.normalize_users([f"u{i}" for i in range(150)])) == vis.MAX_USERS_PER_LIB


# ---------- config 持久化 ----------

def _patch_state(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    return state


def test_config_roundtrip(monkeypatch, tmp_path):
    _patch_state(monkeypatch, tmp_path)
    c = config_mod.Config()
    assert c.get().get("library_visible_users") == {}
    assert c.lib_visible_users(LIB) == []
    ok, _ = c.set_lib_visible_users(LIB, ["alice", "bob"])
    assert ok is True
    assert c.lib_visible_users(LIB) == ["alice", "bob"]
    # 空列表=取消限制，键从配置里删掉
    ok, _ = c.set_lib_visible_users(LIB, [])
    assert ok is True
    assert c.lib_visible_users(LIB) == []
    assert LIB not in c.get().get("library_visible_users", {})
    # 非法 guid 拒绝
    ok, msg = c.set_lib_visible_users("", ["alice"])
    assert ok is False
    # 新 Config 实例读盘后限制还在
    c.set_lib_visible_users(LIB, ["carol"])
    c2 = config_mod.Config()
    assert c2.lib_visible_users(LIB) == ["carol"]


# ---------- identity：只转发客户端自己的凭证 ----------

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"code": 0, "msg": "", "data": {}}

    def json(self):
        return self._body


class _StubClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self.resp


def _req(token, client):
    return types.SimpleNamespace(
        headers={"authorization": token, "authx": "nonce=1&timestamp=2&sign=x"},
        app=types.SimpleNamespace(state=types.SimpleNamespace(up=client)))


@pytest.fixture(autouse=True)
def _clear_cache():
    identitymod._cache.clear()
    identitymod._warn_until = 0.0
    yield
    identitymod._cache.clear()


def test_current_username_forwards_own_credentials():
    client = _StubClient(_Resp(body={"code": 0, "msg": "",
                                     "data": {"username": "alice", "is_admin": 0}}))
    req = _req("Bearer tok-123", client)
    assert asyncio.run(identitymod.current_username(req)) == "alice"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "http://localhost/v/api/v1/user/info"
    # 只转发客户端自己的凭证，不自己编造
    assert call["headers"]["Authorization"] == "Bearer tok-123"
    assert call["headers"]["Authx"] == "nonce=1&timestamp=2&sign=x"
    # 第二次命中缓存，不再调上游
    assert asyncio.run(identitymod.current_username(req)) == "alice"
    assert len(client.calls) == 1
    # 缓存键是令牌摘要，令牌原文不在里面
    assert not any("tok-123" in k for k in identitymod._cache)
    assert all(len(k) == 64 for k in identitymod._cache)


def test_current_username_failure_is_none():
    client = _StubClient(_Resp(body={"code": -1, "msg": "bad", "data": None}))
    assert asyncio.run(identitymod.current_username(_req("t", client))) is None
    client2 = _StubClient(_Resp(status=401, body={"code": -2, "msg": "", "data": None}))
    assert asyncio.run(identitymod.current_username(_req("t2", client2))) is None
    # 无 Authorization 直接返回 None，不发请求
    client3 = _StubClient(_Resp())
    req = types.SimpleNamespace(headers={},
                                app=types.SimpleNamespace(state=types.SimpleNamespace(up=client3)))
    assert asyncio.run(identitymod.current_username(req)) is None
    assert client3.calls == []


def test_list_users():
    client = _StubClient(_Resp(body={"code": 0, "msg": "", "data": [
        {"guid": "g1", "username": "admin", "is_admin": 1},
        {"guid": "g2", "username": "kid", "is_admin": 0},
        {"guid": "g3", "username": "  ", "is_admin": 0},
    ]}))
    users = asyncio.run(identitymod.list_users(_req("t", client)))
    assert users == [{"guid": "g1", "username": "admin", "is_admin": 1},
                     {"guid": "g2", "username": "kid", "is_admin": 0}]
    assert client.calls[0]["url"] == "http://localhost/v/api/v1/manager/user/list"
    bad = _StubClient(_Resp(body={"code": -1, "msg": "", "data": None}))
    assert asyncio.run(identitymod.list_users(_req("t", bad))) is None


# ---------- app 接线：路由与 guid 集合 ----------

def test_app_routes_registered():
    import app as appmod
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    assert "/v/api/ext/users" in paths
    assert "/v/api/ext/lib/visibility" in paths
    assert "/v/api/ext/libs" in paths


def test_virtual_lib_guids_cover_chart_and_catalog():
    import app as appmod
    guids = appmod._all_virtual_lib_guids()
    for g in appmod.CHART_LIB_GUIDS.values():
        assert g in guids
    for g in appmod.CATALOG_LIB_GUIDS.values():
        assert g in guids


def test_canonical_lib_guid_maps_legacy_hot():
    import app as appmod
    assert appmod._canonical_lib_guid(appmod.HOT_LEGACY_GUID) == appmod.CHART_LIB_GUIDS["tv"]
    hot_movie = appmod.HOT_LIB_GUIDS["movie"]
    assert appmod._canonical_lib_guid(hot_movie) == appmod.CHART_LIB_GUIDS["movie"]
    assert appmod._canonical_lib_guid(LIB) == LIB


def test_virtual_lib_list_has_visible_users():
    import app as appmod
    libs = appmod._virtual_lib_list()
    assert libs, "至少应有热门/海量库"
    assert all("visible_users" in d for d in libs)
    assert all(isinstance(d["visible_users"], list) for d in libs)
