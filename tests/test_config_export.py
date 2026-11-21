import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
from config_export import export_config, redact


def test_redact_nested_secrets():
    x = redact({"password":"p", "sources":[{"cookie":"c","api":"https://x"}], "ok":1})
    assert x["password"] == "<REDACTED>"
    assert x["sources"][0]["cookie"] == "<REDACTED>"
    assert x["sources"][0]["api"] == "https://x"
    assert redact("http://user:pass@example.test:8080/p") == "http://<REDACTED>@example.test:8080/p"


def test_export_has_hash_and_no_sensitive_value():
    class C:
        def get(self): return {"admin_token":"secret-token", "source_priority":{"a":2}}
    out = export_config(C())
    assert out["config"]["admin_token"] == "<REDACTED>"
    assert "secret-token" not in repr(out)
    assert len(out["sha256"]) == 64
