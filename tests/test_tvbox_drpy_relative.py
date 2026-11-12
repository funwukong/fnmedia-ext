"""相对路径 drpy JS 订阅导入的离线契约测试（上游 bf16a3b 移植，fork 口径）。

覆盖：GitHub 系订阅里 type=3 的 "./lib/drpy2.min.js" 引擎 +
"./js/规则.js" 相对写法，必须用订阅 base 补全为绝对 URL，才能被
is_drpy_site 识别并交给 Node sidecar（不依赖安卓运行时）。全部离线，
不联网、不启动 Node、不触碰播放主链、不动 drpy3 引擎。

另覆盖 fork 既有行为不变：type=4 与站点 jar 的既有绝对化、非 drpy 源
不受影响。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import tvbox  # noqa: E402

BASE = "https://raw.githubusercontent.com/owner/repo/master/js.json"


class AbsolutizeHelperTests(unittest.TestCase):
    def test_relative_js_engine_and_ext_become_absolute(self):
        site = {"type": 3, "api": "./lib/drpy2.min.js", "ext": "./js/drpy.js"}
        out = tvbox.absolutize_drpy_site(dict(site), BASE)
        self.assertEqual(
            out["api"],
            "https://raw.githubusercontent.com/owner/repo/master/lib/drpy2.min.js")
        self.assertEqual(
            out["ext"],
            "https://raw.githubusercontent.com/owner/repo/master/js/drpy.js")

    def test_rule_ext_supports_txt_and_json_suffix(self):
        for ext in ("./js/lf_live.txt", "./js/alist.json", "./js/规则.js"):
            site = {"type": 3, "api": "./lib/drpy2.min.js", "ext": ext}
            out = tvbox.absolutize_drpy_site(dict(site), BASE)
            self.assertTrue(out["ext"].startswith("https://"), ext)

    def test_absolute_urls_are_left_untouched(self):
        site = {"type": 3, "api": "https://x/a.js", "ext": "https://x/e.js"}
        out = tvbox.absolutize_drpy_site(dict(site), BASE)
        self.assertEqual(out["api"], "https://x/a.js")
        self.assertEqual(out["ext"], "https://x/e.js")

    def test_guards(self):
        # 无 base、非 type=3、csp_ 类名、协议相对 //host、.py 引擎均不得绝对化
        self.assertEqual(
            tvbox.absolutize_drpy_site(
                {"type": 3, "api": "./lib/drpy2.min.js"}, "")["api"],
            "./lib/drpy2.min.js")
        self.assertEqual(
            tvbox.absolutize_drpy_site(
                {"type": 1, "api": "./lib/drpy2.min.js"}, BASE)["api"],
            "./lib/drpy2.min.js")
        self.assertEqual(
            tvbox.absolutize_drpy_site(
                {"type": 3, "api": "csp_XBPQ", "ext": ""}, BASE)["api"],
            "csp_XBPQ")
        self.assertEqual(
            tvbox.absolutize_drpy_site(
                {"type": 3, "api": "//host/a.js"}, BASE)["api"],
            "//host/a.js")
        # Python 脚本不是 drpy JS 引擎，保持原样（下游会跳过）
        self.assertEqual(
            tvbox.absolutize_drpy_site(
                {"type": 3, "api": "./py/douban.py", "ext": None}, BASE)["api"],
            "./py/douban.py")

    def test_is_relative_js_predicate(self):
        self.assertTrue(tvbox._is_relative_js("./lib/drpy2.min.js"))
        self.assertTrue(tvbox._is_relative_js("/js/a.txt"))
        self.assertFalse(tvbox._is_relative_js("https://h/a.js"))
        self.assertFalse(tvbox._is_relative_js("//h/a.js"))
        self.assertFalse(tvbox._is_relative_js("csp_XBPQ"))
        self.assertFalse(tvbox._is_relative_js(""))


class ClassifyRelativeDrpyTests(unittest.TestCase):
    def _classified(self, sites):
        for st in sites:
            tvbox.absolutize_drpy_site(st, BASE)
        return tvbox.classify_subscription(sites, base=BASE)

    def test_relative_js_routes_to_drpy_bucket(self):
        sites = [
            {"key": "a", "name": "豆瓣[js]", "type": 3,
             "api": "./lib/drpy2.min.js", "ext": "./js/douban.js",
             "searchable": 1},
            {"key": "b", "name": "直播[js]", "type": 3,
             "api": "./lib/lf_live_min.js", "ext": "./js/lf_live.txt"},
        ]
        r = self._classified(sites)
        self.assertEqual(len(r["drpy"]), 2)
        for d in r["drpy"]:
            self.assertTrue(d["engine"].startswith("https://"))
            self.assertTrue(d["ext"].startswith("https://"))
        # 关键：不再落入“无法识别运行时”跳过桶
        self.assertEqual(
            [s for s in r["skipped"] if "无法识别" in s.get("reason", "")], [])

    def test_python_relative_scripts_not_misclassified_as_drpy(self):
        sites = [
            {"key": "py1", "name": "豆瓣[py]", "type": 3,
             "api": "./py/douban.py", "ext": None},
        ]
        r = self._classified(sites)
        self.assertEqual(r["drpy"], [])
        # .py 不被支持，进入跳过而非伪装成可运行的 JS 源
        self.assertTrue(any("无法识别" in s.get("reason", "")
                            for s in r["skipped"]))

    def test_csp_and_guard_unaffected(self):
        sites = [
            {"key": "g", "name": "加固", "type": 3, "api": "csp_JpysGuard"},
            {"key": "c", "name": "普通爬虫", "type": 3, "api": "csp_XBPQ",
             "ext": ""},
        ]
        r = self._classified(sites)
        self.assertEqual(len(r["guard"]), 1)
        self.assertEqual(len(r["csp"]), 1)
        self.assertEqual(r["drpy"], [])


class LoadSubscriptionRelativeTests(unittest.IsolatedAsyncioTestCase):
    """端到端：mock fetch_text，验证 walk 循环确实对相对 JS 做了绝对化。"""

    async def _load(self, config):
        text = json.dumps(config, ensure_ascii=False)

        async def fake_fetch_text(client, url):
            return text

        with patch.object(tvbox, "fetch_text", side_effect=fake_fetch_text):
            return await tvbox.load_subscription_full(url=BASE)

    async def test_walk_absolutizes_relative_drpy(self):
        config = {
            "spider": "",
            "sites": [
                {"key": "a", "name": "豆瓣[js]", "type": 3,
                 "api": "./lib/drpy2.min.js", "ext": "./js/douban.js"},
                {"key": "py", "name": "py源", "type": 3,
                 "api": "./py/x.py", "ext": None},
            ],
        }
        sites, _subs, _spider, base, _rules = await self._load(config)

        self.assertEqual(base, BASE)
        js = next(s for s in sites if s["key"] == "a")
        self.assertEqual(
            js["api"],
            "https://raw.githubusercontent.com/owner/repo/master/lib/drpy2.min.js")
        self.assertEqual(
            js["ext"],
            "https://raw.githubusercontent.com/owner/repo/master/js/douban.js")
        # .py 保持相对，最终在分类阶段被跳过（不伪装为可用源）
        py = next(s for s in sites if s["key"] == "py")
        self.assertEqual(py["api"], "./py/x.py")

        groups = tvbox.classify_subscription(sites, base=base)
        self.assertEqual(len(groups["drpy"]), 1)
        self.assertEqual(groups["drpy"][0]["name"], "豆瓣[js]")

    async def test_walk_keeps_type4_and_jar_absolutize_unchanged(self):
        # fork 既有行为：type=4 相对 api 与站点 jar 相对地址照旧补全。
        config = {
            "spider": "./jar/spider.jar;md5;xxxx",
            "sites": [
                {"key": "t4", "name": "T4源", "type": 4,
                 "api": "./api.php/provide/vod"},
                {"key": "c", "name": "爬虫", "type": 3, "api": "csp_XBPQ",
                 "ext": "", "jar": "./jar/site.jar;md5;yyyy"},
            ],
        }
        sites, _subs, _spider, base, _rules = await self._load(config)
        t4 = next(s for s in sites if s["key"] == "t4")
        self.assertEqual(
            t4["api"],
            "https://raw.githubusercontent.com/owner/repo/master/api.php/provide/vod")
        csp = next(s for s in sites if s["key"] == "c")
        # 注意 fork 既有 jar 补全把订阅 URL 当目录拼（js.json/ 后缀），
        # 本移植不改该行为，只锁定它不被回归。
        self.assertEqual(
            csp["_site_jar"],
            "https://raw.githubusercontent.com/owner/repo/master/js.json/jar/site.jar")
        self.assertEqual(csp["api"], "csp_XBPQ")

    async def test_walk_leaves_non_drpy_sites_untouched(self):
        # 非 drpy 源：CMS 相对 api 不由本移植补全；绝对 drpy api 原样不动。
        config = {
            "spider": "",
            "sites": [
                {"key": "cms", "name": "CMS源", "type": 1,
                 "api": "./api.php/provide/vod"},
                {"key": "abs", "name": "绝对drpy", "type": 3,
                 "api": "https://x/lib/drpy2.min.js", "ext": "https://x/js/a.js"},
            ],
        }
        sites, _subs, _spider, base, _rules = await self._load(config)
        cms = next(s for s in sites if s["key"] == "cms")
        self.assertEqual(cms["api"], "./api.php/provide/vod")
        abs_site = next(s for s in sites if s["key"] == "abs")
        self.assertEqual(abs_site["api"], "https://x/lib/drpy2.min.js")
        self.assertEqual(abs_site["ext"], "https://x/js/a.js")


if __name__ == "__main__":
    unittest.main(verbosity=2)
