#!/usr/bin/env python3
"""海阔视界小程序规则（Node sidecar haikuo 引擎分支）的 Python 调用层。

规则是 JSON + pages[] JS 引擎模块 + 懒解析（@lazyRule=/@rule=js:），
由常驻 Node sidecar 的 haikuo 引擎分支执行（见 assets/drpy/haikuo.mjs）。
本模块镜像 drpy.py 的 Sidecar 调用层，但规则 JSON 由 Python 侧从磁盘
读出、内联在 payload 里传进 sidecar——sidecar 绝不从网络取规则。

导入口（四种输入，见 import_rules）：
  - 分享口令：rule:// 全编码规则、￥home_rule￥/￥home_rule_v2￥/￥rule￥
    包裹的 base64://@<标题>@<base64 规则 JSON>、或裸规则 JSON；
  - 云口令：云1/2/5/6/7/9号云剪贴板（云5 → cmd.im，云6 → pasteme，
    其余云号按同一模式依次试两个已知服务，解析不到时明确报错）；
  - 指向规则 json 的网址；
  - .json 文件内容（一个文件可含多条规则）。
规则 JSON 落盘 assets/haikuo/sub/<key>.json，注册表 sub/remote_rules.json
（复用 drpy.import_remote 的登记模式）。v1 只接 type=video 规则，
read 型明确拒绝；X5/WebView 嗅探、磁力播放不在 v1（sidecar 侧已抛错）。

公开构建整文件剥离（tools/public_strip.py DROP_FILES + HAIKUO 开关）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import httpx

import features

_log = logging.getLogger("fnmedia")

_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "haikuo")
_SIDECAR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "drpy", "sidecar.mjs")
_SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "assets", "drpy", "haikuo.mjs")
_SUB_DIR = os.path.join(_ASSET, "sub")
_SUB_REG = os.path.join(_SUB_DIR, "remote_rules.json")

# 云口令解析：单次请求超时与重试上限（硬编码，小阈值，见任务书）。
_CLOUD_TIMEOUT = 15.0
_CLOUD_ATTEMPTS = 2          # 单服务最多 2 次（含一次重试，间隔 0.5s）
_CLOUD_RETRY_DELAY = 0.5
_URL_TIMEOUT = 15.0
_URL_ATTEMPTS = 2
_URL_RETRY_DELAY = 0.5

# 云号 → 已探明的解析端点（2026-11-12 实测，见 ~/workspace/haikuo-codes/
# MANIFEST.json）：云5 走 cmd.im 分享页（HTML 内嵌分享文本），云6 走
# pasteme 的 getContent 接口；xxxxxx 是 pasteme 链接的字面段，不是打码。
_CLOUD_ENDPOINTS = {
    "5": ("cmdim", "https://cmd.im/{key}"),
    "6": ("pasteme", "https://pasteme.tyrantg.com/api/getContent/{key}"),
}
# 未知云号依次试这两个已知服务（云5 的 key 在 pasteme 上返回“数据未找到”，
# 云6 的 key 在 cmd.im 上 404，互不串味）。
_CLOUD_FALLBACK_ORDER = ("5", "6")

# 分享口令：￥home_rule￥/￥home_rule_v2￥/￥rule￥ 包裹的 base64 段。
# 形如 base64://@<标题>@<base64 规则 JSON>，标题本身可含 @，故取最后一段。
_SHARE_B64_RE = re.compile(r"base64://@(.+)", re.S)
# 云口令：云<N>oooole/<key>，云6 的 key 前还有字面段 xxxxxx。
_CLOUD_CODE_RE = re.compile(r"^\s*云\s*(\d+)\s*oooole\s*/\s*(?:xxxxxx\s*/\s*)?([A-Za-z0-9_-]+)\s*$")


# ---------------- 规则本地改写清单（batch 4） ----------------
# 目录与 sidecar haikuo.mjs 的默认 storeDir 一致（req.storeDir >
# HAIKUO_STORE_DIR 环境变量 > 系统临时目录/fnmedia-haikuo）。规则经
# java.io 把改写后的 m3u8 写到这里、返回 file:// 路径；app.py 校验后改走
# 插件内 HTTP 端口（/v/api/ext/haikuo/local/<key>/<fname>.m3u8），清单照常
# 进 HLS 代理改写/缓存。key 只做 URL 命名空间，安全靠 fname 白名单 +
# storeDir 围栏 + #EXTM3U 形状检查，不靠 key 保密。
_LOCAL_MAX_BYTES = 2 * 1024 * 1024
_LOCAL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_LOCAL_FNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*\.m3u8$")


def store_dir() -> str:
    """规则在 sidecar 里的本地文件目录（与 haikuo.mjs 默认一致）。"""
    return os.environ.get("HAIKUO_STORE_DIR") or os.path.join(
        tempfile.gettempdir(), "fnmedia-haikuo")


def local_file_for_url(url: str) -> str | None:
    """sidecar 返回的 file:// 清单 URL → storeDir 内的绝对路径；非法返回 None。"""
    if not url.startswith("file://"):
        return None
    try:
        raw = urllib.parse.urlparse(url).path
        path = urllib.parse.unquote(raw)
    except (ValueError, AttributeError):
        return None
    if not path.endswith(".m3u8"):
        return None
    base = os.path.realpath(store_dir())
    full = os.path.realpath(path)
    if full == base or not full.startswith(base + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full


def local_file_path(key: str, fname: str) -> str | None:
    """本地清单端点用的 (key, fname) → storeDir 内绝对路径；非法返回 None。"""
    if not _LOCAL_KEY_RE.fullmatch(key or ""):
        return None
    if not _LOCAL_FNAME_RE.fullmatch(fname or ""):
        return None
    base = os.path.realpath(store_dir())
    full = os.path.realpath(os.path.join(base, fname))
    if full.startswith(base + os.sep) and os.path.isfile(full):
        return full
    return None


def local_file_is_playlist(path: str) -> bool:
    """文件存在、<2MB 且以 #EXTM3U 开头（规则改写清单的最小形状）。"""
    try:
        if os.path.getsize(path) > _LOCAL_MAX_BYTES:
            return False
        with open(path, "rb") as fh:
            return fh.read(7) == b"#EXTM3U"
    except OSError:
        return False


def local_m3u8_url(key: str, fname: str) -> str:
    """storeDir 内的清单文件对应的插件内相对地址（_self_url 回环取回）。"""
    return f"/v/api/ext/haikuo/local/{key}/{fname}"


class HaikuoImportError(ValueError):
    """导入口令/地址/文件解析失败（用户可见的明确原因）。"""


class HaikuoRuleError(ValueError):
    """规则本身不符合 v1 接入范围（read 型、无标题等）。"""


def _node() -> str:
    for c in ("/var/apps/nodejs_v24/target/bin/node", "/usr/local/bin/node", "node"):
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=8)
            return c
        except (OSError, subprocess.SubprocessError):
            _log.debug("_node failed", exc_info=True)
            continue
    return "node"


