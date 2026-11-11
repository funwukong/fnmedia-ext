#!/usr/bin/env python3
"""HLS 播放链路与线路竞速：探活、复活、竞速、播放列表、HLS 路由。

P2 拆分：从 proxy/app.py 抽出（行为零变化）。本模块不得反向依赖 app.py
的运行时单例；与 app.py 共享的名字在各函数内经 ``from app import ...``
延迟导入（避免循环导入，同时保留测试对 appmod.<name> 的 patch 点）。
"""
from __future__ import annotations

from fastapi.responses import FileResponse
from pathlib import Path
from fastapi.responses import RedirectResponse
from fastapi import Request
from fastapi import Response
from fastapi.responses import StreamingResponse
import asyncio
import features
import httpx
import inject as inj
import linepredict
import providers
import re
import sourcehealth
import time
import urllib.parse

from fnlog import get_logger

_log = get_logger("app")  # 与 app._log 同一 logger 对象

# 与 app.ONLINE_PREFIX 同值（路由装饰器在定义时求值，无法经 app 懒取）。
ONLINE_PREFIX = "/v/media/online"

from fastapi import APIRouter

hls_router = APIRouter()
_LINE_BAD_TTL = 300.0          # 坏线记忆 5 分钟（签名时效内，避免长期误判偶发抖动）
_LINE_BAD: dict[str, float] = {}
_PROBE_TIMEOUT = 8.0
_PROBE_MAX_CANDIDATES = 8     # 最多探测候选线路数（控制起播开销）
_PROBE_RANGE = "bytes=0-2047"


def _line_bad_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{picked.get("line", "")}|{int(picked.get("index", 0) or 0)}'


def _mark_line_bad(picked: dict) -> None:
    if not picked or not picked.get("line"):
        return
    _LINE_BAD[_line_bad_key(picked)] = time.time()
    if len(_LINE_BAD) > 1000:
        for k in list(_LINE_BAD)[:500]:
            _LINE_BAD.pop(k, None)


def _line_is_bad(picked: dict) -> bool:
    ts = _LINE_BAD.get(_line_bad_key(picked))
    if ts is None:
        return False
    if time.time() - ts >= _LINE_BAD_TTL:
        _LINE_BAD.pop(_line_bad_key(picked), None)
        return False
    return True


def _autofail_on() -> bool:
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        return bool(config.get().get("line_autofail", True))
    except Exception:
        _log.debug("_autofail_on failed", exc_info=True)
        return True


def _source_health_enabled() -> bool:
    """源健康自动降级开关（config.source_health_enabled，默认 True）。

    关时：搜索不再跳过冷却窗内的死源，也不再记录新的健康画像
    （降级不记录/不冷却）；取流级熔断（播放 dodge/竞速沉底）不受此开关影响，
    那是播放链的故障切换，不是搜索降级。
    """
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        return bool(config.get().get("source_health_enabled", True))
    except Exception:
        _log.debug("_source_health_enabled failed", exc_info=True)
        return True


def _domain_heal_enabled() -> bool:
    """域名自愈开关（config.domain_heal_enabled，默认 True）。

    关时：/v/api/ext/source/heal 不再试备用域名，直接返回空结果，不换域名。
    """
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        return bool(config.get().get("domain_heal_enabled", True))
    except Exception:
        _log.debug("_domain_heal_enabled failed", exc_info=True)
        return True


def _episode_candidates(picked: dict, include_current: bool = True) -> list[dict]:
    """枚举该集在各线路下的可播目标（按 store 默认顺序：默认线在前）。
    直接复用索引，不发起任何网络请求。"""
    from app import store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    og = picked.get("online_guid", "")
    idx = int(picked.get("index", 0) or 0)
    cur_line = picked.get("line")
    brief = store.get_title_by_guid(og) if og else None
    if not brief:
        return []
    default = store.best_line(og)
    ordered = ([default] if default else []) + \
              [ln for ln in brief.get("lines", {}) if ln != default]
    out: list[dict] = []
    seen = set()
    for ln in ordered:
        if not include_current and ln == cur_line:
            continue
        eps = store._direct_eps(brief, ln)
        if idx >= len(eps):
            continue  # 该线路没有这一集（如小雅仅 1 集），直接排除
        t = store.resolve_episode(og, ln, idx)
        if not t or not t.get("url"):
            continue
        t = dict(t, ep_guid=picked.get("ep_guid", ""),
                 kind=picked.get("kind", "title"))
        if t["url"] in seen:
            continue
        seen.add(t["url"])
        out.append(t)
    return out


def _ordered_candidates(picked: dict) -> list[dict]:
    """候选排序：当前线置顶，已知坏线沉底。"""
    from app import _episode_candidates, _line_is_bad  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    cands = _episode_candidates(picked)
    cur = picked.get("line")

    def rank(t: dict) -> tuple:
        from app import _line_is_bad  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        sk = sourcehealth.source_key_of(t.get("line"), t)
        # 当前线置顶 → 坏线/死源沉底 → 按源健康画像（成功率/延迟）排序
        return (t["line"] != cur,
                _line_is_bad(t),
                sourcehealth.weight(sk) if sk else (1, 5.0, 1.0))

    cands.sort(key=rank)
    return cands


