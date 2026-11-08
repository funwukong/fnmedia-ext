#!/usr/bin/env python3
"""批量实测点播源：搜索可用性 + 播放地址直链率（强制不走 Clash 代理，国内直连）。"""
import json, re, time, urllib.parse
import httpx, asyncio

SOURCES = [
    ("暴风资源", "https://bfzyapi.com/api.php/provide/vod"),
    ("非凡资源", "https://cj.ffzyapi.com/api.php/provide/vod"),
    ("量子资源", "https://cj.lziapi.com/api.php/provide/vod"),
    ("无尽资源", "https://api.wujinapi.me/api.php/provide/vod"),
    ("优酷资源", "https://api.ukuapi88.com/api.php/provide/vod"),
    ("光速资源", "https://api.guangsuapi.com/api.php/provide/vod"),
    ("如意资源", "https://cj.rycjapi.com/api.php/provide/vod"),
    ("极速资源", "https://jszyapi.com/api.php/provide/vod"),
    ("新狼资源", "https://api.xinlangapi.com/xinlangapi.php/provide/vod"),
    ("电影天堂", "https://caiji.dyttzyapi.com/api.php/provide/vod"),
    ("360资源", "https://360zy.com/api.php/provide/vod"),
]
KW = "庆余年"
MEDIA_RE = re.compile(r"\.(m3u8|mp4)(\?|$)", re.I)

async def probe(client, name, url):
    api = url.rstrip("/") + "/at/json"
    row = {"name": name, "search": False, "n": 0, "direct": 0, "share": 0,
           "play_ok": 0, "play_tested": 0, "sample": "", "err": ""}
    try:
        r = await client.get(api, params={"ac": "detail", "wd": KW},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200:
            row["err"] = f"HTTP {r.status_code}"; return row
        j = r.json()
        lst = j.get("list") or []
        row["search"] = True; row["n"] = len(lst)
        checked = []
        for v in lst[:6]:
            pu = v.get("vod_play_url") or ""
            for grp in pu.split("$$$"):
                for ep in grp.split("#"):
                    if "$" not in ep: continue
                    t, u = ep.split("$", 1); u = u.strip()
                    if MEDIA_RE.search(u):
                        row["direct"] += 1
                        if len(checked) < 3: checked.append((t, u))
                    elif u.startswith("http"):
                        row["share"] += 1
        # 实测直链可播性（直连，带源站 Referer）
        root = urllib.parse.urlparse(api)
        referer = f"{root.scheme}://{root.netloc}/"
        for t, u in checked[:3]:
            row["play_tested"] += 1
            try:
                rr = await client.get(u, headers={"User-Agent": "Mozilla/5.0",
                                    "Referer": referer}, timeout=12,
                                    follow_redirects=True)
                ct = rr.headers.get("content-type", "")
                body = rr.text[:200] if "mpegurl" in ct or rr.status_code == 200 else ""
                ok = rr.status_code == 200 and ("mpegurl" in ct or "#EXTM3U" in body or ".mp4" in u)
                row["play_ok"] += 1 if ok else 0
                if ok and not row["sample"]:
                    row["sample"] = u[:80]
            except Exception:
                pass
    except Exception as e:
        row["err"] = type(e).__name__ + ":" + str(e)[:40]
    return row

async def main():
    async with httpx.AsyncClient(trust_env=False) as c:
        rows = await asyncio.gather(*[probe(c, n, u) for n, u in SOURCES])
    print(f"{'源':6} {'搜索':4} {'命中':4} {'直链':4} {'中转':4} {'可播/测':8} 样例/错误")
    for r in rows:
        flag = "✅" if (r["search"] and r["play_ok"] > 0) else ("🔍" if r["search"] else "❌")
        print(f"{flag}{r['name']:5} {str(r['search']):4} {r['n']:<4} {r['direct']:<4} "
              f"{r['share']:<4} {r['play_ok']}/{r['play_tested']:<6} {r['sample'] or r['err']}")

asyncio.run(main())
