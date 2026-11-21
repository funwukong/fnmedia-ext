import config as config_mod


def test_read_returns_copies_for_mutable_values(monkeypatch, tmp_path):
    state = tmp_path / "state" / "config.json"
    monkeypatch.setattr(config_mod, "_STATE", state)
    c = config_mod.Config()
    c.save({"lib_hidden": ["a"], "source_priority": {"cms": 4}})
    hidden = c.read("lib_hidden")
    priority, enabled = c.read_many("source_priority", "hot_library_enabled")
    hidden.append("b")
    priority["cms"] = 99
    assert c.read("lib_hidden") == ["a"]
    assert c.read("source_priority") == {"cms": 4}
    assert enabled is True
