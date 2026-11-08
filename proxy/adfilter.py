#!/usr/bin/env python3
"""HLS(m3u8) 广告分片过滤 —— 移植 TVBox / OK影视 Pro 的 `rules` 去广告机制。

两层数据来源：
1. 订阅自带规则：TVBox 配置顶层 ``rules: [{name, hosts:[], regex:[]}]``。导入订阅时
   由本模块合并持久化（去重），播放时按当前 m3u8 URL 的 host 命中对应规则。
2. 内置通用规则/启发式：不依赖订阅，按标准广告标记（SCTE-35 CUE/DATERANGE）、
   广告片段 URL 关键字（/ad/、/ads/、advert、sponsor、redtraffic、guanggao 等）
   以及可疑的极短分片做安全清理。

每条 ``regex`` 既可能是：
- 纯时长（如 ``5.480000``）：删除匹配该 EXTINF 时长的分片（TVBox 行为）；
- 含 ``#EXT...`` 的完整正则：直接在整段 m3u8 上做 DOTALL 替换。

安全原则：任何规则异常/结果明显损坏（剩余媒体分片为 0）时，回退原始列表，
绝不因为去广告导致无法播放。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import config as _cfg

# ---- 持久化 --------------------------------------------------------------
_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_RULES_FILE = _STATE_DIR / "ad_rules.json"   # 订阅合并来的去广告规则
_ENABLE_KEY = "ad_filter_enabled"            # 总开关存 config.json

_lock = threading.RLock()

# ---- 内置启发式 ----------------------------------------------------------
# 广告分片 URL 关键字（小写子串）。命中 EXTINF 后的 URL 即删该分片。
_AD_URL_HINTS = (
    "/ad/", "/ads/", "/adjump", "/advert", "/advertisement", "/advideo",
    "sponsor", "redtraffic", "guanggao", "/gg/", "preroll", "/promo/",
    "adsegment", "/adevent", "/vast", "openx", "/insertad", "adservice",
)
# SCTE-35 / 标准广告插播标记
_CUE_OUT = ("#EXT-X-CUE-OUT", "#EXT-X-CUE-OUT-CONT", "#EXT-X-DATERANGE",
            "#EXT-X-SCTE35", "#EXT-OATCLS-SCTE35", "#EXT-X-CUE-SPAN")
_CUE_IN = ("#EXT-X-CUE-IN",)
_CUE_SKIP_TAGS = ("#EXT-X-ASSET", "#EXT-X-CUE", "#EXT-X-SCTE35",
                  "#EXT-OATCLS-SCTE35")
# 纯时长：匹配 5.480000 这类数字（允许整数/小数）
_PURE_DURATION = re.compile(r"^\d+(?:\.\d+)?$")
# EXTINF 时长
_EXTINF_DUR = re.compile(r"#EXTINF:([\d.]+)")


def enabled() -> bool:
    """总开关，默认开启；可在管理页关闭。"""
    try:
        return bool(_cfg.config.get().get(_ENABLE_KEY, True))
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    _cfg.config.save({_ENABLE_KEY: bool(on)})


def _load_rules() -> list[dict]:
    try:
        if _RULES_FILE.exists():
            r = json.loads(_RULES_FILE.read_text(encoding="utf-8"))
            return r if isinstance(r, list) else []
    except Exception:
        pass
    return []


def _save_rules(rules: list[dict]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _RULES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_RULES_FILE)


def stats() -> dict:
    with _lock:
        rules = _load_rules()
    hosts = set()
    n_regex = 0
    for r in rules:
        hosts.update(r.get("hosts") or [])
        n_regex += len(r.get("regex") or [])
    return {"enabled": enabled(), "rule_groups": len(rules),
            "hosts": sorted(hosts), "regex_count": n_regex}


def _norm_group(g: dict) -> dict | None:
    if not isinstance(g, dict):
        return None
    hosts = [str(h).strip().lower() for h in (g.get("hosts") or []) if str(h).strip()]
    regex = [str(x) for x in (g.get("regex") or []) if str(x).strip()]
    name = str(g.get("name") or (hosts[0] if hosts else "")).strip()
    if not regex:
        return None
    return {"name": name, "hosts": hosts, "regex": regex}


def merge_subscription_rules(rules) -> int:
    """导入订阅时合并顶层 rules；按 (hosts集合, regex集合) 去重。返回新增组数。"""
    if not isinstance(rules, list):
        return 0
    with _lock:
        cur = _load_rules()

        def sig(g):
            return (tuple(sorted(g.get("hosts") or [])),
                    tuple(g.get("regex") or []))

        existing = {sig(g) for g in cur}
        added = 0
        for raw in rules:
            g = _norm_group(raw)
            if not g:
                continue
            s = sig(g)
            if s in existing:
                continue
            existing.add(s)
            cur.append(g)
            added += 1
        if added:
            _save_rules(cur)
        return added


def replace_rules(rules) -> int:
    """管理页整体替换（清洗非法项）。返回保留组数。"""
    out = []
    for raw in (rules if isinstance(rules, list) else []):
        g = _norm_group(raw)
        if g:
            out.append(g)
    with _lock:
        _save_rules(out)
    return len(out)


def _host_matches(hosts: list[str], media_url: str) -> bool:
    try:
        host = urlsplit(media_url).netloc.lower()
    except Exception:
        host = ""
    if not host:
        return False
    # 去掉端口
    host_no_port = host.split(":", 1)[0]
    for h in hosts:
        h = h.lower()
        if host == h or host_no_port == h or host.endswith("." + h) or h in host:
            return True
    return False


def _rules_for(media_url: str) -> list[str]:
    pat: list[str] = []
    with _lock:
        for g in _load_rules():
            if _host_matches(g.get("hosts") or [], media_url):
                pat.extend(g.get("regex") or [])
    return pat


def _apply_host_regex(text: str, patterns: list[str]) -> str:
    """对命中 host 的 TVBox 规则逐条 DOTALL 替换。"""
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        try:
            if _PURE_DURATION.match(pat):
                # 删除“EXTINF 时长恰为该值”的分片（EXTINF 行 + 紧随的 URL 行）。
                text = _drop_segments_by_duration(text, {float(pat)})
            else:
                text = re.sub(pat, "", text, flags=re.S)
        except re.error:
            # 个别源的正则在 Python 引擎不兼容（如占有量词）：忽略该条，不影响其它
            continue
        except Exception:
            continue
    return text


def _drop_segments_by_duration(text: str, durations: set[float],
                               tol: float = 0.02) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        m = _EXTINF_DUR.search(ln)
        if m:
            try:
                d = float(m.group(1))
            except ValueError:
                d = -1.0
            hit = any(abs(d - x) <= tol for x in durations)
            if hit and i + 1 < n and not lines[i + 1].startswith("#"):
                i += 2  # 跳过 EXTINF + 分片URL
                continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def _apply_heuristic(text: str) -> tuple[str, int]:
    """不依赖订阅规则的通用清理。返回 (新文本, 删除分片数)。"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    removed = 0
    in_cue = False

    def next_url_index(start: int) -> int:
        j = start
        while j < n and (lines[j].startswith("#") or not lines[j].strip()):
            j += 1
        return j

    while i < n:
        ln = lines[i]
        s = ln.strip()

        # SCTE-35 广告区间
        if any(s.startswith(t) for t in _CUE_OUT):
            in_cue = True
            i += 1
            continue
        if any(s.startswith(t) for t in _CUE_IN):
            in_cue = False
            i += 1
            continue
        if any(s.startswith(t) for t in _CUE_SKIP_TAGS):
            i += 1
            continue
        if in_cue:
            # 广告区间内：吞掉 EXTINF+URL（以及裸标签）
            if s.startswith("#EXTINF"):
                removed += 1
                i += 2
                continue
            i += 1
            continue

        # 广告 URL 关键字：删 EXTINF+URL
        if s.startswith("#EXTINF"):
            j = next_url_index(i + 1)
            if j < n:
                url = lines[j].strip().lower()
                if any(k in url for k in _AD_URL_HINTS):
                    removed += 1
                    # 连带紧邻的 DISCONTINUITY 由后续孤立标签清理
                    i = j + 1
                    continue
        out.append(ln)
        i += 1

    # 清理因此产生的孤立/重复 DISCONTINUITY（成对的间断标记保留一个即可）
    cleaned: list[str] = []
    for ln in out:
        if ln.strip() == "#EXT-X-DISCONTINUITY":
            if cleaned and cleaned[-1].strip() == "#EXT-X-DISCONTINUITY":
                continue
        cleaned.append(ln)
    return "\n".join(cleaned), removed


