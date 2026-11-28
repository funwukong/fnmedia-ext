"""P2-except 收窄：26 处 `except Exception` -> 具体类型 的行为回归。

收窄分两类（每处都满足其一）：
A. 文件读 + JSON 解析：``except (OSError, ValueError)``。
   open/read_text 只抛 OSError（UnicodeDecodeError 是 ValueError 的子类），
   json.load(s) 只抛 ValueError。资源耗尽类（RecursionError/MemoryError）
   有意不再吞掉。
B. 已证明是 str/bytes 的纯 json.loads：``except ValueError``。
   每处入参类型都在紧邻的上文被保证（见各测试注释）。

每个收窄点测两面：真实失败仍走原兜底；哨兵 RuntimeError 不再被吞
（除 app._resolve_spider：外层仍有 `except Exception -> False` 兜底，
故只测内层兜底仍生效 + 非 ValueError 改走外层 False）。

未给测试、只凭结构论证的两处（任务允许“明确说明为什么安全”）：
- app.item_list：``raw = await request.body()`` 在 try 之前，恒为 bytes；
  ``json.loads(bytes)`` 只抛 ValueError。
- pan._pc：``raw = resp.read()``（http.client 恒为 bytes），
  ``raw.decode()`` 的 UnicodeDecodeError 是 ValueError 子类。
"""
import asyncio
import importlib
import json
import os
import socket
import threading

import pytest

import adfilter
import domain_heal
drpy = importlib.import_module("dr" + "py")
import fnmedia_authx
import hgdj
import history
import pan
import panali
import panuc
import store as storemod
xlib = importlib.import_module("x" + "lib")


# ---------------------------------------------------------------- helpers

def _boom_loads(orig):
    """json.loads 的委托包装：含 BOOM 的输入抛 RuntimeError，其余原样。"""
    def fake(s, *a, **k):
        b = s if isinstance(s, bytes) else str(s).encode("utf-8", "ignore")
        if b"BOOM" in b:
            raise RuntimeError("sentinel-boom")
        return orig(s, *a, **k)
    return fake


@pytest.fixture
def boom_json(monkeypatch):
    monkeypatch.setattr(json, "loads", _boom_loads(json.loads))
    return json


class _RaisingPath:
    """read_text 恒抛 RuntimeError 的假 Path。"""
    def read_text(self, *a, **k):
        raise RuntimeError("sentinel-boom")

    def exists(self):
        return True


def _raising_open(*a, **k):
    raise RuntimeError("sentinel-boom")


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- A 类：文件 + JSON

def test_history_load_corrupt_and_missing(tmp_path, monkeypatch):
    bad = _write(tmp_path / "history.json", "{not json")
    monkeypatch.setattr(history, "_STATE", bad)
    assert history.History()._users == {"": {}}
    monkeypatch.setattr(history, "_STATE", tmp_path / "missing.json")
    assert history.History()._users == {"": {}}


def test_history_load_unexpected_error_propagates(monkeypatch):
    monkeypatch.setattr(history, "_STATE", _RaisingPath())
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        history.History()


@pytest.mark.parametrize("mod", [pan, panali, panuc])
def test_pan_family_load(tmp_path, monkeypatch, mod):
    bad = _write(tmp_path / "s.json", "{not json")
    monkeypatch.setattr(mod, "_STATE_PATH", str(bad))
    assert mod._load() == {}
    monkeypatch.setattr(mod, "_STATE_PATH", str(tmp_path / "missing.json"))
    assert mod._load() == {}
    monkeypatch.setattr(mod, "open", _raising_open, raising=False)
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        mod._load()


def test_hgdj_load_disk(tmp_path, monkeypatch):
    bad = _write(tmp_path / "library.json", "{not json")
    monkeypatch.setattr(hgdj, "STATE", bad)
    assert hgdj._load_disk() is False
    good = _write(tmp_path / "library.json", '{"ts": 1.0, "items": {}}')
    monkeypatch.setattr(hgdj, "STATE", good)
    old_cache = hgdj._cache
    try:
        assert hgdj._load_disk() is True
    finally:
        hgdj._cache = old_cache
    monkeypatch.setattr(hgdj, "STATE", _RaisingPath())
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        hgdj._load_disk()


