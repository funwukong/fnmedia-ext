#!/usr/bin/env python3
"""Android 后端（承载 DexGuard 加固 jar）。

为什么需要它：纯 dex 可在 JVM 跑，但加固 jar（classes.dex + assets/*_v7/v8.so
+ 加密 *.guard，真实逻辑在 ARM native + 加固壳）只能在真 Android 运行。
本机无 /dev/binder（redroid 不可用），但 /dev/kvm 可用，故采用
**KVM 加速的 android-x86 模拟器 + ARM→x86 二进制翻译**（Android Studio 系统
镜像 API30+ 内置）。

拓扑：
  宿主 runner  --adb/HTTP-->  模拟器内的 catvod-bridge App（一个常驻本地
  HTTP 服务，内部用 DexClassLoader 加载下发的 spider jar，反射调用
  com.github.catvod.crawler.Spider 各方法）。

本模块只做“通道”：探测模拟器、转发 catvod 调用。模拟器的拉起由
由 redroid 容器（127.0.0.1:9110->设备9101 直映）提供；
App 与 jar 的安装/下发由 setup_redroid.sh 完成。未就绪时 available()=False。

环境变量：
  ANDROID_BRIDGE_URL   模拟器内 bridge App 经端口映射暴露的 HTTP 地址
                       （默认 http://127.0.0.1:9101）
  ANDROID_ADB_SERIAL   可选 adb serial（默认 emulator-5554）
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("runner.android")

# 模拟器 bridge 一定是本机/容器内网地址，绝不走环境里的 HTTP(S)_PROXY
# （urllib 不识别 NO_PROXY 的 CIDR 写法，会误走外网代理导致 502/超时）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BRIDGE_URL = os.environ.get("ANDROID_BRIDGE_URL", "http://127.0.0.1:9101").rstrip("/")
ADB_SERIAL = os.environ.get("ANDROID_ADB_SERIAL", "emulator-5554")


def _http(path: str, payload: dict, timeout: float = 60.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, timeout: float = 6.0):
    req = urllib.request.Request(BRIDGE_URL + path, method="GET")
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def available() -> bool:
    """探测 bridge App 是否在线（GET /ping）。"""
    try:
        j = _get("/ping", timeout=4)
        return bool(j.get("ok"))
    except Exception:
        return False


def call(payload: dict, timeout: float = 120.0):
    """转发一次 catvod 调用，返回 result；bridge 内部完成 ClassLoader 缓存。"""
    j = _http("/call", payload, timeout=timeout)
    if j.get("error"):
        raise RuntimeError(str(j["error"])[:600])
    return j.get("result")
