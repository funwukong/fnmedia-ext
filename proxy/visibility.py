#!/usr/bin/env python3
"""媒体库可见权限：纯判断逻辑，不碰网络、不读配置（调用方传 cfg 进来）。

模型：
- 配置键 library_visible_users：{库 guid: [用户名, ...]}。
- 某库没配、配了空列表、配的不是列表——全部视为“不限制”，所有人可见。
  默认行为与没这功能时完全一致。
- 配了非空列表后，只有名单里的用户能看到该库。
- 当前用户名为 None（身份识别不到）时一律放行：绝不因为识别失败
  把库锁死，这是 fail-open 设计。

用户名按原样精确匹配（飞牛用户名区分大小写，以官方 user/info
返回的为准）。
"""
from __future__ import annotations

MAX_USERS_PER_LIB = 100


def allowed_users_for(cfg: dict, guid: str) -> list[str] | None:
    """返回该库允许的用户名列表；None 表示未设限制（全部可见）。"""
    if not isinstance(cfg, dict) or not guid:
        return None
    mapping = cfg.get("library_visible_users")
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(guid)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    users = []
    for item in value:
        name = str(item).strip()
        if name and name not in users:
            users.append(name)
    return users or None


def restrictions_configured(cfg: dict) -> bool:
    """是否配了任何一条限制（配了才需要识别当前用户）。"""
    if not isinstance(cfg, dict):
        return False
    mapping = cfg.get("library_visible_users")
    if not isinstance(mapping, dict):
        return False
    return any(isinstance(v, list) and any(str(x).strip() for x in v)
               or isinstance(v, str) and v.strip()
               for v in mapping.values())


def is_visible(cfg: dict, guid: str, username: str | None) -> bool:
    """该用户能不能看到这个库。username 为 None（身份未知）时放行。"""
    allowed = allowed_users_for(cfg, guid)
    if allowed is None:
        return True
    if username is None:
        return True
    return username in allowed


def normalize_users(users) -> list[str]:
    """清洗管理页提交的用户名列表：去空白、去重、上限截断。"""
    if isinstance(users, str):
        users = [u for u in users.replace("，", ",").split(",")]
    if not isinstance(users, (list, tuple)):
        return []
    out = []
    for item in users:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out[:MAX_USERS_PER_LIB]
