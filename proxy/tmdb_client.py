"""Optional TMDB metadata client. Disabled by default and never used by search."""
from __future__ import annotations
import asyncio, time
import logging
_log = logging.getLogger("tmdb_client")
from collections import OrderedDict
import httpx

_CACHE=OrderedDict(); _INFLIGHT={}; _TTL=7*24*3600; _FAIL_TTL=600

def _key(title,year,kind): return f"{kind}:{title.strip().lower()}:{year or ''}"
def image_url(path,size='original'):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ''

async def lookup(title, year=None, kind='movie', *, api_key='', language='zh-CN', timeout=5, api_base='https://api.themoviedb.org/3'):
    if not api_key or not title: return None
    key=_key(title,year,kind); now=time.time(); hit=_CACHE.get(key)
    if hit and now-hit[0] < (hit[2] if hit[2] else _TTL): return hit[1]
    if key in _INFLIGHT: return await asyncio.shield(_INFLIGHT[key])
    async def run():
        try:
            endpoint='tv' if kind in ('tv','series') else 'movie'
            params={'api_key':api_key,'query':title,'language':language,'page':1}
            if year: params['first_air_date_year' if endpoint=='tv' else 'year']=str(year)
            async with httpx.AsyncClient(timeout=timeout,follow_redirects=True) as c:
                r=await c.get(api_base.rstrip('/')+'/search/'+endpoint,params=params)
                r.raise_for_status(); rows=(r.json() or {}).get('results') or []
            best=rows[0] if rows else None
            if not best: _CACHE[key]=(time.time(),None,_FAIL_TTL); return None
            data={'tmdb_id':best.get('id'),'original_title':best.get('original_title') or best.get('original_name'),
                  'original_language':best.get('original_language'),'overview':best.get('overview') or '',
                  'poster':image_url(best.get('poster_path')),'backdrop':image_url(best.get('backdrop_path')),
                  'vote_average':best.get('vote_average') or 0,'vote_count':best.get('vote_count') or 0,
                  'release_date':best.get('release_date') or best.get('first_air_date') or ''}
            _CACHE[key]=(time.time(),data,0); _CACHE.move_to_end(key)
            while len(_CACHE)>1000: _CACHE.popitem(last=False)
            return data
        except Exception:
            _log.debug("tmdb lookup failed", exc_info=True)
            _CACHE[key]=(time.time(),None,_FAIL_TTL); return None
    task=asyncio.create_task(run()); _INFLIGHT[key]=task
    try: return await task
    finally: _INFLIGHT.pop(key,None)
