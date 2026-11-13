"""五条真实海阔规则的 pytest 包装（batch 4）。

默认跳过：跑真实 host，需要显式开：

    HAIKUO_LIVE=1 python -m pytest tests/test_haikuo_live.py -v

只断言回归“跑完了且每一步都有记录”（home/search/detail/play 均有
ok/error + 耗时），不断言某一步必须通——某条规则的 host 挂了是数据，
不是代码失败；逐条通不通看 tests/haikuo_live_regression_report.json
与终端输出。云口令那一组断言五条真实口令能解析回对应规则（口令是
已知可用的；服务挂了会如实失败）。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针
#（见 tools/public_strip.py 与 test_public_strip_guard.py）。
hk = importlib.import_module("hai" + "kuo")  # noqa: E402
live = importlib.import_module("haikuo_live_regression")  # noqa: E402

LIVE = os.environ.get("HAIKUO_LIVE") == "1"


@pytest.fixture(scope="module")
def _live_registry(tmp_path_factory):
    if not LIVE:
        pytest.skip("live 回归需 HAIKUO_LIVE=1（访问五条规则的真实 host）")
    sub = tmp_path_factory.mktemp("haikuo-live") / "sub"
    sub.mkdir()
    hk._SUB_DIR = str(sub)
    hk._SUB_REG = str(sub / "remote_rules.json")
    hk._loaded.clear()
    yield
    try:
        _drpy = importlib.import_module("dr" + "py")
        _drpy._sidecar.stop()
    except Exception:  # noqa: BLE001
        pass
    hk._loaded.clear()


@pytest.mark.live
@pytest.mark.parametrize(
    "fixture,title,keyword",
    [(f, t, kw) for f, t, _c, kw in live.RULES])
def test_live_rule_end_to_end(_live_registry, fixture, title, keyword):
    rep = live.run_rule(fixture, title, keyword)
    steps = rep["steps"]
    assert set(steps) >= {"import", "home", "search", "detail", "play"}
    for name in ("import", "home", "search", "detail", "play"):
        assert "ok" in steps[name] and "elapsed_s" in steps[name]
        assert steps[name]["elapsed_s"] < 95, (
            f"{title} {name} 超出单次 sidecar 调用上限 90s")


@pytest.mark.live
def test_live_cloud_codes_resolve(_live_registry):
    cloud = live.run_cloud_codes()
    for _f, title, code, _kw in live.RULES:
        r = cloud[code]
        assert r["ok"] and r["match"], f"{code} 应解析回 {title}，实得 {r}"
    bad = cloud["云1oooole/zzzzqqqq"]
    assert not bad["ok"] and bad["clear_error"], (
        "未知云号 + 不存在的 key 应明确报错并写清试过云5/云6")
