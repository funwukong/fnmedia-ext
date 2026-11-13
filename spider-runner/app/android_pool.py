#!/usr/bin/env python3
"""Android bridge 多实例池（redroid 并发后端）。

单台 redroid 对加固 jar 调用必须串行（高频并发会把 bridge app 压进忙等），因此
历史上整个系统只有一个模拟器 + 一把全局锁，豆豆播放扇出各源只能串行（3~9s）。

本池管理 N 个互相独立的 redroid bridge（各自一份 guard.jar / DexClassLoader），
- 每个后端一把锁，保证单设备内仍严格串行；
- 选后端时优先“空闲且历史最快”的，全部忙则在最近最少使用的后端排队（有界）；
- 调用失败/超时自动转移到其它后端重试；后台 ping 维护健康状态。

环境变量：
  ANDROID_BRIDGE_URLS  逗号分隔的多个 bridge，如
                       http://127.0.0.1:9110,http://127.0.0.1:9111
                       不配置则退化为单个 ANDROID_BRIDGE_URL（默认 9110）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import runner_errors
from runner_errors import RunnerError

log = logging.getLogger("runner.androidpool")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_single = os.environ.get("ANDROID_BRIDGE_URL", "http://127.0.0.1:9110").rstrip("/")
_env = os.environ.get("ANDROID_BRIDGE_URLS", "").strip()
URLS = [u.strip().rstrip("/") for u in _env.split(",") if u.strip()] or [_single]

# 单后端调用排队的最长等待（所有后端都忙时）；超时返回忙，由上层快速失败/重试。
ACQUIRE_TIMEOUT = float(os.environ.get("ANDROID_POOL_ACQUIRE", "30"))

# 自动看护：某后端连续失败到阈值即尝试恢复（见 _recover_backend）。
AUTO_RECOVER = os.environ.get("FNM_POOL_AUTORECOVER", "1") not in ("0", "false", "False")
RECOVER_FAILS = int(os.environ.get("FNM_POOL_RECOVER_FAILS", "3"))
RECOVER_COOLDOWN = float(os.environ.get("FNM_POOL_RECOVER_COOLDOWN", "45"))
_recover_lock = threading.Lock()


def _container_for(url: str) -> str | None:
    """bridge URL 端口 -> redroid 容器名（9110 为首个无后缀实例）。

    只认本机 9110+N 映射（N 取 setup_redroid_pool.sh 的池范围，默认 2 台、
    建议 2–3 台，这里放宽到 10 台即端口 9110–9119）；其余端口/非本机
    hostname 一律返回 None，调用方不得据此拼出一个容器名（旧代码把
    :9101 算成 fnmedia-redroid-8）。
    """
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
        host = (parsed.hostname or "").lower()
    except Exception:
        return None
    if port is None or not 9110 <= port <= 9119:
        return None
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    idx = port - 9110 + 1
    return "fnmedia-redroid" if idx == 1 else f"fnmedia-redroid{idx}"


def _recover_backend(b: "_Backend") -> bool:
    """容器内不支持自愈：恢复职责交给宿主侧看门狗/管理页。

    旧实现在这里调 `docker exec` 重启 bridge App，但 spider-runner 镜像内
    无 docker CLI、compose 也没挂 docker.sock，那条分支永远走不通，
    反而让调用方误以为“已恢复”。现直接记一条 warning 并返回 False；
    last_restart 只作“已提醒”的节流标记，避免健康循环每 20s 刷一条日志。
    """
    if not AUTO_RECOVER:
        return False
    now = time.time()
    if now - getattr(b, "last_restart", 0.0) < RECOVER_COOLDOWN:
        return False
    if not _recover_lock.acquire(blocking=False):
        return False  # 另一台设备正在处理，不并发
    try:
        if time.time() - getattr(b, "last_restart", 0.0) < RECOVER_COOLDOWN:
            return False
        name = _container_for(b.url)
        log.warning("android 后端 %s（容器 %s）连续失败 %d，容器内不支持自愈"
                    "（无 docker CLI / 未挂 docker.sock），请由宿主侧看门狗或"
                    "管理页重启 bridge",
                    b.url, name, b.fails)
        b.last_restart = time.time()
        return False
    finally:
        _recover_lock.release()



class _Backend:
    __slots__ = ("url", "lock", "healthy", "latency", "last_used", "fails",
                 "last_restart")

    def __init__(self, url: str):
        self.url = url
        self.lock = threading.Lock()
        # 初始 False：runner 刚起、redroid 全挂时不得乐观报健康；
        # available() 首次调用即 ping，见 available()。
        self.healthy = False
        self.latency = 1.0
        self.last_used = 0.0
        self.fails = 0
        self.last_restart = 0.0

    def ping(self, timeout: float = 4.0) -> bool:
        try:
            req = urllib.request.Request(self.url + "/ping", method="GET")
            with _OPENER.open(req, timeout=timeout) as r:
                ok = bool(json.loads(r.read().decode("utf-8")).get("ok"))
            self.healthy = ok
            if ok:
                self.fails = 0
            return ok
        except Exception:  # noqa: BLE001
            self.healthy = False
            return False

    def post(self, payload: dict, timeout: float):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url + "/call", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.time()
        with _OPENER.open(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        dt = time.time() - t0
        # 指数平滑延迟，用于“最快后端优先”
        self.latency = self.latency * 0.6 + dt * 0.4
        self.fails = 0
        return j


_BACKENDS = [_Backend(u) for u in URLS]


def backends() -> list[_Backend]:
    return list(_BACKENDS)


def available() -> bool:
    """至少一个后端在线。被动：先看缓存健康标志，全不健康时即时 ping 一次。"""
    if any(b.healthy for b in _BACKENDS):
        return True
    return any(b.ping() for b in _BACKENDS)


def health() -> dict:
    return {
        "pool": [
            {"url": b.url, "healthy": b.healthy,
             "latency": round(b.latency, 3),
             "busy": b.lock.locked(), "fails": b.fails}
            for b in _BACKENDS
        ],
        "size": len(_BACKENDS),
    }


def _maybe_recover(b: "_Backend") -> None:
    """在后台守护线程里恢复卡死设备（docker exec + 探活较慢，不阻塞调用转移）。"""
    if not AUTO_RECOVER:
        return
    threading.Thread(target=_recover_backend, args=(b,),
                     name=f"android-recover-{_container_for(b.url) or b.url}",
                     daemon=True).start()


def _order() -> list[_Backend]:
    # 健康优先 → 延迟低优先；同等条件下 last_used 最旧的先（轮询负载）
    now = time.time()
    return sorted(_BACKENDS,
                  key=lambda b: (not b.healthy, b.latency, b.last_used or now))


def call(payload: dict, timeout: float = 120.0):
    """把一次 catvod 调用发到池中：选空闲后端，失败自动转移。

    timeout 是本次调用的总预算（抢锁 + 全部转移合计），不再按后端数叠加：
    抢锁用 min(ACQUIRE_TIMEOUT, 剩余)，每次 post 用 min(timeout, 剩余)。
    """
    timeout = float(timeout)
    deadline = time.time() + timeout
    tried: set[str] = set()
    last_err: Exception | None = None

    while time.time() < deadline and len(tried) < len(_BACKENDS):
        chosen = None
        # 第一轮：只抢当前空闲的锁；否则在“最优先后端”上有界排队
        for b in _order():
            if b.url in tried:
                continue
            if b.lock.acquire(blocking=False):
                chosen = b
                break
        if chosen is None:
            for b in _order():
                if b.url in tried:
                    continue
                remain = deadline - time.time()
                if remain <= 0:
                    break
                if b.lock.acquire(timeout=min(ACQUIRE_TIMEOUT, remain)):
                    chosen = b
                    break
        if chosen is None:
            break
        try:
            chosen.last_used = time.time()
            remain = deadline - time.time()
            if remain <= 0:
                break
            j = chosen.post(payload, timeout=min(timeout, remain))
            if j.get("error"):
                # 业务异常（爬虫抛错）不算后端故障，直接上抛，不转移；
                # kind/原始类名/全文透传，不截断。
                raise runner_errors.from_envelope(j)
            return j.get("result")
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError, OSError) as exc:
            chosen.fails += 1
            if chosen.fails >= 2:
                chosen.healthy = False
            if chosen.fails >= RECOVER_FAILS:
                _maybe_recover(chosen)
            tried.add(chosen.url)
            last_err = exc
            log.warning("android 后端 %s 失败，转移：%s", chosen.url, str(exc)[:600])
            continue
        finally:
            chosen.lock.release()

    if last_err:
        raise RunnerError(f"所有 android 后端不可用: {last_err}",
                          kind=runner_errors.kind_of(last_err),
                          orig=runner_errors.error_class_of(last_err)) from last_err
    raise RunnerError("android 后端忙（池容量不足或全部不可用）",
                      kind="timeout" if time.time() >= deadline else "fatal")


def _health_loop():
    while True:
        time.sleep(20)
        for b in _BACKENDS:
            try:
                ok = b.ping()
            except Exception:  # noqa: BLE001
                ok = False
            # 设备探活持续失败且过了冷却：即使暂无调用也后台自愈
            if not ok and time.time() - b.last_restart >= RECOVER_COOLDOWN:
                _maybe_recover(b)


def start_health_thread():
    t = threading.Thread(target=_health_loop, name="android-pool-health",
                         daemon=True)
    t.start()
