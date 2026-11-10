#!/usr/bin/env python3
"""完整构建冒烟：验证 full 构建暂存树能启动、能跑通核心测试子集。

用法：
    python3 tools/full_build_smoke.py <完整构建的源码树根目录>

CI 里由 full job 在 ``build.sh --stage-only`` 之后、正式打包之前调用；
目标目录是暂存树 ``packaging/fpk/.build/fpk/app/repo``，不是当前仓库。
任何一项失败都以非零退出码结束，让 job 在打包前停下。

与 ``tools/post_strip_smoke.py``（公开构建用）的区别——不要直接复用它：
完整构建不剥离，所以这里断言的方向正好相反：私有模块与剥离器必须
**在**，特性开关必须**全开**；剥离版脚本断言“私有文件不在 / 开关全关”，
在完整构建上跑它会立刻误报失败。

检查项：
  1. 必需文件在，完整构建才有的私有模块与构建工具也在；
  2. 特性开关全开（DDUO/ADULT/CATVOD/DRPY/GUARD 均为 True，红线仍在）；
  3. 子进程里 import app 成功、关键路由在、通用件可用
     （目录树 token、原生加固开关、源优先级）；
  4. 子进程里跑 pytest 核心子集。

核心子集（快，约 2 秒，27 个测试；选它而不跑全量 414 个的理由）：
  - 全量测试已由 test job 在 full/public 之前跑过（needs: test），这里
    只需抓“组装/暂存过程弄坏了树”的问题，不必重跑全部；
  - 五个文件覆盖启动链的关键面：路由注册、配置加载、源解析、
    关联作品、T4 冒烟；任一文件缺失或损坏都会在这里失败。
  - 排除网络/真机类测试（test_verify_tls 等）：冒烟只验证本地树完整，
    不依赖外部服务。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

# 完整构建必须还在的文件：通用必需件 + 私有模块 + 构建工具。
_REQUIRED = [
    "proxy/app.py",
    "proxy/features.py",
    "proxy/providers.py",
    "proxy/config.py",
    "proxy/store.py",
    "proxy/charts.py",
    "proxy/t4.py",
    "requirements.txt",
    "VERSION",
    # 完整构建才有的私有模块（公开构建会被剥离，这里必须在）。
    "proxy/dduo.py",
    "proxy/catvod.py",
    "proxy/drpy.py",
    "proxy/guard.py",
    "proxy/spiders.py",
    "proxy/xlib.py",
    "proxy/pan.py",
    "proxy/panuc.py",
    "proxy/panali.py",
    # 构建工具本身也随完整包进暂存树（公开构建才删 tools/）。
    "tools/public_strip.py",
    "tools/post_strip_smoke.py",
]

_FLAGS_ON = ("DDUO", "ADULT", "CATVOD", "DRPY", "GUARD")

# import app 的最小启动验证（在子进程里跑，与当前解释器隔离）。
_IMPORT_CHECK = """
import app as appmod
import providers

assert appmod.features.DDUO is True
assert appmod.features.ADULT is True
assert appmod.features.CATVOD is True
assert appmod.features.DRPY is True
assert appmod.features.GUARD is True
routes = {getattr(r, "path", None) for r in appmod.app.routes}
for p in ("/v/api/v1/search/list", "/v/api/v1/item/list",
          "/v/api/v1/mediadb/list",
          "/v/api/ext/continue", "/v/api/ext/follow",
          "/v/api/ext/source-health"):
    assert p in routes, p
assert len(routes) >= 50, len(routes)
# 通用件：完整构建里加固开关默认开、目录树 token 与源优先级行为正确。
assert appmod._gn_folder_token("fnmedia-gnfolder:abc") == "abc"
assert appmod._gn_folder_token("plain-guid") is None
assert appmod.guard_native_enabled() is True
assert appmod.GUARD_NATIVE_GUID
assert providers.source_priority({"id": "ffzy"}) == 45
assert providers.source_priority({"id": "no-such-source"}) == 0
print("[smoke] import app ok, routes:", len(routes))
"""

# 完整树上能跑的核心测试子集（见模块 docstring 的选择理由）。
_PYTEST_CMD = [
    "-m", "pytest",
    "tests/test_p2_app_routes.py",
    "tests/test_config.py",
    "tests/test_providers_parse.py",
    "tests/test_related.py",
    "tests/test_t4_smoke.py",
    "-q", "-p", "no:cacheprovider",
]


def _failures(root: Path) -> list[str]:
    bad: list[str] = []
    for rel in _REQUIRED:
        if not (root / rel).is_file():
            bad.append(f"缺必需文件: {rel}")
    spec = importlib.util.spec_from_file_location(
        "smoke_features", root / "proxy" / "features.py")
    if spec is None or spec.loader is None:
        bad.append("读不到 proxy/features.py")
        return bad
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in _FLAGS_ON:
        if getattr(mod, name, None) is not True:
            bad.append(f"特性开关未开: {name}={getattr(mod, name, None)!r}")
    if getattr(mod, "REDLINE", None) is not True:
        bad.append("红线开关丢失或被改")
    return bad


def _run(cmd: list[str], cwd: Path, timeout: int, env_extra: dict) -> tuple[int, str]:
    import os
    env = dict(os.environ)
    env.update(env_extra)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT"
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).resolve()
    if not root.is_dir():
        print(f"[smoke] FAIL: 目标目录不存在: {root}")
        return 2

    bad = _failures(root)
    for item in bad:
        print(f"[smoke] FAIL: {item}")
    if bad:
        return 1
    print("[smoke] 文件与开关检查通过")

    proxy_dir = str(root / "proxy")
    rc, out = _run([sys.executable, "-c", _IMPORT_CHECK], cwd=root,
                   timeout=180, env_extra={"PYTHONPATH": proxy_dir})
    print(out.strip() or "(无输出)")
    if rc != 0:
        print("[smoke] FAIL: import app 未通过")
        return 1
    print("[smoke] import app 检查通过")

    rc, out = _run([sys.executable, *_PYTEST_CMD], cwd=root,
                   timeout=600, env_extra={"PYTHONPATH": proxy_dir})
    tail = "\n".join(out.strip().splitlines()[-15:])
    print(tail or "(无输出)")
    if rc != 0:
        print("[smoke] FAIL: pytest 核心子集未通过")
        return 1
    print("[smoke] pytest 核心子集通过")
    print("[smoke] ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
