import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import runtime_metrics


def test_sample_is_rate_limited_but_forced_works():
    runtime_metrics.reset()
    assert runtime_metrics.sample(cache_bytes=1048576, active_tasks=2, force=True)
    assert runtime_metrics.sample(cache_bytes=2048, active_tasks=3) is None
    assert runtime_metrics.sample(cache_bytes=2048, active_tasks=4, force=True)


def test_snapshot_reports_peak_and_recent():
    runtime_metrics.reset()
    runtime_metrics.sample(cache_bytes=10 * 1048576, active_tasks=1, force=True)
    runtime_metrics.sample(cache_bytes=1048576, active_tasks=2, force=True)
    snap = runtime_metrics.snapshot()
    assert snap["samples"] == 2
    assert snap["rss_peak_mb"] >= snap["rss_mb"] >= 0
    assert snap["cache_mb"] == 1.0
    assert snap["tasks"] == 2
    assert len(snap["recent"]) == 2


def test_reset_clears_samples():
    runtime_metrics.sample(force=True)
    runtime_metrics.reset()
    snap = runtime_metrics.snapshot()
    assert snap["samples"] == 0 and snap["recent"] == []
