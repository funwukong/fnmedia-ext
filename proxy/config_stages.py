from __future__ import annotations

import json
from pathlib import Path


def read_raw(state: Path, restore_backup):
    try:
        raw = json.loads(state.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
        return raw, False, False
    except FileNotFoundError:
        return {}, True, False
    except Exception:
        raw = restore_backup() or {}
        return raw, False, bool(raw)


def merge_sources(defaults, saved, removed, normalize, retired_id):
    saved_by_id = {s.get("id"): s for s in (saved or [])
                   if isinstance(s, dict) and s.get("id") != retired_id}
    merged = []
    builtin_ids = {s.get("id") for s in defaults}
    for source in defaults:
        if source.get("id") in removed:
            continue
        merged.append(normalize(source, builtin=True,
                                override=saved_by_id.get(source.get("id"), {})))
    merged_ids = {s.get("id") for s in merged}
    for sid, source in saved_by_id.items():
        if sid in builtin_ids or sid in removed or sid in merged_ids:
            continue
        merged.append(normalize(source, builtin=bool(source.get("builtin")),
                                override=source))
        merged_ids.add(sid)
    return merged


def migrate_versions(cfg: dict, raw: dict, *, stored_version: int | None,
                      version_known: bool, version_missing: bool,
                      current_version: int, log) -> dict:
    migrated = False
    acl_migrated = False
    version_normalized = False
    if not version_known:
        log.warning("config_version unparsable (%r), skipping migrations; normalizing to %d",
                    raw.get("config_version"), current_version)
        cfg["config_version"] = current_version
        migrated = version_normalized = True
    elif stored_version > current_version:
        log.warning("config_version %d is newer than supported %d; skipping migration",
                    stored_version, current_version)
    else:
        cur = stored_version
        if cur < 2:
            cfg["acl_default_visible"] = False
            cfg["acl_default_online"] = False
            cfg["config_version"] = 2
            cur = 2; migrated = acl_migrated = True
            log.info("config migration v1->v2: fail-closed defaults")
        if cur < 3:
            cfg["acl_default_visible"] = True
            cfg["acl_default_online"] = True
            cfg["config_version"] = 3
            migrated = acl_migrated = True
            log.info("config migration v2->v3: fail-open defaults")
        else:
            log.info("config migration skipped: stored version %d is current", stored_version)
    keys = []
    if migrated:
        keys.extend(("hot_limit", "hot_per_source", "hot_pages_per_source"))
    if acl_migrated:
        keys.extend(("acl_default_visible", "acl_default_online", "config_version"))
    if version_normalized:
        keys.append("config_version")
    if keys:
        raw.update({key: cfg[key] for key in dict.fromkeys(keys) if key in cfg})
    return {"migrated": migrated, "acl_migrated": acl_migrated,
            "version_normalized": version_normalized}


def maintain_admin_token(cfg: dict, *, now: int, new_token, log) -> bool:
    """Ensure current token metadata; return whether a persist is needed."""
    changed = False
    if not cfg.get("admin_token"):
        cfg["admin_token"] = new_token()
        cfg["admin_token_created_at"] = now
        return True
    if not cfg.get("admin_token_created_at"):
        cfg["admin_token_created_at"] = now
        changed = True
    expiry = cfg.get("admin_token_previous_expires_at") or 0
    if cfg.get("admin_token_previous") and expiry and int(expiry) <= now:
        cfg["admin_token_previous"] = ""
        cfg["admin_token_previous_expires_at"] = 0
        changed = True
    return changed
