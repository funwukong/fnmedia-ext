#!/usr/bin/env python3
"""详情页“关联作品”：从已收录的在线片库里给当前片名找关联作品。

只用本地已有的数据（store 里已物化的 brief，即用户浏览过的分类/搜索
结果），不调任何外部接口、不编造片名。关联度按同系列 > 同分类 >
同类型名 > 同地区/同年份/同演员/导演打分，取前 12 个，同名只留一个。

结果以 inj.search_item 的卡片形态返回，由调用方挂在详情响应的
"recommendations" 字段上。该字段名是按“官方详情响应常嵌套相关推荐
数组”这一公开观察（fnos-desktop 作者的注释）推断的，未在官方文档
里核实；飞牛客户端是否渲染，以真机实测为准。

冷启动：related_for 只看已浏览池（store._titles），新装/未浏览时池子
是空的。related_for_with_dense 在入选不足时把分类 dense（catalog 已
物化的去重 brief，纯本地、零网络）并入同一池再统一打分；打分、门槛、
上限、海报要求与 related_for 完全一致。
"""
from __future__ import annotations

import re

from metadata import normalize_title

DEFAULT_LIMIT = 12
# 入选门槛：光“同类型”(+8)或“同地区”(+8)不够，必须至少有一项
# 实信号（同分类 25 / 同类型名 12 / 同系列 60 / 同演员 10 叠加后达标）。
MIN_SCORE = 20

# 续集/系列后缀：片名归一化后，再去掉末尾的“2/II/第二部/第2季”等，
# 得到系列 key（如“狂飙2”“狂飙第二部”都归到“狂飙”）。
_SEQUEL_SUFFIX = re.compile(
    r"(?:第?[0-9一二三四五六七八九十]+[部季集篇]|"
    r"[IVX]+|[0-9]+|第[0-9一二三四五六七八九十]+季)\s*$"
)
_NAME_SPLIT = re.compile(r"[,，、/／|｜;；\s]+")


def _family_key(title: str) -> str:
    """系列 key：归一化片名再去续集后缀。"""
    t = normalize_title(title)
    return _SEQUEL_SUFFIX.sub("", t).strip() or t


def _names(s: str) -> set[str]:
    return {x for x in _NAME_SPLIT.split(s or "") if len(x) >= 2}


def _year(v) -> int:
    try:
        y = int(str(v or "")[:4])
    except (TypeError, ValueError):
        return 0
    return y if 1900 <= y <= 2100 else 0


def _score(brief: dict, cand: dict) -> int:
    """关联度分数。brief=当前片，cand=候选。"""
    s = 0
    if _family_key(cand.get("title", "")) == _family_key(brief.get("title", "")):
        s += 60
    if cand.get("catalog_cat") and cand.get("catalog_cat") == brief.get("catalog_cat"):
        s += 25
    if cand.get("type_name") and cand.get("type_name") == brief.get("type_name"):
        s += 12
    # 同类型（电影/剧集）小加分；跨类型但同系列仍可入选（家族分已给足）。
    try:
        import inject as _inj
        if _inj.fnos_type(cand) == _inj.fnos_type(brief):
            s += 8
    except Exception:
        pass
    if cand.get("area") and cand.get("area") == brief.get("area"):
        s += 8
    y1, y2 = _year(brief.get("year")), _year(cand.get("year"))
    if y1 and y2 and abs(y1 - y2) <= 1:
        s += 5
    shared_actors = _names(brief.get("actor", "")) & _names(cand.get("actor", ""))
    s += min(20, 10 * len(shared_actors))
    if brief.get("director") and brief.get("director") == cand.get("director"):
        s += 10
    return s


def _float_score(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def related_for(brief: dict, pool, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """给 brief 找关联作品，返回 brief 列表（已按关联度排好序）。

    pool 是已收录的 brief 可迭代对象（如 store._titles.values()）。
    排除自身；同名（归一化后）只留分数最高的一个；无海报的不入选。
    """
    if not brief or not brief.get("guid"):
        return []
    best: dict[str, tuple[int, dict]] = {}
    for cand in pool or []:
        if not isinstance(cand, dict):
            continue
        if cand.get("guid") == brief.get("guid"):
            continue
        title = (cand.get("title") or "").strip()
        if not title or not (cand.get("poster") or "").strip():
            continue
        sc = _score(brief, cand)
        if sc < MIN_SCORE:
            continue
        key = normalize_title(title)
        prev = best.get(key)
        if prev is None or sc > prev[0]:
            best[key] = (sc, cand)
    ranked = sorted(
        best.values(),
        key=lambda p: (-p[0], -_float_score(p[1].get("score")), p[1].get("title", "")),
    )
    return [c for _, c in ranked[:max(1, limit)]]


def related_for_with_dense(brief: dict, pool, dense_pool=None,
                           limit: int = DEFAULT_LIMIT) -> list[dict]:
    """关联作品（含冷启动兜底）：先按 pool（已浏览）打分；入选不足 limit
    时，把 dense_pool（分类 dense 已物化的 brief，纯本地、零网络）并入
    同一池再统一打分、统一排序、统一截断。

    触发条件是入选不足（池子为空/过小，或池内无匹配），不是池子大小本身；
    打分、MIN_SCORE、limit 上限、必须有海报、同名去重规则与 related_for
    完全一致；pool 与 dense_pool 里同一 guid 只算一次。
    """
    pool = [b for b in (pool or []) if isinstance(b, dict)]
    items = related_for(brief, pool, limit=limit)
    if len(items) >= limit or not dense_pool:
        return items
    seen = {b.get("guid") for b in pool}
    merged = list(pool)
    for cand in dense_pool:
        if isinstance(cand, dict) and cand.get("guid") not in seen:
            merged.append(cand)
            seen.add(cand.get("guid"))
    return related_for(brief, merged, limit=limit)
