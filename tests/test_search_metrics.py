import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import providers


def test_search_metric_aggregation_contains_no_queries_or_urls():
    providers._SEARCH_METRICS.clear()
    providers._SEARCH_METRICS_TOTAL.clear()
    providers.record_search_metric("source-a", status="success", duration_ms=25,
                                   result_count=3)
    providers.record_search_metric("source-a", status="failed", duration_ms=100)
    providers.record_search_metric("source-b", status="cancelled", duration_ms=500)
    snap = providers.search_metrics_snapshot()
    assert snap["sources"]["source-a"]["calls"] == 2
    assert snap["sources"]["source-a"]["success"] == 1
    assert snap["sources"]["source-a"]["failed"] == 1
    assert snap["sources"]["source-a"]["results"] == 3
    assert snap["sources"]["source-b"]["cancelled"] == 1
    text = repr(snap)
    assert "keyword" not in text and "query" not in text and "url" not in text
    providers.search_metrics_snapshot(reset=True)
    assert providers.search_metrics_snapshot()["sources"] == {}


def test_search_all_records_source_metrics(monkeypatch):
    async def fake_dispatch(client, source, keyword):
        if source["id"] == "bad":
            raise RuntimeError("secret query/url must not be recorded")
        return [object()]
    monkeypatch.setattr(providers, "_dispatch_search", fake_dispatch)
    providers.search_metrics_snapshot(reset=True)
    got = asyncio.run(providers.search_all(
        "private keyword", [{"id":"good","enabled":True},
                            {"id":"bad","enabled":True}], budget=None))
    assert len(got) == 1
    stats = providers.search_metrics_snapshot()["sources"]
    assert stats["good"]["success"] == 1 and stats["good"]["results"] == 1
    assert stats["bad"]["failed"] == 1
    assert "private keyword" not in repr(providers.search_metrics_snapshot())
    providers.search_metrics_snapshot(reset=True)
