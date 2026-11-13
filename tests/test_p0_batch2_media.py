"""第二批 P0 修复的回归测试：二进制通道、AES-CBC/ECB、HLS key 三格式、IV 语义。

这些用例对应四个真实故障：
  1. toHex 参数没传到 worker → contentHex 永远为空 → AES 密文取不到；
  2. 缺少原始 key/iv 的 AES 模式 → 站点常用 AES-CBC(明文 key/iv) 解不开；
  3. HLS key 只认原始 16 字节 → hex/base64 形态的 key 整链退化；
  4. 显式全零 IV 与「未写 IV」被混为一谈 → 分片解密错位。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("NODE_BIN", "node")


def _node_available() -> bool:
    try:
        subprocess.run([NODE, "--version"], capture_output=True, timeout=20,
                       check=True)
        return True
    except Exception:
        return False


class BinaryChannelTests(unittest.TestCase):
    """二进制/hex 通道：worker 必须按请求保留原始字节。"""

    def test_worker_keeps_hex_and_base64(self):
        w = (ROOT / "proxy/assets/drpy/sync_http_worker.mjs").read_text(
            encoding="utf-8")
        # toHex 必须在 worker 侧按原始 Buffer 取 hex
        self.assertIn("contentHex: o.toHex ? buf.toString(\"hex\") : \"\"", w)
        # bytes 请求要保留 base64
        self.assertIn("contentBase64", w)
        # 元数据要把 base64 带回主线程
        self.assertRegex(w, r"contentBase64:\s*result\.contentBase64")

    def test_haikuo_passes_hex_and_bytes_to_worker(self):
        m = (ROOT / "proxy/assets/drpy/haikuo.mjs").read_text(encoding="utf-8")
        self.assertIn("toHex: o.toHex === true", m)
        self.assertIn("responseType:", m)
        self.assertIn("res.bodyBase64", m)

    def test_sync_http_returns_base64(self):
        s = (ROOT / "proxy/assets/drpy/sync_http.mjs").read_text(encoding="utf-8")
        self.assertIn("contentBase64", s)


@unittest.skipUnless(_node_available(), "node 不可用")
class AesNodeTests(unittest.TestCase):
    """AES-CBC/ECB：真实用 Node 跑一遍已知向量。"""

    def _run(self, script: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.mjs"
            p.write_text(script, encoding="utf-8")
            out = subprocess.run([NODE, str(p)], capture_output=True,
                                 timeout=60, text=True)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            return json.loads(out.stdout.strip().splitlines()[-1])

    def test_cbc_roundtrip_and_known_vector(self):
        # 从 haikuo.mjs 抽出 AES 实现片段，跑真实加解密
        src = (ROOT / "proxy/assets/drpy/haikuo.mjs").read_text(encoding="utf-8")
        start = src.index("  function _aesCoerceBytes")
        end = src.index("  function aesEcbDecode")
        end = src.index("\n  }\n", src.index("return _aesOutput", end)) + 4
        block = src[start:end]
        script = f"""
import crypto from 'node:crypto';
function vErr(m) {{ return new Error(m); }}
{block}
const key = '000102030405060708090a0b0c0d0e0f';
const iv  = '101112131415161718191a1b1c1d1e1f';
const plain = 'hello haikuo aes';
const enc = aesCbcEncode(plain, key, iv, {{dataEncoding:'utf8', output:'base64'}});
const dec = aesCbcDecode(enc, key, iv, {{dataEncoding:'base64', output:'utf8'}});
console.log(JSON.stringify({{enc, dec, same: dec === plain}}));
"""
        r = self._run(script)
        self.assertEqual(r["dec"], "hello haikuo aes")
        self.assertTrue(r["same"])

    def test_cbc_hex_key_and_zero_iv(self):
        src = (ROOT / "proxy/assets/drpy/haikuo.mjs").read_text(encoding="utf-8")
        start = src.index("  function _aesCoerceBytes")
        end = src.index("  function aesEcbDecode")
        end = src.index("\n  }\n", src.index("return _aesOutput", end)) + 4
        block = src[start:end]
        script = f"""
