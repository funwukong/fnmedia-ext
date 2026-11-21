import pansou


def test_identity_snapshot_uses_longer_but_bounded_ttl(monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(pansou.time, "time", lambda: now)
    snap = {"ts": now - pansou._PANSOU_SNAP_TTL - 1,
            "provider": "quark", "fid": "saved-file-1"}
    assert pansou._pansou_snap_fresh(snap)
    assert pansou._pansou_snap_ttl(snap) == pansou._PANSOU_IDENTITY_SNAP_TTL
    assert pansou._PANSOU_IDENTITY_SNAP_TTL < 6 * 3600.0


def test_missing_identity_keeps_short_legacy_ttl(monkeypatch):
    now = 20_000.0
    monkeypatch.setattr(pansou.time, "time", lambda: now)
    old = {"ts": now - pansou._PANSOU_SNAP_TTL - 1,
           "provider": "quark", "url": "/v/api/ext/pan/play?fid=x"}
    assert not pansou._pansou_snap_fresh(old)
    assert pansou._pansou_snap_ttl(old) == pansou._PANSOU_SNAP_TTL


def test_unknown_provider_does_not_get_identity_ttl(monkeypatch):
    now = 30_000.0
    monkeypatch.setattr(pansou.time, "time", lambda: now)
    snap = {"ts": now - pansou._PANSOU_SNAP_TTL - 1,
            "provider": "new-provider", "fid": "saved-file-2"}
    assert not pansou._pansou_snap_fresh(snap)


def test_identity_snapshot_expires_at_layer_boundary(monkeypatch):
    now = 40_000.0
    monkeypatch.setattr(pansou.time, "time", lambda: now)
    snap = {"ts": now - pansou._PANSOU_IDENTITY_SNAP_TTL - 1,
            "provider": "ali", "fid": "saved-file-3"}
    assert not pansou._pansou_snap_fresh(snap)
