# -*- coding: utf-8 -*-
"""虚拟媒体库统一管理：显示开关、排序、子账号可见授权。

上游 yujian0815/fnmedia-ext 的 vlibs 设计，本 fork 移植版。
设计要点
--------
- 每张虚拟库有一个与 guid 解耦的**稳定库键 key**（如 ``chart:movie``、
  ``standalone:baofeng``、``guard:abc123``、``xlib:movie``），配置里按 key 存
  显隐 / 排序 / 授权，库 guid 算法调整也不影响用户设置。
- guid 派生与 app.py 中既有定义保持一致，保证老链接/收藏不失效。
- 账号识别（谁在看）在 ``fnusers.py``；本模块只回答“这个账号能不能看到这张库”。

与上游的差别（仅此处）：
- CATALOG_LIB_GUIDS 按本 fork 的 11 个分类（含 documentary/uhd/shortdrama/
  overseas/ai_comic/ted/adult），guid 与 app.py 完全一致；
- 多一个 K_HGDJ = "hgdj"（红果短剧库，guid 是 fork 特有的 "on"+md5 形式，
  由 app.py 的 _vkey_for_guid 现场反查，不进 key_of_guid）。
"""
from __future__ import annotations

import hashlib

from config import config
import features

# —— 与 app.py 完全一致的固定 guid ——（勿改，改了旧入口会失效）
CHART_LIB_GUIDS = {
    "movie":   "0b10d767319eb8cab9d99373ede48d0a",
    "tv":      "9e75c8f2616bd01851911eea649db367",
    "anime":   "18372cada44b72c305343c2bfcc026f0",
    "variety": "cb606d5f699560c630c97f7acc84f75b",
}
CATALOG_LIB_GUIDS = {
    "movie":      "e91dd7347a49dbd874fb8a0e78ad7ad7",
    "tv":         "196381f891d66a4de32f9032e8f244f3",
    "anime":      "97fc49015edcb20cc423658401711b6c",
    "variety":    "cfff30730cffca2d1357e0c2b52fc2f5",
    "documentary": "155f2305c2cbc76c85f981ef31cc192f",
    "uhd":        "262ce51312d7e72020514db5fd5f63e9",
    "shortdrama": "037ed7c6df4a9deb8d815c823fd15f79",
    "overseas":   "c2fe61ae4d3adc87756a253cb108066e",
    "ai_comic":   "6784f423ff4be5af4fe60cde6c8da303",
    "ted":        "34883d4af44ca4a5539cb38827158294",
    "adult":      "411515e17c85709cf1966697b7b2c58e",
}
CHART_CAT_LABEL = {"movie": "电影榜", "tv": "剧集榜", "anime": "动漫榜", "variety": "综艺榜"}
CATALOG_CAT_LABEL = {
    "movie": "海量电影", "tv": "海量剧集", "anime": "海量动漫", "variety": "海量综艺",
    "documentary": "纪录片", "uhd": "4K影视", "shortdrama": "热门短剧",
    "overseas": "海外剧集", "ai_comic": "AI漫剧", "ted": "TED学习",
    "adult": "扩展资源",
}

_STANDALONE_PREFIX = "fnmedia-standalone:"
_GUARD_LIB_PREFIX = "fnmedia-guard:"
_GUARD_NATIVE_GUID = hashlib.md5(b"fnmedia-guardnative:root").hexdigest()
_XLIB_LIB_PREFIX = "fnmedia-xlib:"
_T4_GUID = hashlib.md5(b"fnmedia-t4-browse").hexdigest()

# 库类型（固定/组开关）与 key 前缀
K_CHART = "chart"        # 豆瓣真实榜单四库
K_CATALOG = "catalog"    # 海量分类库
K_GUARD_NATIVE = "guard-native"
K_STANDALONE = "standalone"
K_GUARD = "guard"
K_XLIB = "xlib"
K_T4 = "t4"
K_HGDJ = "hgdj"          # 本 fork 扩展：红果短剧库


