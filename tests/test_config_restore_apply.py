from config_export import apply_restore, export_config, preview_restore


def test_apply_restore_requires_confirmation_and_preserves_token():
    class C:
        def __init__(self): self.v={"max_per_keyword":12,"admin_token":"local"}
        def get(self): return dict(self.v)
        def save(self,p): self.v.update(p)
    c=C(); doc=export_config(c); doc["config"]["max_per_keyword"]=20
    import hashlib,json
    doc["sha256"]=hashlib.sha256(json.dumps(doc["config"],ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert apply_restore(c,doc,confirm=False)["reason"]=="confirmation_required"
    out=apply_restore(c,doc,confirm=True)
    assert out["ok"] and c.v["max_per_keyword"]==20 and c.v["admin_token"]=="local"


def test_preview_reports_ignored_keys():
    class C:
        def get(self): return {"a":1}
    doc=export_config(C()); doc["config"]["unknown_secret"]=1
    import hashlib,json
    doc["sha256"]=hashlib.sha256(json.dumps(doc["config"],ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    out=preview_restore(C(),doc)
    assert "unknown_secret" in out["ignored_keys"]
