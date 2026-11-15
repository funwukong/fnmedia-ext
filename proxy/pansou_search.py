"""按上游设计把 PanSou 搜索结果映射成【真实可播的一集】。

上游完整版（/var/minis/workspace/fnmedia-ext-upstream-full/proxy/pansou.py）
的 pansou.py 就是这么做的：PanSou 搜到的每条分享链接作为该标题下的一集，
线路名固定「PanSou网盘」，播放时由网盘分享解析路径（夸克/UC/阿里）转存出
直链 —— 所以「网盘线路能在线路里直接播」。

我们 fork 之前只有「网盘高清」虚拟手动线（用户点一下才搜索+转存），
与上游不一致；本模块补上同名映射，让网盘结果出现在线路里。

与手动线状态机（_pansou_manual_*）并存：
  · 手动线：用户显式切到「网盘高清（PanSou）」才搜索/转存；
  · 本模块：搜索聚合阶段就把网盘结果作为真实集给出（上游行为）。
"""
from __future__ import annotations

import logging

_log = logging.getLogger("fnmedia.pansou")

# 网盘类型 → 展示名（与服务端 merged_by_type 的键一致：aliyun 而非 ali）
_CLOUD_ORDER = ("quark", "uc", "aliyun")
_CLOUD_LABEL = {"quark": "夸克", "uc": "UC", "aliyun": "阿里"}
_LINE_NAME = "PanSou网盘"
_SOURCE_ID = "builtin-pansou"
_MAX_PER_TITLE = 20


def enabled() -> bool:
    """网盘搜索源是否启用（与手动线同一开关语义）。"""
    try:
        from config import config
        return bool(config.read("pansou_search_enabled",True))
    except Exception:
        return True


def available() -> bool:
    """网盘转存模块齐备且搜索源启用时，搜索映射才可用。"""
    if not enabled():
        return False
    try:
        import pan      # noqa: F401
        import panuc    # noqa: F401
    except Exception:
        return False
    return True


def _query_sync(title: str) -> list[tuple[str, str, str]]:
    """查 PanSou，返回 [(云类型, url, note)]。

    复用 pansou._pansou_search 的 HTTP 通道（同一实例、同一超时策略），
    但保留云类型与备注（上游按相关度排序需要）。
    """
    import pansou as _ps
    try:
        rows = _ps._pansou_search_rows(title)
    except Exception:
        _log.debug("pansou search rows failed", exc_info=True)
        return []
    out: list[tuple[str, str, str]] = []
    for ctype, url, note in rows:
        if url and url not in [u for _, u, _ in out]:
            out.append((ctype, url, note))
    return out


def _relevance(note: str, keyword: str) -> int:
    """标题相关度：包含关键词记 2，仅部分重合记 1，否则 0。"""
    n = (note or "").strip()
    k = (keyword or "").strip()
    if not n or not k:
        return 0
    if k in n:
        return 2
    hit = sum(1 for ch in set(k) if ch in n)
    return 1 if hit >= max(2, len(k) // 2) else 0


async def search(client, source: dict, keyword: str) -> list:
    """供 providers.search_all 调用：PanSou 结果 → OnlineTitle 列表。

    同一标题的多条分享合并为一部标题下的多集（上限 _MAX_PER_TITLE），
    避免结果墙被同名分享刷屏。快速失败：任何异常返回 []，不拖累聚合搜索。
    """
    try:
        from providers import Episode, OnlineTitle
    except Exception:
        return []
    if not available():
        return []
    kw = (keyword or "").strip()
    if not kw:
        return []

    import asyncio
    try:
        rows = await asyncio.to_thread(_query_sync, kw)
    except Exception:
        _log.debug("pansou search failed", exc_info=True)
        return []
    if not rows:
        return []

    # 去重 + 按相关度排序（相同链接保留相关度高的）
    best: dict[str, tuple[int, tuple[str, str, str]]] = {}
    for ctype, url, note in rows:
        score = _relevance(note, kw)
        if score <= 0:
            continue
        key = url.split("?", 1)[0]
        cur = best.get(key)
        if cur is None or score > cur[0]:
            best[key] = (score, (ctype, url, note))
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)
    if not ranked:
        return []

    provider = source.get("id", _SOURCE_ID)
    titles: list = []
    seen: dict[str, int] = {}
    for _score, (ctype, url, note) in ranked:
        base_title = (note or kw).strip() or kw
        label = _CLOUD_LABEL.get(ctype, "网盘")
        if base_title in seen:
            t = titles[seen[base_title]]
            eps = t.sources.setdefault(_LINE_NAME, [])
            if len(eps) < _MAX_PER_TITLE:
                eps.append(Episode(
                    title=f"{label} {len(eps) + 1}", url=url,
                    # direct=False + resolve=True：播放时走
                    # providers.resolve_share 转存出直链（direct=True 会被
                    # 当现成直链，绕过转存 → 403/失效）。
                    direct=False, resolve=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                    # 不设 xsrc：xsrc 线路会走多多解码路径；网盘分享链接
                    # 应走 resolve=True → _resolve_pan_share 转存。
                    play_from=provider))
            continue
        ep = Episode(
            title=label, url=url, direct=False, resolve=True,
            headers={"User-Agent": "Mozilla/5.0"},
            play_from=provider)
        t = OnlineTitle(
            provider=provider,
            vod_id=url,
            title=base_title,
            type_name="网盘", year="", area="", updated="",
            remarks=f"{label}网盘", score="", poster="", overview="",
            actor="", director="",
            sources={_LINE_NAME: [ep]},
        )
        seen[base_title] = len(titles)
        titles.append(t)
    return titles


def source_def() -> dict:
    """注册进 DEFAULT_SOURCES 的内置源定义。"""
    return {
        "id": _SOURCE_ID,
        "name": "PanSou网盘搜索",
        "api": "pansou://local",
        "enabled": enabled(),
        "xsrc": True,
        "builtin": True,
        "pansou": True,
    }
