#!/usr/bin/env python3
"""剥离后冒烟：验证公开构建暂存树能启动、能跑通兼容测试。

用法：
    python3 tools/post_strip_smoke.py <剥离后的源码树根目录>

CI 里由 public job 在 ``build.sh --public --stage-only`` 之后、正式打包之前
调用；目标目录是暂存树 ``packaging/fpk/.build/fpk/app/repo``，不是当前仓库。
任何一项失败都以非零退出码结束，让 job 在打包前停下。

检查项：
  1. 必需文件在，被剥离的私有文件/目录不在；
  2. 特性开关全关（红线开关仍在）；
  3. 子进程里 import app 成功、关键路由在、被移出标记块的通用件可用
     （源优先级、目录树 token、聚合库 guid、原生开关）；
  4. 子进程里跑 pytest 兼容子集。

兼容子集的排除项（按构造不兼容，不是按“这次没过”）：
  - tests/test_public_strip.py：它 import 已剥离的 tools/ 剥离器；
  - tests/test_verify_tls.py：它 import 已剥离的网盘模块；
  - 名含 dduo_probe_source 的两个测试：它们直接调已剥离的私有源探测函数。

注意：本文件故意不出现任何残留扫描的原样关键字（针列表见剥离器源码），
否则它一旦被拷进剥离树就会误报——所有敏感字面量都由拼接构成。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

# 剥离后必须还在的文件。
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
]

# 剥离后必须不在的文件/目录（敏感片段用拼接写，避免撞上残留扫描的针）。
_D = "dduo"
_STRIPPED = [
    "proxy/dduo.py",
    "proxy/catvod.py",
    "proxy/drpy.py",
    "proxy/guard.py",
    "proxy/spiders.py",
    "proxy/xlib.py",
    "proxy/pan.py",
    "proxy/panuc.py",
    "proxy/panali.py",
    "proxy/catvod.local.json",
    "proxy/runner_" + "client.py",
    "proxy/assets/" + _D + "tv.wasm",
    "proxy/assets" + "/cat" + "vod",
    "proxy/assets" + "/dr" + "py",
    "proxy/assets/guard/" + "guard.jar",
    "proxy/assets" + "/spid" + "ers",
    "proxy/_vendor",
    "spider-" + "runner",
    "tools",
]

_FLAGS_OFF = ("DDUO", "ADULT", "CATVOD", "DRPY", "GUARD")

# import app 的最小启动验证（在子进程里跑，与当前解释器隔离）。
_IMPORT_CHECK = """
import app as appmod
import providers

assert appmod.features.GUARD is False
routes = {getattr(r, "path", None) for r in appmod.app.routes}
for p in ("/v/api/v1/search/list", "/v/api/v1/item/list",
          "/v/api/v1/mediadb/list"):
    assert p in routes, p
assert len(routes) >= 50, len(routes)
# 被移出标记块的通用件：剥离后必须还在且行为正确。
assert appmod._gn_folder_token("fnmedia-gnfolder:abc") == "abc"
assert appmod._gn_folder_token("plain-guid") is None
assert appmod.guard_native_enabled() is False
assert appmod.GUARD_NATIVE_GUID
assert providers.source_priority({"id": "ffzy"}) == 45
assert providers.source_priority({"id": "no-such-source"}) == 0
print("[smoke] import app ok, routes:", len(routes))
"""

# 剥离树上能跑的测试子集（见模块 docstring 的排除说明）。
_PYTEST_CMD = [
    "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
    "--ignore=tests/test_public_strip.py",
    "--ignore=tests/test_verify_tls.py",
    "-k", "not dduo_probe_source",
]


def _failures(root: Path) -> list[str]:
    bad: list[str] = []
    for rel in _REQUIRED:
        if not (root / rel).is_file():
            bad.append(f"缺必需文件: {rel}")
    for rel in _STRIPPED:
        if (root / rel).exists():
            bad.append(f"应剥离却还在: {rel}")
    spec = importlib.util.spec_from_file_location(
        "smoke_features", root / "proxy" / "features.py")
    if spec is None or spec.loader is None:
        bad.append("读不到 proxy/features.py")
        return bad
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in _FLAGS_OFF:
        if getattr(mod, name, None) is not False:
            bad.append(f"特性开关未关: {name}={getattr(mod, name, None)!r}")
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
        print("[smoke] FAIL: pytest 兼容子集未通过")
        return 1
    print("[smoke] pytest 兼容子集通过")
    print("[smoke] ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
