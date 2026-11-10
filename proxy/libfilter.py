# -*- coding: utf-8 -*-
"""搜索结果 / 详情标题 → 虚拟库归属映射与按库过滤（decisions-2-libfilter）。

设计（读代码后定稿，见下；身份语义本身不在此——只调 app.py 的
``_viewer_can_open_lib`` / ``_viewer_can_open_key``，它们再调
``vlibs.account_can_see``；本模块不认识任何账号、不改 vlibs.py）。

一、brief 携带的归属信号
------------------------
搜索聚合 brief（``inject.brief_of``）只有：``guid``、``online_id``
（``"<provider>:<vod_id>"``）、``title``、``type_name``、``lines``
（聚合后键形如 ``"<provider>::<线路名>"``）、``locator``（T4 为
``{"kind": "t4", ...}``）。它**没有** ``catalog_*`` 字段，但有
``catalog_route``（P0-6 补，见规则 3）。
分类库 brief（``catalog._briefs_from_vods``）另有：``catalog_cat``、
``catalog_scope``（聚合作用域 ``catalog.AGG_SCOPE == "__agg__"``，
或单源作用域=源 id）、``catalog_source``（源 id），以及同源的
``catalog_route``。
榜单 brief（``hot.py``）不进搜索/详情主路径，不在此映射。

二、归属规则（一个标题可归属多张库；可见 = 任一归属库可打开）
------------------------------------------------------------
1. T4 标题：``locator.kind == "t4"`` → 仅 ``{"t4"}``。
   T4 brief 在聚合时单列（``__aggregate_briefs_inner`` 把 kind=t4 的
   OnlineTitle 另行成卡，**不与 CMS 线路合并**），故无 CMS 归属。
2. 分类库标题（带 ``catalog_*`` 字段）：
   - 聚合作用域（``catalog_scope == "__agg__"``）→
     ``{"catalog:<catalog_cat>", "standalone:<catalog_source>"}``。
     该标题既出现在聚合分类库里，也出现在该源的独立库浏览里。
   - 单源作用域（``catalog_scope`` 为源 id）→
     ``{"standalone:<catalog_scope 或 catalog_source>"}``。
     这是只在某源独立库里浏览到的标题，不进聚合分类库。
3. 聚合搜索标题（无 ``catalog_*`` 字段）：
   - 每条线路的 provider（``lines`` 的 ``"<provider>::..."`` 前缀，
     回退 ``online_id`` 的 provider 部分）→
     ``"standalone:<provider>"``。每个启用源都有一张独立库
     （``standalone_lib_guid(source_id)``），标题的线路即出自该源，
     独立库是标题的首要归宿。
   - 加固源另加 ``"guard:<gkey>"``（需调用方提供
     ``gkey_of(provider)`` 查源定义；provider 本身就是源 id，
     guard 专区库按 ``gkey`` 分组）。
   - 再加一张分类库门：按类型名用与 ``inject.hot_category`` 相同的
     规则归入 movie/tv/anime/variety 之一 → ``"catalog:<cat>"``。
     **这是本次的关键取舍**：非独立库 CMS 源出的标题同时归属
     “源自身的独立库”和“聚合分类库”，取并集（ANY），不是二选一。
     理由：聚合分类库（海量电影/剧集/动漫/综艺）正是用同一批 CMS 源
     的分类路由深翻建成的（``catalog.py`` AGG 路由），该类型标题
     本来就是分类库要列出的内容；只认独立库会让“仅开 catalog:movie
     的账号搜不到任何东西”（搜索为空，但同一批标题在它能打开的
     分类库里明明可以浏览），按库过滤就失去了“搜索可见性 = 可浏览
     性”的含义。反之只认分类库会让“仅开某源独立库”的账号搜不到
     该源标题，同样不对。ANY 语义下，标题被隐藏当且仅当它的
     每一扇门都对查看者关闭。
   - 再加来源分类路由门：brief 带 ``catalog_route``（``{"source",
     "type_id", "cat"}``，聚合搜索由 app 层按 vod 的 type_id 反查
     catalog 路由表补，分类库 brief 由 catalog 产生点直接补）时，
     ``cat`` 有效即另加 ``"catalog:<cat>"``（并集）。路由是源分类的
     真实归属，可覆盖全部 11 个分类库（含 documentary/uhd/
     shortdrama/overseas/ai_comic/ted/adult）；路由缺失或 cat 为空
     （旧缓存）时回退到类型名映射，行为与改前一致。
4. 榜单库（``chart:<cat>``）**不是**搜索标题的归属：榜单标题是
   豆瓣条目（自有 guid），搜索 brief 是资源站标题，两者不是同一
   批记录。榜单详情里“已绑定资源站标题”的分支（``item_detail`` 的
   chart-bound 分支）在 app 层额外并上 ``chart:<cat>``——那是
   榜单库这扇真实存在的门，不属于本模块。
5. 扩展分类库（documentary/uhd/shortdrama/overseas/ai_comic/ted/
   adult）**不从类型名推断**（类型名→扩展分类没有可靠规则，
   强行推断会错放），改走 ``catalog_route`` 路由门（见规则 3）。
   另：类型名含“纪录片”此前被 ``_MOVIE_CAT_WORDS`` 命中归入
   ``catalog:movie``，现 ``catalog_cat_for_type`` 先判纪录片词，
   归入 ``catalog:documentary``（与 ``inject.hot_category`` 的
   四分类有意分歧：热门库只有 movie/tv/anime/variety 四张，
   纪录片在热门侧仍归 movie，搜索过滤侧归 documentary）。

三、无法映射的处理：拒绝（防御性兜底，与身份默认无关）
----------------------------------------
``brief_lib_keys`` 返回空集时，``brief_visible`` 返回 False：标题不
在搜索里出现，详情返回 -6。空集只发生在两类情形：brief 不是 dict，
或分类库 brief 缺了 ``catalog_cat`` 与 ``catalog_source`` 两者；
聚合搜索 brief 一定有归属（分类库门按 hot_category 规则，类型缺失
也默认归 tv，见上），正常标题不会走到这条分支；这只是防御性兜底。
理由：
- 无法证明查看者能打开它所属的库；按 fail-open 语义（未知身份/
  未配置账号按默认开关放行），“不知道归属”仍只能按隐藏算，
  否则一张被隐藏的库的标题会经由“无归属”漏网。
- 实践中搜索 brief 一定有 ``online_id``（``brief_of`` 必填），
  正常标题不会走到这条分支；这只是防御性兜底。
- 与之对照：详情里 ``store`` 查不到 brief 的 guid（本地库条目、
  上游透传）**不走本模块**，直接沿用原有透传逻辑——本地库不是
  虚拟库，不受虚拟库授权管。
"""
from __future__ import annotations