async def _probe_latency(cand: dict, request: Request,
                         build_hls: bool = False) -> float | None:
    """探测一条线路该集是否可播，并返回“起播延迟”（秒，越小越快）。

    会就地解析（专有源/跳转/分享，与真实播放同一套逻辑）。失败/文件不存在返回 None。
    build_hls=True 时，HLS 线路顺手构建改写后的播放列表并填缓存（main 取流零额外抓取）；
    竞速阶段传 False，只做轻量验活（抓原始清单，不改写），选出胜者后再构建。"""
    from app import _fetch_playlist, _get_playlist_cached, _resolve_if_needed, _self_url, _verify_rewritten_first_seg, ext_client  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    t0 = time.time()
    try:
        await _resolve_if_needed(cand, request)
        raw_url = cand.get("url", "")
        if not raw_url:
            return None
        # 网盘转存后是本服务根相对地址（/v/api/ext/pan/play?fid=…），经回环自调用
        url = _self_url(raw_url)
        headers = cand.get("headers") or {"User-Agent": "Mozilla/5.0"}
        cli = ext_client(request)

        # 非 HLS（网盘 /d 直链、mp4 等）：Range GET 探测
        is_m3u8 = bool(cand.get("m3u8")) or bool(
            re.search(r"\.m3u8(\?|$)", url.split("?", 1)[0], re.I))
        # 网盘播放代理按 fid 实时换链：转码档返回清单、原画直出 mp4，URL 无后缀，
        # 直接交给播放列表构建（build_hls）或轻量抓清单判定，避免误走 Range 探测。
        is_pan_proxy = "/v/api/ext/pan/" in url
        if not is_m3u8 and not is_pan_proxy:
            try:
                req = cli.build_request(
                    "GET", url, headers={**headers, "Range": _PROBE_RANGE})
                r = await cli.send(req, follow_redirects=True)
                try:
                    status = r.status_code
                    ctype = (r.headers.get("content-type") or "").lower()
                    body = b""
                    try:
                        body = await r.aread()
                    except Exception:
                        _log.debug("_probe_latency failed", exc_info=True)
                        pass
                finally:
                    try:
                        await r.aclose()
                    except Exception:
                        _log.debug("_probe_latency failed", exc_info=True)
                        pass
            except Exception:
                _log.debug("_probe_latency failed", exc_info=True)
                return None
            if status >= 400:
                return None
            # 小雅/AList 对“目录或不存在文件”回 200 + JSON 错误（假活），判死
            if "json" in ctype or body[:1] in (b"{", b"["):
                return None
            return time.time() - t0

        # HLS
        key = _playlist_cache_key(cand)
        cached = _PLAYLIST_CACHE.get(key)
        if cached:
            body = cached[1]
            if body is not _NOT_HLS and \
                    not await _verify_rewritten_first_seg(body, request, cand):
                _PLAYLIST_CACHE.pop(key, None)
                return None
            return time.time() - t0
        if build_hls:
            mg = getattr(request.state, "_play_mg", "") if hasattr(request, "state") else ""
            if mg:
                body = await _get_playlist_cached(mg, cand, request)
                if body is None:
                    return None
                if body is not _NOT_HLS and \
                        not await _verify_rewritten_first_seg(body, request, cand):
                    _PLAYLIST_CACHE.pop(key, None)
                    return None
                return time.time() - t0
        # 轻量验活：抓原始清单（跟随嵌套 master），不做分片改写
        text, _cur = await _fetch_playlist(request, url, headers)
        if text is None or not _is_media_playlist(text):
            return None
        # 分片级验活：部分加固源（海绵/糯米的 yximgs PNG 伪装流等）清单能下、
        # 但所有分片在源站 404（清单活、分片死）。只验清单会误判可播，起播即报错。
        # 取首个分片 Range 2KB 复核：4xx/5xx 或空体即判死，触发自动换线。
        try:
            first_seg = next(
                (ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")), "")
            if first_seg:
                seg_url = urllib.parse.urljoin(_cur, first_seg)
                sreq = cli.build_request(
                    "GET", seg_url, headers={**headers, "Range": _PROBE_RANGE})
                sr = await cli.send(sreq, follow_redirects=True)
                try:
                    if sr.status_code >= 400:
                        _log.info("线路首分片%d判死: %s 《%s》%s",
                                  sr.status_code, cand.get("line"),
                                  cand.get("title"),
                                  urllib.parse.urlsplit(seg_url).netloc)
                        return None
                    head = b""
                    try:
                        head = await sr.aread()
                    except Exception:
                        _log.debug("_probe_latency failed", exc_info=True)
                        pass
                    # 源站对失效分片常回 200/404 + XML/HTML 错误页（非媒体）
                    if not head or head[:1] in (b"<", b"{", b"["):
                        return None
                finally:
                    try:
                        await sr.aclose()
                    except Exception:
                        _log.debug("_probe_latency failed", exc_info=True)
                        pass
        except Exception:
            _log.debug("_probe_latency failed", exc_info=True)
            return None
        return time.time() - t0
    except Exception:
        _log.debug("_probe_latency failed", exc_info=True)
        return None


async def _probe_target(cand: dict, request: Request) -> bool:
    """探测某条线路是否可播（HLS 顺带构建播放列表缓存）。布尔版，供故障切换复用。"""
    lat = await _probe_latency(cand, request, build_hls=True)
    sk = sourcehealth.source_key_of(cand.get("line"), cand)
    if sk:
        # 取流探测结果只记取流级熔断（5 次/3 分钟），不影响搜索级计数。
        sourcehealth.record(sk, lat is not None, lat or 0.0, level="stream")
    return lat is not None


# 首分片验活的短期正缓存：真实分片 URL -> 验活时间。签名 URL 会变，自然按新
# URL 重新验活。避免正常播放每次取清单都探测上游。
_SEG_OK_CACHE: dict[str, float] = {}
_SEG_OK_TTL = 180.0


async def _verify_rewritten_first_seg(body: str, request: Request,
                                      picked: dict | None = None) -> bool:
    """对已改写的清单做首分片验活：解码 /seg?u= 里的真实上游地址，直接 Range 探测。
    识别“清单活、分片 404/错误页”的死链（如某源 yximgs CDN 删流）。不在进程内
    回环请求自身（会受 worker 并发影响而误判）。探测异常不轻易判死，避免误伤。"""
    from app import _self_url, b64d, ext_client  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        first = next((ln.strip() for ln in body.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")), "")
        if not first or first.startswith("hls?"):
            return True  # 紧凑分片/无分片信息：交由实际分片端点处理，不预判
        qs = urllib.parse.urlparse(first if first.startswith("http")
                                   else _self_url(first)).query
        u = urllib.parse.parse_qs(qs).get("u", [""])[0]
        if not u:
            return True
        real = b64d(u)
        now = time.time()
        if _SEG_OK_CACHE.get(real) and now - _SEG_OK_CACHE[real] < _SEG_OK_TTL:
            return True
        headers = dict((picked or {}).get("headers") or
                       {"User-Agent": "Mozilla/5.0"})
        # >>> PRIVATE:GUARD
        # 网盘分片需登录 Cookie
        try:
            host = urllib.parse.urlsplit(real).netloc.lower()
            if host.endswith(".quark.cn"):
                import pan as _panq
                headers["Cookie"] = _panq.auth_cookie_or_raise()
                headers.setdefault("Referer", "https://pan.quark.cn/")
            elif host.endswith(".uc.cn"):
                import panuc as _panu
                headers["Cookie"] = _panu.auth_cookie_or_raise()
                headers.setdefault("Referer", "https://drive.uc.cn/")
        except Exception:
            _log.debug("_verify_rewritten_first_seg failed", exc_info=True)
            pass
        # <<< PRIVATE:GUARD
        headers["Range"] = _PROBE_RANGE
        cli = ext_client(request)
        req = cli.build_request("GET", real, headers=headers)
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return False
            b = b""
            try:
                b = await r.aread()
            except Exception:
                _log.debug("_verify_rewritten_first_seg failed", exc_info=True)
                return True  # 读体失败不判死
            if not b or b[:1] in (b"<", b"{", b"["):
                return False
        finally:
            try:
                await r.aclose()
            except Exception:
                _log.debug("_verify_rewritten_first_seg failed", exc_info=True)
                pass
        _SEG_OK_CACHE[real] = now
        if len(_SEG_OK_CACHE) > 1500:
            for old in list(_SEG_OK_CACHE)[:700]:
                _SEG_OK_CACHE.pop(old, None)
        return True
    except Exception:
        _log.debug("_verify_rewritten_first_seg failed", exc_info=True)
        return True  # 探测本身异常：保守放行，避免误伤好线


async def _find_playable(picked: dict, request: Request,
                         skip_current: bool = False) -> dict | None:
    """并行探测候选线路，返回（按优先级排序后的）首个可用目标（故障切换用）。"""
    from app import _line_is_bad  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    cands = _ordered_candidates(picked)
    if skip_current:
        cands = [c for c in cands if c["line"] != picked.get("line")]
    if not cands:
        return None
    fresh = [c for c in cands if not _line_is_bad(c)] or cands
    fresh = fresh[:_PROBE_MAX_CANDIDATES]
    for i in range(0, len(fresh), 3):
        batch = fresh[i:i + 3]
        results = await asyncio.gather(
            *[_probe_target(c, request) for c in batch])
        for cand, ok in zip(batch, results):
            if ok:
                return cand
    return None


# 内置线路全死后，按片名全网回源找到的可播记录：原 media_guid -> 真实可播 og。
# 让播放器后续仍用原 mg 请求时能命中新记录（跨同名记录播放）。
_MEDIA_REDIRECT: dict[str, str] = {}
_RESURRECT_FAIL: dict[str, float] = {}
_RESURRECT_FAIL_TTL = 10 * 60
_RESURRECT_FUT: dict[str, asyncio.Future] = {}  # 规范化片名 -> 后台回源任务（单飞）


async def _http_head_ok(url: str, headers: dict) -> bool:
    """不依赖 request 的首片段验活：Range 取 2KB，4xx/错误页/空体判死。"""
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        cli = app.state.ext
        req = cli.build_request(
            "GET", url, headers={**headers, "Range": _PROBE_RANGE})
        r = await cli.send(req, follow_redirects=True)
        try:
            if r.status_code >= 400:
                return False
            b = b""
            try:
                b = await r.aread()
            except Exception:
                _log.debug("_http_head_ok failed", exc_info=True)
                pass
            return bool(b) and b[:1] not in (b"<", b"{", b"[")
        finally:
            try:
                await r.aclose()
            except Exception:
                _log.debug("_http_head_ok failed", exc_info=True)
                pass
    except Exception:
        _log.debug("_http_head_ok failed", exc_info=True)
        return False


async def _resurrect_start(picked: dict, mg: str) -> str:
    """启动（或复用）按片名的后台回源任务。返回 'running' / 'ok' / 'fail'。"""
    title = (picked.get("title") or "").strip()
    nkey = re.sub(r"[\s\W_]+", "", title.lower())
    if not nkey:
        return "fail"
    if mg and mg in _MEDIA_REDIRECT:
        return "ok"
    if time.time() - _RESURRECT_FAIL.get(nkey, 0) < _RESURRECT_FAIL_TTL:
        return "fail"
    fut = _RESURRECT_FUT.get(nkey)
    if fut is None:
        fut = asyncio.ensure_future(_resurrect_worker(picked, mg, nkey))
        _RESURRECT_FUT[nkey] = fut

        def _drop(_f, k=nkey):
            _RESURRECT_FUT.pop(k, None)
        fut.add_done_callback(_drop)
    if fut.done() and not fut.cancelled():
        return "ok" if fut.result() else "fail"
    return "running"


async def _resurrect_worker(picked: dict, mg: str, nkey: str) -> bool:
    """后台按片名找真正能播的同名记录。两阶段：秒回直链源 → 加固源。"""
    from app import _aggregate_briefs, _resolve_if_needed, _self_url, app, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    title = (picked.get("title") or "").strip()
    want_idx = int(picked.get("index", 0) or 0)

    def match(b):
        return re.sub(r"[\s\W_]+", "", (b.get("title") or "").lower()) == nkey

    async def evaluate(briefs, direct_only):
        from app import _resolve_if_needed, _self_url, app, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        for b in briefs:
            if not match(b):
                continue
            og = b.get("guid")
            if not og:
                continue
            for ln, eps in (b.get("lines") or {}).items():
                if not eps:
                    continue
                ep = store.resolve_episode(og, ln, want_idx)
                if not ep or not ep.get("url"):
                    continue
                cand = dict(ep)
                if direct_only and not re.search(
                        r"\.(m3u8|mp4)(\?|$)", cand["url"], re.I):
                    continue
                # 解析到真实地址（加固源 detail+play），再验首片段
                try:
                    tgt = dict(cand, online_guid=og, kind="title")
                    await _resolve_if_needed(tgt, None)
                    if not tgt.get("url") or tgt.get("guard"):
                        continue
                    hdrs = tgt.get("headers") or {"User-Agent": "Mozilla/5.0"}
                    real = _self_url(tgt["url"])
                    is_hls = bool(tgt.get("m3u8")) or bool(
                        re.search(r"\.m3u8(\?|$)", real.split("?", 1)[0], re.I))
                    seg_url = real
                    if is_hls:
                        cur, seg_url = real, ""
                        for _d in range(3):  # 跟随嵌套 master 取首个分片
                            rr = await app.state.ext.get(
                                cur, headers=hdrs, timeout=15,
                                follow_redirects=True)
                            txt = rr.text
                            sl = [x.strip() for x in txt.splitlines()
                                  if x.strip() and not x.lstrip().startswith("#")]
                            if not sl:
                                break
                            if _is_media_playlist(txt):
                                seg_url = urllib.parse.urljoin(cur, sl[0])
                                break
                            cur = urllib.parse.urljoin(cur, sl[0])
                        if not seg_url:
                            continue
                    if not await _http_head_ok(seg_url, hdrs):
                        continue
                except Exception:
                    _log.debug("evaluate failed", exc_info=True)
                    continue
                # 命中：记录可播 og，并补一条可解析线路到该 og（_pick_episode 用）
                if mg:
                    _MEDIA_REDIRECT[mg] = og
                _log.info("内置线路全死，全网回源《%s》-> %s", title, ln)
                return True
        return False

    try:
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=10, fast_only=True), timeout=20)
        if await evaluate(briefs, True):
            return True
    except Exception:
        _log.debug("_resurrect_worker failed", exc_info=True)
        pass
    try:
        briefs = await asyncio.wait_for(
            _aggregate_briefs(title, maxn=10), timeout=30)
        if await evaluate(briefs, False):
            return True
    except Exception:
        _log.debug("_resurrect_worker failed", exc_info=True)
        pass
    _RESURRECT_FAIL[nkey] = time.time()
    return False



# ===== 完全自动选最快线 =====
_LINE_FAST_TTL = 600.0          # 最快线记忆 10 分钟
_LINE_FAST: dict[str, tuple[float, str]] = {}   # 集 key -> (ts, line)
_RACE_CONCURRENCY = 4


def _episode_key(picked: dict) -> str:
    return f'{picked.get("online_guid", "")}|{int(picked.get("index", 0) or 0)}'


def _remember_fast(picked: dict, line: str, latency: float | None = None) -> None:
    """记下竞速实测最快线（_LINE_FAST，turbo 实测结果为准）。

    冻结：只有竞速胜者能写 _LINE_FAST；预测结果绝不写这里。
    另把实测胜者同步给本片预测缓存（单向：turbo 实测 → linepredict，供下次
    竞速作优先种子；linepredict 绝不反向写 _LINE_FAST）。
    """
    if not line:
        return
    _LINE_FAST[_episode_key(picked)] = (time.time(), line)
    if len(_LINE_FAST) > 1000:
        for k in list(_LINE_FAST)[:500]:
            _LINE_FAST.pop(k, None)
    try:
        linepredict.remember(
            _episode_key(picked), line,
            float(latency) if latency is not None else 0.0)
    except Exception:
        _log.debug("_remember_fast failed: line=%s", line, exc_info=True)
        pass


def remembered_fast_line(picked: dict) -> str | None:
    """该集上一次竞速选出的最快线（未过期、且当前未被标记为坏线）。

    冻结：只读 turbo 竞速记忆 _LINE_FAST。刻意不搬上游的 linepredict.fastest
    回落——预测线只能作竞速候选排序种子（见 _race_candidates），绝不能不经
    竞速就被当作最快线复用。
    """
    from app import _line_is_bad  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    rec = _LINE_FAST.get(_episode_key(picked))
    if not rec:
        return None
    ts, line = rec
    if time.time() - ts >= _LINE_FAST_TTL:
        _LINE_FAST.pop(_episode_key(picked), None)
        return None
    if _line_is_bad({**picked, "line": line}):
        _LINE_FAST.pop(_episode_key(picked), None)
        return None
    return line


def _fastest_on() -> bool:
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        return bool(config.get().get("line_fastest", True))
    except Exception:
        _log.debug("_fastest_on failed", exc_info=True)
        return True


# --- 线路实测吞吐（KB/s，指数滑动平均） ------------------------------------
# 竞速只比"首字节延迟"，会挑中"响应快、吞吐烂"的线：实测同一部片里既有 250 KB/s
# 的线，也有 26 KB/s 的坑，延迟上看不出差别，播起来差 10 倍。播放时顺便把每条线
# 真实下片速度记下来（零额外流量），下次选线优先用真快的。
_LINE_SPEED: dict[str, tuple[float, float]] = {}   # line -> (KB/s, ts)


def _remember_speed(picked: dict | None, kbps: float) -> None:
    if not picked or not picked.get("line") or kbps <= 0:
        return
    k = str(picked["line"])
    old = _LINE_SPEED.get(k)
    val = kbps if old is None else old[0] * 0.6 + kbps * 0.4
    _LINE_SPEED[k] = (val, time.time())
    if len(_LINE_SPEED) > 800:
        for kk in list(_LINE_SPEED)[:400]:
            _LINE_SPEED.pop(kk, None)


def _line_speed(picked: dict) -> float:
    rec = _LINE_SPEED.get(str(picked.get("line", "")))
    if not rec:
        return 0.0
    if time.time() - rec[1] > 3600:      # 一小时前的观测不再作数
        return 0.0
    return rec[0]


_SPEED_PRIOR = 150.0   # 未知线路的先验吞吐（KB/s）：没测过的线当作"可能还行"
_SPEED_GOOD = 200.0    # 达到这个实测吞吐才值得直接单发复用，低于它就换线再找


def _line_score(cand: dict) -> float:
    """选线用的吞吐评分：测过的用实测值，没测过的给先验值。

    没测过的线不能排在"实测很差（如 60 KB/s）"的线后面——否则一次测到烂线就
    永远不再试别的线。先验 150 介于"已知很差"和"已知不错"之间。"""
    s = _line_speed(cand)
    return s if s > 0 else _SPEED_PRIOR


def _race_candidates(picked: dict, seed: str = "") -> list[dict]:
    """竞速候选：默认线/上次最快线在前，坏线排除（除非全坏），限量。"""
    from app import _episode_candidates, _line_is_bad, _line_score, config, remembered_fast_line, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    cands = _episode_candidates(picked)
    default = store.best_line(picked.get("online_guid", ""))
    fast = remembered_fast_line(picked)
    # 冻结适配（上游 linepredict 的降级形态）：预测线只作竞速优先种子——在吞吐
    # 打分相同、且非上次竞速最快线时排前一位。竞速仍全量跑（first-success wins
    # 不变），吞吐打分仍是第一排序键，预测线不进 _LINE_FAST、不替代竞速结果。
    # 上游的 _predict_order_lines（预测线直接置顶）刻意不搬。
    pred = None
    try:
        if bool(config.get().get("linepredict_enabled", True)):
            pred = linepredict.fastest(_episode_key(picked))
    except Exception:
        _log.debug("_race_candidates failed", exc_info=True)
        pred = None
    cands = [c for c in cands if not _line_is_bad(c)] or cands
    try:
        n = int(config.get().get("line_fastest_max", 6))
    except Exception:
        _log.debug("_race_candidates failed", exc_info=True)
        n = 6
    cands = cands[:max(2, n)]

    def _src_dead(c: dict) -> bool:
        # 源级取流熔断（上游 v0.2.70）：某资源站取流域名整片不可达时（搜索域可能
        # 仍正常），其所有线路在竞速里沉底，避免每部片都先撞长超时再换线。
        # 取流级阈值 5 次/3 分钟（level="stream"），与搜索级 5 次/3 分钟分开记。
        # 关键：熔断源只沉底、绝不排除——健康候选不足 n 时用它们补足，保证竞速总有
        # 候选，避免“源被熔断→候选只剩 1 条且恰好是死链→起播直接失败”。
        try:
            sk = sourcehealth.source_key_of(c.get("line"), c)
            return bool(sk) and sourcehealth.is_dead(sk, level="stream")
        except Exception:
            _log.debug("_src_dead failed", exc_info=True)
            return False

    def rank(c: dict) -> tuple:
        from app import _line_score  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return (_src_dead(c),                          # 熔断源沉底（仍在候选内）
                -round(_line_score(c), 1),           # 实测/先验吞吐高的线优先
                c["line"] != fast if fast else True,
                c["line"] != pred if pred else True,
                c["line"] != default if default else True,
                # 冻结：种子只作最后一级 tiebreak，绝不盖过吞吐打分/最快线/默认线。
                c["line"] != seed if seed else True)

    cands.sort(key=rank)
    return cands
async def _ensure_playable(picked: dict, request: Request,
                           auto: bool = False) -> dict:
    """取流前确保目标可播。

    - auto=True（播放器自动起播，非用户手动切线）且开启最快线：并行竞速选最快线。
    - 否则（用户手动选了某条线）：先探当前线，失败才记忆坏线并故障切换到首个可用线。
    返回应播放的 picked（可能已换成另一线路）。"""
    from app import _autofail_on, _fastest_on, _line_is_bad, _race_fastest, _resolve_if_needed  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if not picked or not picked.get("online_guid"):
        return picked
    if auto and _autofail_on() and _fastest_on():
        winner = await _race_fastest(picked, request)
        if winner is not None:
            return winner
        # 竞速全失败：退回常规故障切换（探当前线/换线）
    if not _autofail_on():
        await _resolve_if_needed(picked, request)
        return picked
    cur_bad = _line_is_bad(picked)
    if not cur_bad:
        if await _probe_target(picked, request):
            return picked
        _mark_line_bad(picked)
        _log.warning("线路不可用，自动换线: %s 《%s》%s",
                     picked.get("line"), picked.get("title"),
                     picked.get("ep_title"))
    good = await _find_playable(picked, request, skip_current=True)
    if good is not None:
        _log.info("自动切换到可用线路: %s -> %s", picked.get("line"), good.get("line"))
        return good
    return picked


async def _resolve_if_needed(picked: dict, request: Request) -> dict:
    """把播放页(/share/<id>)或 302 跳转地址解析成真实媒体地址（原地更新）。"""
    # >>> PRIVATE:CATVOD
    from app import _resolve_catvod, _resolve_dduo_with_fallback, _resolve_drpy, _resolve_guard, _resolve_spider, ext_client  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if features.CATVOD and picked and picked.get("catvod"):
        await _resolve_catvod(picked)
    # <<< PRIVATE:CATVOD
    # >>> PRIVATE:DRPY
    elif features.DRPY and picked and picked.get("drpy"):
        await _resolve_drpy(picked)
    # <<< PRIVATE:DRPY
    # >>> PRIVATE:GUARD
    elif features.GUARD and picked and picked.get("guard"):
        await _resolve_guard(picked)
    # <<< PRIVATE:GUARD
    # >>> PRIVATE:SPIDER
    elif features.GUARD and picked and picked.get("spider_src"):
        await _resolve_spider(picked)
    # <<< PRIVATE:SPIDER
    # >>> PRIVATE:DDUO
    elif picked and picked.get("xsrc") and features.DDUO:
        await _resolve_dduo_with_fallback(picked, request)
    # <<< PRIVATE:DDUO
    if picked and picked.get("redirect"):
        real = await providers.resolve_redirect(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["redirect"] = False
            picked["m3u8"] = bool(re.search(r"\.m3u8(\?|$)", real, re.I))
    if picked and picked.get("resolve"):
        real = await providers.resolve_share(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["resolve"] = False
    return picked


# ===== 起播速度优化：播放列表短时缓存/预热 =====
# 说明：分辨率（清晰度）探测模块已整体搁置（ffprobe/额外分片占用资源、慢源下不稳定），
# preset 线路菜单只显示线路名、不再追加分辨率；quality.py 仅保留 PNG 伪装分片解包用途。
# 后续有更优的识别方案再行接入。


# 改写后的媒体播放列表短时内存缓存，避免同一集起播/重试反复抓上游。
# 上游地址带时效签名，TTL 取短；只缓存成功结果。
_PLAYLIST_CACHE: dict[str, tuple[float, str]] = {}
_PLAYLIST_TTL = 120.0
# 单飞：同一「集+线路」只允许一次在途抓取，预热与真正取 main 共用同一个 Future
_PLAYLIST_FUT: dict[str, asyncio.Future] = {}

# 紧凑分片注册表：夸克/UC 转码 HLS 的签名分片 URL 极长（~600 字符，base64 后近千），
# 一部 90 分钟的片子上千分片会撑出 1MB+ 的清单，且每个分片 URL 超长，经外网网关
# （Lucky/nginx/fngateway）容易被长度/缓冲限制拦掉，播放器拿到清单却拉不到分片。
# 故把「清单ID + 序号 → 真实分片URL」留在服务端，清单里只给短 URL（~60 字符）。
_HLS_REG: dict[str, tuple[float, list[str]]] = {}
_HLS_REG_TTL = 3 * 3600.0
_HLS_LONG_URL = 320          # 分片 URL 超过该长度即走紧凑注册表
_HLS_REG_MAX = 4000


def _hls_reg_put(urls: list[str]) -> str:
    import secrets
    rid = secrets.token_hex(6)
    _HLS_REG[rid] = (time.time(), urls)
    if len(_HLS_REG) > _HLS_REG_MAX:
        for old in sorted(_HLS_REG, key=lambda k: _HLS_REG[k][0])[:len(_HLS_REG) - _HLS_REG_MAX]:
            _HLS_REG.pop(old, None)
    return rid


def _hls_reg_get(rid: str, i: int) -> str:
    ent = _HLS_REG.get(rid)
    if not ent:
        return ""
    ts, urls = ent
    if time.time() - ts > _HLS_REG_TTL:
        _HLS_REG.pop(rid, None)
        return ""
    return urls[i] if 0 <= i < len(urls) else ""

# 哨兵：该线路解析后实为 mp4 直链（非 HLS），调用方应 302 到 direct
_NOT_HLS = object()


def _playlist_cache_key(picked: dict) -> str:
    return f'{picked.get("online_guid","")}|{picked.get("line","")}|{int(picked.get("index",0) or 0)}'


async def _build_playlist_body(mg: str, picked: dict, request: Request):
    """抓取该线路的上游媒体列表，并把分片改写为经本代理的绝对地址。
    返回 (body_text, base_url)；失败返回 (None, None)。"""
    from app import _fetch_playlist, _resolve_if_needed, _seg_url, _self_url, b64e, ext_client  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    headers = picked["headers"]
    was_redirect = bool(picked.get("redirect"))
    await _resolve_if_needed(picked, request)
    src_url = _self_url(picked["url"])
    # 解析后若发现其实是 mp4 直链（wrapper 预判为 HLS 时的兜底），交由调用方 302
    if not re.search(r"\.m3u8(\?|$)", src_url, re.I) \
            and re.search(r"\.mp4(\?|$)", src_url, re.I):
        return _NOT_HLS, None
    text, cur = await _fetch_playlist(request, src_url, headers)
    # 网盘播放代理：原画直出 mp4 时回包不是清单（content-type video、二进制），
    # 判为非 HLS 交调用方 302 到 direct；转码档则返回正常清单。
    if text is not None and picked.get("pan") and not _is_media_playlist(text):
        return _NOT_HLS, None
    # 跳转型源站极慢且间歇抖动：解析后的真实地址抓取失败时，对原始跳转地址
    # 强制重新解析一次再抓，避免整列表 502。
    if text is None and was_redirect:
        from store import store as _store
        og = picked.get("online_guid")
        raw = _store.get_title_by_guid(og) if og else None
        jump = None
        if raw:
            for _ln, eps in (raw.get("lines") or {}).items():
                for e in eps:
                    if e.get("redirect"):
                        jump = e.get("url")
                        break
                if jump:
                    break
        if jump:
            fresh = await providers.resolve_redirect(
                ext_client(request), jump, (headers or {}).get("Referer", ""))
            if fresh:
                text, cur = await _fetch_playlist(request, fresh, headers)
                if text is not None:
                    picked["url"] = fresh
    if text is None:
        return None, None

    # m3u8 流层去广告（TVBox/OK影视 Pro rules 引擎 + 通用启发式），在改写分片前执行
    try:
        import adfilter as _adfilter
        if _adfilter.enabled():
            text, _ad_removed = _adfilter.filter_hls(text, src_url)
    except Exception:
        _log.info("hls adfilter failed", exc_info=True)

    sel_qs = ""
    if picked.get("line"):
        sel_qs = f'line={urllib.parse.quote(picked["line"])}&index={picked["index"]}'

    # AES-128 加密 HLS：部分移动/TV 原生播放器拉到密钥后无法正确解密（报 http error）。
    # 在服务端取一次密钥，改写每个分片的 IV/密钥参数，由 /seg 统一解密后回传纯 TS，
    # 并从输出列表删除 EXT-X-KEY，彻底规避端侧 AES 兼容差异。
    decrypt_key: bytes | None = None
    explicit_iv: bytes | None = None
    key_marker = next((ln for ln in text.splitlines()
                       if ln.strip().startswith("#EXT-X-KEY")
                       and "METHOD=AES-128" in ln), "")
    if key_marker:
        try:
            import quality as _quality
            parsed = await _quality._parse_hls_key(
                ext_client(request), text, cur, headers)
            if parsed:
                decrypt_key, explicit_iv = parsed
        except Exception as exc:
            _log.warning("hls key parse failed: error=%s", type(exc).__name__)
            decrypt_key = None

    media_seq = 0
    mm = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", text)
    if mm:
        media_seq = int(mm.group(1))
    seg_index = 0
    out_lines = []

    # 先收集所有分片绝对地址（与下方输出顺序一致：URI 标签按出现序，分片行按出现序）
    seg_urls: list[str] = []
    for raw0 in text.splitlines():
        ln0 = raw0.strip()
        if ln0.startswith("#"):
            if ln0.startswith("#EXT-X-MAP") or ln0.startswith("#EXT-X-KEY"):
                m0 = re.search(r'URI="([^"]+)"', ln0, re.I)
                if m0:
                    seg_urls.append(urllib.parse.urljoin(cur, m0.group(1)))
        elif ln0:
            seg_urls.append(urllib.parse.urljoin(cur, ln0))
    # 任一地址过长即整表走紧凑注册表（同表统一，避免混用）
    use_compact = any(len(u) > _HLS_LONG_URL for u in seg_urls)
    compact_rid = _hls_reg_put(seg_urls) if use_compact else ""

    def seg_decrypt_qs() -> str:
        from app import b64e  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        iv = explicit_iv if explicit_iv is not None else (
            (media_seq + seg_index).to_bytes(16, "big"))
        return f"dec=1&k={b64e(decrypt_key.hex())}&iv={b64e(iv.hex())}"

    compact_i = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY") and decrypt_key is not None:
            # 服务端已解密：客户端无需也不能再拿到密钥标签
            continue
        if line.startswith("#EXT-X-MAP") or \
                (line.startswith("#EXT-X-KEY") and 'URI="' in line):
            def repl(m):
                nonlocal compact_i
                uri = m.group(1)
                if compact_rid:
                    ref = f"hls?r={compact_rid}&i={compact_i}"
                else:
                    ref = _seg_url(mg, urllib.parse.urljoin(cur, uri), sel_qs)
                compact_i += 1
                return f'URI="{ref}"'
            raw = re.sub(r'URI="([^"]+)"', repl, line, flags=re.I)
            out_lines.append(raw)
        elif line.startswith("#") or not line:
            out_lines.append(raw)
        else:
            if compact_rid:
                out_lines.append(
                    f"hls?r={compact_rid}&i={compact_i}")
                compact_i += 1
            else:
                if decrypt_key is not None:
                    parts = [p for p in (sel_qs, seg_decrypt_qs()) if p]
                    extra = "&".join(parts)
                else:
                    extra = sel_qs
                out_lines.append(_seg_url(mg, urllib.parse.urljoin(cur, line), extra))
            seg_index += 1
    return "\n".join(out_lines) + "\n", cur


async def _get_playlist_cached(mg: str, picked: dict, request: Request):
    """取改写后的播放列表（内存缓存 + 单飞）。返回文本或 None。
    若该集/线路已有在途抓取（点击时预热启动），直接等同一个结果，不重复请求上游。"""
    from app import _build_playlist_body  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    key = _playlist_cache_key(picked)
    now = time.time()
    hit = _PLAYLIST_CACHE.get(key)
    if hit and now - hit[0] < _PLAYLIST_TTL:
        return hit[1]
    fut = _PLAYLIST_FUT.get(key)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        _PLAYLIST_FUT[key] = fut

        async def produce():
            from app import _build_playlist_body  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
            try:
                body, _base = await _build_playlist_body(mg, picked, request)
                if body is not None and body is not _NOT_HLS:
                    _PLAYLIST_CACHE[key] = (time.time(), body)
                    if len(_PLAYLIST_CACHE) > 600:
                        for k in list(_PLAYLIST_CACHE)[:300]:
                            _PLAYLIST_CACHE.pop(k, None)
                if not fut.done():
                    fut.set_result(body)
            except Exception as exc:
                if not fut.done():
                    fut.set_result(None)
                _log.info("playlist build failed: %s", exc)
            finally:
                _PLAYLIST_FUT.pop(key, None)

        asyncio.create_task(produce())
    return await fut


def _warm_playlist(mg: str, picked: dict, request: Request) -> None:
    """点击播放时后台预热默认线路播放列表（单飞，真正取 main 时并入同一任务）。"""
    from app import _get_playlist_cached  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if picked.get("t4"):
        return
    key = _playlist_cache_key(picked)
    hit = _PLAYLIST_CACHE.get(key)
    if hit and time.time() - hit[0] < _PLAYLIST_TTL:
        return
    if key in _PLAYLIST_FUT:
        return  # 已在途
    asyncio.create_task(_get_playlist_cached(mg, picked, request))


# ---- 真实时长（resume3-duration）----
# 背景：诊断定性客户端每次 record 都报 duration=0，且服务端 play/info /
# stream/list 也只给 duration=0，续播守门拿不到合法时长。
# 时长来源优先级：
#   1. CMS vod_duration（providers._map_vod 已取，标题级，见 inject.brief_duration）；
#   2. HLS playlist 的 EXTINF 求和（_fetch_playlist 已跟随 master 到媒体清单，
#      这里只对已取到的文本求和，不新增上游请求）；
#   3. history 里已存下的 record duration（客户端此前报过 >0 时复用）。
_EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)")
# stream/list 上无缓存时长时，等同一单飞 playlist 任务的上限（秒）：
# 超时/失败回退 0，不阻塞出线菜单、不阻塞播放。
_DURATION_PROBE_TIMEOUT = 2.0
# 单集时长超过 12 小时视为脏数据（与 providers.parse_duration 同口径）。
_DURATION_MAX_SECONDS = 43200


def _playlist_total_seconds(text) -> int:
    """媒体 playlist 的 EXTINF 求和（秒）。master 清单（只有
    EXT-X-STREAM-INF、没有 EXTINF）返回 0——变体清单由 _fetch_playlist
    在抓取时跟随，正常情况下到这里的已是媒体清单。"""
    if not text or not isinstance(text, str) or "#EXTINF" not in text:
        return 0
    total = 0.0
    for m in _EXTINF_RE.finditer(text):
        try:
            total += float(m.group(1))
        except ValueError:
            continue
    secs = int(round(total))
    return secs if 0 < secs <= _DURATION_MAX_SECONDS else 0


def _cached_playlist_seconds(picked: dict) -> int:
    """同步读 _PLAYLIST_CACHE 里已有的改写后清单并求和；无缓存返回 0，
    不发任何请求。"""
    try:
        hit = _PLAYLIST_CACHE.get(_playlist_cache_key(picked))
    except Exception:
        _log.debug("_cached_playlist_seconds failed", exc_info=True)
        return 0
    if not hit:
        return 0
    body = hit[1]
    if body is _NOT_HLS or not isinstance(body, str):
        return 0
    return _playlist_total_seconds(body)


def _remember_episode_duration(og: str, index: int, seconds: int) -> bool:
    """探测到的时长缓存到集记录上，随 titles.json 持久化（store 防抖落盘）。

    键：剧集 brief["ep_durations"][str(index)]；电影直接写
    brief["duration"]。只补缺：已有 CMS 时长 / 已缓存探测时长时不覆盖，
    避免不同线路的清单长度差互相覆盖。失效条件：标题被重新搜索入库
    （brief_of 重建会带上新的 CMS duration，ep_durations 随旧 brief
    一起被替换）或集数/线路变化导致 brief 重建；失败回退：返回 False，
    调用方继续用 0，不影响播放。"""
    from app import store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if not og or not seconds:
        return False
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return False
    if seconds <= 0 or seconds > _DURATION_MAX_SECONDS:
        return False
    try:
        brief = store.get_title_by_guid(og)
        if not brief:
            return False
        with store._lock:
            if inj.is_tv(brief):
                eps = brief.setdefault("ep_durations", {})
                key = str(int(index or 0))
                if eps.get(key):
                    return False
                eps[key] = seconds
            else:
                if brief.get("duration"):
                    return False
                brief["duration"] = seconds
            store._schedule_flush()
    except Exception:
        _log.debug("_remember_episode_duration failed", exc_info=True)
        return False
    return True


def _play_duration_for(ep: dict | None = None,
                       brief: dict | None = None) -> int:
    """play/info 返回的 duration：同步可得的来源按优先级取，
    均不发网络请求（playlist 只读已缓存的 _PLAYLIST_CACHE）。"""
    from app import history, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if ep is not None:
        dur = inj.episode_duration(ep, brief)
        if dur:
            return dur
        try:
            rec = history.get(ep["ep_guid"])
        except Exception:
            _log.debug("_play_duration_for failed: ep=%s", ep, exc_info=True)
            rec = None
        if rec:
            dur = int(rec.get("duration", 0) or 0)
            if dur:
                return dur
        try:
            target = store.get_episode_target(ep["ep_guid"])
        except Exception:
            _log.debug("_play_duration_for failed: ep=%s", ep, exc_info=True)
            target = None
        if target:
            dur = _cached_playlist_seconds(target)
            if dur:
                _remember_episode_duration(
                    ep.get("online_guid", ""), target.get("index", 0), dur)
                return dur
        return 0
    if brief is not None:
        dur = inj.brief_duration(brief)
        if dur:
            return dur
        try:
            rec = history.get(brief["guid"])
        except Exception:
            _log.debug("_play_duration_for failed: ep=%s", ep, exc_info=True)
            rec = None
        if rec:
            dur = int(rec.get("duration", 0) or 0)
            if dur:
                return dur
        try:
            line = store.best_line(brief["guid"])
        except Exception:
            _log.debug("_play_duration_for failed: ep=%s", ep, exc_info=True)
            line = None
        if line:
            dur = _cached_playlist_seconds(
                {"online_guid": brief["guid"], "line": line, "index": 0})
            if dur:
                _remember_episode_duration(brief["guid"], 0, dur)
                return dur
    return 0


def _attach_play_duration(info: dict, *, ep: dict | None = None,
                          brief: dict | None = None) -> dict:
    """给 play/info 返回体补上 duration：顶层 duration 与 item.duration
    两处都给（客户端读哪个字段未证实，两者都给；normalize_detail 不裁剪
    字段，直通）。无真实时长时保持原值（0），不编造。"""
    try:
        dur = _play_duration_for(ep, brief)
    except Exception:
        _log.debug("_attach_play_duration failed: ep=%s", ep, exc_info=True)
        return info
    if dur:
        info["duration"] = dur
        item = info.get("item")
        if isinstance(item, dict):
            item["duration"] = dur
            item["runtime"] = int(dur / 60)
    return info


# main 整条“解析+换线+建清单”的在途服务任务（mg|line|index -> Future）：慢源首个
# 请求只等到预算，超时回占位但后台任务继续跑完并写播放列表缓存，后续轮询直接命中。
# （上游 v1.0.0；键与上游一致：mg 是 URL 路径参数，index 为原值拼串。）
_MAIN_SERVE_FUT: dict[str, asyncio.Future] = {}
# 后台 _serve_normal 异常次数（bk -> 次数）：_serve_normal 体内无 try/except，
# 超时后等待者已拿到占位、引用丢弃，异常若无人取回只在 fut 被 GC 时打一条
# "Future exception was never retrieved"；done 回调取回后记在这里。
_MAIN_SERVE_BG_FAILS: dict[str, int] = {}


def _main_serve_fut_done(bk: str, fut: asyncio.Future):
    """_MAIN_SERVE_FUT 的 done 回调工厂。

    两件事，缺一不可：
    1. 取回异常：fut 若以异常结束，在此取回并记日志/统计（_MAIN_SERVE_BG_FAILS），
       不再等 GC 时才报 "Future exception was never retrieved"。已超时的等待者
       早已拿到占位，这里只记日志，不把异常抛回给任何人。
    2. 仅当字典里仍是同一个 future 才清理：窄窗口下若同键已建新 fut，无条件
       pop 会删掉新 fut，致单飞失效、后台重复启动昂贵解析。
    """
    def _drop(done: asyncio.Future) -> None:
        if not done.cancelled():
            exc = done.exception()
            if exc is not None:
                _MAIN_SERVE_BG_FAILS[bk] = _MAIN_SERVE_BG_FAILS.get(bk, 0) + 1
                _log.warning("慢线后台取流失败 %s: %s", bk, exc, exc_info=exc)
        if _MAIN_SERVE_FUT.get(bk) is fut:
            _MAIN_SERVE_FUT.pop(bk, None)
    return _drop
# 仅【慢源】（加固/爬虫/drpy/专有解码/小雅，常需十几秒解析）才用短预算先占位；普通
# CMS 直链不用预算、直接等出正片（恢复秒播），只在真正抓不到时才回占位。
# 配置 key：main_ready_budget（DEFAULTS 默认 2.4）；每次取 main 时经
# _main_slow_budget() 实时读取并夹取 [0.8, 2.8]，POST 改值即时生效，无需重启。
def _main_slow_budget() -> float:
    """慢线起播预算（秒）：每次实时读配置并夹取 [0.8, 2.8]，非法值回落 2.4。"""
    from app import config  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    try:
        return config.main_ready_budget()
    except Exception:
        _log.debug("_main_slow_budget failed", exc_info=True)
        return 2.4


def _is_slow_line(p: dict) -> bool:
    """是否为解析很慢的非标准源（加固/爬虫/drpy/专有解码/小雅），需先占位后台自愈。
    普通 CMS 直链（暴风/量子等 m3u8）返回 False，走直接秒播。（上游 v0.2.70）"""
    if not isinstance(p, dict):
        return False
    if p.get("guard") or p.get("spider_src") or p.get("drpy") \
            or p.get("catvod"):
        return True
    if p.get("xsrc"):
        return True
    ln = str(p.get("line") or "")
    return ln.startswith(("guard-", "spider-", "drpy-"))


@hls_router.get(f"{ONLINE_PREFIX}/{{mg}}/preset.m3u8")
async def hls_preset(mg: str, request: Request):
    from app import _await_pending_chart, _chart_key_open, _denied_lib_response, _dodge_known_bad, _fastest_on, _online_target_viewable, _pan_match, _pick_episode, _viewer_ctx, app, chart_guid_by_media, remembered_fast_line, store  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    idx0 = int(request.query_params.get("index", 0) or 0)
    # direct/mp4 取流失败回跳入口：直接给占位 master，不再枚举可能同样失效的线路
    if request.query_params.get("ph") == "1":
        return _placeholder_master_response(idx0)
    failed_g = await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    if not picked:
        # P0-3：未绑定榜单片且榜单库打不开时不给占位 master，直接 -6（403）。
        chart_g = chart_guid_by_media(mg)
        if chart_g and not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response(status_code=403)
        # 线路未就绪：回占位 master，由 main(phwait) 继续等待并自动切正片；
        # 播放器先进入加载页，不弹“播放出错”、线路菜单不被锁死（上游 v0.2.65）。
        return _placeholder_master_response(idx0)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6（403）。
    if _online_target_viewable(request, picked, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response(status_code=403)
    # 起播即跳过已知坏线（用户显式选线除外）
    picked = _dodge_known_bad(picked, request)
    # 注意：此处不 await _resolve_if_needed —— preset 只输出线路变体菜单，
    # 播放页/跳转/专有解码延迟到该线路被选中取 main 时再做，避免拖慢首屏。
    # play/play 选定线路后会带 line/index；此时只输出该线路，避免与清晰度菜单重复
    explicit_line = request.query_params.get("line")
    if explicit_line:
        # _pick_episode 已按 line/index 解析出该线路完整 target（含
        # redirect/resolve/xsrc 标志）；这里原样使用，真正解析推迟到取 main 时。
        targets = [picked]
    else:
        targets = store.target_lines(picked)
    # 起播速度优先：preset 只枚举候选线路，不在关键路径解析播放页/302/专有解码，
    # 那些动作延迟到该线路真正被选中取 main 时才做。
    hls_targets = []
    # 任何可按需解析的专有直链（小雅网盘 / JS / 加固等）都要进菜单，死活在 main 探测
    _SPECIAL = ("catv" + "od", "dr" + "py", "gu" + "ard")

    def _need_resolve(t: dict) -> bool:
        return bool(t.get("xsrc")) or any(t.get(k) for k in _SPECIAL)

    for t in targets:
        if t.get("m3u8") or re.search(r"\.(m3u8|mp4)(\?|$)", t.get("url", ""), re.I) \
                or t.get("resolve") or t.get("redirect") or _need_resolve(t):
            hls_targets.append(t)
    if not hls_targets:
        return Response("no hls line", status_code=404)

    # 分辨率探测模块已搁置：线路菜单只显示线路名，不追加分辨率标签。
    idx = int(picked.get("index", 0) or 0)

    # 自动起播：把上一次竞速选出的最快线排到首位（稳定用线）；用户显式选线时不动顺序
    # 冻结：最快线记忆仍是首要顺序；线路卡的种子只作次级 tiebreak，绝不盖过最快线。
    auto_mode = (not explicit_line) and _fastest_on()
    seed_line = request.query_params.get("seed_line") or ""
    if auto_mode:
        fast = remembered_fast_line(picked)
        if fast or seed_line:
            hls_targets.sort(key=lambda t: (
                t["line"] != fast if fast else True,
                t["line"] != seed_line if seed_line else True))
    else:
        # 手动/菜单模式：网盘线（高质量原画）排在在线线之前，便于直接选到网盘资源
        hls_targets.sort(key=lambda t: 0 if _pan_match(str(t.get("url") or ""))
                         else 1)

    def variant_uri(t: dict, is_auto_first: bool = False) -> str:
        q = {"index": idx, "line": t["line"]}
        # 仅播放器自动起播的首选变体带 auto=1（取 main 时触发竞速选最快线）；
        # 用户手动切到的其它变体不带，严格尊重其选择
        if is_auto_first:
            q["auto"] = "1"
            if seed_line:
                # 种子首变体不 pin line：main 见到 auto=1 且无显式 line 才会
                # 真正跑并行竞速（_race_fastest）；种子经 seed_line 参与候选排序。
                q.pop("line")
                q["seed_line"] = seed_line
        return "main.m3u8?" + urllib.parse.urlencode(q)

    if len(hls_targets) == 1:
        # 单线路：无 RESOLUTION 的单一变体（分辨率探测已搁置，不再写死占位分辨率）
        master = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
                  f"{variant_uri(hls_targets[0], auto_mode)}\n")
    else:
        # 多线路：无 RESOLUTION 的变体，飞牛 HLS 播放器呈现为可切换「线路」
        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for i, t in enumerate(hls_targets):
            name = _line_display_name(t)
            # NAME 供支持的播放器显示线路名；BANDWIDTH 差异化避免变体被合并
            out.append(f'#EXT-X-STREAM-INF:BANDWIDTH={1000000 + i * 100000},'
                       f'PROGRAM-ID=1,NAME="{name}"')
            # 仅首个（自动起播）变体带 auto=1
            out.append(variant_uri(t, auto_mode and i == 0))
        master = "\n".join(out) + "\n"
    return Response(master, media_type="application/vnd.apple.mpegurl")


_PROVIDER_CN = {
    "baofeng": "暴风", "ffzy": "非凡", "lzi": "量子", "wujin": "无尽",
    "youku": "优酷", "guangsu": "光速", "ruyi": "如意", "jisu": "极速",
    "xinlang": "新狼", "dytt": "电影天堂", "s360": "360",
}
_GROUP_CN = {"m3u8": "主线路", "yun": "云播", "ff": "非凡", "uk": "优酷",
             "gs": "光速", "bf": "暴风", "lz": "量子"}


def _friendly_line(key: str) -> str:
    """把 'provider::组名' 转成好认的线路名，如 baofeng::bfzym3u8 -> 暴风。"""
    provider, _, group = key.partition("::")
    pcn = _PROVIDER_CN.get(provider, provider)
    if not group:
        return pcn
    if group in _GROUP_CN:
        return f"{pcn}·{_GROUP_CN[group]}"
    # guard-/spider-/drpy- 等技术源：组名常是中文（如 🐻剧圈┃多线），取 ┃| 前可读部分
    if re.match(r"(guard|spider|drpy)-", provider, re.I):
        nice = re.sub(r"[┃|].*$", "", group).strip()
        nice = re.sub(r"^[\s\U0001F000-\U0001FAFF☀-➿]+", "", nice)
        if nice and not re.fullmatch(r"[A-Za-z0-9_-]+", nice):
            return nice
    stem = group
    for suf in ("zym3u8", "m3u8", "yun"):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
    stem = stem.upper() if stem.isascii() and stem else ""
    return f"{pcn}·{stem}" if stem else pcn


def _line_display_name(t: dict) -> str:
    """播放器线路菜单显示名：网盘线路加 ☁网盘名 前缀，便于区分高质量网盘资源。

    网盘线的来源多为 guard-Xxx 这类技术源名，直接展示不友好；改用线路组名里
    的中文可读部分（去掉 ┃多线 等装饰与 emoji 源前缀）。"""
    from app import _pan_match  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    line = t.get("line", "")
    url = str(t.get("url") or "")
    pv = _pan_match(url) if url else None
    if pv is not None:
        tag = pv.get("name", "网盘").replace("网盘", "")
        group = line.split("::", 1)[1] if "::" in line else ""
        nice = re.sub(r"[┃|].*$", "", group).strip(" 🐌🍓🐻🍉🛡️")
        # 去掉常见纯英文技术源尾巴
        nice = re.sub(r"(guard-|spider-|drpy-)", "", nice, flags=re.I).strip()
        return f"☁{tag} {nice}".strip() if nice else f"☁{tag} 网盘"
    return _friendly_line(line)


def _is_media_playlist(text: str) -> bool:
    return "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text


def _first_variant(text: str, base: str) -> str | None:
    """从 master 播放列表里取第一个变体（子 m3u8）的绝对地址。"""
    uri = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        uri = s
        break
    if not uri:
        return None
    return urllib.parse.urljoin(base, uri)


async def _fetch_playlist(request: Request, url: str, headers: dict,
                          tries: int = 3, timeout: float = 15.0):
    """抓取并跟随嵌套 master，返回最终媒体列表文本与其基准 URL。失败返回 (None,url)。"""
    # 网盘播放代理首次需转存+实时换链，给更宽裕的超时
    from app import ext_get_retry  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    if "/v/api/ext/pan/" in url and timeout <= 15.0:
        timeout = 45.0
    cur, text = url, None
    for _ in range(4):
        try:
            r = await ext_get_retry(request, cur, headers, tries=tries, timeout=15.0)
        except Exception as exc:
            # 抓取失败即整表失败，调用方据此换线；记异常类型，不记完整地址。
            _log.warning("playlist fetch failed: error=%s", type(exc).__name__)
            return None, cur
        if r.status_code != 200:
            return None, cur
        text = r.text
        if _is_media_playlist(text):
            break
        nxt = _first_variant(text, cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return text, cur


@hls_router.get(f"{ONLINE_PREFIX}/{{mg}}/main.m3u8")
async def hls_main(mg: str, request: Request):
    from app import _autofail_on, _await_pending_chart, _chart_key_open, _denied_lib_response, _dodge_known_bad, _ensure_playable, _get_playlist_cached, _main_slow_budget, _online_target_viewable, _pick_episode, _verify_rewritten_first_seg, _viewer_ctx, app, chart_guid_by_media  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    _req_t0 = time.time()  # hls_main 入口处、任何分支之前（上游 v1.0.0）

    def _budget_left(floor: float) -> float:
        # 首次 main 的总预算：从请求进入起计，含绑定等待、选线、dodge 等前置耗时。
        # 时钟用 time.time()（墙钟），与 _PLAYLIST_CACHE 的时间戳口径一致。
        # 预算每次实时读配置（_main_slow_budget），改值即时生效。
        from app import _main_slow_budget  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        return max(floor, _main_slow_budget() - (time.time() - _req_t0))

    phwait = request.query_params.get("phwait") == "1"
    failed_g = await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    if not picked:
        # P0-3：未绑定榜单片且榜单库打不开时不给占位，直接 -6（403）。
        chart_g = chart_guid_by_media(mg)
        if chart_g and not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response(status_code=403)
        # 占位自修复通道（上游 v0.2.65/v0.2.70）：只有显式 phwait 的准备通道保留占位，
        # 期间一律回占位 HLS（200），绝不返回 4xx/5xx 锁死菜单；普通 main 仍按
        # v0.2.42 不用无限占位掩盖“未找到片源”。
        if phwait:
            return _placeholder_playlist_response(mg)
        if failed_g:
            return Response("暂无可用在线片源", status_code=503)
        return Response("not found", status_code=404)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6（403）。
    if _online_target_viewable(request, picked, mg, _ctx=_viewer_ctx(request)) is False:
        return _denied_lib_response(status_code=403)

    explicit_line = request.query_params.get("line")
    auto_play = request.query_params.get("auto") == "1" and not explicit_line
    request.state._play_mg = mg  # 供探测复用播放列表构建、填充缓存
    # 起播即跳过已知坏线（用户显式选线除外），避免无谓探测延迟
    # （与上游一致：dodge 在分发前，不在 _serve_normal 内。）
    picked = _dodge_known_bad(picked, request)

    async def _serve_normal(picked: dict):
        # 整条“解析+换线+建清单”服务逻辑（上游 v1.0.0 _serve_normal）。
        # 有意偏离：不含 auto 起播快路径——pick_faster_line（播放中静默换线）与
        # _cached_sibling_playlist 是已冻结、明确不接线的功能，本次不搬。
        # 线路健康探测 + 自动选最快线
        from app import _autofail_on, _ensure_playable, _get_playlist_cached, _pick_episode, _verify_rewritten_first_seg  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        if _autofail_on():
            picked = await _ensure_playable(picked, request, auto=auto_play)
            # 若自动换到了别的线路，后续 _pick_episode 仍按 query 的旧线解析；用换好的目标
            request.state._failover_picked = picked

        # >>> PRIVATE:CATVOD
        from app import _resolve_catvod  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
        # 小雅网盘文件（mkv/ts 等）非 HLS：解析出签名直链后直接 302，让客户端直连网盘 CDN
        if features.CATVOD and picked.get("catvod"):
            if await _resolve_catvod(picked):
                # 再探一次死活，小雅“文件不存在”会回 200+JSON 假活；坏则换线
                if not (await _probe_target(picked, request)):
                    _mark_line_bad(picked)
                    alt = await _find_playable(picked, request, skip_current=True)
                    if alt is not None:
                        picked = alt
                        request.state._failover_picked = picked
                        _log.info("小雅文件不可用，自动切换线路 -> %s", picked.get("line"))
                    else:
                        return Response("该线路文件不存在，且暂无其他可用线路",
                                        status_code=502)
                if picked.get("catvod_direct"):
                    return RedirectResponse(picked["url"], status_code=302,
                                            headers={"Cache-Control": "no-store"})
        # <<< PRIVATE:CATVOD

        body = await _get_playlist_cached(mg, picked, request)
        if body is None:
            # 当前线抓取失败：记忆并自动换一条已探测可用的线路重试一次
            if _autofail_on():
                _mark_line_bad(picked)
                alt = await _find_playable(picked, request, skip_current=True)
                if alt is not None:
                    _log.info("播放列表抓取失败，自动换线 %s -> %s",
                              picked.get("line"), alt.get("line"))
                    picked = alt
                    request.state._failover_picked = alt
                    # 小雅等非 HLS 直链
                    # >>> PRIVATE:CATVOD
                    from app import _resolve_catvod  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
                    if features.CATVOD and picked.get("catvod"):
                        if await _resolve_catvod(picked) and picked.get("catvod_direct"):
                            return RedirectResponse(picked["url"], status_code=302,
                                                    headers={"Cache-Control": "no-store"})
                    # <<< PRIVATE:CATVOD
                    body = await _get_playlist_cached(mg, picked, request)
        if body is None:
            # 上游 v0.2.42/v0.2.70：普通 CMS 抓流失败直接报错，不返回假 HLS 造成无限占位；
            # 加固/爬虫等慢源仍保留占位自愈通道（后台找线，下次轮询切正片）。
            if _is_slow_line(picked):
                return _placeholder_playlist_response(mg)
            return Response("fetch fail upstream", status_code=502)
        if body is _NOT_HLS:
            # 预判为 HLS、解析后实为 mp4：302 到直链代理（direct 复用已解析结果）
            q = urllib.parse.urlencode({"line": picked["line"], "index": picked["index"]})
            return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/direct?{q}", status_code=302)
        # 真实时长（resume3-duration）：手头已是最终媒体清单，EXTINF 求和
        # 后缓存到集记录上，供下次 play/info / stream/list / play/list
        # 直接用，不新增请求；失败（0）不影响播放。
        _remember_episode_duration(
            picked.get("online_guid", ""), picked.get("index", 0),
            _playlist_total_seconds(body))
        # 清单活、分片死（如某源 CDN 删流）：先在已解析清单上验首个分片，失败则
        # 标记坏线，并启动「按片名全网回源」后台任务。回源较慢，首个请求不阻塞到
        # 网关超时，而是让客户端（本就每 3 秒重试）在后台找到可播源后的下一次请求
        # 经 _MEDIA_REDIRECT 命中新记录。
        if _autofail_on() and not await _verify_rewritten_first_seg(body, request, picked):
            _mark_line_bad(picked)
            status = await _resurrect_start(picked, mg)
            if status == "ok":
                alt = _pick_episode(mg, request)
                if alt:
                    body2 = await _get_playlist_cached(mg, alt, request)
                    if body2 and body2 is not _NOT_HLS:
                        return Response(body2, media_type="application/vnd.apple.mpegurl",
                                        headers={"Cache-Control": "no-cache",
                                                 "Access-Control-Allow-Origin": "*"})
            if status == "fail":
                return Response("暂无可播线路（该源分片已失效，且未找到其他可用源）",
                                status_code=502)
            # running：慢源回直播式占位（200），播放器周期重拉 main 即转正，不弹错；
            # 普通源仍让客户端 3 秒后重试，不返回占位视频（上游 v0.2.42）。
            if _is_slow_line(picked):
                return _placeholder_playlist_response(mg)
            # running：让客户端稍候重试，不返回错误清单
            return Response("正在为你寻找可用线路…", status_code=503,
                            headers={"Retry-After": "3",
                                     "Access-Control-Allow-Origin": "*"})
        _sk = sourcehealth.source_key_of(picked.get("line"), picked)
        if _sk:
            # 播出成功：取流域复活（只清取流级计数，搜索级计数不动）。
            sourcehealth.record(_sk, True, level="stream")
        return Response(body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache",
                                 "Access-Control-Allow-Origin": "*"})

    # 起播策略（上游 v1.0.0）：
    #  - 普通 CMS 直链：同步等到真清单，每次 main 独立执行，不做外层单飞合并，
    #    否则前一次探测卡住时，后续重试全部挂在同一个 _serve_normal 上。
    #  - 仅加固/爬虫/drpy/专有解码/小雅这类慢源：短预算到点先回占位、后台自愈，
    #    播放器周期重拉转正片。
    slow_source = _is_slow_line(picked)
    if not slow_source:
        # v0.2.42：普通 CMS 每次 main 独立执行。不要在外层再做单飞合并，
        # 否则前一次探测卡住时，后续重试全部挂在同一个 _serve_normal 上。
        return await _serve_normal(picked)
    # 仅慢源保留单飞与短预算自愈，避免后台重复启动昂贵转存/爬虫任务。
    bk = f"{mg}|{picked.get('line')}|{picked.get('index', 0)}"
    fut = _MAIN_SERVE_FUT.get(bk)
    if fut is None:
        fut = asyncio.ensure_future(_serve_normal(picked))
        _MAIN_SERVE_FUT[bk] = fut
        fut.add_done_callback(_main_serve_fut_done(bk, fut))
    try:
        return await asyncio.wait_for(asyncio.shield(fut),
                                      timeout=_budget_left(floor=0.2))
    except asyncio.TimeoutError:
        return _placeholder_playlist_response(mg)


def _cors_preflight() -> Response:
    """播放路径的 CORS 预检：移动/TV 等跨域客户端在拉 m3u8/seg/key 前可能先发
    OPTIONS。必须由本服务直接应答标准 CORS 头，不能兜底透传给官方后端（后者不带
    CORS 头，会导致客户端判定跨域失败、报 http error）。"""
    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "3600",
        "Content-Length": "0",
    })


@hls_router.options(f"{ONLINE_PREFIX}/{{rest:path}}")
async def hls_cors_preflight(rest: str):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return _cors_preflight()


async def _try_stream_segment(request: Request, src_url: str, fwd: dict,
                              picked: dict | None = None):
    """把分片以「边下边传」方式直通；不适用时返回 None，交回整段缓冲老路径。

    为什么：源站实测只有 10~250 KB/s，而一个 3~6 秒的分片有 400 KB~1.9 MB。
    老实现 body = r.content 是**整段下完才回传**，播放器把"整片下载时间"
    （实测 1~12s）当成首字节等待，表现为长时间转圈不起播。改成流式后 TTFB
    等于上游首字节（约 0.3~1.5s），起播/续播都明显更快。

    回退（返回 None）的三种情况：上游 4xx/5xx 或可重试状态码（交回老路径做
    重试与坏线记忆）、分片带 PNG 外壳需整段剥壳、上游带 content-encoding
    （raw 流不能直接透传给客户端）。
    """
    from app import _safe_response_headers, ext_client  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    client = ext_client(request)
    # 连接超时必须短：源站偶尔 TCP 都连不上，等默认 30s 才回退会让播放器干等半分钟。
    # 读超时给宽松值——慢源是"能连上但很慢"，不能因此判死。
    tmo = httpx.Timeout(connect=8.0, read=90.0, write=30.0, pool=10.0)
    try:
        # 注意：httpx 的 send() 不接受 timeout 参数，超时必须挂在 request 上
        req = client.build_request("GET", src_url, headers=fwd, timeout=tmo)
        rs = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        _log.info("seg stream open fail %s，回退整段重试", type(exc).__name__)
        return None
    if rs.status_code >= 400:
        await rs.aclose()
        return None
    enc = (rs.headers.get("content-encoding") or "identity").lower()
    if enc not in ("", "identity"):
        await rs.aclose()
        return None
    it = rs.aiter_raw()
    try:
        first = await it.__anext__()
    except StopAsyncIteration:
        first = b""
    except Exception:  # noqa: BLE001
        _log.debug("_try_stream_segment failed", exc_info=True)
        await rs.aclose()
        return None
    if first[:8] == b"\x89PNG\r\n\x1a\n":
        # PNG 外壳分片必须整段还原，交回老路径处理
        await rs.aclose()
        return None
    h = _safe_response_headers(rs.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"
    cl = rs.headers.get("content-length")
    if cl:
        h["Content-Length"] = cl

    async def pump():
        nbytes = 0
        t0 = time.time()
        try:
            if first:
                nbytes += len(first)
                yield first
            async for chunk in it:
                nbytes += len(chunk)
                yield chunk
        finally:
            # 边下边播的同时把这条线的真实吞吐记下来（零额外流量）
            try:
                dt = time.time() - t0
                if nbytes and dt > 0.05:
                    _remember_speed(picked, nbytes / 1024.0 / dt)
            except Exception:
                _log.debug("pump failed", exc_info=True)
                pass
            await rs.aclose()

    return StreamingResponse(
        pump(), status_code=rs.status_code, headers=h,
        media_type=rs.headers.get("content-type") or "video/mp2t")


# ---- 线路未就绪占位视频（上游 v0.2.65；保持播放器存活、线路菜单可切，不弹“播放出错”）----
_PH_DIR = Path(__file__).resolve().parent / "assets" / "placeholder"
_PH_SEG_TS = "_placeholder/placeholder.ts"
_PH_SEG_DUR = 2.0
_PH_WIN = 3  # 直播式滑动窗口分片数（3×2s≈6s：缩短冷片占位后的转正重拉间隔）


def _placeholder_playlist_response(mg: str = "", live: bool = True) -> Response:
    """返回循环提示 HLS（HTTP 200）。飞牛对 4xx/5xx 清单会弹“播放出错”并锁死
    线路菜单；200 的有效 HLS 让播放器停在提示画面、菜单仍可切换。

    用直播式滑动窗口（无 ENDLIST、带 MEDIA-SEQUENCE）：客户端只预取窗口内少数分片
    并周期性重拉 main，后台转存/搜索完成后下一次轮询即可自动切回正片。分片用相对
    路径，相对 /{mg}/main.m3u8 解析为 /{mg}/_placeholder/placeholder.ts。"""
    seq = int(time.time() // int(_PH_SEG_DUR))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{int(_PH_SEG_DUR)}",
             f"#EXT-X-MEDIA-SEQUENCE:{seq}",
             "#EXT-X-DISCONTINUITY-SEQUENCE:0"]
    for i in range(_PH_WIN):
        lines.append(f"#EXTINF:{_PH_SEG_DUR:.3f},")
        lines.append(f"{_PH_SEG_TS}?n={seq + i}")
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "X-fnmedia-Placeholder": "1"})


