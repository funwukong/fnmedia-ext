"""P1-8：网盘过期转存清理（pan/panuc.cleanup_expired + app 定时循环）。

清理只删 SAVE_DIR 内年龄 > TTL 的文件：有效转存保留、无可信时间戳的
保留、目录永不删除；开关关闭时不清理；清理失败只记日志、不影响播放。
不碰真实网盘：_pc / logged_in / _active_cookie 全部打桩。
"""
import asyncio

import pytest

import pan as pan_quark
import panuc as pan_uc
import app as appmod

NOW = 1_800_000_000  # 固定“现在”，TTL 边界可精确断言
TTL = 6 * 3600


def _items():
    return [
        {"fid": "old", "file_name": "old.mp4", "dir": False,
         "size": 100, "created_at": NOW - TTL - 1},       # 年龄 TTL+1：删
        {"fid": "oldms", "file_name": "oldms.mkv", "dir": False,
         "size": 50, "created_at": (NOW - 30000) * 1000},  # 毫秒时间戳：删
        {"fid": "edge", "file_name": "edge.mp4", "dir": False,
         "size": 70, "created_at": NOW - TTL},             # 年龄恰好 TTL：保留
        {"fid": "fresh", "file_name": "fresh.mp4", "dir": False,
         "size": 80, "created_at": NOW - 3600},            # 有效：保留
        {"fid": "nots", "file_name": "nots.mp4", "dir": False,
         "size": 90},                                      # 无时间戳：保留
        {"fid": "subdir", "file_name": "子目录", "dir": True,
         "size": 0, "created_at": NOW - 999999},           # 目录：永不删除
    ]


def _fake_pc(mod, fail_delete=False):
    calls = {"list": [], "delete": []}

    def fake_pc(method, url, cookie, _tries=5, **kw):
        params = kw.get("params") or {}
        if method == "GET" and "/file/sort" in url:
            if params.get("pdir_fid") == "0":
                return {"code": 0, "data": {"list": [
                    {"fid": "dirfid", "file_name": mod.SAVE_DIR,
                     "dir": True}]}}
            calls["list"].append(int(params.get("_page", 1)))
            return {"code": 0, "data": {"list": _items()}}
        if method == "POST" and "/file/delete" in url:
            fids = list((kw.get("json") or {}).get("fid_list") or [])
            calls["delete"].append(fids)
            if fail_delete:
                raise mod.PanError("删失败")
            return {"code": 0, "data": {}}
        raise AssertionError(f"unexpected call {method} {url}")

    return fake_pc, calls


def _patch_mod(monkeypatch, mod, fail_delete=False):
    fake_pc, calls = _fake_pc(mod, fail_delete=fail_delete)
    monkeypatch.setattr(mod, "logged_in", lambda: True)
    monkeypatch.setattr(mod, "_active_cookie", lambda *a, **k: "cookie")
    monkeypatch.setattr(mod, "_pc", fake_pc)
    return calls


@pytest.mark.parametrize("mod", [pan_quark, pan_uc])
def test_cleanup_deletes_only_expired(mod, monkeypatch):
    calls = _patch_mod(monkeypatch, mod)
    res = mod.cleanup_expired(TTL, now=NOW)
    assert res["deleted"] == 2
    assert res["freed"] == 150
    assert res["kept"] == 3      # 边界 + 有效 + 无时间戳
    assert res["unknown"] == 1   # 无时间戳那条
    assert res["errors"] == 0
    assert calls["delete"] == [["old", "oldms"]]  # 只删过期两条，一批发出
    assert calls["list"] == [1]  # 一页即列完


@pytest.mark.parametrize("mod", [pan_quark, pan_uc])
def test_cleanup_ttl_boundary(mod, monkeypatch):
    # 年龄恰好等于 TTL 保留、TTL+1 秒删除：边界条件单独断言。
    calls = _patch_mod(monkeypatch, mod)
    res = mod.cleanup_expired(TTL, now=NOW)
    deleted = calls["delete"][0] if calls["delete"] else []
    assert "edge" not in deleted
    assert "old" in deleted
    assert res["kept"] >= 1


