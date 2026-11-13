#!/usr/bin/env bash
# ============================================================================
# redroid 多实例“并发池”一键部署（在目标 NAS 以 root 运行）。
#
# 背景：单台 redroid 内加固 jar 调用必须串行，豆豆片单播放扇出各资源站只能
# 一个个来（3~9s）。多起几台互相独立的 redroid，runner（android_pool）会把
# 并发调用分到不同设备，播放扇出做“同时竞速、谁先回用谁”。
#
# 用法：
#   sudo bash setup_redroid_pool.sh [实例数，默认2]
#   FNM_POOL_SIZE=3 sudo bash setup_redroid_pool.sh
#
# 资源：每实例约 0.6~2GB 内存（-m 2g 上限）、约 3GB 磁盘；i5/16G 起建议 2~3 台。
# 幂等：重复执行只会补齐缺失实例、刷新 bridge/jar 与 runner 的池地址配置。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
N="${1:-${FNM_POOL_SIZE:-2}}"
BASE_PORT="${FNM_POOL_BASE_PORT:-9110}"   # 实例1宿主端口；后续 +1
MEM="${FNM_REDROID_MEMORY:-2g}"

echo "== redroid 并发池：部署 $N 个实例（起始宿主端口 $BASE_PORT，单实例内存 $MEM）=="

URLS=()
for i in $(seq 1 "$N"); do
  IDX=$((i-1))
  PORT=$((BASE_PORT + IDX))
  if [ "$i" -eq 1 ]; then
    NAME="fnmedia-redroid"                 # 与既有单实例同名，零迁移
  else
    NAME="fnmedia-redroid$i"
  fi
  DATA="${FNM_REDROID_DATA_BASE:-/vol2/docker}/redroid-fnmedia-data$i"
  echo "---- 实例 $i：$NAME（宿主 127.0.0.1:$PORT，数据 $DATA）----"
  FNM_REDROID_CONTAINER="$NAME" \
  FNM_BRIDGE_PORT="$PORT" \
  FNM_REDROID_DATA="$DATA" \
  FNM_REDROID_MEMORY="$MEM" \
  FNM_REDROID_AUTOSTART=1 \
    bash "$HERE/setup_redroid.sh"
  URLS+=("http://127.0.0.1:$PORT")
done

# 把全部实例地址下发给 runner（逗号分隔），android_pool 据此建池。
DROPIN_DIR=/etc/systemd/system/spider-runner.service.d
mkdir -p "$DROPIN_DIR"
IFS=,; URL_LINE="${URLS[*]}"; unset IFS
cat > "$DROPIN_DIR/android-pool.conf" <<EOF
[Service]
Environment=ANDROID_BRIDGE_URLS=$URL_LINE
EOF
systemctl daemon-reload
systemctl restart spider-runner
sleep 3
echo "== runner 池配置：ANDROID_BRIDGE_URLS=$URL_LINE =="
curl -s --noproxy '*' --max-time 6 http://127.0.0.1:9100/health \
  | (command -v python3 >/dev/null && python3 -m json.tool || cat) || true
echo "完成。并发池实例数=$N。"
