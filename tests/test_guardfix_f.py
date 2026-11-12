"""F 组复现测试：配置/安全/卫生（guardfix-f 清单第 1-3、6-11 项）。

每项先断言修复后的行为；基线 d5a0f62 上应失败，修复后通过。
第 11 项的 _load_sites 加锁用“并发重载不死锁/结果一致”作回归 guard；
_mid-file 重复 import_ 用源码断言作失败复现。
"""
from __future__ import annotations

import json
import logging
import stat
import subprocess as real_subprocess
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import admin_auth
import app as appmod
import config as config_mod
import guard as guard_mod

GUARD_SRC = Path(guard_mod.__file__).read_text(encoding="utf-8")

SAMPLE_TVBOX = {
    "sites": [
        {"type": 3, "api": "csp_BttwooGuard", "name": "比特",
         "ext": "SECRET-EXT-VALUE", "searchable": 1},
        {"type": 3, "api": "csp_We-ird.Key!Guard", "name": "怪名",
         "ext": "SECRET2", "searchable": 1},
    ],
}


def _patch_guard_assets(monkeypatch, tmp_path):
    asset = tmp_path / "guard_assets"
    asset.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(guard_mod, "_ASSET", asset)
    monkeypatch.setattr(guard_mod, "_SITES", asset / "sites.json")
    monkeypatch.setattr(guard_mod, "_IMPORTED", asset / "imported.json")
    monkeypatch.setattr(guard_mod, "_USER_JAR", asset / "guard.jar")
    monkeypatch.setattr(guard_mod, "_sites", None)
    return asset


def _fresh_env(tmp_path, monkeypatch):
    cfg_state = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", cfg_state)
    cfg = config_mod.Config()
    monkeypatch.setattr(appmod, "config", cfg)
    monkeypatch.setattr(config_mod, "config", cfg)
    monkeypatch.setattr(appmod, "_ADMIN_LOGIN_LIMITER",
                        admin_auth.LoginAttemptLimiter())
    monkeypatch.setattr(appmod, "_schedule_routes_refresh", lambda: None)
    return cfg


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------- 1. 落盘权限 ----------------

def test_imported_json_written_0600(monkeypatch, tmp_path):
    asset = _patch_guard_assets(monkeypatch, tmp_path)
    guard_mod.import_tvbox(json.dumps(SAMPLE_TVBOX), enable=False)
    assert _mode(asset / "imported.json") == 0o600


def test_set_enabled_rewrite_0600(monkeypatch, tmp_path):
    asset = _patch_guard_assets(monkeypatch, tmp_path)
    (asset / "sites.json").write_text(json.dumps(
        {"sites": [{"key": "Bttwoo", "spider": "BttwooGuard",
                    "api": "csp_BttwooGuard", "name": "比特",
                    "ext": "", "enabled": False}]}, ensure_ascii=False),
        encoding="utf-8")
    assert guard_mod.set_enabled("Bttwoo", True) is True
    assert _mode(asset / "imported.json") == 0o600


def test_config_persist_and_backup_0600(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    c = config_mod.Config()
    c.save({"hot_limit": 300})
    assert _mode(state) == 0o600
    backups = list((tmp_path / "state" / "backups").glob("config-*.json"))
    assert backups, "应有备份文件"
    assert all(_mode(b) == 0o600 for b in backups)


# ---------------- 2. guard_import 不回显 ext ----------------

def test_import_tvbox_response_strips_ext(monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)
    res = guard_mod.import_tvbox(json.dumps(SAMPLE_TVBOX), enable=False)
    assert all("ext" not in s for s in res["sites"])
    # 落盘仍保留 ext（只 strip 返回值，不动写入数据）
    imp = json.loads((tmp_path / "guard_assets" / "imported.json")
                     .read_text(encoding="utf-8"))
    assert imp["sites"][0]["ext"] == "SECRET-EXT-VALUE"


def test_import_tvbox_include_ext_opt_in(monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)
    res = guard_mod.import_tvbox(json.dumps(SAMPLE_TVBOX), enable=False,
                                 include_ext=True)
    assert res["sites"][0]["ext"] == "SECRET-EXT-VALUE"


def test_guard_import_endpoint_strips_ext(tmp_path, monkeypatch):
    _patch_guard_assets(monkeypatch, tmp_path)
    _fresh_env(tmp_path, monkeypatch)
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/guard/import",
                     json={"config": json.dumps(SAMPLE_TVBOX),
                           "enable": False})
    assert r.status_code == 200 and r.json()["code"] == 0
    assert "SECRET-EXT-VALUE" not in json.dumps(r.json())
    assert "SECRET2" not in json.dumps(r.json())


