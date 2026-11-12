"""播放历史快照（上游 447b452）移植到 fork 规范键的契约测试。

验证：record 时把精简 brief 快照存入【规范键桶】；store 缓存淘汰后仍能
凭快照重建影片/剧集信息并恢复 ts 续播；跨线（ffzy/dytt 同一标题）可查；
快照上限（每集 6 线、单条 64KB 软上限）enforced；快照缺失时回退原有行为；
过期/超限直链（_slim）仍可恢复标题+ts、由播放链重搜补线。

全部离线、不联网。Part A 只测 proxy/history.py；Part B 走 ASGI 测
proxy/app.py 的接线（play/record 存快照、淘汰后 play/info 与 play/list
用快照恢复、固定线路卡续播到最新集）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import history as histmod
import store as storemod
from history import History
from store import episode_guid

FFZY = "ffzy::ffzy"
DYTT = "dytt::dytt"
TITLE = "快照测试剧"
MOVIE = "快照测试电影"


def tv_brief(guid, title, line, n=8):
    eps = [{"title": "第%d集" % (i + 1),
            "url": "https://%s.test/v%d.m3u8" % (line.split("::")[0], i),
            "direct": True} for i in range(n)]
    return {"guid": guid, "title": title, "year": "2024",
            "type_name": "电视剧", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "", "lines": {line: eps}}


def movie_brief(guid, title, line):
    return {"guid": guid, "title": title, "year": "2024",
            "type_name": "电影", "poster": "https://img.test/p.jpg",
            "overview": "简介", "score": "8", "actor": "", "director": "",
            "area": "", "remarks": "",
            "lines": {line: [{"title": "正片",
                              "url": "https://%s.test/v.m3u8" % line.split("::")[0],
                              "direct": True}]}}


@contextmanager
def _patched_store(tmp_path):
    """真实 Store（两线同标题 + 电影两线），供规范键解析用。"""
    orig_state = storemod._STATE
    storemod._STATE = tmp_path / "titles.json"
    orig_store = storemod.store
    s = storemod.Store()
    storemod.store = s
    try:
        s.remember_merged([
            tv_brief("og-ffzy", TITLE, FFZY),
            tv_brief("og-dytt", TITLE, DYTT),
            tv_brief("og-other", "另一部剧", FFZY),
            movie_brief("og-mv-ffzy", MOVIE, FFZY),
            movie_brief("og-mv-dytt", MOVIE, DYTT),
        ])
        yield s
    finally:
        s.close()
        storemod.store = orig_store
        storemod._STATE = orig_state


@contextmanager
def _patched_store_empty(tmp_path):
    """store 被清空（模拟 titles 缓存淘汰）：规范键算不到，只剩快照。"""
    orig_state = storemod._STATE
    storemod._STATE = tmp_path / "titles-empty.json"
    orig_store = storemod.store
    s = storemod.Store()
    storemod.store = s
    try:
        yield s
    finally:
        s.close()
        storemod.store = orig_store
        storemod._STATE = orig_state


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._tmp_path = Path(self._tmp.name)
        self._orig_state = histmod._STATE
        histmod._STATE = self._tmp_path / "history.json"
        self.addCleanup(setattr, histmod, "_STATE", self._orig_state)
        self._store_ctx = _patched_store(self._tmp_path)
        self.store = self._store_ctx.__enter__()
        self.addCleanup(self._store_ctx.__exit__, None, None, None)
        self.h = History()
        self.ffzy_eps = [episode_guid("og-ffzy", "", i) for i in range(8)]
        self.dytt_eps = [episode_guid("og-dytt", "", i) for i in range(8)]

    def _canon_bucket_keys(self):
        return [k for k in self.h._users.get("", {}).keys()
                if k.startswith("canon:")]


class SnapshotBuildTests(SnapshotTestBase):
    def test_movie_snapshot_keeps_one_playable(self):
        snap = histmod.build_snapshot(movie_brief("og-mv-ffzy", MOVIE, FFZY))
        self.assertEqual(snap["guid"], "og-mv-ffzy")
        self.assertEqual(len(snap["lines"][FFZY]), 1)
        self.assertEqual(snap["lines"][FFZY][0]["url"],
                         "https://ffzy.test/v.m3u8")

    def test_tv_snapshot_keeps_only_target_episode(self):
        ep_guid = "abc123hash456"
        ep = {"ep_guid": ep_guid, "index": 5, "season": 1}
        brief = tv_brief("og-ffzy", TITLE, FFZY, n=100)
        snap = histmod.build_snapshot(brief, ep_guid, ep=ep)
        self.assertEqual(len(snap["lines"][FFZY]), 1)
        self.assertEqual(snap["lines"][FFZY][0]["url"],
                         "https://ffzy.test/v5.m3u8")
        self.assertEqual(snap["_ep_index"], 5)
        self.assertEqual(snap["_ep_guid"], ep_guid)
        self.assertLess(len(json.dumps(snap).encode()), 4096)

    def test_tv_snapshot_plaintext_guid_fallback(self):
        snap = histmod.build_snapshot(tv_brief("og-ffzy", TITLE, FFZY),
                                      "ep:og-ffzy:3")
        self.assertEqual(snap["_ep_index"], 3)
        self.assertEqual(snap["lines"][FFZY][0]["url"],
                         "https://ffzy.test/v3.m3u8")

    def test_invalid_brief_returns_none(self):
        self.assertIsNone(histmod.build_snapshot(None))
        self.assertIsNone(histmod.build_snapshot({}))
        self.assertIsNone(histmod.build_snapshot({"title": "无 guid"}))

    def test_snapshot_keeps_only_16_meta_keys(self):
        brief = movie_brief("og-mv-ffzy", MOVIE, FFZY)
        brief["duration"] = 5400
        brief["ep_durations"] = {"0": 5400}
        brief["internal_debug"] = "must-not-leak"
        snap = histmod.build_snapshot(brief)
        self.assertNotIn("duration", snap)
        self.assertNotIn("ep_durations", snap)
        self.assertNotIn("internal_debug", snap)
        extra = set(snap.keys()) - {"lines", "_ep_index", "_ep_guid", "_slim"}
        self.assertTrue(extra <= set(histmod._SNAPSHOT_KEYS))

    def test_snapshot_line_cap_six(self):
        """每集限 6 线：第 7 条及以后的线路不进快照。"""
        lines = {"L%d" % i: [{"title": "第1集",
                              "url": "https://x.test/%d.m3u8" % i,
                              "direct": True}] for i in range(8)}
        brief = {"guid": "og-ffzy", "title": TITLE, "lines": lines}
        snap = histmod.build_snapshot(brief, self.ffzy_eps[0],
                                      ep={"ep_guid": self.ffzy_eps[0],
                                          "index": 0})
        self.assertEqual(len(snap["lines"]), 6)

    def test_snapshot_oversize_drops_lines_and_marks_slim(self):
        """单条超 64KB 软上限：丢弃 lines 只留元信息，标 _slim。"""
        big_url = "https://x.test/" + "a" * (70 * 1024) + ".m3u8"
        brief = {"guid": "og-ffzy", "title": TITLE,
                 "lines": {FFZY: [{"title": "第1集", "url": big_url,
                                   "direct": True}]}}
        snap = histmod.build_snapshot(brief, self.ffzy_eps[0],
                                      ep={"ep_guid": self.ffzy_eps[0],
                                          "index": 0})
        self.assertIsNotNone(snap)
        self.assertEqual(snap["lines"], {})
        self.assertTrue(snap.get("_slim"))
        self.assertEqual(snap["guid"], "og-ffzy")
        self.assertEqual(snap["title"], TITLE)


class HistorySnapshotRecordTests(SnapshotTestBase):
    def _ep3_snap(self):
        return histmod.build_snapshot(
            tv_brief("og-ffzy", TITLE, FFZY), self.ffzy_eps[3],
            ep={"ep_guid": self.ffzy_eps[3], "index": 3})

    def test_record_stores_snapshot_under_canon_key(self):
        """快照随 record() 的规范键合并走：桶键是 canon 键，不是分源 guid。"""
        snap = self._ep3_snap()
        self.assertTrue(self.h.record({"item_guid": self.ffzy_eps[3],
                                       "ts": 600, "duration": 3600},
                                      snapshot=snap))
        keys = self._canon_bucket_keys()
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("canon:tv:"))
        self.assertNotIn(self.ffzy_eps[3], self.h._users.get("", {}))
        rec = self.h.get(self.ffzy_eps[3])
        self.assertEqual(rec["ts"], 600)
        self.assertEqual(rec["snapshot"]["lines"][FFZY][0]["url"],
                         "https://ffzy.test/v3.m3u8")

    def test_get_snapshot_cross_line(self):
        """跨线可查：ffzy 集记录的快照，用 dytt 集 guid 也能取到。"""
        self.h.record({"item_guid": self.ffzy_eps[3], "ts": 600},
                      snapshot=self._ep3_snap())
        got = self.h.get_snapshot(self.dytt_eps[3])
        self.assertIsNotNone(got)
        self.assertEqual(got["guid"], "og-ffzy")
        self.assertEqual(got["_ep_index"], 3)

    def test_get_snapshot_by_canon_key_direct(self):
        snap = histmod.build_snapshot(movie_brief("og-mv-ffzy", MOVIE, FFZY))
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 100}, snapshot=snap)
        ck = histmod.canonical_movie_key(MOVIE)
        got = self.h.get_snapshot(ck)
        self.assertIsNotNone(got)
        self.assertEqual(got["guid"], "og-mv-ffzy")

    def test_repeated_record_keeps_snapshot_without_recompute(self):
        snap = histmod.build_snapshot(movie_brief("og-mv-ffzy", MOVIE, FFZY))
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 10}, snapshot=snap)
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 20})
        self.assertIsNotNone(self.h.get_snapshot("og-mv-ffzy"))
        self.assertEqual(self.h.get("og-mv-ffzy")["ts"], 20)

    def test_new_snapshot_overwrites_old(self):
        snap1 = histmod.build_snapshot(movie_brief("og-mv-ffzy", MOVIE, FFZY))
        snap2 = histmod.build_snapshot(movie_brief("og-mv-dytt", MOVIE, DYTT))
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 10}, snapshot=snap1)
        self.h.record({"item_guid": "og-mv-dytt", "ts": 20}, snapshot=snap2)
        got = self.h.get_snapshot("og-mv-ffzy")
        self.assertEqual(got["guid"], "og-mv-dytt")

    def test_record_without_snapshot_leaves_no_snapshot(self):
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 10})
        self.assertIsNone(self.h.get_snapshot("og-mv-ffzy"))
        self.assertIsNone(self.h.latest_episode_record("og-mv-ffzy"))

    def test_persistence_across_reload(self):
        snap = histmod.build_snapshot(
            tv_brief("og-ffzy", TITLE, FFZY), self.ffzy_eps[5],
            ep={"ep_guid": self.ffzy_eps[5], "index": 5})
        self.h.record({"item_guid": self.ffzy_eps[5], "ts": 300},
                      snapshot=snap)
        self.h.flush()
        reloaded = History()
        got = reloaded.get_snapshot(self.ffzy_eps[5])
        self.assertIsNotNone(got)
        self.assertEqual(got["lines"][FFZY][0]["url"],
                         "https://ffzy.test/v5.m3u8")

    def test_episode_index_parse(self):
        self.assertEqual(histmod._ep_index_from_guid("ep:tvguid:5"), 5)
        self.assertIsNone(histmod._ep_index_from_guid("movieguid"))
        self.assertIsNone(histmod._ep_index_from_guid("ep:tvguid:x"))


class LatestEpisodeRecordTests(SnapshotTestBase):
    def _tv_snap(self, og, ep_idx, ep_guid):
        return {"guid": og, "title": TITLE, "_ep_index": ep_idx,
                "_ep_guid": ep_guid,
                "lines": {FFZY: [{"url": "https://ffzy.test/v%d.m3u8" % ep_idx}]}}

    def test_find_latest_episode_by_online_guid(self):
        self.h.record({"item_guid": self.ffzy_eps[1], "ts": 100},
                      snapshot=self._tv_snap("og-ffzy", 1, self.ffzy_eps[1]))
        self.h.record({"item_guid": self.ffzy_eps[4], "ts": 1243},
                      snapshot=self._tv_snap("og-ffzy", 4, self.ffzy_eps[4]))
        self.h.record({"item_guid": "og-ffzy", "ts": 980},
                      snapshot={"guid": "og-ffzy", "lines": {}})
        got = self.h.latest_episode_record("og-ffzy")
        self.assertIsNotNone(got)
        self.assertEqual(got["item_guid"], self.ffzy_eps[4])
        self.assertEqual(got["ts"], 1243)

    def test_latest_episode_cross_line(self):
        """跨线：只在 ffzy 录了第 4 集，用 dytt 标题 guid 也能定位到它。"""
        self.h.record({"item_guid": self.ffzy_eps[3], "ts": 600},
                      snapshot=self._tv_snap("og-ffzy", 3, self.ffzy_eps[3]))
        got = self.h.latest_episode_record("og-dytt")
        self.assertIsNotNone(got)
        self.assertEqual(got["item_guid"], self.ffzy_eps[3])
        self.assertEqual(got["snapshot"]["_ep_index"], 3)

    def test_latest_episode_no_cross_title_leak(self):
        self.h.record({"item_guid": self.ffzy_eps[3], "ts": 600},
                      snapshot=self._tv_snap("og-ffzy", 3, self.ffzy_eps[3]))
        self.assertIsNone(self.h.latest_episode_record("og-other"))

    def test_returns_none_when_no_episode(self):
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 50},
                      snapshot={"guid": "og-mv-ffzy",
                                "lines": {FFZY: [{"url": "u"}]}})
        self.assertIsNone(self.h.latest_episode_record("og-mv-ffzy"))

    def test_latest_episode_after_store_evicted_same_line(self):
        """store 淘汰后：同线标题 guid 仍能按 snapshot.guid 定位到集记录。"""
        self.h.record({"item_guid": self.ffzy_eps[3], "ts": 600},
                      snapshot=self._tv_snap("og-ffzy", 3, self.ffzy_eps[3]))
        with _patched_store_empty(self._tmp_path):
            got = self.h.latest_episode_record("og-ffzy")
        self.assertIsNotNone(got)
        self.assertEqual(got["snapshot"]["_ep_index"], 3)


class StoreEvictionTests(SnapshotTestBase):
    def test_record_after_eviction_stays_in_canon_bucket(self):
        """store 淘汰后再次上报：仍落回原规范键桶，不另起旧键桶。"""
        snap = histmod.build_snapshot(
            tv_brief("og-ffzy", TITLE, FFZY), self.ffzy_eps[3],
            ep={"ep_guid": self.ffzy_eps[3], "index": 3})
        self.h.record({"item_guid": self.ffzy_eps[3], "ts": 100},
                      snapshot=snap)
        with _patched_store_empty(self._tmp_path):
            rec = self.h.get(self.ffzy_eps[3])
            self.assertIsNotNone(rec)
            self.assertEqual(rec["ts"], 100)
            prev_snap = self.h.get_snapshot(self.ffzy_eps[3])
            self.assertIsNotNone(prev_snap)
            self.h.record({"item_guid": self.ffzy_eps[3], "ts": 250},
                          snapshot=prev_snap)
        keys = self._canon_bucket_keys()
        self.assertEqual(len(keys), 1)
        self.assertNotIn(self.ffzy_eps[3], self.h._users.get("", {}))
        self.assertEqual(self.h.get(self.ffzy_eps[3])["ts"], 250)
        self.assertIsNotNone(self.h.get_snapshot(self.ffzy_eps[3]))

    def test_merge_preserves_snapshot(self):
        """旧键遗留记录与规范键记录合并时，快照不丢。"""
        snap = histmod.build_snapshot(movie_brief("og-mv-ffzy", MOVIE, FFZY))
        bucket = self.h._bucket("")
        bucket["og-mv-ffzy"] = {"item_guid": "og-mv-ffzy", "ts": 100,
                                "duration": 0, "updated_at": time.time() - 10,
                                "snapshot": snap}
        self.h._dirty = True
        rec = self.h.get("og-mv-ffzy")
        self.assertIsNotNone(rec.get("snapshot"))
        self.assertEqual(rec["snapshot"]["guid"], "og-mv-ffzy")

    def test_update_existing_record_without_store(self):
        snap = histmod.build_snapshot(movie_brief("og-mv-ffzy", MOVIE, FFZY))
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 100}, snapshot=snap)
        self.assertIsNotNone(self.h.get("og-mv-ffzy"))
        self.assertIsNotNone(self.h.get_snapshot("og-mv-ffzy"))
        self.h.record({"item_guid": "og-mv-ffzy", "ts": 250},
                      snapshot=self.h.get_snapshot("og-mv-ffzy"))
        self.assertEqual(self.h.get("og-mv-ffzy")["ts"], 250)
        self.assertIsNotNone(self.h.get_snapshot("og-mv-ffzy"))

    def test_unknown_guid_not_in_history(self):
        self.assertIsNone(self.h.get("never-seen-guid"))
        self.assertIsNone(self.h.get_snapshot("never-seen-guid"))
        self.assertIsNone(self.h.latest_episode_record("never-seen-guid"))


class HistoryFromSnapshotTests(unittest.TestCase):
    """app._history_from_snapshot：快照重建 (brief, ep) 的纯函数契约。"""

    def test_rebuilds_episode(self):
        import app as appmod
        snap = {"guid": "og-ffzy", "title": TITLE, "_ep_index": 3,
                "_ep_guid": "ep-guid-3",
                "lines": {FFZY: [{"title": "第4集",
                                  "url": "https://ffzy.test/v3.m3u8"}]}}
        rec = {"item_guid": "ep-guid-3", "ts": 600, "duration": 3600,
               "snapshot": snap}
        brief, ep = appmod._history_from_snapshot(rec)
        self.assertIsNotNone(brief)
        self.assertEqual(brief["guid"], "og-ffzy")
        self.assertIsNotNone(ep)
        self.assertEqual(ep["index"], 3)
        self.assertEqual(ep["episode_number"], 4)
        self.assertEqual(ep["ep_guid"], "ep-guid-3")
        self.assertEqual(ep["online_guid"], "og-ffzy")
        self.assertEqual(ep["default_line"], FFZY)
        self.assertEqual(ep["ep_title"], "第4集")

    def test_rebuilds_movie_without_ep(self):
        import app as appmod
        rec = {"item_guid": "og-mv-ffzy", "ts": 100,
               "snapshot": {"guid": "og-mv-ffzy", "title": MOVIE,
                            "lines": {FFZY: [{"title": "正片",
                                             "url": "https://ffzy.test/v.m3u8"}]}}}
        brief, ep = appmod._history_from_snapshot(rec)
        self.assertIsNotNone(brief)
        self.assertIsNone(ep)

    def test_slim_snapshot_still_rebuilds(self):
        """过期/超限直链（lines 已丢弃、_slim）：标题+集身份+ts 仍可恢复，
        线路后续由播放链重搜补线，不因此丢续播点。"""
        import app as appmod
        rec = {"item_guid": "ep-guid-3", "ts": 600, "duration": 3600,
               "snapshot": {"guid": "og-ffzy", "title": TITLE, "_ep_index": 3,
                            "_ep_guid": "ep-guid-3", "lines": {},
                            "_slim": True}}
        brief, ep = appmod._history_from_snapshot(rec)
        self.assertIsNotNone(brief)
        self.assertIsNotNone(ep)
        self.assertEqual(ep["index"], 3)
        self.assertEqual(ep["lines"], [])

    def test_missing_snapshot_returns_none_none(self):
        import app as appmod
        self.assertEqual(appmod._history_from_snapshot({}), (None, None))
        self.assertEqual(appmod._history_from_snapshot({"snapshot": {}}),
                         (None, None))
        self.assertEqual(appmod._history_from_snapshot(
            {"snapshot": {"title": "无 guid"}}), (None, None))


# ------------------------------------------------------------------ Part B
# app 级接线测试：沿用 test_resume3_identity.py 的 ASGI harness（CMS 源）。

import app as appmod
import config as configmod
import marks as marksmod
import t4_service

ADMIN = {"Authorization": "Bearer <redacted>"}


def _upstream_handler(request):
    p = request.url.path
    if p.endswith("/play/list"):
        return httpx.Response(200, json={"code": 0, "msg": "", "data": []})
    return httpx.Response(200, json={"code": 0, "msg": "", "data": []})


def _resolve(request):
    tok = request.headers.get("authorization") or ""
    if tok == "Bearer <redacted>":
        return {"known": True, "guid": "admin-guid", "name": "admin",
                "is_admin": True, "token": tok}
    return {"known": False, "guid": "", "name": "", "is_admin": False,
            "token": ""}


class AppSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history"), (marksmod, "marks")]:
            item = patch.object(module, "_STATE",
                                Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [], "removed_ids": []})
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.history = histmod.History()
        self.marks = marksmod.Marks()
        for module, name, value in [
                (appmod, "config", self.config),
                (appmod, "store", self.store),
                (appmod, "history", self.history),
                (appmod, "marks", self.marks),
                (storemod, "store", self.store),
                (appmod, "t4_service",
                 t4_service.T4Service(self.store, self.config)),
                (appmod, "_schedule_routes_refresh", lambda: None)]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(appmod.fnusersmod, "resolve",
                            side_effect=_resolve)
        item.start()
        self.addCleanup(item.stop)
        self.network = httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_handler))
        self.addAsyncCleanup(self.network.aclose)
        appmod.app.state.ext = self.network
        appmod.app.state.up = self.network
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)
        big_url = "https://big.test/" + "b" * (70 * 1024) + ".m3u8"
        big_brief = {"guid": "og-big", "title": "超大直链剧", "year": "2024",
                     "type_name": "电视剧", "poster": "https://img.test/p.jpg",
                     "overview": "简介", "score": "8",
                     "lines": {FFZY: [{"title": "第1集", "url": big_url,
                                       "direct": True}]}}
        self.store.remember_merged([
            tv_brief("og-ffzy", TITLE, FFZY),
            tv_brief("og-dytt", TITLE, DYTT),
            movie_brief("og-mv-ffzy", MOVIE, FFZY),
            big_brief,
        ])
        self.ffzy_eps = [episode_guid("og-ffzy", "", i) for i in range(8)]
        self.big_ep = episode_guid("og-big", "", 0)

    def _evict(self, *ogs):
        """模拟 store 缓存淘汰：标题与集索引一并摘除。"""
        for og in ogs:
            self.store._titles.pop(og, None)
            if hasattr(self.store, "_orig"):
                self.store._orig.pop(og, None)
        self.store._rebuild_index()

    async def record(self, ig, ts=600, duration=3600, **extra):
        body = {"item_guid": ig, "ts": ts, "duration": duration}
        body.update(extra)
        r = await self.client.post("/v/api/v1/play/record", json=body,
                                   headers=ADMIN)
        self.assertEqual(r.json()["code"], 0, r.json())
        self.assertTrue(r.json()["data"], r.json())
        return r

    async def play_info(self, ig):
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": ig}, headers=ADMIN)
        d = r.json().get("data") or {}
        return d.get("guid"), d.get("ts"), d.get("type")

    async def test_play_record_stores_snapshot(self):
        await self.record(self.ffzy_eps[3], ts=600)
        rec = self.history.get(self.ffzy_eps[3], user="admin-guid")
        self.assertIsNotNone(rec.get("snapshot"))
        self.assertEqual(rec["snapshot"]["guid"], "og-ffzy")
        self.assertEqual(rec["snapshot"]["_ep_index"], 3)
        self.assertEqual(rec["snapshot"]["lines"][FFZY][0]["url"],
                         "https://ffzy.test/v3.m3u8")
        keys = [k for k in self.history._users.get("admin-guid", {})
                if k.startswith("canon:")]
        self.assertEqual(len(keys), 1)

    async def test_evicted_play_info_recovers_episode_ts(self):
        """store 淘汰标题后，play/info 仍凭快照回到第 4 集 600 秒。"""
        await self.record(self.ffzy_eps[3], ts=600)
        self._evict("og-ffzy", "og-dytt")
        guid, ts, typ = await self.play_info("og-ffzy")
        self.assertEqual((guid, ts, typ), (self.ffzy_eps[3], 600, "Episode"))

    async def test_evicted_play_info_recovers_movie(self):
        await self.record("og-mv-ffzy", ts=100, duration=5400)
        self._evict("og-mv-ffzy")
        guid, ts, typ = await self.play_info("og-mv-ffzy")
        self.assertEqual((guid, ts, typ), ("og-mv-ffzy", 100, "Movie"))

    async def test_evicted_play_list_shows_snapshot_item(self):
        """store 淘汰后，play/list 仍凭快照展示该电影（不直接丢弃）。"""
        await self.record("og-mv-ffzy", ts=100, duration=5400)
        self._evict("og-mv-ffzy")
        r = await self.client.get("/v/api/v1/play/list", headers=ADMIN)
        items = r.json().get("data") or []
        titles = [it.get("title") for it in items]
        self.assertIn(MOVIE, titles)

    async def test_slim_snapshot_play_info_still_recovers_ts(self):
        """过期/超限直链（快照 _slim、lines 为空）：仍恢复 ts，线路重搜补线。"""
        snap = histmod.build_snapshot(
            self.store.get_title_by_guid("og-big"), self.big_ep,
            ep={"ep_guid": self.big_ep, "index": 0})
        self.assertTrue(snap.get("_slim"))
        self.assertEqual(snap["lines"], {})
        self.history.record({"item_guid": self.big_ep, "ts": 420,
                             "duration": 3600}, snapshot=snap)
        self._evict("og-big")
        guid, ts, typ = await self.play_info("og-big")
        self.assertEqual((guid, ts, typ), (self.big_ep, 420, "Episode"))

    async def test_no_snapshot_falls_back_to_forward(self):
        """快照缺失（旧记录）：淘汰后走原有 forward，不崩、不编造续播点。"""
        self.history.record({"item_guid": self.ffzy_eps[2], "ts": 300})
        self._evict("og-ffzy", "og-dytt")
        r = await self.client.post("/v/api/v1/play/info",
                                   json={"item_guid": "og-ffzy"},
                                   headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("code"), 0)

    async def test_vod_line_card_resumes_latest_episode(self):
        """固定线路卡点进第 1 集：有第 5 集记录时续播第 5 集，而非机械回第 1 集。"""
        await self.record(self.ffzy_eps[4], ts=600)
        line_guid = appmod._line_item_guid("og-ffzy", FFZY, 0)
        guid, ts, typ = await self.play_info(line_guid)
        self.assertEqual((guid, ts, typ), (self.ffzy_eps[4], 600, "TV"))

    async def test_evicted_record_still_updates_via_history(self):
        """store 淘汰后继续上报进度：不透传上游，续播点更新、快照保留。"""
        await self.record(self.ffzy_eps[3], ts=600)
        self._evict("og-ffzy", "og-dytt")
        await self.record(self.ffzy_eps[3], ts=900)
        rec = self.history.get(self.ffzy_eps[3], user="admin-guid")
        self.assertEqual(rec["ts"], 900)
        self.assertIsNotNone(rec.get("snapshot"))


if __name__ == "__main__":
    unittest.main()