@pytest.mark.parametrize("mod", [pan_quark, pan_uc])
def test_cleanup_not_logged_in_noop(mod, monkeypatch):
    fake_pc, calls = _fake_pc(mod)
    monkeypatch.setattr(mod, "logged_in", lambda: False)
    monkeypatch.setattr(mod, "_pc", fake_pc)
    res = mod.cleanup_expired(TTL, now=NOW)
    assert res == {"deleted": 0, "freed": 0, "kept": 0, "unknown": 0,
                   "errors": 0}
    assert calls == {"list": [], "delete": []}  # 未登录不发任何请求


@pytest.mark.parametrize("mod", [pan_quark, pan_uc])
def test_cleanup_delete_failure_does_not_raise(mod, monkeypatch):
    _patch_mod(monkeypatch, mod, fail_delete=True)
    res = mod.cleanup_expired(TTL, now=NOW)
    assert res["deleted"] == 0
    assert res["freed"] == 0
    assert res["errors"] == 2  # 删失败按条计数，不抛异常、不影响播放
    assert res["kept"] == 3 + 2  # 删失败的仍计为保留


@pytest.mark.parametrize("mod", [pan_quark, pan_uc])
def test_cleanup_list_failure_returns_error_dict(mod, monkeypatch):
    monkeypatch.setattr(mod, "logged_in", lambda: True)
    monkeypatch.setattr(mod, "_active_cookie", lambda *a, **k: "cookie")

    def _boom(*a, **k):
        raise mod.PanError("list 失败")

    monkeypatch.setattr(mod, "_pc", _boom)
    res = mod.cleanup_expired(TTL, now=NOW)
    assert res["deleted"] == 0 and res["errors"] == 1
    assert "error" in res


@pytest.mark.parametrize("mod", [pan_quark, pan_uc])
def test_created_ts_units(mod):
    assert mod._created_ts({"created_at": NOW - 100}, NOW) == NOW - 100
    assert mod._created_ts({"created_at": (NOW - 100) * 1000}, NOW) == NOW - 100
    assert mod._created_ts({"created_at": str(NOW - 100)}, NOW) == NOW - 100
    assert mod._created_ts(
        {"created_at": "2027-01-15T00:00:00+00:00"}, NOW) is not None
    assert mod._created_ts({}, NOW) is None
    assert mod._created_ts({"created_at": "garbage"}, NOW) is None
    assert mod._created_ts({"created_at": 123}, NOW) is None  # 1970 年，不合理


def test_cleanup_invalid_ttl_raises():
    with pytest.raises(ValueError):
        pan_quark.cleanup_expired(0)
    with pytest.raises(ValueError):
        pan_uc.cleanup_expired(-5)


def test_cleanup_switch_off_skips(monkeypatch):
    """开关关闭：_pan_cache_cleanup_once 不调任何模块的清理。"""
    called = []
    for mod in (pan_quark, pan_uc):
        monkeypatch.setattr(mod, "logged_in", lambda: True)
        monkeypatch.setattr(
            mod, "cleanup_expired",
            lambda ttl, now=None, _m=mod: called.append(_m.__name__) or {
                "deleted": 1, "freed": 1})
    monkeypatch.setattr(appmod.config, "get", lambda: {
        "pan_cache_cleanup_enabled": False, "pan_cache_ttl_hours": 6})
    out = asyncio.run(appmod._pan_cache_cleanup_once())
    assert out == {}
    assert called == []


def test_cleanup_switch_on_calls_both(monkeypatch):
    called = []
    for mod in (pan_quark, pan_uc):
        monkeypatch.setattr(mod, "logged_in", lambda: True)
        monkeypatch.setattr(
            mod, "cleanup_expired",
            lambda ttl, now=None, _m=mod: called.append(_m.__name__) or {
                "deleted": 1, "freed": 2048})
    monkeypatch.setattr(appmod.config, "get", lambda: {
        "pan_cache_cleanup_enabled": True, "pan_cache_ttl_hours": 6})
    out = asyncio.run(appmod._pan_cache_cleanup_once())
    assert set(out) == {"quark", "uc"}
    assert out["quark"]["deleted"] == 1
    assert sorted(called) == ["pan", "panuc"]
