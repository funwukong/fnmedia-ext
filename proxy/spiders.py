#!/usr/bin/env python3
"""普通 csp_ 爬虫源（非 Guard，纯 dex）的导入与播放桥接（完整版私有特性）。

TVBox 订阅里大量源是 type=3、api=csp_Xxx（不以 Guard 结尾），真实爬虫类在订阅
顶部声明的普通（未加密）spider jar 里，设备端用 DexClassLoader 加载即可，无需
解密。本模块负责：

  - 解析结果的持久化（spider_sites.json）：每个源记录 key/name/cls/ext/jarUrl；
  - spider jar 的本地缓存（assets/spiders/jars/<jarid>.jar）与按源下发到 redroid；
  - 经既有 spider-runner android 后端，按「每个源各自的 jar」调用 home/search/
    detail/play（设备 bridge 已支持每次请求指定 jar 并缓存 ClassLoader）。

jar 自动下载失败（源站 404/502/超时）时该源标记 jar_missing，可在管理页手动
补传同用途 jar，补传后立即恢复。Guard 加固源仍走 guard.py，两者互不影响。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import features
import httpx

_log = logging.getLogger("fnmedia")

_ASSET = Path(__file__).resolve().parent / "assets" / "spiders"
_JAR_DIR = _ASSET / "jars"
_SITES_FILE = _ASSET / "spider_sites.json"
MAX_JAR_BYTES = 80 * 1024 * 1024
HTTP_TIMEOUT = 25.0

_lock = threading.RLock()
_sites: dict[str, dict] | None = None
_jars: dict[str, dict] | None = None   # jarid -> {url,file,size,ok}


def feature_on() -> bool:
    return bool(getattr(features, "GUARD", False))  # 共用安卓运行时开关


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.debug("_read_json failed", exc_info=True)
        return default


def _jar_id(jar_url: str) -> str:
    return hashlib.sha1(jar_url.encode("utf-8")).hexdigest()[:16]


def _device_jar_path(jar_url: str) -> str:
    from guard import DEVICE_SPIDER_JAR_DIR
    return f"{DEVICE_SPIDER_JAR_DIR}/{_jar_id(jar_url)}.jar"


def _load_jars() -> dict[str, dict]:
    global _jars
    if _jars is None:
        data = _read_json(_ASSET / "jars.json", {})
        _jars = data if isinstance(data, dict) else {}
    return _jars


def _save_jars() -> None:
    _ASSET.mkdir(parents=True, exist_ok=True)
    tmp = (_ASSET / "jars.json").with_suffix(".tmp")
    tmp.write_text(json.dumps(_load_jars(), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_ASSET / "jars.json")


def load_sites() -> dict[str, dict]:
    """当前普通爬虫源（实时读文件），键为源 key（如 XBPQ2）。"""
    global _sites
    if _sites is None:
        data = _read_json(_SITES_FILE, {})
        sites = data.get("sites", []) if isinstance(data, dict) else []
        _sites = {s["key"]: s for s in sites if isinstance(s, dict) and s.get("key")}
    return _sites


def _persist_sites(sites: dict[str, dict]) -> None:
    _ASSET.mkdir(parents=True, exist_ok=True)
    tmp = _SITES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sites": list(sites.values())}, ensure_ascii=False,
                              indent=1), encoding="utf-8")
    tmp.replace(_SITES_FILE)


def reload_sites() -> dict[str, dict]:
    global _sites
    _sites = None
    return load_sites()


def site(key: str) -> dict | None:
    return load_sites().get(key)


def site_keys() -> list[str]:
    return list(load_sites().keys())


def download_jar(jar_url: str) -> dict:
    """下载并缓存一个 spider jar；返回 {ok,size,error,local,via?}。

    经用户配置的「订阅资源代理」（subfetch），GitHub 资源在声明地址失败
    （NXDOMAIN/被墙）时自动顺序尝试国内镜像；任意候选取到合法 zip(jar) 即成功，
    按原 URL 的 jar_id 落盘，故已登记的同源站点无需改引用即可就绪。
    """
    jar_url = (jar_url or "").strip()
    if not jar_url.startswith(("http://", "https://")):
        return {"ok": False, "error": "jar 地址非 http(s)"}
    jid = _jar_id(jar_url)
    jars = _load_jars()
    local = _JAR_DIR / f"{jid}.jar"
    if local.is_file() and local.stat().st_size > 0:
        jars[jid] = {**jars.get(jid, {}), "url": jar_url,
                     "file": local.name, "size": local.stat().st_size, "ok": True}
        return {"ok": True, "size": local.stat().st_size, "local": True}
    import subfetch
    candidates = subfetch.github_mirror_candidates(jar_url)
    # 若配置了订阅代理，原地址与 canonical github 经代理很可能直连成功；
    # fetch_first 内部所有候选都走该代理。
    data, used_url = subfetch.fetch_first(candidates, want_zip=True)
    if data is None:
        jars[jid] = {**jars.get(jid, {}), "url": jar_url,
                     "ok": False, "error": str(used_url)[:160]}
        return {"ok": False, "error": str(used_url)[:160]}
    if len(data) > MAX_JAR_BYTES:
        return {"ok": False, "error": "jar 过大"}
    _JAR_DIR.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(".jar.dl")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    tmp.replace(local)
    via_mirror = str(used_url).rstrip("/") != jar_url.rstrip("/")
    jars[jid] = {"url": jar_url, "file": local.name,
                 "size": len(data), "ok": True,
                 **({"via": used_url} if via_mirror else {})}
    _save_jars()
    return {"ok": True, "size": len(data), "local": False,
            **({"via": used_url} if via_mirror else {})}


def upload_jar(jar_url: str, data: bytes) -> dict:
    """用户为某个 jar URL 手动补传 jar（源站下载失败时）。"""
    if not data:
        return {"ok": False, "error": "文件为空"}
    if data[:2] != b"PK":
        return {"ok": False, "error": "不是有效 jar(zip)"}
    if len(data) > MAX_JAR_BYTES:
        return {"ok": False, "error": "jar 过大"}
    jid = _jar_id(jar_url)
    _JAR_DIR.mkdir(parents=True, exist_ok=True)
    local = _JAR_DIR / f"{jid}.jar"
    tmp = local.with_suffix(".jar.ul")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    tmp.replace(local)
    jars = _load_jars()
    jars[jid] = {"url": jar_url, "file": local.name, "size": len(data),
                 "ok": True, "manual": True}
    _save_jars()
    # 联动：启用所有引用此 jar、此前因缺 jar 而关闭的源（风险源仍保持关闭）。
    sites = load_sites()
    changed = False
    for k, s in sites.items():
        if s.get("jar_url") == jar_url and not s.get("enabled") and not s.get("risk"):
            s["enabled"] = True
            changed = True
    if changed:
        _persist_sites(sites)
    return {"ok": True, "size": len(data), "auto_enabled":
            [k for k, s in sites.items()
             if s.get("jar_url") == jar_url and s.get("enabled")] if changed else []}


def jar_local_path(jar_url: str) -> Path | None:
    local = _JAR_DIR / f"{_jar_id(jar_url)}.jar"
    return local if local.is_file() and local.stat().st_size > 0 else None


def jar_missing_keys() -> set[str]:
    """所有引用了未就绪 jar 的源 key。"""
    out = set()
    for k, s in load_sites().items():
        if not jar_local_path(s.get("jar_url", "")):
            out.add(k)
    return out


def _ensure_device_jar(jar_url: str) -> bool:
    """确保该 jar 已下发到运行中的 redroid（已存在且大小一致则跳过）。"""
    local = jar_local_path(jar_url)
    if not local:
        return False
    try:
        import guard as _g
        dev = _device_jar_path(jar_url)
        if _g.device_jar_present(dev, local.stat().st_size):
            return True
        return _g.push_device_jar(local.read_bytes(), dev)
    except Exception as exc:  # noqa: BLE001
        _log.warning("spider jar 下发失败 %s: %s", jar_url, exc)
        return False


def _class_short(cls: str) -> str:
    """从全限定类名取短名（如 com.github.catvod.spider.XBPQ -> XBPQ）。"""
    c = _norm_class(cls or "")
    prefix = "com.github.catvod.spider."
    return c[len(prefix):] if c.startswith(prefix) else ""


def _norm_class(cls: str, key: str = "", base_key: str | None = None) -> str:
    """规范化爬虫 Java 类名。

    TVBox 里站点 spider 标识写作 csp_Xxx，但纯 dex jar 中真实类是
    com.github.catvod.spider.Xxx（csp_ 只是“来自自定义 jar”的标记，不是类名）。
    这里修正历史误存为 ...spider.csp_Xxx 的数据；非 catvod 全限定名保持原样。

    数字后缀：绝不直接对 key 做 rstrip('0123456789')——类名本身可能以数字
    结尾（如 Spider2），会被截断。计数后缀只认调用方显式给的 base_key
    （import_sites 从分类器给出的 cls 短名里取出并存进源记录）；没有
    base_key 时 key 原样使用，不猜、不剥离。
    """
    c = (cls or "").strip()
    if c.startswith("com.github.catvod.spider.csp_"):
        c = "com.github.catvod.spider." + c[len("com.github.catvod.spider.csp_"):]
    if not c:
        base = (base_key or "").strip() or key
        if base:
            c = f"com.github.catvod.spider.{base}"
    return c


def import_sites(items: list[dict], enable: str = "smart") -> dict:
    """合并写入普通爬虫源。items 为分类器产出的 csp 项。

    enable: "smart" 安全源自动启用、风险/缺 jar 默认关；True/False 强制。
    幂等：按 key 合并，保留既有启用态。返回统计。
    """
    sites = dict(load_sites())
    added = updated = enabled_n = 0
    risk_re = re.compile(r"(P2P|磁力|磁鏈|云盘|雲盤|网盘|網盤|夸克|阿里|115|百度|"
                         r"迅雷|我的|私人|收藏)", re.I)
    out_items = []
    for it in items:
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        jar_url = (it.get("jar") or "").strip()
        name = str(it.get("name") or key).strip() or key
        risky = bool(risk_re.search(name))
        # base_key：分类器追加计数后缀前的原始类短名。优先用调用方显式给的
        # base_key，其次从分类器给出的 cls 全限定名取短名；两者都没有时
        # 不猜（key 原样用，_norm_class 不再 rstrip 数字）。
        base_key = str(it.get("base_key") or "").strip() \
            or _class_short(it.get("cls") or "")
        rec = {
            "key": key,
            "base_key": base_key or key,
            "spider": it.get("spider") or f"csp_{base_key or key}",
            "name": name,
            "class": _norm_class(it.get("cls") or "", key,
                                 base_key=base_key or None),
            "ext": it.get("ext") or "",
            "jar_url": jar_url,
            "searchable": bool(it.get("searchable", 1)),
            "risk": risky,
        }
        exists = key in sites
        if exists:
            old = sites[key]
            rec["enabled"] = bool(old.get("enabled", False))
            sites[key].update(rec)
            updated += 1
        else:
            jar_ready = jar_local_path(jar_url) is not None
            if enable == "smart":
                # 安全源且 jar 已就绪才自动启用；jar 下载失败则登记但默认关，
                # 等源站恢复重导或在管理页手动补传 jar 后再开。
                rec["enabled"] = (not risky) and bool(jar_url) and jar_ready
            else:
                rec["enabled"] = bool(enable)
            sites[key] = rec
            added += 1
        if sites[key].get("enabled"):
            enabled_n += 1
        out_items.append({"key": key, "name": name, "enabled": sites[key]["enabled"],
                          "risk": risky, "has_jar": jar_local_path(jar_url) is not None,
                          "jar_url": jar_url})
    _persist_sites(sites)
    reload_sites()
    return {"added": added, "updated": updated, "enabled": enabled_n,
            "items": out_items}


def set_enabled(key: str, on: bool) -> bool:
    sites = load_sites()
    if key not in sites:
        return False
    sites[key]["enabled"] = bool(on)
    _persist_sites(sites)
    return True


def remove(key: str) -> bool:
    reload_sites()  # 读最新文件，避免长驻进程内存表过期
    sites = load_sites()
    if key not in sites:
        return False
    sites.pop(key)
    _persist_sites(sites)
    reload_sites()
    return True


def source_defs() -> list[dict]:
    """供 providers 合并的源字典（id=spider-<key>）。"""
    out = []
    for key, s in load_sites().items():
        out.append({
            "id": "spider-" + key,
            "name": s.get("name") or key,
            "api": "csprun://" + key,
            "enabled": bool(s.get("enabled")),
            "xsrc": True,
            "spider_src": True,
            "spider_key": key,
            "builtin": False,
        })
    return out


# ---------------- 运行时调用（redroid android，按源各自 jar） ----------------

def available() -> bool:
    if not feature_on():
        return False
    try:
        import guard as _g
        return bool(_g.available())
    except Exception:
        _log.debug("available failed", exc_info=True)
        return False


def _runtime_up(force: bool = True) -> bool:
    """android 运行时是否真就绪。强制刷新，避免 health 10s 缓存误判崩溃后仍 True。"""
    try:
        import guard as _g
        return bool(_g.health(force=force).get("ok"))
    except Exception:  # noqa: BLE001
        _log.debug("_runtime_up failed", exc_info=True)
        return available()


def _ensure_runtime(timeout_s: float = 25.0) -> bool:
    """运行时不就绪时主动拉起 bridge App 并等待自愈（WebView 嗅探崩溃后恢复）。"""
    import time as _t
    if _runtime_up():
        return True
    try:
        import guard as _g
        # 容器在跑但 bridge 进程没了：am start 重新拉起
        if _g._container_running():
            try:
                _g._docker_exec(["am", "start", "-n", _g._BRIDGE_COMPONENT], timeout=10)
            except Exception:  # noqa: BLE001
                _log.debug("_ensure_runtime failed", exc_info=True)
                pass
        else:
            _g.start_android(wait_ready=True)
    except Exception:  # noqa: BLE001
        _log.debug("_ensure_runtime failed", exc_info=True)
        pass
    t0 = _t.time()
    while _t.time() - t0 < timeout_s:
        if _runtime_up():
            return True
        _t.sleep(1.0)
    return _runtime_up()


def _call(site_conf: dict, method: str, **params):
    import guard as _g
    jar_url = site_conf.get("jar_url", "")
    if not _ensure_device_jar(jar_url):
        raise RuntimeError("spider jar 未就绪（需下载/补传并已连接 redroid）")
    payload = {
        "runtime": "android",
        "jar": _device_jar_path(jar_url),
        "class": _norm_class(site_conf.get("class", ""), site_conf.get("key", ""),
                             base_key=site_conf.get("base_key") or None),
        "ext": site_conf.get("ext") or "",
        "method": method,
        **params,
    }
    return _g._runner_post("/invoke", payload)


def search(site_conf: dict, keyword: str, quick: bool = False,
           pg: int | str = 1) -> list[dict]:
    res = _call(site_conf, "search", key=keyword, quick=bool(quick), pg=str(pg))
    if isinstance(res, dict):
        return res.get("list") or []
    return res if isinstance(res, list) else []


def detail(site_conf: dict, vod_id: str) -> dict:
    res = _call(site_conf, "detail", ids=[str(vod_id)])
    if isinstance(res, dict):
        items = res.get("list") or []
        if items:
            return items[0]
    return {}


def play(site_conf: dict, flag: str, vod_id: str):
    return _call(site_conf, "play", flag=flag or "", id=str(vod_id), vipFlags=[])


def home(site_conf: dict) -> dict:
    res = _call(site_conf, "home", filter=True)
    return res if isinstance(res, dict) else {}


def category(site_conf: dict, tid, pg=1, extend=None):
    res = _call(site_conf, "category", tid=str(tid), pg=str(pg),
                filter=False, extend=extend or {})
    return res if isinstance(res, dict) else {}


def _short(s: str, n: int = 90) -> str:
    return str(s or "").replace("\n", " ")[:n]


_HOT_WORDS = ("庆余年", "斗破苍穹", "流浪地球", "斗罗大陆", "繁花", "狂飙")


def diagnose_empty(key: str, keyword: str) -> dict:
    """搜索为空时判断源本身死活/有无资源，而不是把原因都推给关键词。

    路径 A（首页可浏览）：home→分类→站内真实片名回搜，区分
        keyword_miss / search_broken / dead_empty。
    路径 B（首页无内容，可能是纯搜索型/网盘聚合源）：用多个热门词试搜，
        有命中=源活着只是该词无片(keyword_miss)；全 0=源无可用内容(dead_empty)。
    调用抛错按错误类型归为 dead_runtime（网络层）或 spider_error（源代码 bug）。
    """
    conf = site(key)
    if not conf:
        return {"verdict": "unknown", "reason": "未知源"}

    def net_fail(m: str) -> bool:
        low = m.lower()
        return any(x in low for x in ("unknownhost", "resolve", "nxdomain",
                                     "timed out", "timeout", "connect",
                                     "closed connection", "ssl", "502", "503", "404"))

    # 1) home（失败不立刻判死，很多纯搜索源 home 就不可用，转路径 B）
    classes = []
    home_err = ""
    try:
        h = home(conf)
        classes = h.get("class") or []
        if isinstance(classes, dict):
            classes = list(classes.values())
    except Exception as exc:  # noqa: BLE001
        _log.debug("diagnose_empty failed", exc_info=True)
        home_err = str(exc)

    def tid_of(c):
        return str(c.get("type_id") or c.get("tid") or c.get("id") or "")

    def name_of(c):
        return str(c.get("type_name") or c.get("name") or "")

    sample = browse_cat = ""
    if classes:
        prefer = [c for c in classes if any(x in name_of(c) for x in
                  ("电影", "剧集", "电视", "综艺", "动漫", "国产", "动作", "连续剧"))]
        for c in (prefer + list(classes))[:4]:
            tid = tid_of(c)
            if not tid:
                continue
            try:
                pg = category(conf, tid, 1)
                for v in pg.get("list") or []:
                    t = str(v.get("vod_name") or "").strip()
                    if t:
                        sample, browse_cat = t, (name_of(c) or tid)
                        break
            except Exception:  # noqa: BLE001
                _log.debug("diagnose_empty failed", exc_info=True)
                continue
            if sample:
                break

    # 2) 路径 A：能浏览到影片 → 用站内片名回搜
    if sample:
        for cut in (0, 6, 4, 3):
            probe_kw = sample if not cut else (sample[:cut] if len(sample) > cut else sample)
            try:
                hits = search(conf, probe_kw)
            except Exception:  # noqa: BLE001
                _log.debug("diagnose_empty failed", exc_info=True)
                hits = []
            if hits:
                return {"verdict": "keyword_miss",
                        "reason": "源正常、有资源，仅未搜到「%s」（该词在此站无片）"
                                  % keyword.strip(),
                        "evidence": f"「{browse_cat}」首片《{sample}》，回搜「{probe_kw}」命中 {len(hits)} 条"}
        return {"verdict": "search_broken",
                "reason": "能浏览到影片（源有资源），但连站内片名都搜不到，搜索接口异常/被反爬",
                "evidence": f"「{browse_cat}」首片《{sample}》，回搜仍 0 条"}

    # 3) 路径 B：首页取不到内容——纯搜索型源用热门词试搜（排除用户原词）
    hot_err = ""
    for w in _HOT_WORDS:
        if w == (keyword or "").strip():
            continue
        try:
            hits = search(conf, w)
        except Exception as exc:  # noqa: BLE001
            _log.debug("diagnose_empty failed", exc_info=True)
            hot_err = str(exc)
            continue
        if hits:
            return {"verdict": "keyword_miss",
                    "reason": "源可搜索（无首页分类，属纯搜索/聚合源），仅「%s」无结果"
                              % keyword.strip(),
                    "evidence": f"热门词「{w}」命中 {len(hits)} 条，如《{(hits[0].get('vod_name') or '')[:20]}》"}

    # 4) 首页与热门词都失败：按错误性质定性
    if home_err and net_fail(home_err):
        if _is_util_source(key):
            return {"verdict": "needs_config",
                    "reason": "网盘/工具类源，需云盘授权或配置，无法离线判断死活",
                    "evidence": _short(home_err)}
        return {"verdict": "dead_runtime",
                "reason": "站点访问失败（域名解析/超时/拒绝），源疑似失效",
                "evidence": _short(home_err)}
    if hot_err and not net_fail(hot_err):
        if _is_util_source(key):
            return {"verdict": "needs_config",
                    "reason": "网盘/工具类源，需云盘授权或配置，无法离线判断死活",
                    "evidence": _short(hot_err)}
        return {"verdict": "spider_error",
                "reason": "爬虫执行报错（该源 jar 代码异常，非网络问题）",
                "evidence": _short(hot_err)}
    if _is_util_source(key):
        return {"verdict": "needs_config",
                "reason": "网盘/工具类源（无首页、需云盘授权），未纳入失效判定，请按需保留",
                "evidence": "离线热门词不适用此类源"}
    if classes:
        return {"verdict": "dead_empty",
                "reason": "首页有分类但取不到任何影片，疑似空源/反爬/接口失效",
                "evidence": "分类数 " + str(len(classes))}
    return {"verdict": "dead_empty",
            "reason": "无首页内容且多个热门词均 0 结果——该源无可用资源或已失效",
            "evidence": ("home 报错：" + _short(home_err, 60)) if home_err else
                        "home 与分类均为空，热门词搜索全 0"}


def probe(key: str, keyword: str = "庆余年", do_play: bool = False) -> dict:
    """单源端到端体检：搜索→详情→（可选）首集取播放地址。

    返回各阶段 ok/耗时/样例，错误归类为文本，供管理页一键判断该源是否可用。
    不抛异常（运行时不可用/站点死/WebView 嗅探等都转为结果字段）。
    """
    import time as _t
    out: dict = {"key": key, "name": key, "runtime": available()}
    conf = site(key)
    if not conf:
        return {**out, "ok": False, "stage": "source", "error": "未知爬虫源"}
    out["name"] = conf.get("name") or key
    if not _ensure_runtime():
        return {**out, "ok": False, "stage": "runtime",
                "error": "Android 运行时未就绪（redroid/bridge 启动或恢复失败）"}
    if not jar_local_path(conf.get("jar_url", "")):
        return {**out, "ok": False, "stage": "jar", "error": "爬虫 jar 未就绪"}

    # 1) 搜索
    t = _t.time()
    try:
        rows = search(conf, keyword or "庆余年")
        out["search_ms"] = int((_t.time() - t) * 1000)
    except Exception as exc:  # noqa: BLE001
        # 运行时可能刚被上一个嗅探源打崩：拉起恢复后重试一次
        _log.debug("probe failed", exc_info=True)
        if ("后端不可用" in str(exc) or "closed connection" in str(exc).lower()) \
                and _ensure_runtime(timeout_s=30.0):
            t = _t.time()
            try:
                rows = search(conf, keyword or "庆余年")
                out["search_ms"] = int((_t.time() - t) * 1000)
            except Exception as exc2:  # noqa: BLE001
                _log.debug("probe failed", exc_info=True)
                return {**out, "ok": False, "stage": "search", "error": str(exc2)[:200]}
        else:
            return {**out, "ok": False, "stage": "search",
                    "search_ms": int((_t.time() - t) * 1000), "error": str(exc)[:200]}
    if not rows:
        diag = diagnose_empty(key, keyword or "庆余年")
        return {**out, "ok": False, "stage": "search",
                "search_ms": out["search_ms"],
                "verdict": diag.get("verdict"),
                "error": diag.get("reason", "搜索无结果"),
                "evidence": diag.get("evidence", "")}
    r0 = rows[0]
    out["sample"] = str(r0.get("vod_name") or "")[:30]
    vid = r0.get("vod_id")
    if not vid:
        return {**out, "ok": False, "stage": "search", "error": "结果缺少 vod_id"}

    # 2) 详情 + 集数解析
    t = _t.time()
    try:
        det = detail(conf, vid)
        out["detail_ms"] = int((_t.time() - t) * 1000)
    except Exception as exc:  # noqa: BLE001
        _log.debug("probe failed", exc_info=True)
        return {**out, "ok": False, "stage": "detail", "error": str(exc)[:200]}
    try:
        import guard as _g
        eps = _g.parse_episodes(det)
    except Exception:  # noqa: BLE001
        _log.debug("probe failed", exc_info=True)
        eps = []
    out["episodes"] = len(eps)
    if not eps:
        return {**out, "ok": True, "playable": False,
                "note": "搜索/详情正常，但未解析到可播剧集（可能仅网盘/嗅探）"}
    if not do_play:
        return {**out, "ok": True, "playable": None,
                "note": "搜索+详情正常，已跳过取播放地址（勾选试播可进一步验证）"}

    # 3) 首集取播放地址
    if not available() and not _ensure_runtime():
        return {**out, "ok": False, "stage": "runtime", "error": "试播前运行时未恢复"}
    ep = eps[0]
    t = _t.time()
    try:
        pr = play(conf, ep.get("line", ""), ep.get("id", ""))
        out["play_ms"] = int((_t.time() - t) * 1000)
        if isinstance(pr, str):
            try:
                import json as _json
                pr = _json.loads(pr)
            except Exception:  # noqa: BLE001
                _log.debug("probe failed", exc_info=True)
                pass
        url = ""
        if isinstance(pr, dict):
            url = str(pr.get("url") or pr.get("playUrl") or "")
        if url and url.startswith(("http://", "https://")):
            return {**out, "ok": True, "playable": True, "play_url": url[:160],
                    "first_ep": ep.get("title", "")}
        return {**out, "ok": True, "playable": False,
                "note": "未取得直链（该源可能需 WebView 嗅探/解析接口，headless 不支持）",
                "raw": str(pr)[:160]}
    except Exception as exc:  # noqa: BLE001
        _log.debug("probe failed", exc_info=True)
        msg = str(exc)[:200]
        out["play_ms"] = int((_t.time() - t) * 1000)
        crashed = "closed connection" in msg.lower() or not available()
        # 嗅探型源会打崩 bridge：主动拉起并等待恢复，避免后续源误报运行时未就绪
        if crashed:
            _ensure_runtime(timeout_s=30.0)
        return {**out, "ok": True, "playable": False,
                "note": ("取播放触发 WebView 嗅探，无界面环境不支持（已自动恢复运行时）"
                         if crashed else "取播放失败"),
                "error": msg}


def status() -> dict:
    sites = load_sites()
    missing = jar_missing_keys()
    return {
        "available": available(),
        "total": len(sites),
        "enabled": len([s for s in sites.values() if s.get("enabled")]),
        "jar_missing": sorted(missing),
        "sites": [{"key": k, "name": s.get("name"), "enabled": bool(s.get("enabled")),
                   "risk": bool(s.get("risk")),
                   "jar_ready": k not in missing,
                   "jar_url": s.get("jar_url", "")}
                  for k, s in sites.items()],
    }


# ---------------- 批量体检（后台任务，线程安全） ----------------
import threading as _threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

_scan_lock = _threading.Lock()
_scan: dict = {"running": False, "total": 0, "done": 0, "results": [],
               "started": 0.0, "finished": 0.0}
# 结论分组顺序与中文标签
VERDICT_LABEL = {
    "playable": "可搜索且可取播放",
    "usable": "可搜索（未试播）",
    "keyword_miss": "源正常·仅该词无片",
    "search_broken": "有内容但搜索异常",
    "needs_config": "网盘/工具类·需配置",
    "spider_error": "爬虫代码报错",
    "dead_runtime": "源失效/无法访问",
    "dead_empty": "空源/已失效",
    "jar": "jar 未就绪",
    "runtime": "运行时未就绪",
    "unknown": "未知",
}
DEAD_VERDICTS = {"dead_runtime", "dead_empty", "spider_error"}

# 网盘/云盘/本地/推送等工具型爬虫：无首页、需云盘授权，离线热门词搜不到属正常，
# 不能据此判失效（不默认勾选删除）。匹配 key/类名/中文名。
_UTIL_RE = __import__("re").compile(
    r"pan|quark|ali|uc|webdav|alist|push|ypan|yun|so\d*$|盘|夸克|阿里|推送|网盘|云盘|本地",
    __import__("re").IGNORECASE)


def _is_util_source(key: str, conf: dict | None = None) -> bool:
    conf = conf or site(key) or {}
    blob = " ".join(str(conf.get(x, "")) for x in ("key", "name", "class", "spider"))
    return bool(_UTIL_RE.search(blob or key))


def scan_status() -> dict:
    with _scan_lock:
        d = dict(_scan)
    return d


def _scan_one(key: str, name: str, keyword: str, do_play: bool) -> dict:
    try:
        r = probe(key, keyword=keyword, do_play=do_play)
    except Exception as exc:  # noqa: BLE001
        _log.debug("_scan_one failed: name=%s", name, exc_info=True)
        r = {"key": key, "name": name, "ok": False, "stage": "unknown",
             "verdict": "unknown", "error": str(exc)[:160]}
    # 归类为统一 verdict
    if r.get("ok") and r.get("playable") is True:
        v = "playable"
    elif r.get("ok") and r.get("playable") is False:
        # 搜索可用但取播失败（多为 WebView 嗅探），仍算“可搜索但不可取流”
        v = "usable"
    elif r.get("ok"):
        v = "usable"
    else:
        v = r.get("verdict") or r.get("stage") or "unknown"
    return {
        "key": key, "name": r.get("name") or name, "verdict": v,
        "ok": bool(r.get("ok")), "playable": r.get("playable"),
        "sample": r.get("sample") or "", "episodes": r.get("episodes"),
        "search_ms": r.get("search_ms"),
        "reason": r.get("error") or r.get("note") or "",
        "evidence": r.get("evidence") or "",
        "suggest_delete": v in DEAD_VERDICTS,
    }


def start_scan(keyword: str = "庆余年", do_play: bool = False,
               only_enabled: bool = True, max_workers: int = 2) -> dict:
    """启动全源批量体检。串行度低（默认2），避免嗅探崩溃互相干扰。"""
    global _scan
    with _scan_lock:
        if _scan.get("running"):
            return {"ok": False, "error": "已有体检在进行中"}
        sites = load_sites()
        items = [(k, s.get("name") or k) for k, s in sites.items()
                 if (not only_enabled or s.get("enabled"))]
        _scan = {"running": True, "total": len(items), "done": 0, "results": [],
                 "started": __import__("time").time(), "finished": 0.0,
                 "keyword": keyword, "do_play": do_play}

    def run():
        import time as _t
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(4, max_workers))) as ex:
                futs = {ex.submit(_scan_one, k, nm, keyword, do_play): k
                        for k, nm in items}
                for fut in __import__("concurrent.futures", fromlist=["as_completed"]).as_completed(futs):
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        _log.debug("run failed", exc_info=True)
                        res = {"key": futs[fut], "name": futs[fut], "verdict": "unknown",
                               "ok": False, "reason": str(exc)[:160],
                               "evidence": "", "suggest_delete": False}
                    with _scan_lock:
                        _scan["results"].append(res)
                        _scan["done"] += 1
        finally:
            with _scan_lock:
                _scan["running"] = False
                _scan["finished"] = _t.time()

    th = _threading.Thread(target=run, daemon=True)
    th.start()
    return {"ok": True, "total": len(items)}


def remove_many(keys: list[str]) -> dict:
    """批量删除源，返回 {removed:[...], missing:[...]}。"""
    keys = [str(k) for k in keys if k]
    reload_sites()  # 强制读最新文件，避免长驻进程的内存表过期导致漏删
    sites = load_sites()
    removed, missing = [], []
    changed = False
    for k in keys:
        if k in sites:
            sites.pop(k)
            removed.append(k)
            changed = True
        else:
            missing.append(k)
    if changed:
        _persist_sites(sites)
        reload_sites()
    return {"removed": removed, "missing": missing, "count": len(removed)}
