"""安装验收 + 播放验收会话回归。

覆盖：
1. `GET /v/api/ext/install-check` 返回构建戳、核心路由齐全、配置/缓存可读；
2. 验收会话开启后，`record` 产生的事件带同一 session tag；
3. 报告接口只包含本次会话事件，未开启会话时为空；
4. `POST .../acceptance/clear` 清掉会话与记录；
5. 管理页包含「安装验收」卡与四个操作入口。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import app as appmod  # noqa: E402
import live as livemod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import config as cfgmod
    import store as storemod
    monkeypatch.setattr(cfgmod, "_STATE", tmp_path / "config.json")
    monkeypatch.setattr(storemod, "_STATE", tmp_path / "titles.json")
    cfg = cfgmod.Config()
    monkeypatch.setattr(appmod, "config", cfg)
    monkeypatch.setattr(cfgmod, "config", cfg)
    monkeypatch.setattr(appmod, "_schedule_routes_refresh", lambda: None)
    livemod.clear_acceptance_session()
    livemod.reset()
    with TestClient(appmod.app, raise_server_exceptions=False) as c:
        yield c
    livemod.clear_acceptance_session()
    livemod.reset()


def test_install_check_reports_ready(client):
    r = client.get("/v/api/ext/install-check")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True, d
    checks = d["checks"]
    assert checks["required_routes"]["ok"] is True, checks["required_routes"]
    assert checks["config_readable"]["ok"] is True
    assert checks["store_readable"]["ok"] is True
    # 构建戳在源码直跑时是 dev（advisory），装包后为版本+提交
    bl = checks["build_label"]
    assert "value" in bl and bl.get("advisory") is True
    assert d["client_acceptance"] == ["首页加载", "搜索", "详情/选集", "起播",
                                      "拖动进度", "续播"]


def test_acceptance_session_tags_events(client):
    started = client.post("/v/api/ext/install-check/acceptance/start", json={})
    assert started.status_code == 200
    tag = started.json()["data"]["session"]
    assert tag

    # 模拟一次播放链路请求（record 直接调用，避免依赖真实播放）
    livemod.record("TV-UA", "127.0.0.1", "GET",
                   "/v/media/online/mg1/main.m3u8", "", 200, "local",
                   title="测试片", stage="hls")

    report = client.get("/v/api/ext/install-check/report").json()
    assert report["ok"] is True
    assert report["session"] == tag
    evs = report["live"]["acceptance_events"]
    assert evs, "本次会话应有事件"
    assert all(e["acceptance_session"] == tag for e in evs)


def test_report_empty_without_session(client):
    livemod.record("TV-UA", "127.0.0.1", "GET", "/v/api/v1/search/list",
                   "q=x", 200, "mixed", stage="search")
    report = client.get("/v/api/ext/install-check/report").json()
    assert report["session"] == ""
    assert report["live"]["acceptance_events"] == []
    # 但设备快照仍在（实时诊断不受影响）
    assert report["live"]["devices"]


def test_acceptance_clear_resets(client):
    client.post("/v/api/ext/install-check/acceptance/start", json={"tag": "t1"})
    livemod.record("TV-UA", "127.0.0.1", "GET", "/v/api/v1/play/info",
                   "", 200, "local", title="片", stage="play")
    cleared = client.post("/v/api/ext/install-check/acceptance/clear", json={})
    assert cleared.status_code == 200
    assert cleared.json()["data"] is True
    assert livemod.acceptance_session() == ""
    assert livemod.snapshot()["devices"] == []


def test_report_includes_install_and_health(client):
    d = client.get("/v/api/ext/install-check/report").json()
    assert "install" in d and "source_health" in d
    assert d["install"]["auth_mode"] in {"off", "local", "token"}
    assert "extension_enabled" in d["install"]
    assert isinstance(d["install"]["background_tasks"], list)
    assert d["steps"][0] == "首页加载"


def test_admin_ui_has_acceptance_card():
    # v1.2.8 起管理页 HTML 外置到 proxy/assets/admin.html
    ui = (ROOT / "proxy" / "assets" / "admin.html").read_text(encoding="utf-8")
    assert "安装验收" in ui
    for fn in ("runInstallCheck", "startAcceptance",
               "downloadAcceptanceReport", "copyAcceptanceReport"):
        assert f"onclick=\"{fn}()\"" in ui, fn
    assert 'id="installCheckResult"' in ui


def test_install_check_reports_feature_status(client):
    """验收自检里的 features 段：优化开关是否真的生效（advisory，不参与判定）。"""
    d = client.get("/v/api/ext/install-check").json()
    feats = d["checks"]["features"]
    assert feats["advisory"] is True, "开关状态只作提示，不参与总判定"
    v = feats["value"]
    assert {"seg_direct", "playlist_ttl", "poster_mirror", "poster_disk"} <= set(v)
    # 默认：直连关 + 白名单空 → 明确标成「未生效」
    assert v["seg_direct"]["enabled"] is False
    assert v["seg_direct"]["effective"] is False
    assert v["seg_direct"]["direct_hits"] == 0
    # 默认 TTL 应被识别为默认值（提示还没按实测调过）
    assert v["playlist_ttl"]["is_default"] is True


def test_report_includes_feature_status(client):
    d = client.get("/v/api/ext/install-check/report").json()
    assert "features" in d["install"]
    assert "playlist_ttl" in d["install"]["features"]


def test_admin_ui_shows_feature_lines():
    ui = (ROOT / "proxy" / "assets" / "admin.html").read_text(encoding="utf-8")
    assert "分片直连: " in ui
    assert "清单缓存: 点播 " in ui
    assert "白名单为空（等于没开）" in ui
    assert 'id="playlistTtlVod"' in ui