def _placeholder_master_response(idx: int = 0) -> Response:
    """线路未就绪时的【master】占位（HTTP 200）。返回单变体，其 main 带 phwait=1，
    hls_main 见到该标记会持续返回占位媒体清单（不挑线路、不报 4xx/5xx），后台绑定/
    找线完成后由后续正常请求自动切回正片。保证任何情况下播放器都不弹“播放出错”、
    线路菜单不被锁死。"""
    q = urllib.parse.urlencode({"index": idx, "phwait": "1"})
    body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1000000,PROGRAM-ID=1,NAME="正在准备线路…"\n'
            f"main.m3u8?{q}\n")
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "X-fnmedia-Placeholder": "1"})


def _placeholder_ts_response() -> Response:
    p = _PH_DIR / "placeholder.ts"
    if not p.exists():
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="video/mp2t",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})


@hls_router.get(f"{ONLINE_PREFIX}/{{mg}}/_placeholder/placeholder.ts")
async def placeholder_ts(mg: str, request: Request):
    from app import app  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    return _placeholder_ts_response()


def _direct_to_placeholder(mg: str, request: Request) -> RedirectResponse:
    """直链(mp4)取流失败：302 回占位 master，播放器不弹错、线路菜单可手动切换。
    带 ph=1，preset 见此标记直接回占位（不再给同一条坏线，避免重定向环）。"""
    didx = int(request.query_params.get("index", 0) or 0)
    return RedirectResponse(
        f"{ONLINE_PREFIX}/{mg}/preset.m3u8?index={didx}&ph=1", status_code=302,
        headers={"Cache-Control": "no-store", "X-fnmedia-Placeholder": "1"})


