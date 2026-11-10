#!/usr/bin/env python3
"""校验 tag 与 VERSION 一致性：tag 名须为 ``v<VERSION>-fpk``。

用法：
    python3 tools/check_tag_version.py --tag v0.2.42-fpk
    python3 tools/check_tag_version.py            # 无 --tag 时读 GITHUB_REF_NAME

规则：
  - tag 必须匹配 ``^v(\\d+\\.\\d+\\.\\d+)-fpk$``，且括号里的版本与仓库根目录
    VERSION 文件第一行（去空白）完全一致；不一致或格式不对都以非零退出。
  - 触发方式的选择（写在 workflow 里的 step 条件上，本脚本只校验给定的 tag）：
    tag push 时校验真实 tag；workflow_dispatch 且 publish=true 时，workflow
    传入由 VERSION 派生的 tag（``v<VERSION>-fpk``），校验的是 VERSION 自身
    格式合法、能拼出合法 tag；dispatch 不发布时跳过本步（没有 tag 可校验，
    且本次不产生 Release，错 tag 不会流出）。

为什么不在 Release 步骤里才发现：Release 步骤用
``${GITHUB_REF_NAME:-v$(VERSION)-fpk}`` 拼 tag，tag 与 VERSION 不一致时，
错误版本的 FPK 会以错误的 tag 名发布出去；校验必须放在打包之前。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)-fpk$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=None,
                        help="待校验的 tag；缺省时读环境变量 GITHUB_REF_NAME")
    parser.add_argument("--version-file", default=None,
                        help="VERSION 文件路径；缺省为仓库根目录的 VERSION")
    args = parser.parse_args(argv)

    tag = args.tag or os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        print("[tag-check] SKIP: 未传入 tag 且无 GITHUB_REF_NAME（非 tag 触发，无需校验）")
        return 0

    repo_root = Path(__file__).resolve().parent.parent
    version_file = Path(args.version_file) if args.version_file else repo_root / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        print(f"[tag-check] FAIL: 读不到 VERSION: {exc}")
        return 1
    if not version:
        print("[tag-check] FAIL: VERSION 为空")
        return 1

    match = _TAG_RE.match(tag)
    if not match:
        print(f"[tag-check] FAIL: tag 格式不对: {tag!r}（应为 v<VERSION>-fpk，"
              f"如 v{version}-fpk）")
        return 1
    if match.group(1) != version:
        print(f"[tag-check] FAIL: tag 与 VERSION 不一致: tag={tag!r} "
              f"（版本 {match.group(1)}）≠ VERSION={version!r}，"
              f"期望 tag=v{version}-fpk")
        return 1
    print(f"[tag-check] OK: tag={tag!r} 与 VERSION={version!r} 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
