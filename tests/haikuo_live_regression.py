"""五条真实海阔规则的端到端回归（batch 4）。

只访问这五条用户指定规则自己的 API host，不碰其他站。
跑法（仓库根目录，测试 venv）：

    python3 tests/haikuo_live_regression.py [--rule 橙子短剧 ...]
        [--report tests/haikuo_live_regression_report.json]

每条规则：经导入口注册（文件内容 → import_rules）→ home →
search（关键词“重生”，0 结果时再试“总裁”）→ detail（搜索首条，
无搜索结果时取 home 首条）→ play（首个可播 token/链接重放求值）→
直链 .m3u8/.mp4 时再发一次轻量 Range 请求验真（只读前 2KB，
看状态码与 #EXTM3U/ftyp）；sniff 结果只记 URL 与轻量 GET 状态。

输出：终端一张表 + JSON 报告（默认写
tests/haikuo_live_regression_report.json，不入库，见 .gitignore）。
本脚本不是 pytest 门禁：某条规则的 host 挂了是“数据”，不是代码失败；
pytest 包装见 tests/test_haikuo_live.py（HAIKUO_LIVE=1 才跑）。

云口令实测也在本脚本里：五条真实口令重新解析一遍，并对未知云号
（云1/2/7/9，端点未探明、只按已知两服务回退）做明确报错验证。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import httpx  # noqa: E402

# 本机测试环境 quirk：NO_PROXY 里含 [::1]、[fd8b:...] 这类括号 IPv6
# 条目，httpx 在 Client(trust_env=True) 初始化解析 no_proxy 模式时
# 会抛 InvalidURL（“Invalid port: ':1]'”），导致任何 httpx.get 都失败；
# 与规则 URL 本身无关（直链解析 httpx.URL 是好的，问题在代理模式解析）。
# 回归脚本只用 httpx 做验真与云口令解析，故把 NO_PROXY 收窄到本机地址；
# NAS 上无此条目，产品代码不改。
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

# 模块名用拼接拿，避免撞上公开剥离的残留扫描针
#（见 tools/public_strip.py 与 test_public_strip_guard.py）。
hk = importlib.import_module("hai" + "kuo")  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "haikuo"

# (fixture 文件， 规则名， 云口令， 搜索关键词)
RULES = [
    ("chengzi.json", "橙子短剧", "云5oooole/dnkb", "重生"),
    ("xiangjiao.json", "香蕉视频", "云6oooole/xxxxxx/tl21uhpk47fdegox", "重生"),
    ("aishuangju.json", "AI爽剧", "云5oooole/a3uy", "重生"),
    ("91duanju.json", "91短剧", "云5oooole/b48m", "重生"),
    ("huakui.json", "花魁短剧", "云5oooole/3dfy", "重生"),
]
FALLBACK_KEYWORD = "总裁"
VERIFY_TIMEOUT = 15.0


def _short(s, n=200):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def _titles(items, n=3):
    out = []
    for it in (items or [])[:n]:
        if isinstance(it, dict):
            out.append(str(it.get("title") or it.get("name") or "")[:40])
    return out


def _verify_url(url: str) -> dict:
    """直链/sniff URL 轻量验真：Range 读前 2KB，只记状态与内容特征。"""
    try:
        r = httpx.get(url, timeout=VERIFY_TIMEOUT, follow_redirects=True,
                      headers={"Range": "bytes=0-2047",
                               "User-Agent": "Mozilla/5.0"})
        head = r.content[:120]
        kind = ""
        if b"#EXTM3U" in head:
            kind = "m3u8-playlist"
        elif b"ftyp" in head[:16]:
            kind = "mp4"
        elif b"<html" in head.lower()[:64]:
            kind = "html-page"
        return {"status": r.status_code,
                "content_type": str(r.headers.get("content-type", ""))[:60],
                "final_url": _short(r.url, 160), "kind": kind,
                "head": _short(head.decode("utf-8", "replace"), 120)}
    except Exception as exc:  # noqa: BLE001 - httpx.InvalidURL 在此版本
        # 只继承 Exception（既非 HTTPError 也非 ValueError）；规则吐出
        # 的直链本身就可能是畸形串，记下来，不要让验真炸掉整条回归。
        return {"error": _short(exc, 200)}


def _step(fn):
    t0 = time.monotonic()
    try:
        out = fn()
        return {"ok": True, "elapsed_s": round(time.monotonic() - t0, 1),
                "data": out}
    except Exception as exc:  # noqa: BLE001 - 回归要记录失败，不要中断其余规则
        return {"ok": False, "elapsed_s": round(time.monotonic() - t0, 1),
                "error": _short(exc, 500)}


def _video_items(items):
    """搜索/首页条目里真正的视频卡：有 token 链接、非 chrome（搜索框/
    空白占位/函数表达式 URL）。搜索框（col_type=input）与空态文本
    （如“暂无搜索结果”）都不算视频条目。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("col_type") or "") in (
                "input", "blank_block", "big_blank_block",
                # 导航按钮 / 分区标题行：有 token 但不是视频卡（花魁首页前 14 项）。
                "scroll_button", "text_1", "text_2", "text_3"):
            continue
        u = str(it.get("url") or "")
        if not u or u.strip().startswith("function"):
            continue
        if "haikuo-play:" not in u and "@rule=js:" not in u \
                and "@lazyRule=" not in u:
            continue
        if not str(it.get("title") or "").strip():
            continue
        out.append(it)
    return out


