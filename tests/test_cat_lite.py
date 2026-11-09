"""内置轻量猫源（上游 v0.2.50/53 移植）：随包 cat_lite.json + tvbox 加载器。

只读本地文件，不碰网络；断言 11 站纯苹果 CMS、加载器返回的
(sites, spider, base, used) 四元组与上游约定一致。
"""
import asyncio

import tvbox


def test_cat_lite_sync_loads_11_cms_sites():
    sites, spider, base, used = tvbox.load_cat_lite_sync()
    assert len(sites) == 11
    assert spider == ""
    assert base == ""
    assert used == "builtin:cat_lite"
    keys = {s["key"] for s in sites}
    assert keys == {
        "bfzy", "lzi", "hnzy", "gtsy", "jszy", "ukzy",
        "xlzy", "360zy", "iqyzy", "maoyan", "ffzy2",
    }
    for s in sites:
        assert s["type"] == 1
        assert s["api"].endswith("/api.php/provide/vod/at/json") or \
            s["api"].endswith("xinlangapi.php/provide/vod/at/json")
        assert s["searchable"] == 1 and s["quickSearch"] == 1


def test_cat_lite_async_matches_sync():
    assert asyncio.run(tvbox.load_cat_lite()) == tvbox.load_cat_lite_sync()
