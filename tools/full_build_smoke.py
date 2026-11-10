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

公共逻辑（文件/开关检查骨架、子进程运行器、冒烟主流程）在
``tools/smoke_common.py``；方向性期望（文件清单、开关开/关、
import 检查、pytest 子集）由本文件传入，见 ``main()``。

检查项：
  1. 必需文件在，完整构建才有的私有模块与构建工具也在；
  2. 特性开关全开（DDUO/ADULT/CATVOD/DRPY/GUARD 均为 True，红线仍在）；
  3. 子进程里 import app 成功、关键路由在、通用件可用
     （目录树 token、原生加固开关、源优先级）；
  4. 子进程里跑 pytest 核心子集。

核心子集（快，约 3 秒，96 个测试；选它而不跑全量 490+ 个的理由）：
  - 全量测试已由 test job 在 full/public 之前跑过（needs: test），这里
    只需抓“组装/暂存过程弄坏了树”的问题，不必重跑全部；
  - 十个文件覆盖启动链的关键面：路由注册、配置加载、源解析、
    关联作品、T4 冒烟，以及三方合入的新测试——libfilter（22）、
    搜索预算（7）、慢线预算（8）、library_visibility（21）、
    管理台 P1 回归（8）；任一文件缺失或损坏都会在这里失败。
  - 排除网络/真机类测试（test_verify_tls 等）：冒烟只验证本地树完整，
    不依赖外部服务。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smoke_common  # noqa: E402

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
    "tools/smoke_common.py",
]

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
    # 三方合入的新测试：libfilter（22）、搜索预算（7）、
    # 慢线预算（8）、library_visibility（21）。
    "tests/test_libfilter.py",
    "tests/test_search_budget_non_t4.py",
    "tests/test_slow_line_budget.py",
    "tests/test_library_visibility.py",
    # 管理台 P1 回归（16/22）。
    "tests/test_admin_ui_p1.py",
    # P0+P1 八分支合入的新测试（slowci / perf / cleanup / subpan /
    # config / search / failclosed；剥离方向见 post_strip_smoke，
    # 它跑整棵 tests/，新文件自动覆盖）。
    "tests/test_build_workflow.py",
    "tests/test_p1_catalog_deepfetch.py",
    "tests/test_p1_fnusers_lock.py",
    "tests/test_p1_store_dirty.py",
    "tests/test_p1_log_noise.py",
    "tests/test_pan_shared.py",
    "tests/test_p1_pan_cleanup.py",
    "tests/test_p1_xml_cms.py",
    "tests/test_poster.py",
    "tests/test_p1_config_items.py",
    "tests/test_p0p1_search_agg_cache.py",
    "tests/test_p0p1_search_catalog_route.py",
    "tests/test_p0p1_search_viewer_snapshot.py",
    "tests/test_p0_early_return_visible.py",
    "tests/test_p0_playchain_favorites.py",
    "tests/test_p0p1_resume_favorite.py",
    "-q", "-p", "no:cacheprovider",
]


def main(argv: list[str]) -> int:
    return smoke_common.main(
        argv,
        required=_REQUIRED,
        flags_expect=True,
        import_check=_IMPORT_CHECK,
        pytest_cmd=_PYTEST_CMD,
        pytest_label="pytest 核心子集",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
