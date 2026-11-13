#!/usr/bin/env bash
# ============================================================================
# fnmedia-ext 外挂爬虫运行时 —— 一键引导（在【目标 NAS】上以 root 运行）
#
# 跨机一键部署：镜像均发布在 Docker Hub（公开、不含第三方私有 jar），
# 私有 jar/凭证只从本机【完整版 fpk】目录读取，绝不进公开镜像。
#
# 做的事：
#   1) runner：MODE=auto（默认）时探测宿主 java/node —— 都有则用 systemd 跑本机
#      Python 编排（零额外镜像）；缺任一则自动 docker compose 拉预构建 runner 容器。
#   2) redroid 轻量 Android（约0.7–0.9GB、冷启10–30秒）：拉公开镜像（内置自研
#      bridge），建持久容器，再从 fpk 目录灌入私有加固 jar 并自动拉起 bridge。
#   3) 自检 java/node/android；主插件同机默认即连通 http://127.0.0.1:9100。
#
# 用法：
#   sudo bootstrap_sidecar.sh                  # 全量（runner 自动 + redroid）
#   sudo bootstrap_sidecar.sh runner-only      # 只要 JVM+Node
#   sudo bootstrap_sidecar.sh android-only     # 只部署 redroid
#   MODE=systemd|docker|auto                   # 强制 runner 形态（默认 auto）
#   FNM_REGISTRY=<dockerid> FNM_IMAGE_TAG=...  # 覆盖镜像仓库/标签
#
# 仅完整版包含本脚本（公开构建整体删除 spider-runner/）。
# ============================================================================
set -euo pipefail

STEP="${1:-all}"
RUNNER_MODE="${MODE:-auto}"            # auto | systemd | docker
RUNNER_PORT="${RUNNER_PORT:-9100}"
REGISTRY="${FNM_REGISTRY:-fnmedia}"
TAG="${FNM_IMAGE_TAG:-latest}"
export FNM_REGISTRY="$REGISTRY" FNM_IMAGE_TAG="$TAG"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER_ROOT="$(cd "$HERE/.." && pwd)"   # spider-runner/
REPO_ROOT="$(cd "$RUNNER_ROOT/.." && pwd)"
# venv 探测：包安装目录（TRIM_APPDEST/repo）→ 常见安装路径 → 开发树自带
VENV_PY=""
for _v in "${TRIM_APPDEST:-}/repo/.venv/bin/python" \
          "/vol2/@appcenter/fnmedia-ext/repo/.venv/bin/python" \
          "$REPO_ROOT/.venv/bin/python"; do
  [ -x "$_v" ] && VENV_PY="$_v" && break
done
JAR="$REPO_ROOT/proxy/assets/guard/guard.jar"
export FNM_GUARD_JAR="$JAR"
# 容器形态：把 fpk 内站点资产只读挂进 runner（不进公开镜像）；缺目录则挂空（后端登记不可用）
[ -d "$REPO_ROOT/proxy/assets/catvod" ] && export FNM_CATVOD_ASSET="$REPO_ROOT/proxy/assets/catvod" || true
[ -d "$REPO_ROOT/proxy/assets/drpy" ]   && export FNM_DRPY_ASSET="$REPO_ROOT/proxy/assets/drpy"   || true
[ -f "$REPO_ROOT/proxy/catvod.local.json" ] && export FNM_CATVOD_LOCAL="$REPO_ROOT/proxy/catvod.local.json" || true

c_ok(){ printf '\033[32m[✓]\033[0m %s\n' "$1"; }
c_warn(){ printf '\033[33m[!]\033[0m %s\n' "$1"; }
c_err(){ printf '\033[31m[✗]\033[0m %s\n' "$1" >&2; }
need(){ command -v "$1" >/dev/null 2>&1; }

# 宿主 java/node 探测（systemd 形态需要二者）
JAVA_BIN="$(command -v java || true)"
NODE_BIN="$(command -v node || true)"
[ -z "$NODE_BIN" ] && [ -x /var/apps/nodejs_v24/target/bin/node ] && NODE_BIN=/var/apps/nodejs_v24/target/bin/node

choose_runner_mode() {
  case "$RUNNER_MODE" in
    systemd) echo systemd ;;
    docker)  echo docker ;;
    auto)
      if [ -n "$JAVA_BIN" ] && [ -n "$NODE_BIN" ]; then echo systemd
      elif need docker; then echo docker
      else echo none; fi ;;
    *) echo none ;;
  esac
}
RESOLVED_MODE="$(choose_runner_mode)"

echo "== fnmedia-ext sidecar 引导（步骤:$STEP, runner:$RUNNER_MODE->$RESOLVED_MODE）=="

