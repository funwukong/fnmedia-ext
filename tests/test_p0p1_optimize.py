"""P0+P1 优化回归：诊断窗口 / 质量样本门槛 / 熔断退避 / 失效源筛选 / 报告开关。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))


def test_live_window_errors():
    src = (ROOT / "proxy" / "live.py").read_text(encoding="utf-8")
    assert "ERROR_WINDOW = 300" in src
    assert "err_ts" in src
    assert '"errors_recent"' in src


def test_live_record_and_snapshot():
    import live
    live.reset()
    live.record("UA-test-window", "1.2.3.4", "GET",
                "/v/api/v1/play/info", "", 404, "local", title="窗口测试",
                stage="play")
    snap = live.snapshot()
    dev = snap["devices"][0]
    assert dev["errors"] == 1
    assert dev["errors_recent"] == 1
    assert dev["error_window_sec"] == 300
    # 老错误（400 秒前）不计入窗口
    dev_obj = live._DEVICES[dev["id"]]
    assert dev_obj["err_ts"], "err_ts should have entries"
    dev_obj["err_ts"].clear()
    dev_obj["err_ts"].append(time.time() - 400)
    snap2 = live.snapshot()
    dev2 = [d for d in snap2["devices"] if d["id"] == dev["id"]][0]
    assert dev2["errors"] == 1
    assert dev2["errors_recent"] == 0
    live.reset()


def test_quality_min_samples_gate():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert "MIN_QUALITY_SAMPLES = 2" in src
    assert "n < MIN_QUALITY_SAMPLES" in src


def test_sourcehealth_backoff():
    import sourcehealth as sh
    sh.reset()
    key = "__backoff_test"
    cooldowns = []
    # 首轮 5 连败触发熔断；之后冷却过期后，fails 计数保持 >= 阈值，
    # 每一条失败都会再次熔断并推进退避级别 —— 与真实「熔断到期重试仍失败」一致。
    for _i in range(sh.DEAD_FAILS):
        sh.record(key, False)
    with sh._LOCK:
        sh._STATS[key]["dead_until"] = time.time() - 1
    for _round in range(4):
        sh.record(key, False)  # 1 次失败即再熔断（fails 已 >= 阈值）
        st = sh.stats(key)
        cd = max(0, st["dead_until"] - time.time())
        cooldowns.append(round(cd))
        with sh._LOCK:
            sh._STATS[key]["dead_until"] = time.time() - 1
    # 逐级翻倍：r1=180, r2=360, r3=720, r4=1440
    assert cooldowns[0] == sh.DEAD_COOLDOWN * 2
    assert cooldowns[1] == sh.DEAD_COOLDOWN * 4
    assert cooldowns[2] == sh.DEAD_COOLDOWN * 8
    assert cooldowns[3] == sh.DEAD_COOLDOWN * 16
    assert cooldowns[-1] <= sh.DEAD_COOLDOWN_MAX
    # 成功归零
    sh.record(key, True)
    st = sh.stats(key)
    assert st["dead_count"] == 0
    assert st["dead_until"] == 0
    sh.reset()


def test_sourcehealth_stats_has_dead_count():
    import sourcehealth as sh
    sh.reset()
    sh.record("__dc", False)
    st = sh.stats("__dc")
    assert "dead_count" in st
    sh.reset()


def test_source_quality_payload_has_last_ok():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '"last_ok": st.get("last_ok") or 0' in src
    assert '"dead_until": st.get("dead_until") or 0' in src
    assert '"dead_count": st.get("dead_count") or 0' in src


def test_ui_suspect_filter_and_bulk_disable():
    html = (ROOT / "proxy" / "assets" / "admin.html").read_text(encoding="utf-8")
    assert 'value="suspect"' in html
    assert 'value="dead"' in html
    assert "srcSuspect" in html
    assert "srcDeadNow" in html
    assert "disableSuspectSources" in html
    assert 'id="srcDeadBtn"' in html
    # 诊断窗口徽章
    assert "errors_recent" in html
    assert "最近错误" in html
    assert "当前正常" in html


def test_features_snapshot_present():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert "def _features_snapshot" in src
    for key in ("linepredict", "browse_probe", "zero_race", "seg_direct",
                "poster_mirror", "poster_disk"):
        assert f'"{key}"' in src
    assert '"features": {"ok": True, "advisory": True' in src
    assert '"features": _features_snapshot()}' in src


def test_mirror_hits_counter():
    src = (ROOT / "proxy" / "adfilter.py").read_text(encoding="utf-8")
    assert "MIRROR_HITS = 0" in src
    assert "MIRROR_HITS += 1" in src
    src2 = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert 'getattr(_af, "MIRROR_HITS", 0)' in src2
