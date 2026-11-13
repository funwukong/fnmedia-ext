"""panqr：把网盘登录二维码烧录进 HLS 占位 TS 分片的契约。

2026-11-12 上游同步第 3 批：模块与测试均取自上游公开仓
（``upstream/main:proxy/panqr.py`` / ``tests/test_panqr.py``），仅把
测试的 PNG 生成从 Pillow 换成标准库（fork 测试 venv 与公开构建均
无 Pillow；被测的二维码 PNG 本身来自 ``pan*.py`` 的 segno，与 PIL 无关）。
"""
from __future__ import annotations

import asyncio
import struct
import sys
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import panqr


def _png(w: int = 200, h: int = 200) -> bytes:
    """标准库生成纯白 RGB PNG（无 Pillow 依赖）。"""
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

    def chunk(typ: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + typ + data
        return out + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@unittest.skipUnless(panqr.ffmpeg_available(), "ffmpeg 不可用")
class PanQrTsTests(unittest.TestCase):
    def test_build_returns_valid_mpegts(self):
        ts = panqr.build_login_ts(_png(), "夸克网盘登录", "扫码后自动播放")
        self.assertIsNotNone(ts)
        # MPEG-TS 同步字节 0x47
        self.assertEqual(ts[0], 0x47)
        self.assertGreater(len(ts), 1000)

    def test_cached_result_identical(self):
        png = _png()
        a = panqr.build_login_ts(png, "t", "s")
        b = panqr.build_login_ts(png, "t", "s")
        self.assertEqual(a, b)

    def test_empty_input_returns_none(self):
        self.assertIsNone(panqr.build_login_ts(b"", "t", "s"))

    def test_async_wrapper(self):
        ts = asyncio.run(panqr.build_login_ts_async(_png(), "t", "s"))
        self.assertIsNotNone(ts)
        self.assertEqual(ts[0], 0x47)


class PanQrFallbackTests(unittest.TestCase):
    def test_missing_ffmpeg_returns_none(self):
        with patch.object(panqr, "ffmpeg_available", return_value=False):
            self.assertIsNone(panqr.build_login_ts(_png(), "t", "s"))

    def test_font_candidates_are_absolute_paths(self):
        for p in panqr._FONT_CANDIDATES:
            self.assertTrue(p.startswith("/"))


if __name__ == "__main__":
    unittest.main()
