#!/usr/bin/env python3
"""按需各爬虫运行时 sidecar 子进程池（stdin/stdout JSONL）。

三种后端，统一行协议：
  请求行  {"id":"<n>","class":..,"ext":..,"token":..,"method":..,...}
  响应行  {"id":"<n>","result":...} 或 {"id":"<n>","error":"..."}

- java ：JVM 跑纯 dex 转换 jar（小雅 AListSh 等），见 catvod Sidecar。
- node ：Node 跑 drpyS JS 站点。
- adb  ：Android-x86（KVM 模拟器/云机）内的 AppJavaBridge，承载加固 jar（占位，
          由 android_bridge 提供 ADB 通道，运行时若未配置则返回不可用）。

本模块只负责进程生命周期与行收发，不解析具体业务。

资源策略（最小内存运行）：
  子进程按需启动。默认常驻（SIDECAR_IDLE_SECONDS=0，旧行为）；如需节省内存，
  可设 SIDECAR_IDLE_SECONDS=N，空闲超过 N 秒由后台回收线程自动结束，下次调用自动重拉。
  调用进行中（持锁）的进程不会被回收。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("runner.pool")

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER_ROOT = os.path.dirname(HERE)

# 空闲回收秒数；0 = 常驻不回收。默认 30 分钟。
IDLE_TIMEOUT = float(os.environ.get("SIDECAR_IDLE_SECONDS", "0") or 0)
_REAP_INTERVAL = float(os.environ.get("SIDECAR_REAP_INTERVAL", "30") or 30)
# JVM / Node 内存上限（最小资源运行；可经环境变量放宽）
JVM_MAX_HEAP = os.environ.get("JVM_MAX_HEAP", "256m")
NODE_MAX_OLD = os.environ.get("NODE_MAX_OLD", "192")

# 各后端工作目录 / 启动命令（容器内外路径由环境变量覆盖）
JVM_ASSET = os.environ.get(
    "CATVOD_ASSET",
    os.path.join(RUNNER_ROOT, "..", "proxy", "assets", "catvod"))
DRPY_ASSET = os.environ.get(
    "DRPY_ASSET",
    os.path.join(RUNNER_ROOT, "..", "proxy", "assets", "drpy"))
JAVA_BIN = os.environ.get("JAVA_BIN", "java")
NODE_BIN = os.environ.get("NODE_BIN", "/var/apps/nodejs_v24/target/bin/node")


class Sidecar:
    def __init__(self, name: str, cmd: list[str], cwd: str | None = None,
                 env: dict | None = None):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self._last_use = time.time()
        self._started_at = 0.0

    def _alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _start(self):
        if self._alive():
            return
        log.info("start sidecar[%s]: %s", self.name, " ".join(self.cmd))
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=full_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._started_at = time.time()

    def call(self, payload: dict, timeout: float = 120.0):
        with self._lock:
            try:
                self._start()
                self._seq += 1
                cid = str(self._seq)
                payload = {"id": cid, **payload}
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"{self.name} sidecar 无响应（已退出）")
                resp = json.loads(line)
            except Exception:
                self.stop()
                raise
            finally:
                self._last_use = time.time()
            if "error" in resp:
                raise RuntimeError(str(resp["error"])[:600])
            return resp.get("result")

    def stop(self):
        try:
            if self.proc:
                self.proc.stdin.close()
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None

    def reap_if_idle(self, idle_for: float) -> bool:
        """空闲超时则结束。调用进行中（拿不到锁）直接跳过，绝不打断真实请求。"""
        if idle_for <= 0 or not self._alive():
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if not self._alive():
                return False
            if time.time() - self._last_use < idle_for:
                return False
            log.info("reap idle sidecar[%s] (idle %.0fs)", self.name, idle_for)
            self.stop()
            return True
        finally:
            self._lock.release()

    def status(self) -> dict:
        alive = self._alive()
        return {
            "enabled": True,
            "running": alive,
            "idle_s": round(time.time() - self._last_use) if alive else None,
            "pid": self.proc.pid if alive else None,
        }


def start_reaper(sidecars: dict[str, "Sidecar"],
                 idle_for: float = IDLE_TIMEOUT) -> threading.Thread | None:
    """启动后台空闲回收守护线程。idle_for<=0 时不启动（常驻）。"""
    if idle_for <= 0:
        return None
    rpcs = {k: v for k, v in sidecars.items() if isinstance(v, Sidecar)}

    def loop():
        while True:
            time.sleep(max(5.0, _REAP_INTERVAL))
            for sc in rpcs.values():
                try:
                    sc.reap_if_idle(idle_for)
                except Exception as exc:  # noqa: BLE001
                    log.warning("reap %s failed: %s", sc.name, exc)

    t = threading.Thread(target=loop, name="sidecar-reaper", daemon=True)
    t.start()
    log.info("sidecar idle reaper on: %ds", int(idle_for))
    return t


def _jvm_classpath() -> str:
    asset = os.path.abspath(JVM_ASSET)
    runner = os.path.join(asset, "build", "catvod-runner.jar")
    libdir = os.path.join(asset, "lib")
    jars = [runner]
    if os.path.isdir(libdir):
        jars += [os.path.join(libdir, f) for f in sorted(os.listdir(libdir))
                 if f.endswith(".jar")]
    jars += [os.path.join(asset, "jars", f)
             for f in sorted(os.listdir(os.path.join(asset, "jars")))
             if f.endswith("-jvm.jar")]
    return os.pathsep.join(jars)


def _jvm_available() -> bool:
    return os.path.exists(os.path.join(
        os.path.abspath(JVM_ASSET), "build", "catvod-runner.jar"))


def _node_available() -> bool:
    return os.path.exists(os.path.join(os.path.abspath(DRPY_ASSET), "sidecar.mjs"))


def build_sidecars() -> dict[str, Sidecar]:
    """按可用性登记后端（缺失的后端登记为 None，由路由返回 503）。

    JVM/Node 均按需启动，不在这里拉起进程；启动后再由空闲回收线程结束。
    """
    out: dict[str, Sidecar] = {}
    if _jvm_available():
        cp = _jvm_classpath()
        out["java"] = Sidecar(
            "java",
            [JAVA_BIN, f"-Xmx{JVM_MAX_HEAP}", "-XX:+UseSerialGC",
             "-XX:MaxMetaspaceSize=128m",
             "-Dfile.encoding=UTF-8", "-Dsun.jnu.encoding=UTF-8",
             "-cp", cp, "Sidecar"],
            cwd=os.path.abspath(JVM_ASSET))
    if _node_available():
        asset = os.path.abspath(DRPY_ASSET)
        out["node"] = Sidecar(
            "node",
            [NODE_BIN, f"--max-old-space-size={NODE_MAX_OLD}",
             "--import", "./register.mjs", "sidecar.mjs"],
            cwd=asset,
            env={"NODE_OPTIONS": f"--max-old-space-size={NODE_MAX_OLD}"})
    return out