# ---------------- 3. 管理页 key 转义 + key 收紧 ----------------

def test_render_guard_rows_escapes_key():
    import admin_ui
    html = admin_ui.ADMIN_HTML
    assert "removeGuard('${esc(s.key)}')" in html
    assert "toggleGuard('${esc(s.key)}'" in html


def test_parse_tvbox_key_sanitized():
    parsed = guard_mod.parse_tvbox(
        {"sites": [{"type": 3, "api": "csp_We-ird.Key!Guard",
                    "name": "怪名", "ext": "", "searchable": 1}]})
    assert [s["key"] for s in parsed["sites"]] == ["WeirdKey"]


def test_parse_tvbox_key_only_illegal_chars_skipped():
    parsed = guard_mod.parse_tvbox(
        {"sites": [{"type": 3, "api": "csp_-.-Guard",
                    "name": "全非法", "ext": "", "searchable": 1}]})
    assert parsed["sites"] == []
    assert parsed["skipped"] == 1


# ---------------- 6. 缓存上限 ----------------

def test_msearch_cache_evicts_oldest_over_1000():
    now = time.time()
    appmod._MSEARCH_CACHE.clear()
    try:
        for i in range(1000):
            appmod._MSEARCH_CACHE[f"t{i}"] = (now - 1000 + i, "k", "v", "l")
        appmod._msearch_cache_put("t-new", (now, "k", "v", "l"))
        assert len(appmod._MSEARCH_CACHE) == 801
        assert "t-new" in appmod._MSEARCH_CACHE
        assert "t0" not in appmod._MSEARCH_CACHE
        assert "t199" not in appmod._MSEARCH_CACHE
        assert "t200" in appmod._MSEARCH_CACHE
    finally:
        appmod._MSEARCH_CACHE.clear()


def test_home_cache_capped_at_200(monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)
    monkeypatch.setattr(guard_mod, "home", lambda conf: {"class": []})
    guard_mod._HOME_CACHE.clear()
    try:
        for i in range(201):
            guard_mod._HOME_CACHE[f"k{i}"] = (float(i), {"class": []})
        out = guard_mod.home_cached({"key": "k-new"}, force=True)
        assert out == {"class": []}
        assert "k-new" in guard_mod._HOME_CACHE
        assert len(guard_mod._HOME_CACHE) == 152  # 202 - 50 最旧
        assert "k0" not in guard_mod._HOME_CACHE
    finally:
        guard_mod._HOME_CACHE.clear()


# ---------------- 7. jar 下发失败不 500 ----------------

class _FakeRun:
    """按容器名决定 docker cp/exec 成败的 subprocess.run 替身。"""

    def __init__(self, fail_cp=(), fail_exec=()):
        self.fail_cp = set(fail_cp)
        self.fail_exec = set(fail_exec)
        self.cp_paths: list[str] = []
        self.modes: list[int] = []

    def __call__(self, args, **kwargs):
        if args[:2] == ["docker", "cp"]:
            self.cp_paths.append(args[2])
            self.modes.append(_mode(Path(args[2])))
            cname = args[3].split(":", 1)[0]
            if cname in self.fail_cp:
                raise real_subprocess.TimeoutExpired(args, timeout=180)
            return real_subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["docker", "exec"]:
            if args[2] in self.fail_exec:
                raise real_subprocess.TimeoutExpired(args, timeout=40)
            return real_subprocess.CompletedProcess(args, 0, "ok\n", "")
        return real_subprocess.CompletedProcess(args, 0, "", "")


