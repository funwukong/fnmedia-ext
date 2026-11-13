"""P0 二批第二批：HLS key 支持 raw/hex/base64 + IV 语义修正 + 无后缀媒体探测。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
changed: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    if new in s:
        print(f"  [skip] {tag}")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}\n---\n{old[:300]}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    changed.append(tag)
    print(f"  [ok] {tag}")


# ---------- P0-3: quality.py —— key 三格式 + IV 语义 ----------
edit(
    "proxy/quality.py",
    '''async def _parse_hls_key(client, text: str, media_url: str,
                         headers) -> tuple[bytes, bytes] | None:
    """从媒体列表解析 AES-128 EXT-X-KEY，返回 (key, 默认IV全0标志)。"""
    m = None
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY") and "METHOD=AES-128" in line:
            m = line
            break
    if not m:
        return None
    mu = re.search(r\'URI="([^"]+)"\', m)
    if not mu:
        return None
    key_url = urllib.parse.urljoin(media_url, mu.group(1))
    try:
        kr = await client.get(key_url, headers=headers or {},
                              timeout=_SEG_TIMEOUT)
        if kr.status_code != 200 or len(kr.content) != 16:
            return None
        key = kr.content
        mi = re.search(r"IV=0x([0-9a-fA-F]+)", m)
        iv = bytes.fromhex(mi.group(1)) if mi else b"\\0" * 16
        if len(iv) != 16:
            return None
        return key, iv
    except Exception:
        _log.debug("_parse_hls_key failed", exc_info=True)
        return None''',
    '''def _coerce_hls_key(raw: bytes) -> bytes | None:
    """HLS key 常见三种返回形态：原始 16 字节 / 32 位 hex 文本 / base64 文本。

    原实现只认原始 16 字节，站点把 key 以 hex 或 base64 文本返回时就整条链
    退化成「客户端自己取 key」，而客户端往往没有正确 headers，最终播放失败。
    """
    if not raw:
        return None
    if len(raw) == 16:
        return raw
    text = raw.strip()
    if len(text) == 16:
        # 去掉 BOM/空白后正好 16 字节：按原始字节处理
        return text
    if len(text) == 32:
        try:
            out = bytes.fromhex(text.decode("ascii"))
            return out if len(out) == 16 else None
        except (ValueError, UnicodeDecodeError):
            return None
    # base64（16 字节 → 24 字符，可能带 padding）
    try:
        out = base64.b64decode(text, validate=False)
        if len(out) == 16:
            return out
    except Exception:  # noqa: BLE001
        pass
    # hex 前缀写法
    if text[:2].lower() == b"0x" and len(text) == 34:
        try:
            out = bytes.fromhex(text[2:].decode("ascii"))
            return out if len(out) == 16 else None
        except (ValueError, UnicodeDecodeError):
            return None
    return None


async def _parse_hls_key(client, text: str, media_url: str,
                         headers) -> tuple[bytes, bytes] | None:
    """从媒体列表解析 AES-128 EXT-X-KEY，返回 (key, iv)。

    iv 语义：清单里写了 IV=0x… 就用清单的（即使全 0 也是显式零向量）；
    没写 IV 返回 None，交给调用方按 media sequence 推导（HLS 规范）。
    """
    m = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-KEY") and "METHOD=AES-128" in s.upper():
            m = s
            break
    if not m:
        return None
    mu = re.search(r\'URI="([^"]+)"\', m)
    if not mu:
        return None
    key_url = urllib.parse.urljoin(media_url, mu.group(1))
    try:
        kr = await client.get(key_url, headers=headers or {},
                              timeout=_SEG_TIMEOUT)
        if kr.status_code != 200:
            return None
        key = _coerce_hls_key(kr.content)
        if not key:
            _log.debug("_parse_hls_key: unsupported key format len=%d",
                       len(kr.content), exc_info=True)
            return None
        mi = re.search(r"IV=0x([0-9a-fA-F]+)", m)
        iv = bytes.fromhex(mi.group(1)) if mi else None
        if iv is not None and len(iv) != 16:
            iv = None
        return key, iv
    except Exception:
        _log.debug("_parse_hls_key failed", exc_info=True)
        return None''',
    "P0-3 quality key 三格式 + IV 语义",
)

# quality.py 需要 base64
p = ROOT / "proxy/quality.py"
s = p.read_text(encoding="utf-8")
if "\nimport base64" not in s:
    s = s.replace("\nimport re\n", "\nimport base64\nimport re\n", 1)
    p.write_text(s, encoding="utf-8")
    changed.append("P0-3b quality import base64")
    print("  [ok] P0-3b quality import base64")

print()
print("changed:", ", ".join(changed) if changed else "(none)")
