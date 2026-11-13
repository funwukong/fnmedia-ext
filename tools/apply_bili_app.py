"""把 B 站独立浏览器接进 app.py（与海阔同一套模式）。幂等。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "proxy/app.py"
s = p.read_text(encoding="utf-8")
changed: list[str] = []


def rep(old: str, new: str, tag: str) -> None:
    global s
    if new in s:
        print(f"  [skip] {tag}")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到\n---\n{old[:400]}")
    s = s.replace(old, new, 1)
    changed.append(tag)
    print(f"  [ok] {tag}")


# 1) import + 实例化
rep(
    """from haikuo_browser import HaikuoBrowser, HaikuoBrowseError, LIB_GUID as HAIKUO_LIB_GUID, LIB_TITLE as HAIKUO_LIB_TITLE""",
    """from haikuo_browser import HaikuoBrowser, HaikuoBrowseError, LIB_GUID as HAIKUO_LIB_GUID, LIB_TITLE as HAIKUO_LIB_TITLE
# >>> PRIVATE:BILIBILI
from bilibili_browser import BilibiliBrowser, BilibiliBrowseError, LIB_GUID as BILI_LIB_GUID, LIB_TITLE as BILI_LIB_TITLE
# <<< PRIVATE:BILIBILI""",
    "1a import B 站浏览器",
)

rep(
    """haikuo_browser = HaikuoBrowser(store, config)""",
    """haikuo_browser = HaikuoBrowser(store, config)
# >>> PRIVATE:BILIBILI
bili_browser = BilibiliBrowser(store, config)
# <<< PRIVATE:BILIBILI""",
    "1b 实例化 B 站浏览器",
)

# 2) 媒体库列表：插入 B 站库卡
rep(
    """        # >>> PRIVATE:HAIKUO
        if haikuo_browser.sources() and _viewer_can_open_lib(request, HAIKUO_LIB_GUID):
            inserts.append(haikuo_browser.library())""",
    """        # >>> PRIVATE:BILIBILI
        if bili_browser.enabled() and _viewer_can_open_lib(request, BILI_LIB_GUID):
            inserts.append(bili_browser.library())
        # <<< PRIVATE:BILIBILI
        # >>> PRIVATE:HAIKUO
        if haikuo_browser.sources() and _viewer_can_open_lib(request, HAIKUO_LIB_GUID):
            inserts.append(haikuo_browser.library())""",
    "2 媒体库列表插入 B 站",
)

# 3) mdb_detail：B 站库元数据
rep(
    """    # >>> PRIVATE:HAIKUO
    if guid == HAIKUO_LIB_GUID:
        return JSONResponse({"code": 0, "msg": "", "data": haikuo_browser.library(detail=True)})""",
    """    # >>> PRIVATE:BILIBILI
    if guid == BILI_LIB_GUID:
        return JSONResponse({"code": 0, "msg": "",
                             "data": bili_browser.library(detail=True)})
    # <<< PRIVATE:BILIBILI
    # >>> PRIVATE:HAIKUO
    if guid == HAIKUO_LIB_GUID:
        return JSONResponse({"code": 0, "msg": "", "data": haikuo_browser.library(detail=True)})""",
    "3 mdb_detail B 站分支",
)

# 4) item_list：B 站浏览分支
rep(
    """    if aguid == HAIKUO_LIB_GUID or parent == HAIKUO_LIB_GUID or haikuo_browser.node(parent):""",
    """    # >>> PRIVATE:BILIBILI
    if aguid == BILI_LIB_GUID or parent == BILI_LIB_GUID or bili_browser.node(parent):
        started = time.monotonic()
        try:
            data = await asyncio.wait_for(
                bili_browser.listing(ext_client(request),
                                     parent or BILI_LIB_GUID, body),
                timeout=25)
        except asyncio.TimeoutError:
            _log.warning("bilibili browse timeout node=%s page=%s elapsed_ms=%d",
                         parent, body.get("page", 1),
                         int((time.monotonic() - started) * 1000))
            return JSONResponse({"code": -1, "msg": "B 站加载超时，请稍后重试",
                                 "data": None}, status_code=504)
        except BilibiliBrowseError as exc:
            _log.warning("bilibili browse failed: %s", exc)
            return JSONResponse({"code": -1, "msg": str(exc), "data": None},
                                status_code=502)
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # <<< PRIVATE:BILIBILI
    if aguid == HAIKUO_LIB_GUID or parent == HAIKUO_LIB_GUID or haikuo_browser.node(parent):""",
    "4 item_list B 站分支",
)

p.write_text(s, encoding="utf-8")
print()
print("changed:", ", ".join(changed) if changed else "(none)")
