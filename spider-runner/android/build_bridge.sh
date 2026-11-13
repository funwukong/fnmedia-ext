#!/bin/bash
# 在 docker-android 容器内编译 fat bridge（Bridge + catvod 基类 + Logger 桩 + gson/okhttp 等）。
# 产物 build/bridge.jar（单 classes.dex），设备上由 app_process 加载。
set -euo pipefail
C="${1:-fnmedia-redroid}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CATVOD_SRC="$REPO/proxy/assets/catvod/src"
LIBDIR="$REPO/proxy/assets/catvod/lib"
STAGE=$(docker exec -u 0 "$C" mktemp -d /tmp/bridge.XXXXXX|tr -d '\r'); echo "stage=$STAGE"
trap 'docker exec -u 0 "$C" rm -rf "$STAGE" 2>/dev/null || true' EXIT
docker exec -u 0 "$C" mkdir -p "$STAGE/src/com/github/catvod/crawler" "$STAGE/src/com/github/catvod/spider" "$STAGE/cls" "$STAGE/dex" "$STAGE/lib"
docker cp "$HERE/src/." "$C:$STAGE/src"
docker cp "$CATVOD_SRC/com/github/catvod/crawler/." "$C:$STAGE/src/com/github/catvod/crawler/"
for j in gson okhttp okio orgjson kotlin; do f=$(ls "$LIBDIR"/$j*.jar|head -1); docker cp "$f" "$C:$STAGE/lib/$j.jar"; done
docker exec -u 0 "$C" chmod -R a+rX "$STAGE"
AJ=$(docker exec "$C" sh -c 'ls /opt/android/platforms/*/android.jar|head -1'|tr -d '\r')
D8=$(docker exec "$C" sh -c 'find /opt/android/build-tools -name d8|head -1'|tr -d '\r')
docker exec -u 0 "$C" sh -c "cd $STAGE && find src -name '*.java'>f.txt && \
  javac -source 8 -target 8 -cp '$AJ' -d cls @f.txt 2>/dev/null"
docker exec -u 0 "$C" sh -c "cd $STAGE && '$D8' --min-api 26 --output dex \
  \$(find cls -name '*.class'|tr '\n' ' ') lib/gson.jar lib/okhttp.jar lib/okio.jar lib/orgjson.jar lib/kotlin.jar"
docker exec -u 0 "$C" sh -c "cd $STAGE/dex && jar cf $STAGE/bridge.jar classes.dex"
mkdir -p "$HERE/build"
docker cp "$C:$STAGE/bridge.jar" "$HERE/build/bridge.jar"
ls -la "$HERE/build/bridge.jar"; echo OK
