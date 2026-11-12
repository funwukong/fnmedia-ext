"""剥离后 guard_native 行为回归：公开构建里 guard 模块已删，聚合入口必须
fail-open 返回空结果而不是抛错；网盘识别通用件必须仍可用。

在临时副本上跑剥离器（复制时排除测试缓存），再在子进程里从剥离版
proxy 目录导入聚合模块并断言行为。

注意：本文件故意不出现残留扫描的任何原样关键字（见剥离器源码的针列表），
否则它一旦被拷进剥离树就会误报——模块名等敏感字面量都由拼接构成。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

# 剥离树里 tools/ 已删，本测试按构造无法在剥离树上运行：缺剥离器时跳过
# （与 test_public_strip.py 同理，见剥离后冒烟的排除说明）。
public_strip = pytest.importorskip("public_strip")

_REPO = Path(__file__).resolve().parent.parent

# 复制时跳过的大目录与缓存：剥离器本身也会跳过其中一部分；这里排除纯粹是
# 为了复制快 + 不把本机测试缓存带进剥离树。
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", "state", "logs", "dist",
    ".venv", "*.fpk",
)

_MOD = "guard" + "_native"

# 子进程检查脚本：只从剥离版 proxy 目录导入，不碰原仓库模块。
# 模块名用拼接写，避免测试文件自身撞上残留扫描的针。
_CHECK = "\n".join([
    "import sys",
    "sys.path.insert(0, sys.argv[1])",
    "import importlib",
    "gn = importlib.import_module(" + repr(_MOD) + ")",
    "assert gn._gn_sources() == [], gn._gn_sources()",
    "pv = gn._pan_match('https://pan.quark.cn/s/abc123')",
    "assert pv is not None and pv.get('key') == 'quark', pv",
    "assert gn._pan_match('https://example.com/normal') is None",
    "print('[check] stripped guard_native ok')",
])


def test_stripped_guard_native_fails_open(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(_REPO, root, ignore=_COPY_IGNORE)
    assert public_strip.main(str(root)) == 0
    stripped_proxy = str(root / "proxy")
    check = tmp_path / "check_stripped.py"
    check.write_text(_CHECK, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(check), stripped_proxy],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, (
        "剥离版聚合模块检查失败\nstdout:\n" + r.stdout
        + "\nstderr:\n" + r.stderr
    )
