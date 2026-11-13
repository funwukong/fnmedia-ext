import asyncio,json
import httpx

async def main():
    headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'}
    async with httpx.AsyncClient(headers=headers,timeout=15,trust_env=False,follow_redirects=True) as c:
        queries=[('/x/player/pagelist',{'bvid':'BV1GJ411x7h7'}),('/x/web-interface/wbi/view',{'bvid':'BV1GJ411x7h7'}),('/x/web-interface/nav',{})]
        cid=0
        for path,params in queries:
            try:
                r=await c.get('https://api.bilibili.com'+path,params=params); j=r.json(); d=j.get('data')
                print(path,'http',r.status_code,'code',j.get('code'),'shape',type(d).__name__)
                if isinstance(d,list) and d: cid=d[0].get('cid',0); print('pages',len(d),'cid',cid)
                if path.endswith('view') and isinstance(d,dict): print('title',d.get('title'),'duration',d.get('duration'))
            except Exception as e: print(path,type(e).__name__)
        if cid:
            for opts in [{'fnval':2192,'qn':120,'fourk':1,'fnver':0},{'platform':'html5','fnval':129,'qn':120,'fourk':1,'high_quality':1}]:
                r=await c.get('https://api.bilibili.com/x/player/playurl',params={'bvid':'BV1GJ411x7h7','cid':cid,**opts})
                j=r.json(); d=j.get('data') or {}; dash=d.get('dash') or {}
                print('play http',r.status_code,'code',j.get('code'),'quality',d.get('quality'),'format',d.get('format'),'durl',len(d.get('durl') or []))
                print('video',[(x.get('id'),x.get('codecid'),x.get('codecs'),x.get('width'),x.get('height'),x.get('SegmentBase') or x.get('segment_base')) for x in dash.get('video',[])])
                print('audio',[(x.get('id'),x.get('codecs'),x.get('SegmentBase') or x.get('segment_base')) for x in dash.get('audio',[])])
                if dash.get('video'):
                    x=dash['video'][0]; u=x.get('baseUrl') or x.get('base_url');
                    r2=await c.get(u,headers={'Range':'bytes='+((x.get('SegmentBase') or {}).get('indexRange') or x.get('segment_base',{}).get('index_range','0-2047'))})
                    print('sidx probe',r2.status_code,r2.headers.get('content-type'),r2.headers.get('content-range'),len(r2.content),'sidx',b'sidx' in r2.content)
asyncio.run(main())
