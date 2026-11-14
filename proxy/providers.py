#!/usr/bin/env python3
"""在线资源 provider：苹果 CMS V10 标准 JSON 接口（ac=detail）。

资源站返回的播放地址有两种：
  - 直链：http(s)://....m3u8 / .mp4
  - 中转页（/share/<id> 等）：需要解析，PoC 阶段标记为需解析、先跳过
provider 只负责"搜索→条目→播放 URL 列表"，不做转码。
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

import features
import netutil

try:
    from fnlog import get_logger
except Exception:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("providers")

# P1-14 高频日志采样（与 app.py 的同名小工具各自独立，避免 providers
# 依赖 app 造成循环引用）：死源跳过每搜 1 条 INFO，搜索扇出时会刷爆日志。
# 默认只记 debug，每 100 次采样 1 条 info 保留心跳。
_LOG_SAMPLE: dict[str, int] = {}


def _sampled_log(key: str, every: int = 100) -> bool:
    """该 key 的第 every 次调用返回 True（其余时间调用方记 debug）。"""
    n = _LOG_SAMPLE.get(key, 0) + 1
    _LOG_SAMPLE[key] = n
    return n % every == 0

try:
    import sourcehealth as _health
except Exception:
    _health = None  # type: ignore


def _source_health_enabled() -> bool:
    """源健康自动降级开关（config.source_health_enabled，默认 True）。

    惰性读 config，避免与 config.py（import providers）循环导入。
    关时：搜索不再跳过冷却窗内的死源，也不再记录新的健康画像
    （降级不记录/不冷却）；开关开时行为与改前一致。
    """
    try:
        from config import config as _cfg
        return bool(_cfg.get().get("source_health_enabled", True))
    except Exception:
        _log.debug("_source_health_enabled failed", exc_info=True)
        return True

# 2026-10-17 国内直连实测（搜索+m3u8 直链可达性）。403/404 多为走 Clash 代理所致。
DEFAULT_SOURCES: list[dict] = [
    {"id": "baofeng", "name": "暴风资源", "api": "https://bfzyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "ffzy", "name": "非凡资源", "api": "https://cj.ffzyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "lzi", "name": "量子资源", "api": "https://cj.lziapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "wujin", "name": "无尽资源", "api": "https://api.wujinapi.me/api.php/provide/vod/at/json", "enabled": False},
    {"id": "youku", "name": "优酷资源", "api": "https://api.ukuapi88.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "guangsu", "name": "光速资源", "api": "https://api.guangsuapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "ruyi", "name": "如意资源", "api": "https://cj.rycjapi.com/api.php/provide/vod/at/json", "enabled": False},
    {"id": "jisu", "name": "极速资源", "api": "https://jszyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "xinlang", "name": "新狼资源", "api": "https://api.xinlangapi.com/xinlangapi.php/provide/vod/at/json", "enabled": True},
    {"id": "dytt", "name": "电影天堂", "api": "https://caiji.dyttzyapi.com/api.php/provide/vod/at/json", "enabled": True},
    {"id": "s360", "name": "360资源", "api": "https://360zy.com/api.php/provide/vod/at/json", "enabled": False},
]
# >>> PRIVATE:DDUO
# 非标准源：多多影视，自研 JSON+WASM 解码，不是苹果CMS。默认关闭，在管理页勾选
# 启用；播放地址由内嵌官方 WASM 实时解码。公开构建剥离此块，源不会出现。
if features.DDUO:
    DEFAULT_SOURCES.append(
        {"id": "dduo", "name": "多多影视", "api": "https://dduotv01.top",
         "enabled": False, "xsrc": True, "builtin": True})
# <<< PRIVATE:DDUO
# >>> PRIVATE:CATVOD
# 小雅网盘（catvod JVM 爬虫 csp_AListSh，非苹果CMS）。默认开启，搜索/详情/播放
# 走 catvod 适配层；公开构建剥离此块，源不会出现。
if features.CATVOD:
    DEFAULT_SOURCES.append(
        {"id": "xiaoya", "name": "小雅网盘", "api": "catvod://xiaoya",
         "enabled": True, "xsrc": True, "catvod": True, "builtin": True})
# <<< PRIVATE:CATVOD

# >>> PRIVATE:DRPY
# drpyS JS 爬虫（Node sidecar 跑 TVBox type=3 的 drpy2 .js 站点）。
# api 形如 drpy://<本地规则脚本名>，非苹果CMS；公开构建整块剥离。
import os as _os
_DRPY_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                          "assets", "drpy")
if features.DRPY:
    for _did, _dname, _dscript, _den in (
        ("drpy-txbei", "童学儿童", "txbei.js", True),
        ("drpy-huya", "虎牙直播", "huya.js", False),
        ("drpy-douyu", "斗鱼直播", "douyu.js", False),
    ):
        if _os.path.exists(_os.path.join(_DRPY_DIR, _dscript)):
            DEFAULT_SOURCES.append(
                {"id": _did, "name": _dname, "api": "drpy://" + _dscript,
                 "enabled": _den, "xsrc": True, "drpy": True, "builtin": True})
    # 订阅导入的远程 drpyS 源（规则脚本已落盘 assets/drpy/sub/）
    try:
        import drpy as _drpym
        for _d in _drpym.remote_sources():
            if not any(s["id"] == _d["id"] for s in DEFAULT_SOURCES):
                DEFAULT_SOURCES.append(_d)
    except Exception:
        pass
# <<< PRIVATE:DRPY
# 源质量默认权重（越高越优先；未列出取 0）。基于国内直连可达性、直链率与片源质量
# 的经验初值，用户可在管理页「片源质量」整体覆盖（存 config.source_priority）。
# 标准 CMS 站按口碑分档；新增自定义源默认 0。
# 注意：这两个函数是通用逻辑（聚合搜索与状态接口都直接调 providers.source_priority），
# 不能放进下面的私有标记块——公开构建剥离该块后它们必须还在。私有源的档位由块内
# 的 _private_source_priority 钩子补上；公开构建里钩子为 None，只走通用表。
DEFAULT_PRIORITY: dict[str, int] = {
    "baofeng": 40, "ffzy": 45, "guangsu": 40, "jisu": 38, "xinlang": 36,
    "lzi": 30, "youku": 28, "dytt": 30, "wujin": 20, "ruyi": 20, "s360": 20,
    "xiaoya": 55,
}

_private_source_priority = None


def default_source_priority(source: dict) -> int:
    if _private_source_priority is not None:
        v = _private_source_priority(source)
        if v is not None:
            return v
    return DEFAULT_PRIORITY.get(source.get("id", ""), 0)


def source_priority(source: dict, overrides: dict | None = None) -> int:
    """源的有效质量权重：用户覆盖优先，否则内省默认。"""
    sid = source.get("id", "")
    if overrides and sid in overrides:
        try:
            return int(overrides[sid])
        except (TypeError, ValueError):
            pass
    return default_source_priority(source)


# >>> PRIVATE:SPIDER
# 普通 csp_ 爬虫源（饭太硬等订阅导入的纯 Java 在线源，经 redroid 真机解析）。
SPIDER_DEFAULT_PRIORITY = 36
DRPY_DEFAULT_PRIORITY = 15
DDUO_DEFAULT_PRIORITY = 15


def _private_source_priority_fn(source: dict) -> int | None:
    """私有源的质量档位；通用源返回 None，走 DEFAULT_PRIORITY 通用表。"""
    sid = source.get("id", "")
    if source.get("spider_src"):
        return SPIDER_DEFAULT_PRIORITY
    if source.get("drpy"):
        return DRPY_DEFAULT_PRIORITY
    if source.get("dduo") or sid == "dduo":
        return DDUO_DEFAULT_PRIORITY
    return None


_private_source_priority = _private_source_priority_fn


def _bootstrap_spider_sources() -> None:
    # 普通 csp_ 爬虫源（订阅导入，按各自 jar 加载）
    try:
        import spiders as _sp
        for d in _sp.source_defs():
            if not any(s["id"] == d["id"] for s in DEFAULT_SOURCES):
                DEFAULT_SOURCES.append(d)
    except Exception:
        _log.debug("_bootstrap_spider_sources failed", exc_info=True)
        pass


_bootstrap_spider_sources()
# <<< PRIVATE:SPIDER

# >>> PRIVATE:PAN
# PanSou 网盘搜索源：搜索结果作为真实的一集注入线路（上游同款行为 ——
# 上游 pansou.py 把每条分享链接映射为该标题下的一集，播放时转存出直链）。
def _bootstrap_pansou_source() -> None:
    try:
        import pansou_search as _pss
        d = _pss.source_def()
        if not any(s.get("id") == d["id"] for s in DEFAULT_SOURCES):
            DEFAULT_SOURCES.append(d)
    except Exception:
        _log.debug("_bootstrap_pansou_source failed", exc_info=True)


_bootstrap_pansou_source()
# <<< PRIVATE:PAN

HTTP_TIMEOUT = 12.0
_DIRECT_RE = re.compile(r"^https?://", re.I)
_MEDIA_RE = re.compile(r"\.(m3u8|mp4|flv|mkv|ts)(\?|$)", re.I)
# 可由自有账号转存原画播放的网盘分享链接（夸克 / UC）。
_PAN_SHARE_RE = re.compile(
    r"https?://(?:pan\.quark\.cn/s/|(?:pan|drive)\.uc\.cn/s/)[A-Za-z0-9]+", re.I)


@dataclass
class Episode:
    title: str
    url: str
    direct: bool          # True=可直接播放的媒体地址
    resolve: bool = False  # True=播放页，需 resolve_share 解析
    headers: dict = field(default_factory=dict)
    # True=该地址是一次 302 跳转，Location 才是真实媒体（如苹果CMS ac=videodiag）
    redirect: bool = False
    # 非标准源标记：url 为加密 token，播放前需在专属适配层解码；
    # play_from 是该集所属线路标识，解码必须一并上送（公开构建下恒为 False）
    xsrc: bool = False
    play_from: str = ""
    t4: bool = False
    episode_key: str = ""
    # >>> PRIVATE:CATVOD
    # 小雅网盘：url 为网盘文件路径，播放前由 catvod 解析成签名直链（公开构建恒 False）
    catvod: bool = False
    # <<< PRIVATE:CATVOD
    # >>> PRIVATE:DRPY
    # drpyS JS 爬虫：播放前由 Node sidecar 的 playerContent 解析（公开构建恒 False）
    drpy: bool = False
    # <<< PRIVATE:DRPY


@dataclass
class OnlineTitle:
    provider: str
    vod_id: str
    title: str
    type_name: str
    year: str
    area: str
    updated: str
    remarks: str
    score: str
    poster: str
    overview: str
    actor: str
    director: str
    sources: dict          # {播放线路名: [Episode,...]}
    # 标题级时长（秒）：电影=全片时长，剧集=单集时长。苹果 CMS V10 标准
    # 字段 vod_duration（模板标签 {$obj.vod_duration}=时长），很多源站
    # 留空或只在详情里给；缺失为 0，播放时再由 HLS playlist 探测补。
    duration: int = 0
    locator: dict = field(default_factory=dict)
    # 苹果 CMS 分类 id（vod 的 type_id；_map_vod 保留，聚合搜索用它反查
    # 该源的分类路由，见 catalog.cat_for_source_type）。非 CMS 源为空。
    type_id: str = ""

    @property
    def online_id(self) -> str:
        return f"{self.provider}:{self.vod_id}"


def parse_duration(value) -> int:
    """CMS 时长字段 → 秒（int），解析不出 / 不合法返回 0。

    接受的形态（均来自真实 CMS 记录的写法，不猜新字段名）：
      - 2700 / 2700.0 / "2700"：秒；
      - "45分钟" / "45分"：分钟；
      - "01:30:00" / "45:30"：时:分:秒 / 分:秒。
    上限 12 小时：超过的视为脏数据（如把毫秒/时间戳误填进来），回 0。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        secs = int(value)
        return secs if 0 < secs <= 43200 else 0
    s = str(value).strip()
    if not s:
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|min)", s, re.I)
    if m:
        secs = int(float(m.group(1)) * 60)
        return secs if 0 < secs <= 43200 else 0
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return 0
        if len(nums) == 2:
            secs = int(nums[0] * 60 + nums[1])
        elif len(nums) == 3:
            secs = int(nums[0] * 3600 + nums[1] * 60 + nums[2])
        else:
            return 0
        return secs if 0 < secs <= 43200 else 0
    try:
        secs = int(float(s))
    except ValueError:
        return 0
    return secs if 0 < secs <= 43200 else 0


