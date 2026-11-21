"""Compatibility view over legacy playback target dictionaries.

The playback pipeline still exchanges dicts for wire compatibility. This module
centralizes mode detection so callers stop duplicating boolean-field precedence.
"""
from __future__ import annotations


def mode(target: dict | None) -> str:
    """Return the next resolver mode without mutating the target."""
    if not isinstance(target, dict):
        return "none"
    if target.get("drpy"):
        return "drpy"
    if target.get("xsrc"):
        return "xsrc"
    if target.get("redirect"):
        return "redirect"
    if target.get("resolve"):
        return "resolve"
    if target.get("m3u8"):
        return "hls"
    if target.get("client_direct"):
        return "client_direct"
    if target.get("direct") or target.get("url"):
        return "direct"
    return "none"


def needs_redirect(target: dict | None) -> bool:
    return isinstance(target, dict) and bool(target.get("redirect"))


def needs_resolve(target: dict | None) -> bool:
    return isinstance(target, dict) and bool(target.get("resolve"))


def needs_special_resolution(target: dict | None) -> bool:
    """Whether a target must enter a resolver before ordinary media probing."""
    if not isinstance(target, dict):
        return False
    return bool(target.get("xsrc") or target.get("drpy")
                or target.get("catvod") or target.get("spider_src"))


def is_slow_line(target: dict | None) -> bool:
    """Whether the target should use the slow-line placeholder budget."""
    if not isinstance(target, dict):
        return False
    if needs_special_resolution(target):
        return True
    line = str(target.get("line") or "")
    return line.startswith(("spider-", "drpy-"))
