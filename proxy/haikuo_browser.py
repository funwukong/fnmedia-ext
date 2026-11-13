"""海阔小程序选片库：已导入规则的浏览入口（首页/分类/标题卡）。

v1 的海阔源只做了搜索+详情+播放：catalog 明确排除 haikuo（不建聚合
路由），mdb_detail/item_list 也没有 haikuo 分支，所以源在资源站里能
看到、点开却是空的。本模块按 T4Browser 的模式补上浏览入口——

* 根节点：每条已启用的海阔规则一个首页文件夹；
* 首页节点：规则有 class_name/class_url 时列分类文件夹，其后是规则
  的 home 内容；
* 分类节点：规则的 category 内容。

首页/分类内容直接调规则的 home/category（与搜索同一条 sidecar 路径，
同一份规则文件），标题卡复用 providers._haikuo_map_item →
inject.brief_of → store.remember_merged → inject.search_item，与搜索
结果卡同一形态；点卡后走已有的详情/播放链，不改搜索与播放行为。

私有功能：公开构建剥离整个模块（见 tools/public_strip.py DROP_FILES）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path

import re as _re
import inject
import providers
import haikuo_cache
from store import video_guid as _video_guid

import haikuo_diag as _hkdiag

LIB_GUID = hashlib.md5(b"fnmedia-haikuo-browse").hexdigest()
LIB_TITLE = "海阔小程序"
_STATE = Path(__file__).resolve().parent.parent / "state" / "haikuo_browse.json"
_log = logging.getLogger("fnmedia.haikuo.browse")

# 单次 home/category 调用的等待上限（sidecar 侧另有 90s 上限；超时后
# 浏览线程继续跑，当前请求先按 502 明确报错，不静默返回空列表）。
_LISTING_TIMEOUT = 30.0
# 首页/分类的合并缓存：规则 home/category 本身是分页的，App 按 offset
# 分页取，逐页拉取并合并后再切片，避免规则页与 App 页错位漏条目。
_PAGE_CACHE_TTL = 300.0
_PAGE_CACHE_MAX_ITEMS = 500

# 分类页里的翻页控制标题（开关行里唯一要留的）：点它进下一页。
_HAIKUO_PAGER_TITLES = ("下一页", "上一页")


def _clean_haikuo_title(title: str) -> str:
    t = _re.sub(r"<[^>]+>", "", str(title or ""))
    t = _re.sub(r"^[^\w\u4e00-\u9fff]+|[\s\u00a0]+$", "", t).strip()
    return t or "未命名目录"


def _haikuo_is_pager_button(title: str) -> bool:
    """标题含“下一页/上一页”即翻页控制，不是分类开关。"""
    t = _clean_haikuo_title(title)
    return any(p in t for p in _HAIKUO_PAGER_TITLES)


class HaikuoBrowseError(ValueError):
    """浏览节点过期/源已移除/上游超时等（app 层按 502 明确报错）。"""


def _number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class HaikuoBrowser:
    """海阔规则浏览：根节点→规则首页文件夹→分类文件夹/标题卡。"""

    def __init__(self, store, config):
        self.store = store
        self.config = config
        self._nodes: dict[str, dict] = {}
        # (source_id, kind, cls) -> {"items": [...], "next_page": int,
        # "exhausted": bool, "built_at": float}
        self._page_cache: dict[tuple, dict] = {}
        try:
            if _STATE.exists():
                data = json.loads(_STATE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._nodes = {str(k): v for k, v in data.items()
                                   if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            _log.debug("haikuo browse state load failed", exc_info=True)
            self._nodes = {}

    # ---------------------------------------------------------- 源与节点

    def sources(self) -> list:
        """当前启用的海阔源（与 config 同步的已导入规则）。"""
        return [s for s in self.config.enabled_sources()
                if isinstance(s, dict) and s.get("haikuo")]

    def source(self, sid: str) -> dict:
        for s in self.sources():
            if s.get("id") == sid:
                return s
        raise HaikuoBrowseError("海阔源已移除，请重新导入")

    def node(self, guid: str):
        return self._nodes.get(guid)

    def _put(self, source, kind, name, parent=LIB_GUID, **values):
        identity = [source["id"], kind, str(values.get("cls", ""))]
        guid = hashlib.md5(("haikuo-directory:" + json.dumps(
            identity, sort_keys=True, ensure_ascii=True)).encode()).hexdigest()
        self._nodes[guid] = {"source_id": source["id"], "kind": kind,
                             "title": name, "parent": parent, **values}
        return guid

    def _save(self) -> None:
        try:
            if len(self._nodes) > 2048:
                self._nodes = dict(list(self._nodes.items())[-2048:])
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STATE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._nodes, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(_STATE)
        except OSError:
            _log.debug("haikuo browse state save failed", exc_info=True)

    def home_guid_for_source(self, source_id: str) -> str:
        """某条规则的首页文件夹 guid（供单源独立库入口复用）。

        与根目录 _put 的 identity 一致，多次调用得到同一 guid。
        """
        source = self.source(source_id)
        guid = self._put(source, "home", source.get("name") or "海阔")
        self._save()
        return guid

    def _trail(self, guid):
        trail, seen, cur = [], set(), guid
        while cur in self._nodes and cur not in seen:
            seen.add(cur)
            node = self._nodes[cur]
            trail.append({"fv_guid": "", "base_name": node["title"]})
            cur = node["parent"]
        return [{"fv_guid": "", "base_name": LIB_TITLE}] + list(reversed(trail))

    # ---------------------------------------------------------- 卡与库

    def card(self, guid):
        node = self._nodes[guid]
        # 文件夹卡的 path 与 file_name 必须为空：客户端把非空 path/
        # file_name 当成可播文件，面包屑路径/标题会让目录显示成点播、
        # 点开转圈（见 _vod_directory_card，已验证文件夹卡 path=""、
        # file_name=""、season_number=0）。其余字段也向已验证文件夹卡
        # 对齐：video_guid 非空（TV 端无 video_guid 的卡会被判不可见）、
        # status "在线"、不带 posters/backdrops（列表卡不带这两个键）。
        return inject._base_item(
            guid=guid, type="Directory", title=_clean_haikuo_title(node["title"]), can_play=0,
            path="", season_number=0,
            file_name="", runtime=0, video_guid=_video_guid(guid),
            parent_guid="" if node["parent"] == LIB_GUID else node["parent"],
            ancestor_guid=LIB_GUID, ancestor_name=LIB_TITLE,
            ancestor_category="Mix", status="在线",
            play_item_guid="", play_error="", logic_type=0)

    def detail(self, guid):
        """文件夹详情卡：与已验证文件夹详情（detail_item + 目录卡）同键形。

        详情不带 video_guid/path/file_name/poster/single_child_guid/
        ts/watched/lan（这些键只在列表卡/剧集详情里出现，空串与缺键
        在客户端的 null 判定里是两种结果）；补详情才有的空字段。
        """
        d = self.card(guid)
        for k in ("video_guid", "path", "file_name", "poster",
                  "single_child_guid", "ts", "watched", "lan"):
            d.pop(k, None)
        d["posters"] = ""
        d["backdrops"] = ""
        d["content_ratings"] = ""
        d["logos"] = ""
        d["original_title"] = ""
        d["production_countries"] = []
        d["is_watched"] = 0
        return d

    def library(self, *, detail=False):
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

    # ---------------------------------------------------------- 内容取数

    async def _classes(self, source):
        """规则的分类列表（无分类的规则返回 []，只出首页内容）。"""
        import haikuo

        key = providers._haikuo_site(source)["ext"]
        try:
            meta = await asyncio.to_thread(haikuo.load, key)
        except (RuntimeError, OSError, ValueError):
            _log.debug("haikuo classes load failed: %s", source.get("id"),
                       exc_info=True)
            return []
        classes = meta.get("classes") if isinstance(meta, dict) else None
        out = []
        for c in classes or []:
            if isinstance(c, dict) and (c.get("name") or c.get("url")):
                out.append({"name": str(c.get("name") or c.get("url")),
                            "url": str(c.get("url") or c.get("name"))})
        return out

    async def _fetch_page(self, source, kind, cls, page):
        """取一页规则内容（home/category），返回原始条目列表。"""
        import haikuo

        key = providers._haikuo_site(source)["ext"]
        if kind == "category":
            call = lambda: haikuo.category(key, cls, page)  # noqa: E731
        elif kind == "lazy":
            # 分类按钮导航：cls 里存的是懒 token（见 _rows），求值懒代码
            # （设分类变量）后重取 home；状态由 sidecar 恢复。
            call = lambda: haikuo.navigate(key, cls, page)  # noqa: E731
        else:
            call = lambda: haikuo.home(key, page)  # noqa: E731
        try:
            items = await asyncio.wait_for(
                asyncio.shield(asyncio.to_thread(call)),
                timeout=_LISTING_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            raise HaikuoBrowseError(
                "海阔规则加载超时，请稍后重新进入") from None
        except (RuntimeError, OSError, ValueError) as exc:
            raise HaikuoBrowseError(str(exc) or "海阔规则加载失败") from exc
        return [v for v in items] if isinstance(items, list) else []

    async def _window(self, source, kind, cls, offset, size):
        """按 App 的 offset/size 切窗口；规则页逐页拉取合并后再切片。

        返回 (卡片列表, 总数)：未拉完时总数 = 已拉条数 + 1（哨兵，App
        会继续翻页），拉完时总数 = 全部条数。
        """
        cache_key = (source["id"], kind, cls)
        now = time.monotonic()
        entry = self._page_cache.get(cache_key)
        if entry is None or now - entry["built_at"] >= _PAGE_CACHE_TTL:
            entry = {"items": [], "next_page": 1, "exhausted": False,
                     "built_at": now}
            self._page_cache[cache_key] = entry
        while (len(entry["items"]) < offset + size
               and not entry["exhausted"]):
            page_items = await self._fetch_page(
                source, kind, cls, entry["next_page"])
            entry["next_page"] += 1
            if not page_items:
                entry["exhausted"] = True
                break
            entry["items"].extend(page_items)
            if len(entry["items"]) >= _PAGE_CACHE_MAX_ITEMS:
                entry["exhausted"] = True
                break
        window = entry["items"][offset:offset + size]
        total = len(entry["items"]) + (0 if entry["exhausted"] else 1)
        return window, total

    def _rows(self, items, source, parent, category_context=None,
          page_kind="home", drop_switchers=False):
        """原始条目 → 卡片：按 col_type 分流（见 providers._haikuo_item_role）。

        folder（分类按钮）→ 目录卡（懒导航节点，点进分类）；
        video（影片）→ 标题卡（与搜索结果卡同一形态，入库后可详情/播放）；
        skip（页面 chrome）→ 一张卡都不出。
        drop_switchers=True（仅 lazy 分类页）：分类开关行（scroll_button）
        全丢，只留影片与翻页控制——navigate() 重取的是整页 home，开关行
        每次都跟着回来，不丢分类页上方就又挂 42 个目录；根目录不动。
        """
        cards, briefs = [], []
        dropped: dict[str, int] = {}
        folders = videos = 0
        id_with = id_without = 0
        for v in items:
            if not isinstance(v, dict):
                continue
            role = providers._haikuo_item_role(v, context=page_kind)
            if role == "skip":
                _ct = str(v.get("col_type") or "")
                dropped[_ct] = dropped.get(_ct, 0) + 1
                continue
            if role in ("folder", "pager"):
                token = str(v.get("url") or "")
                title = _clean_haikuo_title(v.get("title") or "")
                if not title:
                    _ct = str(v.get("col_type") or "")
                    dropped[_ct] = dropped.get(_ct, 0) + 1
                    continue
                if drop_switchers and not _haikuo_is_pager_button(title):
                    _ct = str(v.get("col_type") or "")
                    dropped[_ct] = dropped.get(_ct, 0) + 1
                    continue
                # 动态分类导航：navigate() 失败时明确报错，不让用户看到“无数据”误以为是规则空页。
                if page_kind == "home" and drop_switchers and not self._nav_ok:
                    raise HaikuoBrowseError("海阔分类导航失败，请从首页重新进入")
                # cls 存 token：节点 identity 用它区分不同分类按钮。
                guid = self._put(source, "lazy", title, parent,
                                 cls=token, token=token)
                cards.append(self.card(guid))
                folders += 1
                continue
            extra = v.get("extra") if isinstance(v.get("extra"), dict) else {}
            if str(extra.get("id") or "").strip():
                id_with += 1
            else:
                id_without += 1
            t = providers._haikuo_map_item(source, v, category_context)
            if not t.title or not t.sources:
                continue
            brief = inject.brief_of(t)
            try:
                haikuo_cache.record_discovery(
                    brief["guid"], source.get("id", ""),
                    providers._haikuo_site(source).get("ext", ""),
                    brief, category_context)
                haikuo_cache.attach_brief(brief["guid"], brief)
            except Exception:
                _log.debug("haikuo cache discovery failed", exc_info=True)
            old = self.store.get_title_by_guid(brief["guid"])
            if old is None or any(
                    old.get(k) != brief.get(k) for k in
                    ("title", "type_name", "poster", "year", "remarks",
                     "overview")):
                briefs.append(brief)
            # 海阔 brief 的 lines 恒为单集占位（由当前详情链接导出），没有
            # 已加载详情可保护；直接写新 brief 即可（与 T4 的保护逻辑不同，
            # T4 的 lines 是加载后的真实集数）。
            card = inject.search_item(brief)
            card.update(parent_guid=parent, ancestor_guid=LIB_GUID,
                        ancestor_name=LIB_TITLE, ancestor_category="Mix")
            cards.append(card)
            videos += 1
        if briefs:
            self.store.remember_merged(briefs)
        # 解析索引只记录身份/分类/状态，不在本轮列表中自动拼接，避免规则分页、
        # 推荐区和历史缓存互相重复；后续“已解析”虚拟分类单独读取该索引。
        try:
            _hkdiag.note("map", key=providers._haikuo_site(source)["ext"],
                         name=source.get("name", ""),
                         total=len(items), folders=folders, videos=videos,
                         dropped=dropped, id_with=id_with,
                         id_without=id_without)
        except Exception:
            _log.debug("haikuo diag map note failed", exc_info=True)
        return cards

    # ---------------------------------------------------------- 列表入口

    async def listing(self, client, guid, body):
        size = max(1, min(_number(body.get("page_size"), 22) or 22, 100))
        page = max(1, _number(body.get("page") or body.get("page_index"), 1))
        offset = _number(body.get("offset"), (page - 1) * size)
        if guid == LIB_GUID:
            guids, names = [], {}
            for source in self.sources():
                name = source.get("name") or "海阔"
                names[name] = names.get(name, 0) + 1
                label = name if names[name] == 1 else f"{name} ({names[name]})"
                guids.append(self._put(source, "home", label))
            cards, total = ([self.card(g) for g in guids[offset:offset + size]],
                            len(guids))
            _diag_kind, _diag_key, _diag_name = "root", "", ""
        else:
            node = self.node(guid)
            if node is None:
                raise HaikuoBrowseError("海阔目录已过期，请从海阔小程序重新进入")
            source = self.source(node["source_id"])
            if node["kind"] == "home":
                prefix = [self.card(self._put(
                    source, "category", c["name"], guid, cls=c["url"]))
                    for c in await self._classes(source)]
                cards = prefix[offset:offset + size]
                start = max(0, offset - len(prefix))
                rows, total_items = await self._window(
                    source, "home", "", start, size - len(cards))
                cards += self._rows(rows, source, guid,
                                     {"key": "home", "name": source.get("name", "首页"),
                                      "path": [source.get("name", "海阔"), "首页"],
                                      "kind": "home"}, "home")
                total = len(prefix) + total_items
            elif node["kind"] == "category":
                rows, total = await self._window(
                    source, "category", node.get("cls", ""), offset, size)
                ctx = {"key": "class:" + str(node.get("cls", "")),
                       "name": node.get("title", "未分类"),
                       "path": [source.get("name", "海阔"), node.get("title", "未分类")],
                       "kind": "category"}
                cards = self._rows(rows, source, guid, ctx, "home")
            elif node["kind"] == "lazy":
                # 分类按钮节点：token 存在 node["token"]（cls 同值，供
                # identity 用）；求值懒代码后重取 home 拿该分类的影片。
                # 开关行丢掉（见 _rows 的 drop_switchers），只留影片与翻页。
                rows, total = await self._window(
                    source, "lazy", node.get("token", node.get("cls", "")),
                    offset, size)
                ctx = {"key": "lazy:" + str(node.get("token", node.get("cls", ""))),
                       "name": node.get("title", "未分类"),
                       "path": [source.get("name", "海阔"), node.get("title", "未分类")],
                       "kind": "lazy"}
                self._nav_ok = bool(rows)
                cards = self._rows(rows, source, guid, ctx, "home", drop_switchers=True)
                self._diag_nav_state = {"token": node.get("token", ""), "ok": bool(cards), "rows": len(cards)}
            else:
                raise HaikuoBrowseError("海阔目录已过期，请从海阔小程序重新进入")
            self.source(node["source_id"])
            _diag_kind = node["kind"]
            _diag_key = node["source_id"]
            _diag_name = source.get("name", "")
        try:
            _hkdiag.note(_diag_kind, key=_diag_key, name=_diag_name,
                         count=len(cards), total=total)
        except Exception:
            _log.debug("haikuo diag listing note failed", exc_info=True)
        self._save()
        out = {"mdb_name": LIB_TITLE, "mdb_category": "Mix", "top_dir": "",
               "dir": "/".join(_clean_haikuo_title(row["base_name"]) for row in self._trail(guid)),
               "jump_list": self._trail(guid), "total": total, "list": cards}
        # 诊断字段：动态分类是否实际生效（避免 UI 一直显示无数据但看不到原因）
        if getattr(self, "_diag_nav_state", None):
            out["diag_nav"] = self._diag_nav_state
        return out
