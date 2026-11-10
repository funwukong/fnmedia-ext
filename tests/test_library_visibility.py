"""媒体库可见权限（上游 vlibs + fnusers 设计）：按稳定库键授权、只读 SQLite 识身份。

行为（fail-open，decisions-2 的 fail-closed 已回滚）：管理员始终可见；
识别不出身份的会话（known=False）按默认开关处理（默认可见、可在线搜索）；
已识别但未配置的账号默认可见（acl_default_visible=True）；只有已配置账号
的允许列表内的库才可见，管理员隐藏的库对所有人不可见。测试绝不碰真实
NAS 路径：配置用内存替身，身份库用 tmp_path 里现建的 SQLite。
"""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as config_mod
import fnusers as fnusersmod
import vlibs as vlibsmod

CHART_MOVIE = "chart:movie"
CHART_TV = "chart:tv"


def _fresh_cfg(monkeypatch):
    """把配置单例换成内存替身：DEFAULTS 深拷贝 + save 只合并不落盘。"""
    monkeypatch.setattr(config_mod.config, "_cfg",
                        copy.deepcopy(config_mod.DEFAULTS))

    def _fake_save(patch):
        cfg = config_mod.config.get()
        for k, v in patch.items():
            if k in cfg:
                cfg[k] = v
        config_mod.config._cfg = cfg

    monkeypatch.setattr(config_mod.config, "save", _fake_save)
    fnusersmod._cache.clear()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    _fresh_cfg(monkeypatch)
    yield
    fnusersmod._cache.clear()


def _user(guid="u1", known=True, is_admin=False):
    return {"known": known, "guid": guid, "name": "u1",
            "is_admin": is_admin, "token": "tok"}


ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
         "is_admin": True, "token": "tok-admin"}
UNKNOWN = {"known": False, "guid": "", "name": "", "is_admin": False,
           "token": "tok-x"}


# ---------- vlibs.account_can_see 矩阵 ----------

def test_hidden_beats_everyone():
    ok, _ = vlibsmod.set_hidden(CHART_MOVIE, True)
    assert ok is True
    assert vlibsmod.account_can_see(ADMIN, CHART_MOVIE) is False
    assert vlibsmod.account_can_see(UNKNOWN, CHART_MOVIE) is False
    assert vlibsmod.account_can_see(_user(), CHART_MOVIE) is False
    # 取消隐藏后恢复可见
    assert vlibsmod.set_hidden(CHART_MOVIE, False)[0] is True
    assert vlibsmod.account_can_see(ADMIN, CHART_MOVIE) is True


def test_fail_open_default_everyone_visible():
    # fail-open 默认：管理员、未知身份与未配置账号都可见，在线搜索同样放行。
    assert vlibsmod.acl_default_visible() is True
    assert vlibsmod.acl_default_online() is True
    assert vlibsmod.account_can_see(ADMIN, CHART_TV) is True
    assert vlibsmod.account_online_allowed(ADMIN) is True
    assert vlibsmod.account_can_see(UNKNOWN, CHART_TV) is True
    assert vlibsmod.account_online_allowed(UNKNOWN) is True
    assert vlibsmod.account_can_see(_user("u9"), CHART_TV) is True
    assert vlibsmod.account_online_allowed(_user("u9")) is True
    # user=None 同样按未知身份处理（按默认开关放行）
    assert vlibsmod.account_can_see(None, CHART_TV) is True
    assert vlibsmod.account_online_allowed(None) is True


def test_configured_account_allowlist():
    assert vlibsmod.set_account_acl("u1", ["chart:movie", "__online__"])[0] is True
    assert vlibsmod.account_can_see(_user("u1"), CHART_MOVIE) is True
    assert vlibsmod.account_can_see(_user("u1"), CHART_TV) is False
    assert vlibsmod.account_online_allowed(_user("u1")) is True
    # 没给 __online__ 标记的账号不能在线搜索
    assert vlibsmod.set_account_acl("u2", ["chart:movie"])[0] is True
    assert vlibsmod.account_online_allowed(_user("u2")) is False
    # 取消限制（allow=None）后回到默认：fail-open 下默认可见/允许在线搜索
    assert vlibsmod.set_account_acl("u1", None)[0] is True
    assert vlibsmod.account_can_see(_user("u1"), CHART_TV) is True
    assert vlibsmod.account_online_allowed(_user("u1")) is True


