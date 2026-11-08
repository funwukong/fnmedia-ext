#!/usr/bin/env bash
# ============================================================================
# 构建并（可选）发布 fnmedia-ext 外挂运行时公开镜像到 Docker Hub。
#
# 镜像（均【不含】第三方私有加固 jar / 凭证；公开可匿名拉取）：
#   <REG>/fnmedia-spider-runner:<TAG>   JVM21+Node20 引擎（~476MB），
#                                       catvod/drpy 资产运行时从 fpk 只读挂载
#   <REG>/fnmedia-redroid:<TAG>         redroid11 + NDK ARM 翻译 + 自研 bridge APK（~2.15GB）
#
# 用法：
#   REGISTRY=<dockerid> TAG=latest bash tools/publish-containers.sh build   # 仅本机构建
#   REGISTRY=<dockerid> TAG=latest bash tools/publish-containers.sh push    # 构建并推送
#   REGISTRY=<dockerid> bash tools/publish-containers.sh build-redroid      # 只构建其中之一
#
# 推送前需在宿主机 `docker login -u <dockerid>`（口令不写进任何文件/镜像）。
# 仅完整版源码树使用；公开构建会剥离 spider-runner 与本脚本引用的资产。
# ============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REG="${REGISTRY:-fnmedia}"
TAG="${TAG:-latest}"
REDROID_BASE="${REDROID_BASE:-erstt/redroid:11.0.0_ndk_ChromeOS}"
ACTION="${1:-build}"
RUNNER_IMG="$REG/fnmedia-spider-runner:$TAG"
REDROID_IMG="$REG/fnmedia-redroid:$TAG"

build_runner() {
  echo "== build $RUNNER_IMG（引擎，无站点资产）=="
  docker build -f "$ROOT/spider-runner/Dockerfile" -t "$RUNNER_IMG" "$ROOT"
}
build_redroid() {
  echo "== build $REDROID_IMG（redroid + 自研 bridge，无私有 jar）=="
  docker build -f "$ROOT/spider-runner/android/Dockerfile.redroid" \
    --build-arg REDROID_BASE="$REDROID_BASE" -t "$REDROID_IMG" "$ROOT/spider-runner/android"
}
push_runner()  { docker push "$RUNNER_IMG"; }
push_redroid() { docker push "$REDROID_IMG"; }

case "$ACTION" in
  build)         build_runner; build_redroid ;;
  push)          build_runner; build_redroid; push_runner; push_redroid ;;
  build-runner)  build_runner ;;
  build-redroid) build_redroid ;;
  push-runner)   build_runner; push_runner ;;
  push-redroid)  build_redroid; push_redroid ;;
  *) echo "未知动作: $ACTION（build|push|build-runner|build-redroid|push-*）" >&2; exit 1 ;;
esac
echo "完成。目标机一键部署：FNM_REGISTRY=$REG FNM_IMAGE_TAG=$TAG bootstrap_sidecar.sh all"
