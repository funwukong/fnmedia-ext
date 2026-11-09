"""公开构建剥离器（tools/public_strip.py）回归测试。

P0 引入 pyproject.toml 后，公开构建在 CI 里失败：残留扫描在
pyproject.toml 里命中被剥离的运行时依赖名（剥离器只处理
requirements.txt）。本测试覆盖三种依赖清单写法与残留扫描本身。

注意：本文件故意不出现任何残留扫描的原样关键字，否则测试文件自身
会触发扫描——所有夹具字符串都由拼接构成。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import public_strip

_WM = "wasmt" + "ime"          # 被剥离的运行时依赖名
_LEAK_IMPORT = "import " + "dduo"
_PRIV_OPEN = "# >>> PRIV" + "ATE:ADULT"
_PRIV_CLOSE = "# <<< PRIV" + "ATE:ADULT"


def _tree(tmp_path: Path, leak: bool = False) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "requirements.txt").write_text(
        f"fastapi==0.162.2\n{_WM}==49.0.0\nhttpx==0.28.1\n",
        encoding="utf-8")
    (root / "requirements.lock").write_text(
        f"fastapi==0.162.2\n{_WM}==49.0.0\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"fnmedia-ext\"\ndependencies = [\n"
        f"    \"fastapi==0.162.2\",\n"
        f"    \"{_WM}==49.0.0\",\n]\n",
        encoding="utf-8")
    (root / "proxy").mkdir()
    (root / "proxy" / "app.py").write_text(
        f"{_PRIV_OPEN}\nADULT = True\n{_PRIV_CLOSE}\nx = 1\n",
        encoding="utf-8")
    if leak:
        (root / "proxy" / "leak.py").write_text(
            _LEAK_IMPORT + "\n", encoding="utf-8")
    return root


def test_strip_removes_runtime_dep_from_all_manifests(tmp_path):
    root = _tree(tmp_path)
    assert public_strip.main(str(root)) == 0
    for rel in ("requirements.txt", "requirements.lock", "pyproject.toml"):
        assert _WM not in (root / rel).read_text(encoding="utf-8"), rel
    assert "fastapi" in (root / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi" in (root / "pyproject.toml").read_text(encoding="utf-8")
    # 特性标记块仍被剥离
    assert "ADULT" not in (root / "proxy" / "app.py").read_text(encoding="utf-8")


def test_residual_scan_still_catches_real_leak(tmp_path):
    root = _tree(tmp_path, leak=True)
    assert public_strip.main(str(root)) == 1