def _parse_play(vod_play_from: str, vod_play_note: str, vod_play_url: str,
                referer: str) -> dict:
    """把苹果 CMS 的 from/note/url 解析成 {线路: [Episode]}。"""
    froms = (vod_play_from or "").split("$$$")
    notes = (vod_play_note or "").split("$$$")
    groups = (vod_play_url or "").split("$$$")
    out: dict[str, list[Episode]] = {}
    for i, raw_group in enumerate(groups):
        line = froms[i] if i < len(froms) and froms[i] else f"线路{i+1}"
        eps = []
        for item in raw_group.split("#"):
            item = item.strip()
            if not item or "$" not in item:
                continue
            name, url = item.split("$", 1)
            url = url.strip()
            if not _DIRECT_RE.match(url):
                continue
            ep_name = name.strip() or f"第{len(eps)+1}集"
            # 选集层去广告：跳过“关注公众号/加微信/加群/下载APP”等引流假选集
            try:
                import adfilter as _adf
                if _adf.is_ad_episode(ep_name):
                    continue
            except Exception:
                _log.debug("_parse_play failed", exc_info=True)
                pass
            direct = bool(_MEDIA_RE.search(url))
            # /share/<id> 等播放页：非直链但可按需解析出媒体地址
            resolvable = (not direct) and bool(
                re.search(r"/(share|play|player)/[A-Za-z0-9]+", url))
            # ac=videodiag 等：请求该地址会 302 到真实 m3u8/mp4
            redirect_ep = bool(re.search(r"[?&]ac=videodiag\b", url, re.I))
            eps.append(Episode(
                title=ep_name,
                url=url,
                direct=direct or redirect_ep,  # 视为可播，播放时跟随 302
                resolve=resolvable,
                redirect=redirect_ep,
                headers={"Referer": referer, "User-Agent": "Mozilla/5.0"},
            ))
        if eps:
            out[line] = eps
    return out


