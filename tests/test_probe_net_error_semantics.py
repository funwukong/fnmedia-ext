"""探活判定准确性：网络层失败不得判死源（避免自动删除误杀健康源）。

沙箱实测：真实 CMS 源（caiji.dyttzyapi.com）被 ConnectTimeout 命中时，
旧逻辑直接 alive=False，「失效自动删除」会把正常源删掉。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import providers  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def _r(**kw):
    base = {"ok": False, "status": 0, "elapsed_ms": 1, "count": 0,
            "sample": "", "error": ""}
    base.update(kw)
    return base


def _patch(monkeypatch, **kw):
    async def fake(api, keyword="", timeout=0, source=None):
        return _r(**kw)

    monkeypatch.setattr(providers, "probe_source", fake)


def test_cms_all_timeouts_do_not_conclude(monkeypatch):
    """全部关键词都超时 → alive=None（不下结论），绝不 False。"""
    _patch(monkeypatch, error="ConnectTimeout")
    r = _run(providers.probe_source_alive({"api": "https://ok.example/api.php/provide/vod/"}))
    assert r["alive"] is None
    assert r["reachable"] is False
    assert "网络" in (r.get("reason") or "")


def test_cms_connect_error_do_not_conclude(monkeypatch):
    """域名解析失败同样不下结论（可能是本地 DNS 抖动）。"""
    _patch(monkeypatch, error="ConnectError: Name or service not known")
    r = _run(providers.probe_source_alive({"api": "https://ok.example/api.php/provide/vod/"}))
    assert r["alive"] is None


def test_cms_structural_error_is_dead(monkeypatch):
    """站点明确应答但不是 CMS 结构 → 确实失效，允许删除。"""
    _patch(monkeypatch, error="JSONDecodeError: Expecting value")
    r = _run(providers.probe_source_alive({"api": "https://ok.example/api.php/provide/vod/"}))
    assert r["alive"] is False


def test_cms_http_status_error_is_dead(monkeypatch):
    """站点返回 502/403 这种是站点侧问题，判失效。"""
    _patch(monkeypatch, error="HTTP 502")
    r = _run(providers.probe_source_alive({"api": "https://ok.example/api.php/provide/vod/"}))
    assert r["alive"] is False


def test_cms_reachable_but_no_hits(monkeypatch):
    """接口能正常返回但基准词无结果 → alive=None，不熔断小众源。"""
    _patch(monkeypatch, ok=True, count=0)
    r = _run(providers.probe_source_alive({"api": "https://ok.example/api.php/provide/vod/"}))
    assert r["alive"] is None
    assert r.get("reason")


def test_net_error_text_classifier():
    assert providers._is_net_error_text("ConnectTimeout")
    assert providers._is_net_error_text("Name or service not known")
    assert providers._is_net_error_text("Server disconnected")
    assert not providers._is_net_error_text("JSONDecodeError")
    assert not providers._is_net_error_text("HTTP 403")
    assert not providers._is_net_error_text("")
