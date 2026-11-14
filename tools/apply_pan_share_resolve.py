"""补齐【网盘分享链接 → 转存 → 播放】链路（对齐上游 app.py:8120-8166）。

上游完整版在播放解析里有这么一段：
    pv = _pan_match(raw) → 识别网盘家
    r = mod.resolve_share(share, pwd)   ← 转存，拿 items[{fid,...}]
    picked["url"] = "/v/api/ext/pan/{key}/play?fid=..."
    picked["pan"] = True
我们这边缺这一段 —— providers.resolve_share 只做「网页嗅探」，
对网盘分享链接必然失败，所以：
  · 网盘搜索结果（pansou_search 产出的 resolve=True 集）播不出来；
  · 从任何源拿到的网盘分享链接都播不出来；
  · 手动网盘线（PanSou 手动线）虽然能转存，但走的是另一条路。

本模块实现上游同款分支，并被 app._resolve_if_needed 调用。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
done: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    if not p.exists():
        print(f"  [miss] {tag}")
        return
    s = p.read_text(encoding="utf-8")
    if new in s:
        print(f"  [skip] {tag}")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    done.append(tag)
    print(f"  [ok] {tag}")


# ---------- 1) app.py：新增网盘分享解析函数 ----------
edit("proxy/app.py",
     """def _request_without_line(request: Request) -> Request:""",
     '''# >>> PRIVATE:PAN
async def _resolve_pan_share(picked: dict) -> bool:
    """网盘分享链接 → 转存 → 可播放地址（上游 app.py 同款分支）。

    上游（完整版 app.py:8120 起）就是这样做的：_pan_match 识别是哪家网盘
    → mod.resolve_share(share, pwd) 转存拿到 items[{fid,...}] → 把 picked.url
    换成本服务的 /v/api/ext/pan/{key}/play?fid=... 并标记 picked["pan"]=True。
    我们之前只有网页嗅探（providers.resolve_share），对网盘分享链接无效，
    导致「网盘结果在线路里播不出来」。

    返回 True 表示已解析成可播地址。
    """
    if not isinstance(picked, dict):
        return False
    raw = str(picked.get("url") or "")
    if not raw:
        return False
    import asyncio as _aio
    import urllib.parse as _up
    from panmatch import _PAN_LINKSTART_RE, _PAN_PROVIDERS, _pan_match

    pv = _pan_match(raw)
    share = ""
    if pv:
        m = pv["re"].search(raw)
        if not m:
            return False
        share = m.group(0)
        # 正则可能不含 scheme（pan.quark.cn 开头），parse_share 需要完整 URL。
        if share and not share.lower().startswith(("http://", "https://")):
            share = "https://" + share
    else:
        # 四盘聚合（Seedhub）中转：跟随 302 解真实链接。
        lm = _PAN_LINKSTART_RE.search(raw)
        if not lm:
            return False
        try:
            import pan as _panq
            resolved = await _aio.to_thread(_panq.resolve_linkstart, lm.group(1))
        except Exception:
            _log.debug("_resolve_pan_share linkstart failed", exc_info=True)
            return False
        if not resolved:
            picked["_pan_error"] = ("该资源是四盘聚合中转，本机无法自动解出真实网盘链接")
            return False
        pv = _pan_match(resolved) or _PAN_PROVIDERS[0]
        share = resolved

    if not pv or not pv.get("playable"):
        picked["_pan_error"] = ("该资源来自暂未支持的网盘家"
                                "（目前支持夸克 / UC / 阿里）")
        return False
    key = pv["key"]
    mod = None
    for _k, _m in (("quark", "pan"), ("uc", "panuc"), ("ali", "panali")):
        if _k != key:
            continue
        try:
            mod = __import__(_m)
        except Exception:
            mod = None
        break
    if mod is None:
        picked["_pan_error"] = "当前构建不含该网盘的播放模块"
        return False
    hint = _up.parse_qs(_up.urlparse(share).query).get("pwd", [""])[0]
    try:
        r = await _aio.to_thread(mod.resolve_share, share, hint)
    except Exception as exc:  # noqa: BLE001
        picked["_pan_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return False
    items = [x for x in (r.get("items") or []) if x.get("url")]
    if not items:
        picked["_pan_error"] = "分享内没有可播放的视频文件"
        return False
    try:
        idx = int(picked.get("index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    item = items[min(idx, len(items) - 1)]
    play_path = ("/v/api/ext/pan/play" if key == "quark"
                 else f"/v/api/ext/pan/{key}/play")
    picked["url"] = f"{play_path}?fid={item['fid']}"
    picked["resolve"] = False
    picked["pan"] = True
    picked["pan_provider"] = key
    picked["headers"] = {"User-Agent": "Mozilla/5.0"}
    picked["m3u8"] = False
    return True
# <<< PRIVATE:PAN


def _request_without_line(request: Request) -> Request:''',
     "app._resolve_pan_share")

# ---------- 2) hls_race.py：_resolve_if_needed 里接入网盘分支 ----------
edit("proxy/hls_race.py",
     """    if picked and picked.get("resolve"):
        real = await providers.resolve_share(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["resolve"] = False
    return picked""",
     """    if picked and picked.get("resolve"):
        # 网盘分享链接：先走转存分支（上游同款），失败再退回网页嗅探。
        # 顺序不能反 —— providers.resolve_share 只认网页播放页，对网盘分享
        # 链接必然抓不到 m3u8。
        _pan_done = False
        # >>> PRIVATE:PAN
        try:
            from app import _resolve_pan_share  # noqa: E402 延迟导入：避免与 app.py 循环导入
            _pan_done = await _resolve_pan_share(picked)
        except Exception:
            _log.debug("_resolve_pan_share failed", exc_info=True)
        # <<< PRIVATE:PAN
        if not _pan_done:
            real = await providers.resolve_share(
                ext_client(request), picked["url"],
                (picked.get("headers") or {}).get("Referer", ""))
            if real:
                picked["url"] = real
                picked["resolve"] = False
    return picked""",
     "hls_race._resolve_if_needed pan branch")

print()
print("done:", ", ".join(done) if done else "(none)")