# catalog.AGG_SCOPE 的取值（catalog.py）；此处用字面量保持本模块
# 只依赖标准库，tests/test_libfilter.py 会断言两者一致。
AGG_SCOPE = "__agg__"

# 与 inject.hot_category 完全相同的类型词表与优先级（anime > variety >
# movie > tv），仅一处有意分歧：纪录片词不进 _MOVIE_CAT_WORDS，
# catalog_cat_for_type 先判纪录片 → documentary（见该函数文档）。
# tests/test_libfilter.py 会对一组类型名断言两者一致（纪录片除外），
# 防止任一方改动后漂移。
_ANIME_WORDS = ("动漫", "番剧", "动画剧", "动画", "日本动漫", "日韩动漫",
                "国产动漫", "欧美动漫", "动漫片")
_VARIETY_WORDS = ("综艺", "真人秀")
_MOVIE_CAT_WORDS = ("电影", "影片", "剧情片", "动作片", "喜剧片", "爱情片",
                    "科幻片", "恐怖片", "动画电影")
_DOC_WORDS = ("纪录片", "纪录", "纪实")

# 分类库全部 cat（含扩展分类），供 catalog_route 校验；与
# app.CATALOG_LIB_GUIDS 的键一致（tests 会断言两者一致）。
_KNOWN_CATALOG_CATS = frozenset((
    "movie", "tv", "anime", "variety", "documentary", "uhd",
    "shortdrama", "overseas", "ai_comic", "ted", "adult"))


