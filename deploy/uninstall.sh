#!/usr/bin/env bash
# fnmedia-ext 卸载：停止并移除单元；supervise 会原子换回官方 socket。
# 用法：sudo bash deploy/uninstall.sh [--purge]
#   --purge 同时删除运行配置与缓存（state/），不影响官方任何数据
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

systemctl stop fnmedia-ext 2>/dev/null || true
systemctl disable fnmedia-ext.service 2>/dev/null || true
rm -f /etc/systemd/system/fnmedia-ext.service
systemctl daemon-reload || true

sleep 1
if [ -S /var/run/trim_media.sock ] && [ ! -e /var/run/trim_media_upstream.sock ]; then
  echo "[OK] 已还原官方 trim_media.sock，影视恢复原样。"
else
  echo "[WARN] 请确认官方 socket 已恢复：ls -l /var/run/trim_media*.sock" >&2
fi

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$ROOT/state"
  echo "[OK] 已删除运行配置与缓存 $ROOT/state"
fi
