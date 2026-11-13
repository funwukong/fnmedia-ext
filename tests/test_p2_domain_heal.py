"""P2 域名自愈：归一化、拦截页识别、候选顺序。"""
from domain_heal import candidates, heal, is_challenge_page, is_valid_list_page, normalize_host

GOOD_JSON = '{"code": 1, "list": [{"vod_id": 1}]}'
CHALLENGE = "正在验证，请稍候..." + "x" * 500
SHELL = "<script>location.href='/x'</script>"


def test_normalize_host_variants():
    assert normalize_host("example.com") == "https://example.com"
    assert normalize_host("https://example.com/api.php") == "https://example.com"
    assert normalize_host('{"host": "new.example.com"}') == "https://new.example.com"
    assert normalize_host('{"url": "http://a.example.com/x"}') == "http://a.example.com"
    assert normalize_host("") == ""
    assert normalize_host("{bad") == ""


def test_challenge_detection():
    assert is_challenge_page(200, CHALLENGE) is True
    assert is_challenge_page(200, "x" * 10) is True  # 太短的空壳
    assert is_challenge_page(403, GOOD_JSON) is True
    assert is_challenge_page(200, GOOD_JSON) is False
    assert is_valid_list_page(200, GOOD_JSON, expect_json=True) is True
    assert is_valid_list_page(200, CHALLENGE, expect_json=True) is False


def test_candidates_order_and_dedupe():
    assert candidates("https://work.example.com", "new.example.com",
                      "https://primary.example.com",
                      ["https://primary.example.com", "https://bak.example.com"]) == [
        "https://work.example.com", "https://new.example.com",
        "https://primary.example.com", "https://bak.example.com"]


def test_heal_picks_first_valid():
    def fetch(url):
        if "bad.example.com" in url:
            return 200, CHALLENGE
        if "good.example.com" in url:
            return 200, GOOD_JSON
        return 404, ""
    url = heal("https://bad.example.com/api.php/provide/vod/at/json",
               backups=["https://good.example.com"], fetch=fetch)
    assert url == "https://good.example.com/api.php/provide/vod/at/json"


def test_heal_all_fail_returns_none():
    def fetch(url):
        return 200, CHALLENGE
    assert heal("https://a.example.com/api.php", fetch=fetch) is None
    assert heal("https://a.example.com/api.php", fetch=None) is None
