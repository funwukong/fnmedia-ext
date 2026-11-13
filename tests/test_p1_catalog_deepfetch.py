"""P1-17：分类深翻有界并发（_extend_dense / _source_cat_window）。

对照物是改动前的串行逐页循环（在测试里原样复刻为 _serial_extend /
_serial_window）：并发版必须在顺序、内容、空页容忍、游标上与它一致，
区别只在于一批页是并发取的。get_page 用桩代替，不碰网络。
"""
import asyncio
import time

import catalog as catalogmod
from catalog import Catalog

SCOPE = catalogmod.AGG_SCOPE


def _brief(guid, title=None):
    return {"guid": guid, "title": title or guid}


def _cat_with_pages(n_pages):
    cat = Catalog()
    cat._routes[cat._scope_key(SCOPE, "movie")] = [
        {"source_id": "s1", "type_id": 1, "type_name": "电影",
         "pagecount": n_pages}]
    return cat


class _Pages:
    """get_page 桩：按页号返回固定 brief，记录并发度与调用。"""

    def __init__(self, data, fail=(), delay=0.0, fail_raise=False):
        self.data = data
        self.fail = set(fail)
        self.delay = delay
        self.fail_raise = fail_raise
        self.active = 0
        self.max_active = 0
        self.calls = []

    async def __call__(self, cat, pg, scope):
        self.calls.append(pg)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if pg in self.fail:
                if self.fail_raise:
                    raise RuntimeError("boom pg %d" % pg)
                return []
            return [dict(b) for b in self.data.get(pg, [])]
        finally:
            self.active -= 1


def _data_6_pages():
    # 第 2 页重复第 1 页的 g1-0（跨页去重）；第 5 页空；第 6 页有内容
    return {
        1: [_brief("g1-0"), _brief("g1-1")],
        2: [_brief("g1-0"), _brief("g2-1")],
        3: [_brief("g3-0"), _brief("g3-1")],
        4: [_brief("g4-0"), _brief("g4-1")],
        5: [],
        6: [_brief("g6-0"), _brief("g6-1")],
    }


async def _serial_extend(cat, scope, catname, need_count):
    """改动前的串行 _extend_dense 原样复刻，仅作对照。"""
    sk = cat._scope_key(scope, catname)
    pc = cat.pagecount(catname, scope)
    seen = cat._seen_guids.setdefault(sk, set())
    dense = cat._dense.setdefault(sk, [])
    pg = cat._scanned_page.get(sk, 0) + 1
    empty_streak = 0
    streak_limit = 10 if catname == "uhd" else 3
    while len(dense) < need_count and pg <= pc:
        rows = await cat.get_page(catname, pg, scope)
        added = 0
        for b in rows:
            gid = b.get("guid")
            if gid in seen:
                continue
            seen.add(gid); dense.append(b); added += 1
        if added:
            cat._scanned_page[sk] = pg
            pg += 1
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= streak_limit:
                cat._scanned_page[sk] = pg
                break
            pg += 1
    cat._dense_count[sk] = len(dense)
    return len(dense)


async def _serial_window(cat, scope, catname, offset, limit):
    """改动前的串行 _source_cat_window 原样复刻，仅作对照。"""
    out, seen, pg, empty_streak = [], set(), 1, 0
    while len(out) < offset + limit and pg <= catalogmod.MAX_PAGES \
            and empty_streak < 3:
        rows = await cat.get_page(catname, pg, scope)
        added = 0
        for b in rows:
            g = b.get("guid")
            if g and g not in seen:
                seen.add(g); out.append(b); added += 1
        if added:
            empty_streak = 0
        else:
            empty_streak += 1
        pg += 1
    return out[offset:offset + limit]


def _guids(briefs):
    return [b["guid"] for b in briefs]


def test_extend_dense_matches_serial_full():
    data = _data_6_pages()
    a, b = _cat_with_pages(6), _cat_with_pages(6)
    a.get_page = _Pages(data)
    b.get_page = _Pages(data)
    asyncio.run(a._extend_dense(SCOPE, "movie", 100))
    asyncio.run(_serial_extend(b, SCOPE, "movie", 100))
    sk = a._scope_key(SCOPE, "movie")
    assert _guids(a._dense[sk]) == _guids(b._dense[sk])
    assert _guids(a._dense[sk]) == ["g1-0", "g1-1", "g2-1", "g3-0", "g3-1",
                                    "g4-0", "g4-1", "g6-0", "g6-1"]
    assert a._scanned_page[sk] == b._scanned_page[sk] == 6


