import asyncio
import charts


def test_real_collector_passes_pages_and_refreshes_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(charts, 'STATE', tmp_path / 'charts.json')
    monkeypatch.setattr(charts, '_CHART_RECIPES', {
        'movie': [{'kind': 'tag', 'type': 'movie', 'tag': '热门', 'pages': 3}],
        'tv': [{'kind': 'tag', 'type': 'tv', 'tag': '最新'}],
    })
    calls = []
    async def fetch(client, typ, tag, *, pages=1):
        calls.append((typ, tag, pages))
        return [{'id': typ, 'title': 'test', 'cover': 'https://example.test/p.jpg'}]
    monkeypatch.setattr(charts, '_fetch_tag', fetch)
    cache = charts.ChartCache()
    result = asyncio.run(cache.refresh(force=True))
    assert sorted(calls) == [('movie', '热门', 3), ('tv', '最新', 1)]
    assert result['provider_total'] == result['provider_ok'] == 2
    assert result['last_error'] == ''
    assert cache.items('movie')[0]['title'] == 'test'
    assert (tmp_path / 'charts.json').is_file()