def available() -> bool:
    if not bool(getattr(features, "HAIKUO", False)):
        return False
    if not (os.path.exists(_SIDECAR) and os.path.exists(_SHIM)):
        return False
    try:
        subprocess.run([_node(), "--version"], capture_output=True, timeout=8)
        return True
    except (OSError, subprocess.SubprocessError):
        _log.debug("available failed", exc_info=True)
        return False


# ---------------- sidecar 调用层（复用 drpy 的常驻 Node 进程） ----------------

def _call(site: dict, method: str, args: list | None = None,
          rule: str | None = None, timeout: float = 90.0):
    """经 drpy 的常驻 sidecar 调 haikuo 引擎分支。

    v1 不走外部 spider-runner：haikuo 规则在本地 sidecar 进程内执行，
    runner 的 node 后端尚无 haikuo 引擎分支。
    """
    import drpy as _drpym
    payload = {"engine": "haikuo", "site": site, "method": method,
               "args": args or []}
    if rule is not None:
        payload["rule"] = rule
    return _drpym._sidecar.call(payload, timeout=timeout)


def site_for_key(key: str) -> dict:
    """规则 key → sidecar 的 site（ext 即 ruleKey，会话缓存键）。"""
    return {"ext": str(key), "key": str(key)}


def _rule_path(key: str) -> str:
    return os.path.join(_SUB_DIR, str(key) + ".json")