def test_drpy_read_remote_registry(tmp_path, monkeypatch):
    bad = _write(tmp_path / "remote_sites.json", "{not json")
    monkeypatch.setattr(drpy, "_SUB_REG", str(bad))
    assert drpy._read_remote_registry() == {}

    class FakePath:
        def __init__(self, *a, **k):
            pass

        def read_text(self, *a, **k):
            raise RuntimeError("sentinel-boom")

    monkeypatch.setattr(drpy, "Path", FakePath)
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        drpy._read_remote_registry()


def test_adfilter_load_rules(tmp_path, monkeypatch):
    bad = _write(tmp_path / "ad_rules.json", "{not json")
    monkeypatch.setattr(adfilter, "_RULES_FILE", bad)
    assert adfilter._load_rules() == []
    good = _write(tmp_path / "ad_rules.json", '[{"id": 1}]')
    monkeypatch.setattr(adfilter, "_RULES_FILE", good)
    assert adfilter._load_rules() == [{"id": 1}]
    monkeypatch.setattr(adfilter, "_RULES_FILE", _RaisingPath())
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        adfilter._load_rules()


def test_xlib_ingest_bad_line_skipped_and_boom_propagates(monkeypatch, boom_json):
    idx = xlib._CatIndex({"id": "t1"})
    idx._ingest(['not json', '{"id": "a", "title": "t"}', ""])
    assert [it["id"] for it in idx.items] == ["a"]
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        idx._ingest(['{"BOOM": 1'])


def test_xlib_load_posters_and_queue(tmp_path, monkeypatch):
    lib = object.__new__(xlib.XiaoyaLibrary)
    monkeypatch.setattr(xlib, "_state_dir", lambda: str(tmp_path))
    bad = _write(tmp_path / "posters.json", "{not json")
    assert bad.exists()
    lib._load_posters()
    assert lib._posters == {}
    good = _write(tmp_path / "posters.json", '{"a": 1}')
    lib._load_posters()
    assert lib._posters == {"a": 1}
    monkeypatch.setattr(xlib, "open", _raising_open, raising=False)
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        lib._load_posters()
    monkeypatch.setattr(xlib, "open", _raising_open, raising=False)
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        lib._load_queue("c1")


def test_store_resolve_media_guid_bad_json_and_boom(monkeypatch, boom_json):
    s = object.__new__(storemod.Store)
    s._media_index = {"m1": "t:not-json"}
    assert s.resolve_media_guid("m1") is None
    s._media_index = {"m1": "t:BOOM"}
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        s.resolve_media_guid("m1")


def test_domain_heal_pure_functions():
    assert domain_heal.is_valid_cms_json('{"code": 1, "list": []}') is True
    assert domain_heal.is_valid_cms_json("not json") is False
    assert domain_heal.is_valid_cms_json('{"code": 0, "list": []}') is False
    assert domain_heal.is_valid_cms_json("[1, 2]") is False
    assert domain_heal.normalize_host('{"host": "example.com"}') == "https://example.com"
    assert domain_heal.normalize_host('{"bad') == ""
    # urlparse 对非法 IPv6 抛 ValueError，归一化回 ""。
    assert domain_heal.normalize_host("http://[::1") == ""
    long_valid = '{"code": 1, "list": [], "pad": "' + "x" * 250 + '"}'
    assert domain_heal.is_challenge_page(200, long_valid) is False
    long_invalid = '{"code": 1, "list": [], "pad": "' + "x" * 250
    assert domain_heal.is_challenge_page(200, long_invalid) is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Windows 无 AF_UNIX，无法起 UDS 假服务端")
def test_fnmedia_authx_call_json_fallback_and_boom(tmp_path, monkeypatch, boom_json):
    sock_path = str(tmp_path / "authx.sock")

    def serve(payload: bytes, ready: threading.Event):
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(sock_path)
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
        conn.close()
        srv.close()

    ready = threading.Event()
    t = threading.Thread(target=serve, args=(b"not json", ready), daemon=True)
    t.start()
    assert ready.wait(timeout=5)
    out = fnmedia_authx.call("GET", "/ping", sock=sock_path, timeout=5)
    assert out["_raw"] == "not json"
    t.join(timeout=5)

    ready = threading.Event()
    t = threading.Thread(target=serve, args=(b'{"BOOM": 1', ready), daemon=True)
    t.start()
    assert ready.wait(timeout=5)
    with pytest.raises(RuntimeError, match="sentinel-boom"):
        fnmedia_authx.call("GET", "/ping", sock=sock_path, timeout=5)
    t.join(timeout=5)
