"""删除全部海阔（Haikuo）相关代码与入口。

策略：
  1. 删除 proxy/haikuo*.py、proxy/assets/haikuo/、proxy/assets/drpy/haikuo.mjs；
  2. 剥离所有 `# >>> PRIVATE:HAIKUO` … `# <<< PRIVATE:HAIKUO` 标记块
     （HTML 注释形式 `<!-- >>> PRIVATE:HAIKUO -->` 一并处理）；
  3. 清理残留：haikuo_browser / haikuo_cache / haikuo_diag 的 import 与引用、
     features.HAIKUO 开关、vlibs 的 K_HAIKUO/_HAIKUO_GUID。

幂等：重复执行安全。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = ROOT / "proxy"
removed: list[str] = []

# ---------- 1) 删除海阔专属文件与资产 ----------
for rel in ["proxy/haikuo.py", "proxy/haikuo_browser.py", "proxy/haikuo_cache.py",
            "proxy/haikuo_diag.py", "proxy/assets/drpy/haikuo.mjs"]:
    p = ROOT / rel
    if p.exists():
        p.unlink()
        removed.append(rel)
for rel in ["proxy/assets/haikuo"]:
    p = ROOT / rel
    if p.is_dir():
        shutil.rmtree(p)
        removed.append(rel + "/")

# ---------- 2) 剥离标记块 ----------
_BLOCK = re.compile(
    r"^[ \t]*(?:#|//|<!--)[ \t]*>>>[ \t]*PRIVATE:HAIKUO.*?"
    r"^[ \t]*(?:#|//|<!--)[ \t]*<<<[ \t]*PRIVATE:HAIKUO[ \t]*(?:-->)?[ \t]*\n",
    re.S | re.M)


def strip_blocks(text: str) -> str:
    out = []
    pos = 0
    for m in _BLOCK.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


for py in sorted(PROXY.glob("*.py")):
    src = py.read_text(encoding="utf-8")
    new = strip_blocks(src)
    if new != src:
        py.write_text(new, encoding="utf-8")
        removed.append(f"blocks:{py.name}")

ui = PROXY / "admin_ui.py"
if ui.exists():
    src = ui.read_text(encoding="utf-8")
    new = strip_blocks(src)
    if new != src:
        ui.write_text(new, encoding="utf-8")
        removed.append("blocks:admin_ui.py")

# ---------- 3) 清理残留引用 ----------
CLEAN_RULES: list[tuple[str, str]] = [
    (PROXY / "app.py",
     "from haikuo_browser import HaikuoBrowser, HaikuoBrowseError, "
     "LIB_GUID as HAIKUO_LIB_GUID, LIB_TITLE as HAIKUO_LIB_TITLE\n", ""),
    (PROXY / "app.py", "import haikuo_cache\n", ""),
    (PROXY / "app.py", "haikuo_browser = HaikuoBrowser(store, config)\n", ""),
    (PROXY / "admin_api.py", "import haikuo as _haikuo\n", ""),
    (PROXY / "admin_api.py", "import haikuo_browser as _haikuo_browser\n", ""),
    (PROXY / "admin_api.py", "import haikuo_diag as _haikuo_diag\n", ""),
    (PROXY / "admin_api.py", "import haikuo_cache as _haikuo_cache\n", ""),
]

for path, old, new in CLEAN_RULES:
    if not path.exists():
        continue
    src = path.read_text(encoding="utf-8")
    if old in src:
        path.write_text(src.replace(old, new), encoding="utf-8")
        removed.append(f"clean:{path.name}")

# features.py：去掉 HAIKUO 开关
fp = PROXY / "features.py"
if fp.exists():
    src = fp.read_text(encoding="utf-8")
    src = src.replace(
        "# 海阔视界小程序规则运行时（Node sidecar haikuo 引擎分支）\nHAIKUO = True\n", "")
    src = src.replace('"HAIKUO", ', "")
    src = src.replace('"HAIKUO"', "")
    fp.write_text(src, encoding="utf-8")
    removed.append("features:HAIKUO")

# vlibs.py：去掉海阔库键常量
vp = PROXY / "vlibs.py"
if vp.exists():
    src = vp.read_text(encoding="utf-8")
    src = re.sub(r"# 海阔选片库：.*?\n_HAIKUO_GUID = .*?\n", "", src, flags=re.S)
    src = re.sub(r"\nK_HAIKUO = .*?\n", "\n", src)
    src = src.replace("    if guid == _HAIKUO_GUID:\n        return K_HAIKUO\n", "")
    src = src.replace("K_T4, K_HGDJ, K_HAIKUO", "K_T4, K_HGDJ")
    src = src.replace("固定键：t4 / hgdj / haikuo", "固定键：t4 / hgdj")
    vp.write_text(src, encoding="utf-8")
    removed.append("vlibs:K_HAIKUO")

print("removed/cleaned:")
for r in removed:
    print("  -", r)