"""Regression tests for welfare category route deduplication."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog


def test_welfare_route_categories_are_unique(monkeypatch):
    monkeypatch.setattr(catalog, "_welfare_on", lambda: True)
    normal = catalog.CATS
    welfare = list(catalog.WELFARE_CATS)
    combined = list(dict.fromkeys(normal + welfare))
    assert len(combined) == len(set(combined))
    assert combined.count("domestic") == 1
    assert combined.count("jp_adult") == 1
    assert combined.count("adult") == 1


def test_welfare_disabled_does_not_add_welfare_routes(monkeypatch):
    monkeypatch.setattr(catalog, "_welfare_on", lambda: False)
    combined = list(dict.fromkeys(
        catalog.CATS + (list(catalog.WELFARE_CATS) if catalog._welfare_on() else [])))
    assert not set(catalog.WELFARE_CATS).intersection(combined)
