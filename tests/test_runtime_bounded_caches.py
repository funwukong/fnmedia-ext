"""运行期内存与轮询优化回归。

对应三项优化：
1. 长跑缓存必须有界：榜单解析表 / 榜单失败标记 / 日志采样计数 /
   慢线后台失败计数 / 小雅 media_guid 映射，超限淘汰最旧条目，
   避免 NAS 长时间运行后内存单调上涨。
2. 扩展开启动作不在请求路径里同步等待网络刷新（见
   test_http_core_paths::test_extension_enable_does_not_block_on_network_reload）。
3. 管理页轮询间隔放宽（3s→5s、700ms→1000ms），降低后台页空转。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import app as appmod  # noqa: E402
import hls_race as hracemod  # noqa: E402
import lib_inject  # noqa: E402


def _fill_beyond(d: dict, cap: int, n: int, put):
    for i in range(n):
        put(d, f"k{i}", i, cap)


def test_bounded_put_evicts_oldest_in_insertion_order():
    d: dict[str, int] = {}
    for i in range(5):
        appmod._bounded_put(d, f"k{i}", i, 3)
    assert list(d) == ["k2", "k3", "k4"]


def test_chart_resolved_is_bounded():
    cap = appmod._CHART_RESOLVED_MAX
    assert cap > 0
    backup = dict(appmod._chart_resolved)
    try:
        appmod._chart_resolved.clear()
        _fill_beyond(appmod._chart_resolved, cap, cap + 50, appmod._bounded_put)
        assert len(appmod._chart_resolved) == cap
        assert "k0" not in appmod._chart_resolved
        assert f"k{cap + 49}" in appmod._chart_resolved
    finally:
        appmod._chart_resolved.clear()
        appmod._chart_resolved.update(backup)


def test_chart_failed_ts_is_bounded():
    cap = appmod._CHART_FAILED_MAX
    assert cap > 0
    backup = dict(appmod._CHART_FAILED_TS)
    try:
        appmod._CHART_FAILED_TS.clear()
        _fill_beyond(appmod._CHART_FAILED_TS, cap, cap + 50, appmod._bounded_put)
        assert len(appmod._CHART_FAILED_TS) == cap
    finally:
        appmod._CHART_FAILED_TS.clear()
        appmod._CHART_FAILED_TS.update(backup)


def test_log_sample_is_bounded():
    cap = appmod._LOG_SAMPLE_MAX
    backup = dict(appmod._LOG_SAMPLE)
    try:
        appmod._LOG_SAMPLE.clear()
        for i in range(cap + 25):
            appmod._sampled_log(f"probe-{i}", every=10)
        assert len(appmod._LOG_SAMPLE) <= cap
    finally:
        appmod._LOG_SAMPLE.clear()
        appmod._LOG_SAMPLE.update(backup)


def test_main_serve_bg_fails_is_bounded():
    cap = hracemod._MAIN_SERVE_BG_FAILS_MAX
    assert cap > 0
    d = hracemod._MAIN_SERVE_BG_FAILS
    backup = dict(d)
    try:
        d.clear()
        for i in range(cap + 40):
            hracemod._bounded_put(d, f"bk{i}", 1, cap)
        assert len(d) == cap
        assert "bk0" not in d
    finally:
        d.clear()
        d.update(backup)


def test_xlib_media_map_is_bounded():
    cap = lib_inject._XLIB_MAP_MAX
    assert cap > 0
    d = lib_inject._XLIB_MEDIA_MAP
    backup = dict(d)
    try:
        d.clear()
        for i in range(cap + 40):
            lib_inject._bounded_put(d, f"mg{i}", ("sg", i), cap)
        assert len(d) == cap
        assert "mg0" not in d
        # 最新登记仍可反查（播放链路依赖这一语义）
        assert lib_inject._xlib_resolve_media(f"mg{cap + 39}") == ("sg", cap + 39)
    finally:
        d.clear()
        d.update(backup)


def test_admin_ui_poll_intervals_are_relaxed():
    ui = (ROOT / "proxy" / "admin_ui.py").read_text(encoding="utf-8")
    assert "setInterval(loadLive,5000)" in ui
    assert "setInterval(sourceProbePoll,1000)" in ui
    assert "setInterval(loadLive,3000)" not in ui
    assert "setInterval(sourceProbePoll,700)" not in ui
