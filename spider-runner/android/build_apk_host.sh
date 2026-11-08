#!/bin/bash
# 在【宿主机】用 JDK + Android build-tools(r8/d8/aapt/zipalign/apksigner) 构建
# catvod-bridge.apk。不依赖 redroid 容器内的临时 /opt/android（容器重启即丢）。
#
# 工具链放在 TOOL_DIR（默认 spider-runner/android/.buildtools），首次需联网经代理
# 下载：r8.jar(maven.google.com)、platform-33、build-tools_r33(dl.google.com)。
# 之后完全离线可复现。
#
# 产物：spider-runner/android/build/catvod-bridge.apk
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# 工具链必须放在纯 ASCII 路径：本机仓库路径含中文且 shell LANG 为空时，
# `java -cp <含中文的 r8.jar>` 会 ClassNotFoundException（jar 本身完好）。
TOOL_DIR="${ANDROID_TOOL_DIR:-/opt/bridge-build}"
AJ="$TOOL_DIR/android.jar"
R8="$TOOL_DIR/r8.jar"
KS="${BRIDGE_KEYSTORE:-$TOOL_DIR/bridge.keystore}"

# 经 Clash 代理下载（github/google 直连受限）
PROXY="${HTTPS_PROXY:-${https_proxy:-http://Clash:zE3UQEDI@192.168.31.198:7893}}"

fetch() { # url out
  echo "下载 $2"
  curl -fSL -m300 -x "$PROXY" -o "$2" "$1"
}

mkdir -p "$TOOL_DIR" "$HERE/build"
# 解压后动态定位 aapt（zip 顶层目录名随版本变化）
find_bt() { find "$TOOL_DIR" -type f -name aapt 2>/dev/null | head -1 | xargs -r dirname; }
if [ -z "$(find_bt)" ]; then
  echo "== 准备 Android build-tools r33 =="
  fetch "https://dl.google.com/android/repository/build-tools_r33-linux.zip" "$TOOL_DIR/bt.zip"
  (cd "$TOOL_DIR" && unzip -oq bt.zip && rm -f bt.zip)
  chmod -R u+rwX,go+rX "$TOOL_DIR"
