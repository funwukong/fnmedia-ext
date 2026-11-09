#!/usr/bin/env python3
"""fnmedia-ext 运行配置（可热加载）。state/config.json。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import features
from providers import DEFAULT_SOURCES

try:
    from fnlog import get_logger
except Exception:  # 独立运行 / 旧打包缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("config")

_STATE = Path(__file__).resolve().parent.parent / "state" / "config.json"
_LOCK = threading.RLock()

# 配置版本号：升级时按版本做迁移（见 Config.load）。
CONFIG_VERSION = 1
# 每次保存前保留的旧配置备份份数。
BACKUP_KEEP = 5


def _backup_dir() -> Path:
    return _STATE.parent / "backups"


def _backup_config() -> Path | None:
    """把当前 config.json 复制一份带时间戳的备份，只留最近 N 份。

    由 _persist 在成功写入后调用，备份即“最近一次完好的配置”。"""
    try:
        if not _STATE.is_file():
            return None
        d = _backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        import datetime

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = d / f"config-{ts}.json"
        # 同一秒内多次保存不覆盖已有备份。
        i = 1
        while dst.exists():
            dst = d / f"config-{ts}-{i}.json"
            i += 1
        dst.write_bytes(_STATE.read_bytes())
        for old in sorted(d.glob("config-*.json"))[: -BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return dst
    except Exception as e:
        _log.warning("config backup failed: %s: %s", type(e).__name__, e)
        return None


def _restore_from_backup() -> dict | None:
    """主配置损坏时按时间倒序尝试备份，返回第一个能解析的备份内容。"""
    try:
        backups = sorted(_backup_dir().glob("config-*.json"), reverse=True)
    except Exception:
        return None
    for b in backups:
        try:
            j = json.loads(b.read_text(encoding="utf-8"))
            if isinstance(j, dict):
                _log.error("config corrupted, restored from backup: %s", b.name)
                return j
        except Exception:
            continue
    return None

# 噪音关键词：标题或备注命中则不注入（电影解说、预告、花絮等）。
# P2：AI漫剧 / 漫剧 / 动态漫画已是正式分类（catalog.ai_comic），不再当噪音过滤。
JUNK_KEYWORDS = [
    "电影解说", "影视解说", "解说", "预告片", "预告", "花絮", "抢先", "枪版",
    "彩蛋", "删减", "片场", "开机", "特辑",
]

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "sources": [dict(s) for s in DEFAULT_SOURCES],
    # T4 远程源（type=4）：导入订阅时收录的展示清单 [{name, api, ext}]。
    # 本 fork 未接入 T4 协议，不能搜不能播，只显示；上游 v0.2.66 起才有 T4 选片库。
    "t4_sources": [],
    # 被删除的内置源 id（自定义源删除时直接从 sources 移除）
    "removed_ids": [],
    "max_per_keyword": 12,
    "filter_junk": True,
    "junk_keywords": JUNK_KEYWORDS,
    # 在线资源前排展示（在线片库更齐全，默认前排）
    "online_first": True,
    # 播放线路健康探测与自动换线：当前线路解析失败/文件不存在时，自动探测同集其他
    # 线路并切到首个可用线路；坏线短时记忆，起播自动跳过（关闭则保持纯手动选线）
    "line_autofail": True,
    # >>> PRIVATE:CATVOD
    # 网盘分类媒体库（完整版私有能力）：按网盘目录分类建立多张独立库卡片。
    "pan_lib_enabled": True,
    # <<< PRIVATE:CATVOD
    # 完全自动选最快线：起播时并行竞速探测前若干条候选线路，选首字节延迟最低的线；
    # 最快线短时记忆，同一集后续/重放稳定用它（手动在播放器切线仍被尊重）
    "line_fastest": True,
    # 自动选最快线时最多参与竞速的候选线路数（控制起播开销）
    "line_fastest_max": 6,
    # 竞速超时预算（秒）：超时未响应的线路本轮判慢，不阻塞起播
    "line_fastest_timeout": 4.0,
    # 默认线（高质量源）领先优势（秒）：它在该优势内返回就算赢，避免无谓抖动
    "line_fastest_prefer": 0.25,
    # 虚拟“热门榜单”四库媒体库（豆瓣真实周榜/标签榜）
    "hot_library_enabled": True,
    # 虚拟“海量分类”四库媒体库（苹果 CMS 分类深翻）。与热门榜单相互独立开关。
    "catalog_library_enabled": True,
    "t4_library_enabled": True,
    # 扩展（成人）资源库独立开关：开启后，已被源标记为「福利分类(adult)」的
    # 源才把 ADULT_CAT 纳入浏览。默认关闭：不建路由、不抓取、不显示。
    "welfare_library_enabled": False,
    # 是否把每个启用的 TVBox 加固源（csp_*Guard，如比特/糯米/花花）生成为一张
    # 独立“专区媒体库”卡片。默认关闭：加固源只作为播放时可切换的线路/搜索源，
    # 不在媒体库列表里产生空库。需要专区墙时可在管理页打开。
    "guard_zone_enabled": False,
    # 是否在飞牛原生媒体库注入单张「🛡加固影视」聚合库（源→分类片单，后台预取
    # 缓存、请求即时返回）。与 guard_zone_enabled（每源一张专区墙）相互独立。
    "guard_native_enabled": True,
    # 置顶的虚拟媒体库 guid 集合（打开=在飞牛影视媒体库列表置顶，关闭=普通顺序）。
    "pinned_libs": [],
    # 虚拟媒体库显隐/排序/子账号授权（上游 vlibs 设计，按稳定库键 key 配置，
    # 与 guid 解耦）。lib_hidden：管理员隐藏的库键；lib_order：虚拟库自定义
    # 顺序；lib_placement：front/back/after；lib_anchor：after 的锚点本地库
    # guid；lib_acl：{用户 guid: [允许的库键，含 "__online__" 标记]}；
    # acl_default_visible/acl_default_online：未配置账号的默认（True=可见，
    # 上游 fail-open 默认；改 False 则未配置账号默认看不到虚拟库/在线搜索）。
    "lib_hidden": [],
    "lib_order": [],
    "lib_placement": "front",
    "lib_anchor": "",
    "lib_acl": {},
    "acl_default_visible": True,
    "acl_default_online": True,
    "hot_library_title": "🌐在线热门",
    "hot_limit": 300,          # 四库合计保留条数上限
    "hot_per_source": 120,    # 每个资源站最多抓取的可播条目
    "hot_pages_per_source": 6,  # 每个资源站翻“最近更新”的页数（每页约20条）
    "hot_refresh_interval": 6 * 3600,
    # 源质量优先级：source_id -> 权重（越高越优先）。未列出的源取内省默认（见
    # providers.source_priority）。热门榜单点击搜源时按 (优先级, 直链数) 排序。
    "source_priority": {},
    # 热门资源解析是否优先高质量源（关闭则只按可播直链数）
    "prefer_quality_source": True,
    "admin_token": "",
    # 仅用于「拉取订阅资源」的可选 HTTP(S) 代理（订阅配置 / spider jar / DRPY 脚本）。
    # 播放、聚合搜索、CMS 探测等一律不走此代理（产品客户端保持 trust_env=False 直连）。
    # 形如 http://user:pass@host:port ；留空表示直连。
    "subscription_proxy": "",
    # 仅用于「访问资源站取源」（聚合搜索 / 分类深翻 / 测速探测 / CMS 详情）的可选
    # HTTP(S) 代理。部分运营商直连资源站超时/被墙，挂代理可显著改善搜索结果与速度
    # （issue #30）。播放、m3u8/分片等媒体流默认仍直连，不增加代理带宽压力。
    # 形如 http://user:pass@host:port ；留空=直连。
    "source_proxy": "",
    # 资源站代理模式："off"=一律直连（忽略已配置代理）；"on"=所有取源都走代理；
    # "auto"=默认直连，仅当某个资源站直连发生连接级失败（重置/超时/DNS等）时，
    # 自动对该站切换到已配置代理重试，其余站点保持直连（推荐）。
    "source_proxy_mode": "auto",
    # 检查/下载插件更新安装包时可选的 HTTP(S) 代理（仅更新用途）。
    # 留空则回退 subscription_proxy，再不行直连。仅存于本机 state，不随包分发。
    "update_proxy": "",
    # m3u8 流层去广告总开关（TVBox/OK影视 Pro rules 引擎 + 通用启发式）。
    "ad_filter_enabled": True,
    # 播放时动态检测（无订阅 rules 时，按 DISCONTINUITY 分段+时长/CDN/位置识别广告段）。
    "ad_filter_dynamic": True,
    # 分片编码参数(SPS)指纹去广告：识别"重新编码拼接、无任何结构特征"的广告。
    # 只读分片头 4~32KB，按块时长加权投票 + 多重守卫，判定不确定时绝不删。
    "ad_filter_sps": True,
    # 红果短剧（HGDJ）伪媒体库：由本机 HGDJ 服务提供元数据/播放流，
    # 插件仅充当客户端，本机服务缺省监听 127.0.0.1:18098。
    "hgdj_enabled": False,
    "hgdj_url": "http://127.0.0.1:18098",
    # ---- P2 开关 ----
    # 追剧提醒：开启后，收藏的剧集数变多时在管理页 / _ext/follow 提醒。
    "follow_enabled": True,
    # 源健康自动降级：开启后，连续失败 3 次的源在 15 分钟冷却窗内被搜索跳过。
    "source_health_enabled": True,
    # 域名自愈：开启后，资源站主域名失效时按备用域名列表自动试下一个。
    # 每个源的备用域名：source_id -> [域名/URL/JSON {"host": "..."}]，默认空。
    "domain_heal_enabled": True,
    "source_domain_backups": {},
    # 元数据补全：开启后，详情缺失的年份/地区/简介/海报用同名其他源的条目补齐。
    # 不接任何外部元数据接口；TMDB 需要用户自己填 key（留空=不用）。
    "metadata_enabled": True,
    # 关联作品：开启后，详情页下方挂 recommendations 关联作品（同系列/同分类优先）。
    "related_enabled": True,
    "tmdb_api_key": "",
}

# 已废弃、需从旧配置里清除的键
_DEPRECATED = {"public_base", "strm_root", "library_visible_users"}
_BUILTIN_IDS = {s["id"] for s in DEFAULT_SOURCES}


class Config:
    def __init__(self):
        self._cfg: dict = {}
        self._mtime = 0
        self.load()

    # >>> PRIVATE:GUARD
    def _sync_guard_defaults(self):
        """把 guard 模块的实时源（内置+用户导入）同步进 DEFAULT_SOURCES 底表。

        导入 TVBox 配置后调用，使新源无需重启即进入源列表/聚合；旧的导入源若
        已删除则从底表移除（用户配置中的 enabled 等仍按 saved_by_id 合并）。
        """
        if features.GUARD:
            try:
                import providers as _prov
                live = {d["id"]: d for d in _prov.guard_source_defs()}
                # 普通 csp_ 爬虫源（订阅导入，spider- 前缀）
                try:
                    import spiders as _sp
                    live.update({d["id"]: d for d in _sp.source_defs()})
                except Exception:
                    _sp = None
                # 移除已不存在的 guard/普通爬虫底表项（保留其它与内置项）
                for s in list(DEFAULT_SOURCES):
                    if (s.get("guard") or s.get("spider_src")) and \
                            s["id"] not in live and not s.get("builtin_locked"):
                        DEFAULT_SOURCES.remove(s)
                for d in live.values():
                    ex = next((s for s in DEFAULT_SOURCES if s["id"] == d["id"]), None)
                    if ex is None:
                        DEFAULT_SOURCES.append(dict(d))
                    elif d.get("guard"):
                        # 名称/guard_key 跟随导入定义更新
                        ex["name"] = d["name"]
                        ex["guard_key"] = d["guard_key"]
                    elif d.get("spider_src"):
                        ex["name"] = d["name"]
                        ex["spider_key"] = d.get("spider_key", "")
            except Exception:
                pass
        # <<< PRIVATE:GUARD

    def load(self):
        with _LOCK:
            # >>> PRIVATE:GUARD
            self._sync_guard_defaults()
            # <<< PRIVATE:GUARD
            self._restored_from_backup = False
            try:
                j = json.loads(_STATE.read_text(encoding="utf-8"))
                if not isinstance(j, dict):
                    raise ValueError("config root is not an object")
            except FileNotFoundError:
                j = {}
            except Exception as e:
                # 主配置损坏：先试备份，备份也坏才退回默认值，绝不静默丢配置。
                _log.error("config corrupted (%s: %s), trying backups",
                           type(e).__name__, e)
                j = _restore_from_backup() or {}
                self._restored_from_backup = bool(j)
                if not j:
                    _log.error("no usable config backup, falling back to defaults")
            cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
            cfg.update(j)
            # 旧配置没有版本号时补 1，后续升级按版本号迁移。
            cfg.setdefault("config_version", CONFIG_VERSION)
            for k in list(cfg.keys()):
                if k in _DEPRECATED:
                    cfg.pop(k, None)
            removed = set(cfg.get("removed_ids", []))

            # 源列表：以内置为底（跳过被删除的），保留用户的启用/改名/改地址，
            # 再追加用户自定义源。
            saved = j.get("sources") if isinstance(j.get("sources"), list) else []
            saved_by_id = {s.get("id"): s for s in saved if isinstance(s, dict)}
            merged = []
            for s in DEFAULT_SOURCES:
                if s["id"] in removed:
                    continue
                cur = saved_by_id.get(s["id"], {})
                m = dict(s)
                m.update({k: cur[k] for k in ("enabled", "name", "api") if k in cur})
                m["builtin"] = True
                m["standalone"] = bool(cur.get("standalone", False))
                m["loose_filter"] = bool(cur.get("loose_filter", False))
                # >>> PRIVATE:ADULT
                m["adult"] = bool(cur.get("adult", False)) if features.ADULT else False
                # <<< PRIVATE:ADULT
                # >>> PRIVATE:DDUO
                m["xsrc"] = bool(s.get("xsrc", False)) if features.DDUO else False
                # <<< PRIVATE:DDUO
                # >>> PRIVATE:CATVOD
                m["catvod"] = bool(s.get("catvod", False)) if features.CATVOD else False
                if m["catvod"]:
                    m["xsrc"] = True
                # <<< PRIVATE:CATVOD
                # >>> PRIVATE:DRPY
                m["drpy"] = bool(s.get("drpy", False)) if features.DRPY else False
                if m["drpy"]:
                    m["xsrc"] = True
                # <<< PRIVATE:DRPY
                # >>> PRIVATE:GUARD
                m["guard"] = bool(s.get("guard", False)) if features.GUARD else False
                m["guard_key"] = s.get("guard_key", "") if features.GUARD else ""
                m["spider_src"] = bool(s.get("spider_src", False)) if features.GUARD else False
                m["spider_key"] = s.get("spider_key", "") if features.GUARD else ""
                if m["guard"] or m["spider_src"]:
                    m["xsrc"] = True
                # <<< PRIVATE:GUARD
                merged.append(m)
            for sid, s in saved_by_id.items():
                if sid in _BUILTIN_IDS or sid in removed:
                    continue
                m = {k: s.get(k) for k in ("id", "name", "api", "enabled")}
                m["enabled"] = bool(s.get("enabled", True))
                m["standalone"] = bool(s.get("standalone", False))
                m["loose_filter"] = bool(s.get("loose_filter", False))
                # >>> PRIVATE:ADULT
                m["adult"] = bool(s.get("adult", False)) if features.ADULT else False
                # <<< PRIVATE:ADULT
                # >>> PRIVATE:DDUO
                m["xsrc"] = bool(s.get("xsrc", False)) if features.DDUO else False
                # <<< PRIVATE:DDUO
                # >>> PRIVATE:DRPY
                m["drpy"] = bool(s.get("drpy", False)) if features.DRPY else False
                # <<< PRIVATE:DRPY
                # >>> PRIVATE:GUARD
                m["guard"] = bool(s.get("guard", False)) if features.GUARD else False
                m["guard_key"] = s.get("guard_key", "") if features.GUARD else ""
                # <<< PRIVATE:GUARD
                m["builtin"] = bool(s.get("builtin", False))
                for key in ("kind", "site_key", "ext", "searchable", "filterable", "indexs", "method"):
                    if key in s:
                        m[key] = s[key]
                merged.append(m)
            cfg["sources"] = merged
            cfg["removed_ids"] = sorted(removed)
            # 旧版本（单库/小数量）保存过的热门参数自动升级到深度爬取规模；
            # 仅在检测到旧小值时迁移一次，用户之后自定义的大值不会被覆盖。
            migrated = False
            try:
                if int(cfg.get("hot_limit", 0)) <= 120 and int(DEFAULTS["hot_limit"]) > 120:
                    cfg["hot_limit"] = DEFAULTS["hot_limit"]; migrated = True
                if int(cfg.get("hot_per_source", 0)) <= 40:
                    cfg["hot_per_source"] = DEFAULTS["hot_per_source"]; migrated = True
                if not cfg.get("hot_pages_per_source"):
                    cfg["hot_pages_per_source"] = DEFAULTS["hot_pages_per_source"]; migrated = True
            except (TypeError, ValueError):
                pass
            if migrated:
                j.update({k: cfg[k] for k in
                          ("hot_limit", "hot_per_source", "hot_pages_per_source")})
                try:
                    # 走 _persist：先备份、再写临时文件 + 原子 rename。
                    # cfg 已含合并后的 sources 与迁移后的 hot_* 值，直接持久化它。
                    self._persist(cfg)
                except Exception as e:
                    _log.warning("config migration persist failed: %s: %s",
                                 type(e).__name__, e)
            if not cfg.get("admin_token"):
                import secrets
                cfg["admin_token"] = secrets.token_hex(8)
            self._cfg = cfg
            try:
                self._mtime = _STATE.stat().st_mtime
            except FileNotFoundError:
                self._mtime = 0

    def maybe_reload(self):
        try:
            mt = _STATE.stat().st_mtime
        except FileNotFoundError:
            return
        if mt != self._mtime:
            self.load()

    def get(self):
        with _LOCK:
            return json.loads(json.dumps(self._cfg))

    def enabled_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"] if s.get("enabled")]

    def all_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"]]

    def save(self, patch: dict):
        with _LOCK:
            cfg = self.get()
            for k, v in patch.items():
                if k in cfg:
                    cfg[k] = v
            self._persist(cfg)

    def _persist(self, cfg: dict):
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(_STATE)
        # 成功写入后再备份：备份 = 最近一次已知的完好配置，
        # 主配置日后被外部写坏时可直接恢复到它。
        _backup_config()
        self._cfg = cfg
        self._mtime = _STATE.stat().st_mtime

    @staticmethod
    def make_custom_id(api: str) -> str:
        return "custom_" + hashlib.md5(api.strip().encode()).hexdigest()[:10]

    def add_source(self, name: str, api: str, enabled: bool = True,
                   standalone: bool = False) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            api = (api or "").strip()
            name = (name or "").strip()
            if not api.startswith(("http://", "https://")):
                return False, "API 地址需以 http(s):// 开头"
            if not name:
                name = api.split("/")[2]
            sid = self.make_custom_id(api)
            norm = api.rstrip("/").lower()
            if any(s["id"] == sid or s.get("api", "").rstrip("/").lower() == norm
                   for s in cfg["sources"]):
                return False, "该资源站地址已存在"
            cfg["sources"].append(
                {"id": sid, "name": name, "api": api,
                 "enabled": bool(enabled), "standalone": bool(standalone),
                 "builtin": False})
            self._persist(cfg)
            return True, sid

    def source_priorities(self) -> dict:
        with _LOCK:
            return dict(self.get().get("source_priority") or {})

    def import_t4_sources(self, sources: list[dict]) -> dict:
        import t4
        with _LOCK:
            cfg = self.get()
            existing = {s["id"] for s in cfg["sources"]}
            added, duplicate = [], 0
            for source in sources:
                sid = t4.source_id(source["api"], source.get("ext"))
                if sid in existing:
                    duplicate += 1
                    continue
                cfg["sources"].append(dict(
                    source, id=sid, kind="t4", builtin=False, standalone=False))
                existing.add(sid)
                added.append(source["name"])
            if added:
                self._persist(cfg)
            return {"candidates": len(sources), "added": len(added),
                    "added_names": added, "duplicate": duplicate}

    def set_source_priority(self, mapping: dict) -> tuple[bool, str]:
        """整体保存源优先级（id -> 整数权重），忽略未知源与非法值。"""
        with _LOCK:
            cfg = self.get()
            valid = {s["id"] for s in cfg["sources"]}
            out = {}
            for k, v in (mapping or {}).items():
                if k in valid:
                    try:
                        out[k] = max(-100, min(100, int(v)))
                    except (TypeError, ValueError):
                        continue
            cfg["source_priority"] = out
            self._persist(cfg)
            return True, "ok"

    def set_subscription_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「订阅资源抓取」专用代理（空串=直连）。仅用于订阅配置/jar/DRPY脚本，
        不影响播放与搜索。仅接受 http(s)/socks5 URL 形态。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["subscription_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_source_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「资源站取源」专用代理（空串=直连），issue #30。
        仅用于搜索/分类/探测/详情；不影响订阅抓取与媒体播放。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["source_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_source_proxy_mode(self, mode: str) -> tuple[bool, str]:
        """设置资源站代理模式：off/on/auto。"""
        m = str(mode or "auto").strip().lower()
        if m not in ("off", "on", "auto"):
            return False, "模式须为 off/on/auto"
        with _LOCK:
            cfg = self.get()
            cfg["source_proxy_mode"] = m
            self._persist(cfg)
            return True, "ok"

    def source_proxy_mode(self) -> str:
        try:
            m = str(self.get().get("source_proxy_mode") or "auto").strip().lower()
            return m if m in ("off", "on", "auto") else "auto"
        except Exception:
            return "auto"

    def set_standalone(self, sid: str, on: bool) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if target.get("kind") == "t4" and on:
                return False, "T4 来源统一显示在「T4 选片」，无需逐插件建立独立库"
            target["standalone"] = bool(on)
            self._persist(cfg)
            return True, "ok"

    def pinned_lib_guids(self) -> set:
        with _LOCK:
            try:
                return set(self.get().get("pinned_libs") or [])
            except TypeError:
                return set()

    def set_lib_pinned(self, guid: str, on: bool) -> tuple[bool, str]:
        """置顶/取消置顶某个虚拟媒体库（guid）。"""
        if not isinstance(guid, str) or not guid:
            return False, "guid 非法"
        with _LOCK:
            cfg = self.get()
            cur = [g for g in (cfg.get("pinned_libs") or []) if isinstance(g, str)]
            if on:
                if guid not in cur:
                    cur.append(guid)
            else:
                cur = [g for g in cur if g != guid]
            cfg["pinned_libs"] = cur
            self._persist(cfg)
            return True, "ok"

    def set_loose_filter(self, sid: str, on: bool) -> tuple[bool, str]:
        """放宽正片（标题/备注噪音）过滤；分类层面的红线不受影响。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["loose_filter"] = bool(on)
            self._persist(cfg)
            return True, "ok"

    # >>> PRIVATE:ADULT
    def set_adult(self, sid: str, on: bool) -> tuple[bool, str]:
        """允许该源独立库收录合规成年分类；硬红线（涉未成年/非自愿）不受影响。"""
        if not features.ADULT:
            return False, "当前构建不支持"
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["adult"] = bool(on)
            self._persist(cfg)
            return True, "ok"
    # <<< PRIVATE:ADULT

    def remove_source(self, sid: str) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if target.get("builtin"):
                # 内置源不物理删除，标记移除（可在重置时恢复）
                if sid not in cfg["removed_ids"]:
                    cfg["removed_ids"].append(sid)
            cfg["sources"] = [s for s in cfg["sources"] if s["id"] != sid]
            self._persist(cfg)
            return True, "ok"

    def restore_builtin(self) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            cfg["removed_ids"] = []
            self._persist(cfg)
            self.load()
            return True, "ok"

    @staticmethod
    def normalize_keywords(raw) -> list[str]:
        """把逗号/换行/空白分隔的输入清洗成去重关键词列表。"""
        if isinstance(raw, str):
            parts = re.split(r"[,，、\n\r;；\s]+", raw)
        elif isinstance(raw, (list, tuple)):
            parts = []
            for item in raw:
                parts.extend(re.split(r"[,，、\n\r;；\s]+", str(item)))
        else:
            return []
        out, seen = [], set()
        for p in parts:
            k = p.strip()
            if k and k not in seen and len(k) <= 30:
                seen.add(k)
                out.append(k)
        return out

    def set_junk_keywords(self, raw) -> list[str]:
        kws = self.normalize_keywords(raw)
        self.save({"junk_keywords": kws})
        return kws

    def reset_junk_keywords(self) -> list[str]:
        self.save({"junk_keywords": list(JUNK_KEYWORDS)})
        return list(JUNK_KEYWORDS)

    def is_junk(self, title: str, remarks: str = "") -> bool:
        if not self._cfg.get("filter_junk", True):
            return False
        kws = self._cfg.get("junk_keywords", JUNK_KEYWORDS)
        hay = f"{title} {remarks}"
        return any(k and k in hay for k in kws)


config = Config()
