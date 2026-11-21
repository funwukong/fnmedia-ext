"""Redacted configuration export helpers."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from urllib.parse import urlsplit, urlunsplit

_log = logging.getLogger("fnmedia.config_export")

RESTORE_KEYS = {
    "max_per_keyword", "filter_junk", "junk_keywords", "online_first",
    "hot_library_enabled", "hot_library_title", "hot_limit", "hot_per_source",
    "hot_pages_per_source", "hot_refresh_interval", "catalog_library_enabled",
    "t4_library_enabled", "welfare_library_enabled", "source_priority",
    "prefer_quality_source", "auto_best_source", "line_fastest", "line_fastest_max",
    "line_fastest_timeout", "line_fastest_prefer", "line_autofail", "fixed_line_direct",
    "browse_probe", "poster_speed_badge", "skip_intro_enabled", "skip_intro_seconds",
    "skip_outro_enabled", "skip_outro_seconds", "main_ready_budget", "folder_bind_wait",
    "prewarm_mode", "pan_cache_cleanup_enabled", "pan_cache_ttl_hours",
    "pan_cache_ttl_days", "pansou_enabled", "pansou_url", "pansou_cloud_types",
    "pansou_src", "pansou_timeout", "pansou_limit", "update_proxy", "tmdb_enabled", "tmdb_language", "tmdb_timeout",
    "sources", "removed_ids", "standalone_sources",
}

SENSITIVE_KEYS = {
    "admin_token", "admin_token_previous", "password", "cookie", "cookies",
    "token", "refresh_token", "client_secret", "client_id", "api_key",
    "apikey", "secret", "authorization", "access_token", "user_cookie",
}


def redact(value):
    if isinstance(value, dict):
        return {k: "<REDACTED>" if str(k).lower() in SENSITIVE_KEYS
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and "@" in value:
        try:
            p = urlsplit(value)
            if p.username is not None or p.password is not None:
                host = p.hostname or ""
                if p.port:
                    host += f":{p.port}"
                return urlunsplit((p.scheme, "<REDACTED>@" + host,
                                   p.path, p.query, p.fragment))
        except ValueError:
            pass
    return copy.deepcopy(value)


def _canonical(config: dict) -> str:
    return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def export_config(config_obj) -> dict:
    raw = config_obj.get()
    clean = redact(raw)
    blob = _canonical(clean)
    return {"schema": 1, "exported_at": int(time.time()),
            "sha256": hashlib.sha256(blob.encode()).hexdigest(),
            "config": clean}


def preview_restore(config_obj, document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema") != 1:
        return {"ok": False, "reason": "unsupported_schema"}
    incoming = document.get("config")
    if not isinstance(incoming, dict):
        return {"ok": False, "reason": "invalid_config"}
    expected = hashlib.sha256(_canonical(incoming).encode()).hexdigest()
    if document.get("sha256") != expected:
        return {"ok": False, "reason": "hash_mismatch"}
    current = redact(config_obj.get())
    allowed = {k: v for k, v in incoming.items() if k in RESTORE_KEYS}
    changed = sorted(k for k in set(current) | set(allowed)
                     if current.get(k) != allowed.get(k))
    sensitive = sum(1 for v in incoming.values() if v == "<REDACTED>")
    preview = {"ok": True, "schema": 1, "hash_valid": True,
               "changed_keys": changed, "changed_count": len(changed),
               "ignored_keys": sorted(set(incoming) - RESTORE_KEYS),
               "redacted_fields": sensitive, "apply": False}
    preview["plan"] = {k: v for k, v in allowed.items()
                       if not _contains_redacted(v)}
    return preview


def _contains_redacted(value) -> bool:
    if value == "<REDACTED>":
        return True
    if isinstance(value, dict):
        return any(_contains_redacted(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_redacted(v) for v in value)
    return False


def apply_restore(config_obj, document: dict, *, confirm: bool) -> dict:
    if confirm is not True:
        return {"ok": False, "reason": "confirmation_required"}
    preview = preview_restore(config_obj, document)
    if not preview.get("ok"):
        return preview
    plan = preview.get("plan") or {}
    # Preserve current secret-bearing values inside nested sources; redacted
    # placeholders never overwrite local credentials.
    current = config_obj.get()
    if isinstance(plan.get("sources"), list):
        current_by_id = {s.get("id"): s for s in current.get("sources", []) if isinstance(s, dict)}
        merged = []
        for source in plan["sources"]:
            if not isinstance(source, dict):
                continue
            item = dict(source)
            old = current_by_id.get(item.get("id"), {})
            for key, value in old.items():
                if _is_sensitive_key(key):
                    item.setdefault(key, value)
            merged.append(item)
        plan["sources"] = merged
    if not plan:
        return {"ok": True, "applied": [], "sensitive_preserved": True}
    old_config = current
    try:
        config_obj.save(plan)
        new = config_obj.get()
        if not isinstance(new, dict) or not all(k in new for k in plan):
            raise ValueError("restored config validation failed")
    except Exception:
        _log.exception("config restore apply failed; rolling back")
        config_obj.save(old_config)
        return {"ok": False, "reason": "apply_failed_rolled_back"}
    return {"ok": True, "applied": sorted(plan),
            "sensitive_preserved": True}


def _is_sensitive_key(key: str) -> bool:
    return str(key).lower() in SENSITIVE_KEYS
