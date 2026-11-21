from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from base64 import b64encode
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import adfilter
import app as appmod
import hls_race
import quality


class _Cfg:
    def get(self):
        return {
            "skip_intro_enabled": False,
            "skip_intro_seconds": 0,
            "skip_outro_enabled": False,
            "skip_outro_seconds": 0,
        }

    def read(self, key, default=None):
        return self.get().get(key, default)


class _Store:
    def get_title_by_guid(self, guid):
        return {"guid": guid, "type_name": "电影"}


def _request(path="/v/media/online/mg/main.m3u8"):
    split = urlsplit(path)
    req = Request({
        "type": "http", "method": "GET", "path": split.path,
        "query_string": split.query.encode(),
        "headers": [], "app": appmod.app,
    })
    req.state._play_mg = "mg"
    return req


class CompactAesTests(IsolatedAsyncioTestCase):
    async def test_long_aes_playlist_does_not_index_key_uri(self):
        long_seg = "https://cdn.example.test/seg.ts?token=" + ("x" * 380)
        playlist = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:6,\n" + long_seg + "\n"
            "#EXTINF:6,\n" + long_seg.replace("seg.ts", "seg2.ts") + "\n"
            "#EXT-X-ENDLIST\n"
        )
        picked = {
            "online_guid": "og", "line": "plain", "index": 0,
            "url": "https://media.example.test/list.m3u8", "headers": {},
        }
        with patch.object(appmod, "config", _Cfg()), \
                patch.object(appmod, "store", _Store()), \
                patch.object(appmod, "_resolve_if_needed", new=AsyncMock()), \
                patch.object(appmod, "_fetch_playlist", new=AsyncMock(
                    return_value=(playlist, picked["url"]))), \
                patch.object(appmod, "ext_client", return_value=object()), \
                patch.object(quality, "_parse_hls_key", new=AsyncMock(
                    return_value=(b"0123456789abcdef", None))), \
                patch.object(adfilter, "enabled", return_value=False):
            body, _ = await appmod._build_playlist_body("mg", picked, _request())

        lines = [line for line in body.splitlines() if line.startswith("hls?")]
        self.assertEqual(len(lines), 2)
        rid = parse_qs(urlsplit(lines[0]).query)["r"][0]
        self.assertEqual(hls_race._hls_reg_get(rid, 0), long_seg)
        self.assertEqual(
            hls_race._hls_reg_get(rid, 1),
            long_seg.replace("seg.ts", "seg2.ts"),
        )
        self.assertTrue(all("dec=1" in line for line in lines))
        self.assertTrue(all("key.bin" not in line for line in lines))

    async def test_compact_aes_segment_is_decrypted_and_strips_range_headers(self):
        upstream = SimpleNamespace(
            status_code=206,
            content=b"ciphertext",
            headers={
                "content-type": "video/mp2t",
                "content-range": "bytes 0-9/10",
                "accept-ranges": "bytes",
            },
        )
        plain_ts = b"G" + (b"\x00" * 187)
        with patch.object(
                hls_race, "_hls_reg_get",
                return_value="https://cdn/seg.ts"), \
                patch.object(
                    appmod, "ext_get_retry",
                    new=AsyncMock(return_value=upstream)), \
                patch.object(quality, "_aes_cbc_decrypt",
                             new=AsyncMock(return_value=plain_ts)): 
            response = await hls_race.hls_compact(
                "mg", _request(
                    "/v/media/online/mg/hls?r=r&i=0&dec=1&k=MzAzMTMyMzMzNDM1MzYzNzM4Mzk2MTYyNjM2NDY1NjY=&iv=MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="))

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, plain_ts)
        headers = {key.lower(): value for key, value in response.headers.items()}
        self.assertNotIn("content-range", headers)
        self.assertNotIn("accept-ranges", headers)
        self.assertEqual(headers["content-length"], str(len(plain_ts)))
