import asyncio, httpx, json

VIDS = ["BV1GJ411x7h7", "BV1ex411J7GE"]

async def main():
    H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
         "Referer": "https://www.bilibili.com/"}
    async with httpx.AsyncClient(headers=H, timeout=15, trust_env=False, follow_redirects=True) as c:
        for bvid in VIDS:
            r = await c.get("https://api.bilibili.com/x/player/pagelist", params={"bvid": bvid})
            pages = r.json().get("data") or []
            if not pages:
                print(bvid, "no pages"); continue
            cid = pages[0]["cid"]
            print(f"\n=== {bvid} pages={len(pages)} ===")
            variants = [
                ("fnval=1 qn=120 (合一MP4)", {"fnval": 1, "qn": 120, "fourk": 1, "fnver": 0}),
                ("fnval=1 qn=80", {"fnval": 1, "qn": 80, "fourk": 1, "fnver": 0}),
                ("fnval=1 qn=64", {"fnval": 1, "qn": 64, "fourk": 1, "fnver": 0}),
                ("fnval=1 qn=120 platform=html5", {"fnval": 1, "qn": 120, "fourk": 1, "fnver": 0, "platform": "html5"}),
                ("fnval=1 qn=120 high_quality", {"fnval": 1, "qn": 120, "fourk": 1, "fnver": 0, "high_quality": 1}),
                ("fnval=16 (DASH) qn=120", {"fnval": 16, "qn": 120, "fourk": 1, "fnver": 0}),
            ]
            for label, opts in variants:
                try:
                    rr = await c.get("https://api.bilibili.com/x/player/playurl",
                                     params={"bvid": bvid, "cid": cid, **opts})
                    j = rr.json(); d = j.get("data") or {}
                    durl = d.get("durl") or []
                    dash = d.get("dash") or {}
                    vids = dash.get("video") or []
                    print(f"  {label}: code={j.get('code')} quality={d.get('quality')} "
                          f"format={d.get('format')} durl={len(durl)} dash_v={len(vids)} "
                          f"dash_q={(vids[0].get('id') if vids else None)}")
                    if durl:
                        u = durl[0].get("url") or ""
                        print(f"     durl_host={u.split('/')[2] if '//' in u else ''} size={durl[0].get('size')}")
                except Exception as e:
                    print(f"  {label}: ERR {type(e).__name__}")

asyncio.run(main())
