"""Memory-store 修复：增量索引 / 载入裁剪与形状校验 / detail 缓存淘汰 /
落盘锁外算字节 / titles.json 瘦身（新旧形态往返）。

先写测试复现，再改代码：以下测试在旧实现上应失败（或暴露问题），
在修复后全部通过。
"""
import json
import threading
import time

import providers
import store as storemod

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _ep(i, url="http://cdn.example.com/vod", headers=None):
    return {"title": "第%d集" % (i + 1), "url": "%s/ep%d.m3u8" % (url, i + 1),
            "direct": True, "resolve": False,
            "headers": headers if headers is not None else
            {"Referer": url + "/", "User-Agent": UA},
            "redirect": False, "xsrc": False, "play_from": "",
            "t4": False, "episode_key": "", "catvod": False,
            "drpy": False, "guard": False}


def _brief(guid, n_eps=3, title="T"):
    return {"guid": guid, "online_id": "cms:%s" % guid, "title": title,
            "type_name": "", "year": "", "area": "", "updated": "",
            "remarks": "", "score": "", "poster": "", "overview": "",
            "actor": "", "director": "", "duration": 0,
            "lines": {"A": [_ep(i) for i in range(n_eps)]},
            "locator": {"kind": "cms"}}


def _title(vod_id, n_eps=2, locator=None):
    return providers.OnlineTitle(
        provider="cms", vod_id=vod_id, title="T-%s" % vod_id,
        type_name="", year="", area="", updated="", remarks="",
        score="", poster="", overview="", actor="", director="",
        sources={"A": [providers.Episode(title="第%d集" % (i + 1),
                        url="http://cdn.example.com/vod/ep%d.m3u8" % (i + 1),
                        direct=True,
                        headers={"Referer": "http://cdn.example.com/vod/",
                                 "User-Agent": UA})
                 for i in range(n_eps)]},
        locator=locator or {"kind": "cms"})


def _store(tmp_path, monkeypatch, cfg=(4000, 4 * 1024 * 1024, 60.0)):
    monkeypatch.setattr(storemod, "_STATE", tmp_path / "titles.json")
    monkeypatch.setattr(storemod, "_cache_cfg", lambda: cfg)
    return storemod.Store()


