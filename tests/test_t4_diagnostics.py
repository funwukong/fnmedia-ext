import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import t4_diagnostics


def test_t4_diagnostic_reports_stages_without_sensitive_data(monkeypatch):
    class S:
        async def ensure_detail(self, client, guid, force=False):
            return {"lines": {"hg": [{"play_from":"hg","episode_key":"e1"}]}}
        async def resolve(self, client, target):
            return type("P", (), {"wrapper":"MP4"})()
    async def probe(client, source): return {"classes":[1],"list":[1]}
    monkeypatch.setattr(t4_diagnostics.t4, "probe", probe)
    out=asyncio.run(t4_diagnostics.diagnose(S(),None,{"id":"t4","name":"T4"},"g"))
    assert [x["stage"] for x in out["stages"]]==["connect","detail","episodes","play"]
    assert "token" not in repr(out) and "url" not in repr(out)
