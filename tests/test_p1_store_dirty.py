"""P1-18：标题存储写盘脏跟踪（无变化不写盘）。

防抖 60s、下双上限沿用既有配置；落盘与否用 _dirty 标志 + 文件 mtime
判定，不靠后台线程时序。
"""
import json
import os

import providers
import store as storemod


def _brief(guid, title="T"):
    return {"guid": guid, "title": title,
            "lines": {"A": [{"url": "http://x/%s.mp4" % guid,
                             "direct": True, "title": "第1集"}]},
            "locator": {"kind": "cms"}}


def _title(vod_id, title="T"):
    return providers.OnlineTitle(
        provider="t", vod_id=vod_id, title=title,
        type_name="", year="", area="", updated="", remarks="",
        score="", poster="", overview="", actor="", director="",
        sources={})


def _store(tmp_path, monkeypatch, cfg=(4000, 4 * 1024 * 1024, 60.0)):
    monkeypatch.setattr(storemod, "_STATE", tmp_path / "titles.json")
    monkeypatch.setattr(storemod, "_cache_cfg", lambda: cfg)
    return storemod.Store()


def _read(tmp_path):
    return json.loads((tmp_path / "titles.json").read_text(encoding="utf-8"))


def _mtime_ns(tmp_path):
    return os.stat(tmp_path / "titles.json").st_mtime_ns


def test_identical_remember_merged_does_not_dirty(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g1")])
        assert s._dirty is True
        assert s.flush() is True
        mtime = _mtime_ns(tmp_path)
        seq = s._dirty_seq
        # 内容完全相同的重复写入：不记脏、不排队、不碰磁盘
        s.remember_merged([_brief("g1")])
        assert s._dirty is False
        assert s._dirty_seq == seq
        assert s.flush() is True
        assert _mtime_ns(tmp_path) == mtime
        assert _read(tmp_path)["titles"]["g1"]["title"] == "T"
    finally:
        s.close()


def test_identical_remember_titles_does_not_dirty(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_titles([_title("v1")])
        assert s.flush() is True
        mtime = _mtime_ns(tmp_path)
        seq = s._dirty_seq
        s.remember_titles([_title("v1")])
        assert s._dirty is False
        assert s._dirty_seq == seq
        assert s.flush() is True
        assert _mtime_ns(tmp_path) == mtime
    finally:
        s.close()


def test_changed_remember_still_persists(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g1")])
        assert s.flush() is True
        # 同 guid、标题变了：记脏并落盘
        s.remember_merged([_brief("g1", title="T2")])
        assert s._dirty is True
        assert s.flush() is True
        assert _read(tmp_path)["titles"]["g1"]["title"] == "T2"
        # 新 guid：同样记脏并落盘
        s.remember_merged([_brief("g2")])
        assert s._dirty is True
        assert s.flush() is True
        assert sorted(_read(tmp_path)["titles"]) == ["g1", "g2"]
    finally:
        s.close()


def test_close_flushes_change_but_not_unchanged(tmp_path, monkeypatch):
    # 有变化：close 强制落盘
    s = _store(tmp_path, monkeypatch)
    s.remember_merged([_brief("g1")])
    assert not (tmp_path / "titles.json").exists()
    s.close()
    assert _read(tmp_path)["titles"]["g1"]["title"] == "T"
    # 无变化：close 不重写文件
    s2 = _store(tmp_path, monkeypatch)
    try:
        mtime = _mtime_ns(tmp_path)
        s2.close()
        assert _mtime_ns(tmp_path) == mtime
    finally:
        s2.close()


def test_flush_now_generation_guard(tmp_path, monkeypatch):
    # 写盘快照期间又变脏：旧快照落盘后脏标记必须保留，数据不丢
    s = _store(tmp_path, monkeypatch)
    try:
        s.remember_merged([_brief("g1")])
        assert s.flush() is True
        orig_snapshot = s._snapshot_capped

        def hooked():
            titles, orig = orig_snapshot()
            s.remember_merged([_brief("g2")])
            return titles, orig

        # 先制造一次脏（标题变化），让 _flush_now 真正跑起来；
        # 快照期间 hooked 再注入 g2
        s.remember_merged([_brief("g1", title="T2")])
        assert s._dirty is True
        s._snapshot_capped = hooked
        try:
            assert s._flush_now() is True
        finally:
            del s._snapshot_capped
        # 快照是旧的（只有 g1），但脏标记保留：g2 还没落盘
        assert s._dirty is True
        assert sorted(_read(tmp_path)["titles"]) == ["g1"]
        # 再 flush 一次，g2 落盘
        assert s.flush() is True
        assert sorted(_read(tmp_path)["titles"]) == ["g1", "g2"]
    finally:
        s.close()


def test_entry_cap_still_enforced(tmp_path, monkeypatch):
    # 4000 条上限（此处用 2 条做小化验证）：只裁剪落盘快照，不动内存
    s = _store(tmp_path, monkeypatch, cfg=(2, 0, 60.0))
    try:
        s.remember_merged([_brief("g1"), _brief("g2"), _brief("g3")])
        assert s._dirty is True
        assert s.flush() is True
        assert sorted(_read(tmp_path)["titles"]) == ["g2", "g3"]
        assert len(s._titles) == 3
        # 再记一次相同内容：不脏、不重写，快照上限结果不变
        mtime = _mtime_ns(tmp_path)
        s.remember_merged([_brief("g1"), _brief("g2"), _brief("g3")])
        assert s._dirty is False
        assert s.flush() is True
        assert _mtime_ns(tmp_path) == mtime
    finally:
        s.close()
