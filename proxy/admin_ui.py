#!/usr/bin/env python3
"""管理页 HTML（v1.2.8 外置）。

以前整页 HTML 以三引号字符串内联在本模块里（2900 行、几乎整个文件），
现在改为构建/运行时从 ``proxy/assets/admin.html`` 读取一次并缓存：
- 编辑页面不必再动 Python；
- 语法高亮/格式化工具可用；
- 模块本身只剩加载逻辑，ETag/gzip 仍由 admin_api 负责。

读取失败时回退一个极简错误页（不会让管理页 500）。
"""
from __future__ import annotations

from pathlib import Path

_ASSET = Path(__file__).resolve().parent / "assets" / "admin.html"

_FALLBACK = (
    "<!doctype html><meta charset=utf-8><title>fnmedia-ext</title>"
    "<p>管理页资源缺失：proxy/assets/admin.html（请重新安装扩展）。"
)


def load_admin_html() -> str:
    try:
        return _ASSET.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return _FALLBACK


ADMIN_HTML = load_admin_html()
