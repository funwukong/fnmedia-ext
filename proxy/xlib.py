#!/usr/bin/env python3
"""小雅（AList 网盘）分类媒体库索引（完整版私有特性 CATVOD）。

背景：用户接入的小雅资源本身按目录做好了影视分类，且大量为 4K / 原盘。
爬虫 jar 的聚合分类接口在该部署上会被拦（403），但 AList 标准接口
/api/fs/list、/api/fs/get 可用。本模块直接遍历 AList 目录，把“含视频文件的
目录”识别为一部影片，按顶层大类建立可分页的扁平索引；播放时用 /api/fs/get
取最终公网网盘 CDN 直链（raw_url），交由客户端直连（NAS 到该 CDN 不通，
服务端不代流）。

仅在完整版（features.CATVOD）存在；公开构建整文件删除。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

import httpx

import catvod

_log = logging.getLogger("fnmedia")

# 顶层大类 → (AList 目录, 飞牛媒体库 category, 展示名, 类型关键字)
# 只纳入影视大类；体育/教育/曲艺/有声书/音乐等非影视不建库。
CATEGORIES = [
    ("movie", "/电影", "Movie", "🎬小雅电影", "movie"),
    ("tv", "/电视剧", "TV", "📺小雅剧集", "tv"),
    ("variety", "/综艺", "Mix", "🎤小雅综艺", "variety"),
    ("anime", "/动漫", "Mix", "🌸小雅动漫", "anime"),
    ("doc", "/纪录片", "Mix", "🎞小雅纪录片", "doc"),
    ("short", "/电视剧/短剧", "Mix", "⚡小雅短剧", "short"),
]

VIDEO_EXT = (".mkv", ".mp4", ".ts", ".m2ts", ".iso", ".avi", ".mov",
             ".wmv", ".flv", ".m4v", ".mpg", ".mpeg", ".rmvb", ".rm")
# 明显的样片/广告/非正片
_JUNK_NAME = ("预告", "花絮", "sample", "预告片", "广告", "宣传片")
_QUALITY_TOKENS = ("4K", "2160P", "1080P", "REMUX", "BLURAY", "BLU-RAY",
                   "蓝光", "原盘", "杜比视界", "DOLBY VISION", "HDR", "WEB-DL",
                   "WEBDL", "10BIT", "X265", "X264", "DTS", "TRUEHD", "ATMOS")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 文件名里的集数：E01 / 第01集 / S01E02 / 01集
_EP_RE = re.compile(
    r"(?:第\s*0*(\d{1,4})\s*[集话回]|[Ss]\d{1,2}[Ee]0*(\d{1,4})|\bE?0*(\d{1,4})\b)", re.I)
_SEASON_DIR_RE = re.compile(r"^(?:Season\s*\d+|S\d{1,2}|第\s*[0-9一二三四五六七八九十]+\s*季)\b", re.I)

_TIMEOUT = 25.0
_CONCURRENCY = 2          # 遍历 AList 的并发（建库只读条目名，仍保持低频温和）
_PAGE = 200               # 列目录每页
_MAX_DEPTH = 4            # 大类(0)→子类(1)→合集(2)→影片(3)→季(4)，再深不再下钻
_BUILD_BATCH = 4          # 后台构建每轮并发展开的目录数
_BUILD_PACE = 0.35        # 每批目录之间的节流秒，避免高频 fs/list
_QUEUE_SAVE_EVERY = 5     # 每隔几轮持久化一次待展开队列
_FILE_CACHE_TTL = 300.0   # 播放时懒取的目录文件列表缓存
_XLIB_CACHE_MAX = 2000  # xlib 各缓存上限：_doh/_ip/_file/_raw/_raw_denied 均只存
# 可再生的派生内容（DNS 结果、直链、文件列表、风控冷却戳），超限删最旧


def _bounded_put(d: dict, key, value, cap: int = _XLIB_CACHE_MAX) -> None:
    """有界写：dict 按插入序淘汰最旧条目至 cap（分摊 O(1)，见 hls_race 同名函数）。"""
    d[key] = value
    while len(d) > cap:
        d.pop(next(iter(d)), None)
# 小雅 STRM 播放：冷文件首次播放时容器边“转存到用户网盘”边生成直链，/d 代理会
# 同步阻塞到转存完成（常见 6~25s，4K 原盘偶发更久）。这里给足等待，避免客户端超时。
_TRANSFER_TIMEOUT = 75.0
_DPROXY_PORT = 45678     # xiaoya 容器内 STRM/转存代理端口（仅容器内网可达）
# 小雅镜像 nginx 对 /d/ 的固定放行 sign（/etc/nginx/http.d/default.conf 里写死的
# target_sign）。带此 sign 的 http://<小雅>:5678/d/<path> 不跳转容器内网，而由
# AList 直接代理吐流（支持 Range、免登录），正是 TVBox/Emby STRM 的播放形式。
_STRM_SIGN = "bbcef83496367613d707b65c898f780b"

# 115 网盘 CDN 域名在本机可能被 Clash/代理 TUN 的“伪 IP（198.18/15）”DNS 劫持，
# 导致直连超时。115 转存直链的 CDN 主机名不固定（cdnfhnfile / cdnfhn208 /
# cdnfhnfdfs …），统一按后缀 *.115cdn.net 识别，对这些域名改用公共 DoH 解析出的
# 真实 IP 建连（TLS SNI / Host / 签名 URL 均不变），保持服务端直连、不经任何代理。
_REALIP_SUFFIXES = (".115cdn.net", "115cdn.net")
_doh_cache: dict[str, tuple[float, list[str]]] = {}
_DOH_TTL = 300.0
# 网盘风控冷却：收到 403/invalid signature 后，短时间内播放器重试也不再请求小雅/网盘，
# 避免 ExoPlayer/TV 端每秒重试导致风控加重。
_RAW_DENIED_TTL = 90.0


def _needs_realip(host: str) -> bool:
    h = host.lower()
    return h.endswith(_REALIP_SUFFIXES[0]) or h == _REALIP_SUFFIXES[1]


def _real_ips(host: str) -> list[str]:
    """经阿里 DoH 解析域名 A 记录（带短缓存）。失败回退空列表。"""
    now = time.time()
    hit = _doh_cache.get(host)
    if hit and now - hit[0] < _DOH_TTL:
        return hit[1]
    try:
        req = urllib.request.Request(
            f"https://223.5.5.5/resolve?name={host}&type=A",
            headers={"Host": "dns.alidns.com"})
        data = json.load(urllib.request.urlopen(req, timeout=6))
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    except Exception:  # noqa: BLE001
        _log.debug("_real_ips failed: host=%s", host, exc_info=True)
        ips = []
    if ips:
        _bounded_put(_doh_cache, host, (now, ips))
    return ips


def _patched_media_client() -> httpx.AsyncClient:
    """构造对 115 CDN 域名强制走真实 IP 建连的 httpx 客户端（仅 TCP 目标改写，
    URL/TLS SNI/Host 完全保留）。httpcore 支持注入自定义 network backend。"""
    import httpcore
    from httpcore._backends.auto import AutoBackend

    _ip_cache: dict[str, str] = {}

    class _RealIPBackend(AutoBackend):
        async def connect_tcp(self, host, port, timeout=None,
                              local_address=None, socket_options=None):
            if _needs_realip(host):
                ip = _ip_cache.get(host)
                if ip is None:
                    ips = _real_ips(host)
                    ip = ips[0] if ips else ""
                    _bounded_put(_ip_cache, host, ip)
                if ip:
                    host = ip
            return await super().connect_tcp(
                host, port, timeout, local_address, socket_options)

    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        network_backend=_RealIPBackend())
    return httpx.AsyncClient(
        transport=transport, trust_env=False,
        timeout=httpx.Timeout(connect=15.0, read=180.0, write=120.0, pool=15.0),
        follow_redirects=True)
_POSTER_CONCURRENCY = 2   # 豆瓣海报补全并发（温和，避免限流）
_POSTER_GAP = 0.6         # 每次查询间隔秒
_POSTER_BATCH = 24        # 每轮每个大类补多少张
_FLAT_COLLECTION_VIDS = 12  # 目录直接平铺这么多视频视为合集（应下钻到单片）
_DOUBAN_SUGGEST = "https://movie.douban.com/j/subject_suggest"
_MIN_VIDEO_LEAF = 1       # 目录内出现视频即视为影片叶子（除非判定为合集）
# 目录名命中这些词时视为“合集/容器”，强制下钻而非当成一部片
_COLLECTION_HINT = ("部）", "部)", "合集", "系列", "全集", "汇总", "大合集",
                    "top", "TOP", "影史", "获奖", "套餐", "整理")


def cat_by_id(cat_id: str) -> Optional[dict]:
    for c in CATEGORIES:
        if c[0] == cat_id:
            return {"id": c[0], "dir": c[1], "mdb": c[2],
                    "title": c[3], "kind": c[4]}
    return None


def _state_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "xlib")
    os.makedirs(d, exist_ok=True)
    return d


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXT)


def _junk(name: str) -> bool:
    return any(j in name for j in _JUNK_NAME)


def parse_show(dirname: str) -> dict:
    """从目录名解析 标题/年份/清晰度。"""
    name = dirname.strip()
    year = ""
    m = _YEAR_RE.search(name)
    if m:
        year = m.group(0)
    q = [t for t in _QUALITY_TOKENS if t in name.upper()]
    # 去掉括号年份与清晰度标签得到较干净标题
    title = re.sub(r"[（(【\[]\s*(?:19|20)\d{2}[^）)】\]]*[）)】\]]", "", name)
    title = re.sub(r"(?i)\b(" + "|".join(
        re.escape(t) for t in _QUALITY_TOKENS) + r")\b\.?", "", title)
    title = re.sub(r"[（(][^）)]*(?:4K|2160|1080|REMUX|蓝光|杜比|HDR)[^）)]*[）)]", "",
                   title, flags=re.I)
    title = re.sub(r"\s{2,}", " ", title).strip(" -·_　")
    return {"title": title or name, "year": year,
            "quality": "/".join(sorted(set(q), key=q.index))}

_FLAT_NOISE = re.compile(
    r"(?i)[.\s\-]*("
    r"双语字幕?|中字|国语|粤语|国粤双语|中英双语|内嵌中字|"
    r"HR-?HDTV|HDTV|WEB-?DL|WEB|BluRay|BLURAY|X264|X265|H264|H265|HEVC|AAC|AC3|DTS|"
    r"1024[Xx]576|1280[Xx]720|1920[Xx]1080|1080[Pp]?|720[Pp]?|2160[Pp]?|4K|"
    r"人人影视制作|人人影视|字幕组|制作组|-?人人|官方|高清|加长版|导演剪辑版"
    r").*$")


def clean_flat_title(fname: str) -> str:
    """平铺电影文件名 -> 干净片名：从首个技术/来源标记处截断。"""
    stem = os.path.splitext(os.path.basename(fname))[0]
    t = _FLAT_NOISE.sub("", stem)
    t = re.sub(r"[.\s]+$", "", t).replace(".", " ").replace("_", " ")
    t = re.sub(r"\s{2,}", " ", t).strip(" -·_　")
    return t or stem


def ep_index(fname: str) -> int:
    m = _EP_RE.search(fname)
    if m:
        for g in m.groups():
            if g:
                try:
                    return int(g)
                except ValueError:
                    pass
    return 0


class _CatIndex:
    def __init__(self, cat: dict):
        self.cat = cat
        self.items: list[dict] = []     # {id,title,year,quality,files:[paths]}
        self.seen: set[str] = set()     # show dir 路径去重
        self.building = False
        self.done = False
        self._lock = threading.Lock()
        self._loaded = False
        self._size = -1

    @property
    def _path(self) -> str:
        return os.path.join(_state_dir(), f"{self.cat['id']}.jsonl")

    def _ingest(self, lines) -> None:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except ValueError:
                _log.debug("_ingest failed", exc_info=True)
                continue
            sid = it.get("id", "")
            if sid and sid not in self.seen:
                self.seen.add(sid)
                self.items.append(it)

    def load(self) -> None:
        """初次全量读取；之后仅在文件增长时按偏移追加读取新行（构建中实时可见）。"""
        with self._lock:
            try:
                size = os.path.getsize(self._path)
            except OSError:
                self._loaded = True
                self._size = 0
                return
            if self._loaded and size == self._size:
                return
            try:
                with open(self._path, encoding="utf-8") as fh:
                    if self._loaded and 0 <= self._size <= size:
                        fh.seek(self._size)      # 只读新增的追加行
                    else:
                        self.items = []          # 全新/截断：重读全量
                        self.seen = set()
                    self._ingest(fh)
                    self._size = fh.tell()
            except FileNotFoundError:
                self._size = 0
            except Exception as exc:
                _log.info("xlib load %s: %s", self.cat["id"], exc)
            self._loaded = True

    def append(self, it: dict) -> bool:
        line = json.dumps(it, ensure_ascii=False) + "\n"
        with self._lock:
            if it["id"] in self.seen:
                return False
            self.seen.add(it["id"])
            self.items.append(it)
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    self._size = fh.tell()
            except Exception as exc:
                _log.info("xlib persist %s: %s", self.cat["id"], exc)
        return True


class XiaoyaLibrary:
    def __init__(self):
        self._idx: dict[str, _CatIndex] = {
            c[0]: _CatIndex(cat_by_id(c[0])) for c in CATEGORIES}
        self._posters: dict[str, dict] = {}
        self._poster_lock = threading.Lock()
        self._started: set[str] = set()
        self._build_lock = asyncio.Lock()  # 全库同一时刻只允许一个分类在建
        self._build_enabled = True         # 总开关，可运行期关闭以立即停止扫描
        self._file_cache: dict[str, tuple[float, list[str]]] = {}
        self._raw_cache: dict[str, tuple[float, str]] = {}
        self._raw_denied: dict[str, float] = {}
        self._transfer_fut: dict[str, asyncio.Future] = {}
        self._dproxy_base: str = ""
        self._dproxy_candidates: list[str] = []
        self._load_posters()

    # ---------- 可用性 ----------
    def available(self) -> bool:
        try:
            return bool(catvod.available() and catvod.XIAOYA.get("password"))
        except Exception:
            _log.debug("available failed", exc_info=True)
            return False

    def base(self) -> str:
        return catvod.XIAOYA["server"].rstrip("/")

    def strm_url(self, path: str) -> str:
        """小雅 TVBox/Emby 同款播放直链：AList 经 nginx 代理 /d/<path>?sign=...
        直接吐流，冷文件由 AList 保持连接等待转存，热文件 200 直出并支持 Range。
        全程对外可达、免登录，可直接交给播放器（与 TVBox alist.min.js 一致）。"""
        q = urllib.parse.quote(path)
        return f"{self.base()}/d{q}?sign={_STRM_SIGN}"

    def _dproxy(self) -> str:
        """小雅 STRM 转存代理基址（容器内 :45678）。该地址是 302 的 Location，
        仅由本插件在 NAS 侧请求，不直接交给客户端，故用容器内网 IP 即可；
        容器重建后 IP 可能变化，探测失败时重新解析。"""
        if self._dproxy_base:
            return self._dproxy_base
        host = urllib.parse.urlsplit(self.base()).hostname or ""
        candidates: list[str] = []
        # 转存代理端口通常只绑容器内网 IP（未对 LAN 发布），优先用 docker 解析容器 IP。
        try:
            import subprocess
            out = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 "xiaoya"],
                capture_output=True, text=True, timeout=6).stdout.strip()
            if out:
                candidates.append(f"http://{out}:{_DPROXY_PORT}")
        except Exception:
            _log.debug("_dproxy failed", exc_info=True)
            out = ""
        # 回退：小雅主机同端口（个别部署把 45678 发布到宿主）
        if host:
            candidates.append(f"http://{host}:{_DPROXY_PORT}")
        if not candidates:
            candidates = [f"http://172.17.0.11:{_DPROXY_PORT}"]
        # 不在此处做网络探测（避免每次播放等待）：首个候选失败会在取直链时回退。
        self._dproxy_base = candidates[0]
        self._dproxy_candidates = candidates
        return self._dproxy_base

    async def _dproxy_get_location(self, path: str) -> Optional[str]:
        """请求小雅 /d/<path> 转存代理，返回其 302 Location（最终网盘 CDN 直链）。
        /d 会同步阻塞至转存完成；候选地址失败时自动切换。"""
        q = urllib.parse.quote(path)
        last = None
        for base in getattr(self, "_dproxy_candidates", None) or [self._dproxy()]:
            try:
                async with httpx.AsyncClient(
                        trust_env=False, timeout=_TRANSFER_TIMEOUT,
                        follow_redirects=False) as c:
                    r = await c.get(base + "/d" + q)
                loc = r.headers.get("location") or ""
                if r.status_code in (301, 302, 303, 307, 308) and loc.startswith(
                        ("http://", "https://")):
                    self._dproxy_base = base
                    return loc
                last = f"status={r.status_code}"
            except Exception as exc:  # noqa: BLE001
                _log.debug("_dproxy_get_location failed", exc_info=True)
                last = type(exc).__name__
        raise RuntimeError("小雅转存代理无直链: " + str(last))

    # ---------- 海报缓存 ----------
    @property
    def _poster_path(self) -> str:
        return os.path.join(_state_dir(), "posters.json")

    def _load_posters(self) -> None:
        try:
            with open(self._poster_path, encoding="utf-8") as fh:
                self._posters = json.load(fh) or {}
        except (OSError, ValueError):
            _log.debug("_load_posters failed", exc_info=True)
            self._posters = {}

    def save_poster(self, show_id: str, poster: str, overview: str = "") -> None:
        if not poster:
            return
        with self._poster_lock:
            self._posters[show_id] = {"poster": poster, "overview": overview}
            tmp = self._poster_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._posters, fh, ensure_ascii=False)
                os.replace(tmp, self._poster_path)
            except Exception:
                _log.debug("save_poster failed", exc_info=True)
                pass

    def poster_of(self, show_id: str) -> dict:
        return self._posters.get(show_id) or {}

    # ---------- AList 访问 ----------
    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT)

    async def _token(self) -> str:
        return await catvod.xiaoya_token()

    async def list_dir(self, client: httpx.AsyncClient, token: str,
                       path: str) -> list[dict]:
        """列目录全部条目（自动翻页）。返回 [{name,is_dir}]。"""
        out: list[dict] = []
        pg = 1
        while True:
            j = None
            for attempt in range(3):
                try:
                    r = await client.post(
                        self.base() + "/api/fs/list",
                        headers={"Authorization": token,
                                 "Content-Type": "application/json"},
                        json={"path": path, "page": pg, "per_page": _PAGE,
                              "refresh": False})
                    j = r.json()
                    break
                except Exception:
                    _log.debug("list_dir failed", exc_info=True)
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.8 * (attempt + 1))
            data = (j or {}).get("data") or {}
            content = data.get("content") or []
            out.extend(content)
            total = int(data.get("total") or 0)
            if pg * _PAGE >= total or not content:
                break
            pg += 1
        return out

    async def _signed_via_fsget(self, path: str) -> Optional[str]:
        """备用：经 AList /api/fs/get 取 raw_url（热文件较快）。"""
        token = await self._token()
        async with httpx.AsyncClient(trust_env=False, timeout=20.0) as client:
            r = await client.post(
                self.base() + "/api/fs/get",
                headers={"Authorization": token,
                         "Content-Type": "application/json"},
                json={"path": path, "password": ""})
            url = ((r.json().get("data") or {}).get("raw_url") or "")
            return url or None

    async def signed_raw(self, path: str) -> str:
        """取文件最终公网 CDN 直链（STRM 模式）。

        小雅的机制：文件平时不在用户网盘，点击播放时由容器 /d 代理边“转存到自己
        网盘”边生成直链，并同步阻塞到转存完成（冷文件常见 6~25s）。因此这里走
        /d 代理而非 fs/get，并对同一文件的并发请求做单飞合并，避免重复转存。
        直链短缓存，短时间内重复播放直接命中。"""
        hit = self._raw_cache.get(path)
        if hit and time.time() - hit[0] < _FILE_CACHE_TTL:
            return hit[1]
        fut = self._transfer_fut.get(path)
        if fut is not None and not fut.done():
            return await fut
        fut = asyncio.get_event_loop().create_future()
        self._transfer_fut[path] = fut
        try:
            url: Optional[str] = None
            try:
                url = await self._dproxy_get_location(path)
            except Exception as exc:  # noqa: BLE001
                _log.debug("signed_raw failed", exc_info=True)
                logging.getLogger("xlib").warning(
                    "/d 转存失败，回退 fs/get: %s %s", type(exc).__name__, exc)
            if not url:
                url = await self._signed_via_fsget(path)
            if not url:
                raise RuntimeError("小雅取直链失败: " + path[-80:])
            _bounded_put(self._raw_cache, path, (time.time(), url))
            fut.set_result(url)
            return url
        except Exception as exc:
            _log.debug("signed_raw failed", exc_info=True)
            fut.set_exception(exc)
            raise
        finally:
            self._transfer_fut.pop(path, None)

    def raw_denied(self, path: str) -> bool:
        return time.time() < self._raw_denied.get(path, 0.0)

    def mark_raw_denied(self, path: str) -> None:
        _bounded_put(self._raw_denied, path, time.time() + _RAW_DENIED_TTL)
        self._raw_cache.pop(path, None)

    async def open_media(self, path: str, range_header: Optional[str] = None):
        """同源取流：解析网盘 CDN 直链（冷文件仅触发一次小雅转存），经真实 IP
        直连（绕过 TUN 伪 IP）以流式返回 (httpx.Response, client)。调用方负责
        读完后 await client.aclose()。Range 原样透传，支持拖动。"""
        if self.raw_denied(path):
            raise PermissionError("网盘直链风控冷却中")
        url = await self.signed_raw(path)
        headers = {"User-Agent": "Mozilla/5.0 fnmedia-ext"}
        if range_header:
            headers["Range"] = range_header
        client = _patched_media_client()
        req = client.build_request("GET", url, headers=headers)
        resp = await client.send(req, stream=True)
        # 403/过期通常是临时风控/签名失效：进入冷却，避免播放器高频重试放大问题。
        if resp.status_code in (401, 403):
            body = b""
            try:
                body = await resp.aread()
            finally:
                await resp.aclose()
                await client.aclose()
            self.mark_raw_denied(path)
            raise PermissionError("网盘直链鉴权失败: " + body[:120].decode(
                "utf-8", "ignore"))
        return resp, client

    def invalidate_raw(self, path: str) -> None:
        """直链失效时清缓存，下次重新转存/签名。"""
        self._raw_cache.pop(path, None)

    async def show_files(self, show: dict) -> list[str]:
        """播放时懒取一部剧的视频文件（本层 + 季目录），带短缓存。"""
        sid = show.get("id", "")
        # 平铺合集拆出的单片：id 即文件路径
        if sid and _is_video(sid):
            return [sid]
        cached = self._file_cache.get(sid)
        if cached and time.time() - cached[0] < _FILE_CACHE_TTL:
            return cached[1]
        token = await self._token()
        files: list[str] = []
        async with await self._client() as client:
            async def ls_files(path: str):
                try:
                    entries = await self.list_dir(client, token, path)
                except Exception:
                    _log.debug("ls_files failed", exc_info=True)
                    return [], []
                vids = sorted(os.path.join(path, e["name"]) for e in entries
                              if not e.get("is_dir") and _is_video(e["name"])
                              and not _junk(e["name"]))
                subs = [os.path.join(path, e["name"]) for e in entries
                        if e.get("is_dir")]
                return vids, subs
            vids, subs = await ls_files(sid)
            files.extend(vids)
            for sd in subs:
                if _SEASON_DIR_RE.match(os.path.basename(sd)):
                    sv, _ = await ls_files(sd)
                    files.extend(sv)
        files = sorted(set(files), key=lambda f: _natural_key(os.path.basename(f)))
        _bounded_put(self._file_cache, sid, (time.time(), files))
        return files

    # ---------- 海报后台补全 ----------
    def start_poster_filler(self) -> None:
        if getattr(self, "_poster_started", False):
            return
        self._poster_started = True
        asyncio.create_task(self._poster_loop())

    @staticmethod
    def _poster_query(title: str) -> str:
        # 去掉“全30集 / 528级 / 第1季 / 第2部”等后缀（含“全”字与粘连数字）
        t = re.sub(r"(?i)(?:全|第)?\s*0*\d{1,4}\s*(?:集|话|回|期|级|部|季|篇章).*$",
                   "", title)
        t = re.sub(r"(?i)(动画|动漫|纪录片|综艺|短剧|连续剧|电视剧|电影|高清|国粤双语|中英双语).*$",
                   "", t)
        # 去掉【看鉴】之类前缀标签（保留其后方片名）
        t = re.sub(r"^[【\[(（][^】\])）]*[】\])）]\s*", "", t.strip())
        # 去掉年份括号与清晰度/来源标签
        t = re.sub(r"[（(【\[][^）)】\]]*(?:19|20)\d{2}[^）)】\]]*[）)】\]]", "", t)
        t = re.sub(r"(?i)\b(?:" + "|".join(
            re.escape(x) for x in _QUALITY_TOKENS) + r")\b", "", t)
        t = re.sub(r"(?i)(蓝光|杜比|全景声|国语|中字|双语|修复版|导演剪辑)", "", t)
        parts = re.split(r"[\s/|]+", t)
        parts = [x.strip(" -·_　:：()（）") for x in parts]
        parts = [x for x in parts if x and not re.fullmatch(
            r"(?i)(19|20)\d{2}|4k|2160p?|1080p?|720p?", x)]
        if not parts:
            return title.strip()
        # 含中文且最长者最像片名；否则取首个英文段
        _STOP = ("电视剧", "电影版", "电视剧版", "动画版", "纪录片", "高清版",
                 "完整版", "未删减", "合集", "系列", "国语版", "中文版")
        cjk = [x for x in parts
               if re.search(r"[\u4e00-\u9fff]", x)
               and not any(w in x for w in _STOP)]
        if cjk:
            return max(cjk, key=len)
        # 中文段都被过滤（如“繁花 电视剧版”）：回退到第一个中文词
        cjk0 = [x for x in parts if re.search(r"[\u4e00-\u9fff]", x)]
        return cjk0[0] if cjk0 else parts[0]

    @staticmethod
    def _suggest_hit(it: dict, cand: dict) -> bool:
        ct = (cand.get("title") or "").strip().lower()
        mt = it.get("title", "").strip().lower()
        if not ct:
            return False
        if ct == mt:
            return True
        y = str(it.get("year") or "")[:4]
        cy = str(cand.get("year") or "")[:4]
        year_ok = (not y or not cy or y == cy)
        return year_ok and (ct in mt or mt in ct) and abs(len(ct) - len(mt)) <= 4

    async def _fill_one(self, client, it) -> None:
        q = self._poster_query(it.get("title", ""))
        if not q:
            return
        try:
            r = await client.get(
                _DOUBAN_SUGGEST, params={"q": q},
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://movie.douban.com/"}, timeout=10)
            cands = r.json() or []
        except Exception:
            _log.debug("_fill_one failed", exc_info=True)
            return
        if not isinstance(cands, list):
            return
        pick = next((c for c in cands if self._suggest_hit(it, c)), None)
        if pick is None and cands:
            y = str(it.get("year") or "")[:4]
            same = [c for c in cands if y and str(c.get("year") or "")[:4] == y]
            pick = same[0] if same else None
        if not pick or not pick.get("img"):
            return
        sub = (pick.get("sub_title") or "").strip()
        self.save_poster(it["id"], pick["img"], sub)

    async def _poster_loop(self) -> None:
        while True:
            worked = 0
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    for cat in CATEGORIES:
                        idx = self._idx[cat[0]]
                        idx.load()
                        todo = [it for it in idx.items
                                if not self.poster_of(it["id"]).get("poster")]
                        todo = todo[:_POSTER_BATCH]
                        if not todo:
                            continue
                        sem = asyncio.Semaphore(_POSTER_CONCURRENCY)

                        async def run(it):
                            nonlocal worked
                            async with sem:
                                await self._fill_one(client, it)
                                if self.poster_of(it["id"]).get("poster"):
                                    worked += 1
                                await asyncio.sleep(_POSTER_GAP)

                        await asyncio.gather(*[run(it) for it in todo])
            except Exception as exc:
                _log.info("xlib poster loop: %s", str(exc)[:80])
            await asyncio.sleep(30 if worked == 0 else 2)

    # ---------- 遍历建索引（增量、可恢复、不阻塞首屏） ----------
    def page(self, cat_id: str, offset: int, limit: int) -> tuple[list[dict], int]:
        idx = self._idx.get(cat_id)
        if not idx:
            return [], 0
        idx.load()
        chunk = idx.items[offset:offset + limit]
        return chunk, len(idx.items)

    def item(self, show_id: str) -> Optional[dict]:
        for idx in self._idx.values():
            idx.load()
            for it in idx.items:
                if it["id"] == show_id:
                    return it
        return None

    def _cat_of_show(self, show_id: str) -> Optional[str]:
        for cid, idx in self._idx.items():
            idx.load()
            if show_id in idx.seen:
                return cid
        return None

    def by_guid(self, guid: str) -> Optional[dict]:
        """影片 guid 反查 {cat, show}；扫描各大类（首次加载后内存命中）。"""
        if not guid:
            return None
        for cid, idx in self._idx.items():
            idx.load()
            for it in idx.items:
                if show_guid(it["id"]) == guid:
                    return {"cat": cid, "show": it}
        return None

    def show_by_ep_guid(self, guid: str) -> Optional[dict]:
        return self.by_guid(guid)

    def ensure_build(self, cat_id: str) -> None:
        """已禁用任何自动后台建库（含点进分类触发）。

        即使建库只读 /api/fs/list 条目名、不触发转存，后台遍历数千目录仍会在小雅侧
        持续产生请求，并在网盘风控/挂载未恢复时刷大量“获取目录失败”。媒体库浏览只
        读取已落盘索引即可。需要更新索引时必须由用户在管理入口【手动刷新】，
        显式调用 start_manual_build()。可用环境变量 FNMEDIA_XLIB_AUTOBUILD=1
        临时恢复旧的“点进分类即增量建库”。"""
        if not os.environ.get("FNMEDIA_XLIB_AUTOBUILD"):
            if cat_id in self._idx:
                self._idx[cat_id].load()
            return
        if not getattr(self, "_build_enabled", True):
            return
        if cat_id not in self._idx or cat_id in self._started:
            return
        if not self.available():
            return
        idx = self._idx[cat_id]
        idx.load()
        if idx.done:
            return
        self._started.add(cat_id)
        asyncio.create_task(self._build(cat_id))

    def start_manual_build(self, cat_id: str | None = None) -> str:
        """用户在管理入口显式触发的手动索引刷新（仍只读条目名、不转存）。
        cat_id=None 时由调用方决定是否逐分类串行；这里默认仅刷新指定单分类。"""
        if not self.available():
            return "小雅未配置"
        targets = [c[0] for c in CATEGORIES if (cat_id is None or c[0] == cat_id)]
        n = 0
        for cid in targets:
            idx = self._idx.get(cid)
            if idx is None or cid in self._started:
                continue
            idx.load()
            if idx.done and cat_id is not None:
                # 单分类强制刷新：清 done 让其重扫（队列仍可断点续用）
                idx.done = False
            if not idx.done:
                self._build_enabled = True
                self._started.add(cid)
                asyncio.create_task(self._build(cid))
                n += 1
        return f"已排队 {n} 个分类的手动索引刷新"

    def stop_builds(self) -> None:
        """立即停用自动建库：进行中的构建会在下一批退出，未开始的不再启动。"""
        self._build_enabled = False
        self._started.clear()

    def ensure_all(self) -> None:
        """已禁用：不得一次性后台扫描全部小雅分类（高频、易推高网盘风控）。
        保留空实现仅为兼容旧调用点。索引按用户打开的分类逐个增量构建。"""
        return

    def _queue_path(self, cat_id: str) -> str:
        return os.path.join(_state_dir(), f"{cat_id}.queue.json")

    def _load_queue(self, cat_id: str) -> list[str]:
        try:
            with open(self._queue_path(cat_id), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            _log.debug("_load_queue failed", exc_info=True)
            return []

    def _save_queue(self, cat_id: str, q: list[str]) -> None:
        try:
            tmp = self._queue_path(cat_id) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(q, fh, ensure_ascii=False)
            os.replace(tmp, self._queue_path(cat_id))
        except Exception:
            _log.debug("_save_queue failed", exc_info=True)
            pass

    async def _build(self, cat_id: str) -> None:
        """按队列增量展开目录：每轮处理一小批，影片立即入索引，合集继续入队。
        已入索引的影片跳过文件列举，故中断重启后成本很低。

        全库通过 _build_lock 串行化：同一时刻最多一个分类在建，杜绝多分类并发
        把 AList 打爆；循环每批检查 _build_enabled 以便随时停止。"""
        if not getattr(self, "_build_enabled", True):
            self._started.discard(cat_id)
            return
        async with self._build_lock:
            if not self._build_enabled:
                self._started.discard(cat_id)
                return
            await self._build_locked(cat_id)
        self._started.discard(cat_id)

    async def _build_locked(self, cat_id: str) -> None:
        idx = self._idx[cat_id]
        root = idx.cat["dir"]
        idx.building = True
        added = 0
        q = self._load_queue(cat_id)
        if not q:
            q = [root]
        try:
            token = await self._token()
            sem = asyncio.Semaphore(_CONCURRENCY)

            async with await self._client() as client:
                async def ls(path: str) -> tuple[list[str], list[str]]:
                    async with sem:
                        try:
                            entries = await self.list_dir(client, token, path)
                        except Exception as exc:
                            _log.info("xlib list %s: %s", path, str(exc)[:80])
                            return [], []
                    vids = sorted(os.path.join(path, e["name"]) for e in entries
                                  if not e.get("is_dir") and _is_video(e["name"])
                                  and not _junk(e["name"]))
                    subs = [os.path.join(path, e["name"]) for e in entries
                            if e.get("is_dir")]
                    return vids, subs

                def emit_show(show_dir: str, vids: list[str]) -> int:
                    # 建库阶段不再二次列举季目录（点击播放时由 show_files 懒取）
                    if not vids:
                        return 0
                    meta = parse_show(os.path.basename(show_dir))
                    files = sorted(vids,
                                   key=lambda f: _natural_key(os.path.basename(f)))
                    return 1 if idx.append(
                        {"id": show_dir, **meta, "files": files}) else 0

                def emit_flat_files(dir_path: str, vids: list[str]) -> int:
                    # 纯平铺合集（无单片子目录）：每个视频作为一部单片收录
                    n = 0
                    for f in sorted(vids,
                                    key=lambda z: _natural_key(os.path.basename(z))):
                        meta = parse_show(clean_flat_title(f))
                        if idx.append({"id": f, **meta, "files": [f]}):
                            n += 1
                    return n

                round_n = 0
                while q:
                    if not self._build_enabled:
                        # 运行期被要求停止：保留队列，下次从断点继续
                        self._save_queue(cat_id, q)
                        return
                    # 每轮取一批目录并发展开；已索引影片直接跳过
                    batch = q[:_BUILD_BATCH]
                    q = q[_BUILD_BATCH:]
                    results = await asyncio.gather(*[ls(p) for p in batch])
                    new_dirs: list[str] = []
                    show_jobs: list[tuple[str, list[str], list[str]]] = []
                    flat_jobs: list[tuple[str, list[str]]] = []
                    for (path, depth), (vids, subs) in zip(
                            self._with_depth(batch, root), results):
                        if not vids and not subs:
                            continue
                        name = os.path.basename(path)
                        is_root = (path == root)
                        kind = idx.cat["kind"]
                        non_season_subs = [s for s in subs if not
                                           _SEASON_DIR_RE.match(os.path.basename(s))]
                        name_hint = any(h in name for h in _COLLECTION_HINT)
                        # 目录直接平铺大量视频：剧集型是一部多集剧；电影型是平铺电影合集
                        ep_like = _episode_styled(vids)
                        is_series_dir = kind in ("tv", "anime", "variety", "short")
                        flat_overflow = len(vids) > _FLAT_COLLECTION_VIDS
                        flat_films = (flat_overflow and kind == "movie" and not ep_like)
                        # 合集判定：①名称命中合集词 ②≥3 个非季子目录
                        # ③电影型平铺大量非集数化视频
                        collection = (not is_root and (
                            name_hint or len(non_season_subs) >= 3
                            or flat_films))
                        if vids and not collection:
                            show_jobs.append((path, vids, subs))
                            # 季目录之外的普通子目录：可能是额外合集，限深续探
                            if depth < _MAX_DEPTH:
                                new_dirs.extend(
                                    (s, depth + 1) for s in subs
                                    if not _SEASON_DIR_RE.match(os.path.basename(s)))
                        elif collection:
                            # 纯平铺电影合集（无单片子目录）：每个视频作单片；
                            # 有子目录则下钻，平铺视频丢弃（多为合集介绍/重复）
                            if flat_films and vids and not non_season_subs:
                                flat_jobs.append((path, vids))
                            if depth < _MAX_DEPTH:
                                new_dirs.extend((s, depth + 1) for s in subs)
                        elif depth < _MAX_DEPTH:
                            new_dirs.extend((s, depth + 1) for s in subs)
                    if show_jobs:
                        added += sum(emit_show(p, v) for p, v, _s in show_jobs)
                    for p, v in flat_jobs:
                        added += emit_flat_files(p, v)
                    # 队列存路径即可（深度由相对 root 推断）
                    q.extend(p for p, _d in new_dirs)
                    round_n += 1
                    if round_n % _QUEUE_SAVE_EVERY == 0:
                        self._save_queue(cat_id, q)
                    await asyncio.sleep(_BUILD_PACE)  # 节流，避免高频 fs/list
            self._save_queue(cat_id, [])
            try:
                os.remove(self._queue_path(cat_id))
            except OSError:
                pass
            idx.done = True
            _log.info("xlib 构建完成 %s：本轮新增 %d（累计 %d）",
                      cat_id, added, len(idx.items))
        except Exception as exc:
            _log.warning("xlib 构建失败 %s: %s", cat_id, exc)
            self._save_queue(cat_id, q)
            self._started.discard(cat_id)
        finally:
            idx.building = False

    @staticmethod
    def _with_depth(batch: list[str], root: str):
        """为批量路径附深度（相对 root 的层数）。"""
        root_d = root.rstrip("/").count("/")
        out = []
        for p in batch:
            rel = p[len(root):].strip("/")
            depth = 0 if not rel else rel.count("/") + 1
            out.append((p, depth))
        return out

    def status(self) -> dict:
        out = {}
        for cid, idx in self._idx.items():
            idx.load()
            out[cid] = {"total": len(idx.items),
                        "building": idx.building, "done": idx.done,
                        "queued": len(self._load_queue(cid))}
        return out



_NAT_RE = re.compile(r"(\D+|\d+)")


def _natural_key(text: str):
    return [(0, int(tok)) if tok.isdigit() else (1, tok)
            for tok in _NAT_RE.findall(text)]


def _episode_styled(videos: list[str]) -> bool:
    """多数文件名呈现集数/日期编号特征时，判定为剧集型目录（而非电影合集）。"""
    if not videos:
        return False
    pat = re.compile(
        r"(?:第\s*0*\d{1,4}\s*[集话回期]|全\s*0*\d{1,4}\s*[集话回期]|"
        r"[Ss]\d{1,2}[Ee]\d{1,4}|\bE\d{1,3}\b|\bDAY\s*\d|\bEP\s*\d|"
        r"(?:20\d{2})\s*[01]?\d\s*[0-3]?\d\s*期|第?\s*0*\d{1,4}\s*期)",
        re.I)
    hit = sum(1 for f in videos if pat.search(os.path.basename(f)))
    return hit * 2 >= len(videos)


def epnum(path: str) -> int:
    """从文件/路径名解析集数（无法判定返回 0）。优先显式集标记，避开 4 位年份。"""
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    for pat in (r"第\s*0*(\d{1,4})\s*[集话回]",
                r"(?i)\b(?:EP|E|DAY)\s*0*(\d{1,4})",
                r"[Ss]\d{1,2}[Ee]0*(\d{1,4})",
                r"^0*(\d{1,3})$"):
        m = re.search(pat, stem)
        if m:
            return int(m.group(1))
    # 裸 1~3 位数字（如 xxx.12），排除年份与清晰度规格
    for n in re.findall(r"(?<![0-9])(\d{1,3})(?![0-9pPkK])", stem):
        v = int(n)
        if 1900 <= v <= 2099 or v in (480, 720, 1080, 2160, 4320):
            continue
        if v:
            return v
    return 0


# —— 媒体库/影片 guid（稳定 md5）——
LIB_PREFIX = "fnmedia-xlib:"
SHOW_PREFIX = "fnmedia-xshow:"


def lib_guid(cat_id: str) -> str:
    return hashlib.md5((LIB_PREFIX + cat_id).encode()).hexdigest()


def show_guid(show_id: str) -> str:
    return hashlib.md5((SHOW_PREFIX + show_id).encode()).hexdigest()


library = XiaoyaLibrary()
