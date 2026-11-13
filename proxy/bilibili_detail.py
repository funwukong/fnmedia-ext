"""B 站影片详情：把单集占位替换为真实分P 列表。

关键点：飞牛的选集/播放链是从 store 里的 brief 读 episode 的（detail 响应
本身不带 lines）。因此 B 站详情必须把真实分P **写回 store 的 brief**，
否则详情显示 N 集、播放时却只有 1 集。
"""
from __future__ import annotations

import logging

import bilibili_site
import inject

_log = logging.getLogger("fnmedia.bilibili.detail")


def is_bilibili_brief(brief: dict | None) -> bool:
    """brief 是否来自 B 站独立站点（按线路名判定）。"""
    if not isinstance(brief, dict):
        return False
    lines = brief.get("lines") or {}
    return bilibili_site.LINE_NAME in lines


def _episode_dict(url: str, title: str) -> dict:
    return {
        "title": title,
        "url": url,
        "direct": True,
        "resolve": False,
        "headers": {"User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.bilibili.com/"},
        "redirect": False,
        "xsrc": True,
        "play_from": bilibili_site.SOURCE_ID,
        "episode_key": "",
    }


async def build_detail(brief: dict, client=None):
    """B 站影片详情：真实分P + 元数据，并写回 store brief。

    返回 detail 数据 dict；失败返回 None（调用方回退默认详情）。
    """
    if not is_bilibili_brief(brief):
        return None
    eps = (brief.get("lines") or {}).get(bilibili_site.LINE_NAME) or []
    ref = ""
    for ep in eps:
        u = str(ep.get("url") or "")
        if u.startswith(bilibili_site._BILI_URI):
            ref = u
            break
    if not ref:
        return None
    try:
        info = await bilibili_site.detail(ref)
    except Exception as exc:  # noqa: BLE001
        _log.warning("bilibili detail failed: %s", exc)
        return None
    pages = await _page_list(ref)
    episode_eps = []
    for idx, p in enumerate(pages, start=1):
        part = str(p.get("part") or "").strip() or ("P%d" % idx)
        episode_eps.append(_episode_dict(
            "%s%s?p=%d" % (bilibili_site._BILI_URI, info["bvid"], idx),
            part))
    if not episode_eps:
        episode_eps = [_episode_dict(
            "%s%s" % (bilibili_site._BILI_URI, info["bvid"]),
            bilibili_site.LINE_NAME)]
    lines = {bilibili_site.LINE_NAME: episode_eps}
    # 写回 store：选集/播放链从 brief 取 episode，详情响应不带 lines。
    try:
        import store as _st
        _st.store.remember_merged([_brief_with_lines(brief, lines)])
    except Exception:
        _log.debug("bilibili detail store update failed", exc_info=True)
    data = inject._base_item(
        guid=brief.get("guid", ""),
        type="TV" if len(episode_eps) > 1 else "Movie",
        title=info.get("title") or brief.get("title") or "",
        can_play=1,
        number_of_seasons=1 if len(episode_eps) > 1 else 0,
        number_of_episodes=len(episode_eps),
        local_number_of_episodes=len(episode_eps),
        release_date=info.get("year") or brief.get("year") or "",
        air_date=info.get("year") or brief.get("year") or "",
        ancestor_guid="", ancestor_name=bilibili_site.SOURCE_NAME,
        ancestor_category="Mix",
        poster=inject._poster(info.get("poster") or brief.get("poster") or ""),
        overview=info.get("overview") or brief.get("overview") or "",
        logic_type=0)
    data["play_item_guid"] = brief.get("guid", "")
    data["play_error"] = ""
    return data


def _brief_with_lines(brief: dict, lines: dict) -> dict:
    out = dict(brief)
    out["lines"] = lines
    if brief.get("overview"):
        out["overview"] = brief["overview"]
    return out


async def _page_list(ref: str) -> list:
    import asyncio
    import bilibili_play
    try:
        return await asyncio.to_thread(bilibili_play.page_list, ref)
    except Exception:  # noqa: BLE001
        return []
