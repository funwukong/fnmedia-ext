"""P1-23(7)：ViewerContext 统一化后的 _ctx 透传。

审计结论（其余抽查点均已覆盖，不再重复写）：
- online_search 回调：test_p0_early_return_visible 已从行为上覆盖
  （受限/管理员/缓存隔离 7 个测试）；身份只解析一次则由
  test_p0p1_search_viewer_snapshot.test_ctx_resolves_identity_once
  覆盖，本文件另补一次穿过 online_search 整条路的计数断言
  （见 test_p0_early_return_visible.py）。
- marks 分区 + libs 兜底：test_p0_playchain_favorites 已覆盖
  （分区隔离、libs 在 brief 被淘汰时兜底）。
- libfilter catalog_route：test_p0p1_search_catalog_route 14 个测试已覆盖。
- pan 清理开关/TTL：test_p1_pan_cleanup 9 个测试已覆盖。
- XML 解析分支：test_p1_xml_cms 10 个测试已覆盖。

本文件只补播放链守门 helper 的 _ctx 透传：调用方传了 _ctx 时判定
必须跟 _ctx 的身份走，而不是按 request 重新解析身份；若实现退化为
忽略 _ctx（每次按 request 重解析），下面用例会立刻变红。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as appmod
import vlibs as vlibsmod

ADMIN = {"known": True, "guid": "admin-guid", "name": "admin",
         "is_admin": True, "token": "tok-admin"}


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


# 与 test_p0p1_search_viewer_snapshot.test_ctx_filter_results_match_direct_chain
# 同款 brief：电影走 catalog:movie，电视剧走 catalog:tv。
BRIEF_MOVIE = {"guid": "g1", "online_id": "baofeng:v1", "title": "动作大片",
               "type_name": "动作片", "lines": {"baofeng::线路1": []},
               "locator": {}}
BRIEF_TV = {"guid": "g2", "online_id": "ffzy:v2", "title": "国产剧集",
            "type_name": "国产剧", "lines": {"ffzy::线路1": []},
            "locator": {}}


class CtxPassthroughTests(unittest.TestCase):
    def setUp(self):
        # resolve 按请求对象分发身份：admin 请求与 u1 请求身份不同，
        # 这样“跟 _ctx 走”与“跟 request 走”会得出相反结果。
        self.req_admin = SimpleNamespace(headers={})
        self.req_u1 = SimpleNamespace(headers={})

        def _resolve(request):
            if request is self.req_admin:
                return ADMIN
            return _user("u1")

        p1 = patch.object(appmod.fnusersmod, "resolve",
                          side_effect=_resolve)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = patch.object(appmod.vlibsmod, "account_can_see",
                          _can_see({"u1": {"catalog:movie"}}))
        p2.start()
        self.addCleanup(p2.stop)
        self.ctx_u1 = appmod._ViewerCtx(self.req_u1)
        self.ctx_admin = appmod._ViewerCtx(self.req_admin)

    def test_brief_viewable_uses_explicit_ctx_not_request(self):
        # request 是管理员，但显式 _ctx 是受限账号 u1（打不开 catalog:tv）：
        # 判定必须拒绝；不传 _ctx 时按 request 走，管理员放行。
        self.assertIs(
            appmod._brief_viewable(self.req_admin, BRIEF_TV,
                                   _ctx=self.ctx_u1),
            False)
        self.assertIs(
            appmod._brief_viewable(self.req_admin, BRIEF_TV), True)

    def test_brief_viewable_explicit_admin_ctx_allows_restricted_request(self):
        # 反方向：request 是受限账号，但显式 _ctx 是管理员：判定必须放行；
        # 不传 _ctx 时按 request 走，受限账号被拒。
        self.assertIs(
            appmod._brief_viewable(self.req_u1, BRIEF_TV,
                                   _ctx=self.ctx_admin),
            True)
        self.assertIs(
            appmod._brief_viewable(self.req_u1, BRIEF_TV), False)

    def test_online_target_viewable_uses_explicit_ctx(self):
        # 播放链守门 _online_target_viewable 同样把 _ctx 透传给 _brief_viewable：
        # 管理员请求 + 受限 _ctx → 拒绝；不传 _ctx → 放行。
        target = {"online_guid": "g2"}
        item = patch.object(appmod.store, "get_title_by_guid",
                            lambda guid: BRIEF_TV if guid == "g2" else None)
        item.start()
        self.addCleanup(item.stop)
        self.assertIs(
            appmod._online_target_viewable(self.req_admin, target,
                                           _ctx=self.ctx_u1),
            False)
        self.assertIs(
            appmod._online_target_viewable(self.req_admin, target), True)


if __name__ == "__main__":
    unittest.main()
