"""Stable player line identity encoding.

The player persists bitrate values across titles. Encoding by menu index is
unsafe because the current line is moved to the front; encode by line name
instead and retain the legacy index decoder for upgrades.
"""
from __future__ import annotations

import zlib

_BASE = 90_000_000
_LIMIT = 1_000_000
_LEGACY_STEP = 10001


def bitrate(line: str) -> int:
    return _BASE + (zlib.crc32((line or "").encode("utf-8")) % _LIMIT)


def line_of_bitrate(value, lines: list[str]) -> str | None:
    try:
        code = int(value or 0)
    except (TypeError, ValueError):
        return None
    if not _BASE <= code < _BASE + _LIMIT:
        return None
    for line in lines:
        if bitrate(line) == code:
            return line
    # Pre-stable-encoding compatibility: old menus used index * 10001.
    idx = (code - _BASE) // _LEGACY_STEP
    return lines[idx] if 0 <= idx < len(lines) else None