def catalog_cat_for_type(type_name: str) -> str:
    """类型名 → 聚合分类库 cat（movie/tv/anime/variety/documentary 之一）。

    与 inject.hot_category 同规则，仅一处有意分歧：类型名含“纪录片”
    归 documentary 而非 movie（hot_category 的四分类里没有纪录片库，
    仍归 movie；搜索过滤侧有 catalog:documentary 这张库）。
    扩展分类（uhd/shortdrama/…）仍不从类型名推断，走 catalog_route。
    """
    typ = (type_name or "").strip()
    if any(w in typ for w in _ANIME_WORDS):
        return "anime"
    if any(w in typ for w in _VARIETY_WORDS):
        return "variety"
    if any(w in typ for w in _DOC_WORDS):
        return "documentary"
    if any(w in typ for w in _MOVIE_CAT_WORDS):
        return "movie"
    return "tv"


def brief_lib_keys(brief: dict, *, gkey_of=None) -> set[str]:
    """brief → 该标题归属的虚拟库键集合（可为空集，见模块文档第三条）。

    ``gkey_of``：可选回调 ``provider -> gkey | None``，
    用于加固源的专区库归属；不提供时只按独立库 + 分类库映射。
    本函数是纯函数：不读配置、不识身份、不碰网络。
    """
    if not isinstance(brief, dict):
        return set()
    locator = brief.get("locator") or {}
    if isinstance(locator, dict) and locator.get("kind") == "t4":
        # T4 标题不与 CMS 线路合并，唯一归属就是 T4 选片库。
        return {"t4"}

    # 来源分类路由门（P0-6）：cat 有效即另加一张分类库；缺失/非法/为空
    # 时 route_cat 为 ""，只走类型名映射（旧缓存回退现有行为）。
    route_cat = ""
    route = brief.get("catalog_route") or {}
    if isinstance(route, dict):
        cand = route.get("cat") or ""
        if cand in _KNOWN_CATALOG_CATS:
            route_cat = cand

    cat = brief.get("catalog_cat") or ""
    scope = brief.get("catalog_scope") or ""
    source = brief.get("catalog_source") or ""
    if cat or scope or source:
        # 分类库 brief：归属由 catalog_* 字段直接决定，不再看线路/类型。
        if scope == AGG_SCOPE:
            keys = set()
            if cat:
                keys.add(f"catalog:{cat}")
            if source:
                keys.add(f"standalone:{source}")
            if route_cat:
                keys.add(f"catalog:{route_cat}")
            return keys
        # 单源作用域：只在该源独立库里出现。
        sid = source or scope
        return {f"standalone:{sid}"} if sid else set()

    # 聚合搜索 brief：线路 provider → 独立库；类型 → 聚合分类库。
    providers: set[str] = set()
    lines = brief.get("lines") or {}
    if isinstance(lines, dict):
        for lname in lines:
            if isinstance(lname, str) and "::" in lname:
                providers.add(lname.split("::", 1)[0])
    oid = brief.get("online_id") or ""
    if isinstance(oid, str) and ":" in oid:
        providers.add(oid.split(":", 1)[0])
    providers.discard("")

    keys = set()
    for provider in providers:
        keys.add(f"standalone:{provider}")
        if gkey_of is not None:
            try:
                gkey = gkey_of(provider)
            except Exception:
                gkey = None
            if gkey:
                keys.add(f"guard:{gkey}")
    cat = catalog_cat_for_type(brief.get("type_name") or "")
    if cat:
        keys.add(f"catalog:{cat}")
    if route_cat:
        keys.add(f"catalog:{route_cat}")
    return keys


def brief_visible(brief: dict, can_open_key, *, gkey_of=None) -> bool:
    """该标题对当前查看者是否可见。

    ``can_open_key``：调用方提供的 ``库键 -> bool``（app 层传
    ``_viewer_can_open_key`` 的偏应用，身份逻辑全在那一侧）。
    任一归属库可打开即可见；无归属（无法映射）或 ``can_open_key``
    为 None（无查看者上下文）时一律不可见（与身份默认无关）。
    """
    if can_open_key is None:
        return False
    keys = brief_lib_keys(brief, gkey_of=gkey_of)
    if not keys:
        return False
    for key in keys:
        try:
            if can_open_key(key):
                return True
        except Exception:
            continue
    return False


def filter_briefs(briefs, can_open_key, *, gkey_of=None) -> list:
    """过滤掉查看者打不开的库的标题（保持原顺序；不改动 brief 本身）。"""
    if not briefs:
        return []
    return [b for b in briefs
            if brief_visible(b, can_open_key, gkey_of=gkey_of)]
