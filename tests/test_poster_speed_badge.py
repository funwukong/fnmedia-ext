"""海报速度角标（解冻第 5 项）：实测速度拼进卡片 remarks → 标题后缀。

落点说明（见实现注释）：
- 图片水印（posterbadge.badge）不可行：Pillow 不在项目依赖里，NAS 与
  本测试环境上 posterbadge.available() 恒为 False，烧进图里真机上看不到。
- 新卡片字段（如 speed_badge）不可行：TV 严格端 normalize_card 会按
  TV_CARD_KEEP 白名单剥掉它；加进白名单等于把未知字段喂给 TV 的
  Flutter fromJson（正是白名单要防的整页失败）；且各端 UI 只渲染已知
  字段（标题/海报/overview…），未知字段即使透传也不会显示。
- 拼进 remarks：search_item 等卡片构造器早已把 remarks 拼进 title
  （" ·{remarks}"），title 在白名单内、各端都渲染；remarks 本身在
  详情/info 对象里经 normalize_detail（不裁剪）直通 TV。角标只追加在
  标题后缀与 remarks 文本里，TV 端零新增字段、零 fromJson 风险。

取值口径：只用已有实测值——_LINE_SPEED 里的指数滑动平均吞吐（KB/s，
按线路名记，1 小时过期，由 _line_speed 把关）。linepredict 记忆的是
首字节延迟（秒）不是吞吐，没有 KB/s 值可标，不单独作为角标值来源；
其记忆的线若另有 _LINE_SPEED 实测值，已包含在取最大值候选中。
角标路径只读内存表，不发任何网络请求；开关 poster_speed_badge 默认开。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import client as clientmod
import config as configmod
import inject

BRIEF = {
    "guid": "g-speed-1",
    "title": "测试片",
    "remarks": "更新至12集",
    "year": "2024",
    "area": "大陆",
    "poster": "https://img.test/p.jpg",
    "type_name": "电影",
    "duration": 0,
    "score": "8.0",
    "lines": {"cms::A": [{"url": "https://x.test/m.m3u8"}],
              "cms::B": [{"url": "https://y.test/m.m3u8"}]},
}


def _brief(**over):
    b = json.loads(json.dumps(BRIEF))
    b.update(over)
    return b


@pytest.fixture(autouse=True)
def _isolate_speed_table():
    saved = dict(appmod._LINE_SPEED)
    appmod._LINE_SPEED.clear()
    try:
        yield
    finally:
        appmod._LINE_SPEED.clear()
        appmod._LINE_SPEED.update(saved)


@pytest.fixture()
def _switch_on(monkeypatch):
    monkeypatch.setattr(
        appmod, "config",
        types.SimpleNamespace(get=lambda: {"poster_speed_badge": True}))
    return True


def _seed(line, kbps, age=0.0):
    appmod._LINE_SPEED[line] = (float(kbps), time.time() - age)


# ---------- 取值与格式化 ----------

def test_format_speed_badge_matrix(_switch_on):
    f = appmod._format_speed_badge
    assert f(0) == ""
    assert f(-5) == ""
    assert f(860) == "⚡860KB/s"
    assert f(1023) == "⚡1023KB/s"
    assert f(1024) == "⚡1.0MB/s"
    assert f(2560) == "⚡2.5MB/s"


def test_best_speed_is_max_ewma_across_lines(_switch_on):
    _seed("cms::A", 860.0)
    _seed("cms::B", 120.0)
    assert appmod._brief_best_speed(_brief()) == 860.0
    assert appmod._brief_speed_badge(_brief()) == "⚡860KB/s"


def test_ewma_value_is_displayed_not_single_sample(_switch_on):
    # _remember_speed 是 0.6 旧值 + 0.4 新值：单次 500 KB/s 的抖动
    # 不应直接上墙，显示的应是滑动平均后的 800 KB/s。
    _seed("cms::A", 1000.0)
    appmod._remember_speed({"line": "cms::A"}, 500.0)
    assert appmod._brief_best_speed(_brief()) == pytest.approx(800.0)
    assert appmod._brief_speed_badge(_brief()) == "⚡800KB/s"


def test_no_measurement_no_badge(_switch_on):
    assert appmod._brief_speed_badge(_brief()) == ""
    assert appmod._brief_best_speed(_brief()) == 0.0


def test_expired_measurement_no_badge(_switch_on):
    # _line_speed 的口径：1 小时前的观测不再作数。
    _seed("cms::A", 860.0, age=3601)
    _seed("cms::B", 120.0, age=7200)
    assert appmod._brief_speed_badge(_brief()) == ""
    # 3599 秒内的仍算数。
    _seed("cms::A", 860.0, age=3599)
    assert appmod._brief_speed_badge(_brief()) == "⚡860KB/s"


def test_badge_does_not_mutate_brief(_switch_on):
    b = _brief()
    _seed("cms::A", 860.0)
    appmod._brief_speed_badge(b)
    inject.search_item(b)
    assert b["remarks"] == "更新至12集"
    assert "speed" not in b and "_speed_badge" not in b


def test_linepredict_latency_is_not_a_speed_value(_switch_on):
    # linepredict 只记首字节延迟（秒），没有 KB/s 值：即使它记得某线最快，
    # 只要 _LINE_SPEED 里没有该线的吞吐实测，就不出角标、不编速度。
    import linepredict
    linepredict.remember("g-speed-1|0", "cms::A", 0.3)
    try:
        assert linepredict.fastest("g-speed-1|0") == "cms::A"
        assert appmod._brief_speed_badge(_brief()) == ""
    finally:
        linepredict._FAST.pop("g-speed-1|0", None)


# ---------- 卡片落点：标题后缀 ----------

def test_search_item_title_carries_badge(_switch_on):
    _seed("cms::A", 860.0)
    card = inject.search_item(_brief())
    assert card["title"] == "【在线】测试片 ·更新至12集 ·⚡860KB/s"
    # 角标是标题后缀，不是新字段：卡片键集合不变。
    assert "speed_badge" not in card and "line_speed" not in card


def test_search_item_title_without_measurement_is_unchanged(_switch_on):
    card = inject.search_item(_brief())
    assert card["title"] == "【在线】测试片 ·更新至12集"


def test_search_item_title_without_remarks(_switch_on):
    _seed("cms::B", 2048.0)
    card = inject.search_item(_brief(remarks=""))
    assert card["title"] == "【在线】测试片 ·⚡2.0MB/s"


def test_badge_path_makes_no_network_requests(_switch_on):
    # 角标路径只读内存表：把 httpx 的出口全部钉死，建卡 + 取角标仍须通过。
    _seed("cms::A", 860.0)
    with mock.patch.object(
            httpx.AsyncClient, "request",
            side_effect=AssertionError("badge path must not hit network")), \
        mock.patch.object(
            httpx.AsyncClient, "send",
            side_effect=AssertionError("badge path must not hit network")):
        assert appmod._brief_speed_badge(_brief()) == "⚡860KB/s"
        card = inject.search_item(_brief())
        assert "⚡860KB/s" in card["title"]


# ---------- 严格客户端（TV）安全 ----------

def _strict_from_json(rows):
    """模拟 TV(Flutter) 严格列表解析：只接受白名单字段、JSON 标量、
    status 数字串、完整日期；任一违规即抛错（整页失败）。"""
    raw = json.dumps(rows, ensure_ascii=False)
    data = json.loads(raw)
    assert isinstance(data, list)
    for row in data:
        if not isinstance(row, dict):
            continue  # 非 dict 行原样透传，不影响整页解析
        unknown = set(row) - set(clientmod.TV_CARD_KEEP)
        assert not unknown, f"unknown fields would break TV fromJson: {unknown}"
        assert row.get("status") in ("0", "1"), row.get("status")
        for k in ("release_date", "air_date"):
            v = row.get(k, "")
            assert v == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v), (k, v)
        for v in row.values():
            assert v is None or isinstance(v, (str, int, float, bool, list, dict))
    return data


def test_strict_tv_cards_with_badge_do_not_break_page(_switch_on):
    _seed("cms::A", 860.0)
    good = inject.search_item(_brief())
    other = inject.search_item(_brief(guid="g-speed-2", title="另一部",
                                      remarks=""))
    assert "⚡860KB/s" in good["title"] and "⚡860KB/s" in other["title"]
    rows = [good, "not-a-dict", {"title": "缺 guid 的坏卡"}, other]
    tv = clientmod.parse_user_agent(
        "1.4.2 (com.trim.tv; build:14209; Android 35) okHttp/4.9.0")
    assert tv.strict_card
    out = clientmod.normalize_cards(rows, tv)
    assert len(out) == len(rows)  # 坏卡不得导致整页失败
    _strict_from_json([r for r in out if isinstance(r, dict)])
    dict_rows = [r for r in out if isinstance(r, dict)]
    assert "⚡860KB/s" in dict_rows[0]["title"]
    assert "⚡860KB/s" in dict_rows[-1]["title"]
    assert dict_rows[0]["status"] == "1"


def test_nonstrict_clients_see_badge_unchanged(_switch_on):
    _seed("cms::A", 860.0)
    card = inject.search_item(_brief())
    for ua in ("trimemedia-web", "Dalvik/2.1.0 Android",
               "com.trim.media.ios"):
        profile = clientmod.parse_user_agent(ua)
        assert clientmod.normalize_card(card, profile) is card
        assert "⚡860KB/s" in card["title"]


# ---------- 开关 ----------

def test_config_default_is_on_and_old_config_gets_it(monkeypatch, tmp_path):
    assert configmod.DEFAULTS["poster_speed_badge"] is True
    state = tmp_path / "state" / "config.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"config_version": 2, "hot_limit": 300}),
                     encoding="utf-8")
    monkeypatch.setattr(configmod, "_STATE", state)
    c = configmod.Config()
    assert c.get()["poster_speed_badge"] is True


def test_switch_off_hides_badge(monkeypatch):
    _seed("cms::A", 860.0)
    monkeypatch.setattr(
        appmod, "config",
        types.SimpleNamespace(get=lambda: {"poster_speed_badge": False}))
    assert appmod._poster_speed_badge_on() is False
    assert appmod._brief_speed_badge(_brief()) == ""
    card = inject.search_item(_brief())
    assert card["title"] == "【在线】测试片 ·更新至12集"
    assert "⚡" not in card["title"]


def test_switch_missing_key_defaults_on(monkeypatch):
    # 旧配置没有该键：get() 缺键时按 True 处理（与 DEFAULTS 一致）。
    monkeypatch.setattr(
        appmod, "config", types.SimpleNamespace(get=lambda: {}))
    assert appmod._poster_speed_badge_on() is True


def test_line_speed_badge_single_line(_switch_on):
    _seed("cms::A", 860.0)
    assert appmod._line_speed_badge("cms::A") == "⚡860KB/s"
    assert appmod._line_speed_badge("cms::B") == ""
    _seed("cms::B", 300.0, age=4000)
    assert appmod._line_speed_badge("cms::B") == ""


# ---------- 管理端点白名单（HTTP 级） ----------

class ConfigEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        import history as histmod
        import store as storemod
        for module, filename in [(configmod, "config"),
                                 (storemod, "titles"),
                                 (histmod, "history")]:
            item = mock.patch.object(
                module, "_STATE",
                Path(self._temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": []})
        for module, name, value in [
            (appmod, "config", self.config),
            (appmod, "store", storemod.Store()),
            (appmod, "history", histmod.History()),
        ]:
            item = mock.patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=appmod.app),
            base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_config_endpoint_accepts_poster_speed_badge(self):
        assert self.config.get()["poster_speed_badge"] is True
        resp = await self.client.post(
            "/v/api/ext/config", json={"poster_speed_badge": False})
        assert resp.status_code == 200
        assert resp.json().get("code") == 0
        assert self.config.get()["poster_speed_badge"] is False
        assert appmod._poster_speed_badge_on() is False
        resp = await self.client.post(
            "/v/api/ext/config", json={"poster_speed_badge": True})
        assert resp.json().get("code") == 0
        assert self.config.get()["poster_speed_badge"] is True


# ---------- 落点决策的证据：图片水印在无 Pillow 环境不可用 ----------

def test_posterbadge_unavailable_without_pillow():
    """Pillow 不在依赖里：posterbadge.available() 为 False，图片水印落点
    在 NAS 上恒不生效——这正是角标改走卡片字段的原因。"""
    try:
        import PIL  # noqa: F401
    except ImportError:
        import posterbadge
        assert posterbadge.available() is False
        assert posterbadge.badge(b"\xff\xd8\xff", "夸克", 0) is None
    else:
        pytest.skip("Pillow is installed here; image badge would work")
