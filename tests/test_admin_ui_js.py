"""管理台内联 JS 必须能解析。

2026-11-09 的教训：T4 移植时 renderSubResult 里 const t4 被声明了两次，
整个 <script> 解析失败，管理台一直停在“检测中…”、所有功能点不开，
而服务端接口（/v/api/ext/status）本身是好的。加这个测试，杜绝重蹈。
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
UI = os.path.join(ROOT, "proxy", "admin_ui.py")


def _script() -> str:
    src = open(UI, encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
    assert blocks, "admin_ui.py 里找不到内联 <script>"
    return "\n".join(blocks)


def test_admin_js_parses_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机无 node，跳过 JS 语法检查")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                    encoding="utf-8") as f:
        f.write(_script())
        path = f.name
    try:
        r = subprocess.run([node, "--check", path],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, f"管理台 JS 解析失败：\n{r.stderr}"


def test_no_stale_t4_not_connected_text():
    # T4 已接入，旧的“只显示、不接入”文案与标签必须清掉，否则管理台
    # 会误导用户以为 T4 不能用。
    src = open(UI, encoding="utf-8").read()
    assert "未接入 T4 协议" not in src
    assert "T4·未接入" not in src
