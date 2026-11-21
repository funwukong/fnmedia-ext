"""Configuration cleanup contracts for removed built-in sources."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import config as config_mod


def test_legacy_bilibili_source_is_removed_on_load(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "config_version": config_mod.CONFIG_VERSION,
        "sources": [{
            "id": "bilibili", "name": "哔哩哔哩",
            "api": "bilibili://home", "enabled": True,
            "standalone": True,
        }],
        "removed_ids": [],
    }), encoding="utf-8")
    monkeypatch.setattr(config_mod, "_STATE", state)
    cfg = config_mod.Config().get()
    assert all(src.get("id") != "bilibili" for src in cfg["sources"])
