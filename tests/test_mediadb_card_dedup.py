"""MediaDB card uniqueness regression tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app


def test_dedupe_media_cards_keeps_first_guid_and_order():
    cards = [
        {"guid": "catalog:domestic", "title": "国产系列"},
        {"guid": "catalog:domestic", "title": "国产系列重复"},
        {"guid": "catalog:europe_adult", "title": "欧美专区"},
        {"guid": "catalog:europe_adult", "title": "欧美专区重复"},
        {"title": "无guid卡片"},
    ]
    out = app._dedupe_media_cards(cards)
    assert [c.get("guid") for c in out] == [
        "catalog:domestic", "catalog:europe_adult", None
    ]
    assert out[0]["title"] == "国产系列"
    assert out[1]["title"] == "欧美专区"