def standalone_guid(source_id: str) -> str:
    return hashlib.md5((_STANDALONE_PREFIX + source_id).encode()).hexdigest()


def guard_guid(gkey: str) -> str:
    return hashlib.md5((_GUARD_LIB_PREFIX + gkey).encode()).hexdigest()


def xlib_guid(cat_id: str) -> str:
    return hashlib.md5((_XLIB_LIB_PREFIX + cat_id).encode()).hexdigest()


def make_key(kind: str, ident: str = "") -> str:
    return f"{kind}:{ident}" if ident else kind


def key_of_guid(guid: str) -> str | None:
    """guid 反查稳定库键；非虚拟库返回 None。动态库需要现场遍历（见 app.py 的 _vkey_for_guid）。"""
    if not guid:
        return None
    if guid == _T4_GUID:
        return K_T4
    for cat, g in CHART_LIB_GUIDS.items():
        if g == guid:
            return make_key(K_CHART, cat)
    for cat, g in CATALOG_LIB_GUIDS.items():
        if g == guid:
            return make_key(K_CATALOG, cat)
    if guid == _GUARD_NATIVE_GUID:
        return K_GUARD_NATIVE
    return None


def normalize_key(key) -> str:
    """校验库键形状：合法返回原键，否则返回 ""。

    固定键：t4 / guard-native / hgdj；chart:/catalog: 后缀须是已知分类；
    standalone:/guard:/xlib: 后缀是动态标识，非空即合法。
    """
    if not isinstance(key, str) or not key:
        return ""
    if key in (K_T4, K_GUARD_NATIVE, K_HGDJ):
        return key
    kind, sep, ident = key.partition(":")
    if not sep or not ident:
        return ""
    if kind == K_CHART and ident in CHART_LIB_GUIDS:
        return key
    if kind == K_CATALOG and ident in CATALOG_LIB_GUIDS:
        return key
    if kind in (K_STANDALONE, K_GUARD, K_XLIB):
        return key
    return ""


# --------------------------------------------------------------- 显示开关

def hidden_libs() -> set[str]:
    """管理员手动隐藏的库键集合。"""
    try:
        return set(config.get().get("lib_hidden", []) or [])
    except Exception:
        return set()


def is_hidden(key: str) -> bool:
    return key in hidden_libs()


def set_hidden(key: str, hidden: bool) -> tuple[bool, str]:
    """设置某库的全局显隐（管理员级，不分账号）。非法库键拒绝。"""
    if not normalize_key(key):
        return False, "库键非法"
    cur = set(config.get().get("lib_hidden", []) or [])
    (cur.add if hidden else cur.discard)(key)
    _update({"lib_hidden": sorted(cur)})
    return True, "ok"


def _update(patch: dict) -> None:
    """复用 config 持久化入口。"""
    config.save(patch)


# --------------------------------------------------------------- 排序

# placement: "front" 全部本地库之前 | "back" 全部本地库之后 |
#            "after-local:<本地库guid>" 插到指定本地库之后
def lib_order() -> list[str]:
    """虚拟库的自定义相对顺序（库键数组）。未列入的库按默认顺序追加在后。"""
    try:
        return list(config.get().get("lib_order", []) or [])
    except Exception:
        return []


def lib_placement_default() -> str:
    return str(config.get().get("lib_placement", "front"))  # front/back/after


def lib_anchor() -> str:
    return str(config.get().get("lib_anchor", "") or "")


def order_index(key: str) -> int:
    try:
        return lib_order().index(key)
    except ValueError:
        return 10_000_000  # 未自定义的排到已定义之后