def _xml_text(elem) -> str:
    """取 XML 元素文本（自动解 CDATA），无则空串。"""
    if elem is None:
        return ""
    return (elem.text or "").strip()


def parse_cms_xml(text: str) -> dict | None:
    """把苹果 CMS 的 at/xml 响应（RSS）转成与 at/json 等价的 dict：
    {code:1, list:[ {vod_id, vod_name, vod_play_from, vod_play_url, ...} ]}。

    XML 用 <video> 元素、子标签名（id/name/pic/year/...、<dl><dd flag>），
    JSON 用 vod_ 前缀字段；这里统一映射，后续 _map_vod 无需区分格式。
    来源：上游 providers.py parse_cms_xml（166-229 行），fork 原样补齐。
    """
    if not text or not text.lstrip().startswith("<"):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
    except Exception:
        _log.debug("parse_cms_xml failed", exc_info=True)
        return None
    # 根可能是 <rss>，列表在其下 <list>；也兼容直接 <list>
    list_node = root.find(".//list")
    if list_node is None:
        list_node = root if root.tag.lower() == "list" else None
    if list_node is None:
        return {"code": 1, "list": []}

    def child(video, name):
        el = video.find(name)
        return _xml_text(el) if el is not None else ""

    out = []
    for video in list_node.findall("video"):
        # 播放源：<dl><dd flag="m3u8"><![CDATA[ 第1集$url#第2集$url ]]></dd>
        froms, groups = [], []
        for dd in video.findall(".//dl/dd"):
            flag = (dd.get("flag") or "").strip() or f"line{len(froms)+1}"
            play = _xml_text(dd)
            if not play:
                continue
            froms.append(flag)
            groups.append(play)
        vod = {
            "vod_id": child(video, "id"),
            "vod_name": child(video, "name"),
            "type_name": child(video, "type"),
            "vod_year": child(video, "year"),
            "vod_area": child(video, "lang") or child(video, "area"),
            "vod_time": child(video, "last"),
            "vod_remarks": child(video, "note"),
            "vod_score": child(video, "score"),
            "vod_duration": child(video, "duration"),
            "vod_pic": child(video, "pic"),
            "vod_content": child(video, "des") or child(video, "content")
                           or child(video, "blurb"),
            "vod_actor": child(video, "actor"),
            "vod_director": child(video, "director"),
            "vod_play_from": "$$$".join(froms),
            "vod_play_note": "",
            "vod_play_url": "$$$".join(groups),
        }
        out.append(vod)
    return {"code": 1, "list": out}


def _is_xml_api(api: str) -> bool:
    return "/at/xml" in (api or "").lower()


def _map_vod(v: dict, source: dict) -> OnlineTitle | None:
    sources = _parse_play(v.get("vod_play_from", ""), v.get("vod_play_note", ""),
                          v.get("vod_play_url", ""), _site_root(source["api"]))
    return OnlineTitle(
        provider=source["id"],
        vod_id=str(v.get("vod_id")),
        title=v.get("vod_name", "").strip(),
        type_name=v.get("type_name", ""),
        type_id=str(v.get("type_id") or ""),
        year=str(v.get("vod_year", "") or ""),
        area=v.get("vod_area", "") or "",
        updated=str(v.get("vod_time", "") or v.get("vod_time_add", "") or ""),
        remarks=v.get("vod_remarks", "") or "",
        score=str(v.get("vod_score", "") or ""),
        poster=v.get("vod_pic", "") or "",
        overview=(v.get("vod_content") or v.get("vod_blurb") or "").strip(),
        actor=v.get("vod_actor", "") or "",
        director=v.get("vod_director", "") or "",
        duration=parse_duration(
            v.get("vod_duration") or v.get("vod_length")
            or v.get("duration") or ""),
        sources=sources,
    )


def _site_root(api: str) -> str:
    p = urllib.parse.urlparse(api)
    return f"{p.scheme}://{p.netloc}/"


