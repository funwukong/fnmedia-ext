"""B 站独立内置媒体库：热门/分区浏览 + 分P详情 + 一条「B站自动高清」线路。

与海阔完全独立：不用 sidecar、不用海阔规则、不依赖 Node 运行时。
播放由 bilibili_play 输出飞牛可直接播的合一 MP4（见该模块说明）。

节点结构：
    根(哔哩哔哩) → 热门 / 分区 → 影片卡
影片卡 → 详情页（真实分P，每 P 一集）
"""
from __future__ import annotations

import hashlib
import logging
import time

import bilibili_site
import inject

_log = logging.getLogger("fnmedia.bilibili.browse")

LIB_GUID = hashlib.md5(b"fnmedia-bilibili-browse").hexdigest()
LIB_TITLE = "哔哩哔哩"

_LISTING_TIMEOUT = 20.0
_CACHE_TTL = 120.0

# B 站分区（官方 rid → 名称）。rid 取自 B 站新版分区表，实测
# /x/web-interface/newlist 对这些 rid 均正常返回。
_PARTITIONS = [
    ("1", "动画"), ("23", "电影"), ("11", "电视剧"), ("181", "影视"),
    ("3", "音乐"), ("4", "游戏"), ("5", "娱乐"), ("36", "科技"),
    ("119", "鬼畜"), ("129", "舞蹈"), ("160", "生活"), ("155", "时尚"),
    ("177", "纪录片"), ("202", "资讯"), ("211", "美食"),
]


class BilibiliBrowseError(ValueError):
    """浏览节点过期/上游异常等（app 层按 502 明确报错）。"""


def _number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BilibiliBrowser:
    """B 站独立浏览：根 → 分区 → 影片。"""

    def __init__(self, store, config):
        self.store = store
        self.config = config
        self._nodes: dict[str, dict] = {}
        self._cache: dict[tuple, tuple[float, list]] = {}

    # ---------------------------------------------------------- 源与节点

    def source(self):
        for s in self.config.enabled_sources():
            if isinstance(s, dict) and s.get("bili_site"):
                return s
        return None

    def enabled(self) -> bool:
        return self.source() is not None

    def node(self, guid: str):
        return self._nodes.get(guid)

    def _put(self, kind: str, name: str, parent: str = LIB_GUID,
             **values) -> str:
        identity = kind + "|" + str(values.get("cls", "")) \
            + "|" + str(values.get("page", ""))
        guid = hashlib.md5(("bilibili-directory:" + identity).encode()).hexdigest()
        self._nodes[guid] = {"kind": kind, "title": name, "parent": parent,
                             **values}
        return guid

    def home_guid(self) -> str:
        return self._put("home", LIB_TITLE)

    # ---------------------------------------------------------- 卡

    def card(self, guid: str) -> dict:
        node = self._nodes[guid]
        return inject._base_item(
            guid=guid, type="Directory", title=node["title"], can_play=0,
            path="", season_number=0, file_name="", runtime=0,
            video_guid="", parent_guid="",
            ancestor_guid=LIB_GUID, ancestor_name=LIB_TITLE,
            ancestor_category="Mix", status="在线",
            play_item_guid="", play_error="", logic_type=0)

    def library(self, *, detail: bool = False) -> dict:
        row = {"guid": LIB_GUID, "title": LIB_TITLE, "category": "Mix",
               "poster": "", "posters": [], "view_type": 0,
               "poster_type": 0, "refresh_disabled": True}
        if detail:
            row.update(name=LIB_TITLE, dir_list=[], lan="zh-CN",
                       auto_scrap_subtitle=0, include_adult=False,
                       subtitle_lan="zh-CN", last_update_time=0, sort_num=0,
                       auto_progress_thumb=0, skip_filesize=0,
                       prefer_local_nfo=0)
        return row

    # ---------------------------------------------------------- 内容

    async def _popular(self, page: int) -> list:
        key = ("popular", page)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
        import asyncio
        try:
            # bilibili_site.popular 是 async，直接 await；不要塞进 to_thread
            # （to_thread 会拿到 coroutine 对象本身）。
            items = await asyncio.wait_for(
                bilibili_site.popular(None, self.source() or {}, page),
                timeout=_LISTING_TIMEOUT)
        except Exception as exc:
            raise BilibiliBrowseError(
                "B 站加载失败：%s" % (exc or "未知错误")) from exc
        self._cache[key] = (time.monotonic(), items)
        return items

    async def _partition(self, rid: str, page: int) -> list:
        key = ("partition", rid, page)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
        import asyncio

        def _fetch():
            import httpx
            # 旧接口 /x/web-interface/dynamic/region 已下线（返回 -404
            # 「啥都木有」），这正是登录后分区列表全空的原因；改用
            # /x/web-interface/newlist（实测各分区均正常返回）。
            r = httpx.get(
                "https://api.bilibili.com/x/web-interface/newlist",
                params={"rid": rid, "pn": page, "ps": 24},
                headers=bilibili_site._headers(), timeout=12.0,
                follow_redirects=True)
            j = r.json()
            if int(j.get("code") or 0) != 0:
                raise RuntimeError(str(j.get("message") or "B 站分区接口错误")[:80])
            data = j.get("data") or {}
            raw = []
            for x in (data.get("archives") or []):
                raw.append({
                    "bvid": x.get("bvid") or "",
                    "title": x.get("title") or "",
                    "pic": x.get("pic") or "",
                    "author": (x.get("owner") or {}).get("name") or "",
                    "duration": x.get("duration") or 0,
                    "typename": "",
                    "description": x.get("desc") or "",
                })
            return raw

        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_fetch),
                                         timeout=_LISTING_TIMEOUT)
        except Exception as exc:
            raise BilibiliBrowseError(
                "B 站分区加载失败：%s" % (exc or "未知错误")) from exc
        items = []
        for x in raw:
            t = bilibili_site._map_item(x)
            if t is not None:
                items.append(t)
        self._cache[key] = (time.monotonic(), items)
        return items

    def _titles_cards(self, items) -> list:
        cards = []
        briefs = []
        for t in items:
            brief = inject.brief_of(t)
            briefs.append(brief)
            cards.append(inject.search_item(brief))
        if briefs:
            try:
                self.store.remember_merged(briefs)
            except Exception:
                _log.debug("bilibili browser remember failed", exc_info=True)
        return cards

    async def listing(self, client, guid: str, body: dict) -> dict:
        page = max(1, _number(body.get("page") or body.get("page_index"), 1))
        size = max(1, min(_number(body.get("page_size"), 22) or 22, 100))
        if guid == LIB_GUID:
            prefix = [self.card(self._put("partition", "热门"))]
            for rid, name in _PARTITIONS:
                prefix.append(self.card(self._put("partition", name,
                                                  cls=str(rid))))
            cards = prefix[:size]
            return {"mdb_name": LIB_TITLE, "mdb_category": "Mix",
                    "top_dir": "", "dir": LIB_TITLE, "total": len(prefix),
                    "list": cards, "jump_list": None}
        node = self.node(guid)
        if node is None:
            raise BilibiliBrowseError("B 站目录已过期，请重新进入")
        if node["kind"] == "partition":
            rid = node.get("cls") or "0"
            if rid == "0":
                items = await self._popular(page)
            else:
                items = await self._partition(rid, page)
            cards = self._titles_cards(items)
            return {"mdb_name": LIB_TITLE, "mdb_category": "Mix",
                    "top_dir": "", "dir": node["title"],
                    "total": len(cards), "list": cards, "jump_list": None}
        raise BilibiliBrowseError("B 站目录已过期，请重新进入")