def set_order(keys, placement=None, anchor=None) -> tuple[bool, str]:
    """保存虚拟库自定义顺序与相对本地库位置。

    keys 须是列表：非法键会被丢弃、重复去重、上限 500；placement 仅接受
    front/back/after；anchor 为本地库 guid（""=清除）。只传 keys 时仅改顺序。
    """
    if not isinstance(keys, list):
        return False, "order 非法"
    clean = []
    for k in keys:
        nk = normalize_key(k)
        if nk and nk not in clean:
            clean.append(nk)
    patch = {"lib_order": clean[:500]}
    if placement is not None:
        if placement not in ("front", "back", "after"):
            return False, "placement 非法"
        patch["lib_placement"] = placement
    if anchor is not None:
        if not isinstance(anchor, str):
            return False, "anchor 非法"
        patch["lib_anchor"] = anchor
    _update(patch)
    return True, "ok"


def sort_keys(keys: list[str]) -> list[str]:
    """按自定义顺序排序；未列出的库保持稳定地追加在后。"""
    n = len(keys)
    return sorted(keys, key=lambda k: (order_index(k) if order_index(k) < 10_000_000
                                       else 10_000_000 + keys.index(k)))


# --------------------------------------------------------------- 子账号授权

def _acl() -> dict:
    try:
        return dict(config.get().get("lib_acl", {}) or {})
    except Exception:
        return {}


def set_account_acl(user_guid: str, allowed_keys: list[str] | None) -> tuple[bool, str]:
    """设置某账号允许观看的库；None=删除其单独配置（回到默认）。

    allowed_keys 须是列表：非法键会被丢弃，"__online__" 标记保留（决定
    该账号能否用在线搜索）。非法 guid 或非列表拒绝。
    """
    if not isinstance(user_guid, str) or not user_guid:
        return False, "guid 非法"
    if allowed_keys is None:
        acl = _acl()
        acl.pop(user_guid, None)
        _update({"lib_acl": acl})
        return True, "ok"
    if not isinstance(allowed_keys, list):
        return False, "allow 非法"
    clean = sorted({k for k in allowed_keys
                    if k == "__online__" or normalize_key(k)})[:1000]
    acl = _acl()
    acl[user_guid] = clean
    _update({"lib_acl": acl})
    return True, "ok"


def get_account_acl(user_guid: str):
    """返回该账号配置；None 表示未单独配置（走默认）。"""
    return _acl().get(user_guid)


def account_allowlist(user_guid: str):
    """get_account_acl 的别名：供管理端读取某账号的允许库列表。"""
    return get_account_acl(user_guid)


def acl_default_visible() -> bool:
    """未配置账号默认能否看到虚拟库（False=隐藏，fail-closed 默认）。"""
    try:
        return bool(config.get().get("acl_default_visible", False))
    except Exception:
        return False


def acl_default_online() -> bool:
    """未配置账号默认能否用在线搜索（False=禁用，fail-closed 默认）。"""
    try:
        return bool(config.get().get("acl_default_online", False))
    except Exception:
        return False


def account_can_see(user: dict, key: str) -> bool:
    """该账号能否看到某虚拟库（fail-closed）。

    - 识别不出身份（known=False）→ 始终不可见（不受默认开关影响）
    - 管理员 → 始终可见
    - 已配置账号 → 仅允许列表内可见
    - 已识别但未配置账号 → 默认 acl_default_visible（默认 False=隐藏）
    - 任何被管理员全局隐藏的库对所有人不可见（含管理员自己的飞牛列表，
      管理页仍可重新打开）。
    """
    if not key:
        return True
    if is_hidden(key):
        return False
    if not user or not user.get("known"):
        return False
    if user.get("is_admin"):
        return True
    allow = _acl().get(user.get("guid", ""))
    if allow is None:
        return acl_default_visible()
    return key in allow


def account_online_allowed(user: dict) -> bool:
    """该账号能否使用在线搜索 / 在线片源（家长可只给孩子开本地授权库）。

    fail-closed：识别不出身份（known=False）→ 始终不允许；管理员始终允许；
    已识别但未配置账号走 acl_default_online（默认 False）。
    """
    if not user or not user.get("known"):
        return False
    if user.get("is_admin"):
        return True
    allow = _acl().get(user.get("guid", ""))
    if allow is None:
        return acl_default_online()
    # 已配置账号：用其授权里的特殊标记 "__online__" 决定
    return "__online__" in allow
