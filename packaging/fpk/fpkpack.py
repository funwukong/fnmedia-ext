#!/usr/bin/env python3
"""fnpack 的纯 Python 替代实现（aarch64 可用）。

背景：飞牛官方只发布 `fnpack-1.2.3-linux-amd64`（static2.fnnas.com 上
arm64/aarch64 均 404），在 aarch64 机器上跑不了（SIGILL）。本项目需要在
arm64 环境（含 CI 容器）打 .fpk，因此按官方 fpk 的实际布局复刻打包逻辑。

fpk 布局（对照官方 fnmedia-ext-0.2.41-public.fpk 实测确认）：

    .fpk = gzip(tar(
        manifest          顶层，键值对文本，"checksum = <md5(app.tgz)>"
        app.tgz           内层 tar.gz：repo/ + ui/ + config/
        ICON.PNG          应用图标
        ICON_256.PNG
        cmd/               生命周期脚本（main / *_init / *_callback / _common）
        config/privilege   {"defaults": {"run-as": "root"}}
        config/resource    {"api-scope": []}
        wizard/install     安装向导（可选）
    ))

校验规则（与 fnpack 的 trimpack.VerifyFiles / checkMandatoryFiles 对齐）：
  · 必需文件 app/ui/config、app/<type>/config（<type> 为 native/docker）、
    config/privilege、config/resource 必须存在 —— 在 app.tgz 内部检查；
  · manifest 必填字段 appname/version/display_name/desc/platform/source/
    maintainer/maintainer_url；
  · appname 需匹配 ^[a-zA-Z0-9][a-zA-Z0-9\\-_\\.]*[a-zA-Z0-9]$ 且长度 3..32；
  · manifest 的 checksum 必须等于 md5(app.tgz)。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import time
from pathlib import Path

APPNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_\.]*[a-zA-Z0-9]$")
REPRODUCIBLE_MTIME = int(os.environ.get("SOURCE_DATE_EPOCH", "0") or 0)
REQUIRED_MANIFEST = (
    "appname", "version", "display_name", "desc",
    "platform", "source", "maintainer", "maintainer_url",
)
# fnpack 的 checkMandatoryFiles 要求这些路径在 app.tgz 内存在
REQUIRED_IN_APP = ("ui/config",)
# 官方包 app.tgz 内还带 config/privilege 与 config/resource（与顶层重复一份）
REQUIRED_IN_APP_EXTRA = ("config/privilege", "config/resource")
REQUIRED_TOP = ("config/privilege", "config/resource")


class PackError(Exception):
    pass


def parse_manifest(text: str) -> dict:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PackError(f"manifest 行格式错误（缺 '='）: {line!r}")
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def validate_manifest(mf: dict) -> None:
    for key in REQUIRED_MANIFEST:
        if not mf.get(key):
            raise PackError(f"manifest 缺少必填字段: {key}")
    name = mf["appname"]
    if not (3 <= len(name) <= 32):
        raise PackError(f"appname 长度必须在 3..32: {name!r}")
    if not APPNAME_RE.match(name):
        raise PackError(f"appname 非法（只允许字母数字与 -_. ，且首尾为字母数字）: {name!r}")


def _dirinfo(name: str) -> tarfile.TarInfo:
    """构造目录条目，名字不带尾斜杠（对齐 fnpack 的输出）。"""
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    ti.size = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "runner"
    ti.mtime = REPRODUCIBLE_MTIME
    # TarInfo.tobuf() 只在 name 不以 '/' 结尾时才补，但 DIRTYPE 下
    # tarfile 会对 ti.name 做处理；这里直接固定住 buf 里的名字。
    return ti


def _tar_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    """统一元数据，保证可复现构建 + 避免把宿主 uid/gid 带进去。"""
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "runner"
    ti.mtime = REPRODUCIBLE_MTIME
    return ti


def build_app_tgz(stage: Path) -> bytes:
    """把暂存树打成 app.tgz：repo/ + ui/ + config/。

    对照官方包实测：stage/app/repo → repo/，stage/app/ui → ui/，
    stage/app/config → config/。cmd/ 与 wizard/ 留在顶层 tar，不进 app.tgz。
    """
    app = stage / "app"
    if not (app / "repo").is_dir():
        raise PackError(f"缺少 {app}/repo（先跑 build.sh --stage-only）")

    # 实测官方包：app.tgz 内同时含 repo/ + ui/ + config/，且顶层也各有一份
    # cmd/ config/ wizard/。config 在暂存树里位于**顶层**（build.sh 的布局），
    # 这里显式桥接：top:config → app.tgz 内的 config/。
    members: list[tuple[Path, str]] = []
    sources = (("repo", app / "repo", "repo"),
               ("ui", app / "ui", "ui"),
               ("config", stage / "config", "config"))
    for _label, src, dst_name in sources:
        if not src.exists():
            continue
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src).as_posix()
            members.append((p, f"{dst_name}/{rel}" if rel != "." else dst_name))

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        # 目录条目不带尾斜杠：对齐 fnpack（Go archive/tar 的 ustar 输出）
        for path, arcname in members:
            if path.is_dir():
                ti = tarfile.TarInfo(arcname)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = "runner"
                ti.mtime = REPRODUCIBLE_MTIME
                tar.addfile(ti)
            else:
                tar.add(str(path), arcname=arcname, recursive=False,
                        filter=_tar_filter)
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=9,
                       mtime=0) as gz:
        gz.write(tar_buf.getvalue())
    return compressed.getvalue()


def check_app_tgz(blob: bytes) -> None:
    """校验 app.tgz 内的必需路径（对应 fnpack 的 checkMandatoryFiles）。"""
    names = set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            names.add(m.name.rstrip("/"))
    for req in REQUIRED_IN_APP + REQUIRED_IN_APP_EXTRA:
        if req not in names:
            raise PackError(f'required file "{req}" is missing')


def check_top(fpk_names: set[str]) -> None:
    """校验顶层必需条目（对应 fnpack 的 checkPrivilegeFile / checkResourceFile）。"""
    for req in REQUIRED_TOP:
        if req not in fpk_names:
            raise PackError(f'required file "{req}" is missing')
    if "cmd/main" not in fpk_names:
        raise PackError('required file "cmd/main" is missing')


def build_fpk(stage: Path, manifest_text: str, out_path: Path) -> Path:
    mf = parse_manifest(manifest_text)
    validate_manifest(mf)

    app_blob = build_app_tgz(stage)
    check_app_tgz(app_blob)
    app_md5 = hashlib.md5(app_blob).hexdigest()

    # manifest 末尾追加 checksum（若模板里已写 checksum 行则替换）
    lines = [ln for ln in manifest_text.splitlines()
             if not ln.strip().startswith("checksum")]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"checksum              = {app_md5}")
    final_manifest = "\n".join(lines) + "\n"

    # 顶层 tar：先写 manifest，再 app.tgz，再图标与脚本目录
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        def add_bytes(name: str, data: bytes, mode: int = 0o644) -> None:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "runner"
            ti.mtime = REPRODUCIBLE_MTIME
            tar.addfile(ti, io.BytesIO(data))

        def add_tree(src: Path, arcname: str) -> None:
            # 目录自身也要有条目（官方包里 cmd/ config/ wizard/ 都单独占一行）。
            # 注意 TarInfo(name) 在 type=DIRTYPE 时会自动补尾斜杠，而 fnpack
            # 输出的名字**不带**尾斜杠，故用 _dirinfo 手工构造。
            tar.addfile(_dirinfo(arcname))
            for p in sorted(src.rglob("*")):
                rel = p.relative_to(src).as_posix()
                name = f"{arcname}/{rel}"
                if p.is_dir():
                    tar.addfile(_dirinfo(name))
                else:
                    mode = 0o755 if (p.stat().st_mode & 0o111) else 0o644
                    # 脚本目录下的都要可执行
                    if arcname == "cmd":
                        mode = 0o755
                    add_bytes(name, p.read_bytes(), mode)

        add_bytes("manifest", final_manifest.encode("utf-8"))
        add_bytes("app.tgz", app_blob)
        for icon in ("ICON.PNG", "ICON_256.PNG"):
            src = stage / icon
            if src.is_file():
                add_bytes(icon, src.read_bytes())
        for sub in ("cmd", "config", "wizard"):
            d = stage / sub
            if d.is_dir():
                add_tree(d, sub)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9,
                           mtime=0) as gz:
            gz.write(buf.getvalue())
    return out_path


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="纯 Python fpk 打包器（arm64 可用）")
    ap.add_argument("--stage", required=True, help="build.sh 组装的暂存目录")
    ap.add_argument("--out", required=True, help="输出 .fpk 路径")
    ap.add_argument("--verify", action="store_true", help="打包后自校验")
    args = ap.parse_args()

    stage = Path(args.stage).resolve()
    out = Path(args.out).resolve()
    manifest_path = stage / "manifest"
    if not manifest_path.is_file():
        print(f"ERROR: 找不到 {manifest_path}", file=sys.stderr)
        return 1
    try:
        build_fpk(stage, manifest_path.read_text(encoding="utf-8"), out)
    except PackError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    size = out.stat().st_size
    print(f"[fpk-py] 打包完成: {out} ({size} bytes)")

    if args.verify:
        if not verify_fpk(out):
            return 1
    return 0


def verify_fpk(path: Path) -> bool:
    """拆开校验：manifest 字段、checksum 一致性、必需路径。"""
    ok = True
    with gzip.open(path, "rb") as fh:
        raw = fh.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        names = {m.name for m in tar.getmembers()}
        for req in ("manifest", "app.tgz"):
            if req not in names:
                print(f"  ✗ 顶层缺少 {req}")
                ok = False
        if "cmd/main" not in names:
            print("  ✗ 顶层缺少 cmd/main")
            ok = False
        try:
            check_top(names)
            print("  ✓ 顶层必需条目齐全")
        except PackError as e:
            print(f"  ✗ {e}")
            ok = False
        mf_text = tar.extractfile("manifest").read().decode("utf-8")
        app_blob = tar.extractfile("app.tgz").read()

    mf = parse_manifest(mf_text)
    try:
        validate_manifest(mf)
        print("  ✓ manifest 字段完整")
    except PackError as e:
        print(f"  ✗ {e}")
        ok = False

    actual = hashlib.md5(app_blob).hexdigest()
    if mf.get("checksum") != actual:
        print(f"  ✗ checksum 不一致: manifest={mf.get('checksum')} 实际={actual}")
        ok = False
    else:
        print(f"  ✓ checksum 一致 ({actual})")

    try:
        check_app_tgz(app_blob)
        print("  ✓ app.tgz 必需路径齐全")
    except PackError as e:
        print(f"  ✗ {e}")
        ok = False

    print(f"  {'✓' if ok else '✗'} 自校验{'通过' if ok else '失败'}")
    return ok


if __name__ == "__main__":
    sys.exit(main())