def _segment_url_count(text: str) -> int:
    c = 0
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            c += 1
    return c


# ---- 播放时动态检测（OK影视 Pro 式：当下分析分片序列识别广告段） ----------
def _dynamic_enabled() -> bool:
    try:
        return bool(_cfg.config.get().get("ad_filter_dynamic", True))
    except Exception:
        return True


def _host_of(url: str) -> str:
    try:
        return urlsplit(url.strip()).netloc.lower().split(":", 1)[0]
    except Exception:
        return ""


def _ad_like_duration(total: float) -> bool:
    return 10.0 <= total <= 180.0


def _split_segments(lines: list[str]):
    """把 m3u8 行解析为 (segs[(dur,url)], blocks[[seg_idx...]])。
    块以 #EXT-X-DISCONTINUITY 为界。"""
    n = len(lines)
    segs: list[tuple[float, str]] = []
    blocks: list[list[int]] = [[]]
    i = 0
    while i < n:
        st = lines[i].strip()
        if st == "#EXT-X-DISCONTINUITY":
            if blocks[-1]:
                blocks.append([])
            i += 1
            continue
        if st.startswith("#EXTINF"):
            m = _EXTINF_DUR.search(st)
            dur = float(m.group(1)) if m else 0.0
            j = i + 1
            while j < n and (lines[j].strip().startswith("#") or not lines[j].strip()):
                j += 1
            url = lines[j].strip() if j < n else ""
            blocks[-1].append(len(segs))
            segs.append((dur, url))
            i = j + 1
            continue
        i += 1
    blocks = [b for b in blocks if b]
    return segs, blocks


