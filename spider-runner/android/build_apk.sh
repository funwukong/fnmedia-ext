#!/bin/bash
# 构建宿主 APK：内含 bridge dex（Bridge/HeadlessApp/catvod基类/gson/okhttp）+
# 一个 armeabi-v7a 的占位 ARM 库（确保安装后在 x86 镜像里建立 native-bridge 库目录，
# 真实 App 进程（Zygote fork）内 ndk_translation 生效，加固 v7 .so 才能经翻译加载）。
#
# 产物：spider-runner/android/build/catvod-bridge.apk
# 安装：adb install -r catvod-bridge.apk；启动：
#   am startservice -n com.fnmedia.catvodbridge/.BridgeService
set -euo pipefail
C="${1:-fnmedia-redroid}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CATVOD_SRC="$REPO/proxy/assets/catvod/src"
LIBDIR="$REPO/proxy/assets/catvod/lib"
ARM_SO="$REPO/spider-runner/android/arm-lib/ftyguard_v7.so"
BT="/opt/android/build-tools/36.0.0"
AJ="/opt/android/platforms/android-33/android.jar"

STAGE=$(docker exec -u 0 "$C" mktemp -d /tmp/apk.XXXXXX | tr -d '\r')
echo "stage=$STAGE"
trap 'docker exec -u 0 "$C" rm -rf "$STAGE" 2>/dev/null || true' EXIT

docker exec -u 0 "$C" mkdir -p "$STAGE/src/com/github/catvod/crawler" \
  "$STAGE/src/com/fnmedia/bridge" "$STAGE/cls" "$STAGE/dex" "$STAGE/apk/lib/armeabi-v7a"
# 1) 源码：Bridge + HeadlessApp + APK 组件 + catvod 基类
docker cp "$HERE/src/." "$C:$STAGE/src"
docker cp "$HERE/apk/com/fnmedia/bridge/." "$C:$STAGE/src/com/fnmedia/bridge/"
docker cp "$CATVOD_SRC/com/github/catvod/crawler/." "$C:$STAGE/src/com/github/catvod/crawler/"
# 2) 清单与 ARM 库
docker cp "$HERE/apk/AndroidManifest.xml" "$C:$STAGE/AndroidManifest.xml"
docker cp "$ARM_SO" "$C:$STAGE/apk/lib/armeabi-v7a/libplaceholder.so"
# 3) 运行库 jar
for j in gson okhttp okio orgjson kotlin; do docker cp "$(ls "$LIBDIR"/$j*.jar|head -1)" "$C:$STAGE/$j.jar"; done
docker exec -u 0 "$C" chmod -R a+rX "$STAGE"

echo "[1/7] javac"
docker exec -u 0 "$C" sh -c "cd $STAGE && find src -name '*.java'>f.txt && \
  javac -source 8 -target 8 -cp '$AJ' -d cls @f.txt 2>/dev/null"
echo "[2/7] d8（含运行库）"
docker exec -u 0 "$C" sh -c "cd $STAGE && '$BT/d8' --min-api 26 --output dex \
  \$(find cls -name '*.class'|tr '\n' ' ') gson.jar okhttp.jar okio.jar orgjson.jar kotlin.jar"
docker exec -u 0 "$C" sh -c "mkdir -p $STAGE/root/lib/armeabi-v7a && cp $STAGE/dex/classes.dex $STAGE/root/classes.dex && cp $STAGE/apk/lib/armeabi-v7a/libplaceholder.so $STAGE/root/lib/armeabi-v7a/"
echo "[3/7] aapt 打包 manifest/dex/arm 库"
docker exec -u 0 "$C" sh -c "cd $STAGE/root && '$BT/aapt' package -f -M ../AndroidManifest.xml \
  -I '$AJ' -F ../base-unsigned.apk && cd $STAGE/root && jar uf ../base-unsigned.apk classes.dex lib/armeabi-v7a/libplaceholder.so"
echo "[4/7] (dex/库已内联)"
echo "[5/7] zipalign"
docker exec -u 0 "$C" sh -c "rm -f $STAGE/aligned.apk; '$BT'/zipalign -p -f 4 $STAGE/base-unsigned.apk $STAGE/aligned.apk"
echo "[6/7] 生成调试签名（若无）"
docker exec -u 0 "$C" sh -c "[ -f /opt/android/keystore/bridge.keystore ] || keytool -genkeypair -keystore /opt/android/keystore/bridge.keystore \
  -storepass android -keypass android -alias b -keyalg RSA -keysize 2048 -validity 10000 \
  -dname 'CN=bridge,O=fnmedia,C=CN' >/dev/null 2>&1; echo keystore-ready"
echo "[7/7] apksigner 签名"
docker exec -u 0 "$C" sh -c "'$BT/apksigner' sign --ks /opt/android/keystore/bridge.keystore \
  --ks-pass pass:android --key-pass pass:android --out $STAGE/catvod-bridge.apk $STAGE/aligned.apk"
mkdir -p "$HERE/build"
docker cp "$C:$STAGE/catvod-bridge.apk" "$HERE/build/catvod-bridge.apk"
docker exec -u 0 "$C" sh -c "'$BT/apksigner' verify $STAGE/catvod-bridge.apk && echo VERIFIED"
ls -la "$HERE/build/catvod-bridge.apk"; echo OK