def test_extend_dense_matches_serial_need_cutoff():
    # need=5 时两边都应在第 3 页停下，只取前 5 条
    data = _data_6_pages()
    a, b = _cat_with_pages(6), _cat_with_pages(6)
    a.get_page = _Pages(data)
    b.get_page = _Pages(data)
    asyncio.run(a._extend_dense(SCOPE, "movie", 5))
    asyncio.run(_serial_extend(b, SCOPE, "movie", 5))
    sk = a._scope_key(SCOPE, "movie")
    assert _guids(a._dense[sk]) == _guids(b._dense[sk])
    assert _guids(a._dense[sk]) == ["g1-0", "g1-1", "g2-1", "g3-0", "g3-1"]
    assert a._scanned_page[sk] == b._scanned_page[sk] == 3


def test_extend_dense_empty_streak_stops_both():
    # 第 2/3/4 页连续空：第 4 页空页到限即停，第 5 页有内容也不取
    data = {1: [_brief("g1-0")], 5: [_brief("g5-0")]}
    a, b = _cat_with_pages(6), _cat_with_pages(6)
    a.get_page = _Pages(data)
    b.get_page = _Pages(data)
    asyncio.run(a._extend_dense(SCOPE, "movie", 100))
    asyncio.run(_serial_extend(b, SCOPE, "movie", 100))
    sk = a._scope_key(SCOPE, "movie")
    assert _guids(a._dense[sk]) == _guids(b._dense[sk]) == ["g1-0"]
    assert a._scanned_page[sk] == b._scanned_page[sk] == 4


def test_source_cat_window_matches_serial():
    data = _data_6_pages()
    a, b = _cat_with_pages(6), _cat_with_pages(6)
    a.get_page = _Pages(data)
    b.get_page = _Pages(data)
    got = asyncio.run(a._source_cat_window(SCOPE, "movie", 0, 100))
    want = asyncio.run(_serial_window(b, SCOPE, "movie", 0, 100))
    assert _guids(got) == _guids(want)
    assert _guids(got) == ["g1-0", "g1-1", "g2-1", "g3-0", "g3-1",
                           "g4-0", "g4-1", "g6-0", "g6-1"]
    # 带 offset/limit 的窗口切片也一致
    a2, b2 = _cat_with_pages(6), _cat_with_pages(6)
    a2.get_page = _Pages(data)
    b2.get_page = _Pages(data)
    got2 = asyncio.run(a2._source_cat_window(SCOPE, "movie", 3, 4))
    want2 = asyncio.run(_serial_window(b2, SCOPE, "movie", 3, 4))
    assert _guids(got2) == _guids(want2) == ["g3-0", "g3-1", "g4-0", "g4-1"]


def test_partial_page_failure_keeps_other_pages():
    # 第 2 页抛错、第 4 页返空：其余页照常返回、顺序不变
    data = _data_6_pages()
    cat = _cat_with_pages(6)
    stub = _Pages(data, fail={2, 4}, fail_raise=False)
    cat.get_page = stub
    asyncio.run(cat._extend_dense(SCOPE, "movie", 100))
    sk = cat._scope_key(SCOPE, "movie")
    assert _guids(cat._dense[sk]) == ["g1-0", "g1-1", "g3-0", "g3-1",
                                      "g6-0", "g6-1"]
    # 抛错也一样：不向外抛、不丢其它页
    cat2 = _cat_with_pages(6)
    stub2 = _Pages(data, fail={2}, fail_raise=True)
    cat2.get_page = stub2
    got = asyncio.run(cat2._source_cat_window(SCOPE, "movie", 0, 100))
    assert _guids(got) == ["g1-0", "g1-1", "g3-0", "g3-1",
                           "g4-0", "g4-1", "g6-0", "g6-1"]


def test_concurrency_bounded_and_parallel():
    # 8 页 × 0.05s：串行要 0.40s；并发 4 应约 0.10s，且峰值并发正好 4
    data = {p: [_brief("g%d-0" % p), _brief("g%d-1" % p)]
            for p in range(1, 9)}
    cat = _cat_with_pages(8)
    stub = _Pages(data, delay=0.05)
    cat.get_page = stub
    t0 = time.monotonic()
    asyncio.run(cat._extend_dense(SCOPE, "movie", 100))
    elapsed = time.monotonic() - t0
    assert stub.max_active == catalogmod.DEEP_FETCH_CONCURRENCY
    assert stub.max_active > 1
    assert sorted(stub.calls) == list(range(1, 9))
    assert elapsed < 0.30, "elapsed %.3fs, not parallel" % elapsed
    sk = cat._scope_key(SCOPE, "movie")
    assert _guids(cat._dense[sk]) == [
        "g%d-%d" % (p, i) for p in range(1, 9) for i in (0, 1)]
