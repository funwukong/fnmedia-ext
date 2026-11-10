"""P1-21：fnusers.resolve 锁外查库、锁内只换缓存（双重检查）。

_lookup 用桩代替，不碰真实 SQLite；TTL 语义用缓存时间戳直接验证。
"""
import threading
import time
from types import SimpleNamespace

import pytest

import fnusers as fnusersmod


@pytest.fixture(autouse=True)
def _clean_cache():
    fnusersmod._cache.clear()
    yield
    fnusersmod._cache.clear()


def _req(token):
    return SimpleNamespace(headers={"authorization": token})


def _user(guid):
    return {"guid": guid, "name": guid, "is_admin": False}


def test_cached_token_never_touches_db(monkeypatch):
    calls = []
    monkeypatch.setattr(fnusersmod, "_lookup",
                        lambda tok: calls.append(tok) or _user("u-" + tok))
    assert fnusersmod.resolve(_req("tok-a"))["guid"] == "u-tok-a"
    assert calls == ["tok-a"]
    # 第二次走缓存快路径：_lookup 不再被调，哪怕它现在会抛错
    monkeypatch.setattr(fnusersmod, "_lookup",
                        lambda tok: (_ for _ in ()).throw(RuntimeError("db")))
    assert fnusersmod.resolve(_req("tok-a"))["guid"] == "u-tok-a"


def test_concurrent_distinct_tokens_not_serialized(monkeypatch):
    calls = []
    lock = threading.Lock()

    def slow_lookup(tok):
        time.sleep(0.3)
        with lock:
            calls.append(tok)
        return _user("u-" + tok)

    monkeypatch.setattr(fnusersmod, "_lookup", slow_lookup)
    results = {}
    barrier = threading.Barrier(8)

    def work(i):
        barrier.wait(timeout=5)
        results[i] = fnusersmod.resolve(_req("tok-%d" % i))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.monotonic() - t0
    assert all(v["known"] and v["guid"] == "u-tok-%d" % i
               for i, v in results.items())
    assert sorted(calls) == ["tok-%d" % i for i in range(8)]
    # 8 × 0.3s 串行要 2.4s；锁外查库应约 0.3s
    assert elapsed < 1.5, "elapsed %.3fs, lookups still serialized" % elapsed


def test_cached_resolve_not_blocked_by_inflight_lookup(monkeypatch):
    # 已缓存的 token 在另一个 token 的慢查库进行时必须立刻返回：
    # 这正是“持锁做 SQLite I/O”的反模式——缓存快路径不再等锁。
    fnusersmod._cache["tok-cached"] = (time.time(), _user("u-cached"))
    entered = threading.Event()
    release = threading.Event()

    def slow_lookup(tok):
        entered.set()
        assert release.wait(timeout=5)
        return _user("u-slow")

    monkeypatch.setattr(fnusersmod, "_lookup", slow_lookup)
    done = {}
    t = threading.Thread(
        target=lambda: done.setdefault("slow",
                                       fnusersmod.resolve(_req("tok-slow"))))
    t.start()
    assert entered.wait(timeout=5), "slow lookup never started"
    t0 = time.monotonic()
    assert fnusersmod.resolve(_req("tok-cached"))["guid"] == "u-cached"
    assert time.monotonic() - t0 < 0.5
    release.set()
    t.join(timeout=5)
    assert done["slow"]["guid"] == "u-slow"


def test_ttl_semantics_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(fnusersmod, "_lookup",
                        lambda tok: calls.append(tok) or _user("u-new"))
    now = time.time()
    # 新鲜正缓存（30s 内）：不查库
    fnusersmod._cache["tok-p"] = (now, _user("u-old"))
    assert fnusersmod.resolve(_req("tok-p"))["guid"] == "u-old"
    assert calls == []
    # 过期正缓存（>30s）：重新查库
    fnusersmod._cache["tok-p"] = (now - 31, _user("u-old"))
    assert fnusersmod.resolve(_req("tok-p"))["guid"] == "u-new"
    assert calls == ["tok-p"]
    # 新鲜负缓存（10s 内）：不查库，仍未知
    fnusersmod._cache["tok-n"] = (now, None)
    assert fnusersmod.resolve(_req("tok-n"))["known"] is False
    assert calls == ["tok-p"]
    # 过期负缓存（>10s）：重新查库
    fnusersmod._cache["tok-n"] = (now - 11, None)
    assert fnusersmod.resolve(_req("tok-n"))["guid"] == "u-new"
    assert calls == ["tok-p", "tok-n"]


def test_lookup_failure_cached_negative(monkeypatch):
    calls = []
    monkeypatch.setattr(fnusersmod, "_lookup",
                        lambda tok: calls.append(tok) or None)
    assert fnusersmod.resolve(_req("tok-nope"))["known"] is False
    assert calls == ["tok-nope"]
    # 负缓存期内不再查库
    assert fnusersmod.resolve(_req("tok-nope"))["known"] is False
    assert calls == ["tok-nope"]


def test_double_check_does_not_overwrite_fresher_cache(monkeypatch):
    # A 查库中途，B 已写入更新鲜的缓存：A 写前再查一次，不得覆盖
    entered = threading.Event()
    release = threading.Event()

    def slow_lookup(tok):
        entered.set()
        assert release.wait(timeout=5)
        return _user("u-stale")

    monkeypatch.setattr(fnusersmod, "_lookup", slow_lookup)
    done = {}
    t = threading.Thread(
        target=lambda: done.setdefault("r",
                                       fnusersmod.resolve(_req("tok-x"))))
    t.start()
    assert entered.wait(timeout=5)
    fnusersmod._cache["tok-x"] = (time.time(), _user("u-fresh"))
    release.set()
    t.join(timeout=5)
    assert done["r"]["guid"] == "u-fresh"
    assert fnusersmod._cache["tok-x"][1]["guid"] == "u-fresh"
