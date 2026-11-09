"""TV 端识别：com.trim.tv 必须被认成严格模型；裸 okhttp 不带 bundle 标识时
认不到 TV（已知缺口：该请求会按宽松处理，严格规整不生效）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import client


def test_tv_ua_detected_strict():
    c = client.parse_user_agent("1.4.2 (com.trim.tv; build:14209; Android 35) okHttp/4.9.0")
    assert c.kind == "tv" and c.is_tv and c.strict_card


def test_tv_ua_case_insensitive():
    c = client.parse_user_agent("1.4.2 (COM.TRIM.TV; build:14209) okHttp/4.9.0")
    assert c.kind == "tv"


def test_bare_okhttp_not_tv():
    # 已知缺口：TV 若某次只发裸 okhttp UA（无 bundle 标识），会误判成安卓手机，
    # 严格卡片规整不生效，坏卡下发可能导致 TV 整页空白。
    c = client.parse_user_agent("okhttp/4.9.0")
    assert c.kind != "tv" and not c.strict_card


def test_web_and_ios_not_strict():
    assert not client.parse_user_agent("Mozilla/5.0 trimemedia-web").strict_card
    c = client.parse_user_agent("1.12.2 (com.trim.media.ios; build:1122011; iOS 27.2) Flutter/3.11.5")
    assert c.kind == "ios" and not c.strict_card


def test_strict_card_whitelist_and_date():
    c = client.Client(kind=client.TV, is_tv=True, strict_card=True)
    row = {"guid": "g1", "title": "片名", "type": "Movie",
           "release_date": "0", "can_play": True, "genres": ["动作"]}
    out = client.normalize_card(row, c)
    assert out["status"] == "1" and out["release_date"] == ""
    assert "can_play" not in out and "genres" not in out