def test_push_device_jar_one_container_timeout_does_not_abort_others(
        monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)
    fake = _FakeRun(fail_cp={"c1"})
    monkeypatch.setattr(guard_mod, "pool_containers", lambda: ["c1", "c2"])
    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            run=fake,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    assert guard_mod.push_device_jar(b"PKDATA", "/data/x/guard.jar") is True


def test_push_device_jar_all_containers_fail_returns_false(monkeypatch):
    fake = _FakeRun(fail_cp={"c1", "c2"})
    monkeypatch.setattr(guard_mod, "pool_containers", lambda: ["c1", "c2"])
    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            run=fake,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    assert guard_mod.push_device_jar(b"PKDATA", "/data/x/guard.jar") is False


def test_save_user_jar_push_timeout_returns_hint_not_raise(
        monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)

    def _boom():
        raise real_subprocess.TimeoutExpired(["docker", "cp"], timeout=180)

    monkeypatch.setattr(guard_mod, "_push_user_jar", _boom)
    res = guard_mod.save_user_jar(b"PK\x03\x04" + b"x" * 10, "guard.jar")
    assert res["ok"] is True
    assert res["pushed"] is False
    assert "下发失败" in res["hint"]


def test_install_bridge_assets_warns_on_pm_failure(monkeypatch, caplog):
    calls = []

    def _exec(args, timeout=15):
        calls.append(args)
        if args[:1] == ["sh"]:
            return real_subprocess.CompletedProcess(args, 0, "y\n", "")
        return real_subprocess.CompletedProcess(
            args, 1, "", "INSTALL_FAILED tail-marker")

    monkeypatch.setattr(guard_mod, "_docker_exec", _exec)
    monkeypatch.setattr(guard_mod, "_push_user_jar", lambda: False)
    with caplog.at_level(logging.WARNING, logger="fnmedia"):
        guard_mod._install_bridge_assets()  # 非 0 也不应抛
    assert any("tail-marker" in (r.getMessage() or "")
               for r in caplog.records if r.levelno >= logging.WARNING)


def test_guard_jar_upload_save_failure_is_502(tmp_path, monkeypatch):
    _patch_guard_assets(monkeypatch, tmp_path)
    _fresh_env(tmp_path, monkeypatch)
    import base64
    monkeypatch.setattr(
        guard_mod, "save_user_jar",
        lambda raw, name="": (_ for _ in ()).throw(RuntimeError("disk boom")))
    with TestClient(appmod.app, raise_server_exceptions=False) as cli:
        r = cli.post("/v/api/ext/guard/jar",
                     json={"name": "guard.jar",
                           "data_b64": base64.b64encode(
                               b"PK\x03\x04xxxx").decode()})
    assert r.status_code == 502
    assert "已保存但下发失败" in r.json()["msg"]


# ---------------- 8. 推送临时文件唯一 + 0600 + 串行锁 ----------------

def test_push_device_jar_unique_tmpfiles_0600(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(guard_mod, "pool_containers", lambda: ["c1"])
    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            run=fake,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(guard_mod.push_device_jar, b"PKDATA",
                          "/data/x/guard.jar") for _ in range(2)]
        results = [f.result() for f in futs]
    assert results == [True, True]
    assert len(set(fake.cp_paths)) == 2, "两线程必须用不同临时文件"
    assert all(m == 0o600 for m in fake.modes)


# ---------------- 9. docker 调用超时 ----------------

def test_create_redroid_network_create_has_timeout(monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)
    seen = {}

    def _run(args, **kwargs):
        if args[:3] == ["docker", "network", "create"]:
            seen["timeout"] = kwargs.get("timeout")
        return real_subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(guard_mod, "_resolve_redroid_image",
                        lambda: "test-image")
    monkeypatch.setattr(guard_mod, "_container_exists", lambda: False)
    monkeypatch.setattr(guard_mod, "REDROID_DATA", str(tmp_path / "rdata"))
    monkeypatch.setattr(guard_mod, "_ensure_redroid_dns", lambda *a: None)
    monkeypatch.setattr(guard_mod, "_redroid_booted", lambda: True)
    monkeypatch.setattr(guard_mod, "_install_bridge_assets", lambda: None)
    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            run=_run,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    guard_mod._create_redroid()
    assert seen.get("timeout") == 15