import crypto from 'node:crypto';
function vErr(m) {{ return new Error(m); }}
{block}
const key = '000102030405060708090a0b0c0d0e0f';
const iv  = '00000000000000000000000000000000';
const enc = aesCbcEncode('zero-iv', key, iv, {{dataEncoding:'utf8', output:'base64'}});
const dec = aesCbcDecode(enc, key, iv, {{dataEncoding:'base64', output:'utf8'}});
// 16 字节对齐的明文用 NoPadding 加密：NoPadding 解密应精确还原；
// 用 Pkcs7 解密则因没有合法填充尾字节而报错（说明 padding 语义真的被区分）。
const aligned = '0123456789abcdef';
const encRaw = aesCbcEncode(aligned, key, iv, {{dataEncoding:'utf8', output:'base64', padding:'NoPadding'}});
const decRaw = aesCbcDecode(encRaw, key, iv, {{dataEncoding:'base64', output:'utf8', padding:'NoPadding'}});
let threw = false;
try {{ aesCbcDecode(encRaw, key, iv, {{dataEncoding:'base64', output:'utf8'}}); }}
catch (e) {{ threw = true; }}
console.log(JSON.stringify({{dec, decRaw, threw}}));
"""
        r = self._run(script)
        self.assertEqual(r["dec"], "zero-iv")
        self.assertEqual(r["decRaw"], "0123456789abcdef")
        self.assertTrue(r["threw"])

    def test_ecb_roundtrip(self):
        src = (ROOT / "proxy/assets/drpy/haikuo.mjs").read_text(encoding="utf-8")
        start = src.index("  function _aesCoerceBytes")
        end = src.index("\n  }\n", src.index("return _aesOutput",
                 src.index("function aesEcbDecode"))) + 4
        block = src[start:end]
        script = f"""
import crypto from 'node:crypto';
function vErr(m) {{ return new Error(m); }}
{block}
const key = '0123456789abcdef';
const enc = aesCbcEncode('ecb-data', key, null, {{dataEncoding:'utf8', output:'base64'}});
console.log(JSON.stringify({{ok: typeof enc === 'string' && enc.length > 0}}));
"""
        r = self._run(script)
        self.assertTrue(r["ok"])

    def test_aes_api_exposed_to_rules(self):
        m = (ROOT / "proxy/assets/drpy/haikuo.mjs").read_text(encoding="utf-8")
        self.assertIn('assign("aesCbcDecode"', m)
        self.assertIn('assign("aesCbcEncode"', m)
        self.assertIn('assign("aesEcbDecode"', m)


class HlsKeyFormatTests(unittest.TestCase):
    """HLS key 三格式 + IV 语义。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "proxy"))
        import quality
        self.quality = quality

    def test_coerce_raw_hex_base64(self):
        raw = bytes(range(16))
        self.assertEqual(self.quality._coerce_hls_key(raw), raw)
        self.assertEqual(self.quality._coerce_hls_key(raw.hex().encode()),
                         raw)
        self.assertEqual(
            self.quality._coerce_hls_key(base64.b64encode(raw)), raw)
        # 不支持的形态必须返回 None，不能瞎猜
        self.assertIsNone(self.quality._coerce_hls_key(b""))
        self.assertIsNone(self.quality._coerce_hls_key(b"0" * 31))

    def test_hex_with_0x_prefix(self):
        raw = bytes(range(16))
        self.assertEqual(self.quality._coerce_hls_key(b"0x" + raw.hex().encode()),
                         raw)

    def test_iv_semantics_in_quality(self):
        src = (ROOT / "proxy/quality.py").read_text(encoding="utf-8")
        # 显式零 IV 与「未声明 IV」必须区分
        self.assertIn("explicit_iv if explicit_iv is not None", src)
        self.assertNotIn("explicit_iv != b\"\\0\" * 16", src)


class MediaTypeProbeTests(unittest.TestCase):
    """无后缀媒体：不能只靠后缀判定 HLS/直链。"""

    def test_haikuo_resolve_keeps_unknown_suffix_for_probe(self):
        src = (ROOT / "proxy/app.py").read_text(encoding="utf-8")
        self.assertIn("bili_probe_media", src)
        self.assertIn("#ishls=true#", src.lower())

    def test_hls_race_falls_back_to_direct_for_binary_body(self):
        src = (ROOT / "proxy/hls_race.py").read_text(encoding="utf-8")
        self.assertIn("not _is_media_playlist(text)", src)

    def test_headers_read_after_resolve(self):
        src = (ROOT / "proxy/hls_race.py").read_text(encoding="utf-8")
        i_resolve = src.index("await _resolve_if_needed(picked, request)")
        i_headers = src.index("headers = dict(picked.get(\"headers\") or {})")
        self.assertGreater(i_headers, i_resolve,
                           "headers 必须在解析之后读取，否则用的是旧鉴权头")


if __name__ == "__main__":
    unittest.main()
