#!/usr/bin/env bash
# 构建【完整版】fpk 并归档到本地专用目录（仓库外，绝不进 GitHub）。
#
# 完整版含非标准源 WASM 解码与独立库成年模式，仅供本机 / 自用 NAS，禁止外传。
# 用法：tools/build-local-archive.sh
# 可覆盖：FNM_LOCAL_ARCHIVE_DIR（归档目录）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY3="${PYTHON:-python3}"
ARC="${FNM_LOCAL_ARCHIVE_DIR:-/vol1/1000/Ai工作区/fnmedia-ext-local-fpk}"
VERSION="$(head -n1 "$ROOT/VERSION" | tr -d '[:space:]')"
[ -n "$VERSION" ] || { echo "ERROR: 读不到 VERSION" >&2; exit 1; }

mkdir -p "$ARC"
echo "[archive] 构建完整功能 fpk v${VERSION}（不剥离）…"
( cd "$ROOT" && PYTHON="$PY3" bash packaging/fpk/build.sh )

SRC="$ROOT/dist/fnmedia-ext-${VERSION}.fpk"
[ -f "$SRC" ] || { echo "ERROR: 未找到 $SRC" >&2; exit 1; }

# 安全闸：完整包里必须仍是完整特性（DDUO/ADULT=True、含 dduo.py、wasmtime）
STAGE="$ROOT/packaging/fpk/.build/fpk/app/repo"
if [ -f "$STAGE/proxy/features.py" ]; then
  grep -q '^DDUO = True' "$STAGE/proxy/features.py" || { echo "ERROR: 完整包 DDUO 不为 True，拒绝归档" >&2; exit 1; }
  grep -q '^ADULT = True' "$STAGE/proxy/features.py" || { echo "ERROR: 完整包 ADULT 不为 True，拒绝归档" >&2; exit 1; }
  [ -f "$STAGE/proxy/dduo.py" ] || { echo "ERROR: 完整包缺少 dduo.py，拒绝归档" >&2; exit 1; }
fi

DST="$ARC/fnmedia-ext-${VERSION}-full.fpk"
cp -f "$SRC" "$DST"
( cd "$ARC" && sha256sum "$(basename "$DST")" > "$(basename "$DST").sha256" )
echo "[archive] 已归档完整版：$DST"
