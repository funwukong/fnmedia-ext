#!/usr/bin/env python3
"""剥离后冒烟：验证公开构建暂存树能启动、能跑通兼容测试。

用法：
    python3 tools/post_strip_smoke.py <剥离后的源码树根目录>

CI 里由 public job 在 ``build.sh --public --stage-only`` 之后、正式打包之前
调用；目标目录是暂存树 ``packaging/fpk/.build/fpk/app/repo``，不是当前仓库。
任何一项失败都以非零退出码结束，让 job 在打包前停下。

公共逻辑（文件/开关检查骨架、子进程运行器、冒烟主流程）在
``tools/smoke_common.py``；方向性期望（文件清单、开关开/关、
import 检查、pytest 子集）由本文件传入，见 ``main()``。剥离版断言
“私有文件不在 / 开关全关”，与完整版脚本方向相反，绝不能混用。

检查项：
  1. 必需文件在，被剥离的私有文件/目录不在；
  2. 特性开关全关（红线开关仍在）；
  3. 子进程里 import app 成功、关键路由在、被移出标记块的通用件可用
     （源优先级、目录树 token、聚合库 guid、原生开关）；
  4. 子进程里跑 pytest 兼容子集。

兼容子集的排除项（按构造不兼容，不是按“这次没过”）：
  - tests/test_public_strip.py：它 import 已剥离的 tools/ 剥离器；
  - tests/test_verify_tls.py：它 import 已剥离的网盘模块；
  - tests/test_p1_pan_cleanup.py、tests/test_pan_shared.py：同上，
    测的是已剥离的网盘模块（P1-8/P1-15），公开构建里无此模块可 import；
  - 名含 dduo_probe_source 的两个测试：它们直接调已剥离的私有源探测函数。

注意：本文件故意不出现任何残留扫描的原样关键字（针列表见剥离器源码），
否则它一旦被拷进剥离树就会误报——所有敏感字面量都由拼接构成。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smoke_common  # noqa: E402

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
    "--ignore=tests/test_p1_pan_cleanup.py",
    "--ignore=tests/test_pan_shared.py",
    "-k", "not dduo_probe_source",
]


def main(argv: list[str]) -> int:
    return smoke_common.main(
        argv,
        required=_REQUIRED,
        forbidden=_STRIPPED,
        flags_expect=False,
        import_check=_IMPORT_CHECK,
        pytest_cmd=_PYTEST_CMD,
        pytest_label="pytest 兼容子集",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
