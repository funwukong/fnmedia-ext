"""移植验证：上游 v0.2.48 历史按账号隔离 + v0.2.52 titles 防抖落盘。

上游无对应测试文件（tests/ 里只有零星引用），故按上游行为现写。
"""
import json
from contextlib import contextmanager
from unittest.mock import patch

import history as histmod
import store as storemod
from history import History


@contextmanager
def as_user(guid, admin=False):
    tokens = histmod.set_current_user(guid, admin)
    try:
        yield
    finally:
        histmod.reset_current_user(tokens)


def _brief(guid, title="T"):
    return {"guid": guid, "title": title,
            "lines": {"A": [{"url": "http://x/%s.mp4" % guid,
                             "direct": True, "title": "第1集"}]},
            "locator": {"kind": "cms"}}


def _history(tmp_path, monkeypatch):
    monkeypatch.setattr(histmod, "_STATE", tmp_path / "history.json")
    return History()


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(storemod, "_STATE", tmp_path / "titles.json")
    s = storemod.Store()
    return s


# ---- 任务 8：历史按账号隔离 ----

def test_history_partitions_isolated(tmp_path, monkeypatch):
    h = _history(tmp_path, monkeypatch)
    with as_user("u1"):
        assert h.record({"item_guid": "ep1", "ts": 100, "duration": 3600})
    with as_user("u2"):
        assert h.record({"item_guid": "ep1", "ts": 200, "duration": 3600})
    with as_user("u1"):
        assert h.get("ep1")["ts"] == 100
        assert [r["item_guid"] for r in h.all()] == ["ep1"]
    with as_user("u2"):
        assert h.get("ep1")["ts"] == 200
    # 未识别身份走全局分区，看不到任何账号的记录
    assert h.get("ep1") is None
    assert h.all() == []
    # u1 删除不影响 u2
    with as_user("u1"):
        assert h.delete("ep1") == 1
    with as_user("u2"):
        assert h.get("ep1")["ts"] == 200


def test_history_legacy_flat_migrates_to_global(tmp_path, monkeypatch):
    p = tmp_path / "history.json"
    p.write_text(json.dumps({"records": {"ep9": {
        "item_guid": "ep9", "ts": 55, "duration": 3600,
        "updated_at": 1000}}}), encoding="utf-8")
    h = _history(tmp_path, monkeypatch)
    assert h.get("ep9")["ts"] == 55
    assert h.get("ep9", user="u1") is None


def test_history_admin_adopts_global_once(tmp_path, monkeypatch):
    p = tmp_path / "history.json"
    p.write_text(json.dumps({"records": {"ep9": {
        "item_guid": "ep9", "ts": 55, "duration": 3600,
        "updated_at": 1000}}}), encoding="utf-8")
    h = _history(tmp_path, monkeypatch)
    with as_user("admin", admin=True):
        assert h.get("ep9")["ts"] == 55
    h.flush()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["adopted"] == ["admin"]
    assert "ep9" in data["users"]["admin"]
    # 重启后仍保留，且子账号看不到
    h2 = _history(tmp_path, monkeypatch)
    with as_user("kid"):
        assert h2.get("ep9") is None
    with as_user("admin", admin=True):
        assert h2.get("ep9")["ts"] == 55


def test_history_non_admin_does_not_adopt(tmp_path, monkeypatch):
    p = tmp_path / "history.json"
    p.write_text(json.dumps({"records": {"ep9": {
        "item_guid": "ep9", "ts": 55, "duration": 3600,
        "updated_at": 1000}}}), encoding="utf-8")
    h = _history(tmp_path, monkeypatch)
    with as_user("kid"):
        assert h.get("ep9") is None
    h.flush()
    # 未产生脏数据时上游不落盘：文件保持旧扁平格式，无 adopted 标记
    assert (json.loads(p.read_text(encoding="utf-8")).get("adopted")
            or []) == []


def test_history_continue_watching_partitioned(tmp_path, monkeypatch):
    h = _history(tmp_path, monkeypatch)
    with as_user("u1"):
        h.set_position("ep1", ts=600, duration=3600)
    with as_user("u2"):
        h.set_position("ep2", ts=600, duration=3600)
    with as_user("u1"):
        guids = [r["item_guid"] for r in h.continue_watching()]
        assert guids == ["ep1"]
    with as_user("u2"):
        guids = [r["item_guid"] for r in h.continue_watching()]
        assert guids == ["ep2"]


