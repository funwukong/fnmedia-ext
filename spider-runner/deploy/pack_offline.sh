#!/usr/bin/env bash
# ============================================================================
# 打包「重运行时 sidecar」离线安装包 —— 目标 NAS 无需访问 Docker Hub。
#
# 产物（默认 dist/sidecar-offline/）：
#   images/redroid.tar.gz          erstt/redroid:11.0.0_ndk_ChromeOS 镜像（含 ARM 翻译）
#   images/spider-runner.tar.gz    （可选，--with-runner）JVM+Node runner 镜像
#   assets/catvod-bridge.apk       设备内 bridge
#   assets/guard.jar               内置兼容加固 jar
#   install.sh                     目标机离线安装入口
#   MANIFEST.txt                   版本/大小/校验
#
# 在【已联网、已 docker pull 好 redroid 镜像】的机器上执行一次，把整个目录拷到目标 NAS：
#   sudo bash pack_offline.sh                 # 只打加固源必需（redroid 镜像+apk+jar）
#   sudo bash pack_offline.sh --with-runner   # 额外打 JVM+Node runner 镜像（全离线）
#
# 注意体积：redroid 镜像 save|gzip 约 900MB–1.2GB，需要足够磁盘与拷贝带宽。
# 仅完整版包含。
# ============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$RUNNER_ROOT/.." && pwd)"

WITH_RUNNER=0
for a in "$@"; do
  case "$a" in
    --with-runner) WITH_RUNNER=1 ;;
  esac
done
OUT="${OUT:-$REPO_ROOT/dist/sidecar-offline}"
REDROID_IMAGE="${FNM_REDROID_IMAGE:-erstt/redroid:11.0.0_ndk_ChromeOS}"
RUNNER_IMAGE="fnmedia/spider-runner:latest"
APK="$RUNNER_ROOT/android/build/catvod-bridge.apk"
JAR="$REPO_ROOT/proxy/assets/guard/guard.jar"

mkdir -p "$OUT/images" "$OUT/assets"

echo "== [1/4] redroid 镜像（$REDROID_IMAGE）-> images/redroid.tar.gz =="
if docker image inspect "$REDROID_IMAGE" >/dev/null 2>&1; then
  docker save "$REDROID_IMAGE" | gzip -1 > "$OUT/images/redroid.tar.gz"
else
  echo "  本机没有 $REDROID_IMAGE，请先：docker pull $REDROID_IMAGE" >&2; exit 1
fi

if [ "$WITH_RUNNER" -eq 1 ]; then
  echo "== [2/4] runner 镜像 -> images/spider-runner.tar.gz =="
  if docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1; then
    docker save "$RUNNER_IMAGE" | gzip -1 > "$OUT/images/spider-runner.tar.gz"
  else
    echo "  构建 $RUNNER_IMAGE …"
    ( cd "$RUNNER_ROOT" && docker compose build runner )
    docker save "$RUNNER_IMAGE" | gzip -1 > "$OUT/images/spider-runner.tar.gz"
  fi
else
  echo "== [2/4] 跳过 runner 镜像（目标机用 systemd 跑本机 Python；需要时加 --with-runner）"
fi

rm -rf "$OUT/sysimg"
echo "== [3/4] APK / 加固 jar =="
[ -f "$APK" ] || { echo "缺少 $APK，请先在 android/ 构建 bridge" >&2; exit 1; }
cp -f "$APK" "$OUT/assets/catvod-bridge.apk"
if [ -f "$JAR" ]; then cp -f "$JAR" "$OUT/assets/guard.jar"; else
  echo "  [warn] 未找到内置 guard.jar，离线包将不含 jar"; fi

echo "== [4/4] 安装入口 / 清单 =="
cat > "$OUT/install.sh" <<EOF
#!/usr/bin/env bash
# 离线安装入口：在目标 NAS 上以 root 运行。
#   sudo bash install.sh                # 全量：runner(systemd) + redroid
#   sudo bash install.sh runner-only    # 只装 runner
#   sudo bash install.sh android-only   # 只部署 redroid
set -euo pipefail
D="\$(cd "\$(dirname "\$0")" && pwd)"
ARG="\${1:-all}"
# 找到随插件落地的 spider-runner
SR="\${RUNNER_HINT:-}"
[ -z "\$SR" ] && for c in "\$D/spider-runner" \\
    /vol2/@appcenter/fnmedia-ext/repo/spider-runner \\
    "\${TRIM_APPDEST:-}/repo/spider-runner"; do
  [ -f "\$c/deploy/bootstrap_sidecar.sh" ] && SR="\$c" && break
done
[ -n "\$SR" ] || { echo "找不到 spider-runner/deploy/bootstrap_sidecar.sh，请设 RUNNER_HINT=<spider-runner 目录>" >&2; exit 1; }

# 1) 导入 redroid 镜像（幂等）
if [ -f "\$D/images/redroid.tar.gz" ] && ! docker image inspect $REDROID_IMAGE >/dev/null 2>&1; then
  echo "[offline] 载入 redroid 镜像…"; gunzip -c "\$D/images/redroid.tar.gz" | docker load
fi
# 2) runner 镜像（可选）
if [ -f "\$D/images/spider-runner.tar.gz" ] && ! docker image inspect fnmedia/spider-runner:latest >/dev/null 2>&1; then
  echo "[offline] 载入 runner 镜像…"; gunzip -c "\$D/images/spider-runner.tar.gz" | docker load
  export MODE=docker
fi
export OFFLINE_BUNDLE_DIR="\$D"
exec bash "\$SR/deploy/bootstrap_sidecar.sh" "\$ARG"
EOF
chmod +x "$OUT/install.sh"

{
  echo "fnmedia-ext sidecar offline bundle (redroid)"
  echo "built_at=$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "with_runner=$WITH_RUNNER"
  echo "redroid_image=$REDROID_IMAGE"
  echo "arch=$(uname -m)"
  echo
  ( cd "$OUT" && find . -type f -exec ls -l {} \; | awk '{printf "%-40s %s\n",$NF,$5}' )
  echo
  echo "sha256:"
  ( cd "$OUT" && find . -type f ! -name MANIFEST.txt -print0 | sort -z | xargs -0 sha256sum )
} > "$OUT/MANIFEST.txt"

echo "完成：$OUT"
du -sh "$OUT"
