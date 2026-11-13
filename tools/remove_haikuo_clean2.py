"""清理海阔删除后的残留引用（第二轮）。

第一轮剥离了成对的 PRIVATE:HAIKUO 标记块，但有若干处是单侧标记或散落
引用，这一轮逐个清掉：
  * features.py 的 HAIKUO 开关（标记在 FEATURE_FLAGS 块内，没被单独剥）；
  * admin_ui.py 的海阔 JS（两侧标记不在同一行，块正则没匹配）；
  * admin_ui.py 的 FEAT_HAIKUO 常量；
  * providers.py / catalog.py 的 haikuo 源判定；
  * hls_race.py 的海阔诊断（_haikuo_deliver_note / _is_haikuo_pick）；
  * updater.py 的海阔资源目录；
  * bilibili.py 的海阔规则 cookie 落盘路径（改为本模块 state）。

幂等。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = ROOT / "proxy"
done: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    if old not in s:
        print(f"  [miss] {tag}")
        return
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    done.append(tag)
    print(f"  [ok] {tag}")


# ---------- features.py ----------
edit("proxy/features.py",
     "# 海阔视界小程序规则运行时（Node sidecar haikuo 引擎分支）\nHAIKUO = True\n", "",
     "features.HAIKUO")

# ---------- admin_ui.py：海阔 JS 块（两侧标记不配对，手工切） ----------
p = PROXY / "admin_ui.py"
if p.exists():
    s = p.read_text(encoding="utf-8")
    m = re.search(r"/\* >>> PRIVATE:HAIKUO \*/.*?/\* <<< PRIVATE:HAIKUO \*/", s, re.S)
    if m:
        s = s[:m.start()] + s[m.end():]
        p.write_text(s, encoding="utf-8")
        done.append("admin_ui haikuo js block")
        print("  [ok] admin_ui haikuo js block")
    edit("proxy/admin_ui.py",
         "const FEAT_DDUO=true,FEAT_ADULT=true,FEAT_HAIKUO=true;",
         "const FEAT_DDUO=true,FEAT_ADULT=true;", "admin_ui FEAT_HAIKUO")

# ---------- providers.py：haikuo 源判定 ----------
edit("proxy/providers.py",
     "    if source.get(\"haikuo\"):\n", "", "providers haikuo branch")

# ---------- catalog.py：haikuo 非 CMS 判定 ----------
edit("proxy/catalog.py",
     "_NON_CMS_KINDS = {\"t4\", \"spider\", \"drpy\", \"catvod\", \"xsrc\", \"haikuo\",\n"
     "                  \"bili_site\"}",
     "_NON_CMS_KINDS = {\"t4\", \"spider\", \"drpy\", \"catvod\", \"xsrc\",\n"
     "                  \"bili_site\"}", "catalog _NON_CMS_KINDS")
edit("proxy/catalog.py",
     "                or source.get(\"haikuo\") or source.get(\"xsrc\"))",
     "                or source.get(\"xsrc\"))", "catalog haikuo marker")

# ---------- hls_race.py：海阔诊断 ----------
p = PROXY / "hls_race.py"
if p.exists():
    s = p.read_text(encoding="utf-8")
    m = re.search(r"\ndef _is_haikuo_pick\(picked: dict\) -> bool:.*?"
                  r"\ndef _haikuo_deliver_note\(.*?(?=\ndef )", s, re.S)
    if m:
        s = s[:m.start()] + "\n" + s[m.end():]
        done.append("hls_race haikuo helpers")
        print("  [ok] hls_race haikuo helpers")
    # 调用点：_haikuo_deliver_note(...) → 换成空操作
    s = re.sub(r"_haikuo_deliver_note\((?:[^()]|\([^()]*\))*\)", "", s)
    s = s.replace("            if picked.get(\"haikuo\") and not picked.get(\"resolve_error\"):\n", "")
    s = s.replace("                # 已解析过（resolve_error 已设）或已解析成功（haikuo 标志已清）\n", "")
    s = s.replace("                await _resolve_if_needed(picked, request)\n", "")
    p.write_text(s, encoding="utf-8")
    done.append("hls_race haikuo calls")
    print("  [ok] hls_race haikuo calls")

# ---------- updater.py：海阔资源目录 ----------
p = PROXY / "updater.py"
if p.exists():
    s = p.read_text(encoding="utf-8")
    s = s.replace('[ -d "$ROOT/proxy/assets/haikuo" ] && TARARGS+=(proxy/assets/haikuo)\n', "")
    s = s.replace("  --exclude='proxy/assets/haikuo/' --exclude='*local.json'\n",
                  "  --exclude='*local.json'\n")
    s = s.replace("      --exclude='proxy/assets/haikuo' \\\n", "")
    p.write_text(s, encoding="utf-8")
    done.append("updater haikuo assets")
    print("  [ok] updater haikuo assets")

# ---------- bilibili.py：cookie 落盘改为本模块 state ----------
edit("proxy/bilibili.py",
     '_HAIKUO_DIR = Path(__file__).resolve().parent / "assets" / "haikuo" / "sub"\n'
     '_COOKIE_FILE = _HAIKUO_DIR / "Bilibili" / "Cookie" / "bilibili_cookie.txt"',
     '# Cookie 只在本模块 state 内保存（不再写海阔规则目录，那套已删除）\n'
     '_COOKIE_FILE = Path(__file__).resolve().parent.parent / "state" '
     '/"bilibili_cookie.txt"',
     "bilibili cookie path")

print()
print("done:", ", ".join(done) if done else "(none)")