#!/usr/bin/env python3
"""catvod JVM 爬虫运行时桥接（完整版私有特性 CATVOD）。

在 NAS 的 JDK 上以常驻子进程运行 TVBox type=3 的纯 dex 爬虫 jar
（见 assets/catvod/README.md）。Python 侧通过 JSONL 与 Sidecar 通信。

仅支持「纯 dex」jar；DexGuard 加固 jar（含 assets/*.so、*.guard）无法在 JVM 运行，
由 tvbox.classify_site 标记为 skipped（需 Android 运行时，本模块不处理）。

当前内置适配：小雅 Alist（csp_AListSh）。其登录态由本模块用账号密码换取 alist JWT，
随每次调用注入 Sidecar（Sidecar 反射写入每个 drive，绕开 Android 登录弹窗）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time

import httpx

import features

_log = logging.getLogger("fnmedia")

_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "catvod")
_RUNNER = os.path.join(_ASSET, "build", "catvod-runner.jar")
_XIAOYA_JVM = os.path.join(_ASSET, "jars", "xiaoya-jvm.jar")

ALIST_CLASS = "com.github.catvod.spider.AListSh"

# 本地私有配置（git 忽略、公开构建删除）。保存小雅等 catvod 源的凭证与地址，
# 绝不写进会入库/公开的代码。结构：{"xiaoya": {"server","username","password","drives"}}
_PRIVATE_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catvod.local.json")


def _load_private() -> dict:
    try:
        with open(_PRIVATE_CONF, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        _log.debug("_load_private failed", exc_info=True)
        return {}


_priv = _load_private().get("xiaoya", {})

# catvod 源（可扩展；凭证只来自本地私有配置/环境变量，不进入任何分发包）。
# 默认不带维护者个人的服务器/账号/订阅：分发包中保持空白，由用户在管理页填写自己的
# 小雅（AList）地址与账号后启用。
XIAOYA = {
    "jar": "xiaoya",
    "cls": ALIST_CLASS,
    "server": os.environ.get("FNM_XIAOYA_SERVER", _priv.get("server", "")),
    "username": os.environ.get("FNM_XIAOYA_USER", _priv.get("username", "")),
    "password": os.environ.get("FNM_XIAOYA_PASSWORD", _priv.get("password", "")),
    # 用户自己的完整盘列表（含短剧/原盘/筛选器），来自本地私有配置；缺省为
    # 小雅公开版通用目录（非任何个人订阅）。
    "ext": _priv.get("ext"),
    "drives": [
        {"name": "电影", "startPage": "/电影"},
        {"name": "电视剧", "startPage": "/电视剧"},
        {"name": "综艺", "startPage": "/综艺"},
        {"name": "纪录片", "startPage": "/纪录片"},
        {"name": "动漫", "startPage": "/动漫"},
    ],
}



def _save_private(data: dict):
    tmp = _PRIVATE_CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _PRIVATE_CONF)


def private_status() -> dict:
    """供管理 UI 展示（绝不回传明文密码）。"""
    return {
        "available": available(),
        "server": XIAOYA["server"],
        "username": XIAOYA["username"],
        "has_password": bool(XIAOYA["password"]),
        "drives": len(XIAOYA.get("ext") or XIAOYA.get("drives") or []),
        "custom_ext": bool(XIAOYA.get("ext")),
    }


def save_credentials(server: str | None = None, username: str | None = None,
                     password: str | None = None) -> dict:
    """更新小雅凭证/地址并立即生效（写回仅完整版存在的本地私有配置）。
    空串密码表示不修改；显式传 None 表示该字段不动。"""
    data = _load_private()
    xy = dict(data.get("xiaoya", {}))
    if server is not None and server.strip():
        xy["server"] = server.strip().rstrip("/")
    if username is not None and username.strip():
        xy["username"] = username.strip()
    if password:  # 空串保持原密码
        xy["password"] = password
    data["xiaoya"] = xy
    _save_private(data)
    # 立即重载到运行态
    if xy.get("server"):
        XIAOYA["server"] = xy["server"]
    if xy.get("username"):
        XIAOYA["username"] = xy["username"]
    if xy.get("password"):
        XIAOYA["password"] = xy["password"]
    if "ext" in xy:
        XIAOYA["ext"] = xy.get("ext")
    _token_cache.clear()
    return private_status()


def available() -> bool:
    return bool(getattr(features, "CATVOD", False)) and os.path.exists(_RUNNER)


def _java() -> str:
    for cand in ("/usr/local/bin/java", "java"):
        try:
            subprocess.run([cand, "-version"], capture_output=True, timeout=8)
            return cand
        except Exception:
            _log.debug("_java failed", exc_info=True)
            continue
    return "java"


def ensure_built() -> bool:
    """runner.jar 缺失时用 build.sh 编译（需要 javac）。"""
    if os.path.exists(_RUNNER):
        return True
    build = os.path.join(_ASSET, "build.sh")
    if not os.path.exists(build):
        return False
    try:
        subprocess.run(["bash", build], capture_output=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        _log.warning("catvod runner 构建失败: %s", exc)
    return os.path.exists(_RUNNER)


class Sidecar:
    """常驻 JVM Sidecar（单进程，JSONL）。线程安全串行调用。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._seq = 0

    def _classpath(self) -> str:
        libs = sorted(
            os.path.join(_ASSET, "lib", f)
            for f in os.listdir(os.path.join(_ASSET, "lib"))
            if f.endswith(".jar")
        )
        return os.pathsep.join([_RUNNER, *libs, _XIAOYA_JVM])

    def _start(self):
        if self._proc and self._proc.poll() is None:
            return
        if not ensure_built():
            raise RuntimeError("catvod runner 不可用（编译失败或缺 JDK）")
        cmd = [
            _java(), "-Dfile.encoding=UTF-8", "-Dsun.jnu.encoding=UTF-8",
            "-cp", self._classpath(), "Sidecar",
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def call(self, payload: dict, timeout: float = 90.0):
        with self._lock:
            self._start()
            self._seq += 1
            cid = str(self._seq)
            payload = {"id": cid, **payload}
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("catvod sidecar 无响应")
                resp = json.loads(line)
            except Exception:
                _log.debug("call failed", exc_info=True)
                self.stop()
                raise
            if "error" in resp:
                raise RuntimeError(str(resp["error"])[:500])
            return resp.get("result")

    def stop(self):
        try:
            if self._proc:
                self._proc.stdin.close()
                self._proc.terminate()
        except Exception:
            _log.debug("stop failed", exc_info=True)
            pass
        self._proc = None


_sidecar = Sidecar()


def xiaoya_ext(server: str | None = None) -> list[dict]:
    """构造爬虫 ext。优先用私有配置里用户 TVBox 源自带的完整 drive 列表
    （含短剧/原盘/筛选器），否则用内置默认盘。统一规范化 server。"""
    cfg = XIAOYA
    base = (server or cfg["server"]).rstrip("/") + "/"
    if cfg.get("ext"):
        out = []
        for d in cfg["ext"]:
            d = dict(d)
            if d.get("type") == "global":
                # 账号不放 global（小雅不据此自动登录）；登录态走 token 注入
                d.pop("username", None)
                out.append(d)
            else:
                d["server"] = base
                out.append(d)
        return out
    drives = [{**d, "server": base} for d in cfg["drives"]]
    return [{"type": "global"}, *drives]


_token_cache: dict[str, tuple[float, str]] = {}
_TOKEN_TTL = 90 * 60  # alist JWT 默认有效期较长，保守缓存 90 分钟


async def xiaoya_token(server: str | None = None, force: bool = False) -> str:
    cfg = XIAOYA
    base = (server or cfg["server"]).rstrip("/")
    if not cfg["password"]:
        return ""
    hit = _token_cache.get(base)
    if not force and hit and time.time() - hit[0] < _TOKEN_TTL:
        return hit[1]
    async with httpx.AsyncClient(trust_env=False, timeout=12) as client:
        r = await client.post(
            base + "/api/auth/login",
            json={"username": cfg["username"], "password": cfg["password"]},
        )
        j = r.json()
    if j.get("code") != 200:
        raise RuntimeError(f"小雅登录失败: {j.get('message')}")
    tok = j["data"]["token"]
    _token_cache[base] = (time.time(), tok)
    return tok


def _call(method: str, *, token: str = "", ext=None, **kw):
    return _sidecar.call({
        "class": ALIST_CLASS,
        "ext": ext if ext is not None else xiaoya_ext(),
        "token": token,
        "method": method,
        **kw,
    })


# —— 对外高层接口（阻塞；调用方用 asyncio.to_thread 包装）——

def home(filter_: bool = True, token: str = "") -> dict:
    return _call("home", filter=str(bool(filter_)).lower(), token=token)


def category(tid: str, pg=1, extend: dict | None = None, token: str = "") -> dict:
    return _call("category", tid=tid, pg=str(pg), filter="false",
                 extend=extend or {}, token=token)


def detail(vod_id: str, token: str = "") -> dict:
    res = _call("detail", ids=[vod_id], token=token)
    lst = (res or {}).get("list") or []
    return lst[0] if lst else {}


def search(keyword: str, token: str = "") -> list[dict]:
    res = _call("search", key=keyword, quick="false", token=token)
    return (res or {}).get("list") or []


def play(flag: str, vid: str, token: str = "") -> dict:
    return _call("play", flag=flag, id=vid, token=token)


def parse_episodes(detail: dict) -> list[dict]:
    """把小雅 detail 的 vod_play_from/vod_play_url 解析成
    [{"line","title","id"}]。id 为网盘内文件路径（playerContent 入参）。
    去掉 ~~~danmu 等附加片段。"""
    froms = (detail.get("vod_play_from") or "").split("$$$")
    groups = (detail.get("vod_play_url") or "").split("$$$")
    out = []
    for gi, grp in enumerate(groups):
        line = froms[gi] if gi < len(froms) and froms[gi] else f"线路{gi+1}"
        for item in grp.split("#"):
            item = item.strip()
            if not item or "$" not in item:
                continue
            name, rest = item.split("$", 1)
            fid = rest.split("~~~", 1)[0]
            out.append({"line": line, "title": name.strip() or f"第{len(out)+1}集",
                        "id": fid})
    return out


async def resolve_play(vid: str, flag: str = "") -> dict:
    """解析单集，返回 {url, headers}。url 为小雅签名 /d 链（客户端播放时
    302 到网盘 CDN）。"""
    tok = await xiaoya_token()
    res = await asyncio.to_thread(play, flag, vid, tok)
    headers = {}
    h = res.get("header")
    if h:
        try:
            headers = json.loads(h) if isinstance(h, str) else dict(h)
        except Exception:
            _log.debug("resolve_play failed: vid=%s", vid, exc_info=True)
            headers = {}
    return {"url": res.get("url", ""), "headers": headers,
            "parse": res.get("parse", 0)}


def shutdown():
    _sidecar.stop()
