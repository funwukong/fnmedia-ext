#!/usr/bin/env bash
# ============================================================================
# redroid 轻量 Android 加固运行时 —— 一键部署（在目标 NAS 以 root 运行）
#
# 跨机一键部署（完整版 fpk）：
#   - 直接拉取 Docker Hub 上【公开、不含第三方私有 jar】的预构建镜像
#       <registry>/fnmedia-redroid:<tag>   （= redroid 11 + NDK ARM 翻译 + 自研 bridge）
#   - 第三方加固 jar 不内置、不进镜像；由用户在管理页上传（proxy/assets/guard/guard.jar）后灌入容器；
#   - 自动建持久容器、装/校验 bridge、下发加固 jar、拉起设备内 bridge，主插件随即连通。
#
# 覆盖：sudo FNM_REGISTRY=docker.io/<ns> FNM_IMAGE_TAG=x bash setup_redroid.sh
# 幂等。仅完整版随包（公开构建整体删除 spider-runner/）。
# ============================================================================
set -euo pipefail

REGISTRY="${FNM_REGISTRY:-fnmedia}"
TAG="${FNM_IMAGE_TAG:-latest}"
IMAGE="${FNM_REDROID_IMAGE:-$REGISTRY/fnmedia-redroid:$TAG}"
# 拉不到预构建镜像时的基础回退（只有 Android，无内置 bridge，下面会用本仓 APK 补装）
FALLBACK_IMAGE="${FNM_REDROID_BASE:-erstt/redroid:11.0.0_ndk_ChromeOS}"
CONTAINER="${FNM_REDROID_CONTAINER:-fnmedia-redroid}"
NET="${FNM_SPIDER_NET:-fnmedia-spider}"
MEM="${FNM_REDROID_MEMORY:-2g}"
HPORT="${FNM_BRIDGE_PORT:-9110}"
# 常驻（默认）：开机/异常退出自动回来；用户在管理页手动停止后不拉起
AUTOSTART="${FNM_REDROID_AUTOSTART:-1}"
RP="unless-stopped"; [ "$AUTOSTART" = "0" ] && RP="no"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$RUNNER_ROOT/.." && pwd)"
LOCAL_APK="$HERE/build/catvod-bridge.apk"
JAR="$REPO_ROOT/proxy/assets/guard/guard.jar"
DATA_DIR="${FNM_REDROID_DATA:-$REPO_ROOT/state/redroid-data}"
BRIDGE_PKG="com.fnmedia.catvodbridge"
BRIDGE_ACT="$BRIDGE_PKG/com.fnmedia.bridge.BootActivity"

c_ok(){ printf '\033[32m[✓]\033[0m %s\n' "$1"; }
c_warn(){ printf '\033[33m[!]\033[0m %s\n' "$1"; }
c_err(){ printf '\033[31m[✗]\033[0m %s\n' "$1" >&2; }
need(){ command -v "$1" >/dev/null 2>&1; }

echo "== redroid 部署（镜像:$IMAGE 容器:$CONTAINER 内存:$MEM）=="
need docker || { c_err "缺少 docker（fnOS 应用中心安装 Docker）"; exit 1; }

# --------------------------------------------------------------- 1) binderfs
if [ ! -e /dev/binderfs/binder-control ]; then
  echo "-- 挂载 binderfs"
  mkdir -p /dev/binderfs
  mount -t binder binder /dev/binderfs \
    || { c_err "binderfs 挂载失败：内核需 CONFIG_ANDROID_BINDERFS=y"; exit 1; }
fi
c_ok "binderfs 就绪"
if need systemctl; then
  cat > /etc/systemd/system/fnmedia-binderfs.service <<'UNIT'
[Unit]
Description=Mount binderfs for fnmedia redroid Android containers
DefaultDependencies=no
After=systemd-tmpfiles-setup-dev.service
Before=docker.service
ConditionPathExists=/sys/module/binder
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mkdir -p /dev/binderfs
ExecStart=/bin/mount -t binder binder /dev/binderfs
ExecStop=/bin/umount /dev/binderfs
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable fnmedia-binderfs.service >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------- 2) 镜像
IMAGE_BUILTIN_APK=0
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  c_ok "镜像已在本机：$IMAGE"
else
  echo "-- 拉取预构建镜像 $IMAGE"
  if docker pull "$IMAGE"; then
    c_ok "已拉取预构建镜像（含内置 bridge）"
  else
    c_warn "预构建镜像不可达，回退基础镜像 $FALLBACK_IMAGE（将用 fpk 自带 APK 补装 bridge）"
    IMAGE="$FALLBACK_IMAGE"
    docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE"
  fi
fi

# --------------------------------------------------------------- 3) 容器
# 共享网络：容器化 runner 经固定名 http://fnmedia-redroid:9101 访问；
# 本机 systemd runner 仍走 127.0.0.1:9110。网络缺失则建（幂等）。
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR"

