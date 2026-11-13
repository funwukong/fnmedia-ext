"""从 sidecar.mjs 删除海阔引擎分支（第四轮）。

sidecar.mjs 里海阔是「batch 2」加的独立引擎分支：
loadHaikuo / getHaikuoSession / handleHaikuoRequest / haikuoSessions。
海阔方案作废后这部分整体删除，sidecar 回到只跑 drpy。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "proxy/assets/drpy/sidecar.mjs"
s = p.read_text(encoding="utf-8")
before = len(s)

# 1) 删除 loadHaikuo / haikuoSessions / getHaikuoSession / handleHaikuoRequest
m = re.search(r"// storeDir：req\.storeDir > HAIKUO_STORE_DIR.*?"
              r"^async function handleHaikuoRequest\(req\) \{.*?"
              r"^\}\n", s, re.S | re.M)
if m:
    s = s[:m.start()] + s[m.end():]
    print("  [ok] 删除海阔引擎函数块")
else:
    print("  [miss] 海阔引擎函数块")

# 2) 删除 handleRequest 里的海阔分发
s = s.replace(
    "  // Haikuo 引擎（batch 2）：规则加载 / 解析串解释 / lazy 评估 + sidecar\n"
    "  // 内按 ruleKey 缓存会话；不走 drpy 的站点缓存与方法分发。\n"
    "  if (req && req.engine === \"haikuo\") return await handleHaikuoRequest(req);\n", "")

# 3) 清理可能残留的引用
s = s.replace("haikuoMod", "_removedHaikuoMod")
s = re.sub(r"\n?// storeDir：req\.storeDir > HAIKUO_STORE_DIR.*?\n", "\n", s,
           flags=re.S)

p.write_text(s, encoding="utf-8")
print(f"  [ok] sidecar.mjs {before} → {len(s)} 字节")
print("  残留 haikuo:", s.lower().count("haikuo"))