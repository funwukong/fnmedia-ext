#!/usr/bin/env python3
"""drpyS (QuickJS 风格) JS 爬虫的 Node 运行时桥接（完整版私有特性 DRPY）。

TVBox type=3 中 api 指向 drpy2 引擎（drpy2.min.js）、站点规则在 ext 的站点，
由常驻 Node sidecar 执行（见 assets/drpy/README.md）。Python 经 JSONL 通信。

与 catvod.py（JVM 纯 dex）平级；两者统一由 runner 调度。直播站点（虎牙/斗鱼）
主要用 home/category/search，播放可能依赖端上嗅探（见 drpy README 已知限制）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

import features

_log = logging.getLogger("fnmedia")

_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "drpy")
_SIDECAR = os.path.join(_ASSET, "sidecar.mjs")
_REGISTER = os.path.join(_ASSET, "register.mjs")
_SUB_DIR = os.path.join(_ASSET, "sub")
_SUB_REG = os.path.join(_SUB_DIR, "remote_sites.json")

# Node 解释器：优先本机 v24，其次 PATH
_NODE_CANDIDATES = (
    "/var/apps/nodejs_v24/target/bin/node",
    "/usr/local/bin/node",
    "node",
)


def _node() -> str:
    for c in _NODE_CANDIDATES:
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=8)
            return c
        except Exception:
            _log.debug("_node failed", exc_info=True)
            continue
    return "node"


# drpy 引擎选择（进程级切换，改开关需重启 sidecar）：
#   DRPY_ENGINE=drpy3（默认）—— drpy3 Runtime，每站独立实例；
#   DRPY_ENGINE=drpy2 —— 老路 drpy2.min.js 单例。drpy2 全保留作 fallback。
def drpy_engine() -> str:
    return "drpy2" if os.environ.get("DRPY_ENGINE", "drpy3").strip().lower() == "drpy2" else "drpy3"


def _engine_files(engine: str) -> list[str]:
    if engine == "drpy2":
        return [os.path.join(_ASSET, "drpy2.min.js")]
    return [os.path.join(_ASSET, f) for f in (
        "drpy3.js", "drpy3-peer.js", "drpy3-globals-capture.js",
        "drpy-core-lite.min.js")]


def available() -> bool:
    if not bool(getattr(features, "DRPY", False)):
        return False
    if not (os.path.exists(_SIDECAR) and os.path.exists(_REGISTER)):
        return False
    if not all(os.path.exists(f) for f in _engine_files(drpy_engine())):
        return False
    try:
        subprocess.run([_node(), "--version"], capture_output=True, timeout=8)
        return True
    except Exception:
        _log.debug("available failed", exc_info=True)
        return False


class Sidecar:
    """常驻 Node sidecar（单进程，JSONL，内部严格串行）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._seq = 0
        self._engine = None

    def _start(self):
        engine = drpy_engine()
        if self._proc and self._proc.poll() is None:
            if self._engine == engine:
                return
            self.stop()  # 引擎开关变了：重启 sidecar
        cmd = [_node(), "--import", "./register.mjs", "sidecar.mjs"]
        env = {**os.environ, "DRPY_ENGINE": engine}
        self._proc = subprocess.Popen(
            cmd, cwd=_ASSET, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self._engine = engine

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
                    raise RuntimeError("drpy sidecar 无响应")
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


def _call(site: dict, method: str, args: list | None = None):
    # 部署了独立 spider-runner 容器时（FNM_RUNNER_URL）走统一 HTTP 服务；
    # 否则用进程内 Node sidecar。
    try:
        import runner_client
        if runner_client.enabled():
            return runner_client.invoke({"runtime": "node", "site": site,
                                         "method": method, "args": args or []})
    except Exception as exc:  # noqa: BLE001
        _log.warning("外部 runner 调用失败，回退本地 Node sidecar: %s", exc)
    return _sidecar.call({
        "engine": "drpy",
        "site": site,
        "method": method,
        "args": args or [],
    })


# —— 高层接口（阻塞；调用方 asyncio.to_thread）——

def home(site: dict) -> dict:
    return _call(site, "home")


def home_vod(site: dict) -> dict:
    return _call(site, "homeVod")


def category(site: dict, tid: str, pg=1, extend: dict | None = None) -> dict:
    return _call(site, "category", [tid, int(pg) if str(pg).isdigit() else pg,
                                    False, extend or {}])


def detail(site: dict, vod_id: str) -> dict:
    res = _call(site, "detail", [[vod_id]])
    if isinstance(res, dict):
        lst = res.get("list") or []
        if lst:
            return lst[0]
    return res if isinstance(res, dict) else {}


def search(site: dict, keyword: str, quick: bool = False, pg=1) -> list[dict]:
    res = _call(site, "search", [keyword, quick, int(pg) if str(pg).isdigit() else pg])
    if isinstance(res, dict):
        return res.get("list") or []
    return res if isinstance(res, list) else []


def play(site: dict, flag: str, vid: str) -> dict:
    res = _call(site, "play", [flag, vid])
    return res if isinstance(res, dict) else {"url": str(res)}


# ---------------- 远程 drpyS 订阅源（type=3、规则在 http URL） ----------------

def _read_remote_registry() -> dict:
    try:
        return json.loads(Path(_SUB_REG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.debug("_read_remote_registry failed", exc_info=True)
        return {}


def _write_remote_registry(reg: dict) -> None:
    Path(_SUB_DIR).mkdir(parents=True, exist_ok=True)
    tmp = Path(_SUB_REG).with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(Path(_SUB_REG))


def _script_path(key: str) -> str:
    return os.path.join(_SUB_DIR, key + ".js")


def import_remote(items: list[dict], enable: str = "smart") -> dict:
    """登记远程 drpy 源并下载规则脚本到 sub/<key>.js。

    items: [{name,engine,ext,searchable}]（分类器 drpy 组）。
    每个源用其规则脚本 ext 的 hash 作 key；下载 ext（规则）落盘，引擎 drpy2.min.js
    由 sidecar 按 site.api（本地 drpy://sub/<key>.js）加载时统一使用内置引擎。
    返回 {added,updated,enabled,items:[{key,name,enabled,script_ready}]}。
    """
    Path(_SUB_DIR).mkdir(parents=True, exist_ok=True)
    reg = _read_remote_registry()
    added = updated = enabled_n = 0
    out = []
    import subfetch as _sf
    for it in items:
        ext_url = str(it.get("ext") or "").strip()
        name = str(it.get("name") or "drpy").strip() or "drpy"
        if not ext_url.startswith(("http://", "https://")):
            continue
        key = "r" + hashlib.sha1(ext_url.encode()).hexdigest()[:12]
        spath = _script_path(key)
        ready = os.path.exists(spath) and os.path.getsize(spath) > 0
        if not ready:
            blob, _hit = _sf.fetch_first(_sf.github_mirror_candidates(ext_url))
            if blob:
                Path(spath).write_bytes(blob)
                ready = True
        if not ready:
            _log.warning("drpy 规则下载失败 %s", ext_url)
        rec = reg.get(key)
        if rec:
            en = bool(rec.get("enabled", False))
            reg[key].update({"name": name, "ext": ext_url,
                             "engine": it.get("engine", "")})
            updated += 1
        else:
            en = bool(ready) if enable == "smart" else bool(enable)
            reg[key] = {"key": key, "name": name, "ext": ext_url,
                        "engine": it.get("engine", ""),
                        "script": "sub/" + key + ".js", "enabled": en}
            added += 1
        if reg[key].get("enabled"):
            enabled_n += 1
        out.append({"key": key, "name": name, "enabled": reg[key]["enabled"],
                    "script_ready": ready})
    _write_remote_registry(reg)
    return {"added": added, "updated": updated, "enabled": enabled_n, "items": out}


def remote_sources() -> list[dict]:
    """远程 drpy 源定义，供 providers 合并（api=drpy://sub/<key>.js）。"""
    out = []
    for key, r in _read_remote_registry().items():
        if not r.get("enabled"):
            continue
        spath = _script_path(key)
        if not os.path.exists(spath):
            continue
        out.append({"id": "drpy-sub-" + key, "name": r.get("name", key),
                    "api": "drpy://sub/" + key + ".js",
                    "enabled": True, "xsrc": True, "drpy": True,
                    "builtin": False, "remote_drpy": True})
    return out


def remote_status() -> list[dict]:
    """全部远程 drpy 源（含未启用/脚本未就绪），供管理页。"""
    out = []
    for key, r in _read_remote_registry().items():
        spath = _script_path(key)
        out.append({
            "key": key,
            "id": "drpy-sub-" + key,
            "name": r.get("name", key),
            "enabled": bool(r.get("enabled")),
            "script_ready": os.path.exists(spath) and os.path.getsize(spath) > 0,
            "ext": r.get("ext", ""),
        })
    return out


def set_remote_enabled(key: str, on: bool) -> bool:
    reg = _read_remote_registry()
    if key not in reg:
        return False
    if on and not (os.path.exists(_script_path(key))
                   and os.path.getsize(_script_path(key)) > 0):
        return False  # 脚本未就绪不能启用
    reg[key]["enabled"] = bool(on)
    _write_remote_registry(reg)
    return True


def retry_remote_script(key: str) -> dict:
    """重新下载某远程 drpy 规则脚本（经订阅代理+镜像，源站恢复后）。"""
    import subfetch as _sf
    reg = _read_remote_registry()
    r = reg.get(key)
    if not r:
        return {"ok": False, "error": "未知远程 JS 源"}
    ext_url = r.get("ext", "")
    spath = _script_path(key)
    blob, hit = _sf.fetch_first(_sf.github_mirror_candidates(ext_url))
    if not blob:
        return {"ok": False, "error": f"下载失败：{str(hit)[:100]}"}
    Path(spath).write_bytes(blob)
    return {"ok": True, "size": len(blob)}


def remove_remote(key: str) -> bool:
    reg = _read_remote_registry()
    if key not in reg:
        return False
    reg.pop(key)
    _write_remote_registry(reg)
    try:
        os.unlink(_script_path(key))
    except OSError:
        pass
    return True


def parse_episodes(detail: dict) -> list[dict]:
    """与 catvod.parse_episodes 同构：解析 $$$/#/$ 分组剧集。"""
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


def shutdown():
    _sidecar.stop()