fi
BT="$(find_bt)"
[ -n "$BT" ] && [ -x "$BT/aapt" ] || { echo "找不到 build-tools/aapt"; exit 1; }
echo "build-tools: $BT"
if [ ! -f "$AJ" ]; then
  echo "== 准备 android.jar (API33) =="
  fetch "https://dl.google.com/android/repository/platform-33_r02.zip" "$TOOL_DIR/p33.zip"
  (cd "$TOOL_DIR" && unzip -oq p33.zip "*/android.jar" && rm -f p33.zip)
  cp "$(find "$TOOL_DIR" -path '*/android.jar' -not -path "$TOOL_DIR/build-tools/*" | head -1)" "$AJ"
fi
if [ ! -f "$R8" ]; then
  fetch "https://maven.google.com/com/android/tools/r8/8.2.42/r8-8.2.42.jar" "$R8"
fi
chmod 644 "$R8" "$AJ" 2>/dev/null || true

# ARM 加固占位库（私有资产，未入库时从 guard.jar 的 assets 解出，内容一致 md5 d78e…）
ARM_SO="$HERE/arm-lib/ftyguard_v7.so"
if [ ! -f "$ARM_SO" ]; then
  echo "== 从 guard.jar 解出 ftyguard_v7.so =="
  mkdir -p "$HERE/arm-lib"
  REPO="$REPO" OUT="$ARM_SO" python3 - <<'PY'
import os, zipfile
jar = os.path.join(os.environ["REPO"], "proxy/assets/guard/guard.jar")
out = os.environ["OUT"]
with zipfile.ZipFile(jar) as z:
    open(out, "wb").write(z.read("assets/ftyguard_v7.so"))
print("ftyguard_v7.so", os.path.getsize(out), "bytes")
PY
fi

CATVOD_SRC="$REPO/proxy/assets/catvod/src"
LIBDIR="$REPO/proxy/assets/catvod/lib"
STAGE="$(mktemp -d /tmp/bridge-apk.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/cls" "$STAGE/dex" "$STAGE/root/lib/armeabi-v7a"

# javac/java 在 LANG 为空时对【含中文的 -cp / @argfile 源路径】会失败，故为仓库建
# ASCII 软链，源码、依赖 jar、manifest、arm so 全部经软链读取；仅最后把 APK 写回真实路径。
WORK="$(mktemp -d /tmp/fnrepo.XXXXXX)"
trap 'rm -rf "$STAGE" "$WORK"' EXIT
ln -s "$REPO" "$WORK/repo"
A="$WORK/repo/spider-runner/android"
ALIBDIR="$WORK/repo/proxy/assets/catvod/lib"
ACATVOD="$WORK/repo/proxy/assets/catvod/src/com/github/catvod/crawler"
AARM="$A/arm-lib/ftyguard_v7.so"
if [ ! -f "$AARM" ]; then
  mkdir -p "$A/arm-lib"
  REPO="$REPO" OUT="$A/arm-lib/ftyguard_v7.so" python3 - <<'PY'
import os, zipfile
jar = os.path.join(os.environ["REPO"], "proxy/assets/guard/guard.jar")
out = os.environ["OUT"]
with zipfile.ZipFile(jar) as z:
    open(out, "wb").write(z.read("assets/ftyguard_v7.so"))
print("ftyguard_v7.so", os.path.getsize(out), "bytes")
PY
fi

LIBCP="$ALIBDIR/gson.jar:$ALIBDIR/kotlin.jar:$ALIBDIR/okhttp.jar:$ALIBDIR/okio.jar:$ALIBDIR/orgjson.jar"
{ find "$A/src" -name '*.java';
  find "$A/apk" -name '*.java';
  find "$ACATVOD" -name '*.java'; } > "$STAGE/srcs.txt"
echo "[1/6] javac ($(wc -l < "$STAGE/srcs.txt") 源)"
javac -source 8 -target 8 -nowarn -encoding UTF-8 \
  -cp "$AJ:$LIBCP" -d "$STAGE/cls" @"$STAGE/srcs.txt"
echo "[2/6] d8"
CLASSES=$(find "$STAGE/cls" -name '*.class' | tr '\n' ' ')
java -cp "$R8" com.android.tools.r8.D8 --min-api 26 --output "$STAGE/dex" \
  $CLASSES "$ALIBDIR/gson.jar" "$ALIBDIR/kotlin.jar" "$ALIBDIR/okhttp.jar" \
  "$ALIBDIR/okio.jar" "$ALIBDIR/orgjson.jar"
cp "$STAGE/dex/classes.dex" "$STAGE/root/classes.dex"
cp "$AARM" "$STAGE/root/lib/armeabi-v7a/libplaceholder.so"
echo "[3/6] aapt package"
(cd "$STAGE/root" && "$BT/aapt" package -f -M "$A/apk/AndroidManifest.xml" \
  -I "$AJ" -F "$STAGE/base-unsigned.apk")
(cd "$STAGE/root" && jar uf "$STAGE/base-unsigned.apk" classes.dex lib/armeabi-v7a/libplaceholder.so)
echo "[4/6] zipalign"
"$BT/zipalign" -p -f 4 "$STAGE/base-unsigned.apk" "$STAGE/aligned.apk"
echo "[5/6] 签名（无既有 keystore 则生成；换签名需先 pm uninstall）"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -storepass android -keypass android -alias b \
    -keyalg RSA -keysize 2048 -validity 10000 -dname 'CN=bridge,O=fnmedia,C=CN' >/dev/null 2>&1
fi
# apksigner 的 JVM 在中文 CWD 下 FilePermission.<clinit> 会抛 InvalidPathException，
# 故切到 ASCII 的 STAGE 目录、用相对文件名签名。
( cd "$STAGE" && "$BT/apksigner" sign --ks "$KS" --ks-pass pass:android \
    --key-pass pass:android --out catvod-bridge.apk aligned.apk )
echo "[6/6] 校验"
( cd "$STAGE" && "$BT/apksigner" verify catvod-bridge.apk )
cp "$STAGE/catvod-bridge.apk" "$HERE/build/catvod-bridge.apk"
ls -la "$HERE/build/catvod-bridge.apk"; echo OK
