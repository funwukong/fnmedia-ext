"""修复海阔 GBK 往返：TextDecoder 不支持 GBK，gbk.js 的 decode 只认 %XX 形态。

根因：
  * Node 未编译 full-icu → new TextDecoder('gbk') 抛错 → 宿主回退 latin1；
  * gbkTool.decode 只处理 "%XX%XX" 形态，而宿主传的是 latin1 字节串；
  两者一叠加，decodeStr(encodeStr('你好','GBK'),'GBK') 必然失败
  （实测得到 "ÄãºÃ"）。

修法：
  1. gbk.js 增加 decodeBytes：直接吃 latin1 字节串，按 GBK 双字节表还原；
  2. 宿主 hostDecodeStr 对 GBK/GB2312 走 gbkTool.decodeBytes；
  3. hostEncodeStr 保持既有 %XX→latin1 形态（规则侧既有约定不变）。
"""
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
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    changed.append(tag)
    print(f"  [ok] {tag}")


# ---------- gbk.js：加 decodeBytes（latin1 字节串 → GBK 文本） ----------
edit(
    "proxy/assets/drpy/gbk.js",
    """        decode:function(str){
            return str.replace(/%[0-9A-F]{2}%[0-9A-F]{2}/g,function(a){
                if(a in G2Uhash){
                    return String.fromCharCode('0x'+G2Uhash[a]);
                }else{
                    return a;
                }
            }).replace(/%[\\w]{2}/g,function(a){
                return decodeURIComponent(a);

            });
        }""",
    """        decode:function(str){
            return str.replace(/%[0-9A-F]{2}%[0-9A-F]{2}/g,function(a){
                if(a in G2Uhash){
                    return String.fromCharCode('0x'+G2Uhash[a]);
                }else{
                    return a;
                }
            }).replace(/%[\\w]{2}/g,function(a){
                return decodeURIComponent(a);

            });
        },
        // latin1 字节串 → GBK 文本：宿主传的是原始字节（每个字符码位就是
        // 一个字节），这里按 GBK 双字节表还原成中文。
        decodeBytes:function(bytes){
            var buf = (bytes instanceof Uint8Array) ? bytes
                : (typeof bytes === 'string'
                    ? (function(s){var a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i)&0xff;return a;})(bytes)
                    : new Uint8Array(0));
            var out = '';
            var i = 0;
            while (i < buf.length) {
                var b = buf[i];
                if (b < 0x80) { out += String.fromCharCode(b); i += 1; continue; }
                var key = null;
                if (b >= 0x81 && b <= 0xFE && i + 1 < buf.length) {
                    var b2 = buf[i + 1];
                    if (b2 >= 0x40 && b2 <= 0xFE && b2 !== 0x7F) {
                        key = ('%' + b.toString(16).toUpperCase())
                            + ('%' + b2.toString(16).toUpperCase());
                    }
                }
                if (key && key in G2Uhash) {
                    out += String.fromCharCode('0x' + G2Uhash[key]);
                    i += 2;
                } else {
                    out += String.fromCharCode(b);
                    i += 1;
                }
            }
            return out;
        }""",
    "gbk.js decodeBytes",
)

# ---------- 宿主：GBK/GB2312 走 gbkTool.decodeBytes ----------
edit(
    "proxy/assets/drpy/haikuo.mjs",
    """  function hostDecodeStr(input, charset) {
    const cs = String(charset || "UTF-8").toUpperCase();
    const bytes = Buffer.from(String(input ?? ""), "latin1");
    try {
      if (cs === "UTF-8" || cs === "UTF8") return bytes.toString("utf8");
      return new TextDecoder(cs.toLowerCase()).decode(bytes);
    } catch { return bytes.toString("latin1"); }
  }""",
    """  function hostDecodeStr(input, charset) {
    const cs = String(charset || "UTF-8").toUpperCase();
    const bytes = Buffer.from(String(input ?? ""), "latin1");
    if (cs === "UTF-8" || cs === "UTF8") return bytes.toString("utf8");
    // GBK/GB2312：Node 的 TextDecoder 不带 full-icu 时不支持 gbk，
    // 且 gbkTool.decode 只认 %XX 形态；这里走字节表直接还原。
    if (cs === "GBK" || cs === "GB2312" || cs === "GB18030") {
      try { return _gbk.decodeBytes(bytes); }
      catch (e) { debug("decodeStr GBK failed:", e && e.message); }
    }
    try {
      return new TextDecoder(cs.toLowerCase()).decode(bytes);
    } catch { return bytes.toString("latin1"); }
  }""",
    "hostDecodeStr 走 gbkTool",
)

print()
print("changed:", ", ".join(changed) if changed else "(none)")
