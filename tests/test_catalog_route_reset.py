"""Catalog reset invalidates both memory and disk route state."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog


def test_reset_removes_route_snapshot(monkeypatch, tmp_path):
    state = tmp_path / "catalog_routes.json"
    state.write_text('{"routes": {"x": []}}', encoding="utf-8")
    monkeypatch.setattr(catalog, "ROUTE_STATE", state)
    cat = catalog.Catalog()
    cat._routes["x"] = []
    cat._route_state_loaded = True
    cat.reset()
    assert cat._routes == {}
    assert cat._route_state_loaded is False
    assert not state.exists()
