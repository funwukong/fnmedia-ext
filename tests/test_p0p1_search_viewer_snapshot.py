"""P0 第 2 项：搜索过滤链每请求重复深拷贝 → 每请求快照缓存。

覆盖：
- vlibs.ViewerContext 快照：一次 config.get()，多次 can_see 不再读配置；
  account_can_see / account_online_allowed 单次调用也只有一次深拷贝；
- vlibs.sort_keys：顺序表只读一次（此前每次比较调两次 order_index，
  每次都深拷贝全配置）；
- app._ViewerCtx：同一请求内身份只解析一次、源反查表只建一次、
  同一库键的判定只算一次；过滤结果与旧链一致。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import libfilter as libfiltermod
import vlibs as vlibsmod

ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
         "is_admin": True, "token": "tok-admin"}
UNKNOWN = {"known": False, "guid": "", "name": "", "is_admin": False,
           "token": "tok-x"}


def _user(guid="u1"):
    return {"known": True, "guid": guid, "name": guid,
            "is_admin": False, "token": "tok-" + guid}


def _can_see(allow_map, hidden=()):
    hidden = set(hidden)

    def fake(user, key):
        if not key:
            return True
        if key in hidden:
            return False
        if not user or not user.get("known"):
            return False
        if user.get("is_admin"):
            return True
        allow = allow_map.get(user.get("guid", ""))
        if allow is None:
            return False
        return key in allow

    return fake


class _Counting:
    """包一个函数记调用次数（用完后调用 restore 还原）。"""

    def __init__(self, obj, attr):
        self.obj = obj
        self.attr = attr
        self.orig = getattr(obj, attr)
        self.calls = 0

    def __enter__(self):
        def counting(*a, **kw):
            self.calls += 1
            return self.orig(*a, **kw)

        setattr(self.obj, self.attr, counting)
        return self

    def __exit__(self, *exc):
        setattr(self.obj, self.attr, self.orig)
        return False


# ---------- vlibs.ViewerContext 快照 ----------

class ViewerSnapshotTests(unittest.TestCase):
    def test_snapshot_reads_config_once_for_many_checks(self):
        canned = {"lib_hidden": ["catalog:tv"], "lib_acl": {"u1": ["catalog:movie"]},
                  "acl_default_visible": False, "acl_default_online": False}
        orig_get = vlibsmod.config.get
        calls = {"n": 0}

        def counting_get():
            calls["n"] += 1
            return dict(canned)

        vlibsmod.config.get = counting_get
        try:
            snap = vlibsmod.ViewerContext.for_user(_user("u1"))
            keys = ["catalog:movie", "catalog:tv", "standalone:baofeng",
                    "catalog:movie", "catalog:tv", "t4"]
            seen = [snap.can_see(k) for k in keys]
        finally:
            vlibsmod.config.get = orig_get
        self.assertEqual(calls["n"], 1)
        self.assertEqual(seen, [True, False, False, True, False, False])

    def test_snapshot_semantics_match_spec(self):
        # 直接注入快照数据（不碰配置文件）：逐条核 fail-closed 语义。
        snap = vlibsmod.ViewerContext(
            _user("u1"), hidden={"catalog:tv"},
            acl={"u1": ["catalog:movie", "standalone:ffzy"]},
            default_visible=False, default_online=False)
        self.assertIs(snap.can_see(""), True)          # 空键放行（旧语义）
        self.assertIs(snap.can_see("catalog:movie"), True)
        self.assertIs(snap.can_see("catalog:tv"), False)     # 全局隐藏优先
        self.assertIs(snap.can_see("standalone:ffzy"), True)
        self.assertIs(snap.can_see("catalog:anime"), False)  # 白名单外
        self.assertIs(snap.can_see_any(["catalog:tv", "catalog:movie"]), True)
        self.assertIs(snap.can_see_any(["catalog:tv"]), False)
        self.assertIs(snap.can_see_any([]), False)
        self.assertIs(snap.online_allowed(), False)     # 未含 __online__
        admin = vlibsmod.ViewerContext(
            ADMIN, hidden={"catalog:movie"}, acl={}, default_visible=False)
        self.assertIs(admin.can_see("catalog:tv"), True)     # 管理员全看
        self.assertIs(admin.can_see("catalog:movie"), False)  # 但隐藏库除外
        self.assertIs(admin.online_allowed(), True)
        unknown = vlibsmod.ViewerContext(UNKNOWN, hidden=set(), acl={},
                                         default_visible=True)
        self.assertIs(unknown.can_see("catalog:movie"), False)  # 默认开也不放行
        self.assertIs(unknown.online_allowed(), False)
        unconfigured = vlibsmod.ViewerContext(
            _user("u9"), hidden=set(), acl={}, default_visible=True)
        self.assertIs(unconfigured.can_see("catalog:movie"), True)
        self.assertIs(unconfigured.online_allowed(), False)

    def test_account_can_see_single_config_get_per_call(self):
        with _Counting(vlibsmod.config, "get") as c:
            vlibsmod.account_can_see(_user("u1"), "catalog:movie")
        self.assertEqual(c.calls, 1)
        with _Counting(vlibsmod.config, "get") as c:
            vlibsmod.account_online_allowed(_user("u1"))
        self.assertEqual(c.calls, 1)

    def test_sort_keys_reads_order_once(self):
        with _Counting(vlibsmod.config, "get") as c:
            got = vlibsmod.sort_keys(
                ["catalog:tv", "catalog:movie", "standalone:ffzy",
                 "catalog:anime", "t4", "standalone:baofeng"])
        self.assertEqual(c.calls, 1)
        # 未自定义顺序时保持原相对顺序（稳定追加）。
        self.assertEqual(got, ["catalog:tv", "catalog:movie", "standalone:ffzy",
                               "catalog:anime", "t4", "standalone:baofeng"])

    def test_sort_keys_respects_custom_order(self):
        orig = vlibsmod.config.get
        canned = dict(orig())
        canned["lib_order"] = ["standalone:ffzy", "catalog:movie"]

        def canned_get():
            return dict(canned)

        vlibsmod.config.get = canned_get
        try:
            got = vlibsmod.sort_keys(
                ["catalog:tv", "catalog:movie", "standalone:ffzy",
                 "catalog:anime"])
        finally:
            vlibsmod.config.get = orig
        self.assertEqual(got, ["standalone:ffzy", "catalog:movie",
                               "catalog:tv", "catalog:anime"])


# ---------- app._ViewerCtx 每请求缓存 ----------

class AppViewerCtxTests(unittest.TestCase):
    def _req(self):
        # 带 state 的请求对象：ctx 可挂在上面复用（Starlette Request 同理）。
        return SimpleNamespace(headers={}, state=SimpleNamespace())

    def test_ctx_reused_across_calls_on_same_request(self):
        req = self._req()
        self.assertIs(appmod._viewer_ctx(req), appmod._viewer_ctx(req))

    def test_ctx_resolves_identity_once_and_memoizes_key_checks(self):
        req = self._req()
        resolve_calls = {"n": 0}
        orig_resolve = appmod.fnusersmod.resolve
        orig_can_see = vlibsmod.account_can_see
        can_see_calls = {"n": 0}
        fake = _can_see({"u1": {"catalog:movie"}})

        def counting_resolve(r):
            resolve_calls["n"] += 1
            return _user("u1")

        def counting_can_see(user, key):
            can_see_calls["n"] += 1
            return fake(user, key)

        appmod.fnusersmod.resolve = counting_resolve
        vlibsmod.account_can_see = counting_can_see
        try:
            for _ in range(5):
                self.assertIs(appmod._viewer_can_open_key(req, "catalog:movie"), True)
            for _ in range(3):
                self.assertIs(appmod._viewer_can_open_key(req, "catalog:tv"), False)
            self.assertIs(appmod._viewer_can_open_key(req, "standalone:baofeng"), False)
        finally:
            appmod.fnusersmod.resolve = orig_resolve
            vlibsmod.account_can_see = orig_can_see
        # 身份只解析一次（原先每键一次）；每 distinct key 只判定一次。
        self.assertEqual(resolve_calls["n"], 1)
        self.assertEqual(can_see_calls["n"], 3)

    def test_ctx_builds_source_maps_once(self):
        req = self._req()
        orig_resolve = appmod.fnusersmod.resolve
        orig_can_see = vlibsmod.account_can_see
        appmod.fnusersmod.resolve = lambda r: ADMIN
        vlibsmod.account_can_see = _can_see({})
        try:
            with _Counting(appmod.config, "all_sources") as c:
                ctx = appmod._viewer_ctx(req)
                for _ in range(5):
                    ctx.gkey_of_source("baofeng")
                for _ in range(5):
                    ctx.vkey_for_guid(appmod.standalone_lib_guid("baofeng"))
                ctx.gkey_of_source("ffzy")
        finally:
            appmod.fnusersmod.resolve = orig_resolve
            vlibsmod.account_can_see = orig_can_see
        # gkey 表、standalone 表各建一次（原先每键一次全源扫描）。
        self.assertEqual(c.calls, 2)

    def test_ctx_filter_results_match_direct_chain(self):
        # 同一组 brief：经 ctx 过滤的可见集 == 逐键走旧链的可见集。
        briefs = [
            {"guid": "g1", "online_id": "baofeng:v1", "title": "动作大片",
             "type_name": "动作片", "lines": {"baofeng::线路1": []}, "locator": {}},
            {"guid": "g2", "online_id": "ffzy:v2", "title": "国产剧集",
             "type_name": "国产剧", "lines": {"ffzy::线路1": []}, "locator": {}},
            {"guid": "g3", "online_id": "t4src:v3", "title": "T4剧集",
             "type_name": "连续剧", "lines": {"T4线路": []},
             "locator": {"kind": "t4", "source_id": "t4src"}},
        ]
        req = self._req()
        orig_resolve = appmod.fnusersmod.resolve
        orig_can_see = vlibsmod.account_can_see
        appmod.fnusersmod.resolve = lambda r: _user("u1")
        vlibsmod.account_can_see = _can_see({"u1": {"catalog:movie"}})
        try:
            ctx = appmod._viewer_ctx(req)
            kept_ctx = libfiltermod.filter_briefs(
                briefs, ctx.can_open_key, gkey_of=ctx.gkey_of_source)
            kept_direct = libfiltermod.filter_briefs(
                briefs, lambda k: appmod._viewer_can_open_key(req, k),
                gkey_of=appmod._gkey_of_source)
        finally:
            appmod.fnusersmod.resolve = orig_resolve
            vlibsmod.account_can_see = orig_can_see
        self.assertEqual([b["guid"] for b in kept_ctx], ["g1"])
        self.assertEqual([b["guid"] for b in kept_direct], ["g1"])

    def test_ctx_none_request_is_fail_closed(self):
        self.assertIs(appmod._viewer_ctx(None).can_open_key("catalog:movie"), False)
        self.assertIs(appmod._viewer_can_open_key(None, "catalog:movie"), False)
        self.assertIs(appmod._brief_viewable(None, {"guid": "g"}), False)


if __name__ == "__main__":
    unittest.main()