def _write_state(tmp_path, obj):
    (tmp_path / "titles.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ---- 1. 增量索引 ----

def test_remember_titles_is_incremental(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g%d" % i) for i in range(50)])
        calls = []
        monkeypatch.setattr(s, "_rebuild_index",
                            lambda: calls.append(1))
        t = _title("new1")
        s.remember_titles([t])
        assert calls == [], "remember_titles must not full-rebuild the index"
        g = storemod.make_guid(t)
        assert s.resolve_media_guid(storemod.media_guid(g)) is not None
        assert s.resolve_media_guid(storemod.media_guid("g0")) is not None
        assert s.get_episode_target(storemod.episode_guid(g, "", 0)) is not None
        # 覆盖写入同一 guid：旧线路索引摘除，不留死链
        t2 = _title("new1")
        t2.sources = {"B": [providers.Episode(
            title="第1集", url="http://other.example.com/x.mp4", direct=True)]}
        s.remember_titles([t2])
        assert calls == []
        tgt = s.get_episode_target(storemod.episode_guid(g, "", 0))
        assert tgt["url"] == "http://other.example.com/x.mp4"
        assert tgt["line"] == "B"
    finally:
        s.close()


def test_remember_detail_is_incremental(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g%d" % i) for i in range(50)])
        calls = []
        monkeypatch.setattr(s, "_rebuild_index",
                            lambda: calls.append(1))
        g = s.remember_detail(_title("d1"))
        assert calls == []
        assert s.resolve_media_guid(storemod.media_guid(g)) is not None
        assert s.resolve_media_guid(storemod.media_guid("g0")) is not None
        assert s.get_detail("cms:d1") is not None
    finally:
        s.close()


def test_replace_t4_detail_is_incremental(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g%d" % i) for i in range(20)])
        calls = []
        monkeypatch.setattr(s, "_rebuild_index",
                            lambda: calls.append(1))
        t = providers.OnlineTitle(
            provider="cms", vod_id="t4-1", title="T4剧", type_name="",
            year="", area="", updated="", remarks="", score="", poster="",
            overview="", actor="", director="",
            sources={"线路": [providers.Episode(
                title="S01E01", url="http://t4.example.com/e1.m3u8",
                direct=True, t4=True, episode_key="S01E01")]},
            locator={"kind": "t4"})
        g = storemod.make_guid(t)
        s.replace_t4_detail(g, t)
        assert calls == []
        assert s.resolve_media_guid(storemod.media_guid(g)) is not None
        assert s.resolve_media_guid(storemod.media_guid("g0")) is not None
        assert s.episode_count(g) == 1
    finally:
        s.close()


def test_prune_drops_stale_index_entries(tmp_path, monkeypatch):
    # 内存软上限淘汰的标题，其集/media 索引必须同步摘除，否则
    # resolve 会返回已淘汰标题的死链（旧 remember_merged 只清 _titles）。
    s = _store(tmp_path, monkeypatch, cfg=(4, 0, 0))
    try:
        s.remember_merged([_brief("g%d" % i) for i in range(6)])
        assert len(s._titles) == 6
        s.remember_merged([_brief("g6"), _brief("g7")])
        assert len(s._titles) == 4
        assert "g0" not in s._titles
        assert s.resolve_media_guid(storemod.media_guid("g0")) is None
        assert s.get_episode_target(storemod.episode_guid("g0", "", 0)) is None
        assert s.resolve_media_guid(storemod.media_guid("g7")) is not None
    finally:
        s.close()


# ---- 2. 载入裁剪与形状校验 ----

def test_load_applies_entry_cap(tmp_path, monkeypatch):
    _write_state(tmp_path, {
        "titles": {("g%03d" % i): _brief("g%03d" % i) for i in range(200)},
        "orig": {("g%03d" % i): "cms:g%03d" % i for i in range(200)}})
    s = _store(tmp_path, monkeypatch, cfg=(10, 0, 0))
    try:
        assert len(s._titles) == 10
        # 只留最新的 10 条
        assert "g199" in s._titles and "g190" in s._titles
        assert "g000" not in s._titles
        assert s.resolve_media_guid(storemod.media_guid("g199")) is not None
        assert s.resolve_media_guid(storemod.media_guid("g000")) is None
    finally:
        s.close()


def test_load_applies_byte_cap(tmp_path, monkeypatch):
    briefs = {("g%d" % i): _brief("g%d" % i) for i in range(5)}
    _write_state(tmp_path, {
        "titles": briefs,
        "orig": {("g%d" % i): "cms:g%d" % i for i in range(5)}})
    # 预算只够留最新 1 条（+256 常量开销与 0.92 余量）；配置必须在
    # Store() 构造（即 _load）之前就打好补丁，否则载入用的还是旧配置。
    one = (len(json.dumps(storemod._compact_brief(briefs["g4"]),
                          ensure_ascii=False).encode("utf-8"))
           + len(json.dumps("cms:g4", ensure_ascii=False).encode("utf-8")))
    s = _store(tmp_path, monkeypatch,
               cfg=(4000, (one + 256 + 10) / 0.92, 0))
    try:
        assert "g4" in s._titles
        assert "g0" not in s._titles
        assert len(s._titles) == 1
    finally:
        s.close()


def test_load_malformed_shape_does_not_crash(tmp_path, monkeypatch):
    _write_state(tmp_path, {"titles": [1, 2, 3], "orig": {}})
    s = _store(tmp_path, monkeypatch)
    try:
        assert s._titles == {} and s._orig == {}
    finally:
        s.close()
    _write_state(tmp_path, {"titles": {"g1": 1, "g2": _brief("g2")},
                            "orig": "not-a-dict"})
    s = _store(tmp_path, monkeypatch)
    try:
        assert "g1" not in s._titles  # 非 dict 的条目丢弃
        assert "g2" in s._titles
        assert s._orig == {}  # 非 dict 的 orig 丢弃，不崩溃
        assert s.resolve_media_guid(storemod.media_guid("g2")) is not None
    finally:
        s.close()
    _write_state(tmp_path, {"titles": {"g1": _brief("g1")}, "orig": {"g1": 5}})
    s = _store(tmp_path, monkeypatch)
    try:
        assert "g1" in s._titles
        assert s._orig.get("g1", "") == ""  # 非 str 的 orig 值丢弃
    finally:
        s.close()


def test_load_large_file_does_not_hold_everything(tmp_path, monkeypatch):
    # 超限大文件（>1MB）：流式载入，只保留上限内的最新条目。
    # 用小上限验证“不再全量进内存”的行为：800 条写入、只留 10 条。
    n = 800
    titles = {("g%04d" % i): _brief("g%04d" % i) for i in range(n)}
    _write_state(tmp_path, {
        "titles": titles,
        "orig": {("g%04d" % i): "cms:g%04d" % i for i in range(n)}})
    assert (tmp_path / "titles.json").stat().st_size > 1024 * 1024
    s = _store(tmp_path, monkeypatch, cfg=(10, 0, 0))
    try:
        assert len(s._titles) == 10
        assert "g0799" in s._titles and "g0000" not in s._titles
    finally:
        s.close()


# ---- 3. _detail_cache 淘汰 ----

def test_detail_cache_evicted_with_prune(tmp_path, monkeypatch):
    # max_n=4 → 内存软上限 6 条；_detail_cache 与标题同步淘汰，
    # 不再 1:1 无界增长，且缓存键始终是仍在库的 online_id。
    s = _store(tmp_path, monkeypatch, cfg=(4, 0, 0))
    try:
        for i in range(10):
            s.remember_detail(_title("v%d" % i))
        assert len(s._titles) <= 6
        assert len(s._detail_cache) == len(s._titles)
        assert set(s._detail_cache) <= set(s._orig.values())
        assert s.get_detail("cms:v0") is None  # 最旧的已被淘汰
        assert s.get_detail("cms:v9") is not None
    finally:
        s.close()


# ---- 4. 落盘算字节移出持锁区 ----

def test_snapshot_capped_releases_lock(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g%d" % i) for i in range(50)])
        assert s._dirty is True
        entered = threading.Event()
        released = threading.Event()
        orig_dumps = json.dumps
        gate = {"done": False}

        def slow_dumps(*a, **k):
            # 只在第一次 dumps 时卡住：既证明持锁/放锁，又不让后续
            # 每次 dumps 都等满超时（旧实现每条目都要 dumps 一次）。
            if not gate["done"]:
                gate["done"] = True
                entered.set()
                released.wait(timeout=5)
            return orig_dumps(*a, **k)

        monkeypatch.setattr(storemod.json, "dumps", slow_dumps)
        # 真实落盘路径：_flush_now 旧实现全程持 _lock 调 _snapshot_capped
        th = threading.Thread(target=lambda: s._flush_now())
        th.start()
        assert entered.wait(timeout=5), "flush did not reach json.dumps"
        # 快照算字节期间，另一线程应能拿到 _lock（不在持锁区内算字节）
        assert s._lock.acquire(timeout=2), "lock still held during size computation"
        try:
            released.set()
        finally:
            s._lock.release()
        th.join(timeout=5)
        assert not th.is_alive()
    finally:
        s.close()


# ---- 5. titles.json 瘦身：新旧形态往返 ----

def test_compact_roundtrip_old_file(tmp_path, monkeypatch):
    old_brief = _brief("g1", n_eps=4)
    _write_state(tmp_path, {"titles": {"g1": old_brief},
                            "orig": {"g1": "cms:g1"}})
    s = _store(tmp_path, monkeypatch)
    try:
        # 旧文件读入后内存仍是完整形态（逐集 headers、布尔字段齐全）
        assert s._titles["g1"] == old_brief
        assert s.resolve_media_guid(storemod.media_guid("g1")) is not None
        # 仅载入不记脏：强制记脏一次才落盘（旧文件按需迁移，不主动重写）
        with s._lock:
            s._dirty = True
        assert s.flush() is True
        raw = (tmp_path / "titles.json").read_text(encoding="utf-8")
        new_size = len(raw.encode("utf-8"))
        old_size = len(json.dumps({"titles": {"g1": old_brief},
                                   "orig": {"g1": "cms:g1"}},
                                  ensure_ascii=False).encode("utf-8"))
        assert new_size < old_size, (new_size, old_size)
        # 落盘形态：线路级一份 headers + eps，且无恒 False 布尔字段
        data = json.loads(raw)
        line = data["titles"]["g1"]["lines"]["A"]
        assert isinstance(line, dict) and "eps" in line
        assert line["headers"] == {"Referer": "http://cdn.example.com/vod/",
                                   "User-Agent": UA}
        assert all("headers" not in e for e in line["eps"])
        assert all("resolve" not in e and "redirect" not in e
                   and "guard" not in e for e in line["eps"])
        assert all(e["direct"] is True for e in line["eps"])
    finally:
        s.close()
    # 新文件再读入：内存形态与旧文件读入时完全一致
    s2 = _store(tmp_path, monkeypatch)
    try:
        assert s2._titles["g1"] == old_brief
        assert s2.resolve_media_guid(storemod.media_guid("g1")) is not None
    finally:
        s2.close()


def test_compact_new_shape_read(tmp_path, monkeypatch):
    compact = {"guid": "g9", "online_id": "cms:g9", "title": "T",
               "type_name": "", "year": "", "area": "", "updated": "",
               "remarks": "", "score": "", "poster": "", "overview": "",
               "actor": "", "director": "", "duration": 0,
               "lines": {"A": {"headers": {"Referer": "http://r/",
                                            "User-Agent": UA},
                               "eps": [{"title": "第1集",
                                        "url": "http://r/ep1.m3u8",
                                        "direct": True},
                                       {"title": "第2集",
                                        "url": "http://r/ep2.m3u8",
                                        "direct": True}]}},
               "locator": {"kind": "cms"}}
    _write_state(tmp_path, {"titles": {"g9": compact},
                            "orig": {"g9": "cms:g9"}})
    s = _store(tmp_path, monkeypatch)
    try:
        b = s._titles["g9"]
        assert b["lines"]["A"][0]["headers"] == {"Referer": "http://r/",
                                                 "User-Agent": UA}
        assert b["lines"]["A"][1]["headers"] == {"Referer": "http://r/",
                                                 "User-Agent": UA}
        assert b["lines"]["A"][0]["resolve"] is False
        assert b["lines"]["A"][0]["guard"] is False
        tgt = s.get_episode_target(storemod.episode_guid("g9", "", 1))
        assert tgt["url"] == "http://r/ep2.m3u8"
        assert tgt["headers"]["Referer"] == "http://r/"
    finally:
        s.close()


def test_compact_keeps_divergent_headers(tmp_path, monkeypatch):
    brief = _brief("g1", n_eps=2)
    brief["lines"]["A"][1]["headers"] = {"Referer": "http://other/",
                                         "User-Agent": UA}
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([brief])
        assert s.flush() is True
        data = json.loads((tmp_path / "titles.json").read_text(
            encoding="utf-8"))
        line = data["titles"]["g1"]["lines"]["A"]
        # 各集 headers 不同时不得提为线路级：逐集保留
        assert isinstance(line, list)
        assert line[0]["headers"]["Referer"] == "http://cdn.example.com/vod/"
        assert line[1]["headers"]["Referer"] == "http://other/"
    finally:
        s.close()
    s2 = _store(tmp_path, monkeypatch)
    try:
        assert s2._titles["g1"] == brief
    finally:
        s2.close()


def test_compact_helpers_direct():
    b = _brief("g1", n_eps=2)
    c = storemod._compact_brief(b)
    assert isinstance(c["lines"]["A"], dict)
    assert storemod._expand_brief(c) == b
    assert storemod._expand_brief(b) == b  # 旧形态原样通过
    divergent = _brief("g1", n_eps=2)
    divergent["lines"]["A"][1]["headers"] = {"Referer": "http://other/"}
    assert isinstance(storemod._compact_brief(divergent)["lines"]["A"], list)
    assert storemod._expand_brief(
        storemod._compact_brief(divergent)) == divergent
