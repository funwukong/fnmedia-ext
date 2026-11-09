#!/usr/bin/env python3
"""元数据补全（P2）——只做本地合并，不编造、不 invent 外部接口。

原则：
- 不接任何需要 key 的外部元数据接口；TMDB key 留空即不用（config.tmdb_api_key）。
- 只补“同名其他源已有”的字段：年份、地区、评分、简介、海报、演员、导演。
- 绝不改写已有非空字段；绝不凭标题猜年份、猜评分。
- 标题归一化只做去噪（“第X集 / 预告 / 解说 / 花絮”等后缀），不做语义改写。

入口：
- normalize_title(title) -> 归一化标题；
- enrich_brief(brief, candidates) -> 补齐后的 brief（原 dict 不变，返回新 dict）；
- dedupe_titles(titles) -> 按归一化标题合并，选信息最全的一条做主条目。
"""
from __future__ import annotations

import re

_FILL_FIELDS = ("year", "area", "score", "overview", "poster", "actor", "director")
_STRIP_SUFFIX = re.compile(
    r"(第\s*\d+\s*集|更新至\s*\d+\s*集|全\s*\d+\s*集|预告|解说|花絮|抢先版|枪版|"
    r"HD|BD|4K|1080P|720P|中字|国语|粤语)$"
)
_STRIP_BRACKET = re.compile(r"[\[\(（][^\]\)）]*[\]\)）]\s*$")


def normalize_title(title: str) -> str:
    t = (title or "").strip()
    # 去掉末尾的括号备注与集数/画质后缀，循环去直到稳定。
    for _ in range(3):
        new = _STRIP_BRACKET.sub("", t).strip()
        new = _STRIP_SUFFIX.sub("", new).strip()
        if new == t:
            break
        t = new
    return t


def _is_better(new: str, old: str) -> bool:
    """新值是否值得补：旧值空、新值非空；简介取更长的。"""
    if not (new or "").strip():
        return False
    if not (old or "").strip():
        return True
    return False


def enrich_brief(brief: dict, candidates: list[dict]) -> dict:
    """用同名候选条目补齐 brief 的空字段；不改已有值。"""
    out = dict(brief or {})
    target = normalize_title(out.get("title", ""))
    if not target:
        return out
    for cand in candidates or []:
        if normalize_title(cand.get("title", "")) != target:
            continue
        for f in _FILL_FIELDS:
            if _is_better(cand.get(f, ""), out.get(f, "")):
                out[f] = cand.get(f)
        # 简介单独取更长的（同名不同源的简介常有详略之分）。
        if len(str(cand.get("overview", "") or "")) > len(str(out.get("overview", "") or "")):
            out["overview"] = cand.get("overview")
    return out


def dedupe_titles(titles: list[dict]) -> list[dict]:
    """按归一化标题去重：信息最全的做主条目，其余的只用来补字段。"""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for t in titles or []:
        key = normalize_title(t.get("title", "")) or str(t.get("title", ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)
    out = []
    for key in order:
        g = groups[key]
        # 主条目：非空字段最多的那条。
        main = max(g, key=lambda d: sum(1 for f in _FILL_FIELDS if (d.get(f) or "").strip()))
        out.append(enrich_brief(main, g))
    return out
