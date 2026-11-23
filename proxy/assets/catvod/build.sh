#!/bin/bash
# 编译 catvod JVM 运行时（Android 桩 + catvod 基类 + Runner/Sidecar）。
# javac 对含中文的绝对路径（源码与 classpath 均然）有兼容问题，故全部在 ASCII 临时目录进行。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE="$(mktemp -d /tmp/catvod-build.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$HERE/src" "$STAGE/src"
mkdir -p "$STAGE/lib" "$STAGE/jars" "$STAGE/classes"
cp "$HERE"/lib/*.jar "$STAGE/lib/" 2>/dev/null || true
cp "$HERE"/jars/*-jvm.jar "$STAGE/jars/" 2>/dev/null || true
CP=$(printf "%s:" "$STAGE"/lib/*.jar "$STAGE"/jars/*.jar)
CP="${CP%:}"
echo "[catvod-build] 编译（stage=$STAGE）"
# shellcheck disable=SC2046
javac -encoding UTF-8 -cp "$CP" $(find "$STAGE/src" -name '*.java') -d "$STAGE/classes"
( cd "$STAGE/classes" && jar cf "$STAGE/catvod-runner.jar" . )
mkdir -p "$HERE/build"
cp "$STAGE/catvod-runner.jar" "$HERE/build/catvod-runner.jar"
echo "[catvod-build] 完成: $HERE/build/catvod-runner.jar"
