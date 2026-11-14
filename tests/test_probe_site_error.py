"""探活：站点侧 TLS 错误必须判失效（不是"网络未知"）。

背景：httpx 把证书链缺失包装成 ConnectError，类型判不出来，
      导致 agedm 这类站点（证书链不全）永远停在 alive=None，
      既不会被删除，也不会被禁用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import httpx  # noqa: E402
import providers  # noqa: E402


def _conn_error(msg: str) -> Exception:
    """造一个真正的 httpx.ConnectError（与线上同一类型）。"""
    try:
        raise httpx.ConnectError(msg)
    except httpx.ConnectError as exc:
        return exc


def test_tls_text_detected_as_site_side():
    for msg in (
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1016)",
        "certificate has expired",
        "Hostname mismatch, certificate is not valid for 'x.com'",
        "wrong version number",
    ):
        exc = _conn_error(msg)
        assert type(exc).__name__ == "ConnectError"
        assert providers._is_site_side_error(exc) is True, msg


def test_plain_network_error_is_not_site_side():
    for msg in ("Connection reset by peer", "Network is unreachable",
                "Connection refused", "timed out"):
        assert providers._is_site_side_error(_conn_error(msg)) is False, msg


def test_ssl_error_type_detected():
    import ssl
    assert providers._is_site_side_error(ssl.SSLError("whatever")) is True
