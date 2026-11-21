import config_health


def test_health_reports_sources_routes_and_tasks():
    class C:
        def get(self): return {"sources":[{"id":"x"}]}
    class Cat:
        _routes={"x":[]}
    class State:
        background_tasks=set()
    class A: state=State()
    out=config_health.check(C(),Cat(),A())
    assert out["ok"] and out["source_count"] == 1
    assert out["route_count"] == 1
    assert out["running_tasks"] == 0
