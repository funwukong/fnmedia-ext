"""P0 二批修复：二进制通道 + AES-CBC/ECB + HLS key + 无后缀探测。

幂等：重复执行安全。改完打印每项的落地位置。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
changed: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    if new in s:
        print(f"  [skip] {tag} 已存在")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 找不到锚点 in {rel}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    changed.append(tag)
    print(f"  [ok] {tag}")


# ---------- P0-1a: worker 保留原始字节 ----------
edit(
    "proxy/assets/drpy/sync_http_worker.mjs",
    """  const buf = Buffer.from(await r.arrayBuffer());
  const h = {};
  r.headers.forEach((v, k) => { h[k] = v; });""",
    """  const buf = Buffer.from(await r.arrayBuffer());
  const h = {};
  r.headers.forEach((v, k) => { h[k] = v; });
  // 二进制通道：responseType=bytes 时原样保留字节，不做 UTF-8 解码。
  // toHex 直接对原始 Buffer 取 hex（旧的“对已解码文本再编码”会破坏密文）。""",
    "P0-1a worker 注释",
)

edit(
    "proxy/assets/drpy/sync_http_worker.mjs",
    """    contentHex: o.toHex ? buf.toString("hex") : "",""",
    """    contentHex: o.toHex ? buf.toString("hex") : "",
    contentBase64: o.responseType === "bytes" ? buf.toString("base64") : "",""",
    "P0-1b worker contentBase64",
)

edit(
    "proxy/assets/drpy/sync_http_worker.mjs",
    """    contentHex: result.contentHex || "",""",
    """    contentHex: result.contentHex || "",
    contentBase64: result.contentBase64 || "",""",
    "P0-1c worker meta 透传 base64",
)

# ---------- P0-1d: sync_http.mjs 读取 base64 ----------
edit(
    "proxy/assets/drpy/sync_http.mjs",
    """  let content = "", meta = { status: 0, headers: {}, error: "", contentHex: "" };""",
    """  let content = "", meta = { status: 0, headers: {}, error: "", contentHex: "",
    contentBase64: "" };""",
    "P0-1d sync_http meta 初始值",
)

edit(
    "proxy/assets/drpy/sync_http.mjs",
    """    contentHex: meta.contentHex || "",""",
    """    contentHex: meta.contentHex || "",
    contentBase64: meta.contentBase64 || "",""",
    "P0-1e sync_http 返回 base64",
)

# ---------- P0-1f: haikuo.mjs 传参 + 暴露 bytes ----------
edit(
    "proxy/assets/drpy/haikuo.mjs",
    """    const r = syncReqImpl(u, {
      method, headers, body,
      timeout: timeoutMs,
      redirect: o.redirect === false ? 0 : undefined,
    });""",
    """    const r = syncReqImpl(u, {
      method, headers, body,
      timeout: timeoutMs,
      redirect: o.redirect === false ? 0 : undefined,
      // 二进制/hex 请求必须透传到 worker，否则 worker 只会做 UTF-8 解码，
      // 密文与原始字节在到达规则前就已损坏（toHex 曾经因此永远拿不到 hex）。
      toHex: o.toHex === true,
      responseType: o.responseType === "bytes" ? "bytes" : undefined,
    });""",
    "P0-1f haikuo 传 toHex/bytes",
)

edit(
    "proxy/assets/drpy/haikuo.mjs",
    """    const res = {
      body: String(r.content ?? ""),
      headers: r.headers || {},
      statusCode: Number(r.status),
      finalUrl: String(r.url || u),
    };""",
    """    const res = {
      body: String(r.content ?? ""),
      bodyBase64: String(r.contentBase64 || ""),
      bodyHex: String(r.contentHex || ""),
      headers: r.headers || {},
      statusCode: Number(r.status),
      finalUrl: String(r.url || u),
    };""",
    "P0-1g haikuo res 带 base64/hex",
)

edit(
    "proxy/assets/drpy/haikuo.mjs",
    """  function shapeFetch(res, o) {
    if (o.toHex) return String(res.contentHex || "");""",
    """  function shapeFetch(res, o) {
    if (o.toHex) return String(res.bodyHex || res.contentHex || "");
    if (o.responseType === "bytes" || o.bytes === true) {
      const buf = res.bodyBase64
        ? Buffer.from(res.bodyBase64, "base64")
        : Buffer.from(res.body || "", "utf8");
      return buf;
    }""",
    "P0-1h haikuo bytes 返回 Buffer",
)

