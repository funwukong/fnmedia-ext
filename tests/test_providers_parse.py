"""苹果 CMS 解析：_parse_play / _map_vod，不发任何网络请求。"""
import providers

SRC = {"id": "test", "api": "https://example.com/api.php/provide/vod/at/json"}


def test_parse_play_direct_m3u8():
    out = providers._parse_play(
        "线路1", "", "第1集$https://cdn.example.com/a.m3u8#第2集$https://cdn.example.com/b.mp4",
        "https://example.com/",
    )
    assert list(out) == ["线路1"]
    eps = out["线路1"]
    assert len(eps) == 2
    assert eps[0].direct is True
    assert eps[0].url == "https://cdn.example.com/a.m3u8"
    assert eps[1].title == "第2集"


def test_parse_play_ignores_non_http_and_empty():
    out = providers._parse_play("线路1", "", "第1集$#第2集$https://cdn.example.com/b.m3u8", "")
    assert len(out["线路1"]) == 1


def test_parse_play_multiple_lines():
    out = providers._parse_play(
        "线路1$$$线路2", "", "第1集$https://a.example/1.m3u8$$$第1集$https://b.example/1.m3u8", ""
    )
    assert set(out) == {"线路1", "线路2"}


def test_map_vod_fields():
    v = {
        "vod_id": 123, "vod_name": " 测试剧 ", "vod_year": 2024,
        "vod_play_from": "线路1",
        "vod_play_url": "第1集$https://cdn.example.com/1.m3u8",
        "vod_pic": "https://cdn.example.com/cover.jpg",
        "vod_content": "简介",
    }
    t = providers._map_vod(v, SRC)
    assert t is not None
    assert t.title == "测试剧"
    assert t.vod_id == "123"
    assert t.online_id == "test:123"
    assert t.poster.endswith("cover.jpg")
    assert "线路1" in t.sources


def test_map_vod_no_play_url():
    t = providers._map_vod({"vod_id": 1, "vod_name": "无播放"}, SRC)
    assert t is not None
    assert t.sources == {}