def test_default_can_be_switched_off():
    # 管理员在管理页把默认改成 False：已识别但未配置的账号与未知身份的会话
    # 恢复隐藏/禁在线搜索；已配置账号的允许列表不受影响；管理员仍可见。
    config_mod.config.save({"acl_default_visible": False,
                            "acl_default_online": False})
    assert vlibsmod.acl_default_visible() is False
    assert vlibsmod.account_can_see(UNKNOWN, CHART_TV) is False
    assert vlibsmod.account_online_allowed(UNKNOWN) is False
    assert vlibsmod.account_can_see(_user("u9"), CHART_TV) is False
    assert vlibsmod.account_online_allowed(_user("u9")) is False
    assert vlibsmod.account_can_see(ADMIN, CHART_TV) is True
    vlibsmod.set_account_acl("u1", [CHART_TV])
    assert vlibsmod.account_can_see(_user("u1"), CHART_TV) is True
    assert vlibsmod.account_can_see(_user("u1"), CHART_MOVIE) is False


def test_vlibs_validation():
    assert vlibsmod.set_hidden("", True)[0] is False
    assert vlibsmod.set_hidden("not-a-key", True)[0] is False
    assert vlibsmod.set_hidden("bogus:x", True)[0] is False
    assert vlibsmod.set_order(["chart:movie", "bogus:x", "chart:movie"],
                              "front", None)[0] is True
    assert vlibsmod.lib_order() == ["chart:movie"]
    assert vlibsmod.set_order([], "sideways", None)[0] is False
    assert vlibsmod.set_account_acl("", ["chart:movie"])[0] is False
    assert vlibsmod.set_account_acl("u1", "not-a-list")[0] is False
    assert vlibsmod.account_allowlist("nobody") is None


def test_key_of_guid_covers_fork_libs():
    for cat, g in appmod.CHART_LIB_GUIDS.items():
        assert vlibsmod.key_of_guid(g) == f"chart:{cat}"
    for cat, g in appmod.CATALOG_LIB_GUIDS.items():
        assert vlibsmod.key_of_guid(g) == f"catalog:{cat}"
    # 本 fork 比上游多的分类也必须有稳定键
    for cat in ("overseas", "ai_comic", "ted", "uhd", "shortdrama",
                "documentary", "adult"):
        assert vlibsmod.key_of_guid(appmod.CATALOG_LIB_GUIDS[cat]) == f"catalog:{cat}"
    assert vlibsmod.key_of_guid(appmod.T4_LIB_GUID) == "t4"
    assert vlibsmod.key_of_guid(appmod.GUARD_NATIVE_GUID) == "guard-native"
    assert vlibsmod.key_of_guid("not-a-virtual-lib") is None
    assert vlibsmod.key_of_guid("") is None


# ---------- fnusers：只读 SQLite，绝不碰真实 NAS 路径 ----------

def _make_dbs(tmp_path):
    act = tmp_path / "trimactivity.db"
    med = tmp_path / "trimmedia.db"
    c = sqlite3.connect(act)
    c.execute("CREATE TABLE user_token(token TEXT, user_guid TEXT, "
              "status INTEGER, update_time INTEGER)")
    c.execute("INSERT INTO user_token VALUES ('tok-alice','u-alice',1,100)")
    c.execute("INSERT INTO user_token VALUES ('tok-admin','u-admin',1,100)")
    c.execute("INSERT INTO user_token VALUES ('tok-dead','u-alice',0,100)")
    c.commit()
    c.close()
    c = sqlite3.connect(med)
    c.execute("CREATE TABLE user(guid TEXT, username TEXT, is_admin INTEGER, "
              "status INTEGER)")
    c.execute("INSERT INTO user VALUES ('u-admin','admin',1,1)")
    c.execute("INSERT INTO user VALUES ('u-alice','alice',0,1)")
    c.execute("INSERT INTO user VALUES ('u-noname','',0,1)")
    c.execute("INSERT INTO user VALUES ('default-user-template','tpl',0,1)")
    c.commit()
    c.close()
    return tmp_path


def _req(headers=None):
    return SimpleNamespace(headers=headers or {})


@pytest.fixture
def _dbs(monkeypatch, tmp_path):
    d = _make_dbs(tmp_path)
    monkeypatch.setattr(fnusersmod, "_DB_CANDIDATES", [str(d)])
    return d