def _detect_ad_blocks(text: str) -> tuple[str, int]:
    """播放当下按 DISCONTINUITY 分段、多信号评分识别广告段并删除。

    信号：片头/片中位置、分片时长模式异于正片主体、CDN 域名切换、广告典型
    时长、URL 广告关键字。需多信号叠加（或关键字强信号）才删，避免误伤。
    """
    lines = text.splitlines()
    segs, blocks = _split_segments(lines)
    if len(segs) < 8 or len(blocks) < 2:
        return text, 0

    main_blk = max(range(len(blocks)), key=lambda k: len(blocks[k]))
    main_durs = sorted(segs[idx][0] for idx in blocks[main_blk] if segs[idx][0] > 0)
    main_dur_med = main_durs[len(main_durs) // 2] if main_durs else 0.0
    main_hosts: dict[str, int] = {}
    for idx in blocks[main_blk]:
        h = _host_of(segs[idx][1])
        if h:
            main_hosts[h] = main_hosts.get(h, 0) + 1
    main_host = max(main_hosts, key=main_hosts.get) if main_hosts else ""

    pre_duration = 0.0
    drop: set[int] = set()
    for k, blk in enumerate(blocks):
        if k == main_blk:
            pre_duration += sum(segs[idx][0] for idx in blk)
            continue
        total = sum(segs[idx][0] for idx in blk)
        durs = sorted(segs[idx][0] for idx in blk if segs[idx][0] > 0)
        med = durs[len(durs) // 2] if durs else 0.0
        hosts = {_host_of(segs[idx][1]) for idx in blk if _host_of(segs[idx][1])}
        url_blob = " ".join(segs[idx][1] for idx in blk).lower()

        score = 0
        if pre_duration < 60.0 and k < main_blk:
            score += 2            # 片头
        elif k != main_blk:
            score += 1            # 片中/片尾边界
        if main_dur_med and med and med < main_dur_med * 0.75:
            score += 2            # 切片明显更小
        if durs and len({round(x, 3) for x in durs}) <= max(2, len(durs) // 3):
            score += 1            # 广告切片高度同构
        if main_host and hosts and main_host not in hosts:
            score += 2            # CDN 域名切换
        if _ad_like_duration(total):
            score += 1
        has_kw = any(kw in url_blob for kw in _AD_URL_HINTS)
        if has_kw:
            score += 3

        if (has_kw and _ad_like_duration(total)) or score >= 4:
            drop.update(blk)
        pre_duration += total

    if not drop:
        return text, 0

    # 重建：删除广告分片（EXTINF+其URL），保留其它标签
    out: list[str] = []
    n = len(lines)
    i = 0
    cur = 0
    while i < n:
        st = lines[i].strip()
        if st.startswith("#EXTINF"):
            j = i + 1
            while j < n and (lines[j].strip().startswith("#") or not lines[j].strip()):
                j += 1
            if cur in drop:
                i = j + 1
                cur += 1
                continue
            out.extend(lines[i:j + 1])
            cur += 1
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    # 清理孤立/重复/打头的 DISCONTINUITY
    cleaned: list[str] = []
    for ln in out:
        if ln.strip() == "#EXT-X-DISCONTINUITY":
            if not cleaned or cleaned[-1].strip() == "#EXT-X-DISCONTINUITY":
                continue
        cleaned.append(ln)
    while cleaned and cleaned[0].strip() == "#EXT-X-DISCONTINUITY":
        cleaned.pop(0)
    return "\n".join(cleaned) + ("\n" if text.endswith("\n") else ""), len(drop)


def filter_hls(text: str | None, media_url: str = "",
               use_heuristic: bool = True) -> tuple[str, int]:
    """对一份 m3u8 文本去广告。

    返回 (过滤后文本, 删除的媒体分片数估计)。任何异常都回退原文。
    子清单（master，含 #EXT-X-STREAM-INF）不做分片处理。
    """
    if not text or not enabled():
        return text or "", 0
    try:
        original = text
        if "#EXT-X-STREAM-INF" in text:
            return text, 0  # 主清单不分片

        before = _segment_url_count(text)

        # 1) 订阅 host 规则（TVBox rules）
        if media_url:
            patterns = _rules_for(media_url)
            if patterns:
                text = _apply_host_regex(text, patterns)

        # 2) 通用启发式（SCTE35/关键字）
        if use_heuristic:
            text, removed_h = _apply_heuristic(text)
        else:
            removed_h = 0
        # 3) 播放时动态检测（无 rules 时按分段+时长+CDN 识别广告段）
        removed_dyn = 0
        if _dynamic_enabled() and _segment_url_count(text) >= 8:
            text, removed_dyn = _detect_ad_blocks(text)

        after = _segment_url_count(text)
        removed = max(0, before - after) + removed_h

        # 安全护栏：媒体分片被清空说明规则误伤，回退
        if before > 0 and _segment_url_count(text) == 0:
            return original, 0
        return text, removed
    except Exception:
        return original, 0


# ---- 选集列表层：去掉“关注公众号/加微信/加群”等假选集广告 ----------------
# 命中即视为广告选集（非正片）。仅作用于选集【名称】，不碰正片名。
_EP_AD_PATTERNS = (
    r"关注(?:公众号|微信|官方|公众|抖音|快手)",
    r"(?:官方)?公众号",
    r"加(?:微信|群|VX|vx|V信|企微|客服)",
    r"(?:添加|联系)(?:微信|客服|QQ|qq)",
    r"扫码(?:关注|领取|添加)",
    r"微信[:：]?\s*[A-Za-z0-9_\-]{4,}",
    r"(?:QQ|qq)群?\s*[:：]?\s*\d{5,}",
    r"(?:VX|vx|v信|薇信)\s*[:：]?\s*[A-Za-z0-9_\-]{4,}",
    r"领(?:取|红包)(?:福利|资源|大片|影视)",
    r"(?:永久|免费)(?:网址|地址|入口|官网|看片)",
    r"(?:更多|精彩)(?:资源|影片|大片)?(?:请|前往|访问|关注)",
    r"(?:复制|打开)(?:浏览器|链接).{0,12}(?:搜索|访问|打开|下载)",
    r"www\.[A-Za-z0-9.\-]+\.(?:com|net|cn|cc|xyz|top|vip|tv|me|app)",
    r"[A-Za-z0-9.\-]+\.(?:com|net|cn|cc|xyz|top|vip|tv)\s*$",
    r"下载\s*(?:APP|app|应用|客户端)",
    r"(?:广告|推广|宣发|引流)(?:位|片|视频|选集)?$",
    r"本(?:集|片|视频)由.{0,20}(?:赞助|提供|广告)",
)
_EP_AD_RE = [re.compile(p, re.I) for p in _EP_AD_PATTERNS]

# 白名单：选集名里出现这些正片信号时，即使含“加/关注”等也不轻易删（降低误伤）。
# 仅当名字里没有任何集数/正片结构，且明确命中强引流模式时才删。
_EP_REAL = re.compile(
    r"(?:第?\s*\d{1,4}\s*[集期话]|^\s*\d{1,4}\s*$|正片|预告|花絮|花絮|彩蛋|"
    r"HD|BD|TC|TS|清晰|国语|粤语|中英|字幕|线路|CD\d|完结|大结局|"
    r"上$|下$|中$|前$|后$)")


def is_ad_episode(name: str) -> bool:
    """选集名是否为引流广告（公众号/微信/加群/网址/下载APP 等）。"""
    if not enabled():
        return False
    n = (name or "").strip()
    if not n or len(n) > 60:
        return False
    if _EP_REAL.search(n):
        return False
    return any(rx.search(n) for rx in _EP_AD_RE)
