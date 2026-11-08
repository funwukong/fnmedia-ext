#!/usr/bin/env python3
"""可选特性开关（单源码 / 构建期剥离）。

本仓库维护一份源码，产出两种构建：

* **完整构建**：含全部能力（DDUO / ADULT / CATVOD 等可选特性开启）。
* **公开构建**：由 `tools/public_strip.py` 在打包时删除标记块与相关专有
  文件，并把下面的开关区块改写为关闭态；产物中不含这些特性的任何代码
  或第三方资产。

硬红线（涉未成年人性化、非自愿内容的永久屏蔽）**不属于**可剥离特性，
公开构建同样保留。

代码用两种方式处理可选逻辑：

1. 整段自洽的独立语句，用注释包起来，剥离器按行删除：

       # >>> PRIVATE:DDUO
       ...
       # <<< PRIVATE:DDUO

2. 不便删块的分支，用运行时开关门控（公开构建里常量为 False）：

       if features.DDUO:
           ...
"""
from __future__ import annotations

# 完整构建：两个可选特性均开启。剥离器会把这一区块改写为 False。
# >>> PRIVATE:FEATURE_FLAGS
DDUO = True
ADULT = True
# catvod JVM 爬虫运行时（TVBox type=3 纯 dex jar，如小雅 AListSh）
CATVOD = True
# drpyS JS 爬虫运行时（Node 跑 TVBox type=3 的 drpy2 .js 站点）
DRPY = True
# DexGuard/ftyguard 加固 TVBox 源（csp_*Guard），只能在真实 Android 运行时
# 解密执行；主插件经外置 spider-runner 的 android 后端 HTTP 调用。
GUARD = True
# <<< PRIVATE:FEATURE_FLAGS

# 无论何种构建都恒为真的安全红线开关（仅用于语义清晰，不参与剥离）
REDLINE = True


def names() -> tuple[str, ...]:
    return ("DDUO", "ADULT", "CATVOD", "DRPY", "GUARD")
