import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import source_diagnostics


def test_safe_url_redacts_query_and_diagnosis_stages(monkeypatch):
    assert source_diagnostics._safe_url("https://user:pass@example.test/api?token=secret") == "https://user:pass@example.test/api"
    class Resp:
        status_code=200
        def json(self): return {"class":[{}],"list":[{"vod_id":"1"}]}
    class Client:
        async def get(self,*a,**k): return Resp()
    class CM:
        async def __aenter__(self): return Client()
        async def __aexit__(self,*a): pass
    monkeypatch.setattr(source_diagnostics.netutil,"async_client",lambda:CM())
    result=asyncio.run(source_diagnostics.diagnose_cms({"id":"s","name":"S","api":"https://x/api"},"片名"))
    assert [x["stage"] for x in result["stages"]]==["list","search","detail"]
    assert "片名" not in repr(result) and "token" not in repr(result)