@hls_router.get(f"{ONLINE_PREFIX}/{{mg}}/seg")
async def hls_seg(mg: str, request: Request):
    from app import _RETRY_STATUS, _await_pending_chart, _chart_key_open, _denied_lib_response, _online_target_viewable, _pick_episode, _safe_response_headers, _try_stream_segment, _viewer_ctx, app, b64d, chart_guid_by_media, ext_get_retry  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    u = request.query_params.get("u", "")
    try:
        src_url = b64d(u)
    except Exception:
        _log.debug("hls_seg failed", exc_info=True)
        return Response("bad u", status_code=400)
    await _await_pending_chart(mg)
    picked = _pick_episode(mg, request)
    # P0-3：已知在线标题但打不开任一归属库时直接 -6（403）；未解析出
    # 目标时沿用原有逻辑（与 item_detail 的季/集节点口径一致，淘汰后
    # 拒绝会误伤断点续播类直达；未绑定榜单片则只在榜单库关闭时拒绝）。
    if picked is not None:
        if _online_target_viewable(request, picked, mg, _ctx=_viewer_ctx(request)) is False:
            return _denied_lib_response(status_code=403)
    else:
        chart_g = chart_guid_by_media(mg)
        if chart_g and not _chart_key_open(request, chart_g, _ctx=_viewer_ctx(request)):
            return _denied_lib_response(status_code=403)
    headers = picked["headers"] if picked else {}
    # 透传 Range（播放器 seek）
    fwd = dict(headers)
    # >>> PRIVATE:GUARD
    # 网盘转码档（mkv 等不直出的源）的 HLS 分片在夸克/UC CDN 上，必须带对应网盘
    # 登录 Cookie（裸取 412）。picked 可能已脱离线路上下文，这里按 URL 域名兜底注入。
    try:
        _host = urllib.parse.urlsplit(src_url).netloc.lower()
        if _host.endswith("drive.quark.cn") or _host.endswith(".quark.cn"):
            import pan as _panq
            fwd["Cookie"] = _panq.auth_cookie_or_raise()
            fwd.setdefault("Referer", "https://pan.quark.cn/")
        elif _host.endswith(".uc.cn"):
            import panuc as _panu
            fwd["Cookie"] = _panu.auth_cookie_or_raise()
            fwd.setdefault("Referer", "https://drive.uc.cn/")
    except Exception:
        _log.debug("hls_seg failed", exc_info=True)
        pass
    # <<< PRIVATE:GUARD
    # 加密 HLS 由本服务解密：不能按密文转发任意字节 Range，需完整拉取后回传纯 TS。
    if request.query_params.get("dec") == "1":
        fwd.pop("Range", None)
    # >>> PRIVATE:DDUO
    if features.DDUO and picked and picked.get("xsrc"):
        # 多多解出的真实 CDN 鉴权直链：带外站 Referer 会 403
        fwd.pop("Referer", None)
    # <<< PRIVATE:DDUO
    if "range" in request.headers and request.query_params.get("dec") != "1":
        fwd["Range"] = request.headers["range"]
    # 起播速度优先：普通分片走流式直通（见 _try_stream_segment）。
    # 加密分片 dec=1 由本服务解密、Range 请求要按字节回传，两者仍走整段缓冲老路径。
    if request.query_params.get("dec") != "1":
        _streamed = await _try_stream_segment(request, src_url, fwd, picked)
        if _streamed is not None:
            return _streamed
    try:
        # 分片在播放中途持续拉取，对瞬时抖动必须重试，否则单次失败播放器直接报错
        r = await ext_get_retry(request, src_url, fwd, tries=4, timeout=30.0)
    except Exception as exc:
        _log.warning("seg fetch fail %s %s", type(exc).__name__,
                     urllib.parse.urlsplit(src_url).netloc)
        return Response(f"seg fail {type(exc).__name__}", status_code=502)
    body = r.content
    # 服务端解密 AES-128 HLS 分片：m3u8 已移除 EXT-X-KEY，这里把密文 TS 解成纯 TS，
    # 并强制按完整 200 内容返回（密文 206 不能直接解密/拼接）。
    decrypt_resp_status = r.status_code
    if request.query_params.get("dec") == "1":
        try:
            import quality as _quality
            key = bytes.fromhex(b64d(request.query_params.get("k", "")))
            ivhex = b64d(request.query_params.get("iv", ""))
            iv = bytes.fromhex(ivhex)
            if len(key) == 16 and len(iv) == 16:
                dec = await _quality._aes_cbc_decrypt(body, key, iv)
                if dec and len(dec) >= 188 and dec[0] == 0x47:
                    body = dec
                    decrypt_resp_status = 200
        except Exception as exc:
            _log.warning("seg decrypt fail: %s", type(exc).__name__)
    ctype = r.headers.get("content-type", "")
    unwrapped = False
    # 部分源把真实 TS 分片伪装成「1×1 PNG 头 + TS」。剥掉 PNG 外壳
    # 还原成纯 TS，飞牛播放器才能正常解码（通用兼容，对所有源生效）
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            import quality
            payload, _ = quality._unwrap_segment(body)
            if payload is not body:
                body = payload
                ctype = "video/mp2t"
                unwrapped = True
        except Exception:
            _log.debug("hls_seg failed", exc_info=True)
            pass
    drop = {"content-security-policy"}
    if request.query_params.get("dec") == "1":
        # 已在服务端解密并返回完整纯 TS：旧的 Range/content-range 必须移除，
        # 否则严格客户端会因 200 + Content-Range/Accept-Ranges 语义矛盾拒绝该分片。
        drop.update({"content-range", "accept-ranges"})
    if unwrapped:
        drop.add("content-type")
    h = _safe_response_headers(r.headers, drop)
    h["Access-Control-Allow-Origin"] = "*"
    h["Content-Length"] = str(len(body))
    if unwrapped or "content-type" not in {k.lower() for k in h}:
        h["Content-Type"] = ctype or "video/mp2t"
    if r.status_code in _RETRY_STATUS:
        _log.warning("seg upstream %s %s", r.status_code,
                     urllib.parse.urlsplit(src_url).netloc)
    return Response(content=body, status_code=decrypt_resp_status, headers=h)