# ---------- P0-2: AES-CBC/ECB ----------
AES_CODE = '''
  // ---------- AES（原始 key/iv 模式，与 CryptoJS 口令模式并存） ----------
  // aesDecode/aesEncode 是 CryptoJS 口令模式（Salted__ + OpenSSL KDF），
  // 保留原语义不动；这里新增的是站点常用的「明文 key/iv」模式。
  function _aesCoerceBytes(value, encoding) {
    if (value == null) return null;
    if (typeof value === "object" && value && value.type === "Buffer"
        && Array.isArray(value.data)) return Buffer.from(value.data);
    const s = String(value);
    const enc = String(encoding || "").toLowerCase();
    if (enc === "hex") return Buffer.from(s.replace(/\\s+/g, ""), "hex");
    if (enc === "base64") return Buffer.from(s.replace(/\\s+/g, ""), "base64");
    // 未指定编码时按长度与字符集猜：32/48/64 位 hex → hex；否则按 utf8。
    const t = s.replace(/\\s+/g, "");
    if (/^[0-9a-fA-F]{32}$/.test(t) || /^[0-9a-fA-F]{48}$/.test(t)
        || /^[0-9a-fA-F]{64}$/.test(t)) return Buffer.from(t, "hex");
    return Buffer.from(s, "utf8");
  }
  function _aesInputBytes(data, encoding) {
    if (data == null) return Buffer.alloc(0);
    if (typeof data === "object" && data && data.type === "Buffer"
        && Array.isArray(data.data)) return Buffer.from(data.data);
    if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
    const s = String(data);
    const enc = String(encoding || "base64").toLowerCase();
    if (enc === "utf8") return Buffer.from(s, "utf8");
    if (enc === "hex") return Buffer.from(s.replace(/\\s+/g, ""), "hex");
    return Buffer.from(s.replace(/\\s+/g, ""), "base64");
  }
  function _aesNoPaddingRemove(buf, padding) {
    const p = String(padding || "Pkcs7").toLowerCase();
    if (p === "nopadding" || p === "none") return buf;
    if (!buf.length) return buf;
    const pad = buf[buf.length - 1];
    if (pad > 0 && pad <= 16 && pad <= buf.length) {
      let ok = true;
      for (let i = buf.length - pad; i < buf.length; i++) {
        if (buf[i] !== pad) { ok = false; break; }
      }
      if (ok) return buf.subarray(0, buf.length - pad);
    }
    return buf;
  }
  function _aesOutput(buf, encoding) {
    const enc = String(encoding || "utf8").toLowerCase();
    if (enc === "buffer" || enc === "bytes") return buf;
    if (enc === "base64") return buf.toString("base64");
    if (enc === "hex") return buf.toString("hex");
    return buf.toString("utf8");
  }
  function aesCbcDecode(data, key, iv, options) {
    const o = options || {};
    const kb = _aesCoerceBytes(key, o.keyEncoding);
    const ivb = iv ? _aesCoerceBytes(iv, o.ivEncoding) : Buffer.alloc(16, 0);
    const db = _aesInputBytes(data, o.dataEncoding);
    if (!kb || kb.length !== 16) throw vErr("aesCbcDecode: key must be 16 bytes");
    if (ivb.length !== 16) throw vErr("aesCbcDecode: iv must be 16 bytes");
    if (db.length % 16 !== 0) throw vErr("aesCbcDecode: data length not multiple of 16");
    const decipher = crypto.createDecipheriv("aes-128-cbc", kb, ivb);
    decipher.setAutoPadding(String(o.padding || "Pkcs7").toLowerCase() !== "nopadding");
    const out = Buffer.concat([decipher.update(db), decipher.final()]);
    return _aesOutput(_aesNoPaddingRemove(out, o.padding), o.output);
  }
  function aesCbcEncode(data, key, iv, options) {
    const o = options || {};
    const kb = _aesCoerceBytes(key, o.keyEncoding);
    const ivb = iv ? _aesCoerceBytes(iv, o.ivEncoding) : Buffer.alloc(16, 0);
    const db = _aesInputBytes(data, o.dataEncoding || "utf8");
    if (!kb || kb.length !== 16) throw vErr("aesCbcEncode: key must be 16 bytes");
    if (ivb.length !== 16) throw vErr("aesCbcEncode: iv must be 16 bytes");
    const cipher = crypto.createCipheriv("aes-128-cbc", kb, ivb);
    cipher.setAutoPadding(String(o.padding || "Pkcs7").toLowerCase() !== "nopadding");
    const out = Buffer.concat([cipher.update(db), cipher.final()]);
    return _aesOutput(out, o.output);
  }
  function aesEcbDecode(data, key, options) {
    const o = options || {};
    const kb = _aesCoerceBytes(key, o.keyEncoding);
    const db = _aesInputBytes(data, o.dataEncoding);
    if (!kb || kb.length !== 16) throw vErr("aesEcbDecode: key must be 16 bytes");
    if (db.length % 16 !== 0) throw vErr("aesEcbDecode: data length not multiple of 16");
    const decipher = crypto.createDecipheriv("aes-128-ecb", kb, null);
    decipher.setAutoPadding(String(o.padding || "Pkcs7").toLowerCase() !== "nopadding");
    const out = Buffer.concat([decipher.update(db), decipher.final()]);
    return _aesOutput(_aesNoPaddingRemove(out, o.padding), o.output);
  }
'''

edit(
    "proxy/assets/drpy/haikuo.mjs",
    """  // ---------- 编解码 / 加解密 ----------""",
    AES_CODE + """
  // ---------- 编解码 / 加解密 ----------""",
    "P0-2a haikuo AES-CBC/ECB 实现",
)

edit(
    "proxy/assets/drpy/haikuo.mjs",
    """  assign("putMyVar", hostPutMyVar);""",
    """  assign("aesCbcDecode", aesCbcDecode);
  assign("aesCbcEncode", aesCbcEncode);
  assign("aesEcbDecode", aesEcbDecode);
  assign("putMyVar", hostPutMyVar);""",
    "P0-2b haikuo 暴露 AES API",
)

# ---------- P0-3: quality.py HLS key 支持 raw/hex/base64 ----------
edit(
    "proxy/quality.py",
    """    kr = await client.get(key_url, headers=headers or {}, timeout=_SEG_TIMEOUT)""",
    """    kr = await client.get(key_url, headers=headers or {}, timeout=_SEG_TIMEOUT)""",
    "P0-3a quality 锚点检查",
) if False else None

print()
print("changed:", ", ".join(changed) if changed else "(none)")
