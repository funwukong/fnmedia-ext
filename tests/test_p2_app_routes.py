"""P2 管理接口已注册：只检查路由存在，不启动真机服务。"""
import app as appmod


def _paths():
    return {getattr(r, "path", "") for r in appmod.app.routes}


def test_p2_routes_registered():
    paths = _paths()
    for p in ("/v/api/ext/continue", "/v/api/ext/follow",
              "/v/api/ext/follow/check", "/v/api/ext/source-health",
              "/v/api/ext/source-health/reset", "/v/api/ext/source/heal",
              "/v/api/ext/favorite/enrich"):
        assert p in paths, f"missing route {p}"


def test_new_catalog_guids_registered():
    assert "overseas" in appmod.CATALOG_LIB_GUIDS
    assert "ai_comic" in appmod.CATALOG_LIB_GUIDS
    assert "ted" in appmod.CATALOG_LIB_GUIDS
    assert len({appmod.CATALOG_LIB_GUIDS[k] for k in ("overseas", "ai_comic", "ted")}) == 3
