

def test_special_resolution_and_slow_line_modes():
    from playback_target import is_slow_line, needs_special_resolution
    assert needs_special_resolution({"drpy": True})
    assert needs_special_resolution({"xsrc": True})
    assert not needs_special_resolution({"m3u8": True})
    assert is_slow_line({"line": "drpy-source::main"})
    assert is_slow_line({"catvod": True})
    assert not is_slow_line({"line": "cms::main", "m3u8": True})


def test_client_direct_and_direct_view_helpers():
    from playback_target import client_direct_only, is_direct_view

    assert client_direct_only({"client_direct": True}) is True
    assert client_direct_only({"url": "x"}) is False
    assert client_direct_only(None) is False

    # legacy 'direct' 语义：显式 HLS，或无需网页解析
    assert is_direct_view({"m3u8": True}) is True
    assert is_direct_view({"url": "x"}) is True
    assert is_direct_view({"url": "x", "resolve": True}) is False
    assert is_direct_view(None) is True
