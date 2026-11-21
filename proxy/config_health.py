"""Post-restore configuration health checks."""
from __future__ import annotations

try:
    from fnlog import get_logger
except ImportError:
    import logging

    def get_logger(name):
        return logging.getLogger(name)


_log = get_logger("config_health")


def check(config, catalog=None, app=None) -> dict:
    """Return a small, redacted health summary after a config restore."""
    issues = []
    try:
        cfg = config.get()
        if not isinstance(cfg.get("sources"), list):
            issues.append("sources_not_list")
        source_count = len(cfg.get("sources") or [])
    except Exception as exc:
        _log.debug("config health sources check failed", exc_info=True)
        return {"ok": False, "issues": [type(exc).__name__], "source_count": 0}
    route_count = None
    if catalog is not None:
        try:
            route_count = len(catalog._routes)
        except Exception:
            _log.debug("config health catalog check failed", exc_info=True)
            issues.append("catalog_unavailable")
    tasks = 0
    if app is not None:
        try:
            tasks = len([t for t in getattr(app.state, "background_tasks", set())
                         if not t.done()])
        except Exception:
            _log.debug("config health task check failed", exc_info=True)
            issues.append("tasks_unavailable")
    return {"ok": not issues, "issues": issues, "source_count": source_count,
            "route_count": route_count, "running_tasks": tasks}