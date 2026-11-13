# spider-runner：统一 TVBox 爬虫运行时（JVM 纯dex + Node drpyS）。
# 加固 jar 的 Android 运行时是独立的 redroid 容器（见 docker-compose.yml 的 android
# 服务），本镜像只提供 JVM + Node，不含 Android、不含任何第三方私有加固 jar。
#
# 基础：Temurin JRE21 + Node 20（jammy）。纯标准库 Python 仅做进程编排。
# 构建上下文必须为仓库根：docker build -f spider-runner/Dockerfile -t <runner 镜像> .
FROM eclipse-temurin:21-jre-jammy

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates python3 xz-utils \
 && curl -fsSL https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.xz \
      | tar -xJ -C /opt \
 && ln -sf /opt/node-v20.18.1-linux-x64/bin/node /usr/local/bin/node \
 && ln -sf /opt/node-v20.18.1-linux-x64/bin/npm /usr/local/bin/npm \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/runner
COPY spider-runner/app/ /srv/runner/app/

# 只内置【引擎】，不烤任何站点资产/第三方 jar：catvod/drpy 资产在运行时从完整版
# fpk 目录只读挂载到 /srv/catvod、/srv/drpy（见 docker-compose.yml）。
# 公开镜像因此保持中性、可匿名分发；未挂载时对应后端自动登记为不可用（503）。
RUN mkdir -p /srv/catvod /srv/drpy

ENV CATVOD_ASSET=/srv/catvod \
    DRPY_ASSET=/srv/drpy \
    JAVA_BIN=java \
    NODE_BIN=node \
    SIDECAR_IDLE_SECONDS=0 \
    JVM_MAX_HEAP=256m \
    NODE_MAX_OLD=192 \
    RUNNER_HOST=0.0.0.0 \
    RUNNER_PORT=9100 \
    ANDROID_BRIDGE_URL=http://android:9101

EXPOSE 9100
# 解析 /health 的 ok 字段：curl 只看 HTTP 200 会把“全后端挂掉但服务在跑”
# 也判为健康。ok 为三者（java/node/android）至少其一可用。
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python3 -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:9100/health',timeout=4)); raise SystemExit(0 if d.get('ok') else 1)"

CMD ["python3", "/srv/runner/app/server.py"]
