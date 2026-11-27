"""源质量标签 / 排序模式 / 分类排除 / 镜像 —— 后端回归。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import catalog  # noqa: E402


def test_sort_mode_and_order_whitelisted():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '"source_sort_mode"' in src
    assert '"source_user_order"' in src
    # 校验：非法 mode 回落 auto；user order 只保留已知源 id
    assert 'mode if mode in ("auto", "user") else "auto"' in src
    assert "known = {str(x.get" in src


def test_source_quality_endpoint_exists():
    src = (ROOT / "proxy" / "admin_api.py").read_text(encoding="utf-8")
    assert '@admin_router.get("/v/api/ext/source-quality")' in src
    # 标签映射齐全
    for label in ("流畅", "偶发卡顿", "易失效", "未知", "可用"):
        assert f'"{label}"' in src
    # 分数夹取
    assert "max(0, min(100" in src


def test_user_sort_wired_in_aggregate():
    src = (ROOT / "proxy" / "app.py").read_text(encoding="utf-8")
    assert 'config.read("source_sort_mode", "auto") == "user"' in src
    assert "user_order.get(g[\"rep\"].provider)" in src
    # user 模式下未列出的源用大序号兜底（排后）
    assert "len(user_order) + key[0]" in src


def test_defaults_present():
    import sys
    sys.path.insert(0, str(ROOT / "proxy"))
    import config as cfgmod
    assert cfgmod.DEFAULTS["source_sort_mode"] == "auto"
    assert cfgmod.DEFAULTS["source_user_order"] == []
    assert cfgmod.DEFAULTS["poster_mirror_hosts"] == []
    assert cfgmod.DEFAULTS["catalog_cat_exclude"] == {}


def test_ui_sort_and_drag_present():
    html = (ROOT / "proxy" / "assets" / "admin.html").read_text(encoding="utf-8")
    assert 'id="sortMode"' in html
    assert "initSrcDrag" in html
    assert "applyUserOrder" in html
    assert 'draggable="true"' in html
    assert "qbadge" in html
    # 外观设置卡
    assert 'id="layoutCompact"' in html
    assert 'id="hideScore"' in html