def test_create_redroid_network_create_timeout_is_tolerated(
        monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)

    def _run(args, **kwargs):
        if args[:3] == ["docker", "network", "create"]:
            raise real_subprocess.TimeoutExpired(args, timeout=15)
        return real_subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(guard_mod, "_resolve_redroid_image",
                        lambda: "test-image")
    monkeypatch.setattr(guard_mod, "_container_exists", lambda: False)
    monkeypatch.setattr(guard_mod, "REDROID_DATA", str(tmp_path / "rdata"))
    monkeypatch.setattr(guard_mod, "_ensure_redroid_dns", lambda *a: None)
    monkeypatch.setattr(guard_mod, "_redroid_booted", lambda: True)
    monkeypatch.setattr(guard_mod, "_install_bridge_assets", lambda: None)
    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            run=_run,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    guard_mod._create_redroid()  # 不应抛


def test_resolve_redroid_image_pull_timeout_300_and_logs(
        monkeypatch, caplog):
    seen = {}

    def _run(args, **kwargs):
        if args[:2] == ["docker", "pull"]:
            seen["timeout"] = kwargs.get("timeout")
        return real_subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(guard_mod, "_redroid_image_present",
                        lambda name=None: False if seen.get("pulled") is None
                        else True)
    orig_run = real_subprocess.run

    def _run2(args, **kwargs):
        if args[:2] == ["docker", "pull"]:
            seen["timeout"] = kwargs.get("timeout")
            seen["pulled"] = True
        return real_subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            run=_run2,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    with caplog.at_level(logging.INFO, logger="fnmedia"):
        out = guard_mod._resolve_redroid_image()
    assert out == guard_mod.REDROID_IMAGE
    assert seen.get("timeout") == 300
    assert any("拉取" in (r.getMessage() or "") for r in caplog.records
               if r.levelno >= logging.INFO)


# ---------------- 10. start_android 锁泄漏 / _bg 异常 / 安装任务超时 ----------------

def test_start_android_thread_start_failure_releases_lock(monkeypatch):
    monkeypatch.setattr(guard_mod, "_docker", lambda: True)
    monkeypatch.setattr(guard_mod, "_container_running", lambda: False)
    monkeypatch.setattr(guard_mod, "_redroid_image_present", lambda: True)
    monkeypatch.setattr(guard_mod, "_container_exists", lambda: False)

    class _RaisingThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("thread boom")

    monkeypatch.setattr(guard_mod, "threading",
                        types.SimpleNamespace(Thread=_RaisingThread))
    res = guard_mod.start_android(wait_ready=False)
    assert isinstance(res, dict) and res.get("ok") is False
    assert guard_mod._android_start_lock.acquire(blocking=False) is True
    guard_mod._android_start_lock.release()