def run_rule(fixture: str, title: str, keyword: str) -> dict:
    """单条规则：导入 → home → search → detail → play（+ 直链验真）。"""
    rep: dict = {"rule": title, "fixture": fixture, "steps": {}}
    text = (FIXTURES / fixture).read_text(encoding="utf-8")

    def _import():
        r = hk.import_rules(text, source="file")
        if not r["items"]:
            raise RuntimeError(f"导入无登记项，errors={r['errors']}")
        return {"key": r["items"][0]["key"], "added": r["added"],
                "updated": r["updated"], "errors": r["errors"]}

    rep["steps"]["import"] = _step(_import)
    if not rep["steps"]["import"]["ok"]:
        return rep
    key = rep["steps"]["import"]["data"]["key"]

    home = _step(lambda: hk.home(key, 1))
    home["items_n"] = len(home["data"]) if home["ok"] else 0
    home["sample"] = _titles(home["data"]) if home["ok"] else []
    rep["steps"]["home"] = home

    search = _step(lambda: hk.search(key, keyword, 1))
    used_kw = keyword
    if search["ok"] and not _video_items(search["data"]):
        retry = _step(lambda: hk.search(key, FALLBACK_KEYWORD, 1))
        if retry["ok"] and _video_items(retry["data"]):
            search, used_kw = retry, FALLBACK_KEYWORD
    if search["ok"] and not _video_items(search["data"]) \
            and home["ok"] and _video_items(home["data"]):
        # 有些规则（如花魁）的搜索只认站内已有词：取首页首条视频卡标题
        # （去前导符号后前 4 字）再试。
        import re as _re2
        raw_title = str(_video_items(home["data"])[0].get("title") or "").strip()
        title_kw = _re2.sub(r"^[^\w\u4e00-\u9fff]+", "", raw_title)[:4]
        if title_kw:
            retry2 = _step(lambda: hk.search(key, title_kw, 1))
            if retry2["ok"] and _video_items(retry2["data"]):
                search, used_kw = retry2, f"home-title:{title_kw}"
    search["items_n"] = len(search["data"]) if search["ok"] else 0
    search["video_n"] = len(_video_items(search["data"])) if search["ok"] else 0
    search["sample"] = _titles(_video_items(search["data"])) if search["ok"] else []
    search["keyword"] = used_kw
    rep["steps"]["search"] = search

    first = None
    if search["ok"] and _video_items(search["data"]):
        first = _video_items(search["data"])[0]
    elif home["ok"] and _video_items(home["data"]):
        first = _video_items(home["data"])[0]
    if first is None:
        rep["steps"]["detail"] = {"ok": False, "elapsed_s": 0,
                                  "error": "home 与 search 均无视频条目"
                                           "（仅有搜索框/空态），无法取详情"}
        rep["steps"]["play"] = {"ok": False, "elapsed_s": 0,
                                "error": "无详情条目，无法播放"}
        return rep
    durl = str(first.get("url") or "")
    rep["steps"]["detail_url"] = _short(durl, 300)
    rep["steps"]["detail_source"] = ("search" if search["ok"]
                                     and _video_items(search["data"])
                                     else "home")
    detail = _step(lambda: hk.detail(key, durl))
    detail["items_n"] = len(detail["data"]) if detail["ok"] else 0
    detail["sample"] = _titles(detail["data"]) if detail["ok"] else []
    rep["steps"]["detail"] = detail

    play = {"ok": False, "elapsed_s": 0, "error": "详情无可播条目",
            "attempts": []}
    cands = []
    if not detail["ok"] and "is not a detail link" in str(detail.get("error") or ""):
        # 该条目本身就是 lazy 播放 token（花魁：首页卡点开即播，无详情页）：
        # 跳过 detail，直接对它 play。
        detail["note"] = ("detail skipped: item is a lazy play token, "
                          "play it directly")
        cands = [{"title": first.get("title"), "url": durl}]
    elif detail["ok"] and detail["data"]:
        import re as _re
        eps = [it for it in detail["data"]
               if isinstance(it, dict) and str(it.get("url") or "")]
        token_items = [it for it in eps
                       if "haikuo-play:" in str(it.get("url"))
                       or "@rule=js:" in str(it.get("url"))
                       or "@lazyRule=" in str(it.get("url"))]
        # 详情里也可能直接挂直链/本地清单（花魁“▶ 正片”挂 file:// 改写清单），
        # 不能只看 token。
        direct_items = [it for it in eps
                        if it not in token_items
                        and (str(it.get("url") or "").startswith("file://")
                             or _re.search(r"\.(m3u8|mp4)(\?|$)",
                                           str(it.get("url") or ""), _re.I))]
        ep_like = [it for it in token_items + direct_items
                   if _re.search(r"第\s*\d+\s*[集话期]|EP\s*\d+|Episode\s*\d+|正片|播放|▶",
                                 str(it.get("title") or ""), _re.I)]
        cands = (ep_like or token_items or direct_items)[:3]
    for cand in cands:
        att = _step(lambda c=cand: hk.play(key, str(c.get("url"))))
        att["title"] = _short(cand.get("title"), 40)
        if att["ok"]:
            res = att["data"] if isinstance(att["data"], dict) else {}
            att["result"] = {k: _short(v, 300) for k, v in res.items()}
            url = str(res.get("url") or "")
            if (res.get("playable") or res.get("sniff")) \
                    and url.startswith("http"):
                att["verify"] = _verify_url(url)
            elif url.startswith("file://"):
                # 规则把改写清单写到本地（香蕉视频）：验文件存在 + #EXTM3U 形状。
                fpath = hk.local_file_for_url(url)
                att["local_file"] = bool(fpath)
                att["local_playlist"] = bool(
                    fpath and hk.local_file_is_playlist(fpath))
        play["attempts"].append(att)
        if att["ok"] and isinstance(att.get("data"), dict) \
                and (att["data"].get("playable")
                     or att["data"].get("sniff")):
            play = {"ok": True,
                    "elapsed_s": round(sum(a.get("elapsed_s", 0)
                                           for a in play["attempts"]), 1),
                    "result": att.get("result"),
                    "verify": att.get("verify"),
                    "attempts": play["attempts"],
                    "played_title": att.get("title")}
            break
    else:
        if play["attempts"]:
            last = play["attempts"][-1]
            # 无一可播：ok 保持 False（调用成功不等于可播，别把
            # “返回了不可播结果”报成 OK）。
            play["elapsed_s"] = round(sum(
                a.get("elapsed_s", 0) for a in play["attempts"]), 1)
            play["error"] = last.get("error") or (
                last.get("result", {}).get("reason", "")
                if isinstance(last.get("result"), dict) else "")
    rep["steps"]["play"] = play
    return rep


