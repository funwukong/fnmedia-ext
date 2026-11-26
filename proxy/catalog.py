#!/usr/bin/env python3
"""海量分类片库（方案 C）：直接按资源站苹果 CMS 分类深翻。

与豆瓣“热榜”（charts.py，精选 ~160 部、点击才搜源）不同，这里面向“无限浏览”：
- 启动/定时拉取各资源站的分类表 ac=list，把所有子分类按名称归并为
  movie / tv / anime / variety 四个标准大类；
- 每个大类选一个“规范源”（可深翻、页数多、稳定），浏览时按页直连
  ac=detail&t=<子类id>&pg=N，每页 20 条且自带播放地址，点开即可播，无需再搜索；
- 页级短 TTL 内存缓存 + 单 flight；规范源某页失败自动回退到备选源；
- 条目 guid 绑定 (源, vod_id)，brief 入库后复用既有详情/选集/播放/续播链。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any
from pathlib import Path

import httpx

import netutil
import providers
from store import make_guid

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("catalog")


def _monotonic() -> float:
    """页缓存 TTL 用单调钟：墙钟可能被 NTP 回拨，单调钟不受影响。
    只用于内存缓存时间戳；落盘的 _updated/built_at 仍用墙钟。"""
    return time.monotonic()


PAGE_SIZE = 20
# 每个大类最多可浏览的页数（页数×20=条数）。250 页 ≈ 5000 部。
MAX_PAGES = 250
# P1-17：分类深翻有界并发度。一批最多同时在途这么多页；CMS 源多为
# 慢源/共享带宽，并发度取 4 既能把逐页等待折叠，又不至于冲垮源站。
DEEP_FETCH_CONCURRENCY = 4
# 内存工人 3：路由探测有界并发。首轮每个 (源,类) 只探首选叶子，源多
# 时约 100 路突发；信号量 12 把在途探测压到有界范围，慢源不再互相
# 挤占连接，也不放大对源站的请求压力（只限流，不多发请求）。
PROBE_CONCURRENCY = 12
# 内存工人 3：单源独立库交错 merged 缓存 TTL。页缓存本身 10 分钟，
# merged 只取 60s：同一浏览会话内翻页直接切片，深翻不再每次从 0
# 重建 O(offset) 合并；路由刷新/_updated 变化时立即失效，不靠 TTL。
STANDALONE_MERGED_TTL = 60.0
# Retain only a bounded prefix per standalone scope. A deep request still
# returns its full window from the local build, but does not pin an unbounded
# merged list until TTL expiry.
_STANDALONE_MERGED_MAX = 5000
CLASS_REFRESH = 6 * 3600
PAGE_TTL = 10 * 60
ROUTE_STATE = Path(__file__).resolve().parent.parent / "state" / "catalog_routes.json"
ROUTE_STATE_TTL = 6 * 3600
CATALOG_SCHEMA_VERSION = 3
# 空页（多为慢源抖动超时）只短缓存，避免该页被长时间冻结
EMPTY_TTL = 15
HTTP_TIMEOUT = 12.0
# 缓存上限（memory-caches 批）：淘汰的都是可再生的分页/去重派生内容，
# 删后下次浏览重新翻页，不动用户数据。
_DENSE_TOTAL_MAX = 50000  # dense 去重列表全作用域总条数（10 作用域 × 5000；
# 实测 10 大类 × 5000 条重 brief 时 _dense 13.7MB + _seen_guids 9.2MB，
# 5 万条总量约 23MB 级，可控）
_PAGE_CACHE_MAX = 500      # 页缓存总页数（每页 ≤20 条 brief，500 页约万条量级）


def _log_background_task(task: asyncio.Task) -> None:
    """读取后台任务结果，避免异常变成未回收的 Task exception。"""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        _log.warning("catalog background task failed", exc_info=True)

# 大类 -> 展示名 / MediaDB category
CATALOG_GUIDS = {
    "movie": "e91dd7347a49dbd874fb8a0e78ad7ad7",
    "tv": "196381f891d66a4de32f9032e8f244f3",
    "anime": "97fc49015edcb20cc423658401711b6c",
    "variety": "cfff30730cffca2d1357e0c2b52fc2f5",
    "documentary": "155f2305c2cbc76c85f981ef31cc192f",
    "uhd": "262ce51312d7e72020514db5fd5f63e9",
    "shortdrama": "037ed7c6df4a9deb8d815c823fd15f79",
    "overseas": "c2fe61ae4d3adc87756a253cb108066e",
    "ai_comic": "6784f423ff4be5af4fe60cde6c8da303",
    "ted": "34883d4af44ca4a5539cb38827158294",
    "adult": "411515e17c85709cf1966697b7b2c58e",
    "jp_adult": "1da6f6024e13cd963384ca32151a9a68",
    "domestic": "89fc9a5d4d3253ccab527b5808c8d8c4",
    "europe_adult": "92f724d4d361966cf617f201fec0e76d",
    "sports": "9b93ebd34f12099811bcb2e700825169",
    "live": "465e85f3a6cb1a4ce5d4e397de29384b",
    "kids": "cd91e59c8f342225b8741f18d70a26be",
    "opera": "495704bcc69d765082a218219fbf3c1e",
    "music": "00a744b183e5c4ef1d375ab8514f515b",
}

CATALOG_DEFS = {
    "movie": ("🎞海量电影", "Movie"),
    "tv": ("📺海量剧集", "TV"),
    "anime": ("🌸海量动漫", "Mix"),
    "variety": ("🎤海量综艺", "Mix"),
    "documentary": ("🎞纪录片", "Movie"),
    "uhd": ("📽4K影视", "Movie"),
    "shortdrama": ("📱热门短剧", "TV"),
    "overseas": ("🌍海外剧集", "TV"),
    "ai_comic": ("🤖AI漫剧", "Mix"),
    "ted": ("🎓教育", "Mix"),
    "sports": ("⚽体育节目", "TV"),
    "live": ("📡直播", "TV"),
    "kids": ("🧒少儿", "Mix"),
    "opera": ("🎭戏曲", "Mix"),
    "music": ("🎵音乐MV", "Mix"),
}
CATALOG_CATEGORIES = [(cat, title, mdb)
                      for cat, (title, mdb) in CATALOG_DEFS.items()]

# 跨源聚合的“海量四库”使用的保留作用域 id；单源独立库作用域为其 source id。
AGG_SCOPE = "__agg__"
CATS = [c for c, _, _ in CATALOG_CATEGORIES]


def visible_categories(*, welfare: bool | None = None) -> list[tuple[str, str, str]]:
    """Return the canonical categories visible to the current feature set."""
    enabled = _welfare_on() if welfare is None else bool(welfare)
    out = list(CATALOG_CATEGORIES)
    if enabled:
        out.extend((cat, catalog_title(cat), catalog_mdb(cat))
                   for cat in WELFARE_CATS)
    return list(dict.fromkeys(out))

# 名称关键字（匹配 type_name 子串）。顺序：动漫/综艺优先于“电影/剧”判定。
_ANIME_KW = ("动漫", "动画", "番剧", "卡通", "哔哩", "次元动漫", "沙雕动画", "有声动漫")
_VARIETY_KW = ("综艺", "真人秀", "脱口秀", "娱乐", "音乐综艺")
_DOC_KW = ("纪录片", "纪录", "记录片", "纪录剧集", "记录", "纪实")
_UHD_KW = ("4k", "杜比视界", "dolby", "uhd", "超高清", "蓝光", "bluray", "邵氏4k", "4k新电影", "老电影4k")
# 片名级 4K 判定（标题/备注里的画质信号）：比分类名多认 2160 与 HDR。
# 注意不含“超清”——太多源把所有片都标超清，放进来 4K 库就等于全库。
_UHD_TITLE_KW = _UHD_KW + ("2160", "hdr")


def is_uhd_title(title: str, remarks: str) -> bool:
    """片名/备注是否带 4K 画质信号（4K/2160/UHD/杜比/HDR/蓝光/超高清）。"""
    s = f"{title or ''} {remarks or ''}".lower()
    return any(k in s for k in _UHD_TITLE_KW)
_SHORT_KW = ("短剧", "微短剧", "剧场", "短剧大全", "其他短剧", "爽文短剧")
# P2 新增：AI漫剧（先于 _EXCLUDE_KW 判定，见 classify），海外剧（先于普通剧集判定）。
_AI_COMIC_KW = ("AI漫剧", "AI动漫", "AI漫画", "动态漫画", "漫剧")
# P2 追加：TED（现显示为“教育”）（大小写不敏感，用小写匹配，见 classify）。
_TED_KW = ("ted", "ted演讲", "tedx", "ted-ed", "ted演讲")
# cat-core 新增：体育 / 直播 / 少儿 / 戏曲 / 音乐（判定顺序见 classify）。
# 体育与音乐必须在 _EXCLUDE_KW 判定之前（“体育/赛事”“音乐/MV”在排除表里）；
# 音乐用小写匹配（nl），参考 ted 的写法；裸“晚会”不在音乐表里（与综艺重叠）。
_SPORTS_KW = ("体育", "足球", "篮球", "赛事", "乒乓", "网球", "排球",
              "羽毛球", "田径", "拳击", "格斗", "斯诺克", "高尔夫", "赛车", "运动")
_LIVE_KW = ("直播", "现场直播", "电视直播")
_KIDS_KW = ("少儿", "儿童", "儿歌", "幼儿", "亲子", "童话")
_EDUCATION_KW = ("两性课堂", "采集教程", "科普学习", "教育学习")
_OPERA_KW = ("戏曲", "京剧", "越剧", "黄梅戏", "豫剧", "评剧", "昆曲",
             "秦腔", "川剧", "粤剧", "二人转", "沪剧", "淮剧")
_MUSIC_KW = ("mv", "音乐", "演唱会", "音乐会", "音乐节", "音乐现场", "音综", "打歌")
_OVERSEAS_KW = ("海外剧", "美剧", "英剧", "韩剧", "日剧", "泰剧",
                "港台剧", "欧美剧", "海外电视剧", "Netflix", "马泰剧", "东南亚剧", "美国剧", "日本剧", "韩国剧")
# 成人向分类名：显式导向 ADULT 福利分类，绝不落入常规电影/剧集类。
# 硬红线（_REDLINE_KW）在 is_redline 中先于本表拦截，任何开关都不放行。
# 扩围（第 5 批，真源分类表 2026-11-12，11/11 内置 CMS 源拉通）：
# - “理论”（baofeng 理论片）、“三级”（wujin 港台三级）——DIFF §5.2 重点候选，
#   真源命中后并入；“限制级”真源零命中，不加；R18/18禁/激情/性感/美女/
#   诱惑/床戏/大尺度/黄片/毛片真源亦零命中，一律不加。
# - “两性”（wujin 两性课堂）、“擦边”（guangsu/jisu/wujin/xinlang 擦边短剧）——
#   真源分类名里真实出现的成人类目，一并并入（见 tests/test_welfare_expansion.py）。
# 注意“理论”仍在 _EXCLUDE_KW 里（与“福利/写真/午夜”同款双轨），classify()
# 遇它返回 None，靠 classify_source_class() 的成人段捞回。
_ADULT_KW = ("福利", "伦理", "情色", "午夜", "写真", "里番", "成人",
             "理论", "三级", "两性", "擦边")
_JP_ADULT_KW = ("日本无码", "日本有码", "中字无码", "中字有码",
                "JAV视频", "JAV视频中字", "JAV自拍", "有码字幕", "视频一区",
                "视频二区", "视频三区", "视频四区", "留学生", "素人搭讪", "女优明星",
                "日本AV", "中文字幕", "水果派解说")
_EUROPE_ADULT_KW = ("欧美无码", "欧美性爱", "欧美情色", "欧美三级", "欧美AV", "欧美色情", "欧美成人")
_DOMESTIC_KW = ("国产专区", "写真热舞", "抖阴视频", "国产视频", "美女写真", "国产伦理",
                "国产偷拍", "国产户外", "国产探花", "国产自拍", "网友自拍", "酒店探花",
                "国产传媒", "麻豆传媒", "精东影业", "葫芦影业", "91制片厂", "SA传媒",
                "乌托邦传媒", "传媒厂商", "国产厂商", "大象传媒", "扣扣传媒", "星空传媒",
                "星空无限传媒", "果冻传媒", "渡边传媒", "爱神传媒", "爱豆传媒", "皇家华人",
                "蜜桃传媒", "蜜桃影像传媒", "起点传媒", "香蕉传媒", "国产主播", "台湾swag",
                "网红主播", "主播秀色", "主播视讯", "福利姬", "福利片", "国产精品",
                "国产专题", "国产丝袜", "国产人妻", "国产系列")
_BAN_CATEGORIES = frozenset({"乱伦原创", "乱伦系列", "兄妹姐弟", "公媳乱伦", "叔嫂乱伦",
    "国产乱伦", "家庭乱伦", "岳父岳母", "母子乱伦", "父女乱伦", "真实乱伦", "黑人专区",
    "特殊嗜好", "男同GAY", "SM重口", "同性恋", "国产SM", "鸡教练", "黑椒盖饭",
    "SM调教", "伪娘系列", "男同系列", "成人动漫", "资讯", "公告", "头条", "新闻资讯",
    "娱乐新闻", "电影资讯", "网红头条", "娱乐动态", "影片资讯", "影视快讯", "影视资讯",
    "明星资讯", "站内新闻", "系统通知", "网站公告", "预告资讯", "影视解说", "电影解说",
    "预告解说", "专题", "日韩专题", "八卦爆料", "明星八卦", "剧情介绍", "演员",
    "子类113", "子类12", "未分类", "另类小说", "长篇连载", "其他", "其他地区", "其它",
    "微短剧", "VR视角", "短视频"})
# 电影：电影/影片/各类型“XX片”且非剧非动漫；剧：电视/连续剧/美剧/国产剧…
_MOVIE_KW = ("电影", "影片", "动作片", "喜剧片", "恐怖片", "科幻片", "战争片", "爱情片", "犯罪片", "悬疑片", "奇幻片", "灾难片", "惊悚片", "历史片", "古装片", "西部片", "家庭片", "冒险片", "武侠")
_TV_KW = ("电视", "连续剧", "剧集", "美剧", "英剧", "韩剧", "日剧", "泰剧",
          "国产剧", "港台剧", "海外剧", "番剧")
# 明确排除的脏类（伦理/福利/资讯/解说/预告/体育/短剧等）
_EXCLUDE_KW = ("理论", "福利", "写真", "午夜", "解说", "预告",
               "花絮", "资讯", "新闻", "体育", "赛事", "漫剧", "AI",
               "演员", "音乐", "MV")

# 硬红线：任何开关都不能放开。涉未成年人性化、非自愿（换脸/迷奸/强奸/偷拍等）。
# 同时作用于“分类名”和条目标题/备注，命中即丢弃，聚合库、独立库、搜索一律不收录。
_REDLINE_KW = (
    # 未成年人
    "萝莉", "童贞",
    # 非自愿 / 性暴力
    "换脸"
)
# 即便开启福利分类，也仍按“非影视/中性噪音”排除的分类
_NEUTRAL_KW = ("解说", "预告", "花絮", "资讯", "新闻", "体育", "赛事",
               "演员", "音乐", "MV", "直播", "电台")

# 福利分类使用的内部 cat id（不进入 CATS，故永不进入跨源聚合的四个海量库）
ADULT_CAT = "adult"
JP_ADULT_CAT = "jp_adult"
DOMESTIC_CAT = "domestic"
EUROPE_ADULT_CAT = "europe_adult"
WELFARE_CATS = (ADULT_CAT, JP_ADULT_CAT, DOMESTIC_CAT, EUROPE_ADULT_CAT)

try:
    import features
except Exception:  # 兜底：独立运行/剥离异常时按公开（关闭）处理
    features = None


def _welfare_on() -> bool:
    """扩展（成人）资源库独立开关（管理页可调，默认关）。

    与构建期 features.ADULT（是否编译进本包的能力）是两个维度：
    - features.ADULT=False：完整能力未编译，任何开关都不放行；
    - welfare_library_enabled=False：独立开关关闭，不建路由、不抓取、不显示。
    开启后还需该源显式勾选「福利分类」(source.adult=true) 才会实际建库。
    """
    if not getattr(features, "ADULT", False):
        return False
    try:
        from config import config as _cfg
        return bool(_cfg.get().get("welfare_library_enabled", False))
    except Exception:
        _log.debug("_welfare_on failed", exc_info=True)
        return False


def is_redline(*texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        for k in _REDLINE_KW:
            if k and k in t:
                return True
    return False


def catalog_title(cat: str) -> str:
    if cat == ADULT_CAT:
        return "🔞伦理片"
    if cat == JP_ADULT_CAT:
        return "🔞日韩成人"
    if cat == DOMESTIC_CAT:
        return "🔞国产系列"
    if cat == EUROPE_ADULT_CAT:
        return "🔞欧美专区"
    for c, title, _ in CATALOG_CATEGORIES:
        if c == cat:
            return title
    return "🌐海量片库"


def catalog_mdb(cat: str) -> str:
    if cat in WELFARE_CATS:
        return "Mix"
    for c, _, mdb in CATALOG_CATEGORIES:
        if c == cat:
            return mdb
    return "Mix"


def classify(name: str) -> str | None:
    n = (name or "").strip()
    nl = n.lower()
    if not n:
        return None
    # P2：AI漫剧先于脏类排除判定——“漫剧 / AI”曾在 _EXCLUDE_KW 里，
    # 现在是正式分类，不再当噪音丢掉。
    if any(k in n for k in _AI_COMIC_KW):
        return "ai_comic"
    # P2 追加：TED（现显示为“教育”）（小写匹配，TED / Ted / TEDx 都认）。
    if any(k in nl for k in _TED_KW):
        return "ted"
    # cat-core：新分类判定，统一在 ai_comic/ted 之后、_EXCLUDE_KW 之前——
    # “体育/赛事”“音乐/MV”在排除表里，须先截获；“电视直播”含“电视”、
    # “少儿动画”含“动画”、“音乐纪录片”含“纪录”，须先于 tv/anime/
    # documentary 判定；五类之间互不重叠，顺序不限。
    if any(k in n for k in _SPORTS_KW):
        return "sports"
    if any(k in n for k in _LIVE_KW):
        return "live"
    if any(k in n for k in _KIDS_KW):
        return "kids"
    if any(k in n for k in _OPERA_KW):
        return "opera"
    if any(k in nl for k in _MUSIC_KW):
        return "music"
    if any(k in n for k in _EXCLUDE_KW):
        return None
    # 分类专属标记优先于宽泛类型词；短剧/画质等依明确类目名归类。
    if any(k in n for k in _SHORT_KW):
        return "shortdrama"
    if any(k in nl for k in _UHD_KW):
        return "uhd"
    # 动漫词先于海外词：源站常见「Netflix动漫 / 海外动漫」复合类目，
    # 先判海外会把整类动漫错挂进海外剧集库（真机 2026-11-24 串分类）。
    # 真人海外剧（海外剧/美剧…）不受影响；「海外剧场」仍走 shortdrama。
    if any(k in n for k in _ANIME_KW):
        return "anime"
    if any(k in n for k in _OVERSEAS_KW):
        return "overseas"
    if any(k in n for k in _VARIETY_KW):
        return "variety"
    if any(k in n for k in _DOC_KW):
        return "documentary"
    if any(k in n for k in _TV_KW):
        return "tv"
    if any(k in n for k in _MOVIE_KW):
        return "movie"
    # “动作片/喜剧片/剧情片/科幻片…”这类以“片”结尾的归入电影
    if n.endswith("片") and len(n) <= 4:
        return "movie"
    return None


def _excluded_cats_for(source_id) -> set[str]:
    """该源被用户排除的分类集合（config.catalog_cat_exclude）。

    值为 dict：source_id -> [cat, ...]；空/非法时返回空集合（不排除）。
    """
    sid = str(source_id or "")
    if not sid:
        return set()
    try:
        from config import config as _cfg
        raw = _cfg.read("catalog_cat_exclude", {}) or {}
        cats = raw.get(sid) if isinstance(raw, dict) else None
        if isinstance(cats, (list, tuple, set)):
            valid = set(CATS)
            return {str(c) for c in cats if str(c) in valid}
    except Exception:  # noqa: BLE001
        _log.debug("_excluded_cats_for failed", exc_info=True)
    return set()


def filter_category_list(categories: list[dict]) -> list[dict]:
    """统一过滤分类列表：精确命中黑名单的类目不进入任何后续抓取/缓存。"""
    return [c for c in (categories or [])
            if isinstance(c, dict)
            and str(c.get("type_name") or c.get("name") or "").strip() not in _BAN_CATEGORIES]


def classify_source_class(name: str, adult_allowed: bool) -> str | None:
    """按源的扩展授权决定分类归属。

    - 命中硬红线（涉未成年/非自愿）一律返回 None，任何开关都不放行；
    - 福利开关开时，命中 _ADULT_KW 的分类名先于常规分类进福利库
      （否则“伦理片”会被 classify() 的“片尾规则”判进电影库，
      “擦边短剧”会被判进短剧库）；开关关时本段不生效；
    - 常规分类走 classify（其内部脏类/中性噪音仍排除）；
    - 仅当该源显式开启福利分类(adult_allowed)、且为完整构建时，未归入常规
      分类、又不属于中性噪音/红线的分类，纳入内部福利分类（只进该源独立库）。
    """
    n = (name or "").strip()
    if not n or is_redline(n) or n in _BAN_CATEGORIES:
        return None
    # 成人向优先（第 5 批）：classify() 的“以‘片’结尾 ≤4 字 → movie”规则会
    # 把“伦理片”判进电影库、“短剧”规则会把“擦边短剧”判进短剧库，导致
    # _ADULT_KW 的第二段永远看不到它们。福利开关开时先认成人词，再走
    # 常规分类；开关关时本段不生效，常规路由与改前完全一致。
    if _welfare_on():
        if any(k in n for k in _JP_ADULT_KW):
            return JP_ADULT_CAT
        if any(k in n for k in _EUROPE_ADULT_KW):
            return EUROPE_ADULT_CAT
        if any(k in n for k in _DOMESTIC_KW) and "写真热舞" not in n:
            return DOMESTIC_CAT
        if any(k in n for k in _ADULT_KW):
            return ADULT_CAT
    if _welfare_on() and any(k in n for k in _EDUCATION_KW):
        return "ted"
    cat = classify(n)
    if cat:
        return cat
    if not _welfare_on():
        return None
    if any(k in n for k in _NEUTRAL_KW):
        return None
    # 其余未归类福利分类：仍需该源显式勾选福利分类才纳入
    if adult_allowed:
        return ADULT_CAT
    return None


async def _fetch_class_list(client: httpx.AsyncClient, source: dict) -> list[dict]:
    base = source["api"].split("/at/json")[0].split("?")[0]
    r = await client.get(base, params={"ac": "list"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    cls = j.get("class") if isinstance(j, dict) else None
    return cls if isinstance(cls, list) else []


# 非苹果 CMS 协议的源：分类表深翻只认 http(s) 的 CMS 接口，spider://、
# drpy://、catvod:// 等其它协议源一律不参与。
_NON_CMS_KINDS = {"t4", "spider", "drpy", "catvod", "xsrc"}


def _is_non_cms_source(source: dict) -> bool:
    """该源是否不是苹果 CMS 源（无 kind 时按 spider/drpy/catvod/xsrc 标记判）。"""
    if str(source.get("kind") or "") in _NON_CMS_KINDS:
        return True
    return bool(source.get("spider_src")
                or source.get("drpy") or source.get("catvod")
                or source.get("xsrc"))


async def _fetch_category_page(client: httpx.AsyncClient, source: dict,
                               type_id, page: int,
                               timeout: float = HTTP_TIMEOUT) -> tuple[list, int]:
    """返回 (本页 vod 列表, 该分类总页数)。"""
    base = source["api"].split("/at/json")[0].split("?")[0]
    # 注意：分类深翻不能带 h 参数（部分源加 h 后返回空/pagecount=0）。
    r = await client.get(base, params={"ac": "detail", "t": type_id, "pg": page},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return [], 0
    return (j.get("list") or []), int(j.get("pagecount") or 0)


class Catalog:
    def __init__(self):
        self._lock = threading.RLock()
        # source_id -> [ {type_id, type_name, type_pid, cat} ]
        self._classes: dict[str, list[dict]] = {}
        # "scope:cat" -> [ {source_id, type_id, type_name, pagecount?} ]（首选在最前）
        # scope 为 AGG_SCOPE（跨源聚合）或某个 source_id（单源独立媒体库）。
        self._routes: dict[str, list[dict]] = {}
        self._updated = 0.0
        self._refreshing = False
        self._refresh_future: asyncio.Future | None = None
        # key = (scope, cat, page)
        self._page_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        # "scope:cat" -> 去重缓冲（资源站分页会重复个别 vod）
        self._seen_guids: dict[str, set[str]] = {}
        self._dense: dict[str, list[dict]] = {}
        self._scanned_page: dict[str, int] = {}
        self._dense_count: dict[str, int] = {}
        # (sid, cat) -> [备用叶子 route...]（首选分类探测失败时 rescue 得到）
        self._fallbacks: dict[tuple[str, str], list[dict]] = {}
        # 内存工人 3：单源独立库交错 merged 缓存。
        # scope -> {merged, total, cats, updated, built_at}；失效见
        # standalone_window（TTL / 路由刷新 / reset）。
        self._standalone_cache: dict[str, dict] = {}
        self._standalone_inflight: dict[str, asyncio.Future] = {}
        self._standalone_warm_tasks: dict[str, asyncio.Task] = {}
        self._task_registrar = None
        self._cache_generation = 0
        self._route_state_loaded = False
        self._route_state_signature: str | None = None
        self._route_state_sequence = 0
        self._route_state_lock = threading.Lock()

    # 移植上游 v0.2.54（缓存管理）：清空海量库分类路由/分页/聚合缓冲
    # （管理页一键清缓存），下次浏览海量库时重新探测分类并深翻。
    # fork 适配：上游无 _fallbacks 字段，这里一并清空，避免残留备用路由
    # 与已清空的路由表不一致。
    def reset(self) -> None:
        with self._lock:
            self._classes.clear()
            self._routes.clear()
            self._updated = 0.0
            self._page_cache.clear()
            self._inflight.clear()
            self._seen_guids.clear()
            self._dense.clear()
            self._scanned_page.clear()
            self._dense_count.clear()
            self._fallbacks.clear()
            self._standalone_cache.clear()
            for task in self._standalone_warm_tasks.values():
                task.cancel()
            self._standalone_warm_tasks.clear()
            for fut in self._standalone_inflight.values():
                if not fut.done():
                    fut.cancel()
            self._standalone_inflight.clear()
            self._route_state_loaded = False
            self._route_state_signature = None
            self._route_state_sequence += 1
            self._cache_generation += 1
        # Serialize deletion with an in-progress atomic snapshot write. Acquiring
        # this lock after releasing _lock avoids holding the catalog lock on I/O.
        with self._route_state_lock:
            try:
                ROUTE_STATE.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _log.debug("catalog route state remove failed", exc_info=True)

    @staticmethod
    def _scope_key(scope: str, cat: str) -> str:
        return f"{scope}:{cat}"

    def _enforce_dense_limits_locked(self, keep_sk: str | None = None) -> None:
        """dense 总量超限时整作用域淘汰最旧分类（keep_sk 除外：正在浏览的
        作用域不动）。按作用域整删，不动作用域内条目——dense 按全局偏移
        索引，删中间条目会错位 get_window 的偏移；删整作用域只会让该分类
        下次重新翻页。单个作用域独自超限时不删（偏移语义优先），等其它
        作用域出现后再按总量淘汰。"""
        with self._lock:
            total = sum(len(v) for v in self._dense.values())
            while total > _DENSE_TOTAL_MAX:
                victim = next((sk for sk in self._dense if sk != keep_sk),
                              None)
                if victim is None:
                    break
                total -= len(self._dense.pop(victim, []))
                self._seen_guids.pop(victim, None)
                self._scanned_page.pop(victim, None)
                self._dense_count.pop(victim, None)

    def set_task_registrar(self, registrar) -> None:
        """Set optional application task registrar: registrar(coro, *, name)."""
        self._task_registrar = registrar

    def _create_background_task(self, coro, *, name: str) -> asyncio.Task:
        if self._task_registrar is not None:
            return self._task_registrar(coro, name=name)
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_log_background_task)
        return task

    def standalone_scopes(self) -> list[str]:
        """当前需要独立成库的源 id（来源配置中的 standalone 标记）。"""
        try:
            from config import config
            return [s["id"] for s in config.all_sources() if s.get("standalone")]
        except Exception:
            _log.debug("standalone_scopes failed", exc_info=True)
            return []

    # -------------------------------------------------- 分类表 / 路由
    @staticmethod
    def _route_signature(sources: list[dict]) -> str:
        rows = []
        for src in sources:
            if _is_non_cms_source(src):
                continue
            rows.append((str(src.get("id") or ""), str(src.get("api") or ""),
                         bool(src.get("adult"))))
        # Include category/routing policy so a classification update cannot reuse
        # a snapshot created by an older taxonomy.
        policy = {
            "schema": CATALOG_SCHEMA_VERSION,
            "welfare": _welfare_on(),
            "categories": list(CATALOG_DEFS.items()),
            "welfare_categories": list(WELFARE_CATS),
        }
        return json.dumps({"sources": sorted(rows), "policy": policy},
                          ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _read_route_state_file() -> dict | None:
        try:
            if (not ROUTE_STATE.exists()
                    or time.time() - ROUTE_STATE.stat().st_mtime > ROUTE_STATE_TTL):
                return None
            raw = json.loads(ROUTE_STATE.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except (OSError, ValueError, TypeError):
            _log.debug("catalog route state file read failed", exc_info=True)
            return None

    async def _load_route_state(self, sources: list[dict]) -> bool:
        signature = self._route_signature(sources)
        with self._lock:
            if self._route_state_loaded:
                return (bool(self._routes)
                        and self._route_state_signature == signature
                        and time.time() - self._updated <= ROUTE_STATE_TTL)
            # In-memory state always wins over disk, but is valid only for the
            # configuration that produced it.
            if self._classes or self._routes:
                self._route_state_loaded = True
                self._route_state_signature = signature
                return False
            self._route_state_loaded = True
            self._route_state_signature = signature
            generation = self._cache_generation
        raw = await asyncio.to_thread(self._read_route_state_file)
        if (raw is None or raw.get("schema") != CATALOG_SCHEMA_VERSION
                or raw.get("signature") != signature):
            return False
        classes = raw.get("classes") or {}
        routes = raw.get("routes") or {}
        if not isinstance(classes, dict) or not isinstance(routes, dict):
            return False
        # 路由状态里的叶子按**当时的**关键词表归类；升级后关键词表可能演进
        # （如新增/调整海外·动漫判定），旧盘态会把动漫类目一直错挂在
        # 海外剧集（真机串分类根因）。这里对每个叶子重跑一次
        # classify_source_class（纯内存、几千条目毫秒级），任何不一致
        # 都整份作废、走全量重探。
        try:
            adult_by_src = {sid: bool((self._source_by_id(sid) or {}).get("adult"))
                            for sid in classes}
            for sid, cls in classes.items():
                allowed = adult_by_src.get(sid, False)
                excluded = _excluded_cats_for(sid)
                for cl in (cls or []):
                    # 用户后来排除了某一类 → 旧盘态里该类路由必须作废
                    if cl.get("cat") in excluded:
                        _log.info("route state stale: sid=%s cat=%s excluded",
                                  sid, cl.get("cat"))
                        return False
                    want = classify_source_class(
                        str(cl.get("type_name") or ""), allowed)
                    if cl.get("cat") != want:
                        _log.info(
                            "route state stale after classify change: "
                            "sid=%s type=%r saved=%s now=%s; re-probe all",
                            sid, cl.get("type_name"), cl.get("cat"), want)
                        return False
        except Exception:  # noqa: BLE001
            _log.debug("route state revalidate failed", exc_info=True)
            return False
        with self._lock:
            if generation != self._cache_generation or not self._route_state_loaded:
                return False
            self._classes = classes
            self._routes = routes
            self._updated = float(raw.get("updated") or time.time())
            self._route_state_signature = signature
        _log.info("catalog routes restored from disk: %d routes", len(routes))
        return bool(routes)

    @staticmethod
    def _write_route_state(data: dict) -> None:
        try:
            ROUTE_STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = ROUTE_STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(ROUTE_STATE)
        except Exception:
            _log.debug("catalog route state persist failed", exc_info=True)

    async def _persist_route_state(self, sources: list[dict],
                                   generation: int | None = None) -> None:
        # Snapshot under the lock, then write off the event loop. Route refreshes
        # can contain hundreds of entries and should not block request handling.
        with self._lock:
            if generation is not None and generation != self._cache_generation:
                return
            signature = self._route_signature(sources)
            self._route_state_sequence += 1
            sequence = self._route_state_sequence
            data = {"schema": CATALOG_SCHEMA_VERSION,
                    "sequence": sequence,
                    "signature": signature,
                    "updated": self._updated,
                    "classes": {k: [dict(item) for item in items]
                                for k, items in self._classes.items()},
                    "routes": {k: [dict(item) for item in items]
                               for k, items in self._routes.items()}}
        await asyncio.to_thread(self._write_route_state_if_current,
                                data, generation, sequence)

    def _write_route_state_if_current(self, data: dict,
                                      generation: int | None,
                                      sequence: int) -> None:
        with self._route_state_lock:
            with self._lock:
                if generation is not None and generation != self._cache_generation:
                    return
                if sequence != self._route_state_sequence:
                    return
            self._write_route_state(data)

    async def refresh_routes(self, sources: list[dict]) -> dict[str, Any]:
        # 只有苹果 CMS 源能走分类表深翻；T4 与 spider/drpy/catvod/xsrc
        # 均非 CMS 协议（spider:// 等），当成 CMS 请求只会报 UnsupportedProtocol
        # 且独立库恒为空，故在入口一并排除。
        # Snapshot source fields once: callers may reload config while probing runs.
        sources = [dict(s) for s in sources if not _is_non_cms_source(s)]
        # Join active work before any asynchronous disk lookup; otherwise a
        # follower can race ahead and launch another route refresh.
        if self._refreshing and self._refresh_future is not None:
            return await asyncio.shield(self._refresh_future)
        if await self._load_route_state(sources):
            return self.status()
        if self._refreshing and self._refresh_future is not None:
            return await asyncio.shield(self._refresh_future)
        loop = asyncio.get_running_loop()
        self._refreshing = True
        generation = self._cache_generation
        self._refresh_future = loop.create_future()
        try:
            # 上游同位置走 netutil.async_client（按代理模式直连/走代理/自动切代理），
            # 与本文件 _load_page 的两处用法一致。
            async with netutil.async_client(
                    timeout=HTTP_TIMEOUT) as client:
                await asyncio.gather(
                    *[self._load_source_classes(client, s) for s in sources],
                    return_exceptions=True)

            # 每个源每类先探测一个代表性叶子；首选超时/空时，回退探测该类下
            # 其它叶子，选能连通且页数最多的（部分私有源个别分类响应很慢）。
            # all_by_cat[(sid,cat)] = [cl,...]，首个为首选。
            all_by_cat: dict[tuple[str, str], list[dict]] = {}
            order: list[tuple[str, str]] = []
            for sid, classes in self._classes.items():
                per_cat: dict[str, list[dict]] = {}
                for cl in classes:
                    cat = cl.get("cat")
                    if cat:
                        per_cat.setdefault(cat, []).append(cl)
                for cat, cls in per_cat.items():
                    key = (sid, cat)
                    all_by_cat[key] = cls
                    order.append(key)
            probed: dict[tuple[str, str], dict] = {}

            probe_sem = asyncio.Semaphore(PROBE_CONCURRENCY)

            async with netutil.async_client(
                    timeout=HTTP_TIMEOUT) as client:
                async def probe_one(sid, cat, cl):
                    # 探测有界并发：首轮 + rescue 共用同一信号量，约 100 路
                    # (源,类) 探测突发变为最多 PROBE_CONCURRENCY 路在途。
                    async with probe_sem:
                        source = self._source_by_id(sid)
                        if not source:
                            return None
                        try:
                            _lst, pc = await _fetch_category_page(
                                client, source, cl["type_id"], 1)
                        except Exception:
                            _log.debug("probe_one failed: sid=%s", sid, exc_info=True)
                            return None
                        if not _lst:
                            return None
                        return {"source_id": sid, "type_id": cl["type_id"],
                                "type_name": cl["type_name"],
                                "pagecount": int(pc or 0)}

                # 第一轮：每个 (源,类) 只探首选
                first = await asyncio.gather(
                    *[probe_one(sid, cat, all_by_cat[(sid, cat)][0])
                      for sid, cat in order],
                    return_exceptions=True)
                missing: list[tuple[str, str]] = []
                for (sid, cat), res in zip(order, first):
                    if isinstance(res, dict):
                        probed[(cat, sid)] = res
                    else:
                        missing.append((sid, cat))

                # 第二轮：对首轮失败/空的(源,类)，并发尝试其余叶子，取页数最多者
                async def rescue(sid, cat):
                    cls = all_by_cat[(sid, cat)][1:9]  # 最多再试 8 个叶子
                    if not cls:
                        return
                    res = await asyncio.gather(
                        *[probe_one(sid, cat, cl) for cl in cls],
                        return_exceptions=True)
                    ok = [r for r in res if isinstance(r, dict)]
                    if ok:
                        ok.sort(key=lambda r: -(r.get("pagecount") or 0))
                        probed[(cat, sid)] = ok[0]
                        # 记录备用叶子，供翻页超时/空页时同类并发竞速回退
                        self._fallbacks[(sid, cat)] = ok[1:7]
                await asyncio.gather(*[rescue(s, c) for s, c in missing],
                                     return_exceptions=True)

            def routes_for_scope(scope: str) -> dict[str, list[dict]]:
                out: dict[str, list[dict]] = {}
                # 福利分类由 welfare_library_enabled 独立开关控制：
                # 开关开 → 聚合与单源独立库都纳入 ADULT 池；开关关 → 一律不建。
                cats = list(dict.fromkeys(CATS + (list(WELFARE_CATS) if _welfare_on() else [])))
                for cat in cats:
                    if scope == AGG_SCOPE:
                        rs = [probed[(cat, sid)] for (c2, sid) in probed if c2 == cat]
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        # 聚合：首选最深的源，并附带其余可深翻源做回退
                        out[cat] = rs
                    else:
                        # 单源独立库：不依赖建库探测是否成功——直接纳入该类下
                        # 全部叶子（探测到页数的排前面，未探测到的用 MAX_PAGES
                        # 兜底），取页时并发抓取合并、天然容错慢/超时叶子。
                        by_id = {}
                        seed = []
                        hit = probed.get((cat, scope))
                        if hit:
                            seed.append(hit)
                        seed.extend(self._fallbacks.get((scope, cat), []))
                        for r in seed:
                            by_id[r["type_id"]] = r
                        for cl in self._classes.get(scope, []):
                            if cl.get("cat") == cat and cl["type_id"] not in by_id:
                                by_id[cl["type_id"]] = {
                                    "source_id": scope,
                                    "type_id": cl["type_id"],
                                    "type_name": cl["type_name"],
                                    "pagecount": MAX_PAGES}
                        rs = list(by_id.values())
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        out[cat] = rs
                return out

            new_routes: dict[str, list[dict]] = {}
            agg = routes_for_scope(AGG_SCOPE)
            for cat, rs in agg.items():
                new_routes[self._scope_key(AGG_SCOPE, cat)] = rs
            for sid in self.standalone_scopes():
                for cat, rs in routes_for_scope(sid).items():
                    if rs:
                        new_routes[self._scope_key(sid, cat)] = rs
            with self._lock:
                if generation != self._cache_generation:
                    return self.status()
                # 清理已不再需要的作用域缓冲
                valid_keys = set(new_routes.keys())
                self._routes = new_routes
                self._route_state_signature = self._route_signature(sources)
                for d in (self._seen_guids, self._dense, self._scanned_page):
                    for k in list(d.keys()):
                        if k not in valid_keys:
                            d.pop(k, None)
                self._updated = time.time()
            await self._persist_route_state(sources, generation)
            warm_scopes = [sid for sid in self.standalone_scopes()
                           if any(self.available(c, sid) for c in
                                  (list(dict.fromkeys(CATS + (list(WELFARE_CATS) if _welfare_on() else [])))))]
            for sid in warm_scopes:
                old = self._standalone_warm_tasks.get(sid)
                if old is not None and not old.done():
                    continue
                task = self._create_background_task(self.warm_standalone(sid),
                                                  name=f"catalog-warm-{sid}")
                self._standalone_warm_tasks[sid] = task
                task.add_done_callback(_log_background_task)
                task.add_done_callback(
                    lambda done, sid=sid: self._standalone_warm_tasks.pop(sid, None))
            # 4K 派生预热：源没有 4K 分类时，后台先翻电影页把 4K 片抄进
            # uhd 密集列表——库卡才有海报、首次点开才有内容；只在 uhd
            # 还是空的时候跑一次（密集列表在路由刷新时保留，不会反复跑）。
            if (not self._routes.get(self._scope_key(AGG_SCOPE, "uhd"))
                    and self._routes.get(self._scope_key(AGG_SCOPE, "movie"))
                    and not self._dense.get(self._scope_key(AGG_SCOPE, "uhd"))):
                task = self._create_background_task(self.get_window("uhd", 0, 40), name="catalog-warm-uhd")
                task.add_done_callback(_log_background_task)
        finally:
            result = self.status()
            self._refreshing = False
            if self._refresh_future is not None and not self._refresh_future.done():
                self._refresh_future.set_result(result)
            self._refresh_future = None
        return result

    async def _load_source_classes(self, client: httpx.AsyncClient, source: dict):
        # 非 http(s) api（如 drpy://x.js）不是 CMS 接口，直接跳过：交给
        # _fetch_class_list 会把 "drpy://..." 当 URL 请求，httpx 报
        # UnsupportedProtocol。
        api = str(source.get("api") or "")
        if not api.lower().startswith(("http://", "https://")):
            _log.info("_load_source_classes skipped non-http source: id=%s",
                      source.get("id"))
            return
        try:
            cls = filter_category_list(await _fetch_class_list(client, source))
        except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError):
            _log.debug("_load_source_classes failed: source=%s", source, exc_info=True)
            cls = []
        # 只取叶子子类：苹果CMS 的父类（电影片/连续剧…）不聚合子类、只有零星条目，
        # 真正可深翻的是动作片/国产剧这类子分类。
        parent_ids = {str(x.get("type_id")) for x in cls}
        pids_with_child = {str(x.get("type_pid") or "0") for x in cls
                           if str(x.get("type_pid") or "0") not in ("", "0")}
        norm = []
        adult_allowed = bool(source.get("adult"))
        # Explicit short-drama parent categories are authoritative for child
        # leaves whose names (e.g. 都市/逆袭) carry no type signal themselves.
        parent_cat = {str(x.get("type_id")): classify_source_class(
            str(x.get("type_name") or ""), adult_allowed) for x in cls}
        for x in cls:
            try:
                tid = x.get("type_id")
                pid = str(x.get("type_pid") or "0")
                name = str(x.get("type_name") or "")
                cat = classify_source_class(name, adult_allowed)
                if (cat is None or cat == "tv") and parent_cat.get(pid) == "shortdrama":
                    cat = "shortdrama"
                if tid is None or cat is None:
                    continue
                is_leaf = pid != "0" or str(tid) not in pids_with_child
                if not is_leaf:
                    continue
                norm.append({"type_id": tid, "type_name": name,
                             "type_pid": pid, "cat": cat})
            except Exception:
                _log.debug("_load_source_classes failed: source=%s", source, exc_info=True)
                continue
        # 分类排除：该源被标记为脏的类目直接不建路由（聚合与独立库都不受影响
        # 之外的其它类），避免脏数据串到别的分类库。配置见 catalog_cat_exclude。
        excluded = _excluded_cats_for(source.get("id"))
        if excluded:
            before = len(norm)
            norm = [cl for cl in norm if cl.get("cat") not in excluded]
            if len(norm) != before:
                _log.info("catalog cat excluded sid=%s cats=%s dropped=%d",
                          source.get("id"), sorted(excluded), before - len(norm))
        with self._lock:
            self._classes[source["id"]] = norm

    def _source_by_id(self, sid: str) -> dict | None:
        try:
            from config import config
            return next((s for s in config.all_sources() if s["id"] == sid), None)
        except Exception:
            _log.debug("_source_by_id failed: sid=%s", sid, exc_info=True)
            return None

    def cat_for_source_type(self, source_id: str, type_id) -> str | None:
        """(source_id, type_id) → 该源分类路由的 cat；路由未加载/无匹配返回 None。

        供聚合搜索 brief 补 ``catalog_route`` 用：搜索返回的 vod 自带
        type_id，与建库时抓到的分类表（``_classes``）同一 id 空间；
        cat 由建库时的 ``classify_source_class`` 定，与分类库路由一致。
        """
        if not source_id or type_id is None or str(type_id) == "":
            return None
        tid = str(type_id)
        with self._lock:
            for cl in self._classes.get(source_id, []) or []:
                if str(cl.get("type_id")) == tid:
                    return cl.get("cat") or None
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": int(self._updated),
                "categories": {
                    cat: {
                        "routes": len(self._routes.get(
                            self._scope_key(AGG_SCOPE, cat), [])),
                        "source": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("source_id", ""),
                        "type_name": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("type_name", ""),
                        "pagecount": min(int((self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0]
                            .get("pagecount") or 0), MAX_PAGES),
                    } for cat, _, _ in CATALOG_CATEGORIES},
            }

    def _uhd_fallback_routes(self, scope: str) -> list[dict]:
        """4K 回退路由：源没有“4K”分类名时，借电影分类的路由翻页，
        再由 _briefs_from_vods 按片名画质信号过滤出真 4K。"""
        with self._lock:
            return list(self._routes.get(self._scope_key(scope, "movie")) or [])

    def available(self, cat: str, scope: str = AGG_SCOPE) -> bool:
        with self._lock:
            if self._routes.get(self._scope_key(scope, cat)):
                return True
            # 4K：没有 4K 分类名、但有电影路由时也可派生；或已从浏览中抄到 4K 片。
            if cat == "uhd":
                if self._routes.get(self._scope_key(scope, "movie")):
                    return True
                return bool(self._dense.get(self._scope_key(scope, "uhd")))
            return False

    def find_brief_by_guid(self, guid: str) -> dict | None:
        """纯本地、零网络：在已缓存的密集列表/分页里按 guid 反查目录片 brief。

        供二级线路墙对“海量库冷片”（非 chart、尚未 remember 进 store）按需
        落库使用；找不到返回 None（调用方再走搜索绑定或返回空）。
        _page_cache 同时兼容早期 (ts, briefs) 与当前 (ts, briefs, ttl) 记录。
        """
        if not guid:
            return None
        with self._lock:
            for briefs in list(self._dense.values()):
                for b in briefs:
                    if b.get("guid") == guid:
                        return b
            for entry in list(self._page_cache.values()):
                briefs = entry[1]
                for b in briefs:
                    if b.get("guid") == guid:
                        return b
        return None

    def pagecount(self, cat: str, scope: str = AGG_SCOPE) -> int:
        with self._lock:
            routes = self._routes.get(self._scope_key(scope, cat)) or []
            if not routes and cat == "uhd":
                routes = self._routes.get(self._scope_key(scope, "movie")) or []
            if not routes:
                return 0
            return min(int(routes[0].get("pagecount") or 0), MAX_PAGES)

    def dense_count(self, cat: str, scope: str = AGG_SCOPE) -> int:
        """已实际找到并去重的条目数（密集列表长度）。"""
        with self._lock:
            return len(self._dense.get(self._scope_key(scope, cat)) or [])

    def cached_dense(self, cat: str, limit: int = 40,
                     scope: str = AGG_SCOPE) -> list[dict]:
        """纯本地、零网络：取某大类已物化的去重密集列表前 limit 条。

        只返回已在内存里的 brief（浏览分类时由 _extend_dense 填入），
        未物化的分类返回 []，不触发任何网络拉取。供详情页关联作品冷启动
        兜底用：新装/未浏览时已浏览池为空，靠它补候选。
        """
        with self._lock:
            dense = self._dense.get(self._scope_key(scope, cat)) or []
            return list(dense[:max(0, int(limit))])

    def dense_fingerprint(self, scope: str = AGG_SCOPE) -> tuple[int, ...]:
        """各大类 dense 长度元组（纯本地、零网络）：详情关联缓存的
        dense 侧指纹。dense 只追加不清零（reset 除外），长度变化即
        代表候选池变化；与 store.titles_version 一起作缓存失效依据。"""
        with self._lock:
            return tuple(len(self._dense.get(self._scope_key(scope, c)) or [])
                         for c in CATS)

    def total(self, cat: str, scope: str = AGG_SCOPE) -> int:
        # 用规范源页数估算 total；最后一页条数未知，前端以空页自然停止。
        # 例外：4K 没有原生路由时是派生库，电影页数 × 20 是虚数——源备注里
        # 没有 4K 信号时库是空的，却报几十条，前端会一直翻空页转圈；
        # 只报实际找到的数，没找到就是 0。
        if cat == "uhd" and not self._routes.get(self._scope_key(scope, cat)):
            return self.dense_count(cat, scope)
        return self.pagecount(cat, scope) * PAGE_SIZE

    def routes_stale(self) -> bool:
        return (not self._updated) or (time.time() - self._updated) >= CLASS_REFRESH

    async def _fetch_pages(self, scope: str, cat: str,
                         pages: list[int]) -> list[tuple[int, list[dict]]]:
        """P1-17：有界并发取一批 CMS 页，返回 [(页号, rows)]，按页号升序。

        并发只发生在取页层：信号量把同时在途的 get_page 控制在
        DEEP_FETCH_CONCURRENCY 以内；get_page/_load_page 内部的
        3 轮重试与 (16, 10, 8) 三档超时回退原样不动。
        任一页失败（get_page 本身已吞异常返 []；此处再防非常规抛错）
        只记该页为空，不丢同批其它页；调用方按页号顺序合并，去重、
        空页容忍与游标语义与串行逐页完全一致。
        """
        sem = asyncio.Semaphore(DEEP_FETCH_CONCURRENCY)

        async def bound(pg: int) -> tuple[int, list[dict]]:
            async with sem:
                try:
                    rows = await self.get_page(cat, pg, scope)
                except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError):
                    _log.debug("bound failed: scope=%s cat=%s page=%s",
                               scope, cat, pg, exc_info=True)
                    return pg, []
                return pg, rows if isinstance(rows, list) else []

        results = await asyncio.gather(
            *[bound(pg) for pg in pages], return_exceptions=True)
        out: list[tuple[int, list[dict]]] = []
        for pg, res in zip(pages, results):
            if isinstance(res, tuple) and len(res) == 2:
                out.append(res)
            else:
                out.append((pg, []))
        return out

    @staticmethod
    def _content_key(brief: dict) -> tuple | None:
        """仅在标题、明确年份、类型均可确认时跨来源合并展示卡片。

        不剥离季数、版本或标点，不把未知年份当成同一年；guid 和
        播放线路保持原样，来源记录仍由 store 保存。
        """
        title = str(brief.get("title") or "").strip().casefold()
        year = str(brief.get("year") or "").strip()
        kind = classify(str(brief.get("type_name") or ""))
        if not kind:
            kind = brief.get("catalog_cat")
        if kind == "uhd":  # 画质不能判断电影/剧集身份
            return None
        if (not title or len(year) != 4 or not year.isascii()
                or not year.isdigit() or not 1800 <= int(year) <= 2199 or not kind):
            return None
        return title, year, kind

    @classmethod
    def _append_unique(cls, rows: list[dict], dense: list[dict], seen: set) -> int:
        content = {key for b in dense if (key := cls._content_key(b)) is not None}
        added = 0
        for brief in rows:
            guid = brief.get("guid")
            key = cls._content_key(brief)
            if guid and guid in seen:
                continue
            if guid:
                seen.add(guid)
            if key is not None and key in content:
                continue
            if key is not None:
                content.add(key)
            dense.append(brief)
            added += 1
        return added

    # -------------------------------------------------- 页级抓取
    async def _extend_dense(self, scope: str, cat: str, need_count: int) -> int:
        """顺序翻页直到去重后的累计条目数达到 need_count（或翻到该源末页）。

        P1-17：按 DEEP_FETCH_CONCURRENCY 分批并发取页，批内按页号顺序
        合并——每页的去重、游标前移、空页计数与串行版逐页体完全一致。
        达到 need / 空页到限时停下：批次里已取到但未处理到的页只留在
        _page_cache，不并入 dense、不前移游标，下次接着翻时从缓存取，
        不重复请求。"""
        generation = self._cache_generation
        sk = self._scope_key(scope, cat)
        pc = self.pagecount(cat, scope)
        seen = self._seen_guids.setdefault(sk, set())
        dense = self._dense.setdefault(sk, [])
        pg = self._scanned_page.get(sk, 0) + 1
        empty_streak = 0
        # 4K 回退是借电影页过滤，4K 片稀疏时连续多页都可能 0 新增，
        # 空页容忍放宽到 10 页，别翻两页就断。
        streak_limit = 10 if cat == "uhd" else 3
        while len(dense) < need_count and pg <= pc:
            batch = list(range(pg, min(pg + DEEP_FETCH_CONCURRENCY, pc + 1)))
            pages = await self._fetch_pages(scope, cat, batch)
            if generation != self._cache_generation:
                return 0
            stopped = False
            for pg_i, rows in pages:
                added = self._append_unique(rows, dense, seen)
                if added:
                    # 只有真正取到新增才把游标前移，慢源抖动的空页下次可重试
                    self._scanned_page[sk] = pg_i
                    empty_streak = 0
                else:
                    # 连续多页空（超时/末页）才停止，容忍慢源的偶发抖动
                    empty_streak += 1
                    if empty_streak >= streak_limit:
                        self._scanned_page[sk] = pg_i
                        stopped = True
                        break
                pg = pg_i + 1
                if len(dense) >= need_count:
                    stopped = True
                    break
            if stopped:
                break
        self._dense_count[sk] = len(dense)
        self._enforce_dense_limits_locked(keep_sk=sk)
        return len(dense)

    async def get_window(self, cat: str, offset: int, limit: int,
                         scope: str = AGG_SCOPE) -> list[dict]:
        """按“去重后的全局偏移”取一窗 brief，自动顺序跨 CMS 页，密集无重复。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        sk = self._scope_key(scope, cat)
        generation = self._cache_generation
        dense = self._dense.setdefault(sk, [])
        if len(dense) < offset + limit:
            await self._extend_dense(scope, cat, offset + limit)
        if generation != self._cache_generation:
            return []
        return dense[offset:offset + limit]

    async def get_page(self, cat: str, page: int, scope: str = AGG_SCOPE) -> list[dict]:
        """取某作用域某大类第 page 页（1 起）的可播放 brief 列表（已入库）。"""
        page = max(1, int(page))
        if page > MAX_PAGES:
            return []
        key = (scope, cat, page)
        generation = self._cache_generation
        with self._lock:
            hit = self._page_cache.get(key)
            if hit:
                ts, data, ttl = hit
                if _monotonic() - ts < ttl:
                    return data
                self._page_cache.pop(key, None)
            fut = self._inflight.get(key)
            if fut is not None and fut.get_loop() is not asyncio.get_running_loop():
                # 旧事件循环残留的 future（循环已关闭、拉取任务已取消）：
                # 丢弃重建，否则新循环里等一个永远不会完成的 future。
                fut = None
                self._inflight.pop(key, None)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            with self._lock:
                self._inflight[key] = fut

            async def work():
                try:
                    briefs = await self._load_page(scope, cat, page)
                    with self._lock:
                        if generation != self._cache_generation:
                            if not fut.done():
                                fut.set_result([])
                            return
                        # 空结果只短缓存，避免慢源一次抖动把该页冻结 10 分钟
                        ttl = EMPTY_TTL if not briefs else PAGE_TTL
                        self._page_cache[key] = (_monotonic(), briefs, ttl)
                        # 页缓存总页数上限：按插入序删最旧页（可重新拉取）
                        while len(self._page_cache) > _PAGE_CACHE_MAX:
                            self._page_cache.pop(
                                next(iter(self._page_cache)), None)
                    if not fut.done():
                        fut.set_result(briefs)
                except asyncio.CancelledError:
                    # 拉取任务被取消：先给等它的人一个空结果再重抛，
                    # 别把 key 永远挂在 _inflight 让后来的人等死。
                    if not fut.done():
                        fut.set_result([])
                    raise
                except Exception:
                    _log.debug("work failed", exc_info=True)
                    if not fut.done():
                        fut.set_result([])
                finally:
                    with self._lock:
                        if self._inflight.get(key) is fut:
                            self._inflight.pop(key, None)

            asyncio.create_task(work())
        return await asyncio.shield(fut)

    def cached_page_posters(self, cat: str, n: int = 8,
                            scope: str = AGG_SCOPE) -> list[str]:
        """纯本地、零网络：从已缓存的首页/密集列表取海报，供媒体库卡片秒回。"""
        out: list[str] = []
        with self._lock:
            # 优先已翻好的去重密集列表（最新），其次首页缓存
            dense = self._dense.get(self._scope_key(scope, cat)) or []
            pool = list(dense)
            hit = self._page_cache.get((scope, cat, 1))
            if hit and _monotonic() - hit[0] < hit[2]:
                for b in hit[1]:
                    if b not in pool:
                        pool.append(b)
            seen = set()
            for b in pool:
                p = b.get("poster") or ""
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
                    if len(out) >= n:
                        break
        return out

    def _briefs_from_vods(self, vods: list, source: dict, route: dict,
                          scope: str, cat: str) -> list[dict]:
        from config import config as _cfg
        briefs = []
        for v in vods:
            try:
                t = providers._map_vod(v, source)
            except Exception:
                _log.debug("_briefs_from_vods failed: source=%s", source, exc_info=True)
                continue
            if not t or not t.title:
                continue
            playable = [e for line in t.sources.values() for e in line
                        if e.direct or e.resolve]
            if not playable:
                continue
            # 硬红线（任何模式都不放行）：涉未成年/非自愿，标题或备注命中即丢。
            if is_redline(t.title, t.remarks):
                continue
            # 私有源放宽：单源作用域且该源开启 loose_filter 时，
            # 跳过标题/备注噪音过滤；硬红线与常规分类红线仍在。
            loose = (scope != AGG_SCOPE and bool(source.get("loose_filter")))
            if not loose and _cfg.is_junk(t.title, t.remarks):
                continue
            # 4K 库只收带画质信号的片（见 _load_page 的 uhd 回退路由）。
            if cat == "uhd" and not is_uhd_title(t.title, t.remarks):
                continue
            briefs.append({
                "guid": make_guid(t),
                "online_id": t.online_id, "title": t.title,
                "type_name": t.type_name, "year": t.year,
                "poster": t.poster, "overview": t.overview,
                "remarks": t.remarks, "score": t.score,
                "actor": t.actor, "director": t.director, "area": t.area,
                "lines": {name: [e.__dict__ for e in eps]
                          for name, eps in t.sources.items()},
                "catalog_cat": cat, "catalog_scope": scope,
                "catalog_source": route["source_id"],
                # P0-6：来源分类路由（与 catalog_* 同源，供 libfilter
                # 并集归属；聚合搜索 brief 由 app 层补同一形状字段）。
                "catalog_route": {"source": source["id"],
                                  "type_id": route["type_id"], "cat": cat},
            })
        # 派生 4K：浏览电影/剧集等分类时，带 4K 画质信号的片顺手抄一份进
        # 同作用域的 uhd 密集列表——源分类名里没有“4K”时 4K 库也能有内容。
        if cat != "uhd" and briefs:
            uhd_sk = self._scope_key(scope, "uhd")
            uhd_seen = self._seen_guids.setdefault(uhd_sk, set())
            uhd_dense = self._dense.setdefault(uhd_sk, [])
            self._append_unique([
                {**b, "catalog_cat": "uhd",
                 "catalog_route": {**b["catalog_route"], "cat": "uhd"}}
                for b in briefs if is_uhd_title(b["title"], b["remarks"])
            ], uhd_dense, uhd_seen)
            self._dense_count[uhd_sk] = len(uhd_dense)
        return briefs

    async def _load_page(self, scope: str, cat: str, page: int) -> list[dict]:
        from store import store
        generation = self._cache_generation
        with self._lock:
            routes = list(self._routes.get(self._scope_key(scope, cat)) or [])
        if not routes and cat == "uhd":
            # 源没有“4K”分类名：借电影分类翻页，片名画质信号过滤在
            # _briefs_from_vods 里做（cat 仍是 uhd，只留真 4K）。
            routes = self._uhd_fallback_routes(scope)
        if scope == AGG_SCOPE:
            routes = routes[:4]
        if not routes:
            return []

        async def fetch_one(client, route, timeout):
            source = self._source_by_id(route["source_id"])
            if not source:
                return None
            try:
                vods, pc = await _fetch_category_page(
                    client, source, route["type_id"], page, timeout=timeout)
            except Exception:
                _log.debug("fetch_one failed", exc_info=True)
                return None
            if not vods:
                return None
            return source, route, vods

        # 单源独立库：把该类下全部叶子的同一页并发抓取后“合并去重”，
        # 而不是竞速只取一个——慢/不稳的私有源单页常超时，合并所有叶子
        # 才能保证条目完整。首轮给较长超时，全空再重试两轮。
        if scope != AGG_SCOPE and len(routes) > 1:
            sem = asyncio.Semaphore(10)

            async def once(timeout):
                # 上游 v1.0.0 做法：深翻经 netutil.async_client（按代理模式
                # 直连/走代理/自动切代理），与 providers.py 已有用法一致。
                # 证书校验开启（与 providers.py 的 CMS 抓取一致；用户源
                # API 与搜索/详情走同一批主机，不应在此处单独关校验）。
                async with netutil.async_client(
                        timeout=HTTP_TIMEOUT) as client:
                    async def bound(r):
                        async with sem:
                            return await fetch_one(client, r, timeout)
                    results = await asyncio.gather(
                        *[bound(r) for r in routes],
                        return_exceptions=True)
                if generation != self._cache_generation:
                    return []
                merged: list[dict] = []
                seen_g: set[str] = set()
                for res in results:
                    if not isinstance(res, tuple):
                        continue
                    source, route, vods = res
                    for b in self._briefs_from_vods(vods, source, route, scope, cat):
                        g = b.get("guid")
                        if g and g not in seen_g:
                            seen_g.add(g)
                            merged.append(b)
                return merged

            for attempt, tmo in enumerate((16.0, 10.0, 8.0)):
                briefs = await once(tmo)
                if generation != self._cache_generation:
                    return []
                if briefs:
                    return store.remember_merged(briefs)
            return []

        # 跨源聚合：串行回退（不同源结果应一致，并发会无谓翻倍请求）
        # 证书校验开启，理由同上（与 providers.py 的 CMS 抓取一致）。
        async with netutil.async_client(timeout=HTTP_TIMEOUT) as client:
            for route in routes:
                got = await fetch_one(client, route, HTTP_TIMEOUT)
                if generation != self._cache_generation:
                    return []
                if not got:
                    continue
                source, _route, vods = got
                briefs = self._briefs_from_vods(vods, source, route, scope, cat)
                if briefs:
                    return store.remember_merged(briefs)
        return []

    async def warm_standalone(self, scope: str):
        """建库后后台预热独立库各分类首页，缓解慢源首屏空白。"""
        try:
            src = self._source_by_id(scope)
            pool = list(dict.fromkeys(CATS + list(WELFARE_CATS))) if (_welfare_on() and src and src.get("adult")) else CATS
            await asyncio.gather(
                *[self.get_page(c, 1, scope) for c in pool
                  if self.available(c, scope)],
                return_exceptions=True)
        except Exception:
            _log.debug("warm_standalone failed", exc_info=True)
            pass

    async def _source_cat_window(self, scope: str, cat: str,
                                 offset: int, limit: int) -> list[dict]:
        """单源某类窗口：每页 = 该类下全部叶子同页合并去重（首页约数百条）。

        顺序取合并大页直到攒够 offset+limit；容忍慢源偶发的整页空（重试到
        连续 3 个空页才认为到底），避免一次抖动就把分页永久截断。

        P1-17：按 DEEP_FETCH_CONCURRENCY 分批并发取页，批内按页号顺序
        合并；任一页失败只记该页为空，不丢同批其它页。
        """
        generation = self._cache_generation
        out: list[dict] = []
        seen: set[str] = set()
        pg = 1
        empty_streak = 0
        while len(out) < offset + limit and pg <= MAX_PAGES and empty_streak < 3:
            batch = list(range(pg, min(pg + DEEP_FETCH_CONCURRENCY,
                                       MAX_PAGES + 1)))
            pages = await self._fetch_pages(scope, cat, batch)
            if generation != self._cache_generation:
                return []
            stopped = False
            for pg_i, rows in pages:
                added = self._append_unique(rows, out, seen)
                if added:
                    empty_streak = 0
                else:
                    empty_streak += 1
                pg = pg_i + 1
                if len(out) >= offset + limit or empty_streak >= 3:
                    stopped = True
                    break
            if stopped:
                break
        return out[offset:offset + limit]

    # --------------------------------------- 单源独立库：跨四类交错窗口
    async def standalone_window(self, scope: str, offset: int,
                                limit: int) -> tuple[list[dict], int]:
        """单源独立库：把该源的电影/剧/动漫/综艺按轮次交错成一条“最近更新”流。

        返回 (brief 列表, 估算 total)。四类各取 offset..offset+limit 的窗，
        再按排名轮次合并去重，最后按全局 offset 切片。

        内存工人 3：交错 merged 结果按 scope 缓存。同一浏览会话内翻页
        （offset 增大、但仍在已缓存 merged 覆盖范围内）直接切片返回，
        不再每次从 0 重建 O(offset) 合并、重复调 _source_cat_window。
        失效：TTL 60s、路由刷新（_updated 变化）、分类集合变化、reset()；
        请求超出已缓存 merged 长度时按新 end 重建一次并更新缓存。
        合并顺序/去重逻辑与旧版完全一致，只是不重复算。
        """
        src = self._source_by_id(scope)
        pool = list(dict.fromkeys(CATS + list(WELFARE_CATS))) if (_welfare_on() and src and src.get("adult")) else CATS
        cats = [c for c in pool if self.available(c, scope)]
        if not cats:
            return [], 0
        offset = max(0, int(offset))
        limit = max(1, int(limit))
        end = offset + limit
        with self._lock:
            entry = self._standalone_cache.get(scope)
            updated = self._updated
            generation = self._cache_generation
        if (entry is not None
                and entry.get("cats") == tuple(cats)
                and entry.get("updated") == updated
                and time.time() - entry.get("built_at", 0) < STANDALONE_MERGED_TTL
                and len(entry.get("merged") or []) >= end):
            return list(entry["merged"][offset:end]), entry["total"]
        with self._lock:
            inflight = self._standalone_inflight.get(scope)
            if inflight is None:
                inflight = asyncio.get_running_loop().create_future()
                self._standalone_inflight[scope] = inflight
                owner = True
            else:
                owner = False
        if not owner:
            merged, total = await asyncio.shield(inflight)
            return list(merged[offset:offset + limit]), total
        try:
            results = await asyncio.gather(
                *[self._source_cat_window(scope, c, 0, end) for c in cats],
                return_exceptions=True)
            per: list[list[dict]] = []
            total = 0
            for c, r in zip(cats, results):
                per.append(r if isinstance(r, list) else [])
                if isinstance(r, list):
                    total += self.total(c, scope)
            merged: list[dict] = []
            seen: set[str] = set()
            maxlen = max((len(x) for x in per), default=0)
            # 轮次交错：第 i 轮依次取每类第 i 条，近似源站“最近更新”混合排序
            interleaved = [arr[i] for i in range(maxlen) for arr in per if i < len(arr)]
            self._append_unique(interleaved, merged, seen)
            with self._lock:
                if generation != self._cache_generation:
                    if not inflight.done():
                        inflight.set_result(([], 0))
                    return [], 0
                # Keep the complete local build for this request and shared
                # waiters, but retain only a bounded prefix for later slices.
                cached_merged = merged[:_STANDALONE_MERGED_MAX]
                self._standalone_cache[scope] = {
                    "merged": cached_merged, "total": total, "cats": tuple(cats),
                    "updated": self._updated, "built_at": time.time()}
                if not inflight.done():
                    inflight.set_result((merged, total))
            return merged[offset:offset + limit], total
        except BaseException as exc:
            with self._lock:
                if not inflight.done():
                    inflight.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._standalone_inflight.get(scope) is inflight:
                    self._standalone_inflight.pop(scope, None)

    def standalone_available(self, scope: str) -> bool:
        src = self._source_by_id(scope)
        pool = list(dict.fromkeys(CATS + list(WELFARE_CATS))) if (_welfare_on() and src and src.get("adult")) else CATS
        return any(self.available(c, scope) for c in pool)


catalog = Catalog()
