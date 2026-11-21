import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
from config_export import export_config, preview_restore


def test_restore_preview_validates_hash_and_reports_changes():
    class C:
        def get(self): return {"a": 1, "admin_token": "secret"}
    doc = export_config(C())
    doc["config"]["a"] = 2
    import hashlib, json
    doc["sha256"] = hashlib.sha256(json.dumps(doc["config"], ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    out = preview_restore(C(), doc)
    assert out["ok"] and out["hash_valid"]
    assert out["changed_keys"] == ["a", "admin_token"]
    assert out["redacted_fields"] == 1
    assert out["apply"] is False


def test_restore_preview_rejects_tampered_document():
    class C:
        def get(self): return {"a": 1}
    doc = export_config(C()); doc["config"]["a"] = 99
    assert preview_restore(C(), doc) == {"ok": False, "reason": "hash_mismatch"}