def test_resolve_known_user_from_sqlite(_dbs):
    u = fnusersmod.resolve(_req({"authorization": "tok-alice"}))
    assert u == {"known": True, "guid": "u-alice", "name": "alice",
                 "is_admin": False, "token": "tok-alice"}
    a = fnusersmod.resolve(_req({"authorization": "tok-admin"}))
    assert a["known"] is True and a["is_admin"] is True


def test_resolve_token_sources(_dbs):
    # Trim-MC-token 头与 cookie 都能识别
    u = fnusersmod.resolve(_req({"trim-mc-token": "tok-alice"}))
    assert u["known"] is True and u["guid"] == "u-alice"
    u2 = fnusersmod.resolve(_req({"cookie": "foo=1; trim-mc-token=tok-admin"}))
    assert u2["known"] is True and u2["is_admin"] is True


def test_resolve_unknown_or_disabled_token(_dbs):
    assert fnusersmod.resolve(_req({"authorization": "tok-nope"}))["known"] is False
    # status=0 的 token 不认
    assert fnusersmod.resolve(_req({"authorization": "tok-dead"}))["known"] is False


def test_resolve_no_token_never_touches_db(monkeypatch, tmp_path):
    # 无 token 时直接返回未知，不读库；候选目录指到空目录也不会报错
    monkeypatch.setattr(fnusersmod, "_DB_CANDIDATES", [str(tmp_path)])
    assert fnusersmod.resolve(_req({}))["known"] is False
    # 候选目录为空时有 token 也只返回未知，不抛错
    monkeypatch.setattr(fnusersmod, "_DB_CANDIDATES", [])
    assert fnusersmod.resolve(_req({"authorization": "tok-alice"}))["known"] is False
    assert fnusersmod.list_users() == []


def test_list_users_from_sqlite(_dbs):
    users = fnusersmod.list_users()
    # ORDER BY is_admin DESC, username：空用户名排在 alice 之前
    assert [u["guid"] for u in users] == ["u-admin", "u-noname", "u-alice"]
    assert users[0]["name"] == "admin" and users[0]["is_admin"] is True
    # 空用户名回退到 guid 前 8 位；模板账号被排除
    assert users[1]["name"] == "u-noname"[:8]
    assert all(u["guid"] != "default-user-template" for u in users)


# ---------- app 接线 ----------

def _req_for_viewer(viewer):
    return SimpleNamespace(headers={})


def test_viewer_can_open_lib(monkeypatch):
    with patch.object(appmod.fnusersmod, "resolve", return_value=ADMIN):
        assert appmod._viewer_can_open_lib(_req_for_viewer(ADMIN),
                                           appmod.CHART_LIB_GUIDS["movie"]) is True
        vlibsmod.set_hidden(CHART_MOVIE, True)
        assert appmod._viewer_can_open_lib(_req_for_viewer(ADMIN),
                                           appmod.CHART_LIB_GUIDS["movie"]) is False
        # 旧热门 guid 归一化到当前榜单库（tv）：只隐藏 movie 时它仍可见；
        # 隐藏 chart:tv 后它同样受限
        assert appmod._viewer_can_open_lib(_req_for_viewer(ADMIN),
                                           appmod.HOT_LEGACY_GUID) is True
        vlibsmod.set_hidden(CHART_TV, True)
        assert appmod._viewer_can_open_lib(_req_for_viewer(ADMIN),
                                           appmod.HOT_LEGACY_GUID) is False
        vlibsmod.set_hidden(CHART_MOVIE, False)
        vlibsmod.set_hidden(CHART_TV, False)
    with patch.object(appmod.fnusersmod, "resolve",
                      return_value=_user("u1")):
        # 已识别但未配置的账号：fail-open 默认下能看到虚拟库
        assert appmod._viewer_can_open_lib(_req_for_viewer(None),
                                           appmod.CHART_LIB_GUIDS["tv"]) is True
        vlibsmod.set_account_acl("u1", [CHART_MOVIE])
        assert appmod._viewer_can_open_lib(_req_for_viewer(None),
                                           appmod.CHART_LIB_GUIDS["tv"]) is False
        assert appmod._viewer_can_open_lib(_req_for_viewer(None),
                                           appmod.CHART_LIB_GUIDS["movie"]) is True
    # 未知身份：fail-open 默认下虚拟库可打开；非虚拟库恒 True；T4 选片库按 t4 键计
    with patch.object(appmod.fnusersmod, "resolve", return_value=UNKNOWN):
        assert appmod._viewer_can_open_lib(_req_for_viewer(None),
                                           appmod.CHART_LIB_GUIDS["tv"]) is True
        assert appmod._viewer_can_open_lib(_req_for_viewer(None), "local-lib") is True
        vlibsmod.set_hidden("t4", True)
        assert appmod._viewer_can_open_lib(_req_for_viewer(None),
                                           appmod.T4_LIB_GUID) is False