# 现有容器若指向不同镜像（例如升级后从旧基础镜像切到本项目镜像），
# 用目标镜像重建。/data 是独立 bind 卷，重建不丢已装 bridge / 数据。
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  CUR_IMG="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)"
  if [ -n "$CUR_IMG" ] && [ "$CUR_IMG" != "$IMAGE" ]; then
    echo "-- 检测到容器镜像 $CUR_IMG 与目标 $IMAGE 不一致，用新镜像重建（数据卷 $DATA_DIR 保留）"
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker rm "$CONTAINER" >/dev/null 2>&1 || true
  fi
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "-- 创建持久容器（数据卷 $DATA_DIR，接入网络 $NET）"
  docker run -itd --privileged --name "$CONTAINER" --restart="$RP" \
    -m "$MEM" -p "127.0.0.1:$HPORT:9101" \
    --network "$NET" --network-alias android \
    --dns "${FNM_REDROID_DNS:-223.5.5.5}" --dns "${FNM_REDROID_DNS2:-119.29.29.29}" \
    -v "$DATA_DIR:/data" \
    "$IMAGE" \
    androidboot.redroid_gpu_mode=guest \
    androidboot.redroid_width=720 androidboot.redroid_height=1280 >/dev/null
else
  docker update --restart=no "$CONTAINER" >/dev/null
  # 老容器可能未接网络，补接（幂等，已接入则忽略错误）
  docker network connect --alias android "$NET" "$CONTAINER" >/dev/null 2>&1 || true
  docker start "$CONTAINER" >/dev/null || true
fi
# 容器存在但当前停止（预置后默认停止）也确保运行以完成首装
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true   || docker start "$CONTAINER" >/dev/null 2>&1 || true
c_ok "容器已启动（预置）"

# --------------------------------------------------------------- 4) 开机
echo "-- 等待 Android 开机…"
for i in $(seq 1 60); do
  [ "$(docker exec "$CONTAINER" getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 1
done
[ "$(docker exec "$CONTAINER" getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] \
  || { c_err "redroid 开机超时"; exit 1; }
c_ok "Android 已开机"

# --------------------------------------------------------------- 5) bridge
# 优先镜像内只读层自带的 APK（/fnmedia/catvod-bridge.apk）；否则用 fpk 自带 build APK。
APK_IN_CONTAINER=""
if docker exec "$CONTAINER" sh -c 'test -f /fnmedia/catvod-bridge.apk' >/dev/null 2>&1; then
  APK_IN_CONTAINER="/fnmedia/catvod-bridge.apk"
  echo "-- 使用镜像内置 bridge APK"
elif [ -f "$LOCAL_APK" ]; then
  docker cp "$LOCAL_APK" "$CONTAINER:/data/local/tmp/bridge.apk"
  docker exec "$CONTAINER" chmod 644 /data/local/tmp/bridge.apk
  APK_IN_CONTAINER="/data/local/tmp/bridge.apk"
  echo "-- 已拷入 fpk 自带 bridge APK"
else
  c_warn "无可用 bridge APK（镜像无 /fnmedia/catvod-bridge.apk，仓内也无 build APK）"
fi
if [ -n "$APK_IN_CONTAINER" ] && ! docker exec "$CONTAINER" sh -c "pm path $BRIDGE_PKG" 2>/dev/null | grep -q package:; then
  docker exec "$CONTAINER" pm install -r "$APK_IN_CONTAINER" >/dev/null
  c_ok "bridge App 已安装"
elif [ -n "$APK_IN_CONTAINER" ]; then
  docker exec "$CONTAINER" pm install -r "$APK_IN_CONTAINER" >/dev/null 2>&1 || true
  c_ok "bridge App 已就绪"
fi

# 第三方私有加固 jar：仅从完整版 fpk 目录灌入（公开镜像里绝不含它）。
if [ -f "$JAR" ]; then
  docker cp "$JAR" "$CONTAINER:/data/local/tmp/spider.jar"
  docker exec "$CONTAINER" sh -c \
    'D=/data/data/com.fnmedia.catvodbridge/files/jars;mkdir -p $D;cp /data/local/tmp/spider.jar $D/guard.jar;chmod 666 $D/guard.jar'
  c_ok "加固 jar 已下发（用户自备，不进镜像、不进安装包）"
else
  c_warn "未找到自备加固 jar（$JAR）；容器与 bridge 已就绪，请在管理页「加固执行 jar（自备）」上传"
fi

# 拉起设备内 bridge（App 不自启）
docker exec "$CONTAINER" am start -n "$BRIDGE_ACT" >/dev/null 2>&1 || true
ready=0
for i in $(seq 1 25); do
  if curl -s --noproxy '*' --max-time 3 http://127.0.0.1:$HPORT/ping 2>/dev/null | grep -q '"ok"'; then
    c_ok "bridge 健康：http://127.0.0.1:$HPORT"; ready=1; break
  fi
  docker exec "$CONTAINER" am start -n "$BRIDGE_ACT" >/dev/null 2>&1 || true
  sleep 1
done
[ "$ready" = 1 ] || c_warn "bridge 暂未响应，稍后在管理页点“启动运行时”即可"
# 默认常驻：预置后保持运行，Docker 重启策略 unless-stopped 会在宿主重启/异常退出后自动回来；
# 管理页“停止运行时”会写停止标记并把重启策略改为 no，手动停止后不再自动拉起。
if [ "$AUTOSTART" = "0" ]; then
  docker update --restart=no "$CONTAINER" >/dev/null 2>&1 || true
  docker stop "$CONTAINER" >/dev/null 2>&1 || true
  echo "已预置并停止（FNM_REDROID_AUTOSTART=0 手动模型，0 常驻内存）；需要时管理页点“启动运行时”"
else
  docker update --restart=unless-stopped "$CONTAINER" >/dev/null 2>&1 || true
  echo "已预置并保持运行（常驻）。宿主重启/异常退出会自动回来；不需要时管理页点“停止运行时”。"
fi
echo "完成。常驻模式：管理页「TVBox 加固」可手动停止/启动，或 docker start/stop $CONTAINER"
