#!/usr/bin/env python3
"""多多影视（dduotv*.top）专用适配 —— 仅私有版。

该站不是苹果 CMS，没有标准采集接口：
  * 搜索/详情走自研 JSON API，需固定请求头 web-sign；
  * vod_play_url 是加密串，播放地址由前端 Rust WASM 生成签名 protobuf，
    POST /api.php/web/decode/url 换取真实 m3u8/mp4。

本模块直接内嵌运行其官方 WASM（wasmtime），实现：
  DduoWasm.decode(token, play_from) -> 真实媒体 URL

WASM 来自站点公开前端 JS（/assets/web_app_wasm_bg-*.wasm），仅做编解码，
不包含任何密钥之外的业务逻辑；签名所需 sk/nonce 均在 WASM 内部生成。

注意：
  - wasm 实例非线程安全，所有调用经 Lock 串行化（单次解码为亚毫秒级）；
  - 在 asyncio 事件循环里用 asyncio.to_thread 调用，避免阻塞；
  - WASM/接口随站点改版可能失效，失败时抛 DduoError，由上层降级跳过该线路。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

try:
    from fnlog import get_logger
except ImportError:  # 独立运行缺少 fnlog 时退回标准 logging
    import logging as _logging

    def get_logger(name):  # type: ignore
        return _logging.getLogger(f"fnmedia.{name}")

_log = get_logger("dduo")

try:
    import wasmtime as _wt
except Exception:  # pragma: no cover - 依赖缺失时延迟报错
    _wt = None

# ---- 站点常量（从前端 index-*.js 提取） ----
WEB_SIGN = "ddtvf65f3a83d6d9ad6f"
DEFAULT_BASE = "https://dduotv01.top"
DECODE_PATH = "/api.php/web/decode/url"
WASM_PATH = os.path.join(os.path.dirname(__file__), "assets", "dduotv.wasm")
HTTP_TIMEOUT = 12.0

# 该线路在网页端可解（decode_status / 实测）。APP 专属线路（如 zijianm3u8）
# 服务端直接回 PLATFORM_NOT_ALLOWED，收录也无意义，直接在解析层跳过。
# 不作硬编码白名单——以解码结果 code==1 为准；此处仅为默认偏好排序。


class DduoError(RuntimeError):
    pass


# ---------------- WASM host shim 的极简 JS 对象模型 ----------------

class _Obj:
    def __init__(self):
        self.p: dict = {}


class _Fn(_Obj):
    def __init__(self, body: str = ""):
        super().__init__()
        self.body = body


class _U8:
    def __init__(self, n=0):
        self.buf = bytearray(int(n))
        self.length = len(self.buf)
        self.p: dict = {}


class _View:
    def __init__(self, base, start, end):
        self.base = base
        self.s = start
        self.e = end
        self.length = max(0, end - start)


class _Versions(_Obj):
    def __init__(self):
        super().__init__()
        self.node = "24.0.0"


class _Proc(_Obj):
    def __init__(self):
        super().__init__()
        self.versions = _Versions()
        self.node = True


class _Crypto(_Obj):
    def __init__(self):
        super().__init__()
        self.p["getRandomValues"] = _Fn("grv")
        self.p["randomFillSync"] = _Fn("grv")


def _fill_random(arr):
    if isinstance(arr, _U8):
        arr.buf[:] = os.urandom(len(arr.buf))
    elif isinstance(arr, _View):
        arr.base.buf[arr.s:arr.e] = os.urandom(arr.length)


def _invoke(fn, _this, args):
    if isinstance(fn, _Fn):
        if fn.body == "grv":
            _fill_random(args[0])
            return args[0]
        if fn.body == "return this":
            return _GLOBAL
    return None


_GLOBAL = _Obj()
_CRYPTO = _Crypto()
_PROC = _Proc()
_GLOBAL.p["crypto"] = _CRYPTO
_GLOBAL.p["process"] = _PROC


class DduoWasm:
    """单例：加载一次 WASM，串行调用 create_decode_request / parse_decode_response。"""

    _instance: "DduoWasm | None" = None
    _instance_lock = threading.Lock()

    def __init__(self):
        if _wt is None:
            raise DduoError("缺少 wasmtime 依赖，无法解码多多影视")
        if not os.path.exists(WASM_PATH):
            raise DduoError(f"WASM 文件不存在: {WASM_PATH}")
        self._lock = threading.Lock()
        self._store = _wt.Store()
        module = _wt.Module.from_file(self._store.engine, WASM_PATH)
        linker = _wt.Linker(self._store.engine)
        self._ex: dict = {}
        self._define_hosts(linker)
        inst = linker.instantiate(self._store, module)
        exports_obj = inst.exports(self._store)
        for name in (
            "memory", "__wbindgen_externrefs", "__wbindgen_malloc",
            "__wbindgen_realloc", "__wbindgen_free", "__wbindgen_start",
            "create_decode_request", "parse_decode_response",
            "decoderesult_code", "decoderesult_msg", "decoderesult_data",
            "__wbg_decoderesult_free",
        ):
            self._ex[name] = exports_obj[name]
        self._ex["__wbindgen_start"](self._store)

    # ---- 单例 ----
    @classmethod
    def get(cls) -> "DduoWasm":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---- host imports ----
    def _define_hosts(self, linker):
        wt = _wt
        ER, I32, F64 = wt.ValType.externref(), wt.ValType.i32(), wt.ValType.f64()
        M = "./web_app_wasm_bg.js"
        store = self._store

        def host(name, params, results, cb):
            import inspect
            narg = len(inspect.signature(cb).parameters)

            def wrap(*a):
                return cb() if narg == 0 else cb(*a)

            linker.define(store, M, name,
                          wt.Func(store, wt.FuncType(params, results), wrap))

        def mem():
            return self._ex["memory"]

        def reg(o):
            t = self._ex["__wbindgen_externrefs"]
            i = t.size(store)
            t.grow(store, 1, None)
            t.set(store, i, o)
            return i

        def read_str(p, l):
            return bytes(mem().read(store, p, p + l)).decode("utf-8", "replace")

        host("__wbg_randomFillSync_f8c153b79f285817", [ER, ER], [],
             lambda o, a: _fill_random(a))
        host("__wbg_getRandomValues_b3f15fcbfabb0f8b", [ER, ER], [],
             lambda o, a: _fill_random(a))
        host("__wbg_crypto_86f2631e91b51511", [ER], [ER],
             lambda o: o.p.get("crypto", _CRYPTO) if isinstance(o, _Obj) else _CRYPTO)
        host("__wbg_process_3975fd6c72f520aa", [ER], [ER],
             lambda o: o.p.get("process", _PROC) if isinstance(o, _Obj) else _PROC)
        host("__wbg_versions_4e31226f5e8dc909", [ER], [ER],
             lambda o: getattr(o, "versions", None))
        host("__wbg_node_e1f24f89a7336c2e", [ER], [ER],
             lambda o: getattr(o, "node", True))
        host("__wbg_msCrypto_d562bbe83e0d4b91", [ER], [ER],
             lambda o: o.p.get("msCrypto") if isinstance(o, _Obj) else None)
        host("__wbg_require_b74f47fc2d022fd6", [], [ER], lambda: _Fn("require"))

        def new_fn(p, l):
            return _Fn(read_str(p, l))

        host("__wbg_new_no_args_1c7c842f08d00ebb", [I32, I32], [ER], new_fn)
        host("__wbg_call_389efe28435a9388", [ER, ER], [ER],
             lambda f, th: _invoke(f, th, ()))
        host("__wbg_call_4708e0c13bdc8e95", [ER, ER, ER], [ER],
             lambda f, th, a: _invoke(f, th, (a,)))
        host("__wbg_now_a3af9a2f4bbaa4d1", [], [F64],
             lambda: time.time() * 1000.0)
        host("__wbg_length_32ed9a279acd054c", [ER], [I32],
             lambda o: getattr(o, "length", 0))
        host("__wbg_prototypesetcall_bdcdcc5842e4d77d", [I32, I32, ER], [],
             lambda p, l, fn: None)
        host("__wbg_new_with_length_a2c39cbe88fd8ff1", [I32], [ER],
             lambda n: _U8(n))
        host("__wbg_subarray_a96e1fef17ed23cb", [ER, I32, I32], [ER],
             lambda a, s, e: _View(a, s, e))
        host("__wbg_static_accessor_GLOBAL_THIS_e628e89ab3b1c95f", [], [I32],
             lambda: reg(_GLOBAL))
        host("__wbg_static_accessor_SELF_a621d3dfbb60d0ce", [], [I32],
             lambda: reg(_GLOBAL))
        host("__wbg_static_accessor_GLOBAL_12837167ad935116", [], [I32],
             lambda: reg(_GLOBAL))
        host("__wbg_static_accessor_WINDOW_f8727f0cf888e0bd", [], [I32],
             lambda: reg(None))

        def throw(p, l):
            raise DduoError(read_str(p, l))

        host("__wbg___wbindgen_throw_be289d5034ed271b", [I32, I32], [], throw)
        host("__wbg___wbindgen_is_object_5ae8e5880f2c1fbd", [ER], [I32],
             lambda o: 1 if isinstance(o, _Obj) else 0)
        host("__wbg___wbindgen_is_string_cd444516edc5b180", [ER], [I32],
             lambda o: 1 if isinstance(o, str) else 0)
        host("__wbg___wbindgen_is_function_0095a73b8b156f76", [ER], [I32],
             lambda o: 1 if isinstance(o, _Fn) else 0)
        host("__wbg___wbindgen_is_undefined_9e4d92534c42d778", [ER], [I32],
             lambda o: 1 if o is None else 0)
        host("__wbindgen_init_externref_table", [], [], lambda: None)
        host("__wbindgen_cast_0000000000000001", [I32, I32], [ER],
             lambda p, l: ("mem", p, l))
        host("__wbindgen_cast_0000000000000002", [I32, I32], [ER], read_str)

    # ---- 编解码 ----
    def _pass(self, b: bytes):
        p = self._ex["__wbindgen_malloc"](self._store, len(b), 1)
        self._ex["memory"].write(self._store, b, p)
        return p, len(b)

    def _gstr(self, pair):
        p, l = pair
        return bytes(self._ex["memory"].read(self._store, p, p + l)).decode(
            "utf-8", "replace")

    def build_request(self, token: str, play_from: str) -> bytes:
        with self._lock:
            p0, l0 = self._pass(token.encode())
            p1, l1 = self._pass(play_from.encode())
            p, l = self._ex["create_decode_request"](
                self._store, p0, l0, p1, l1)
            out = bytes(self._ex["memory"].read(self._store, p, p + l))
            self._ex["__wbindgen_free"](self._store, p, l, 1)
        return out

    def parse_response(self, data: bytes) -> tuple[int, str, str]:
        with self._lock:
            p, l = self._pass(data)
            r, _err, iserr = self._ex["parse_decode_response"](
                self._store, p, l)
            if iserr:
                raise DduoError("WASM 解析解码响应失败")
            code = self._ex["decoderesult_code"](self._store, r)
            msg = self._gstr(self._ex["decoderesult_msg"](self._store, r))
            url = self._gstr(self._ex["decoderesult_data"](self._store, r))
            self._ex["__wbg_decoderesult_free"](self._store, r, 0)
        return code, msg, url


# ---------------- 数据模型 ----------------

@dataclass
class DduoLine:
    play_from: str
    show: str            # 线路展示名（如 PO蓝光、奇艺）
    tokens: list[str] = field(default_factory=list)  # 每集加密串（与集名对齐）


def base_of(source: dict) -> str:
    """从源配置 api 字段取站点根地址（允许用户换域名）。"""
    api = (source or {}).get("api", "") or DEFAULT_BASE
    p = urllib.parse.urlparse(api)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return DEFAULT_BASE


def _headers(base: str) -> dict:
    return {
        "web-sign": WEB_SIGN,
        "User-Agent": "Mozilla/5.0",
        "Referer": base + "/",
        "Origin": base,
    }


async def search(client: httpx.AsyncClient, source: dict,
                 keyword: str) -> list[dict]:
    """搜索，返回原始 vod dict 列表（JSON data 数组）。"""
    base = base_of(source)
    try:
        r = await client.get(
            base + "/api.php/web/search/index",
            params={"wd": keyword, "page": 1, "limit": 20},
            headers=_headers(base), timeout=HTTP_TIMEOUT)
        j = r.json()
    except Exception:
        _log.debug("search failed: source=%s", source, exc_info=True)
        return []
    if not isinstance(j, dict) or j.get("code") != 200:
        return []
    data = j.get("data")
    return data if isinstance(data, list) else []


async def get_detail(client: httpx.AsyncClient, source: dict,
                     vod_id: str) -> dict | None:
    base = base_of(source)
    try:
        r = await client.get(
            base + "/api.php/web/vod/get_detail",
            params={"vod_id": vod_id},
            headers=_headers(base), timeout=HTTP_TIMEOUT)
        j = r.json()
    except Exception:
        _log.debug("get_detail failed: source=%s", source, exc_info=True)
        return None
    if not isinstance(j, dict) or j.get("code") != 200:
        return None
    data = j.get("data")
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def parse_lines(detail: dict) -> list[DduoLine]:
    """把详情里的 vod_play_from / vod_play_url 拆成线路 -> 每集 token。"""
    froms = (detail.get("vod_play_from") or "").split("$$$")
    groups = (detail.get("vod_play_url") or "").split("$$$")
    # vodplayer 携带每线路展示名 / decode_status
    shows: dict[str, str] = {}
    for vp in (detail.get("vodplayer") or []):
        if isinstance(vp, dict):
            pf = vp.get("from") or vp.get("play_from") or ""
            nm = vp.get("show") or vp.get("name") or pf
            if pf:
                shows[pf] = str(nm)
    out: list[DduoLine] = []
    for i, group in enumerate(groups):
        pf = froms[i] if i < len(froms) else ""
        if not pf:
            continue
        tokens: list[str] = []
        for item in group.split("#"):
            item = item.strip()
            if not item:
                continue
            # 形如 第1集$<token>；个别源无集名时整段即 token
            tokens.append(item.split("$", 1)[1] if "$" in item else item)
        if tokens:
            out.append(DduoLine(pf, shows.get(pf, pf), tokens))
    return out


async def decode_url(client: httpx.AsyncClient, source: dict,
                     token: str, play_from: str) -> str | None:
    """调用 WASM + decode 接口，把加密串换成真实媒体 URL。失败返回 None。"""
    import asyncio
    base = base_of(source)
    try:
        pb = await asyncio.to_thread(
            DduoWasm.get().build_request, token, play_from)
        r = await client.post(
            base + DECODE_PATH, content=pb,
            headers={**_headers(base),
                     "Content-Type": "application/x-protobuf",
                     "Accept": "application/x-protobuf"},
            timeout=HTTP_TIMEOUT)
        raw = r.content
        code, msg, url = await asyncio.to_thread(
            DduoWasm.get().parse_response, raw)
    except Exception:
        _log.debug("decode_url failed: source=%s", source, exc_info=True)
        return None
    if code == 1 and url.startswith(("http://", "https://")):
        return url
    return None


def is_dduo(source: dict) -> bool:
    return bool((source or {}).get("dduo"))