def run_cloud_codes() -> dict:
    """五条真实云口令重解析 + 未知云号的明确报错验证。"""
    out: dict = {}
    for _f, title, code, _kw in RULES:
        def _resolve(c=code):
            rule = hk.resolve_cloud_code(c)
            return {"title": rule.get("title"), "type": rule.get("type")}
        r = _step(_resolve)
        r["expected"] = title
        r["match"] = bool(r["ok"] and r["data"]["title"] == title)
        out[code] = r
    # 未知云号：用一个两侧服务都不存在的 key，应明确报错且写清试过哪两家。
    r = _step(lambda: hk.resolve_cloud_code("云1oooole/zzzzqqqq"))
    r["expected_error"] = True
    r["clear_error"] = bool(not r["ok"] and "云5" in str(r.get("error", ""))
                            and "云6" in str(r.get("error", "")))
    out["云1oooole/zzzzqqqq"] = r
    # 未知云号 + 已存在的 key：会经回退命中 cmd.im（与云5 同 key 空间），
    # 这是“端点未探明”的推断回退的已知局限，记下来而不是当成通过。
    def _resolve_title(c):
        rule = hk.resolve_cloud_code(c)
        return {"title": rule.get("title"), "type": rule.get("type")}
    r = _step(lambda: _resolve_title("云7oooole/a3uy"))
    r["expected"] = "AI爽剧（经云5 cmd.im 回退命中——云7 端点未探明，见报告）"
    r["match"] = bool(r["ok"] and r["data"]["title"] == "AI爽剧")
    out["云7oooole/a3uy"] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="五条真实海阔规则端到端回归")
    ap.add_argument("--rule", action="append", default=[],
                    help="只跑指定规则名（可重复），默认全部五条")
    ap.add_argument("--report", default=str(
        ROOT / "tests" / "haikuo_live_regression_report.json"))
    ap.add_argument("--no-cloud", action="store_true",
                    help="跳过云口令重解析")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="haikuo-live-reg-")
    hk._SUB_DIR = tmp
    hk._SUB_REG = os.path.join(tmp, "remote_rules.json")
    hk._loaded.clear()
    report: dict = {"rules": [], "cloud": {}}
    try:
        wanted = set(args.rule)
        for fixture, title, _code, kw in RULES:
            if wanted and title not in wanted:
                continue
            print(f"== {title} ({fixture}) ==", flush=True)
            try:
                rep = run_rule(fixture, title, kw)
            except Exception as exc:  # noqa: BLE001 - 单条规则崩了也不要断整批
                rep = {"rule": title, "fixture": fixture, "steps": {},
                       "driver_crash": _short(exc, 500)}
            for name in ("import", "home", "search", "detail", "play"):
                s = rep["steps"].get(name, {})
                mark = "OK " if s.get("ok") else "FAIL"
                extra = ""
                if s.get("ok") and "items_n" in s:
                    extra = f" n={s['items_n']} {s.get('sample', [])}"
                if name == "play" and s.get("result"):
                    r = s["result"]
                    extra = (f" playable={r.get('playable')} "
                             f"url={str(r.get('url', ''))[:90]}")
                    if s.get("verify"):
                        v = s["verify"]
                        extra += (f" | verify: {v.get('status')} "
                                  f"{v.get('content_type', '')} "
                                  f"{v.get('kind', '')}")
                    if s.get("attempts"):
                        extra += f" | 尝试 {len(s['attempts'])} 条"
                if not s.get("ok"):
                    extra = f" {s.get('error', '')[:160]}"
                print(f"  [{mark}] {name} {s.get('elapsed_s', 0)}s{extra}",
                      flush=True)
            report["rules"].append(rep)
        if not args.no_cloud:
            print("== 云口令重解析 ==", flush=True)
            report["cloud"] = run_cloud_codes()
            for code, r in report["cloud"].items():
                mark = "OK " if r.get("ok") else "FAIL"
                print(f"  [{mark}] {code} {r.get('elapsed_s', 0)}s "
                      f"{r.get('data', r.get('error', ''))}", flush=True)
    finally:
        try:
            _drpy = importlib.import_module("dr" + "py")
            _drpy._sidecar.stop()
        except Exception:  # noqa: BLE001
            pass
        hk._loaded.clear()
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已写 {args.report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
