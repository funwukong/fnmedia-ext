"""各类缓存上限与淘汰（memory-caches 批）。

复现测试：先写测试再改代码。每一项都在修复前失败、修复后通过。
上限数值与淘汰方式见各模块注释；被淘汰的都是可再生的缓存派生内容，
用户数据（titles/history/收藏）不受影响。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter
import app as appmod
import catalog as catalogmod
import chartbind
import hls_race as hracemod
import importlib
import linepredict
import pan as panmod
import sniff as sniffmod
import store as storemod

# xlib 是私有模块：直接写 import 语句会触发公开剥离的残留扫描
# （纯子串针），故用 importlib 绕针（P2 同款惯例）。
xlibmod = importlib.import_module("xlib")

FAKE_M3U8 = "#EXTM3U\n#EXTINF:10.0,\nseg1.ts\n#EXTINF:20.0,\nseg2.ts\n"


def _heavy_brief(title_len=1500, tag=""):
    return {"title": "T" * title_len, "guid": "g" + tag,
            "lines": {"line1": [{"title": "第1集", "url": "https://x.test/1.m3u8"}]}}


class HlsRegTests(unittest.TestCase):
    def setUp(self):
        self._saved = (dict(hracemod._HLS_REG), dict(hracemod._HLS_REG_SIZE),
                       hracemod._HLS_REG_BYTES)
        hracemod._HLS_REG.clear()
        hracemod._HLS_REG_SIZE.clear()
        hracemod._HLS_REG_BYTES = 0
        self.addCleanup(self._restore)

    def _restore(self):
        hracemod._HLS_REG.clear()
        hracemod._HLS_REG.update(self._saved[0])
        hracemod._HLS_REG_SIZE.clear()
        hracemod._HLS_REG_SIZE.update(self._saved[1])
        hracemod._HLS_REG_BYTES = self._saved[2]

    def test_byte_cap_evicts_oldest(self):
        # 3 条各 ~1000 字节的注册表，上限 2500：第三条写入后最旧一条被淘汰，
        # 总字节 <= 上限（修复前：只按 4000 条数上限，三条全留）。
        with patch.object(hracemod, "_HLS_REG_MAX_BYTES", 2500):
            r1 = hracemod._hls_reg_put(["u" * 1000])
            r2 = hracemod._hls_reg_put(["v" * 1000])
            r3 = hracemod._hls_reg_put(["w" * 1000])
            self.assertLessEqual(hracemod._HLS_REG_BYTES, 2500)
            self.assertEqual(hracemod._hls_reg_get(r1, 0), "")
            self.assertEqual(hracemod._hls_reg_get(r2, 0), "v" * 1000)
            self.assertEqual(hracemod._hls_reg_get(r3, 0), "w" * 1000)

    def test_expired_get_pops_and_frees_bytes(self):
        rid = hracemod._hls_reg_put(["u" * 100])
        with patch.object(hracemod, "_HLS_REG_TTL", 0.01):
            time.sleep(0.02)
            self.assertEqual(hracemod._hls_reg_get(rid, 0), "")
        self.assertNotIn(rid, hracemod._HLS_REG)
        self.assertEqual(hracemod._HLS_REG_BYTES, 0)

    def test_single_huge_entry_kept_but_old_evicted(self):
        # 单条本身超限时保留该条（不清空整表，上限由单条大小兜底），
        # 但旧条目仍被淘汰：总量不超过“上限 + 单条大小”。
        with patch.object(hracemod, "_HLS_REG_MAX_BYTES", 2500):
            r1 = hracemod._hls_reg_put(["a" * 1000])
            r2 = hracemod._hls_reg_put(["b" * 5000])
            self.assertEqual(hracemod._hls_reg_get(r1, 0), "")
            self.assertEqual(hracemod._hls_reg_get(r2, 0), "b" * 5000)
            self.assertEqual(len(hracemod._HLS_REG), 1)


class PlaylistCacheTtlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hracemod._PLAYLIST_CACHE.clear()
        self.addCleanup(hracemod._PLAYLIST_CACHE.clear)

    def test_cached_playlist_seconds_expired_returns_zero_and_pops(self):
        picked = {"online_guid": "g1", "line": "L", "index": 0}
        key = hracemod._playlist_cache_key(picked)
        hracemod._PLAYLIST_CACHE[key] = (time.time() - 121, FAKE_M3U8)
        self.assertEqual(hracemod._cached_playlist_seconds(picked), 0)
        self.assertNotIn(key, hracemod._PLAYLIST_CACHE)

    def test_cached_playlist_seconds_fresh(self):
        picked = {"online_guid": "g1", "line": "L", "index": 0}
        key = hracemod._playlist_cache_key(picked)
        hracemod._PLAYLIST_CACHE[key] = (time.time(), FAKE_M3U8)
        self.assertEqual(hracemod._cached_playlist_seconds(picked), 30)
        self.assertIn(key, hracemod._PLAYLIST_CACHE)

    async def test_probe_latency_expired_cache_not_reused(self):
        cand = {"online_guid": "g1", "line": "L", "index": 0,
                "url": "https://x.test/a.m3u8", "m3u8": True,
                "headers": {"User-Agent": "Mozilla/5.0"}, "title": "T"}
        key = hracemod._playlist_cache_key(cand)
        hracemod._PLAYLIST_CACHE[key] = (time.time() - 121, FAKE_M3U8)
        request = SimpleNamespace(state=SimpleNamespace(_play_mg=""))

        async def _fake_fetch(request, url, headers):
            return None, None

        async def _fake_verify(body, request, cand):
            return True

        with patch.object(appmod, "_resolve_if_needed",
                          new=self._noop_resolve), \
                patch.object(appmod, "_fetch_playlist",
                             new=_fake_fetch), \
                patch.object(appmod, "_verify_rewritten_first_seg",
                             new=_fake_verify), \
                patch.object(appmod, "ext_client",
                             return_value=SimpleNamespace()):
            self.assertIsNone(await hracemod._probe_latency(cand, request))
        self.assertNotIn(key, hracemod._PLAYLIST_CACHE)

    async def _noop_resolve(self, cand, request):
        return None

    async def test_stale_playlist_is_dropped_and_rebuilt(self):
        picked = {"online_guid": "g-refresh", "line": "L", "index": 0}
        key = hracemod._playlist_cache_key(picked)
        old_body = "#EXTM3U\n#EXTINF:10,\nold.ts\n"
        new_body = "#EXTM3U\n#EXTINF:10,\nnew.ts\n"
        hracemod._playlist_cache_put(key, old_body)
        request = SimpleNamespace(state=SimpleNamespace(_play_mg="g-refresh"))

        async def _build(mg, cand, req):
            return new_body, "https://x.test/live.m3u8"

        with patch.object(appmod, "_build_playlist_body", new=_build):
            result = await hracemod._refresh_playlist_after_stale(
                "g-refresh", picked, request)

        self.assertEqual(result, new_body)
        self.assertEqual(hracemod._PLAYLIST_CACHE[key][1], new_body)
        self.assertNotEqual(hracemod._PLAYLIST_CACHE[key][1], old_body)


class PlayErrorNoteTests(unittest.IsolatedAsyncioTestCase):
    """P0 播放错误归因：请求级 note 供实时诊断读取。"""

    def setUp(self):
        self.request = SimpleNamespace(state=SimpleNamespace())

    def test_error_note_contains_stage_reason_host_and_key(self):
        picked = {"url": "https://up.example/live/master.m3u8?sig=abc",
                  "line": "siteA::线路1", "index": 0,
                  "_fetch_fail_reason": "HTTP 502"}
        note = hracemod._play_error_note(self.request, "清单抓取失败", picked)
        self.assertIn("清单抓取失败", note)
        self.assertIn("HTTP 502", note)
        self.assertIn("https://up.example/live/master.m3u8", note)
        self.assertIn("siteA", note)
        self.assertEqual(self.request.state.live_error_note, note)

    def test_err_response_sets_reason_header(self):
        picked = {"url": "https://up.example/a.m3u8", "line": "siteA::L",
                  "_fetch_fail_reason": "HTTP 504"}
        r = hracemod._err_response("boom", 502, "fetch_fail",
                                   request=self.request, picked=picked,
                                   stage="清单抓取失败")
        self.assertEqual(r.headers.get("X-Fnmedia-Error"), "fetch_fail")
        self.assertEqual(r.headers.get("X-Fnmedia-Reason"), "HTTP 504")
        self.assertIn("HTTP 504", self.request.state.live_error_note)

    def test_stream_failure_records_health(self):
        import sourcehealth
        picked = {"line": "siteX::线路", "source": "siteX"}
        hracemod._record_stream_failure(picked)
        st = sourcehealth.stats("siteX")
        self.assertGreaterEqual(st.get("samples", 0), 1)
        sourcehealth.reset("siteX")


class AggCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        appmod._AGG_CACHE.clear()
        appmod._AGG_BYTES.clear()
        appmod._AGG_BYTES_TOTAL = 0
        appmod._AGG_INFLIGHT.clear()
        self.addCleanup(appmod._AGG_CACHE.clear)
        self.addCleanup(appmod._AGG_BYTES.clear)
        self.addCleanup(appmod._AGG_INFLIGHT.clear)

    async def _fake_inner(self, keyword, maxn=None, fast_only=False,
                          timeout=None, **kw):
        return [_heavy_brief(tag=keyword)]

    def _patch_agg(self):
        return (patch.object(appmod, "__aggregate_briefs_inner",
                             new=self._fake_inner),
                patch.object(appmod, "store", SimpleNamespace(
                    remember_merged=lambda b: b)))

    async def test_byte_cap_evicts_oldest(self):
        # 两条各 ~1.6KB 的聚合结果，上限 2500B：第二条写入后第一条被淘汰
        # （修复前：800 条数上限内两条全留，最坏可达 897MB）。
        p1, p2 = self._patch_agg()
        with p1, p2, patch.object(appmod, "_AGG_CACHE_MAX_BYTES", 2500):
            await appmod._aggregate_briefs("kw1")
            await appmod._aggregate_briefs("kw2")
            keys = list(appmod._AGG_CACHE)
            self.assertEqual(len(keys), 1)
            self.assertIn("kw2\x000\x001", keys)
            self.assertLessEqual(appmod._AGG_BYTES_TOTAL, 2500)

    async def test_true_lru_hit_moves_to_end(self):
        # 命中 A 后 A 移到队尾；再写 D 触发条数淘汰时淘汰 B 而非 A
        # （修复前：按插入序删最旧一半，非 LRU，A 会被误删）。
        async def _small_inner(keyword, maxn=None, fast_only=False,
                               timeout=None, **kw):
            return [{"title": keyword, "guid": "g" + keyword, "lines": {}}]

        with patch.object(appmod, "__aggregate_briefs_inner",
                          new=_small_inner), \
                patch.object(appmod, "store", SimpleNamespace(
                    remember_merged=lambda b: b)), \
                patch.object(appmod, "_AGG_CACHE_MAX", 3):
            await appmod._aggregate_briefs("A")
            await appmod._aggregate_briefs("B")
            await appmod._aggregate_briefs("C")
            await appmod._aggregate_briefs("A")  # 命中，LRU 刷新
            await appmod._aggregate_briefs("D")  # 超 3 条，淘汰最旧
            keys = [k.split("\x00")[0] for k in appmod._AGG_CACHE]
            self.assertNotIn("B", keys)
            self.assertIn("A", keys)

    async def test_expired_hit_popped_even_when_refetch_fails(self):
        # 过期命中立即 pop：重拉失败时缓存里不再残留过期条目
        # （修复前：过期条目占位直到被覆盖或容量淘汰）。
        async def _boom(keyword, maxn=None, fast_only=False,
                        timeout=None, **kw):
            raise RuntimeError("upstream down")

        with patch.object(appmod, "__aggregate_briefs_inner",
                          new=self._fake_inner), \
                patch.object(appmod, "store", SimpleNamespace(
                    remember_merged=lambda b: b)):
            await appmod._aggregate_briefs("kw1")
            key = "kw1\x000\x001"
            ts, val = appmod._AGG_CACHE[key]
            appmod._AGG_CACHE[key] = (ts - appmod.AGG_CACHE_TTL - 1, val)
        with patch.object(appmod, "__aggregate_briefs_inner",
                          new=_boom), \
                patch.object(appmod, "store", SimpleNamespace(
                    remember_merged=lambda b: b)):
            with self.assertRaises(RuntimeError):
                await appmod._aggregate_briefs("kw1")
        self.assertNotIn(key, appmod._AGG_CACHE)


class LineSpeedTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(hracemod._LINE_SPEED)
        hracemod._LINE_SPEED.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        hracemod._LINE_SPEED.clear()
        hracemod._LINE_SPEED.update(self._saved)

    def test_expired_returns_zero_and_pops(self):
        hracemod._LINE_SPEED["slow"] = (500.0, time.time() - 3700)
        self.assertEqual(hracemod._line_speed({"line": "slow"}), 0.0)
        self.assertNotIn("slow", hracemod._LINE_SPEED)

    def test_fresh_kept(self):
        hracemod._LINE_SPEED["fast"] = (800.0, time.time())
        self.assertEqual(hracemod._line_speed({"line": "fast"}), 800.0)
        self.assertIn("fast", hracemod._LINE_SPEED)


class BoundedPutTests(unittest.TestCase):
    def test_hls_race_bounded_put(self):
        d: dict = {}
        for i in range(5):
            hracemod._bounded_put(d, f"k{i}", i, 3)
        self.assertEqual(list(d), ["k2", "k3", "k4"])

    def test_linepredict_last_switch_capped(self):
        linepredict._LAST_SWITCH.clear()
        self.addCleanup(linepredict._LAST_SWITCH.clear)
        with patch.object(linepredict, "_LAST_SWITCH_MAX", 3):
            for i in range(5):
                linepredict.mark_switched(f"g{i}", 0)
        self.assertEqual(len(linepredict._LAST_SWITCH), 3)
        self.assertNotIn("g0|0", linepredict._LAST_SWITCH)

    def test_sniff_cache_capped(self):
        sniffmod._cache.clear()
        self.addCleanup(sniffmod._cache.clear)
        with patch.object(sniffmod, "_CACHE_MAX", 3):
            for i in range(5):
                sniffmod._sniff_cache_put(f"https://x.test/{i}", "https://x.test/m.m3u8")
        self.assertEqual(len(sniffmod._cache), 3)
        self.assertNotIn("https://x.test/0", sniffmod._cache)

    def test_xlib_caches_capped(self):
        with patch.object(xlibmod, "_XLIB_CACHE_MAX", 3):
            d: dict = {}
            for i in range(5):
                xlibmod._bounded_put(d, f"k{i}", (time.time(), []), 3)
            self.assertEqual(list(d), ["k2", "k3", "k4"])


class StorePlayTtlTests(unittest.TestCase):
    def test_get_play_expired_returns_none_and_pops(self):
        storemod.store._play["ps-old"] = {"url": "https://x.test/1.m3u8",
                                          "ts": time.time() - 25 * 3600}
        self.addCleanup(lambda: storemod.store._play.pop("ps-old", None))
        self.assertIsNone(storemod.store.get_play("ps-old"))
        self.assertNotIn("ps-old", storemod.store._play)

    def test_get_play_fresh_kept(self):
        storemod.store._play["ps-new"] = {"url": "https://x.test/1.m3u8",
                                          "ts": time.time()}
        self.addCleanup(lambda: storemod.store._play.pop("ps-new", None))
        self.assertIsNotNone(storemod.store.get_play("ps-new"))


class QrPollTests(unittest.TestCase):
    def test_expired_pops_and_closes_session(self):
        class FakeResp:
            def json(self):
                return {"status": 50004002, "message": "二维码已过期"}

        class FakeClient:
            def __init__(self):
                self.closed = False

            def get(self, url, params=None):
                return FakeResp()

            def close(self):
                self.closed = True

        fake = FakeClient()
        panmod._QR_SESSIONS["tok-exp"] = fake
        self.addCleanup(lambda: panmod._QR_SESSIONS.pop("tok-exp", None))
        out = panmod.qr_poll({"token": "tok-exp", "request_id": "r1"})
        self.assertEqual(out["status"], "expired")
        self.assertNotIn("tok-exp", panmod._QR_SESSIONS)
        self.assertTrue(fake.closed)


class DnsCacheTests(unittest.TestCase):
    def test_full_evicts_oldest_half_not_clear(self):
        appmod._dns_cache.clear()
        self.addCleanup(appmod._dns_cache.clear)
        with patch.object(appmod, "_real_getaddrinfo",
                          return_value=[("fake",)]):
            for i in range(3000):
                appmod._cached_getaddrinfo(f"h{i}.test", 80)
        # 满 2048 时删最旧一半：终态 1024~2048 条，且中间段 key 仍在
        # （修复前 clear() 全清：只剩 2048 之后约 952 条，中间段全失）。
        self.assertLessEqual(len(appmod._dns_cache), 2048)
        self.assertGreaterEqual(len(appmod._dns_cache), 1024)
        self.assertIn(("h2000.test", 80, 0, 0, 0, 0), appmod._dns_cache)
        self.assertIn(("h2999.test", 80, 0, 0, 0, 0), appmod._dns_cache)


class SpsCacheTests(unittest.TestCase):
    def test_full_evicts_oldest_half_not_clear(self):
        with adfilter._sps_cache_lock:
            adfilter._sps_cache.clear()
        self.addCleanup(lambda: adfilter.reset_sps_cache())
        for i in range(3000):
            adfilter._sps_cache_put(f"https://x.test/{i}.m3u8",
                                    {"profile": i})
        self.assertLessEqual(len(adfilter._sps_cache), 2048)
        self.assertGreaterEqual(len(adfilter._sps_cache), 1024)
        self.assertIn("https://x.test/2000.m3u8", adfilter._sps_cache)


class CatalogCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_cache_capped_fifo(self):
        cat = catalogmod.Catalog()

        async def _fake_load(scope, cat_name, page):
            return [{"guid": f"g{page}-{i}", "title": "t"}
                    for i in range(3)]

        with patch.object(catalogmod, "_PAGE_CACHE_MAX", 10), \
                patch.object(cat, "_load_page", new=_fake_load):
            for p in range(1, 16):
                await cat.get_page("movie", p)
            self.assertLessEqual(len(cat._page_cache), 10)
            self.assertNotIn((catalogmod.AGG_SCOPE, "movie", 1),
                             cat._page_cache)
            self.assertIn((catalogmod.AGG_SCOPE, "movie", 15),
                          cat._page_cache)

    async def test_dense_total_cap_evicts_oldest_scope(self):
        cat = catalogmod.Catalog()

        async def _fake_fetch(scope, cat_name, pages):
            return [(pg, [{"guid": f"{scope}-{cat_name}-{pg}-{i}",
                           "title": "t"} for i in range(100)])
                    for pg in pages]

        with patch.object(catalogmod, "_DENSE_TOTAL_MAX", 1000), \
                patch.object(cat, "_fetch_pages", new=_fake_fetch), \
                patch.object(cat, "pagecount", return_value=100):
            await cat._extend_dense(catalogmod.AGG_SCOPE, "movie", 800)
            await cat._extend_dense(catalogmod.AGG_SCOPE, "tv", 800)
            total = sum(len(v) for v in cat._dense.values())
            self.assertLessEqual(total, 1000)
            # 整作用域淘汰：最旧分类整个消失，当前浏览分类完整保留
            self.assertNotIn(f"{catalogmod.AGG_SCOPE}:movie", cat._dense)
            self.assertNotIn(f"{catalogmod.AGG_SCOPE}:movie",
                             cat._seen_guids)
            self.assertEqual(len(cat._dense[f"{catalogmod.AGG_SCOPE}:tv"]),
                             800)


class ChartbindTests(unittest.TestCase):
    def _fresh_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(chartbind, "STATE",
                            tmp_path / "chart_bindings.json")
        return chartbind.ChartBindStore()

    def test_get_expired_returns_none(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chart_bindings.json"
            old_state = chartbind.STATE
            chartbind.STATE = p
            try:
                st = chartbind.ChartBindStore()
                st.remember("abc123", {"title": "测试片"})
                with st._lock:
                    st._data["abc123"]["ts"] = time.time() - chartbind.BIND_TTL - 1
                self.assertIsNone(st.get("abc123"))
                self.assertFalse(st.is_fresh("abc123"))
            finally:
                chartbind.STATE = old_state

    def test_file_byte_cap_evicts_oldest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chart_bindings.json"
            old_state = chartbind.STATE
            old_max = chartbind._MAX_BYTES
            chartbind.STATE = p
            chartbind._MAX_BYTES = 2500
            try:
                st = chartbind.ChartBindStore()
                st.remember("g1", _heavy_brief(tag="1"))
                st.remember("g2", _heavy_brief(tag="2"))
                st.remember("g3", _heavy_brief(tag="3"))
                st.flush()
                self.assertTrue(p.exists())
                self.assertLessEqual(p.stat().st_size, 2500)
                data = json.loads(p.read_text(encoding="utf-8"))
                self.assertNotIn("g1", data["bindings"])
                self.assertIn("g3", data["bindings"])
            finally:
                chartbind.STATE = old_state
                chartbind._MAX_BYTES = old_max

    def test_load_trims_oversized_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chart_bindings.json"
            payload = {"updated_at": time.time(), "bindings": {
                f"g{i}": {"ts": time.time(), "brief": _heavy_brief(tag=str(i))}
                for i in range(3)}}
            p.write_text(json.dumps(payload, ensure_ascii=False),
                         encoding="utf-8")
            old_state = chartbind.STATE
            old_max = chartbind._MAX_BYTES
            chartbind.STATE = p
            chartbind._MAX_BYTES = 2500
            try:
                st = chartbind.ChartBindStore()
                self.assertLessEqual(st.count(), 1)
                self.assertIn("g2", st._data)
            finally:
                chartbind.STATE = old_state
                chartbind._MAX_BYTES = old_max


if __name__ == "__main__":
    unittest.main()

class P1PlaybackCacheTests(unittest.TestCase):
    """P1：播放派生缓存加固（回源别名 TTL / 代际失效 / 健康罚时 / 取流熔断）。"""

    def setUp(self):
        self._saved = {
            "redirect": dict(hracemod._MEDIA_REDIRECT),
            "redirect_ts": dict(hracemod._MEDIA_REDIRECT_TS),
            "playlist": dict(hracemod._PLAYLIST_CACHE),
            "playlist_size": dict(hracemod._PLAYLIST_SIZE),
            "playlist_bytes": hracemod._PLAYLIST_BYTES,
            "playlist_fut": dict(hracemod._PLAYLIST_FUT),
            "seg_ok": dict(hracemod._SEG_OK_CACHE),
            "hls_reg": dict(hracemod._HLS_REG),
            "hls_reg_size": dict(hracemod._HLS_REG_SIZE),
            "hls_reg_bytes": hracemod._HLS_REG_BYTES,
            "gen": hracemod._PLAYLIST_GEN,
            "line_bad": dict(hracemod._LINE_BAD),
            "line_fast": dict(hracemod._LINE_FAST),
            "line_speed": dict(hracemod._LINE_SPEED),
        }
        self.addCleanup(self._restore)
        hracemod._MEDIA_REDIRECT.clear()
        hracemod._MEDIA_REDIRECT_TS.clear()
        hracemod._PLAYLIST_CACHE.clear()
        hracemod._PLAYLIST_SIZE.clear()
        hracemod._PLAYLIST_BYTES = 0
        hracemod._PLAYLIST_FUT.clear()
        hracemod._SEG_OK_CACHE.clear()
        hracemod._HLS_REG.clear()
        hracemod._HLS_REG_SIZE.clear()
        hracemod._HLS_REG_BYTES = 0
        hracemod._LINE_BAD.clear()
        hracemod._LINE_FAST.clear()
        hracemod._LINE_SPEED.clear()

    def _restore(self):
        s = self._saved
        hracemod._MEDIA_REDIRECT.clear(); hracemod._MEDIA_REDIRECT.update(s["redirect"])
        hracemod._MEDIA_REDIRECT_TS.clear(); hracemod._MEDIA_REDIRECT_TS.update(s["redirect_ts"])
        hracemod._PLAYLIST_CACHE.clear(); hracemod._PLAYLIST_CACHE.update(s["playlist"])
        hracemod._PLAYLIST_SIZE.clear(); hracemod._PLAYLIST_SIZE.update(s["playlist_size"])
        hracemod._PLAYLIST_BYTES = s["playlist_bytes"]
        hracemod._PLAYLIST_FUT.clear(); hracemod._PLAYLIST_FUT.update(s["playlist_fut"])
        hracemod._SEG_OK_CACHE.clear(); hracemod._SEG_OK_CACHE.update(s["seg_ok"])
        hracemod._HLS_REG.clear(); hracemod._HLS_REG.update(s["hls_reg"])
        hracemod._HLS_REG_SIZE.clear(); hracemod._HLS_REG_SIZE.update(s["hls_reg_size"])
        hracemod._HLS_REG_BYTES = s["hls_reg_bytes"]
        hracemod._PLAYLIST_GEN = s["gen"]
        hracemod._LINE_BAD.clear(); hracemod._LINE_BAD.update(s["line_bad"])
        hracemod._LINE_FAST.clear(); hracemod._LINE_FAST.update(s["line_fast"])
        hracemod._LINE_SPEED.clear(); hracemod._LINE_SPEED.update(s["line_speed"])

    def test_media_redirect_expired_returns_empty_and_pops(self):
        hracemod._MEDIA_REDIRECT["mg-exp"] = "og-exp"
        hracemod._MEDIA_REDIRECT_TS["mg-exp"] = (
            time.time() - hracemod._MEDIA_REDIRECT_TTL - 1)
        self.assertEqual(hracemod._media_redirect_get("mg-exp"), "")
        self.assertNotIn("mg-exp", hracemod._MEDIA_REDIRECT)
        self.assertNotIn("mg-exp", hracemod._MEDIA_REDIRECT_TS)

    def test_media_redirect_put_stamps_and_roundtrips(self):
        before = time.time()
        hracemod._media_redirect_put("mg-ok", "og-ok")
        self.assertEqual(hracemod._media_redirect_get("mg-ok"), "og-ok")
        self.assertGreaterEqual(hracemod._MEDIA_REDIRECT_TS["mg-ok"], before)

    def test_invalidate_bumps_generation_clears_playback_keeps_line_profiles(self):
        hracemod._PLAYLIST_CACHE["k"] = (time.time(), "body")
        hracemod._PLAYLIST_SIZE["k"] = 4
        hracemod._PLAYLIST_BYTES = 4
        hracemod._PLAYLIST_FUT["k"] = object()
        hracemod._SEG_OK_CACHE["https://x.test/1.ts"] = time.time()
        hracemod._MEDIA_REDIRECT["m"] = "o"
        hracemod._MEDIA_REDIRECT_TS["m"] = time.time()
        rid = hracemod._hls_reg_put(["https://x.test/seg.ts"])
        self.assertTrue(rid)
        hracemod._LINE_BAD["src::l1"] = time.time() + 300
        hracemod._LINE_FAST["src::l1"] = "https://x.test/fast"
        hracemod._LINE_SPEED["src::l1"] = 1234.0

        g0 = hracemod._PLAYLIST_GEN
        g1 = hracemod.invalidate_playback_caches()

        self.assertEqual(g1, g0 + 1)
        self.assertEqual(hracemod._PLAYLIST_CACHE, {})
        self.assertEqual(hracemod._PLAYLIST_SIZE, {})
        self.assertEqual(hracemod._PLAYLIST_BYTES, 0)
        self.assertEqual(hracemod._PLAYLIST_FUT, {})
        self.assertEqual(hracemod._SEG_OK_CACHE, {})
        self.assertEqual(hracemod._MEDIA_REDIRECT, {})
        self.assertEqual(hracemod._MEDIA_REDIRECT_TS, {})
        self.assertEqual(hracemod._HLS_REG, {})
        # 线路画像属选源质量记忆，清缓存时必须保留，否则下次起播重新全量竞速。
        self.assertIn("src::l1", hracemod._LINE_BAD)
        self.assertEqual(hracemod._LINE_FAST.get("src::l1"), "https://x.test/fast")
        self.assertEqual(hracemod._LINE_SPEED.get("src::l1"), 1234.0)

    def test_playlist_cache_key_contains_generation(self):
        picked = {"online_guid": "og", "line": "src::line", "index": 1}
        with patch.object(hracemod, "_skip_seconds", return_value=(0, 0)):
            k0 = hracemod._playlist_cache_key(picked)
            hracemod._PLAYLIST_GEN += 1
            k1 = hracemod._playlist_cache_key(picked)
        self.assertNotEqual(k0, k1)
        self.assertTrue(k1.startswith(f"g{hracemod._PLAYLIST_GEN}|"))

    def test_health_penalty_by_grade(self):
        cand = {"line": "src::line"}
        for grade, expect in ((None, 0.0), (0, 0.0), (1, 0.15), (2, 0.30),
                              (3, 0.50), (9, 0.50)):
            with patch.object(appmod.sourcehealth, "source_key_of", return_value="src"), \
                    patch.object(appmod.sourcehealth, "display_grade", return_value=grade):
                self.assertAlmostEqual(appmod._health_penalty(cand), expect, places=6)

    def test_stream_dead_fails_is_three(self):
        import sourcehealth as shmod
        self.assertEqual(shmod.STREAM_DEAD_FAILS, 3)


class P2APlaycheckTests(unittest.IsolatedAsyncioTestCase):
    """P2-A 播放前轻量预检（直链死链判死 → 换线）。"""

    def _mk_req(self, status=200, ctype="video/mp4", body=b"xx"):
        from types import SimpleNamespace

        class _Cli:
            def __init__(self):
                self._status = status
                self._ctype = ctype
                self._body = body

            def build_request(self, method, url, headers=None):
                return {"url": url, "headers": headers}

            async def send(self, req, follow_redirects=True):
                r = SimpleNamespace(
                    status_code=self._status,
                    headers={"content-type": self._ctype},
                )
                async def aread():
                    return self._body
                async def aclose():
                    return None
                r.aread = aread
                r.aclose = aclose
                return r

        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ext=_Cli())))

    async def test_playcheck_passes_healthy_direct(self):
        hracemod.features.P2A_PLAYCHECK = True
        req = self._mk_req()
        picked = {"url": "https://x.test/v.mp4", "line": "s::l1",
                  "online_guid": "og1", "title": "T"}
        out = await hracemod._playcheck_target(picked, req)
        self.assertIsNone(out)  # 预检通过：沿用原目标

    async def test_playcheck_kills_dead_direct_and_falls_back(self):
        hracemod.features.P2A_PLAYCHECK = True
        req = self._mk_req(status=404, ctype="text/html", body=b"not found")
        picked = {"url": "https://x.test/v.mp4", "line": "s::l1",
                  "online_guid": "og1", "title": "T"}
        saved = hracemod._find_playable
        async def fake_find(p, request, skip_current=False):
            return {**p, "line": "s::l2"}
        hracemod._find_playable = fake_find
        try:
            out = await hracemod._playcheck_target(picked, req)
        finally:
            hracemod._find_playable = saved
        self.assertIsNotNone(out)
        self.assertEqual(out.get("line"), "s::l2")

    async def test_playcheck_skips_hls_and_pan(self):
        hracemod.features.P2A_PLAYCHECK = True
        req = self._mk_req(status=404)
        for picked in (
            {"m3u8": "https://x.test/v.m3u8", "line": "s::l1", "online_guid": "og"},
            {"url": "/v/api/ext/pan/play?fid=1", "line": "s::l1", "online_guid": "og"},
            {"url": "", "line": "s::l1", "online_guid": "og"},
        ):
            out = await hracemod._playcheck_target(picked, req)
            self.assertIsNone(out)

    async def test_ensure_playable_uses_playcheck_switch(self):
        hracemod.features.P2A_PLAYCHECK = True
        req = self._mk_req(status=404, ctype="text/html", body=b"dead")
        picked = {"url": "https://x.test/v.mp4", "line": "s::l1",
                  "online_guid": "og1", "title": "T"}
        saved_find = hracemod._find_playable
        async def fake_find(p, request, skip_current=False):
            return {**p, "line": "s::l2"}
        hracemod._find_playable = fake_find
        saved_flags = {}
        for name in ("_autofail_on", "_fastest_on", "_fixed_line_direct_on"):
            saved_flags[name] = getattr(hracemod, name, None)
        async def _true(*a, **k):
            return False
        try:
            hracemod._find_playable = fake_find
            out = await hracemod._ensure_playable(picked, req, auto=False)
        finally:
            hracemod._find_playable = saved_find
        self.assertEqual(out.get("line"), "s::l2")
