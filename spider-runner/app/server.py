#!/usr/bin/env python3
"""统一 TVBox 爬虫运行时 HTTP 服务（外挂容器/本机均可运行）。

后端：java（纯dex，JVM）、node（drpyS JS）、android（加固 jar，KVM 模拟器）。
对外两套等价路由：

1) 统一调用（本插件主用）
   POST /invoke
   {"runtime":"java|node|android|auto",
    "class":"com.github.catvod.spider.AListSh",      # java/android
    "site":{api,ext,...},                            # node drpy
    "ext":..., "token":"...", "method":"home|...", ...catvod参数 }

2) FongMi Node bundle 兼容（每站 /init /home /category /detail /search /play）
   POST /node/<siteId>/<method>   body=catvod参数
   便于把任意 spider 当作“服务器爬虫(type4 风格)”被标准 TVBox 客户端消费。

GET /health：各后端可用性。
仅标准库，无第三方依赖。
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pool
import android_bridge
import android_pool

# 多 redroid 实例池就绪时（≥1 个后端）android 走池，单后端也由池统一调度；
# 池为空才退回旧单通道模块。
android_pool.start_health_thread()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("runner")

SIDECARS: dict[str, pool.Sidecar] = {}


def _runtime_for(payload: dict) -> str:
    rt = payload.get("runtime") or "auto"
    if rt != "auto":
        return rt
    if payload.get("class"):
        return "java"
    if payload.get("site"):
        return "node"
    return "java"


def invoke(payload: dict):
    rt = _runtime_for(payload)
    timeout = float(payload.pop("_timeout", 120))
    if rt == "android":
        if android_pool.backends():
            if not android_pool.available():
                raise RuntimeError("android 后端不可用（模拟器/bridge App 未就绪）")
            return android_pool.call(payload, timeout=timeout)
        if not android_bridge.available():
            raise RuntimeError("android 后端不可用（模拟器/bridge App 未就绪）")
        return android_bridge.call(payload, timeout=timeout)
    sc = SIDECARS.get(rt)
    if sc is None:
        raise RuntimeError(f"后端 {rt} 不可用（未随服务提供/依赖缺失）")
    payload.pop("runtime", None)
    return sc.call(payload, timeout=timeout)


class Handler(BaseHTTPRequestHandler):
    server_version = "SpiderRunner/1.0"

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def log_message(self, *a):  # 静默默认访问日志
        pass

    def do_GET(self):
        path0 = self.path.split("?", 1)[0]
        _android_ok = android_pool.available() if android_pool.backends() \
            else android_bridge.available()
        if path0 == "/health":
            self._json(200, {"ok": True, "runtimes": {
                "java": "java" in SIDECARS,
                "node": "node" in SIDECARS,
                "android": _android_ok,
            }, "android_pool": android_pool.health()})
            return
        if path0 == "/status":
            # 实际进程状态：registered=已提供，running=当前在跑（空闲会被回收）
            procs = {}
            for name, sc in SIDECARS.items():
                procs[name] = sc.status() if sc is not None else {"enabled": False}
            self._json(200, {
                "ok": True,
                "idle_timeout_s": pool.IDLE_TIMEOUT,
                "runtimes": {
                    "java": "java" in SIDECARS,
                    "node": "node" in SIDECARS,
                    "android": _android_ok,
                },
                "android_pool": android_pool.health(),
                "processes": procs,
            })
            return
        if self.path.startswith("/node/"):
            # GET 仅健康探测
            parts = self.path.strip("/").split("/")
            self._json(200, {"ok": True, "site": parts[1] if len(parts) > 1 else ""})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
        try:
            if path == "/invoke":
                return self._json(200, {"ok": True, "result": invoke(payload)})
            if path.startswith("/node/"):
                parts = path.strip("/").split("/")  # node/<siteId>/<method>
                if len(parts) != 3:
                    return self._json(400, {"error": "bad node path"})
                _, site_id, method = parts
                req = {"runtime": "node",
                       "site": payload.get("site") or {"_id": site_id},
                       "method": method}
                # FongMi 参数风格透传
                for k in ("ext", "tid", "pg", "filter", "extend", "ids",
                          "key", "quick", "flag", "id", "token", "args"):
                    if k in payload:
                        req[k] = payload[k]
                return self._json(200, {"ok": True, "result": invoke(req)})
            self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log.warning("invoke fail: %s", exc)
            self._json(500, {"ok": False, "error": str(exc)[:600]})


def main():
    global SIDECARS
    SIDECARS = pool.build_sidecars()
    reaper = pool.start_reaper(SIDECARS)
    log.info("可用 sidecar 后端: %s（空闲回收 %s）",
             sorted(SIDECARS),
             f"{int(pool.IDLE_TIMEOUT)}s" if pool.IDLE_TIMEOUT > 0 else "关闭")
    port = int(os.environ.get("RUNNER_PORT", "9100"))
    host = os.environ.get("RUNNER_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("spider-runner listening on %s:%s", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for sc in SIDECARS.values():
            sc.stop()


if __name__ == "__main__":
    main()
