#!/usr/bin/env bash
# fnmedia-ext 安装：渲染 systemd 单元（路径自适应）并启用可逆接管。
# 用法：sudo bash deploy/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
SRC_IN="$ROOT/deploy/fnmedia-ext.service.in"
DST="/etc/systemd/system/fnmedia-ext.service"

if [ ! -x "$PY" ]; then
  echo "ERROR: 未找到虚拟环境 $PY；请先：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# 停掉可能存在的旧 transient/持久同名单元（supervise 会自动还原官方 socket）
systemctl stop fnmedia-ext 2>/dev/null || true

# 渲染绝对路径
sed -e "s#@ROOT@#$ROOT#g" "$SRC_IN" > "$DST"
chmod 0644 "$DST"
systemctl daemon-reload
systemctl enable fnmedia-ext.service
systemctl restart fnmedia-ext.service

sleep 6
if systemctl is-active --quiet fnmedia-ext; then
  echo "[OK] fnmedia-ext 已安装并接管运行（ROOT=$ROOT）"
  echo "[OK] 管理页：https://<飞牛地址>/v/online-admin"
  echo "[OK] 卸载/还原：bash $ROOT/deploy/uninstall.sh"
else
  echo "ERROR: 服务未正常启动；journalctl -u fnmedia-ext -n 50" >&2
  exit 1
fi
