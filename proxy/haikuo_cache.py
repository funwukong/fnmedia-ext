"""Stable Haikuo discovery/category index and short-lived play state."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from threading import RLock

_STATE = Path(__file__).resolve().parent.parent / "state" / "haikuo_video_index.json"
_MAX = 10000
_LOCK = RLock()
_MEM: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _MEM
    if _MEM is not None:
        return _MEM
    try:
        raw = json.loads(_STATE.read_text(encoding="utf-8"))
        _MEM = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        _MEM = {}
    return _MEM


def _save() -> None:
    data = _MEM or {}
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".haikuo-index-", dir=str(_STATE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush(); os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _STATE)
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def _now() -> int:
    return int(time.time())


def record_discovery(guid: str, source_id: str, rule_key: str, brief: dict,
                     category: dict | None = None) -> None:
    """Persist identity + category relation; never persists expiring media URL."""
    if not guid or not source_id:
        return
    with _LOCK:
        db = _load()
        row = db.get(guid) or {"guid": guid, "categories": [], "episodes": {}}
        row.update({"source_id": source_id, "rule_key": rule_key,
                    "title": str(brief.get("title") or ""),
                    "detail_url": str(brief.get("vod_id") or ""),
                    "poster": str(brief.get("poster") or ""),
                    "remarks": str(brief.get("remarks") or ""),
                    "last_seen": _now()})
        if category and category.get("key"):
            item = {"key": str(category["key"]),
                    "name": str(category.get("name") or "未分类"),
                    "path": list(category.get("path") or []),
                    "kind": str(category.get("kind") or "category")}
            cats = [x for x in row.get("categories", [])
                    if isinstance(x, dict) and x.get("key") != item["key"]]
            row["categories"] = (cats + [item])[-32:]
        row.setdefault("first_seen", _now())
        db[guid] = row
        if len(db) > _MAX:
            old = sorted(db.items(), key=lambda kv: kv[1].get("last_seen", 0))
            for key, _ in old[:len(db) - _MAX]: db.pop(key, None)
        _save()


def record_play(guid: str, index: int, *, ok: bool, url: str = "",
                headers: dict | None = None, error_code: str = "",
                error: str = "") -> None:
    if not guid:
        return
    with _LOCK:
        db = _load(); row = db.setdefault(guid, {"guid": guid, "episodes": [], "categories": []})
        eps = row.setdefault("episodes", {})
        key = str(max(0, int(index or 0)))
        old = eps.get(key) or {}
        now = _now()
        old.update({"state": "ready" if ok else "failed", "updated_at": now})
        if ok:
            old.update({"url": str(url or ""), "headers": dict(headers or {}),
                        "expires_at": now + 180, "error_code": "", "error": ""})
            row["last_play_at"] = now
        else:
            old.update({"url": "", "headers": {}, "expires_at": 0,
                        "error_code": error_code, "error": error[:300]})
        eps[key] = old
        _save()


def briefs_for_category(source_id: str, category_key: str) -> list[dict]:
    with _LOCK:
        out = []
        for row in (_load() or {}).values():
            if row.get("source_id") != source_id:
                continue
            if not any(isinstance(c, dict) and c.get("key") == category_key
                       for c in row.get("categories", [])):
                continue
            brief = row.get("brief")
            if isinstance(brief, dict): out.append(dict(brief))
        return out


def attach_brief(guid: str, brief: dict) -> None:
    with _LOCK:
        row = _load().get(guid)
        if row is not None:
            row["brief"] = dict(brief)
            _save()


def status(guid: str) -> dict:
    with _LOCK:
        return dict((_load() or {}).get(guid) or {})


def reset() -> None:
    global _MEM
    with _LOCK:
        _MEM = {}
        try: _STATE.unlink()
        except OSError: pass
