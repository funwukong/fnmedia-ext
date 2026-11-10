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

try:
    import sourcehealth as _health
except Exception:
    _health = None  # type: ignore

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
# 不能放进下面的加固标记块——公开构建剥离该块后它们必须还在。私有源的档位由块内
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


# >>> PRIVATE:GUARD
# DexGuard 加固源（csp_*Guard）：真实逻辑在 Android 模拟器内解密执行。

# 加固源（guard-<key>）默认质量档；guard:// 源通常为秒播、片源较新，给较高基础分。
GUARD_DEFAULT_PRIORITY = 50
# 网盘聚合 guard 源（夸克/UC 原画、无广告、>2GB 大文件也可）质量最高。
GUARD_PAN_PRIORITY = 58
# 普通 csp_ 爬虫源（饭太硬等订阅导入的纯 Java 在线源，经 redroid 真机解析）。
SPIDER_DEFAULT_PRIORITY = 36
DRPY_DEFAULT_PRIORITY = 15
DDUO_DEFAULT_PRIORITY = 15

# 输出夸克/UC 分享链接、已由自有网盘模块原画播放的 guard 源（与 guard.py
# _PAN_PLAYABLE_KEYS 对应）。
_GUARD_PAN_KEYS = {"YpanSo"}


def _guard_source_priority(source: dict) -> int | None:
    """私有源的质量档位；通用源返回 None，走 DEFAULT_PRIORITY 通用表。"""
    sid = source.get("id", "")
    if source.get("guard"):
        if source.get("guard_key") in _GUARD_PAN_KEYS:
            return GUARD_PAN_PRIORITY
        return GUARD_DEFAULT_PRIORITY
    if source.get("spider_src"):
        return SPIDER_DEFAULT_PRIORITY
    if source.get("drpy"):
        return DRPY_DEFAULT_PRIORITY
    if source.get("dduo") or sid == "dduo":
        return DDUO_DEFAULT_PRIORITY
    return None


_private_source_priority = _guard_source_priority


def guard_source_defs() -> list:
    """当前全部加固源（内置 sites.json + 用户导入 imported.json）。

    每次调用都读 guard 模块的实时视图，使“导入 TVBox 配置”后无需重启即可
    进入源列表与聚合搜索。返回可直接并入 DEFAULT_SOURCES 的源字典。
    """
    try:
        import guard as _g
        if not _g.feature_on():
            return []
        out = []
        for key, gs in _g._load_sites().items():
            out.append({
                "id": "guard-" + key,
                "name": gs.get("name") or key,
                "api": "guard://" + key,
                "enabled": bool(gs.get("enabled")),
                "xsrc": True,
                "guard": True,
                "guard_key": key,
                "builtin": bool(gs.get("_builtin", True)),
            })
        return out
    except Exception:
        return []


def _bootstrap_guard_sources() -> None:
    if not features.GUARD:
        return
    for d in guard_source_defs():
        if not any(s["id"] == d["id"] for s in DEFAULT_SOURCES):
            DEFAULT_SOURCES.append(d)
    # 普通 csp_ 爬虫源（订阅导入，共用安卓运行时，按各自 jar 加载）
    try:
        import spiders as _sp
        for d in _sp.source_defs():
            if not any(s["id"] == d["id"] for s in DEFAULT_SOURCES):
                DEFAULT_SOURCES.append(d)
    except Exception:
        pass


_bootstrap_guard_sources()
# <<< PRIVATE:GUARD

HTTP_TIMEOUT = 12.0
# 加固（真机）搜索单源硬超时：聚合搜索要 gather 全部源，真机卡顿不能拖垮整体。
# 取较短值让慢加固源快速失败，纯 CMS 源通常 5s 内返回，不影响热榜片起播。
GUARD_SEARCH_TIMEOUT = 8.0
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
    # >>> PRIVATE:GUARD
    # 加固爬虫：播放前由 Android runner 的 playerContent 解析（公开构建恒 False）
    guard: bool = False
    # <<< PRIVATE:GUARD


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
    locator: dict = field(default_factory=dict)

    @property
    def online_id(self) -> str:
        return f"{self.provider}:{self.vod_id}"


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
        year=str(v.get("vod_year", "") or ""),
        area=v.get("vod_area", "") or "",
        updated=str(v.get("vod_time", "") or v.get("vod_time_add", "") or ""),
        remarks=v.get("vod_remarks", "") or "",
        score=str(v.get("vod_score", "") or ""),
        poster=v.get("vod_pic", "") or "",
        overview=(v.get("vod_content") or v.get("vod_blurb") or "").strip(),
        actor=v.get("vod_actor", "") or "",
        director=v.get("vod_director", "") or "",
        sources=sources,
    )


