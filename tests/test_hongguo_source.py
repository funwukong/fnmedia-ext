import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import hongguo_source

class Resp:
    status_code = 200
    def __init__(self, data): self.data=data
    def json(self): return self.data
class Client:
    async def get(self, url, **kwargs):
        if url.endswith('/api/search'):
            return Resp({'items':[{'id':'s1','title':'短剧A','cover':'https://img/a.jpg'}]})
        if url.endswith('/api/detail'):
            return Resp({'id':'s1','title':'短剧A','lines':[{'flag':'hg','episodes':[{'id':'e1','name':'第1集'}]}]})
        return Resp({'service':'hongguo-web','ok':True})


def test_hongguo_search_and_detail_mapping():
    source={'id':'hg1','kind':'hongguo','api':'https://hg.example'}
    async def run():
        c=Client(); rows=await hongguo_source.search(c,source,'短剧A')
        detail=await hongguo_source.detail(c,source,'s1')
        return rows,detail
    rows,detail=asyncio.run(run())
    assert rows[0].provider == 'hg1' and rows[0].type_name == '短剧'
    assert detail.sources['hg'][0].direct
    assert '/api/stream/e1' in detail.sources['hg'][0].url
