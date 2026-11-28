"""验收会话事件「按 stage 保底」回归。

真机 2026-11-26 验收报告的教训：每设备 60 条环形缓冲 + 报告取最新 30 条，
一次起播的几十条 seg 就把 play/info、play/play、search 这些关键阶段事件
冲出窗口，报告里只剩分片。现在会话开着时每 stage 各保底保留最新 6 条，
报告合并时无条件并入（按 seq 去重），会话结束即清。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import live as livemod


def _setup():
    livemod.reset()
    livemod.start_acceptance_session("t")


def _rec(stage, path="/x", status=200, **kw):
    livemod.record("UA", "1.2.3.4", "GET", path, "", status,
                   "local", stage=stage, **kw)


def test_keep_rescues_play_from_seg_flood():
    _setup()
    for i in range(40):
        _rec("seg", f"/seg?u={i}")
    _rec("play", "/play/info")
    for i in range(40, 85):
        _rec("seg", f"/seg?u={i}")
    evs = livemod.snapshot()["devices"][0]["events"]
    c = Counter(e["stage"] for e in evs)
    assert c.get("play", 0) >= 1, f"play 应被保底捞回: {dict(c)}"
    assert len(evs) <= 30
    seqs = [e["seq"] for e in evs]
    assert all(seqs[i] > seqs[i + 1] for i in range(len(seqs) - 1))


def test_keep_early_search_detail_still_visible():
    # 首页/搜索发生在起播前很久、早滚出 60 条环形窗口——保底的意义所在。
    _setup()
    _rec("search", "/search")
    _rec("detail", "/detail")
    for i in range(80):
        _rec("seg", f"/seg?u={i}")
    evs = livemod.snapshot()["devices"][0]["events"]
    c = Counter(e["stage"] for e in evs)
    assert c.get("search", 0) >= 1 and c.get("detail", 0) >= 1
    assert len(evs) <= 30


def test_no_session_behaves_as_before():
    _setup()
    for i in range(40):
        _rec("seg", f"/seg?u={i}")
    _rec("play", "/play/info")
    for i in range(40, 85):
        _rec("seg", f"/seg?u={i}")
    livemod.clear_acceptance_session()
    evs = livemod.snapshot()["devices"][0]["events"]
    assert set(Counter(e["stage"] for e in evs)) == {"seg"}


def test_clear_drops_keep_queues():
    _setup()
    _rec("play", "/play/info")
    with livemod._LOCK:
        dev = next(iter(livemod._DEVICES.values()))
        assert dev.get("_acc_keep", {}).get("play")
    livemod.clear_acceptance_session()
    with livemod._LOCK:
        dev = next(iter(livemod._DEVICES.values()))
        assert not (dev.get("_acc_keep") or {})


def test_error_event_not_lost():
    _setup()
    _rec("play", "/play/play", status=500, note="boom")
    for i in range(80):
        _rec("seg", f"/seg?u={i}")
    evs = livemod.snapshot()["devices"][0]["events"]
    assert any(e["stage"] == "play" and e["status"] == 500 for e in evs)
