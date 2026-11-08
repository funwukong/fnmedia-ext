# Android 加固运行时（redroid 轻量容器）

承载 DexGuard 加固 jar（`csp_*Guard` 源）。**唯一方案：redroid 容器化 Android**，
无 QEMU/模拟器，镜像内置 NDK ARM/ARM64 翻译，可直接加载加固 jar 内的
`armeabi-v7a` / `arm64-v8a` native（`ftyguard_v7.so` / `ftyguard_v8.so`）。

| 项 | 值 |
|---|---|
| 镜像 | 发布版 **`fnmedia/fnmedia-redroid:latest`**（Docker Hub 公开，= 基础镜像 + 自研 bridge；解压约 2.15GB、压缩约 870MB）。基础镜像 `erstt/redroid:11.0.0_ndk_ChromeOS`，仅在发布镜像不可达时回退 |
| 容器名 | `fnmedia-redroid` |
| 稳态内存 | 约 0.7–0.9GB（旧 QEMU 模拟器 2–3.7GB） |
| 冷启动 | 约 10–30 秒（旧 QEMU 1–3 分钟） |
| 网络 | 容器端口直映 `127.0.0.1:9110 -> 设备 9101`（无 adb forward / socat） |
| 内核要求 | `CONFIG_ANDROID_BINDERFS=y`（fnOS 6.18 已内建）；**不需要 /dev/kvm** |
| 启停 | **默认常驻**：`--restart=unless-stopped`，开机自启、异常退出自动拉起；管理页可手动停止（停止后不自启，直到再启动） |

## 一键部署

```bash
# 在目标 NAS 以 root 运行（幂等；首跑拉镜像约 2.1GB）
sudo bash spider-runner/android/setup_redroid.sh
# 或经统一引导：
sudo bash spider-runner/deploy/bootstrap_sidecar.sh android-only
```

脚本会：挂载 binderfs（并装 `fnmedia-binderfs.service` 开机自挂载）→ 拉镜像 →
创建持久容器（数据卷 `state/redroid-data`，bridge App 与 jar 装好后重启不丢）→
等开机 → 安装 `build/catvod-bridge.apk` → 下发
`proxy/assets/guard/guard.jar` → 拉起设备内 bridge 并自检 9110。

## 手动启停

```bash
docker start fnmedia-redroid    # 启动后约 10–30 秒就绪（bridge App 由插件自动拉起）
docker stop  fnmedia-redroid    # 立即释放约 0.7–0.9GB
curl http://127.0.0.1:9110/ping # {"ok":true,...} 即就绪
```

主插件管理页「TVBox 加固」提供同样的 **启动运行时 / 停止并释放内存 / 重接 bridge**
按钮，并有就绪/启动中/已停止状态。

## 重新构建 bridge APK

需要 Android SDK（`$ANDROID_HOME`）：

```bash
./build_apk.sh        # Gradle 构建并输出 build/catvod-bridge.apk
```

加固 jar 更新后，把新 jar 放到 `proxy/assets/guard/guard.jar`，重跑
`setup_redroid.sh`（幂等，会重新下发并重启 bridge）。

## 与 runner 的关系

- runner（`spider-runner/app`）统一通过 `ANDROID_BRIDGE_URL`（默认
  `http://127.0.0.1:9110`）调用设备内 bridge；
- 不启动 redroid **不影响** 小雅（JVM）与 JS（Node）源。