# ------------------------------------------------------------- 0) 依赖体检
dep_check() {
  local fail=0
  if [ "$STEP" != "runner-only" ] || [ "$RESOLVED_MODE" = docker ]; then
    need docker || { c_err "缺少 docker（fnOS 在应用中心安装 Docker）；容器形态与 redroid 都需要"; fail=1; }
  fi
  if [ "$STEP" != "runner-only" ] && [ ! -e /sys/module/binder ]; then
    c_warn "内核未见 binder 模块：redroid 需要 CONFIG_ANDROID_BINDERFS=y（fnOS 6.18 已内建）"
  fi
  [ "$fail" -eq 0 ] || exit 1
}

# ------------------------------------------------------------- 1) runner
install_runner_systemd() {
  echo "-- 安装/更新 spider-runner systemd 服务（java:$JAVA_BIN node:$NODE_BIN）"
  [ -x "$VENV_PY" ] || { c_err "找不到主插件虚拟环境（请先在 fnOS 应用中心完成插件安装）"; exit 1; }
  local unit="$HERE/spider-runner.service"
  sed -e "s#WorkingDirectory=.*#WorkingDirectory=$RUNNER_ROOT/app#" \
      -e "s#ExecStart=.*python.*#ExecStart=$VENV_PY -u server.py#" \
      -e "s#CATVOD_ASSET=.*#CATVOD_ASSET=$REPO_ROOT/proxy/assets/catvod#" \
      -e "s#DRPY_ASSET=.*#DRPY_ASSET=$REPO_ROOT/proxy/assets/drpy#" \
      -e "s#^Environment=NODE_BIN=.*#Environment=NODE_BIN=$NODE_BIN#" \
      "$unit" > /etc/systemd/system/spider-runner.service
  # 若宿主 java 不在 PATH（systemd 环境最小化），显式写入 JAVA_BIN
  grep -q '^Environment=JAVA_BIN=' /etc/systemd/system/spider-runner.service \
    || sed -i "/^Environment=NODE_BIN=/a Environment=JAVA_BIN=$JAVA_BIN" /etc/systemd/system/spider-runner.service
  systemctl daemon-reload
  systemctl enable --now spider-runner.service
  sleep 2
}

install_runner_docker() {
  echo "-- 用预构建镜像启动 runner 容器（JVM+Node，自动 pull $REGISTRY/fnmedia-spider-runner:$TAG）"
  need docker || { c_err "缺少 docker"; exit 1; }
  ( cd "$RUNNER_ROOT" && docker compose pull runner && docker compose up -d runner )
}

# ------------------------------------------------------------- 2) android
install_android() {
  echo "-- 部署 redroid 轻量 Android 容器"
  local setup="$RUNNER_ROOT/android/setup_redroid.sh"
  [ -f "$setup" ] || { c_err "缺少 setup_redroid.sh（当前可能是公开/精简构建）"; exit 1; }
  bash "$setup"
}

# ------------------------------------------------------------- 3) 自检
healthcheck() {
  echo "-- 自检"
  local h
  h="$(curl -s --noproxy '*' --max-time 6 "http://127.0.0.1:$RUNNER_PORT/health" || true)"
  echo "   runner /health: ${h:-无响应}"
  if echo "$h" | grep -q '"android":true'; then
    c_ok "Android 加固运行时就绪"
  else
    c_ok "redroid 已预置并常驻（开机自启/异常自动拉起）；不需要时管理页点“停止并释放内存”"
  fi
  cat <<EOF

 主插件同机默认即连通（http://127.0.0.1:$RUNNER_PORT），无需配置。
 Android(redroid) 默认常驻：开机自启、异常退出自动拉起。不需要时管理页点“停止并释放内存”，或：
     docker stop  fnmedia-redroid   # 手动停止（停止后不再自启，直到再次启动）
     docker start fnmedia-redroid   # 重新启动（约10–30秒），并恢复常驻
EOF
}

dep_check
case "$STEP" in
  all)
    case "$RESOLVED_MODE" in
      docker)  install_runner_docker ;;
      systemd) install_runner_systemd ;;
      *) c_err "需要 docker，或宿主具备 java+node 才能跑 runner"; exit 1 ;;
    esac
    install_android ;;
  runner-only)
    case "$RESOLVED_MODE" in
      docker)  install_runner_docker ;;
      systemd) install_runner_systemd ;;
      *) c_err "需要 docker，或宿主具备 java+node 才能跑 runner"; exit 1 ;;
    esac ;;
  android-only)
    install_android ;;
  *) c_err "未知参数: $STEP（all|runner-only|android-only）"; exit 1 ;;
esac
healthcheck