@hls_router.get(f"{ONLINE_PREFIX}/{{mg}}/hls")
async def hls_compact(mg: str, request: Request):
    """紧凑分片：清单只带 r=<注册表ID>&i=<序号>，真实 CDN（夸克/UC 等）地址留
    在服务端。用于签名分片超长、清单过大的转码 HLS，规避外网网关 URL/体积限制。"""
    from app import _safe_response_headers, app, ext_get_retry  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    rid = request.query_params.get("r", "")
    try:
        idx = int(request.query_params.get("i", "0"))
    except ValueError:
        idx = 0
    src_url = _hls_reg_get(rid, idx)
    if not src_url:
        return Response("expired", status_code=410)
    fwd: dict = {}
    # 网盘 CDN 分片必须带登录 Cookie（裸取 412），按域名兜底注入
    # >>> PRIVATE:GUARD
    try:
        host = urllib.parse.urlsplit(src_url).netloc.lower()
        if host.endswith("drive.quark.cn") or host.endswith(".quark.cn"):
            import pan as _panq
            fwd["Cookie"] = _panq.auth_cookie_or_raise()
            fwd["Referer"] = "https://pan.quark.cn/"
        elif host.endswith(".uc.cn"):
            import panuc as _panu
            fwd["Cookie"] = _panu.auth_cookie_or_raise()
            fwd["Referer"] = "https://drive.uc.cn/"
    except Exception:
        _log.debug("hls_compact failed", exc_info=True)
        pass
    # <<< PRIVATE:GUARD
    if request.headers.get("range"):
        fwd["Range"] = request.headers["range"]
    try:
        r = await ext_get_retry(request, src_url, fwd, tries=4, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        _log.warning("hls compact seg fail %s", type(exc).__name__)
        return Response("seg fail", status_code=502)
    body = r.content
    ctype = r.headers.get("content-type", "")
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            import quality
            payload, _ = quality._unwrap_segment(body)
            if payload is not body:
                body, ctype = payload, "video/mp2t"
        except Exception:
            _log.debug("hls_compact failed", exc_info=True)
            pass
    h = _safe_response_headers(r.headers, {"content-security-policy"})
    h["Access-Control-Allow-Origin"] = "*"
    h["Content-Length"] = str(len(body))
    h.setdefault("Content-Type", ctype or "video/mp2t")
    return Response(content=body, status_code=r.status_code, headers=h)


# ------------------------------------------------------------- 海报代理

def _decode_tok(tok: str) -> str:
    from app import b64d  # noqa: E402,F401 延迟导入：避免与 app.py 循环导入
    tok = tok.split(".", 1)[0]
    return b64d(tok)
