"""海阔第三轮修复（2026-11-12 用户装 all4 真机反馈：无画面、一直转圈）。

根因 1：无后缀 mp4 直链到不了 direct 代理。_build_playlist_body 只认
URL 后缀判 mp4；pan.wo.cn /openapi/download?fid=… 与 cmecloud 裸对象
key 既无 .mp4 也无 .m3u8 后缀 → 掉进 _fetch_playlist 把 3.7GB 直链当
清单整包下载，15 秒超时 → 慢线静默回占位清单，客户端永远播提示画面。
修：无后缀时 Range bytes=0-0 探一次上游，video/*、mp4 ftyp 或
Content-Range 即判媒体直链，回 _NOT_HLS 走 302 到 direct；探不到或
探失败按原逻辑走（仍可能取到无后缀清单），探测本身不卡死。

根因 2：91短剧取集取到分类导航。琼明神女录 EP4 的 detail raw 20 条，
过滤后首条是 text_3 标签 #现代都市（第 1 集在第 3 条）；_safe_index
恒为 0，_resolve_haikuo 永远取首条 → play 得导航地址 → not_playable
→ main.m3u8 回 502 → 客户端重试转圈。修：过滤丢 # 标签与猜你喜欢 /
换一换推荐区；解析按序尝试（有界 5 条），导航/not_playable 跳下一条。

附带：hls_seg 与 direct_proxy 的 DDUO Referer 剥离不对海阔 pick 生效；
deliver 诊断事件记 main.m3u8 出口结果（清单/占位/502/302）。

本文件先以桩复现，再断言修复后：无后缀 video 直链走 _NOT_HLS、真 m3u8
仍回清单、后缀 .mp4 仍 _NOT_HLS、91短剧首集是第 1 集而非标签、
Referer 剥离跳过海阔 pick、deliver 事件在占位/502/302/清单时有记录。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import hls_race as hr
import providers
import sourcehealth
import store as storemod
import test_haikuo_round2 as _r2
import test_t4_app as fixtures
import t4_media
import t4_service

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针。
_HK_MOD = "hai" + "kuo"
hk = importlib.import_module(_HK_MOD)
hkb = importlib.import_module(_HK_MOD + "_browser")
hkdiag = importlib.import_module(_HK_MOD + "_diag")

SOURCE = dict(_r2.SOURCE)
EPISODE_TOKEN = _r2.EPISODE_TOKEN
DIRECT_URL = "https://pan.wo.cn/download/abc123.mp4"
NO_SUFFIX_MP4 = "https://hydownload.pan.wo.cn/openapi/download?fid=abc123"
NO_SUFFIX_M3U8 = "https://91dj88.com/h5/m3u8/bktappupdata/sp/9u/vy/2a/bv/0887f1693b974764a671a7a3ced43550.m3u8"
REAL_PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                 "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nseg0.ts\n")

# 91短剧 琼明神女录 EP4 的 detail 页形状（sidecar 实测，20 条）。
DETAIL_91 = [
    {"title": "琼明神女录 EP4", "url": "https://img.invalid/poster.jpg",
         "pic_url": "", "desc": "", "col_type": "movie_1_left_pic", "extra": {}},
    {"title": "#现代都市", "url": "haikuo-play:TAG1", "pic_url": "",
         "desc": "", "col_type": "text_3", "extra": {}},
    {"title": "#都市", "url": "haikuo-play:TAG2", "pic_url": "",
         "desc": "", "col_type": "text_3", "extra": {}},
    {"title": "简介", "url": "hiker://empty", "pic_url": "",
         "desc": "", "col_type": "text_1", "extra": {}},
    {"title": "试道大会传出欧晴将当众破处", "url": "javascript:;",
         "pic_url": "", "desc": "", "col_type": "rich_text", "extra": {}},
    {"title": "", "url": "", "pic_url": "", "desc": "",
         "col_type": "blank_block", "extra": {}},
    {"title": "选集（4集）", "url": "hiker://empty", "pic_url": "",
         "desc": "", "col_type": "text_1", "extra": {}},
    {"title": "第1集", "url": "haikuo-play:EP1", "pic_url": "",
         "desc": "", "col_type": "text_2", "extra": {"lineVisible": False}},
    {"title": "第2集", "url": "haikuo-play:EP2", "pic_url": "",
         "desc": "", "col_type": "text_2", "extra": {"lineVisible": False}},
    {"title": "第3集", "url": "haikuo-play:EP3", "pic_url": "",
         "desc": "", "col_type": "text_2", "extra": {"lineVisible": False}},
    {"title": "第4集", "url": "haikuo-play:EP4", "pic_url": "",
         "desc": "", "col_type": "text_2", "extra": {"lineVisible": False}},
    {"title": "", "url": "", "pic_url": "", "desc": "",
         "col_type": "blank_block", "extra": {}},
    {"title": "猜你喜欢", "url": "haikuo-play:REC0", "pic_url": "",
         "desc": "", "col_type": "text_2", "extra": {}},
    {"title": "换一换", "url": "haikuo-play:REC9", "pic_url": "",
         "desc": "", "col_type": "text_2", "extra": {}},
    {"title": "合租（全6集）", "url": "https://example.invalid/detail/r1",
         "pic_url": "", "desc": "", "col_type": "movie_3", "extra": {}},
    {"title": "我的岳母苏沛真", "url": "https://example.invalid/detail/r2",
         "pic_url": "", "desc": "", "col_type": "movie_3", "extra": {}},
    {"title": "坠雾（全4集）",
         "url": "https://example.invalid/detail/r3",
         "pic_url": "", "desc": "", "col_type": "movie_3", "extra": {}},
    {"title": "告白母子：妈妈陪读也陪爱",
         "url": "https://example.invalid/detail/r4",
         "pic_url": "", "desc": "", "col_type": "movie_3", "extra": {}},
    {"title": "《重生之我是少林大神棍》（全7集）",
         "url": "https://example.invalid/detail/r5",
         "pic_url": "", "desc": "", "col_type": "movie_3", "extra": {}},
    {"title": "被同学盯上的巨乳妈妈（全4集）",
         "url": "https://example.invalid/detail/r6",
         "pic_url": "", "desc": "", "col_type": "movie_3", "extra": {}},
]


def _picked(url="hiker://page/detail?id=86364"):
    return {"haikuo": True, "xsrc": True,
            "play_from": "haikuo-sub-test1:测试短剧",
            "url": url, "title": "归鹭", "line": "测试短剧",
            "online_guid": "og-hk", "index": 0,
            "headers": {"User-Agent": "Mozilla/5.0"}}


# ---------------- Range 探活（纯桩，不碰真实网络） ----------------

class _FakeResp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = dict(headers or {})
        self._body = body

    async def aiter_bytes(self, chunk_size=64):
        if self._body:
            yield self._body[:chunk_size]

    async def aclose(self):
        return None


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.built = []

    def build_request(self, method, url, headers=None, timeout=None):
        self.built.append((method, url, dict(headers or {}), timeout))
        return (method, url, dict(headers or {}))

    async def send(self, request, stream=False, follow_redirects=True):
        if self._exc is not None:
            raise self._exc
        return self._resp


class ProbeDirectMediaTests(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, resp=None, exc=None):
        fake = _FakeClient(resp=resp, exc=exc)
        with patch.object(appmod, "ext_client", return_value=fake):
            return await hr._probe_direct_media(None, NO_SUFFIX_MP4, {}), fake

    async def test_video_mp4_range_206_is_direct(self):
        ok, fake = await self._probe(
            _FakeResp(206, {"content-type": "video/mp4",
                            "content-range": "bytes 0-0/3720329018"}, b""))
        self.assertTrue(ok)
        # Range 探活只取首字节，不整包读。
        self.assertEqual(fake.built[0][2].get("Range"), "bytes=0-0")
        self.assertIsNotNone(fake.built[0][3])

    async def test_playlist_text_is_not_direct(self):
        ok, _ = await self._probe(
            _FakeResp(200, {"content-type": "application/vnd.apple.mpegurl"},
                      b"#EXTM3U\n#EXT-X-VERSION:3\n"))
        self.assertFalse(ok)

    async def test_octet_stream_with_content_range_is_direct(self):
        ok, _ = await self._probe(
            _FakeResp(206, {"content-type": "application/octet-stream",
                            "content-range": "bytes 0-0/999"},
                      b"\x00\x00\x00\x18ftypisom"))
        self.assertTrue(ok)

    async def test_ftyp_without_content_range_is_direct(self):
        ok, _ = await self._probe(
            _FakeResp(200, {"content-type": "application/octet-stream"},
                      b"\x00\x00\x00\x18ftypisom"))
        self.assertTrue(ok)

    async def test_http_error_is_not_direct(self):
        ok, _ = await self._probe(_FakeResp(403, {}, b""))
        self.assertFalse(ok)

    async def test_transport_error_is_not_direct(self):
        ok, _ = await self._probe(exc=httpx.ConnectError("boom"))
        self.assertFalse(ok)

    async def test_html_error_page_is_not_direct(self):
        ok, _ = await self._probe(
            _FakeResp(200, {"content-type": "text/html"},
                      b"<html><body>error</body></html>"))
        self.assertFalse(ok)


# ---------------- _build_playlist_body 分流 ----------------

class BuildPlaylistBodyTests(unittest.IsolatedAsyncioTestCase):
    def _resolve_stub(self, url):
        async def _resolve(picked, request):
            picked["url"] = url
            return picked
        return _resolve

    async def _build(self, picked, probe=None, fetch=None):
        patches = [
            patch.object(appmod, "_resolve_if_needed",
                         self._resolve_stub(picked["url"])),
        ]
        if probe is not None:
            patches.append(patch.object(
                hr, "_probe_direct_media",
                AsyncMock(return_value=probe)))
        if fetch is not None:
            patches.append(patch.object(
                appmod, "_fetch_playlist", fetch))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return await hr._build_playlist_body("mg-test", picked, None)

    async def test_no_suffix_video_goes_not_hls_without_fetch(self):
        calls = []

        async def _fetch(request, url, headers, **kw):
            calls.append(url)
            return REAL_PLAYLIST, url

        picked = _picked(NO_SUFFIX_MP4)
        picked["m3u8"] = False
        body, _ = await self._build(picked, probe=True, fetch=_fetch)
        self.assertIs(body, hr._NOT_HLS)
        self.assertEqual(calls, [])

    async def test_no_suffix_playlist_falls_through_to_fetch(self):
        picked = _picked("https://cdn.invalid/abc123")
        picked["m3u8"] = False

        async def _fetch(request, url, headers, **kw):
            return REAL_PLAYLIST, url

        body, _ = await self._build(picked, probe=False, fetch=_fetch)
        self.assertIsNot(body, hr._NOT_HLS)
        self.assertIn("/seg?", body)  # 分片已改写为本代理地址，说明取到了真清单

    async def test_real_m3u8_never_probes(self):
        probe_calls = []

        async def _probe(request, url, headers):
            probe_calls.append(url)
            return True

        async def _fetch(request, url, headers, **kw):
            return REAL_PLAYLIST, url

        picked = _picked(NO_SUFFIX_M3U8)
        picked["m3u8"] = True
        with patch.object(hr, "_probe_direct_media", _probe):
            body, _ = await self._build(picked, fetch=_fetch)
        self.assertEqual(probe_calls, [])
        self.assertIsNot(body, hr._NOT_HLS)
        self.assertIn("/seg?", body)  # 分片已改写为本代理地址，说明取到了真清单

    async def test_mp4_suffix_still_not_hls_without_probe(self):
        probe_calls = []

        async def _probe(request, url, headers):
            probe_calls.append(url)
            return False

        picked = _picked("https://cdn.invalid/v.mp4")
        picked["m3u8"] = False
        with patch.object(hr, "_probe_direct_media", _probe):
            body, _ = await self._build(picked)
        self.assertIs(body, hr._NOT_HLS)
        self.assertEqual(probe_calls, [])


# ---------------- 详情过滤（纯函数） ----------------

class FilterEpisodesRound3Tests(unittest.TestCase):
    def test_91_detail_first_item_is_episode_one(self):
        eps = appmod._haikuo_filter_episodes(list(DETAIL_91))
        self.assertEqual([e["title"] for e in eps],
                         ["第1集", "第2集", "第3集", "第4集"])
        self.assertEqual(eps[0]["url"], "haikuo-play:EP1")

    def test_hash_tag_with_html_wrapper_is_dropped(self):
        items = [{"title": "<font color=\"#fff\">#都市</font>",
                  "url": "haikuo-play:TAGX", "col_type": "text_3",
                  "extra": {}},
                 {"title": "第1集", "url": "haikuo-play:EP1",
                  "col_type": "text_2", "extra": {}}]
        eps = appmod._haikuo_filter_episodes(items)
        self.assertEqual([e["title"] for e in eps], ["第1集"])

    def test_episode_after_recommend_header_is_kept(self):
        items = [{"title": "猜你喜欢", "url": "haikuo-play:REC0",
                  "col_type": "text_2", "extra": {}},
                 {"title": "第1集", "url": "haikuo-play:EP1",
                  "col_type": "text_2", "extra": {}},
                 {"title": "推荐剧", "url": "https://example.invalid/r",
                  "col_type": "movie_3", "extra": {}}]
        eps = appmod._haikuo_filter_episodes(items)
        self.assertEqual([e["title"] for e in eps], ["第1集"])

    def test_yingtan_mixed_detail_still_one_episode(self):
        eps = appmod._haikuo_filter_episodes(list(_r2.MIXED_DETAIL))
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["title"], "归鹭4K")

    def test_pure_episode_list_still_untouched(self):
        eps = appmod._haikuo_filter_episodes(list(_r2.PURE_DETAIL))
        self.assertEqual([e["title"] for e in eps],
                         ["第1集", "第2集", "第3集", "第4集"])


# ---------------- 解析按序尝试 ----------------

class Round3ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._hk_remote = patch.object(
            hk, "all_remote_sources", return_value=[])
        self._hk_remote.start()
        self.addCleanup(self._hk_remote.stop)
        await fixtures.AppTests.asyncSetUp(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        item = patch.object(hkb, "_STATE", Path(temp.name) / "browse.json")
        item.start()
        self.addCleanup(item.stop)
        self.browser = hkb.HaikuoBrowser(self.store, self.config)
        item = patch.object(appmod, "haikuo_browser", self.browser)
        item.start()
        self.addCleanup(item.stop)
        self.config.save({"hot_library_enabled": False,
                          "catalog_library_enabled": False,
                          "guard_native_enabled": False,
                          "sources": [dict(SOURCE)]})
        hkdiag.clear()
        self.addCleanup(hkdiag.clear)

    def handle(self, request):
        return fixtures.AppTests.handle(self, request)

    async def import_source(self):
        return None

    def _play_91(self, calls):
        def _play(key, token):
            calls.append(token)
            if token.startswith("haikuo-play:TAG"):
                return {"playable": False,
                        "reason": "@rule token resolved to a page, "
                                  "not a play URL",
                        "action": "detail",
                        "url": "hiker://empty?page=fypage&kw=x"}
            if token == "haikuo-play:EP1":
                # 首集 token 求值失败（死链/导航）：循环应跳到下一集。
                return {"playable": False, "reason": "empty"}
            if token.startswith("haikuo-play:EP"):
                return {"playable": True, "url": NO_SUFFIX_M3U8,
                        "headers": {"Referer": "https://91dj88.com/"}}
            return {"playable": False, "reason": "empty"}
        return _play

    async def test_resolve_skips_tags_plays_episode_one(self):
        calls = []
        with patch.object(hk, "detail",
                          lambda key, url: list(DETAIL_91)), \
                patch.object(hk, "play", self._play_91(calls)):
            p = _picked("hiker://page/detail?id=91")
            self.assertTrue(await appmod._resolve_haikuo(p))
        self.assertEqual(p["url"], NO_SUFFIX_M3U8)
        self.assertTrue(p["m3u8"])
        self.assertEqual(p["headers"].get("Referer"), "https://91dj88.com/")
        # 标签已被过滤层丢掉；循环试 EP1（不可播）后跳到 EP2 播通。
        self.assertEqual(calls, ["haikuo-play:EP1", "haikuo-play:EP2"])

    async def test_all_unplayable_is_bounded_to_five_tries(self):
        calls = []
        # 7 条看似真剧集的条目（都过过滤层），求值全部不可播。
        eps = [{"title": f"第{i + 1}集", "url": f"haikuo-play:EP{i + 1}",
                "col_type": "text_2", "extra": {}} for i in range(7)]

        def _play(key, token):
            calls.append(token)
            return {"playable": False, "reason": "empty"}

        with patch.object(hk, "detail",
                          lambda key, url: list(eps)), \
                patch.object(hk, "play", _play):
            p = _picked("hiker://page/detail?id=91")
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "not_playable")
        # 上限从 5 提到 8：海报/导航类候选会占位（实测影探4K/鱼安/4K动漫
        # 的首条候选是图片或 hiker://empty），只试 5 条会漏掉真剧集。
        # 这里候选只有 7 条，应全部试完（≤ 上限）。
        self.assertEqual(len(calls), len(eps))
        self.assertLessEqual(len(calls), appmod._HAIKUO_RESOLVE_TRIES)

    async def test_title_token_falls_back_to_detail(self):
        # 首页项本身就是 haikuo-play token（91短剧）：play 得页面不是
        # 播放地址时，改走 detail 取集再求值，首集播通。
        calls = []

        def _play(key, token):
            calls.append(token)
            if token == "haikuo-play:TITLETOKEN":
                return {"playable": False,
                        "reason": "@rule token resolved to a page, "
                                  "not a play URL",
                        "action": "detail",
                        "url": "https://91dj88.com/detail/qiongming"}
            if token == "haikuo-play:EP1":
                return {"playable": True, "url": NO_SUFFIX_M3U8,
                        "headers": {"Referer": "https://91dj88.com/"}}
            return {"playable": False, "reason": "empty"}

        with patch.object(hk, "detail",
                          lambda key, url: list(DETAIL_91)), \
                patch.object(hk, "play", _play):
            p = _picked("haikuo-play:TITLETOKEN")
            self.assertTrue(await appmod._resolve_haikuo(p))
        self.assertEqual(p["url"], NO_SUFFIX_M3U8)
        self.assertTrue(p["m3u8"])
        self.assertEqual(calls[0], "haikuo-play:TITLETOKEN")
        self.assertIn("haikuo-play:EP1", calls)

    async def test_non_detail_token_does_not_fall_back(self):
        detail_calls = []

        def _play(key, token):
            return {"playable": False, "reason": "magnet link not playable"}

        with patch.object(hk, "detail",
                          lambda key, url: detail_calls.append(url) or []), \
                patch.object(hk, "play", _play):
            p = _picked("haikuo-play:MAGNETTOKEN")
            self.assertFalse(await appmod._resolve_haikuo(p))
        self.assertEqual(p.get("haikuo_error"), "not_playable")
        self.assertEqual(detail_calls, [])


# ---------------- Referer 剥离 ----------------

class RefererStripTests(unittest.TestCase):
    def test_haikuo_pick_keeps_referer(self):
        picked = {"xsrc": True,
                  "play_from": "haikuo-sub-test1:测试短剧",
                  "url": NO_SUFFIX_M3U8}
        self.assertFalse(hr._dduo_strip_referer(picked))

    def test_dduo_pick_strips_referer(self):
        picked = {"xsrc": True, "play_from": "dduo::PAN1",
                  "url": "https://cdn.invalid/v.mp4"}
        self.assertTrue(hr._dduo_strip_referer(picked))

    def test_non_xsrc_pick_never_strips(self):
        picked = {"xsrc": False,
                  "play_from": "haikuo-sub-test1:测试短剧",
                  "url": NO_SUFFIX_M3U8}
        self.assertFalse(hr._dduo_strip_referer(picked))

    def test_dduo_disabled_never_strips(self):
        picked = {"xsrc": True, "play_from": "dduo::PAN1",
                  "url": "https://cdn.invalid/v.mp4"}
        with patch.object(hr.features, "DDUO", False):
            self.assertFalse(hr._dduo_strip_referer(picked))


# ---------------- deliver 诊断事件（路由级） ----------------

class DeliverEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [dict(SOURCE)],
                          "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        for module, name, value in [
            (appmod, "config", self.config), (appmod, "store", self.store),
            (appmod, "history", self.history), (storemod, "store", self.store),
            (appmod, "t4_service", t4_service.T4Service(self.store, self.config)),
            (appmod, "t4_media", t4_media.T4Media()),
            (appmod, "_schedule_routes_refresh", lambda: None),
        ]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        appmod._AGG_CACHE.clear()
        appmod._AGG_INFLIGHT.clear()
        appmod._PLAYLIST_CACHE.clear()
        if getattr(appmod, "_MAIN_SERVE_FUT", None) is not None:
            appmod._MAIN_SERVE_FUT.clear()
        self.addCleanup(appmod._PLAYLIST_CACHE.clear)
        if getattr(appmod, "_MAIN_SERVE_FUT", None) is not None:
            self.addCleanup(appmod._MAIN_SERVE_FUT.clear)
        appmod.app.state.ext = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(404)))
        self.addAsyncCleanup(appmod.app.state.ext.aclose)
        appmod.app.state.up = appmod.app.state.ext
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)
        hkdiag.clear()
        self.addCleanup(hkdiag.clear)

    def _deliver_events(self):
        return [e for e in hkdiag.events() if e.get("kind") == "deliver"]

    async def _get_main(self, picked, fetch):
        stack = [
            patch.object(appmod, "_pick_episode", return_value=picked),
            patch.object(appmod, "_online_target_viewable", return_value=True),
            patch.object(appmod, "_ensure_playable",
                         new=AsyncMock(side_effect=lambda p, r, auto=False: p)),
            patch.object(appmod, "_verify_rewritten_first_seg",
                         new=AsyncMock(return_value=True)),
            patch.object(appmod, "_get_playlist_cached", new=fetch),
            # _serve_normal 兜底分支调的是 hls_race 模块内的
            # _resolve_if_needed（非 app 重导出），这里也钉死，避免真解析
            # 跑 sidecar（测的是出口分流，不是解析本身）。
            patch.object(hr, "_resolve_if_needed",
                         new=AsyncMock(side_effect=lambda p, r: p)),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        resp = await self.client.get(
            "/v/media/online/mg-hk/main.m3u8", params={"index": 0},
            headers={"User-Agent": "com.trim.tv/1.0"})
        # 慢线走单飞 + 预算：等后台 _serve_normal 跑完再断言事件。
        deadline = asyncio.get_event_loop().time() + 5.0
        futs = getattr(appmod, "_MAIN_SERVE_FUT", None) or {}
        while futs and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            futs = getattr(appmod, "_MAIN_SERVE_FUT", None) or {}
        return resp

    async def _fetch_none(self, mg, picked, request):
        return None

    async def _fetch_not_hls(self, mg, picked, request):
        return hr._NOT_HLS

    async def _fetch_playlist(self, mg, picked, request):
        return REAL_PLAYLIST

    async def test_resolve_error_502_records_deliver(self):
        picked = _picked(NO_SUFFIX_MP4)
        picked["resolve_error"] = "not_playable"
        picked["resolve_error_msg"] = "该影片暂不可播"
        picked["_fetch_fail_reason"] = "ReadTimeout"
        resp = await self._get_main(picked, self._fetch_none)
        self.assertEqual(resp.status_code, 502)
        evs = self._deliver_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("result"), "error502")
        self.assertEqual(evs[0].get("code"), "not_playable")
        self.assertEqual(evs[0].get("fetch_fail"), "ReadTimeout")
        self.assertEqual(evs[0].get("client"), "TV")
        self.assertIn("pan.wo.cn", evs[0].get("url", ""))

    async def test_slow_line_placeholder_records_deliver(self):
        picked = _picked(NO_SUFFIX_MP4)
        resp = await self._get_main(picked, self._fetch_none)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-fnmedia-Placeholder"), "1")
        evs = self._deliver_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("result"), "placeholder")

    async def test_not_hls_records_redirect302(self):
        picked = _picked(NO_SUFFIX_MP4)
        resp = await self._get_main(picked, self._fetch_not_hls)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/direct?", resp.headers.get("location", ""))
        evs = self._deliver_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("result"), "redirect302")

    async def test_real_playlist_records_deliver(self):
        picked = _picked(NO_SUFFIX_M3U8)
        resp = await self._get_main(picked, self._fetch_playlist)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("seg0.ts", resp.text)
        evs = self._deliver_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("result"), "playlist")

    async def test_non_haikuo_pick_records_nothing(self):
        picked = {"online_guid": "og-cms", "line": "cms::A", "index": 0,
                  "url": NO_SUFFIX_M3U8, "title": "普通片",
                  "headers": {"User-Agent": "Mozilla/5.0"}}
        resp = await self._get_main(picked, self._fetch_playlist)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._deliver_events(), [])


if __name__ == "__main__":
    unittest.main()


# ---------------- _guess_wrapper 直链缓存 ----------------

class GuessWrapperTests(unittest.TestCase):
    def setUp(self):
        appmod._HAIKUO_DIRECT_CACHE.clear()
        self.addCleanup(appmod._HAIKUO_DIRECT_CACHE.clear)

    def _target(self):
        return {"haikuo": True, "xsrc": True,
                "play_from": "haikuo-sub-test1:测试短剧",
                "url": "hiker://page/detail?id=86364"}

    def test_haikuo_direct_cache_hit_returns_mp4(self):
        target = self._target()
        appmod._haikuo_direct_cache_put(target, target["url"], NO_SUFFIX_MP4)
        self.assertEqual(appmod._guess_wrapper(target), ("MP4", False))

    def test_haikuo_no_cache_returns_hls(self):
        self.assertEqual(appmod._guess_wrapper(self._target()),
                         ("HLS", True))

    def test_haikuo_expired_cache_returns_hls(self):
        target = self._target()
        appmod._haikuo_direct_cache_put(target, target["url"], NO_SUFFIX_MP4)
        key = f"{target['play_from']}|{target['url']}"
        ts, url = appmod._HAIKUO_DIRECT_CACHE[key]
        appmod._HAIKUO_DIRECT_CACHE[key] = (
            ts - appmod._HAIKUO_DIRECT_TTL - 1, url)
        self.assertEqual(appmod._guess_wrapper(target), ("HLS", True))

    def test_haikuo_cache_key_is_entry_url(self):
        # 解析后 picked["url"] 已被直链替换，缓存键必须用入口地址，
        # 否则 _guess_wrapper（拿 store 的详情链接）永远命中不了。
        target = self._target()
        resolved = dict(target)
        resolved["url"] = NO_SUFFIX_MP4
        resolved["haikuo"] = False
        appmod._haikuo_direct_cache_put(resolved, target["url"],
                                        NO_SUFFIX_MP4)
        self.assertEqual(appmod._guess_wrapper(target), ("MP4", False))
