#!/usr/bin/env python3
"""两冒烟脚本的公共逻辑：文件/开关检查骨架与 pytest 子集运行器。

``tools/full_build_smoke.py``（完整构建）与
``tools/post_strip_smoke.py``（公开构建）共用本模块。断言期望全部由
调用方传入，本模块不写死任何方向：
  - 文件：调用方给“必须在”与“必须不在”两份清单；
  - 开关：调用方给期望（True=全开 / False=全关），红线开关始终要求 True；
  - import 检查代码与 pytest 子集也由调用方传入。
剥离版断言“私有文件不在 / 开关全关”，完整版断言正好相反，抽公共时
绝不能把方向写死在一处。

注意：本文件与两调用脚本一样，不得出现残留扫描的原样关键字
（针列表见 tools/public_strip.py），否则一旦被拷进剥离树就会误报。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_FLAGS = ("DDUO", "ADULT", "CATVOD", "DRPY", "GUARD")


def check_files(root: Path, required=(), forbidden=()) -> list[str]:
    """必需文件在、应剥离文件不在；返回问题清单（空表示通过）。"""
    bad: list[str] = []
    for rel in required:
        if not (root / rel).is_file():
            bad.append(f"缺必需文件: {rel}")
    for rel in forbidden:
        if (root / rel).exists():
            bad.append(f"应剥离却还在: {rel}")
    return bad


def check_flags(root: Path, flags=_FLAGS, expect: bool = True) -> list[str]:
    """特性开关期望：expect=True 要求全开，False 要求全关。

    红线开关不参与方向参数，始终要求 True。
    """
    spec = importlib.util.spec_from_file_location(
        "smoke_features", root / "proxy" / "features.py")
    if spec is None or spec.loader is None:
        return ["读不到 proxy/features.py"]
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bad: list[str] = []
    for name in flags:
        if getattr(mod, name, None) is not expect:
            bad.append(
                f"特性开关{'未开' if expect else '未关'}: "
                f"{name}={getattr(mod, name, None)!r}")
    if getattr(mod, "REDLINE", None) is not True:
        bad.append("红线开关丢失或被改")
    return bad


def run(cmd: list[str], cwd: Path, timeout: int,
        env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(env_extra)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT"
    return proc.returncode, proc.stdout + proc.stderr


def run_smoke(root: Path, *, required=(), forbidden=(),
              flags_expect: bool = True, import_check: str,
              pytest_cmd: list[str],
              pytest_label: str = "pytest 子集") -> int:
    """文件/开关检查 → 子进程 import app → pytest 子集；任一步失败返回 1。"""
    bad = check_files(root, required, forbidden)
    bad += check_flags(root, expect=flags_expect)
    for item in bad:
        print(f"[smoke] FAIL: {item}")
    if bad:
        return 1
    print("[smoke] 文件与开关检查通过")

    proxy_dir = str(root / "proxy")
    rc, out = run([sys.executable, "-c", import_check], cwd=root,
                  timeout=180, env_extra={"PYTHONPATH": proxy_dir})
    print(out.strip() or "(无输出)")
    if rc != 0:
        print("[smoke] FAIL: import app 未通过")
        return 1
    print("[smoke] import app 检查通过")

    rc, out = run([sys.executable, *pytest_cmd], cwd=root,
                  timeout=600, env_extra={"PYTHONPATH": proxy_dir})
    tail = "\n".join(out.strip().splitlines()[-15:])
    print(tail or "(无输出)")
    if rc != 0:
        print(f"[smoke] FAIL: {pytest_label}未通过")
        return 1
    print(f"[smoke] {pytest_label}通过")
    print("[smoke] ALL OK")
    return 0


def main(argv: list[str], *, required=(), forbidden=(),
         flags_expect: bool = True, import_check: str,
         pytest_cmd: list[str],
         pytest_label: str = "pytest 子集") -> int:
    """冒烟脚本入口：argv 为 ``[脚本名, 目标源码树根目录]``。"""
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).resolve()
    if not root.is_dir():
        print(f"[smoke] FAIL: 目标目录不存在: {root}")
        return 2
    return run_smoke(root, required=required, forbidden=forbidden,
                     flags_expect=flags_expect, import_check=import_check,
                     pytest_cmd=pytest_cmd, pytest_label=pytest_label)
