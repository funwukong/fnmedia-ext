#!/usr/bin/env bash
# 把【完整构建】部署到本机 NAS 的 fnmedia-ext 安装副本并重启。
# 约定：新版完整构建只部署到本地，不推送 GitHub；公开发布用 publish-public.sh。
#
# 用法：tools/deploy-local.sh [--no-restart]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${FNM_DEST:-/vol2/@appcenter/fnmedia-ext/repo}"
SERVICE="${FNM_SERVICE:-fnmedia-ext.service}"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

[ -d "$DEST" ] || { echo "ERROR: 部署目录不存在: $DEST" >&2; exit 1; }
VERSION="$(head -n1 "$ROOT/VERSION" | tr -d '[:space:]')"

echo "[deploy] $ROOT (v$VERSION) -> $DEST （完整构建，仅本地）"
BK="/tmp/fnmedia-deploy-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK/proxy"
# 备份将被覆盖的现有文件
if [ -d "$DEST/proxy" ]; then
  cp -a "$DEST/proxy" "$BK/proxy.prev" 2>/dev/null || true
fi
echo "[deploy] 备份: $BK"

# 同步完整源码（保留 NAS 上的 .venv / state / logs / dist）
rsync -a \
  --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='state/' --exclude='logs/' --exclude='dist/' --exclude='.release/' \
  --exclude='.DS_Store' \
  "$ROOT/proxy/" "$DEST/proxy/"
cp "$ROOT/VERSION" "$DEST/VERSION"
# requirements 若有变化则补装（完整构建含 wasmtime 等）
if [ -f "$DEST/.venv/bin/pip" ]; then
  "$DEST/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" || true
fi
find "$DEST/proxy" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# 部署后冒烟：所有源码可被语法解析
PYBIN="$DEST/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="python3"
"$PYBIN" - "$DEST/proxy" <<'EOF'
import ast, glob, sys
d = sys.argv[1]
for f in glob.glob(d + "/*.py"):
    ast.parse(open(f).read())
print("[deploy] 语法检查通过：", len(glob.glob(d + "/*.py")), "个文件")
EOF

if [ "$RESTART" -eq 1 ]; then
  systemctl restart "$SERVICE"
  sleep 3
  if [ "$(systemctl is-active "$SERVICE")" = "active" ]; then
    echo "[deploy] $SERVICE 已重启，状态 active（v$VERSION）"
  else
    echo "[deploy] ERROR: $SERVICE 未处于 active，见 journalctl -u $SERVICE" >&2
    exit 1
  fi
else
  echo "[deploy] 已同步文件，未重启（--no-restart）"
fi
