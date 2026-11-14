#!/usr/bin/env python3
"""上游完整版对比工具：检查我们 fork 是否遗漏官方能力。

用法：
    python tools/upstream_diff.py                  # 对比报告
    python tools/upstream_diff.py --check          # CI 模式：有遗漏则退出码 1

上游基线目录（完整版 FPK 解包）：
    /var/minis/workspace/fnmedia-ext-upstream-full
可用环境变量 UPSTREAM_FULL 覆盖。

判定规则：
  · 官方模块文件：官方有、我们无 → 报告「缺模块」（需人工判断是否该移植）
  · 官方能力标记：官方有、我们无 → 报告「缺能力」
  · 我们的能力标记：我们有、官方无 → 报告「我们领先」（保护，不回退）
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UP = Path(os.environ.get("UPSTREAM_FULL",
                         "/var/minis/workspace/fnmedia-ext-upstream-full"))
OURS = ROOT / "proxy"

# 官方能力标记 → 说明。出现在上游即视为「官方具备该能力」。
CAPABILITY_MARKERS: dict[str, str] = {
    "skip_marks": "跳片头/片尾标记（play_config）",
    "source_probe": "CMS 源批量探活",
    "PANSOU_LINE": "PanSou 网盘手动线",
    "pan_cache_cleanup": "网盘播放缓存清理",
    "subscriptions": "订阅有界刷新",
    "subfetch": "订阅抓取代理 + GitHub 镜像",
    "webui_gateway": "桌面应用网关",
    "sps": "H.264 SPS 解析（去广告指纹层）",
    "adfilter": "播放去广告",
    "_inject_hls_start": "续播 EXT-X-START 注入",
    "crop=16x9": "16:9 横幅裁切",
    "_race_fastest": "线路竞速",
    "_find_playable": "故障切换",
    "updater": "更新器",
    "t4_browser": "T4 选片库",
    "xlib": "小雅网盘媒体库",
    "spiders": "csp_ 爬虫源",
    "catvod": "catvod JVM 运行时",
    "drpy": "drpyS JS 运行时",
    "dduo": "多多影视",
    "guard": "TVBox 加固（本 fork 已按计划移除）",
}

# 我们领先、官方没有的能力（保护项：官方将来补上也不算「我们落后」）
OUR_LEAD: dict[str, str] = {
    "_maybe_mid_switch": "播放中静默换线",
    "_pansou_fallback_target": "网盘线静默兜底",
    "reset_all": "一键重置",
    "pan_cache_ttl_days": "网盘缓存按天保留",
    "_probe_direct_media": "无后缀媒体探测",
    "_coerce_hls_key": "HLS key 三格式（raw/hex/base64）",
    "_split_poster_suffix": "海报 URL 后缀请求头",
    "_poster_ok": "成功海报内存 LRU",
    "_mirror_item_ts": "续播位置三处同步",
}


def _read_all(base: Path) -> str:
    out = []
    for p in sorted(base.glob("*.py")):
        try:
            out.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(out)


def main() -> int:
    if not UP.is_dir():
        print(f"[skip] 上游完整版基线不存在：{UP}")
        print("       （用 UPSTREAM_FULL 环境变量指定）")
        return 0
    up_text = _read_all(UP / "proxy")
    our_text = _read_all(OURS)

    missing_modules = []
    for p in sorted((UP / "proxy").glob("*.py")):
        if not (OURS / p.name).exists():
            missing_modules.append(p.name)

    missing_caps = []
    for marker, desc in CAPABILITY_MARKERS.items():
        if marker in up_text and marker not in our_text:
            missing_caps.append((marker, desc))

    our_lead = []
    for marker, desc in OUR_LEAD.items():
        if marker in our_text and marker not in up_text:
            our_lead.append((marker, desc))

    print("=" * 62)
    print(f"上游完整版：{UP}")
    print(f"本 fork   ：{ROOT}")
    print("=" * 62)

    print("\n[模块文件对比]")
    known_exempt = {"guard.py"}   # TVBox 加固：按 fork 计划不引入
    real_missing = [m for m in missing_modules if m not in known_exempt]
    if missing_modules:
        print("  官方有、我们无：")
        for m in missing_modules:
            tag = "（已知豁免：不引入）" if m in known_exempt else "需判断是否移植"
            print(f"    - {m:22s} {tag}")
    else:
        print("  ✓ 无缺失模块")

    print("\n[能力标记对比]")
    if missing_caps:
        print("  ⚠ 官方具备、我们缺：")
        for m, d in missing_caps:
            print(f"    - {m:26s} {d}")
    else:
        print("  ✓ 官方能力我们均已具备")

    print("\n[我们领先（不回退）]")
    if our_lead:
        for m, d in our_lead:
            print(f"  ✓ {m:26s} {d}")
    else:
        print("  （无）")

    print()
    if "--check" in sys.argv and (missing_caps or real_missing):
        print("[FAIL] 存在未回补的官方能力")
        return 1
    print("[OK] 对比完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