def test_start_android_bg_error_recorded(monkeypatch):
    monkeypatch.setattr(guard_mod, "_docker", lambda: True)
    monkeypatch.setattr(guard_mod, "_container_running", lambda: False)
    monkeypatch.setattr(guard_mod, "_redroid_image_present", lambda: True)
    monkeypatch.setattr(guard_mod, "_container_exists", lambda: False)
    monkeypatch.setattr(guard_mod, "_last_start_error", {})

    def _boom():
        raise RuntimeError("bringup boom")

    monkeypatch.setattr(guard_mod, "_start_android_blocking", _boom)

    class _SyncThread:
        def __init__(self, target=None, **k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(guard_mod, "threading",
                        types.SimpleNamespace(Thread=_SyncThread))
    res = guard_mod.start_android(wait_ready=False)
    assert res.get("starting") is True
    assert "bringup boom" in str(guard_mod._last_start_error.get("error", ""))


def test_sidecar_status_exposes_last_start_error(monkeypatch, tmp_path):
    _patch_guard_assets(monkeypatch, tmp_path)
    monkeypatch.setattr(guard_mod, "_last_start_error",
                        {"ok": False, "error": "bringup boom", "at": 123.0})
    monkeypatch.setattr(guard_mod, "_docker", lambda: True)
    monkeypatch.setattr(guard_mod, "_redroid_image_present",
                        lambda name=None: False)
    monkeypatch.setattr(guard_mod, "_container_exists", lambda: False)
    monkeypatch.setattr(guard_mod, "_container_running", lambda: False)
    monkeypatch.setattr(guard_mod, "_manual_stop_requested", lambda: False)
    monkeypatch.setattr(guard_mod, "android_last_use", lambda: 0.0)
    monkeypatch.setattr(guard_mod, "_job", None)
    st = guard_mod.sidecar_status()
    assert st["android_last_start_error"]["error"] == "bringup boom"


class _FakeProc:
    def __init__(self, pid=1):
        self.pid = pid
        self.terminated = False
        self.killed = False
        self._waits = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self._waits += 1
        if self._waits == 1:
            raise real_subprocess.TimeoutExpired(["x"], timeout)
        return 0

    def kill(self):
        self.killed = True


def test_cancel_install_terminate_then_kill(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(guard_mod, "_job",
                        {"pid": 1, "proc": proc, "started": time.time(),
                         "what": "all", "log": "x"})
    res = guard_mod.cancel_install()
    assert res["ok"] is True
    assert proc.terminated is True
    assert proc.killed is True
    assert guard_mod._job is None


def test_start_install_overwrites_timed_out_job(monkeypatch, tmp_path):
    bootstrap = tmp_path / "bootstrap_sidecar.sh"
    bootstrap.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(guard_mod, "_BOOTSTRAP", bootstrap)
    monkeypatch.setattr(guard_mod, "_SIDECAR_STATE", tmp_path / "state")
    monkeypatch.setattr(guard_mod, "_INSTALL_LOG",
                        tmp_path / "state" / "sidecar-install.log")
    monkeypatch.setattr(guard_mod, "_job",
                        {"pid": 1, "proc": _FakeProc(), "started":
                         time.time() - 2000, "what": "all", "log": "x"})

    class _FakePopen:
        def __init__(self, *a, **k):
            self.pid = 999

    monkeypatch.setattr(guard_mod, "subprocess",
                        types.SimpleNamespace(
                            Popen=_FakePopen,
                            STDOUT=real_subprocess.STDOUT,
                            TimeoutExpired=real_subprocess.TimeoutExpired,
                            CompletedProcess=real_subprocess.CompletedProcess))
    res = guard_mod.start_install("all")
    assert res["ok"] is True and res["pid"] == 999


# ---------------- 11. 小项 ----------------

def test_reload_sites_concurrent_no_deadlock(monkeypatch, tmp_path):
    asset = _patch_guard_assets(monkeypatch, tmp_path)
    (asset / "sites.json").write_text(json.dumps(
        {"sites": [{"key": "K1", "name": "一"}]}, ensure_ascii=False),
        encoding="utf-8")
    (asset / "imported.json").write_text(json.dumps({"sites": []}),
                                         encoding="utf-8")
    results = []

    def _work():
        results.append(guard_mod.reload_sites())

    threads = [threading.Thread(target=_work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads), "reload_sites 死锁"
    assert all("K1" in r for r in results)


def test_no_duplicate_midfile_imports():
    assert GUARD_SRC.count("\nimport shutil\n") == 1
    assert GUARD_SRC.count("\nimport subprocess\n") == 1


def test_manual_stop_requested_fail_open_unchanged(monkeypatch):
    """OSError → False 是看门狗 fail-open 设计，刻意不改：只锁定行为。"""
    class _BadFlag:
        def exists(self):
            raise OSError("disk gone")

    monkeypatch.setattr(guard_mod, "_ANDROID_STOP_FLAG", _BadFlag())
    assert guard_mod._manual_stop_requested() is False
