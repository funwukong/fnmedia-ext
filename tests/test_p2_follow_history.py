"""P2 追剧提醒 + 继续观看。"""
from follow import FollowList
from history import History


def test_follow_check_updates(tmp_path):
    f = FollowList(tmp_path / "follow.json")
    assert f.follow("g1", "测试剧", episodes=10) is True
    assert f.is_following("g1") is True
    # 没更新
    assert f.check_updates({"g1": 10}) == []
    # 更新了 2 集
    ups = f.check_updates({"g1": 12})
    assert len(ups) == 1 and ups[0]["new_episodes"] == 2
    assert ups[0]["current_episodes"] == 12
    # 标记已看后提醒清零
    assert f.mark_seen("g1", 12) is True
    assert f.check_updates({"g1": 12}) == []
    assert f.unfollow("g1") is True
    assert f.is_following("g1") is False


def test_follow_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "follow.json"
    p.write_text("{bad", encoding="utf-8")
    f = FollowList(p)
    assert f.list() == []


def test_continue_watching(tmp_path, monkeypatch):
    import history as histmod
    monkeypatch.setattr(histmod, "_STATE", tmp_path / "history.json")
    h = History()
    h.set_position("ep1", ts=600, duration=3600)   # 未看完 16%
    h.set_position("ep2", ts=3500, duration=3600)  # 97% 已看完，不进列表
    h.set_position("ep3", ts=0, duration=3600)     # 刚开始
    lst = h.continue_watching()
    guids = [r["item_guid"] for r in lst]
    assert "ep1" in guids and "ep3" in guids
    assert "ep2" not in guids
    ep1 = next(r for r in lst if r["item_guid"] == "ep1")
    assert abs(ep1["progress"] - 600 / 3600) < 0.01
