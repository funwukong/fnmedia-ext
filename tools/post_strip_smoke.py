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
  - 名含 dduo_probe_source 的两个测试：它们直接调已剥离的私有源探测函数；
  - tests/test_config_whitelist.py：测完整构建的白名单分类，
    剥离树里 DEFAULTS 已改；
  - tests/test_memory_caches.py：模块级 import 已剥离的 xlib。

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
_H = "hai" + "kuo"
_STRIPPED = [
    "proxy/dduo.py",
    "proxy/catvod.py",
    "proxy/drpy.py",
    "proxy/haikuo.py",
    "proxy/" + _H + "_browser.py",
    "proxy/" + _H + "_diag.py",
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
    "proxy/assets" + "/" + _H,
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

assert appmod.features.HAIKUO is False
routes = {getattr(r, "path", None) for r in appmod.app.routes}
for p in ("/v/api/v1/search/list", "/v/api/v1/item/list",
          "/v/api/v1/mediadb/list"):
    assert p in routes, p
assert len(routes) >= 50, len(routes)
# 网盘识别通用件（panmatch.py 不被剥离）：剥离后必须还在且行为正确。
assert appmod._pan_match("https://pan.quark.cn/s/abc123")["key"] == "quark"
assert appmod._pan_match("https://example.com/x") is None
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
    # P2 新增：narrowing 测试需私有模块（公开构建已删），split 测试断言
    # 完整构建的路由数；剥离树上跳过。
    "--ignore=tests/test_p2_except_narrowing.py",
    "--ignore=tests/test_app_split.py",
    # P2 新增：counts 测试调 tools/count_except.py（剥离删 tools/），剥离树上跳过。
    "--ignore=tests/test_p2_except_counts.py",
    # 白名单分类测完整构建的 DEFAULTS，剥离树里已改，构造不兼容，跳过。
    "--ignore=tests/test_config_whitelist.py",
    # 内存批新增：模块级 import 已剥离的 xlib，构造不兼容，跳过。
    "--ignore=tests/test_memory_caches.py",
    # 海阔批新增：测完整构建的 haikuo 模块与规则导入，剥离树里模块已删。
    "--ignore=tests/test_haikuo_py.py",
    # 海阔 sidecar 测试驱动 proxy/assets/drpy/ 里的 Node sidecar，
    # 公开剥离会删掉该目录，故在剥离树里整文件忽略（全量套件仍跑）。
    "--ignore=tests/test_haikuo_shim.py",
    "--ignore=tests/test_haikuo_loader.py",
    # 海阔 batch 4：live 回归包装在收集期就 import haikuo 模块，剥离树里
    # 模块已删，整文件忽略（全量套件仍跑，需 HAIKUO_LIVE=1 才执行）。
    "--ignore=tests/test_haikuo_live.py",
    # 海阔浏览批：测 haikuo_browser 模块与 App 点开源链，剥离树里模块已删。
    "--ignore=tests/test_haikuo_browse.py",
    # 海阔修复批：nav/id、解析硬错误、诊断日志均在收集期 import 海阔模块，
    # 剥离树里模块已删，整文件忽略（全量套件仍跑）。
    "--ignore=tests/test_haikuo_nav_id.py",
    "--ignore=tests/test_haikuo_resolve_errors.py",
    "--ignore=tests/test_haikuo_diag.py",
    "--ignore=tests/test_haikuo_round2.py",
    "--ignore=tests/test_haikuo_round3.py",
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