def test_resume_episode_order_default_latest_interaction_opt_in(
        tmp_path, monkeypatch):
    h = _history(tmp_path, monkeypatch)
    # ep2 先看（updated=100），ep1 后回看（updated=200）
    with patch("history.time.time", return_value=100):
        h.record({"item_guid": "ep2", "ts": 300, "duration": 3600})
    with patch("history.time.time", return_value=200):
        h.record({"item_guid": "ep1", "ts": 120, "duration": 3600})
    # 默认 CMS 语义：按集序续播靠后的未看完集
    assert h.resume_episode(["ep1", "ep2"]) == ("ep2", 300)
    # T4 回看语义：按最近一次交互
    assert h.resume_episode(["ep1", "ep2"],
                            latest_interaction=True) == ("ep1", 120)


# ---- 任务 9：titles 防抖落盘 + 双上限 ----

def test_store_debounce_no_sync_write_then_flush(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(storemod, "_cache_cfg",
                        lambda: (4000, 4 * 1024 * 1024, 60.0))
    try:
        s.remember_merged([_brief("g1")])
        # 防抖期内不落盘：搜索热路径零磁盘 IO
        assert not (tmp_path / "titles.json").exists()
        assert s._writer_started is True
        # flush(wait=True) 同步写完
        assert s.flush() is True
        data = json.loads((tmp_path / "titles.json").read_text(
            encoding="utf-8"))
        assert "g1" in data["titles"]
    finally:
        s.close()


def test_store_close_flushes_pending(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(storemod, "_cache_cfg",
                        lambda: (4000, 4 * 1024 * 1024, 60.0))
    s.remember_merged([_brief("g1")])
    assert not (tmp_path / "titles.json").exists()
    s.close()
    data = json.loads((tmp_path / "titles.json").read_text(
        encoding="utf-8"))
    assert "g1" in data["titles"]


def test_store_snapshot_capped_by_entries(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(storemod, "_cache_cfg", lambda: (2, 0, 0))
    try:
        s.remember_merged([_brief("g1"), _brief("g2"), _brief("g3")])
        assert s.flush() is True
        data = json.loads((tmp_path / "titles.json").read_text(
            encoding="utf-8"))
        # 条数上限只裁剪落盘快照：只留最新的 2 条
        assert sorted(data["titles"]) == ["g2", "g3"]
        # 内存不受快照裁剪影响
        assert len(s._titles) == 3
    finally:
        s.close()


def test_store_snapshot_capped_by_bytes(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    briefs = [_brief("g1"), _brief("g2"), _brief("g3")]
    # 按上游公式自算预算：只够留最新一条（+256 常量开销与 0.92 余量）
    sizes = [len(json.dumps(b, ensure_ascii=False).encode("utf-8")) + 2
             for b in briefs]
    budget = sizes[2] + 256 + 10
    # 注意：_cache_cfg 返回的第二项已是字节数（MB 已在函数内换算）
    max_bytes = budget / 0.92
    monkeypatch.setattr(storemod, "_cache_cfg",
                        lambda: (4000, max_bytes, 0))
    try:
        s.remember_merged(briefs)
        titles, _ = s._snapshot_capped()
        # 字节上限从最旧丢起，至少丢掉 g1，且总是最新的留下来
        assert "g1" not in titles
        assert "g3" in titles
        assert len(titles) < 3
    finally:
        s.close()


def test_store_memory_prune_keeps_guards(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(storemod, "_cache_cfg", lambda: (4, 0, 0))
    try:
        s.remember_merged([_brief("g%d" % i) for i in range(6)])
        assert len(s._titles) == 6
        s.remember_merged([_brief("g6"), _brief("g7")])
        # 内存软上限 1.5x=6：超限后丢最旧的非保护条目，留 4 条
        assert len(s._titles) == 4
        assert "g6" in s._titles and "g7" in s._titles
        assert "g0" not in s._titles
    finally:
        s.close()
