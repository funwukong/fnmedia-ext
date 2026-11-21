import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import tmdb_client

class Resp:
    def raise_for_status(self): pass
    def json(self): return {'results':[{'id':7,'title':'电影','original_title':'Film','original_language':'en','overview':'介绍','poster_path':'/p.jpg','backdrop_path':'/b.jpg','vote_average':8.1,'vote_count':100,'release_date':'2024-01-01'}]}
class Client:
    async def __aenter__(self): return self
    async def __aexit__(self,*a): pass
    async def get(self,*a,**k): return Resp()

def test_tmdb_lookup_is_opt_in_and_maps_metadata(monkeypatch):
    monkeypatch.setattr(tmdb_client.httpx,'AsyncClient',lambda **k: Client())
    out=asyncio.run(tmdb_client.lookup('电影',2024,api_key='secret'))
    assert out['tmdb_id']==7 and out['original_title']=='Film'
    assert out['poster'].endswith('/original/p.jpg')
    assert 'secret' not in repr(out)
