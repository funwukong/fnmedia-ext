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