def test_virtual_lib_list_has_keys_hidden_order():
    libs = appmod._virtual_lib_list()
    assert libs, "至少应有热门/海量库"
    assert all("key" in d and "hidden" in d and "order" in d for d in libs)
    assert all("visible_users" not in d for d in libs)
    by_key = {d["key"]: d for d in libs}
    assert by_key["chart:movie"]["guid"] == appmod.CHART_LIB_GUIDS["movie"]
    assert by_key["t4"]["guid"] == appmod.T4_LIB_GUID
    vlibsmod.set_hidden(CHART_MOVIE, True)
    assert [d for d in appmod._virtual_lib_list()
            if d["key"] == CHART_MOVIE][0]["hidden"] is True


def test_app_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ("/v/api/ext/libs", "/v/api/ext/lib/visibility",
              "/v/api/ext/lib/order", "/v/api/ext/accounts",
              "/v/api/ext/account/acl", "/v/api/ext/account/defaults",
              "/v/api/ext/local-libs"):
        assert p in paths
    # 旧“每库用户名”端点已删除
    assert "/v/api/ext/users" not in paths


# ---------- 管理端点（ASGI + 模拟上游） ----------

class _AclEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 配置内存替身（不落盘）
        self._orig_cfg = copy.deepcopy(config_mod.config._cfg)
        config_mod.config._cfg = copy.deepcopy(config_mod.DEFAULTS)

        def _fake_save(patch):
            cfg = config_mod.config.get()
            for k, v in patch.items():
                if k in cfg:
                    cfg[k] = v
            config_mod.config._cfg = cfg

        self._orig_save = config_mod.config.save
        config_mod.config.save = _fake_save
        fnusersmod._cache.clear()

        def official(request):
            return httpx.Response(200, json={"code": 0, "data": []})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(official))
        self.addAsyncCleanup(self.upstream.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="https://media.example.com")
        self.addAsyncCleanup(self.client.aclose)
        self._patches = []
        for name, value in (
            ("up_client", lambda request: self.upstream),
            ("charts_enabled", lambda: True),
            ("catalog_enabled", lambda: False),
            ("standalone_sources_active", lambda: []),
            ("chart_cache", SimpleNamespace(items=lambda cat: [])),
            ("guard_native_enabled", lambda: False),
        ):
            item = patch.object(appmod, name, value)
            item.start()
            self._patches.append(item)
        # fail-open 下未识别会话默认可见；本类测的是管理端点与
        # 显隐过滤本身，以管理员身份请求。
        item = patch.object(appmod.fnusersmod, "resolve", return_value=ADMIN)
        item.start()
        self._patches.append(item)

    async def asyncTearDown(self):
        for item in self._patches:
            item.stop()
        config_mod.config._cfg = self._orig_cfg
        config_mod.config.save = self._orig_save
        fnusersmod._cache.clear()

    async def test_libs_endpoint_returns_keys_and_placement(self):
        r = await self.client.get("/v/api/ext/libs")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["placement"] == "front" and d["anchor"] == ""
        keys = {x["key"] for x in d["libs"]}
        assert {"chart:movie", "chart:tv", "t4"} <= keys
        assert all("hidden" in x and "order" in x for x in d["libs"])

    async def test_lib_visibility_endpoint(self):
        r = await self.client.post("/v/api/ext/lib/visibility",
                                   json={"key": "chart:movie", "hidden": True})
        assert r.json()["code"] == 0
        libs = (await self.client.get("/v/api/ext/libs")).json()["data"]["libs"]
        assert [x for x in libs if x["key"] == "chart:movie"][0]["hidden"] is True
        bad = await self.client.post("/v/api/ext/lib/visibility",
                                     json={"key": "bogus:x", "hidden": True})
        assert bad.json()["code"] == 1

    async def test_lib_order_endpoint_and_mediadb_list_applies_it(self):
        r = await self.client.post("/v/api/ext/lib/order", json={
            "order": ["chart:tv", "chart:movie", "chart:anime", "chart:variety"],
            "placement": "front"})
        assert r.json()["code"] == 0
        assert vlibsmod.lib_order()[0] == "chart:tv"
        bad = await self.client.post("/v/api/ext/lib/order", json={
            "order": [], "placement": "sideways"})
        assert bad.json()["code"] == 1

        lst = (await self.client.get("/v/api/v1/mediadb/list")).json()
        assert lst["code"] == 0
        guids = [row["guid"] for row in lst["data"]]
        assert guids.index(appmod.CHART_LIB_GUIDS["tv"]) < \
            guids.index(appmod.CHART_LIB_GUIDS["movie"])

    async def test_mediadb_list_hides_hidden_lib_and_mdb_returns_minus6(self):
        vlibsmod.set_hidden("chart:movie", True)
        lst = (await self.client.get("/v/api/v1/mediadb/list")).json()
        assert appmod.CHART_LIB_GUIDS["movie"] not in \
            {row["guid"] for row in lst["data"]}
        assert appmod.CHART_LIB_GUIDS["tv"] in \
            {row["guid"] for row in lst["data"]}
        mdb = await self.client.get(
            "/v/api/v1/mdb/" + appmod.CHART_LIB_GUIDS["movie"])
        assert mdb.json()["code"] == -6
        mdb2 = await self.client.get(
            "/v/api/v1/mdb/" + appmod.CHART_LIB_GUIDS["tv"])
        assert mdb2.json()["code"] == 0

    async def test_accounts_and_acl_endpoints(self):
        fake_accounts = [
            {"guid": "u-admin", "name": "admin", "is_admin": True, "status": 1},
            {"guid": "u-kid", "name": "kid", "is_admin": False, "status": 1},
        ]
        with patch.object(appmod.fnusersmod, "list_users",
                          return_value=fake_accounts):
            r = await self.client.get("/v/api/ext/accounts")
        assert r.status_code == 200
        accts = r.json()["data"]["accounts"]
        assert [a["username"] for a in accts] == ["admin", "kid"]
        assert all(a["configured"] is False for a in accts)
        # fail-open 默认：新配置两项均为 True
        assert r.json()["data"]["acl_default_visible"] is True
        assert r.json()["data"]["acl_default_online"] is True

        r = await self.client.post("/v/api/ext/account/acl", json={
            "guid": "u-kid", "allow": ["chart:movie", "__online__"]})
        assert r.json()["code"] == 0
        with patch.object(appmod.fnusersmod, "list_users",
                          return_value=fake_accounts):
            r2 = await self.client.get("/v/api/ext/accounts")
        kid = [a for a in r2.json()["data"]["accounts"]
               if a["guid"] == "u-kid"][0]
        assert kid["configured"] is True
        assert set(kid["allowed"]) == {"chart:movie", "__online__"}
        # 恢复默认
        r3 = await self.client.post("/v/api/ext/account/acl",
                                    json={"guid": "u-kid", "allow": None})
        assert r3.json()["code"] == 0
        assert vlibsmod.account_allowlist("u-kid") is None

    async def test_account_defaults_endpoint(self):
        r = await self.client.post("/v/api/ext/account/defaults", json={
            "acl_default_visible": False, "acl_default_online": False})
        assert r.json()["code"] == 0
        assert vlibsmod.acl_default_visible() is False
        assert vlibsmod.acl_default_online() is False
        # 管理员手动改成 False 后生效（fail-open 只改默认，不锁死开关）
        r = await self.client.post("/v/api/ext/account/defaults", json={
            "acl_default_visible": True, "acl_default_online": True})
        assert r.json()["code"] == 0
        assert vlibsmod.acl_default_visible() is True
        assert vlibsmod.acl_default_online() is True

    async def test_item_list_hidden_lib_forwards_upstream(self):
        vlibsmod.set_hidden("chart:movie", True)
        r = await self.client.post(
            "/v/api/v1/item/list",
            json={"ancestor_guid": appmod.CHART_LIB_GUIDS["movie"]})
        # 透传官方：模拟上游返回空 data（本测试的 MockTransport）
        assert r.status_code == 200
        assert r.json()["data"] == []