def _rule_text(key: str) -> str:
    return Path(_rule_path(key)).read_text(encoding="utf-8")


_loaded: set[str] = set()  # 本进程已 load 进 sidecar 会话的 ruleKey


def ensure_loaded(key: str) -> None:
    """该规则尚未进 sidecar 会话时 load 一次（规则文本内联进 payload）。"""
    key = str(key)
    if key in _loaded:
        return
    _call(site_for_key(key), "load", rule=_rule_text(key))
    _loaded.add(key)


def _call_loaded(key: str, method: str, args: list | None = None):
    """已 load 的规则上调一个方法；sidecar 重启丢会话时重 load 再试一次。"""
    key = str(key)
    ensure_loaded(key)
    try:
        return _call(site_for_key(key), method, args=args)
    except RuntimeError as exc:
        # RunnerError 是 RuntimeError 的子类；sidecar 重启丢会话时重 load 再试一次。
        if "no loaded rule" not in str(exc):
            raise
        _loaded.discard(key)
        _log.debug("haikuo session lost, reloading key=%s", key)
        ensure_loaded(key)
        return _call(site_for_key(key), method, args=args)


# —— 高层接口（阻塞；调用方 asyncio.to_thread）——

def load(key: str) -> dict:
    ensure_loaded(key)
    return _call(site_for_key(key), "load")


def home(key: str, pg: int = 1) -> list[dict]:
    res = _call_loaded(key, "home", [int(pg)])
    return res.get("items", []) if isinstance(res, dict) else []


def category(key: str, cls: str, pg: int = 1) -> list[dict]:
    res = _call_loaded(key, "category", [str(cls), int(pg)])
    return res.get("items", []) if isinstance(res, dict) else []


def search(key: str, keyword: str, pg: int = 1) -> list[dict]:
    res = _call_loaded(key, "search", [str(keyword), int(pg)])
    return res.get("items", []) if isinstance(res, dict) else []


def detail(key: str, url: str) -> list[dict]:
    try:
        res = _call_loaded(key, "detail", [str(url)])
    except Exception as exc:
        _log_diag("detail", key, outcome="error", url=url,
                  error=f"{type(exc).__name__}: {str(exc)[:120]}")
        raise
    items = res.get("items", []) if isinstance(res, dict) else []
    _log_diag("detail", key, outcome="ok", count=len(items), url=url)
    return items


def navigate(key: str, token: str, pg: int = 1) -> list[dict]:
    """分类按钮导航：sidecar 求值懒代码（设分类变量）后重取 home，
    状态由 sidecar 恢复（默认首页不受影响）。"""
    try:
        res = _call_loaded(key, "navigate", [str(token), int(pg)])
    except Exception as exc:
        _log_diag("navigate", key, outcome="error",
                  error=f"{type(exc).__name__}: {str(exc)[:120]}")
        raise
    items = res.get("items", []) if isinstance(res, dict) else []
    _log_diag("navigate", key, outcome="ok", count=len(items), page=int(pg))
    return items


def play(key: str, token: str) -> dict:
    """懒 token 重放求值，返回 sidecar 的播放分发结果。

    {playable, url, headers?} / {playable: False, sniff: True, url} /
    {playable: False, reason, action}；video:// 与 x5Rule:// 由 sidecar
    直接抛错（v1 不支持，调用方按“明确报错”处理）。
    """
    try:
        res = _call_loaded(key, "play", [str(token)])
    except Exception as exc:
        _log_diag("play", key, outcome="error", url=token,
                  error=f"{type(exc).__name__}: {str(exc)[:120]}")
        raise
    out = res if isinstance(res, dict) else {"playable": False,
                                             "reason": "empty play result",
                                             "action": "none", "url": ""}
    _log_diag("play", key, outcome="ok", url=token,
              result=("playable" if out.get("playable")
                      else "sniff" if out.get("sniff") else "not_playable"),
              reason=str(out.get("reason") or "")[:80])
    return out