async def search_source(client: httpx.AsyncClient, source: dict,
                        keyword: str) -> list[OnlineTitle]:
    # P2 源健康自动降级：连续失败 5 次的源在 3 分钟冷却窗内直接跳过，
    # 不再每次都等超时拖慢整体搜索；冷却过后自动放行一次试探。
    # 开关 source_health_enabled 关闭时不跳过、也不记录（降级不记录/不冷却）。
    _health_on = _source_health_enabled()
    if _health_on and _health is not None and not _health.usable(source.get("id", "")):
        _emit = _log.info if _sampled_log("source_skip") else _log.debug
        _emit("source skipped (dead cooldown): id=%s", source.get("id"))
        return []
    _t0 = time.monotonic()
    if source.get("kind") == "t4":
        import t4 as _t4mod
        try:
            out = await _t4mod.search(client, source, keyword)
        except _t4mod.T4Error as exc:
            _log.warning("T4 search failed: id=%s error=%s elapsed=%.2fs",
                         source.get("id"), exc, time.monotonic() - _t0)
            if _health_on and _health is not None:
                _health.record(source.get("id", ""), False, time.monotonic() - _t0)
            return []
        if _health_on and _health is not None:
            _health.record(source.get("id", ""), True, time.monotonic() - _t0)
        return out
    params = {"ac": "detail", "wd": keyword}
    try:
        r = await client.get(source["api"], params=params,
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            _log.warning("source search failed: id=%s status=%s elapsed=%.2fs",
                         source.get("id"), r.status_code, time.monotonic() - _t0)
            if _health_on and _health is not None:
                _health.record(source.get("id", ""), False, time.monotonic() - _t0)
            return []
        # 苹果 CMS at/xml：RSS XML 响应，先转成与 at/json 等价的 dict
        j = parse_cms_xml(r.text) if _is_xml_api(source["api"]) else r.json()
    except Exception as e:
        _log.warning("source search failed: id=%s error=%s elapsed=%.2fs",
                     source.get("id"), type(e).__name__, time.monotonic() - _t0)
        if _health_on and _health is not None:
            _health.record(source.get("id", ""), False, time.monotonic() - _t0)
        return []
    if _health_on and _health is not None:
        _health.record(source.get("id", ""), True, time.monotonic() - _t0)
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return []
    out = []
    dropped = 0
    first_err = ""
    for v in j.get("list", []) or []:
        try:
            t = _map_vod(v, source)
        except Exception as exc:
            # 单条 vod 映射失败只丢该条（上游 JSON 字段不可信），按调用汇总记一条日志。
            _log.debug("search_source failed: source=%s", source, exc_info=True)
            dropped += 1
            if not first_err:
                first_err = type(exc).__name__
            continue
        if t and t.title:
            out.append(t)
    if dropped:
        _log.warning("source search dropped unmappable vods: id=%s dropped=%d error=%s",
                     source.get("id"), dropped, first_err)
    return out


# 非直链页面里内嵌媒体地址的解析规则
_EMBED_URL_RES = [
    re.compile(r"""(?:const|let|var)\s+url\s*=\s*["']([^"']+)["']""", re.I),
    re.compile(r"""["']url["']\s*:\s*["']([^"']+)["']""", re.I),
    re.compile(r"""(https?://[^"'\s<>]+?\.m3u8[^"'\s<>]*)""", re.I),
]
_resolve_cache: dict[str, tuple[float, str]] = {}
_RESOLVE_TTL = 30 * 60


async def resolve_redirect(client: httpx.AsyncClient, jump_url: str,
                           referer: str = "") -> str | None:
    """跟随一次 302 跳转（ac=videodiag 等），返回 Location 指向的真实媒体地址。

    部分私有源该接口极慢且间歇抖动（同一地址有时 1s、有时 16~19s 超时），
    只试一次会在播放中途偶发解析失败导致整个 m3u8 502。这里放宽超时并做
    退避重试，成功后缓存较长时间，保证一次会话内地址稳定。
    """
    import asyncio as _aio
    import time as _t
    jump_url = jump_url.strip()
    hit = _resolve_cache.get(jump_url)
    if hit and _t.time() - hit[0] < _RESOLVE_TTL:
        return hit[1]
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    found = None
    for attempt, tmo in enumerate((20.0, 16.0, 12.0)):
        try:
            r = await client.get(jump_url, headers=headers, timeout=tmo,
                                 follow_redirects=False)
            loc = r.headers.get("location") or ""
            if loc and _MEDIA_RE.search(loc):
                found = urllib.parse.urljoin(jump_url, loc)
                break
        except httpx.HTTPError as exc:
            _log.debug("resolve_redirect attempt failed: attempt=%d error=%s",
                       attempt, type(exc).__name__)
            found = None
        if attempt < 2:
            await _aio.sleep(0.3 * (2 ** attempt))
    if found:
        _resolve_cache[jump_url] = (_t.time(), found)
        if len(_resolve_cache) > 2000:
            for k in list(_resolve_cache)[:1000]:
                _resolve_cache.pop(k, None)
    return found


async def resolve_share(client: httpx.AsyncClient, page_url: str,
                        referer: str = "") -> str | None:
    """把 /share/<id> 等播放页解析成真实媒体地址（按需、带缓存）。"""
    import time as _t
    page_url = page_url.strip()
    hit = _resolve_cache.get(page_url)
    if hit and _t.time() - hit[0] < _RESOLVE_TTL:
        return hit[1]
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    # 优先用强化嗅探引擎（player_aaaa / iframe 递归 / 转义解码 / base64），
    # 它是纯 HTTP 解析，可替代部分原本要靠爬虫或 WebView 的网页源。
    try:
        import sniff as _sniff
        found = await _sniff.sniff(client, page_url, referer)
        if found:
            return found
    except Exception as exc:
        _log.debug("resolve_share sniff failed: error=%s", type(exc).__name__)
    try:
        r = await client.get(page_url, headers=headers, timeout=10,
                             follow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text
    except httpx.HTTPError as exc:
        _log.debug("resolve_share fetch failed: error=%s", type(exc).__name__)
        return None
    found = None
    for rx in _EMBED_URL_RES:
        m = rx.search(html)
        if m:
            cand = m.group(1)
            # 必须像媒体地址（.m3u8/.mp4 或相对媒体路径）
            if _MEDIA_RE.search(cand) or re.search(r"/\d{6,}/[^\"']+\.(m3u8|mp4)", cand):
                found = urllib.parse.urljoin(str(r.url), cand)
                break
    if found:
        _resolve_cache[page_url] = (_t.time(), found)
        if len(_resolve_cache) > 2000:
            for k in list(_resolve_cache)[:1000]:
                _resolve_cache.pop(k, None)
    return found


def _is_site_side_error(exc: BaseException) -> bool:
    """站点自身问题（不是本机网络抖动）→ 可作失效证据。

    典型：SSL 证书链不全（站点漏装中间证书）、证书过期/域名不匹配、
    TLS 握手协议错误、HTTP 解析错。这些错误换任何网络/设备都一样，
    不会因为手机换 WiFi 而恢复，因此可以下「失效」结论。

    注意：httpx 会把 TLS 失败包装成 ConnectError，异常类型不够用，
    必须同时看错误文本（实测 agedm 证书链缺失就落在 ConnectError 里）。
    """
    import ssl as _ssl
    if isinstance(exc, (_ssl.SSLError, _ssl.CertificateError)):
        return True
    txt = str(exc).lower()
    if any(m in txt for m in (
        "certificate verify failed", "unable to get local issuer",
        "certificate is not valid", "hostname mismatch",
        "wrong version number", "ssl:", "certificate has expired",
    )):
        return True
    return type(exc).__name__ in {"RemoteProtocolError", "UnsupportedProtocol"}


async def probe_source(api: str, keyword: str = "庆余年",
                       timeout: float = 8.0, source: dict | None = None) -> dict:
    """探测一个苹果CMS资源站是否可用。
    返回 {ok,status,elapsed_ms,count,sample,error}。"""
    api = (api or "").strip()
    if source and source.get("kind") == "t4":
        import t4 as _t4mod
        async with httpx.AsyncClient(trust_env=False) as client:
            return await _t4mod.probe(client, source, timeout)
    t0 = time.monotonic()
    if not api.startswith(("http://", "https://")):
        return {"ok": False, "status": 0, "elapsed_ms": 0, "count": 0,
                "sample": "", "error": "地址需以 http(s):// 开头"}
    try:
        async with netutil.async_client(timeout=timeout) as client:
            r = await client.get(
                api, params={"ac": "detail", "wd": keyword},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                # 站点已明确应答（403/404/410/5xx）= 接口本身坏了，是强失效证据。
                # 标记 site_error 供上层与「网络层抖动」区分，否则 403 会被
                # 当成网络不可达而永远不下结论（实测 heimuer 403 一直停在 alive=None）。
                return {"ok": False, "status": r.status_code,
                        "elapsed_ms": ms, "count": 0, "sample": "",
                        "site_error": True,
                        "error": f"HTTP {r.status_code}"}
            # 同时支持 at/json 与 at/xml（XML 先转等价 dict）
            j = parse_cms_xml(r.text) if _is_xml_api(api) else r.json()
    except Exception as exc:
        _log.debug("probe_source failed: source=%s", source, exc_info=True)
        return {"ok": False, "status": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "count": 0, "sample": "", "error": type(exc).__name__,
                # ssl 证书链不全（站点缺中间证书，真实飞牛同样会失败）属于站点侧问题，
                # 不是本机网络抖动 → 记强失效证据，否则永远停在「不下结论」。
                "site_error": _is_site_side_error(exc)}
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return {"ok": False, "status": 200, "elapsed_ms": ms, "count": 0,
                "sample": "", "error": "非苹果CMS JSON（code≠1）"}
    lst = j.get("list", []) or []
    sample = ""
    direct = 0
    for v in lst:
        name = v.get("vod_name", "")
        play = v.get("vod_play_url", "") or ""
        if not sample and name:
            sample = name
        if _MEDIA_RE.search(play):
            direct += 1
    if not lst:
        # 接口合法但该词无结果，再试分类列表确认源活着
        try:
            async with netutil.async_client(timeout=timeout) as client:
                r2 = await client.get(
                    api, params={"ac": "list", "pg": 1},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
                j2 = parse_cms_xml(r2.text) if _is_xml_api(api) else r2.json()
            if isinstance(j2, dict) and j2.get("code") in (1, "1"):
                return {"ok": True, "status": 200, "elapsed_ms": ms,
                        "count": 0, "sample": "",
                        "error": "接口正常（该词无结果）"}
        except Exception as exc:
            _log.debug("probe list fallback failed: error=%s", type(exc).__name__)
        return {"ok": False, "status": 200, "elapsed_ms": ms, "count": 0,
                "sample": "", "error": "无返回结果"}
    return {"ok": True, "status": 200, "elapsed_ms": ms,
            "count": len(lst), "direct": direct, "sample": sample, "error": ""}


# 资源站有效性检测专用的【常见热门】关键词。只选各大主流 CMS 基本都收录的长红
# 影视剧，避免用冷门片导致“源其实正常、只是没这部”的误判。任一关键词搜到即视为有效。
# 移植上游 v0.2.71（批量体检）。
PROBE_HOT_KEYWORDS = ("庆余年", "狂飙", "流浪地球", "斗罗大陆", "琅琊榜")

# >>> PRIVATE:RISK 风险源判定（2026-11-13）
# 爬虫源导入时会带 risk 字段；CMS/TVBox 源没有，这里按关键词兜底判定。
# 命中即视为「风险/重资源源」：探活通过也自动禁用，避免拖慢聚合或
# 在缺登录态时反复报错。
_RISKY_NAME_KW = (
    "伦理", "色", "福利", "午夜", "性感", "两性", "偷拍", "自拍",
    "蜜桃", "麻豆", "天美", "果冻", "swag", "91", "黑料", "吃瓜",
    "破解", "福利姬", "里番",
)
_RISKY_URL_KW = ("/adult", "adult/", "sexy", "porn", "18av")


def source_is_risky(source: dict) -> bool:
    """判断一个源是否「风险源」（纯函数，不触网）。"""
    if not isinstance(source, dict):
        return False
    if source.get("risk"):
        return True
    name = str(source.get("name") or "").lower()
    if any(k.lower() in name for k in _RISKY_NAME_KW):
        return True
    url = str(source.get("api") or source.get("url") or "").lower()
    return any(k in url for k in _RISKY_URL_KW)
# <<< PRIVATE:RISK


async def probe_source_alive(source: dict, timeout: float = 8.0,
                             keywords=PROBE_HOT_KEYWORDS) -> dict:
    """用多个常见热门关键词检测资源站是否有效。

    依次尝试关键词，任一返回非空结果即判定有效（ok=True）并立即返回；全部关键词
    都搜不到才判定失效。这样不会因单个片名缺失而误伤正常源。返回与 probe_source
    兼容的结构，并额外带 tried（尝试过的词）与 hits（命中的词）。
    移植上游 v0.2.71（批量体检）。"""
    api = (source.get("api") or "").strip()
    t0 = time.monotonic()
    tried, hit = [], ""
    last = {"ok": False, "status": 0, "elapsed_ms": 0, "count": 0,
            "sample": "", "error": ""}
    net_err = 0          # 网络层失败（超时/连不上）次数
    hard_err = 0         # 明确站点侧失败（4xx/5xx、非 CMS 结构）
    for kw in keywords:
        tried.append(kw)
        r = await probe_source(api, keyword=kw, timeout=timeout, source=source)
        last = r
        if r.get("ok") and (r.get("count") or 0) > 0:
            hit = kw
            return {**r, "alive": True, "hit": hit, "tried": tried,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        err = str(r.get("error") or "")
        if r.get("site_error"):
            # 站点明确应答异常（403/404/5xx、SSL 证书链坏、非 CMS 结构）→
            # 换任何网络都一样，是强失效证据，直接判死不再空耗其余关键词。
            hard_err += 1
            return {**r, "alive": False, "reachable": True, "hit": "",
                    "tried": tried, "reason": f"站点侧错误：{err or '接口异常'}",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        if _is_net_error_text(err):
            net_err += 1
        else:
            hard_err += 1
    # 全部关键词都无结果：若接口仍能返回合法 CMS 结构，只说明没有这些热门片，
    # 不能据此把正常小众源熔断。用 alive=None 表示“可访问但无基准命中”。
    # 移植上游 beta.3（B11）。
    if last.get("ok"):
        return {**last, "alive": None, "reachable": True, "hit": "", "tried": tried,
                "reason": "接口正常但基准热门词无结果",
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    if net_err and not hard_err:
        # 纯网络层失败（沙箱/内网 DNS 抖动、移动网络握手超时）不足以判死源：
        # 只有站点明确应答了错误（4xx/5xx、非 CMS 结构）才确定失效。
        # 否则整个探活会被一次 ConnectTimeout 团灭（实测真 CMS 源被误判失效）。
        return {**last, "alive": None, "reachable": False, "hit": "", "tried": tried,
                "reason": f"网络不可达（{net_err} 次），不下结论",
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    return {**last, "alive": False, "hit": "", "tried": tried,
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


_NET_ERROR_MARKS = ("timeout", "timed out", "connect", "connecterror",
                    "connection", "unreachable", "dns", "getaddrinfo",
                    "name or service", "ssl", "handshake", "reset",
                    "read error", "remotedisconnected", "network")


def _is_net_error_text(err: str) -> bool:
    """错误文本是否是网络层失败（不代表站点失效）。"""
    t = (err or "").lower()
    return any(m in t for m in _NET_ERROR_MARKS)

def _is_net_layer_error(exc: BaseException) -> bool:
    """异常是否属于网络层失败。

    区分标准：**请求有没有到达站点**。超时/连接/握手/DNS 抖动属于「没拿到
    应答」，一秒前还正常的源可能只是临时断网，不能据此判死；反之站点返回了
    HTML 当 JSON、结构不符、4xx/5xx，说明接口本身坏了，是强失效证据。
    """
    try:
        import httpx as _hx
        if isinstance(exc, (_hx.TimeoutException, _hx.TransportError,
                            _hx.ConnectError, _hx.ReadError,
                            _hx.RemoteProtocolError, _hx.ProxyError)):
            return True
    except Exception:
        pass
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        # OSError 覆盖 getaddrinfo 失败、连接被拒、socket reset 等。
        return True
    return _is_net_error_text(type(exc).__name__) or _is_net_error_text(str(exc))


async def probe_any_source(source: dict, timeout: float = 8.0,
                           keywords=PROBE_HOT_KEYWORDS) -> dict:
    """按源类型探活任意源，返回与 probe_source_alive 兼容的结构。

    2026-11-13：原批量体检只覆盖纯 CMS 源，spider/drpy/t4/catvod/xsrc
    全被跳过，用户看到的“探活不全”即此。这里按类型分发：
      · CMS / T4       → probe_source_alive（多热门词）
      · spider / drpy  → 交给各自搜索实现（需 sidecar 运行时可跑）
      · catvod / xsrc  → 同上
    运行时不具备时返回 alive=None（reachable=False），
    由调用方决定不据此禁用，避免误伤。
    """
    src = dict(source or {})
    kind = _source_kind_of(src)

    if kind in ("", "cms", "t4"):
        return await probe_source_alive(src, timeout=timeout, keywords=keywords)

    # 非 CMS 源：走各自搜索探活，需要运行时（sidecar）在。
    # 判据分三层：命中=活着；运行时缺失=不下结论；超时/连不上=判定失效。
    t0 = time.monotonic()
    hard_fail = 0
    site_bad = 0
    last_err = ""
    for kw in keywords:
        try:
            async with netutil.async_client() as client:
                titles = await _dispatch_search(client, src, kw)
        except Exception as exc:  # noqa: BLE001
            _log.debug("probe_any_source %s failed: id=%s", kind,
                       src.get("id"), exc_info=True)
            last_err = type(exc).__name__
            # 运行时缺失（Node/容器没起来）绝不能据此判死源。
            if _is_runtime_missing(exc):
                return {"ok": False, "alive": None, "reachable": False,
                        "status": 0, "count": 0, "sample": "", "hit": "",
                        "tried": [kw], "error": last_err,
                        "reason": f"{kind} 运行时不可用，未参与判定",
                        "elapsed_ms": int((time.monotonic() - t0) * 1000)}
            # 站点侧超时/连接失败：网络层抖动，不能据此判死源，只计数。
            if _is_net_layer_error(exc):
                hard_fail += 1
                continue
            # 站点明确应答了但内容不可用（返回 HTML/乱码/结构不符）→ 强证据判死。
            site_bad += 1
            continue
        n = len(titles or [])
        if n > 0:
            return {"ok": True, "alive": True, "reachable": True, "status": 200,
                    "count": n, "sample": "", "hit": kw, "tried": [kw],
                    "error": "",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    if hard_fail and hard_fail >= len(list(keywords)):
        # 每个热门词都超时/连不上。但纯网络层失败不足以判死源（沙箱/内网 DNS
        # 抖动、移动网络握手超时都会命中），只报“不下结论”，交给人工。
        return {"ok": True, "alive": None, "reachable": False, "status": 0,
                "count": 0, "sample": "", "hit": "",
                "tried": list(keywords), "error": last_err,
                "reason": f"{kind} 全部基准词无响应（网络层），不下结论",
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    if site_bad:
        # 站点有应答但内容不可用（老接口下线、返回 HTML、结构不符）→ 判失效。
        return {"ok": True, "alive": False, "reachable": True, "status": 0,
                "count": 0, "sample": "", "hit": "",
                "tried": list(keywords), "error": last_err,
                "reason": f"{kind} 接口无有效数据（{last_err}），判失效",
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    return {"ok": True, "alive": None, "reachable": True, "status": 200,
            "count": 0, "sample": "", "hit": "", "tried": list(keywords),
            "error": "", "reason": "接口正常但基准热门词无结果",
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


def _source_kind_of(src: dict) -> str:
    """归一化源类型。

    历史坑：T4 源用 kind="t4" 标记而非 t4 布尔字段，旧代码 `src.get("t4")`
    恒为空 → T4 源全部掉进 CMS 分支用空 api 探测，判死误删。
    """
    k = str(src.get("kind") or "").strip().lower()
    if k in ("t4", "catvod", "drpy", "spider", "dduo"):
        return k
    if src.get("spider_src"):
        return "spider"
    if src.get("drpy"):
        return "drpy"
    if src.get("catvod"):
        return "catvod"
    if src.get("xsrc"):
        return "dduo"
    if src.get("kind") == "t4":
        return "t4"
    return "cms"


def _is_runtime_missing(exc: BaseException) -> bool:
    """异常是否属于「本地运行时缺失」而非「站点挂了」。

    侧车（Node/容器）没起来时抛的是 FileNotFoundError / ConnectionRefused
    到本机回环 / 显式的 RuntimeNotReady，此时不拿它判源失效。
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "runtime" in name and "not" in name:
        return True
    if isinstance(exc, (FileNotFoundError,)):
        return True
    for key in ("connection refused", "connect call failed",
                "sidecar", "runner not", "no such file"):
        if key in msg:
            return True
    return False


# >>> PRIVATE:DDUO
# ---------------- 多多影视（非标准源，仅完整构建） ----------------

def _dduo_root(source: dict) -> str:
    import dduo
    return dduo.base_of(source)


def _dduo_map_vod(v: dict, source: dict, detail: dict | None = None) -> OnlineTitle | None:
    """把多多 vod 映射成 OnlineTitle。

    搜索结果本身不含播放线路，需额外拉一次详情；为控制搜索时延，
    detail=None 时构造一个 sources 为空的标题（详情按需补拉）。
    """
    import dduo
    detail = detail if detail is not None else v
    sources_map: dict[str, list[Episode]] = {}
    root = _dduo_root(source)
    if detail.get("vod_play_url"):
        for line in dduo.parse_lines(detail):
            eps = []
            for i, tok in enumerate(line.tokens):
                eps.append(Episode(
                    title=f"第{i + 1}集",
                    url=tok,                       # 加密 token，播放时解码
                    direct=True,                  # 视作可播（解码后是媒体地址）
                    headers={"Referer": root + "/", "User-Agent": "Mozilla/5.0"},
                    xsrc=True,
                    play_from=line.play_from,
                ))
            if eps:
                # 用「线路标识」做内部 key（稳定），展示名在 app 层映射
                sources_map[line.play_from] = eps
    # 已知仅限其自家 App、网页端解码必回 PLATFORM_NOT_ALLOWED 的线路排到最后
    sources_map = dict(sorted(
        sources_map.items(),
        key=lambda kv: kv[0].lower().startswith(("zijian", "app"))))
    area = detail.get("vod_area", "")
    if isinstance(area, list):
        area = "/".join(str(x) for x in area)
    return OnlineTitle(
        provider=source["id"],
        vod_id=str(detail.get("vod_id")),
        title=(detail.get("vod_name") or "").strip(),
        type_name=detail.get("type_name", "") or "",
        year=str(detail.get("vod_year", "") or ""),
        area=area or "",
        updated=str(detail.get("vod_time", "") or ""),
        remarks=detail.get("vod_remarks", "") or "",
        score=str(detail.get("vod_douban_score", "")
                  or detail.get("vod_score", "") or ""),
        poster=detail.get("vod_pic", "") or "",
        overview=(detail.get("vod_content") or detail.get("vod_blurb") or "").strip(),
        actor=detail.get("vod_actor", "") or "",
        director=detail.get("vod_director", "") or "",
        duration=parse_duration(detail.get("vod_duration") or ""),
        sources=sources_map,
    )


async def dduo_search_source(client: httpx.AsyncClient, source: dict,
                             keyword: str) -> list[OnlineTitle]:
    """多多搜索：先搜出列表，再逐个补详情（取到可播线路）。

    详情并发拉取，限制前 N 条控制时延；失败的条目降级为空线路（不可播则丢弃）。
    """
    import asyncio
    import dduo
    vods = await dduo.search(client, source, keyword)
    if not vods:
        return []

    async def one(v: dict) -> OnlineTitle | None:
        try:
            detail = await dduo.get_detail(
                client, source, str(v.get("vod_id"))) or v
            t = _dduo_map_vod(v, source, detail)
            if t and t.title and t.sources:
                return t
        except Exception:
            _log.debug("one failed", exc_info=True)
            return None
        return None

    results = await asyncio.gather(
        *[one(v) for v in vods[:12]], return_exceptions=True)
    return [t for t in results if isinstance(t, OnlineTitle)]


async def dduo_probe_source(source: dict, keyword: str = "庆余年",
                            timeout: float = 10.0) -> dict:
    """探测多多源：搜索接口可用即视为在线。"""
    import dduo
    t0 = time.monotonic()
    try:
        # 上游 v1.0.0 probe_source 做法：探测经 netutil.async_client
        #（按代理模式直连/走代理/自动切代理）；DDUO 是 fork 私有模块，
        # 上游无同名函数，这里搬的是上游通用探测的客户端构造方式。
        async with netutil.async_client(timeout=timeout) as client:
            vods = await dduo.search(client, source, keyword)
            ms = int((time.monotonic() - t0) * 1000)
            if vods:
                return {"ok": True, "status": 200, "elapsed_ms": ms,
                        "count": len(vods),
                        "sample": vods[0].get("vod_name", ""), "error": ""}
            return {"ok": True, "status": 200, "elapsed_ms": ms, "count": 0,
                    "sample": "", "error": "接口正常（该词无结果）"}
    except Exception as exc:
        _log.debug("dduo_probe_source failed: source=%s", source, exc_info=True)
        return {"ok": False, "status": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "count": 0, "sample": "", "error": type(exc).__name__}


# <<< PRIVATE:DDUO


# >>> PRIVATE:CATVOD
# ---------------- 小雅网盘（catvod JVM 爬虫，仅完整构建） ----------------

def _xiaoya_map_item(v: dict) -> OnlineTitle:
    """把小雅分类/搜索列表项映射成 OnlineTitle。

    列表项不带播放线路（线路要 detail 才有），这里用一个占位 Episode 承载
    vod_id，供聚合层判定"可播"；真正线路在按需详情时补全。
    """
    vod_id = str(v.get("vod_id") or "")
    episodes = []
    if vod_id:
        episodes.append(Episode(
            title="播放", url=vod_id, direct=True,
            headers={"User-Agent": "Mozilla/5.0"},
            play_from="xiaoya", catvod=True,
        ))
    return OnlineTitle(
        provider="xiaoya",
        vod_id=vod_id,
        title=(v.get("vod_name") or "").strip(),
        type_name=v.get("type_name", "") or "",
        year=str(v.get("vod_year", "") or ""),
        area=v.get("vod_area", "") or "",
        updated="",
        remarks=v.get("vod_remarks", "") or "",
        score="",
        poster=v.get("vod_pic", "") or "",
        overview=(v.get("vod_content") or "").strip(),
        actor="",
        director="",
        sources={"小雅网盘": episodes} if episodes else {},
    )


async def xiaoya_search_source(client, source: dict,
                               keyword: str) -> list[OnlineTitle]:
    """小雅搜索：catvod 爬虫检索全库索引。不逐个补详情（线路在播放时按需解析）。"""
    import asyncio
    import catvod
    try:
        lst = await asyncio.to_thread(catvod.search, keyword,
                                      (await catvod.xiaoya_token()))
    except Exception:
        _log.debug("xiaoya_search_source failed: source=%s", source, exc_info=True)
        return []
    out = []
    for v in lst:
        t = _xiaoya_map_item(v)
        if t.title and t.sources:
            out.append(t)
    return out


# <<< PRIVATE:CATVOD


# >>> PRIVATE:DRPY
# ---------------- drpyS JS 爬虫（Node sidecar，仅完整构建） ----------------

def _drpy_site(source: dict) -> dict:
    """把内部 source 映射成 sidecar 需要的 {"ext": <规则脚本绝对路径>}。"""
    api = source.get("api", "")
    script = api[len("drpy://"):] if api.startswith("drpy://") else ""
    return {"ext": _os.path.join(_DRPY_DIR, script) if script else ""}


def _drpy_provider(source: dict) -> str:
    return source.get("id", "drpy")


def _drpy_map_item(source: dict, v: dict) -> OnlineTitle:
    vod_id = str(v.get("vod_id") or "")
    episodes = []
    if vod_id:
        episodes.append(Episode(
            title="播放", url=vod_id, direct=True,
            headers={"User-Agent": "Mozilla/5.0"},
            play_from=_drpy_provider(source), drpy=True,
        ))
    return OnlineTitle(
        provider=_drpy_provider(source),
        vod_id=vod_id,
        title=(v.get("vod_name") or "").strip(),
        type_name=v.get("type_name", "") or "",
        year=str(v.get("vod_year", "") or ""),
        area=v.get("vod_area", "") or "",
        updated="",
        remarks=v.get("vod_remarks", "") or "",
        score="",
        poster=v.get("vod_pic", "") or "",
        overview=(v.get("vod_content") or "").strip(),
        actor="",
        director="",
        sources={source.get("name", "JS源"): episodes} if episodes else {},
    )


async def drpy_search_source(client, source: dict,
                             keyword: str) -> list[OnlineTitle]:
    import asyncio
    import drpy
    try:
        site = _drpy_site(source)
        fut = asyncio.to_thread(drpy.search, site, keyword, False, 1)
        # 与爬虫搜索同一上限：8s 内无结果即放弃（shield：超时只断开等待，
        # 不取消底层的 to_thread 任务本身）。
        lst = await asyncio.wait_for(asyncio.shield(fut), timeout=8)
    except Exception:
        _log.debug("drpy_search_source failed: source=%s", source, exc_info=True)
        return []
    out = []
    for v in lst:
        t = _drpy_map_item(source, v)
        if t.title and t.sources:
            out.append(t)
    return out


# <<< PRIVATE:DRPY



# >>> PRIVATE:SPIDER
# ---------------- 普通 csp_ 爬虫（订阅导入，redroid 按各自 jar） ----------------

def _spider_conf(source: dict) -> dict | None:
    import spiders
    return spiders.site(source.get("spider_key") or "")


def _spider_provider(source: dict) -> str:
    return source.get("id", "spider")


def _spider_map_item(source: dict, v: dict) -> OnlineTitle:
    vod_id = str(v.get("vod_id") or "")
    episodes = []
    if vod_id:
        episodes.append(Episode(
            title="播放", url=vod_id, direct=True,
            headers={"User-Agent": "Mozilla/5.0"},
            play_from=_spider_provider(source), xsrc=True, spider_src=True,
        ))
    return OnlineTitle(
        provider=_spider_provider(source),
        vod_id=vod_id,
        title=(v.get("vod_name") or "").strip(),
        type_name=v.get("type_name", "") or "",
        year=str(v.get("vod_year", "") or ""),
        area=v.get("vod_area", "") or "",
        updated="",
        remarks=v.get("vod_remarks", "") or "",
        score=str(v.get("vod_score", "") or ""),
        poster=v.get("vod_pic", "") or "",
        overview=(v.get("vod_content") or v.get("vod_blurb") or "").strip(),
        actor=v.get("vod_actor", "") or "",
        director=v.get("vod_director", "") or "",
        sources={source.get("name", "爬虫源"): episodes} if episodes else {},
    )


async def spider_search_source(client, source: dict, keyword: str) -> list[OnlineTitle]:
    import asyncio
    import spiders
    conf = _spider_conf(source)
    if not conf or not spiders.available():
        return []
    try:
        lst = await asyncio.to_thread(spiders.search, conf, keyword, False, 1)
    except Exception:
        _log.debug("spider_search_source failed: source=%s", source, exc_info=True)
        return []
    out = []
    for v in lst:
        t = _spider_map_item(source, v)
        if t.title and t.vod_id:
            out.append(t)
    return out


# <<< PRIVATE:SPIDER


async def search_all(keyword: str, sources=None,
                     timeout: float | None = None,
                     soft_timeout: float | None = None,
                     *, budget: float | None = None,
                     visible=None,
                     early_min_wait: float | None = None,
                     early_min_results: int | None = None) -> list[OnlineTitle]:
    """聚合搜索所有启用源。

    timeout/soft_timeout/budget：三者任一给出即为总预算（秒），到点返回已
    拿到的部分结果，未完成的源取消（不因单个慢源而整块超时清空）。
    为 None 时等全部完成。T4 与普通源交错排队、最多 8 路并发，避免 T4 源
    排在所有慢 CMS 后面。sources=[] 保持空列表，不恢复默认源。

    visible：可选回调 ``OnlineTitle -> bool``，早返回阈值按它判为可见的
    标题数计算（增量计数，到达的标题逐条判定，不重算）。不传时沿用旧行
    为：按原始结果总数计算。调用方（app.online_search）传按查看者可打开
    的库判定的回调：快源先回的 25 条若全属被隐藏的库，可见数为 0，不触
    发早返回，慢源里本可见的标题就不会被提前取消；判定异常按不可见算。
    本函数只用该回调做早返回决策，返回的仍是全部结果，过滤仍在上层做。
    """
    from itertools import zip_longest
    sources = DEFAULT_SOURCES if sources is None else sources
    enabled = [s for s in sources if s.get("enabled")]
    # T4 源不排在所有普通源后面：T4/普通交错，最多 8 路并发。
    # PanSou 不进全局搜索（仅播放菜单手动线）。
    remote = [s for s in enabled if s.get("kind") == "t4"]
    legacy = [s for s in enabled if s.get("kind") != "t4"]
    scheduled = [s for pair in zip_longest(remote, legacy)
                 for s in pair if s is not None]
    if budget is None:
        budget = soft_timeout if soft_timeout is not None else timeout
    sem = asyncio.Semaphore(8)

    async def _one(source):
        async with sem:
            return await _dispatch_search(client, source, keyword)

    async with netutil.async_client() as client:
        if not scheduled:
            return []
        tasks = [asyncio.create_task(_one(s)) for s in scheduled]
        if budget is None:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            # 够快即返回（上游 v0.2.70）：快源先完成、累计结果达到阈值且过了最小
            # 等待后，立即取消仍在途的慢源，不必干等满预算；预算仍作兜底，保证
            # 慢词/冷门词能拿全结果。只改预算分支的等待方式，不动源健康降级
            # （search_source 内）与并发/交错调度。
            # early_* 由调用方覆盖（二级目录冷片绑定传 0.4s/15 条，让最快源
            # ~0.5s 即收手）；不传时保持旧默认 0.8s/25 条，其他调用方行为不变。
            if early_min_results is None:
                early_min_results = 25
            if early_min_wait is None:
                early_min_wait = 0.8
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(budget)
            early_gate = loop.time() + min(early_min_wait, float(budget))
            pending = set(tasks)
            results = []
            seen_ids = set()
            # 早返回阈值按可见数计：visible 传入时只累计判定为可见的标题，
            # 增量计数（到达即判，不重算）；不传时沿用原始总数。
            visible_total = 0
            while pending and loop.time() < deadline:
                done, pending = await asyncio.wait(
                    pending, timeout=min(0.25, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.cancelled() and t.exception() is None:
                        r = t.result()
                        if isinstance(r, list):
                            results.append(r)
                            seen_ids.add(id(t))
                            if visible is not None:
                                for title in r:
                                    try:
                                        if visible(title):
                                            visible_total += 1
                                    except Exception:
                                        _log.debug("search_all failed", exc_info=True)
                                        pass
                if visible is not None:
                    total = visible_total
                else:
                    total = sum(len(x) for x in results if isinstance(x, list))
                if loop.time() >= early_gate and total >= early_min_results:
                    break
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 被取消/超时的任务若已产出也尽量回收。按任务身份去重，不再用
            # 列表相等比较（OnlineTitle 是 dataclass，逐字段比较是 O(n²)）。
            for t in tasks:
                if (t.done() and not t.cancelled() and t.exception() is None
                        and id(t) not in seen_ids):
                    r = t.result()
                    if isinstance(r, list):
                        results.append(r)
                        seen_ids.add(id(t))
    # 汇总：聚合搜索里单个源抛异常会被上面的结果过滤悄悄丢掉，这里按源记一条日志
    # （只记源 id 与数量，不记搜索关键词）。
    failed = [s.get("id", "?") for s, t in zip(scheduled, tasks)
              if t.done() and not t.cancelled() and t.exception() is not None]
    if failed:
        _log.warning("search_all: %d/%d sources failed: ids=%s",
                     len(failed), len(scheduled), failed)
    titles: list[OnlineTitle] = []
    for r in results:
        if isinstance(r, list):
            titles.extend(r)
    return titles


async def _dispatch_search(client, source, keyword):
    """按源类型把一次搜索分发到对应的搜索实现。"""
    # >>> PRIVATE:CATVOD
    if features.CATVOD and source.get("catvod"):
        return await xiaoya_search_source(client, source, keyword)
    # <<< PRIVATE:CATVOD
    # >>> PRIVATE:DRPY
    if features.DRPY and source.get("drpy"):
        return await drpy_search_source(client, source, keyword)
    # <<< PRIVATE:DRPY
    # >>> PRIVATE:PAN
    # PanSou 网盘搜索：结果映射为真实的一集（上游同款行为 ——
    # 上游 pansou.py 把每条分享链接作为该标题下的一集，播放时转存出直链）。
    if source.get("pansou"):
        import pansou_search as _pss
        try:
            return await _pss.search(client, source, keyword)
        except Exception as exc:
            _log.warning("pansou search failed: %s", exc, exc_info=True)
            return []
    # <<< PRIVATE:PAN
    # >>> PRIVATE:SPIDER
    if source.get("spider_src"):
        return await spider_search_source(client, source, keyword)
    # <<< PRIVATE:SPIDER
    # >>> PRIVATE:DDUO
    if features.DDUO and source.get("xsrc"):
        return await dduo_search_source(client, source, keyword)
    # <<< PRIVATE:DDUO
    return await search_source(client, source, keyword)
