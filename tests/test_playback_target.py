

def test_special_resolution_and_slow_line_modes():
    from playback_target import is_slow_line, needs_special_resolution
    assert needs_special_resolution({"drpy": True})
    assert needs_special_resolution({"xsrc": True})
    assert not needs_special_resolution({"m3u8": True})
    assert is_slow_line({"line": "drpy-source::main"})
    assert is_slow_line({"catvod": True})
    assert not is_slow_line({"line": "cms::main", "m3u8": True})
