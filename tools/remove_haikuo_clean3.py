"""海阔残留清理（第三轮）：处理不成对标记与孤立引用。"""
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
     "# 海阔视界小程序规则运行时（Node 跑海阔规则 JSON + pages[] JS 引擎模块）\n"
     "HAIKUO = True\n", "", "features HAIKUO flag")

# ---------- providers.py：优先级常量与分支 ----------
edit("proxy/providers.py", "HAIKUO_DEFAULT_PRIORITY = 15\n", "",
     "providers HAIKUO_DEFAULT_PRIORITY")
edit("proxy/providers.py",
     "    if source.get(\"drpy\"):\n        return DRPY_DEFAULT_PRIORITY\n"
     "        return HAIKUO_DEFAULT_PRIORITY\n",
     "    if source.get(\"drpy\"):\n        return DRPY_DEFAULT_PRIORITY\n",
     "providers haikuo priority branch")

# ---------- app.py：未成对标记的海阔浏览块 ----------
p = PROXY / "app.py"
if p.exists():
    s = p.read_text(encoding="utf-8")
    m = re.search(r"\n    if aguid == HAIKUO_LIB_GUID or parent == HAIKUO_LIB_GUID "
                  r"or haikuo_browser\.node\(parent\):.*?"
                  r"# <<< PRIVATE:HAIKUO\n", s, re.S)
    if m:
        s = s[:m.start()] + "\n" + s[m.end():]
        done.append("app haikuo browse block")
        print("  [ok] app haikuo browse block")
    # 孤立的海阔播放错误分支
    s = s.replace(
        "    # 解析失败（如海阔规则详情为空/不可播/需 X5）时直接给硬错误，不再\n"
        "    # 拿详情链接当直链去取、失败后给占位（客户端会一直转圈）。\n"
        "    if target.get(\"haikuo\") and not target.get(\"resolve_error\"):\n"
        "        await _resolve_if_needed(target, request)\n", "")
    s = s.replace(
        "    if target.get(\"resolve_error\"):\n"
        "        return Response(target.get(\"resolve_error_msg\") or \"海阔规则解析失败\",\n"
        "                        status_code=502)\n", "")
    s = s.replace("_resolve_haikuo 与 spider _resolve_spider 共用，故恢复为独立函数。）",
                  "spider _resolve_spider 独立使用，故为独立函数。）")
    p.write_text(s, encoding="utf-8")
    done.append("app haikuo play branches")
    print("  [ok] app haikuo play branches")

# ---------- catalog.py：注释 ----------
edit("proxy/catalog.py",
     "# drpy://、catvod://、haikuo:// 等其它协议源一律不参与。",
     "# drpy://、catvod:// 等其它协议源一律不参与。", "catalog comment")
edit("proxy/catalog.py",
     "（无 kind 时按 spider/drpy/catvod/xsrc/haikuo 标记判）",
     "（无 kind 时按 spider/drpy/catvod/xsrc 标记判）", "catalog docstring")

print()
print("done:", ", ".join(done) if done else "(none)")