def _log_diag(kind: str, key: str, **fields) -> None:
    """海阔诊断事件（best-effort：诊断模块缺失/异常时不影响主链路）。"""
    try:
        import haikuo_diag as _hkdiag
        _hkdiag.note(kind, key=str(key or ""), **fields)
    except Exception:
        _log.debug("haikuo diag note failed", exc_info=True)


# ---------------- 规则校验与注册表（复用 drpy.import_remote 模式） ----------------

def validate_rule(rule: dict) -> dict:
    """Python 侧结构校验（sidecar load 时还会再校验一次）。

    v1 只接 video；read 型明确拒绝，不进注册表。
    """
    if not isinstance(rule, dict):
        raise HaikuoRuleError("规则不是 JSON 对象")
    title = str(rule.get("title") or "").strip()
    if not title:
        raise HaikuoRuleError("规则缺少 title")
    rtype = str(rule.get("type") or "video").strip().lower()
    if rtype != "video":
        raise HaikuoRuleError(
            f"规则「{title}」是 {rtype} 型，v1 只支持 video 型")
    pages = rule.get("pages")
    if pages is not None and not isinstance(pages, (str, list)):
        raise HaikuoRuleError(f"规则「{title}」的 pages 格式不正确")
    return rule


def _rule_key(rule: dict) -> str:
    """规则内容哈希作 key（重导入同一规则即更新，不重复登记）。"""
    blob = json.dumps(rule, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "h" + hashlib.sha1(blob).hexdigest()[:12]


def _read_registry() -> dict:
    try:
        return json.loads(Path(_SUB_REG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.debug("_read_registry failed", exc_info=True)
        return {}


def _write_registry(reg: dict) -> None:
    Path(_SUB_DIR).mkdir(parents=True, exist_ok=True)
    tmp = Path(_SUB_REG).with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(Path(_SUB_REG))


def _register(rule: dict, group: str | None, source: str) -> dict:
    """规则落盘 + 注册表登记，返回登记记录。"""
    validate_rule(rule)
    key = _rule_key(rule)
    Path(_SUB_DIR).mkdir(parents=True, exist_ok=True)
    Path(_rule_path(key)).write_text(
        json.dumps(rule, ensure_ascii=False), encoding="utf-8")
    reg = _read_registry()
    rec = reg.get(key)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    if rec:
        rec.update({"name": str(rule.get("title") or key),
                    "group": (group if group is not None
                              else rec.get("group") or str(rule.get("group") or "")),
                    "file": "sub/" + key + ".json",
                    "source": source, "updated_at": now})
        updated = True
    else:
        rec = {"key": key, "name": str(rule.get("title") or key),
               "group": (group if group is not None
                         else str(rule.get("group") or "")),
               "file": "sub/" + key + ".json", "enabled": True,
               "source": source, "imported_at": now}
        reg[key] = rec
        updated = False
    _write_registry(reg)
    return {"key": key, "name": rec["name"], "group": rec.get("group", ""),
            "enabled": bool(rec.get("enabled", True)), "updated": updated}


def all_remote_sources() -> list[dict]:
    """全部已登记规则的源定义（含已停用，enabled 原样保留），供底表同步。"""
    out = []
    for key, r in _read_registry().items():
        if not os.path.exists(_rule_path(key)):
            continue
        out.append({"id": "haikuo-sub-" + key, "name": r.get("name", key),
                    "api": "haikuo://sub/" + key + ".json",
                    "enabled": bool(r.get("enabled")),
                    "xsrc": True, "haikuo": True,
                    "builtin": False, "remote_haikuo": True})
    return out


def remote_sources() -> list[dict]:
    """已启用规则的源定义，供 providers 合并（api=haikuo://sub/<key>.json）。"""
    return [s for s in all_remote_sources() if s.get("enabled")]


def remote_status() -> list[dict]:
    """全部已登记规则（含未启用/文件缺失），供管理页。"""
    out = []
    for key, r in _read_registry().items():
        out.append({
            "key": key,
            "id": "haikuo-sub-" + key,
            "name": r.get("name", key),
            "group": r.get("group", ""),
            "enabled": bool(r.get("enabled")),
            "script_ready": os.path.exists(_rule_path(key)),
            "source": r.get("source", ""),
        })
    return out


def set_remote_enabled(key: str, on: bool) -> bool:
    reg = _read_registry()
    if key not in reg:
        return False
    if on and not os.path.exists(_rule_path(key)):
        return False  # 规则文件缺失不能启用
    reg[key]["enabled"] = bool(on)
    _write_registry(reg)
    return True


def remove_remote(key: str) -> bool:
    reg = _read_registry()
    if key not in reg:
        return False
    reg.pop(key)
    _write_registry(reg)
    try:
        os.unlink(_rule_path(key))
    except OSError:
        pass
    _loaded.discard(str(key))
    return True


# ---------------- 导入：四种输入 ----------------

def _b64_json(payload: str) -> dict:
    """base64 规则 JSON → dict（缺 padding 自动补）。"""
    raw = (payload or "").strip()
    if not raw:
        raise HaikuoImportError("分享内容为空")
    pad = (-len(raw)) % 4
    try:
        blob = base64.b64decode(raw + "=" * pad)
    except (ValueError, base64.binascii.Error):
        raise HaikuoImportError("分享内容不是合法的 base64")
    try:
        obj = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HaikuoImportError("分享内容解码后不是合法的规则 JSON")
    if not isinstance(obj, dict):
        raise HaikuoImportError("分享内容解码后不是单个规则对象")
    return obj


def parse_share_text(text: str) -> dict:
    """分享口令/分享文本 → 单个规则 dict。

    接受：裸规则 JSON；含 base64://@<标题>@<b64> 的分享文本；
    rule://<b64> 全编码。
    """
    t = (text or "").strip()
    if not t:
        raise HaikuoImportError("输入为空")
    if t.startswith("{"):
        try:
            obj = json.loads(t)
        except ValueError:
            raise HaikuoImportError("输入不是合法的规则 JSON")
        if not isinstance(obj, dict):
            raise HaikuoImportError("输入不是单个规则对象")
        return obj
    m = _SHARE_B64_RE.search(t)
    if m:
        # 标题本身可含 @，base64 段不含 @：取最后一段；分享文本常嵌在
        # 网页 HTML 里，base64 后可能粘着 </html> 等尾巴，只取前导 base64 段。
        tail = m.group(1).strip().rstrip("￥").strip()
        if "@" not in tail:
            raise HaikuoImportError("分享文本里找不到 base64 规则段")
        b64part = tail.rsplit("@", 1)[1].strip()
        m2 = re.match(r"[A-Za-z0-9+/=]+", b64part)
        if not m2:
            raise HaikuoImportError("分享文本里找不到 base64 规则段")
        return _b64_json(m2.group(0))
    if t.startswith("rule://"):
        return _b64_json(t[len("rule://"):])
    raise HaikuoImportError("无法识别的分享内容（既不是规则 JSON，"
                            "也不是 base64:// 分享段或 rule:// 口令）")


def _http_get(url: str, timeout: float, attempts: int) -> httpx.Response:
    """带重试上限的 GET；最后一次失败直接抛（调用方转译为明确报错）。"""
    last: httpx.HTTPError | None = None
    for i in range(max(1, attempts)):
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r
        except httpx.HTTPError as exc:
            last = exc
            _log.debug("_http_get attempt failed: url=%s attempt=%d",
                       url, i, exc_info=True)
            if i + 1 < max(1, attempts):
                time.sleep(_CLOUD_RETRY_DELAY)
    assert last is not None
    raise last


def resolve_cloud_code(code: str) -> dict:
    """云口令（如 云5oooole/a3uy）→ 单个规则 dict。

    云5 → cmd.im 分享页（HTML 内嵌分享文本）；云6 → pasteme
    getContent 接口（data 字段即分享文本）。其余云号依次试两个已知
    服务，都解析不到时明确报错（不猜新服务地址）。
    """
    m = _CLOUD_CODE_RE.match(code or "")
    if not m:
        raise HaikuoImportError(
            "云口令格式不正确，应为「云N + oooole/<口令>」，如 云5oooole/a3uy")
    num, key = m.group(1), m.group(2)
    order = [num] if num in _CLOUD_ENDPOINTS else list(_CLOUD_FALLBACK_ORDER)
    tried: list[str] = []
    for n in order:
        kind, tmpl = _CLOUD_ENDPOINTS[n]
        url = tmpl.format(key=key)
        tried.append(f"云{n}({kind})")
        try:
            r = _http_get(url, _CLOUD_TIMEOUT, _CLOUD_ATTEMPTS)
        except httpx.HTTPError:
            _log.debug("resolve_cloud_code fetch failed: url=%s", url,
                       exc_info=True)
            continue
        if kind == "pasteme":
            try:
                data = r.json()
            except ValueError:
                _log.debug("resolve_cloud_code bad json: url=%s", url,
                           exc_info=True)
                continue
            if not isinstance(data, dict) or data.get("return_code") != 0:
                continue  # 4004 数据未找到：这个云号不是 pasteme，试下一个
            text = str(data.get("data") or "")
        else:  # cmdim：分享页 HTML 内嵌分享文本
            text = r.text
        if "base64://" not in text:
            continue
        try:
            return parse_share_text(text)
        except HaikuoImportError:
            _log.debug("resolve_cloud_code parse failed: url=%s", url,
                       exc_info=True)
            continue
    raise HaikuoImportError(
        f"云口令解析失败（已试：{'、'.join(tried)}）：该口令可能已过期、"
        f"或所属云剪贴板不在已知服务里")


def _rules_from_json_text(text: str) -> list[dict]:
    """规则 .json 文件内容 → 规则列表。

    接受：单个规则对象；规则数组；{"rules": [...]}。
    """
    try:
        obj = json.loads(text)
    except ValueError:
        raise HaikuoImportError("文件不是合法的 JSON")
    if isinstance(obj, dict) and isinstance(obj.get("rules"), list):
        items = obj["rules"]
    elif isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = [obj]
    else:
        raise HaikuoImportError("文件里没有规则对象（应为单个规则或规则数组）")
    rules = [it for it in items if isinstance(it, dict)]
    if not rules:
        raise HaikuoImportError("文件里没有规则对象（应为单个规则或规则数组）")
    return rules


def _fetch_rule_url(url: str) -> list[dict]:
    """规则 json 网址 → 规则列表（直链 JSON 或含分享段的页面）。"""
    r = _http_get(url, _URL_TIMEOUT, _URL_ATTEMPTS)
    body = (r.text or "").strip()
    if "base64://" in body:
        return [parse_share_text(body)]
    return _rules_from_json_text(body)


def import_rules(text: str, group: str | None = None,
                 source: str = "paste") -> dict:
    """导入口总入口：自动识别输入种类，返回登记报告。

    text：分享口令 / 云口令 / 规则 json 网址 / 裸规则 JSON / .json 文件内容。
    source：登记来源标注（paste/cloud/url/file，由调用方按实际入口填）。
    返回 {added, updated, items:[{key,name,group,enabled,updated}],
           errors:[...]}。单条失败不阻断其余条目。
    """
    t = (text or "").strip()
    if not t:
        raise HaikuoImportError("输入为空")
    group = (group or "").strip() or None
    rules: list[dict]
    if _CLOUD_CODE_RE.match(t):
        rules = [resolve_cloud_code(t)]
        source = "cloud"
    elif re.match(r"^https?://\S+$", t):
        rules = _fetch_rule_url(t)
        source = "url"
    elif t.startswith("{") or t.startswith("["):
        try:
            rules = _rules_from_json_text(t)
        except HaikuoImportError:
            rules = [parse_share_text(t)]
        source = "file" if source == "file" else source
    else:
        rules = [parse_share_text(t)]
    added = updated = 0
    items: list[dict] = []
    errors: list[str] = []
    for rule in rules:
        try:
            rec = _register(rule, group, source)
        except (HaikuoRuleError, OSError, ValueError) as exc:
            errors.append(str(exc)[:200])
            _log.debug("import_rules one rule failed", exc_info=True)
            continue
        if rec["updated"]:
            updated += 1
        else:
            added += 1
        items.append(rec)
    return {"added": added, "updated": updated, "items": items,
            "errors": errors}
