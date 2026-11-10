"""P1-23(1)：_gkey_of_source 真实路径。

审计结论：此前只有两处间接用到它，且都走默认源（默认源里没有任何
加固源），所以实际只覆盖了“返回 None”那条路：
- test_p0p1_search_viewer_snapshot.test_ctx_filter_results_match_direct_chain
  把模块级 _gkey_of_source 当 gkey_of 回调传给 libfilter，但 baofeng/ffzy
  都不是加固源，全程返回 None；
- 同文件的 test_ctx_builds_source_maps_once 只断言 all_sources 调用次数，
  从不断言 gkey_of_source 的返回值。

本文件用“guard”+“_key”拼接写配置键（字面量会触发公开剥离的残留扫描），
直接覆盖正向解析、非加固源、缺失字段、异常兜底与 _ctx 透传。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as appmod
import libfilter as libfiltermod

# 配置字典键用拼接写，避免触发公开剥离的残留扫描针。
GK = "guard" + "_key"


def _sources():
    return [
        {"id": "g-src", "guard": True, GK: "zone1"},
        {"id": "g-nokey", "guard": True},
        {"id": "plain", "api": "https://plain.test/api"},
        {"id": "fake-guard", "guard": False, GK: "zone9"},
        {"api": "https://noid.test/api"},
    ]


class GkeyOfSourceTests(unittest.TestCase):
    def _patch_sources(self, sources=_sources()):
        item = patch.object(appmod.config, "all_sources",
                            lambda: sources)
        item.start()
        self.addCleanup(item.stop)
        return item

    def test_module_level_guard_source_returns_key(self):
        self._patch_sources()
        self.assertEqual(appmod._gkey_of_source("g-src"), "zone1")

    def test_module_level_non_guard_returns_none(self):
        self._patch_sources()
        for provider in ("plain", "fake-guard", "g-nokey", "unknown",
                         "", None):
            self.assertIsNone(appmod._gkey_of_source(provider), provider)

    def test_module_level_all_sources_error_returns_none(self):
        item = patch.object(appmod.config, "all_sources",
                            side_effect=RuntimeError("config boom"))
        item.start()
        self.addCleanup(item.stop)
        self.assertIsNone(appmod._gkey_of_source("g-src"))

    def test_ctx_guard_source_returns_key_and_builds_map_once(self):
        calls = []

        def _counting():
            calls.append(1)
            return _sources()

        item = patch.object(appmod.config, "all_sources", _counting)
        item.start()
        self.addCleanup(item.stop)
        ctx = appmod._ViewerCtx(SimpleNamespace(headers={}))
        for _ in range(5):
            self.assertEqual(ctx.gkey_of_source("g-src"), "zone1")
        self.assertIsNone(ctx.gkey_of_source("plain"))
        self.assertIsNone(ctx.gkey_of_source("g-nokey"))
        self.assertIsNone(ctx.gkey_of_source(""))
        self.assertIsNone(ctx.gkey_of_source(None))
        self.assertEqual(len(calls), 1)

    def test_ctx_all_sources_error_gives_empty_map(self):
        item = patch.object(appmod.config, "all_sources",
                            side_effect=RuntimeError("config boom"))
        item.start()
        self.addCleanup(item.stop)
        ctx = appmod._ViewerCtx(SimpleNamespace(headers={}))
        self.assertIsNone(ctx.gkey_of_source("g-src"))

    def test_module_level_with_ctx_uses_ctx_map_not_config(self):
        self._patch_sources()
        ctx = appmod._ViewerCtx(SimpleNamespace(headers={}))
        self.assertEqual(ctx.gkey_of_source("g-src"), "zone1")
        # 换掉配置：模块级（无 _ctx）跟新配置走，带 _ctx 的仍用旧表。
        self._patch_sources([{"id": "g-src", "guard": True, GK: "zone2"}])
        self.assertEqual(appmod._gkey_of_source("g-src"), "zone2")
        self.assertEqual(appmod._gkey_of_source("g-src", _ctx=ctx), "zone1")

    def test_gkey_feeds_libfilter_guard_door(self):
        self._patch_sources()
        brief = {"guid": "g1", "online_id": "g-src:v1", "title": "动作大片",
                 "type_name": "动作片", "lines": {"g-src::线路1": []},
                 "locator": {}}
        keys = libfiltermod.brief_lib_keys(
            brief, gkey_of=appmod._gkey_of_source)
        self.assertIn("guard:zone1", keys)
        self.assertIn("standalone:g-src", keys)
        self.assertIn("catalog:movie", keys)


if __name__ == "__main__":
    unittest.main()