def _site_root(api: str) -> str:
    p = urllib.parse.urlparse(api)
    return f"{p.scheme}://{p.netloc}/"


async def search_source(client: httpx.AsyncClient, source: dict,
                        keyword: str) -> list[OnlineTitle]:
    # P2 源健康自动降级：连续失败 5 次的源在 3 分钟冷却窗内直接跳过，
    # 不再每次都等超时拖慢整体搜索；冷却过后自动放行一次试探。
    if _health is not None and not _health.usable(source.get("id", "")):
        _log.info("source skipped (dead cooldown): id=%s", source.get("id"))
        return []
    _t0 = time.monotonic()
    if source.get("kind") == "t4":
        import t4 as _t4mod
        try:
            out = await _t4mod.search(client, source, keyword)
        except _t4mod.T4Error as exc:
            _log.warning("T4 search failed: id=%s error=%s elapsed=%.2fs",
                         source.get("id"), exc, time.monotonic() - _t0)
            if _health is not None:
                _health.record(source.get("id", ""), False, time.monotonic() - _t0)
            return []
        if _health is not None:
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
            if _health is not None:
                _health.record(source.get("id", ""), False, time.monotonic() - _t0)
            return []
        # 苹果 CMS at/xml：RSS XML 响应，先转成与 at/json 等价的 dict
        j = parse_cms_xml(r.text) if _is_xml_api(source["api"]) else r.json()
    except Exception as e:
        _log.warning("source search failed: id=%s error=%s elapsed=%.2fs",
                     source.get("id"), type(e).__name__, time.monotonic() - _t0)
        if _health is not None:
            _health.record(source.get("id", ""), False, time.monotonic() - _t0)
        return []
    if _health is not None:
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
    # 它是纯 HTTP 解析，可替代部分原本要靠加固爬虫或 WebView 的网页源。
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
                return {"ok": False, "status": r.status_code,
                        "elapsed_ms": ms, "count": 0, "sample": "",
                        "error": f"HTTP {r.status_code}"}
            # 同时支持 at/json 与 at/xml（XML 先转等价 dict）
            j = parse_cms_xml(r.text) if _is_xml_api(api) else r.json()
    except Exception as exc:
        return {"ok": False, "status": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "count": 0, "sample": "", "error": type(exc).__name__}
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
    for kw in keywords:
        tried.append(kw)
        r = await probe_source(api, keyword=kw, timeout=timeout, source=source)
        last = r
        if r.get("ok") and (r.get("count") or 0) > 0:
            hit = kw
            return {**r, "alive": True, "hit": hit, "tried": tried,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    # 全部关键词都无结果：判定失效（保留最后一次的错误/状态供展示）
    return {**last, "alive": False, "hit": "", "tried": tried,
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


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
        return {"ok": False, "status": 0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "count": 0, "sample": "", "error": type(exc).__name__}


async def dduo_get_title(client: httpx.AsyncClient, source: dict,
                         vod_id: str) -> OnlineTitle | None:
    """按 vod_id 拉详情并映射（供按需详情/独立库使用）。"""
    import dduo
    detail = await dduo.get_detail(client, source, vod_id)
    if not detail:
        return None
    return _dduo_map_vod(detail, source, detail)
# <<< PRIVATE:DDUO


# >>> PRIVATE:CATVOD
# ---------------- 小雅网盘（catvod JVM 爬虫，仅完整构建） ----------------

def _xiaoya_map_item(v: dict) -> OnlineTitle:
    """把小雅分类/搜索列表项映射成 OnlineTitle。

    列表项不带播放线路（线路要 detail 才有），这里用一个占位 Episode 承载
    vod_id，供聚合层判定"可播"；真正线路在按需详情（xiaoya_get_title）补全。
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
        return []
    out = []
    for v in lst:
        t = _xiaoya_map_item(v)
        if t.title and t.sources:
            out.append(t)
    return out


async def xiaoya_get_title(client, source: dict, vod_id: str) -> OnlineTitle | None:
    """按 vod_id 拉详情，解析出真实文件线路（供按需详情/播放）。"""
    import asyncio
    import catvod
    tok = await catvod.xiaoya_token()
    det = await asyncio.to_thread(catvod.detail, vod_id, tok)
    if not det:
        return None
    eps = catvod.parse_episodes(det)
    episodes = [Episode(
        title=e["title"], url=e["id"], direct=True,
        headers={"User-Agent": "Mozilla/5.0"},
        play_from="xiaoya:" + e["line"], catvod=True,
    ) for e in eps]
    t = _xiaoya_map_item(det)
    t.sources = {}
    # 按线路分组（同一部影片的文件通常只有一个目录线路）
    groups: dict[str, list] = {}
    for ep in eps:
        groups.setdefault(ep["line"], []).append(
            Episode(title=ep["title"], url=ep["id"], direct=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                    play_from="xiaoya:" + ep["line"], catvod=True))
    t.sources = groups
    return t
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
        lst = await asyncio.to_thread(drpy.search, site, keyword, False, 1)
    except Exception:
        return []
    out = []
    for v in lst:
        t = _drpy_map_item(source, v)
        if t.title and t.sources:
            out.append(t)
    return out


async def drpy_get_title(client, source: dict, vod_id: str) -> OnlineTitle | None:
    import asyncio
    import drpy
    site = _drpy_site(source)
    try:
        det = await asyncio.to_thread(drpy.detail, site, vod_id)
    except Exception:
        return None
    if not det:
        return None
    prov = _drpy_provider(source)
    t = _drpy_map_item(source, det)
    groups: dict[str, list] = {}
    for e in drpy.parse_episodes(det):
        groups.setdefault(e["line"], []).append(
            Episode(title=e["title"], url=e["id"], direct=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                    play_from=prov + ":" + e["line"], drpy=True))
    t.sources = groups
    return t
# <<< PRIVATE:DRPY

# >>> PRIVATE:GUARD
# ---------------- DexGuard 加固爬虫（Android runner，仅完整构建） ----------------

def _guard_conf(source: dict) -> dict | None:
    import guard
    return guard.site(source.get("guard_key") or "")


def _guard_provider(source: dict) -> str:
    return source.get("id", "guard")


def _guard_map_item(source: dict, v: dict) -> OnlineTitle:
    vod_id = str(v.get("vod_id") or "")
    episodes = []
    if vod_id:
        episodes.append(Episode(
            title="播放", url=vod_id, direct=True,
            headers={"User-Agent": "Mozilla/5.0"},
            play_from=_guard_provider(source), xsrc=True, guard=True,
        ))
    return OnlineTitle(
        provider=_guard_provider(source),
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
        sources={source.get("name", "加固源"): episodes} if episodes else {},
    )


async def guard_search_source(client, source: dict, keyword: str) -> list[OnlineTitle]:
    import asyncio
    import guard
    conf = _guard_conf(source)
    if not conf or not guard.available():
        return []
    try:
        # 加固搜索走安卓真机（to_thread 本身无超时）。聚合搜索会并发几十个源并
        # gather 等全部完成，若不给硬超时，单台设备卡顿（深预热后偶发降级）会把整次
        # 搜索拖过播放端 ~20s 窗口，导致热榜片点击播放 503。这里快速失败、绝不拖累聚合。
        lst = await asyncio.wait_for(
            asyncio.to_thread(guard.search, conf, keyword, False, 1),
            timeout=GUARD_SEARCH_TIMEOUT)
    except Exception:
        return []
    out = []
    for v in lst:
        t = _guard_map_item(source, v)
        if t.title and t.vod_id:
            out.append(t)
    return out


async def guard_get_title(client, source: dict, vod_id: str) -> OnlineTitle | None:
    import asyncio
    import guard
    conf = _guard_conf(source)
    if not conf:
        return None
    # 网盘聚合源（夸父等）：搜索返回的 vod_id 本身就是完整夸克/UC 分享链接。
    # 其 detail 走 Cloudflare 常需 15s+（超过 22s 即超时），且详情集用夸父私有
    # sToken 编码（我们无法直接播放）。直接短路：用分享链接作唯一可播线路，
    # 播放时 _resolve_guard 识别网盘 → 自有账号转存原画，避免超时与空集。
    if _PAN_SHARE_RE.search(str(vod_id)):
        t = _guard_map_item(source, {"vod_id": str(vod_id).strip()})
        if t.sources:
            return t
    try:
        det = await asyncio.to_thread(guard.detail, conf, vod_id)
    except Exception:
        return None
    if not det:
        return None
    t = _guard_map_item(source, det)
    groups: dict[str, list[Episode]] = {}
    for e in guard.parse_episodes(det):
        groups.setdefault(e["line"], []).append(
            Episode(title=e["title"], url=e["id"], direct=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                    play_from=_guard_provider(source) + ":" + e["line"],
                    xsrc=True, guard=True))
    t.sources = groups
    return t
# <<< PRIVATE:GUARD


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
        return []
    out = []
    for v in lst:
        t = _spider_map_item(source, v)
        if t.title and t.vod_id:
            out.append(t)
    return out


async def spider_get_title(client, source: dict, vod_id: str) -> OnlineTitle | None:
    import asyncio
    import spiders
    import guard
    conf = _spider_conf(source)
    if not conf:
        return None
    try:
        det = await asyncio.to_thread(spiders.detail, conf, vod_id)
    except Exception:
        return None
    if not det:
        return None
    t = _spider_map_item(source, det)
    groups: dict[str, list[Episode]] = {}
    for e in guard.parse_episodes(det):
        groups.setdefault(e["line"], []).append(
            Episode(title=e["title"], url=e["id"], direct=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                    play_from=_spider_provider(source) + ":" + e["line"],
                    xsrc=True, spider_src=True))
    t.sources = groups
    return t
# <<< PRIVATE:SPIDER


async def search_all(keyword: str, sources=None,
                     timeout: float | None = None,
                     soft_timeout: float | None = None,
                     *, budget: float | None = None) -> list[OnlineTitle]:
    """聚合搜索所有启用源。

    timeout/soft_timeout/budget：三者任一给出即为总预算（秒），到点返回已
    拿到的部分结果，未完成的源取消（不因单个慢源而整块超时清空）。
    为 None 时等全部完成。T4 与普通源交错排队、最多 8 路并发，避免 T4 源
    排在所有慢 CMS 后面。sources=[] 保持空列表，不恢复默认源。
    """
    from itertools import zip_longest
    sources = DEFAULT_SOURCES if sources is None else sources
    enabled = [s for s in sources if s.get("enabled")]
    # T4 源不排在所有普通源后面：T4/普通交错，最多 8 路并发。
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
            early_min_results, early_min_wait = 25, 0.8
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(budget)
            early_gate = loop.time() + min(early_min_wait, float(budget))
            pending = set(tasks)
            results = []
            while pending and loop.time() < deadline:
                done, pending = await asyncio.wait(
                    pending, timeout=min(0.25, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.cancelled() and t.exception() is None:
                        r = t.result()
                        if isinstance(r, list):
                            results.append(r)
                total = sum(len(x) for x in results if isinstance(x, list))
                if loop.time() >= early_gate and total >= early_min_results:
                    break
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 被取消/超时的任务若已产出也尽量回收
            for t in tasks:
                if t.done() and not t.cancelled() and t.exception() is None:
                    r = t.result()
                    if isinstance(r, list) and r not in results:
                        results.append(r)
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
    # >>> PRIVATE:GUARD
    if features.GUARD and source.get("guard"):
        return await guard_search_source(client, source, keyword)
    if source.get("spider_src"):
        return await spider_search_source(client, source, keyword)
    # <<< PRIVATE:GUARD
    # >>> PRIVATE:DDUO
    if features.DDUO and source.get("xsrc"):
        return await dduo_search_source(client, source, keyword)
    # <<< PRIVATE:DDUO
    return await search_source(client, source, keyword)


def detail_source(source: dict, vod_id: str) -> OnlineTitle | None:
    """同步取详情（带缓存由调用方做）。"""
    import requests  # 仅 PoC；主代理内用 httpx 异步版
    return None
