#!/usr/bin/env python3
"""DexGuard/ftyguard 加固 TVBox 爬虫的 Android 运行时桥接（完整版 GUARD）。

这些 csp_*Guard 源的真实逻辑在加密 *.guard + ARM native 中，JVM/Node 无法执行；
统一 spider-runner 的 android 后端会把调用转发到 KVM 模拟器内的 catvod-bridge。
主插件只通过 HTTP 访问，不直接依赖 Android/ADB/模拟器。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import features
import httpx

_log = logging.getLogger("fnmedia")


def _env_float(name: str, default: float) -> float:
    """模块级 env 浮点解析：非法值记 warning 后用默认值，绝不让 import 失败。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        _log.warning("guard: invalid %s=%r, using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    """模块级 env 整数解析：非法值记 warning 后用默认值；可 clamp 下限。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            _log.warning("guard: invalid %s=%r, using default %s",
                         name, raw, default)
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value

_ASSET = Path(__file__).resolve().parent / "assets" / "guard"
_SITES = _ASSET / "sites.json"
# 用户通过“导入 TVBox 配置”加入的加固源（私有、gitignore，不随公开构建分发）。
_IMPORTED = _ASSET / "imported.json"
# 用户自备的加固 jar（私有，不随 fpk 分发）。发布包不再内置任何第三方 jar。
_USER_JAR = _ASSET / "guard.jar"
MAX_JAR_BYTES = 60 * 1024 * 1024

# 设备端 bridge 内的绝对 jar 路径（setup_redroid.sh 下发到这里）。
DEVICE_JAR = "/data/data/com.fnmedia.catvodbridge/files/jars/guard.jar"
RUNNER_URL = os.environ.get("FNM_GUARD_RUNNER_URL",
                            os.environ.get("FNM_RUNNER_URL", "http://127.0.0.1:9100")).rstrip("/")
TIMEOUT = _env_float("FNM_GUARD_TIMEOUT", 150.0)

_lock = threading.Lock()
# redroid 以“多实例池”提供并发：runner（android_pool）对每台设备各持一把锁保证
# 设备内严格串行，设备之间真并发。代理侧只做有界并发（默认 2，可按池大小自动放宽），
# 避免极端扇出在池前堆积；不再像单实例时代那样全局串行。
_bridge_sem = threading.BoundedSemaphore(
    _env_int("FNM_GUARD_CONCURRENCY", 2, minimum=1))
# 仅用于串行化“看门狗重启桥 app”这一动作，不包裹正常调用。
_bridge_restart_lock = threading.Lock()
_sites: dict[str, dict] | None = None
_health_cache = (0.0, {"ok": False})
# 桥 App 看门狗：连续失败计数与重启冷却，避免桥 app 进入 do_epoll 忙等死循环后
# 所有 /invoke 永久超时。单设备串行锁内调用，无需额外加锁。
_bridge_fail = 0
_bridge_restart_cooldown = 0.0
_BRIDGE_FAIL_RESTART = 3
_BRIDGE_RESTART_GAP = 30.0
# 重启失败时的冷却（比成功更长）：失败不代表桥好了，立即再重启只会雪上加霜。
_BRIDGE_RESTART_FAIL_GAP = 120.0
# /invoke 单次调用超时（首调与重试一致；原重试落回 150s 默认极易拖垮调用方）。
_GUARD_RETRY_TIMEOUT = float(os.environ.get("FNM_GUARD_RETRY_TIMEOUT", "22"))
# 等 bridge 许可的最长排队时间：超时按调用失败处理，不无限等。
_BRIDGE_SEM_TIMEOUT = float(os.environ.get("FNM_GUARD_SEM_TIMEOUT", "25"))
# 最近一次传输层失败的后端 URL：重启时按它选目标容器。
_last_failed_runner_url = ""


def _resolve_restart_container() -> str | None:
    """按最近失败的后端 URL 选重启目标容器（防御式）。

    映射 `_container_for(url)` 由 E 组维护；尚未提供时回退默认容器。
    映射无效（无返回值/非字符串/抛异常）或容器不存在时返回 None，
    调用方跳过重启并记 warning——绝不对不存在的容器 docker exec。
    """
    url = _last_failed_runner_url or RUNNER_URL
    fn = globals().get("_container_for")
    if callable(fn):
        try:
            name = fn(url)
        except Exception:  # noqa: BLE001
            _log.warning("_resolve_restart_container failed: url=%s", url,
                         exc_info=True)
            return None
        if not name or not isinstance(name, str):
            _log.warning("跳过桥重启：_container_for(%s) 未返回有效容器", url)
            return None
        if not _container_exists(name):
            _log.warning("跳过桥重启：容器 %s 不存在（后端 %s）", name, url)
            return None
        return name
    if not _container_exists(REDROID_CONTAINER):
        _log.warning("跳过桥重启：默认容器 %s 不存在", REDROID_CONTAINER)
        return None
    return REDROID_CONTAINER


def _restart_bridge_app(container: str | None = None) -> bool:
    """桥 app 卡死（do_epoll_wait 忙等、/invoke 超时）时 force-stop 后重新拉起。

    只能在 `_bridge_restart_lock` 内调用，且绝不能在持有 `_bridge_sem` 时调用
    （重启本身要等桥起，持许可会把并发调用全拖住）。只尝试拉起一次、健康轮询
    封顶约 8s；桥启动通常很快，没起来也交给下一次失败重试。

    无论成败，返回前一定写冷却：成功用 `_BRIDGE_RESTART_GAP`，失败用更长的
    `_BRIDGE_RESTART_FAIL_GAP`（失败时不立即再触发重启）。返回桥是否起来。"""
    global _bridge_restart_cooldown
    ok = False
    try:
        if container is None:
            container = _resolve_restart_container()
        elif not _container_exists(container):
            _log.warning("跳过桥重启：容器 %s 不存在", container)
            return False
        if container is None:
            return False
        try:
            _docker_exec(["am", "force-stop", "com.fnmedia.catvodbridge"],
                         timeout=10, container=container)
        except Exception:  # noqa: BLE001
            _log.debug("_restart_bridge_app failed", exc_info=True)
            pass
        time.sleep(1.0)
        try:
            _docker_exec(["am", "start", "-n", _BRIDGE_COMPONENT],
                         timeout=8, container=container)
        except Exception:  # noqa: BLE001
            _log.debug("_restart_bridge_app failed", exc_info=True)
            pass
        for _ in range(8):
            if _redroid_bridge_up():
                ok = True
                break
            time.sleep(0.5)
        return ok
    finally:
        _bridge_restart_cooldown = time.time() + (
            _BRIDGE_RESTART_GAP if ok else _BRIDGE_RESTART_FAIL_GAP)

# ---- Android 加固运行时（redroid 轻量容器）生命周期：默认常驻 + 可手动停止 ----
# 只支持 redroid（轻量容器化 Android）。是否启动完全由管理页按钮控制，
# 不做空闲自动停机——小内存机器上由用户按需手动“启动/停止”。
REDROID_CONTAINER = os.environ.get("FNM_REDROID_CONTAINER", "fnmedia-redroid")
_BRIDGE_COMPONENT = "com.fnmedia.catvodbridge/com.fnmedia.bridge.BootActivity"
_SIDECAR_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
# 跨机一键部署：默认拉 Docker Hub 上公开、只含自研 bridge 的预构建镜像；
# 第三方私有加固 jar 不进镜像，部署时从完整版 fpk 灌入。
_REGISTRY = os.environ.get("FNM_REGISTRY", "fnmedia")
_TAG = os.environ.get("FNM_IMAGE_TAG", "latest")
REDROID_IMAGE = os.environ.get(
    "FNM_REDROID_IMAGE", f"{_REGISTRY}/fnmedia-redroid:{_TAG}")
# 预构建镜像不可达时的基础回退（仅 Android，无内置 bridge，用 fpk 自带 APK 补）
REDROID_BASE = os.environ.get(
    "FNM_REDROID_BASE", "erstt/redroid:11.0.0_ndk_ChromeOS")
REDROID_MEMORY = os.environ.get("FNM_REDROID_MEMORY", "2g")
REDROID_BRIDGE_PORT = os.environ.get("FNM_BRIDGE_PORT", "9110")
REDROID_DATA = os.environ.get(
    "FNM_REDROID_DATA", str(_SIDECAR_STATE_DIR / "redroid-data"))
_ANDROID_USE_FILE = _SIDECAR_STATE_DIR / "android_last_use"
_ANDROID_STOP_FLAG = _SIDECAR_STATE_DIR / "android_manual_stop"
_android_start_lock = threading.Lock()


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# 常驻模式：开机/插件启动后自动拉起 redroid 并保持运行（容器退出自动重启、
# bridge 掉了自动重拉），直到用户在管理页手动停止。可设 FNM_REDROID_RESIDENT=0
# 恢复旧的“纯手动、预置即停”（设 FNM_REDROID_RESIDENT=0）。
REDROID_RESIDENT = _env_flag("FNM_REDROID_RESIDENT", "1")
# 常驻模式下容器的 Docker 重启策略：宿主重启/容器异常退出会自动回来；
# 但用户手动 docker stop 后不会被 Docker 自身拉起（由看门狗结合停止标记决定）。
REDROID_RESTART_POLICY = os.environ.get("FNM_REDROID_RESTART", "unless-stopped")


def _manual_stop_requested() -> bool:
    """用户是否在管理页明确要求停止（停止后不自动拉起，直到再次手动启动）。"""
    try:
        return _ANDROID_STOP_FLAG.exists()
    except Exception:
        _log.debug("_manual_stop_requested failed", exc_info=True)
        return False


def _request_manual_stop() -> None:
    try:
        _SIDECAR_STATE_DIR.mkdir(parents=True, exist_ok=True)
        _ANDROID_STOP_FLAG.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        _log.debug("_request_manual_stop failed", exc_info=True)
        pass


def _clear_manual_stop() -> None:
    try:
        _ANDROID_STOP_FLAG.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        _log.debug("_clear_manual_stop failed", exc_info=True)
        pass


def _mark_android_used() -> None:
    try:
        _SIDECAR_STATE_DIR.mkdir(parents=True, exist_ok=True)
        _ANDROID_USE_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        _log.debug("_mark_android_used failed", exc_info=True)
        pass


def android_last_use() -> float:
    try:
        return float(_ANDROID_USE_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        _log.debug("android_last_use failed", exc_info=True)
        return 0.0


def _docker() -> bool:
    return shutil.which("docker") is not None


def _container_exists(name: str = REDROID_CONTAINER) -> bool:
    if not _docker():
        return False
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name],
                             capture_output=True, text=True, timeout=6)
        return out.returncode == 0
    except Exception:
        _log.debug("_container_exists failed: name=%s", name, exc_info=True)
        return False


def _container_running(name: str = REDROID_CONTAINER) -> bool:
    if not _docker():
        return False
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                             capture_output=True, text=True, timeout=6)
        return out.stdout.strip() == "true"
    except Exception:
        _log.debug("_container_running failed: name=%s", name, exc_info=True)
        return False


def pool_containers() -> list[str]:
    """枚举所有在运行的 redroid 池容器（主容器 + fnmedia-redroid2 等多实例）。

    runner(android_pool) 会把 /invoke 负载到 9110/9111 等多台设备，因此 jar 必须
    同步到【每一台】，否则分到缺 jar 的设备即 ClassNotFound(DexPathList 空)。
    """
    names = [REDROID_CONTAINER]
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=8)
        for nm in (out.stdout or "").splitlines():
            nm = nm.strip()
            if nm and nm.startswith("fnmedia-redroid") and nm not in names:
                names.append(nm)
    except Exception:
        _log.debug("pool_containers failed", exc_info=True)
        pass
    return [n for n in names if _container_running(n)]


def android_backend() -> str:
    """固定 redroid（保留字段名供管理页/状态兼容）。"""
    return "redroid"


def _docker_exec(args: list, timeout: float = 15,
                 container: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", container or REDROID_CONTAINER, *args],
                          capture_output=True, text=True, timeout=timeout)


def _redroid_booted() -> bool:
    try:
        out = _docker_exec(["getprop", "sys.boot_completed"], timeout=8)
        return out.stdout.strip() == "1"
    except Exception:
        _log.debug("_redroid_booted failed", exc_info=True)
        return False


def _redroid_bridge_up() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{REDROID_BRIDGE_PORT}/ping", timeout=4) as r:
            return b'"ok"' in r.read(200)
    except Exception:
        _log.debug("_redroid_bridge_up failed", exc_info=True)
        return False


def _ensure_binderfs() -> None:
    # redroid 需要 binderfs；fnOS 内核已内建 BINDERFS，仅缺挂载时补挂（幂等）。
    try:
        if Path("/dev/binderfs/binder-control").exists():
            return
        subprocess.run(["mkdir", "-p", "/dev/binderfs"], timeout=5)
        subprocess.run(["mount", "-t", "binder", "binder", "/dev/binderfs"], timeout=5)
    except Exception:
        _log.debug("_ensure_binderfs failed", exc_info=True)
        pass


def _redroid_image_present(name: str | None = None) -> bool:
    try:
        out = subprocess.run(["docker", "image", "inspect", name or REDROID_IMAGE],
                             capture_output=True, text=True, timeout=8)
        return out.returncode == 0
    except Exception:
        _log.debug("_redroid_image_present failed: name=%s", name, exc_info=True)
        return False


def _resolve_redroid_image() -> str:
    """返回本机可用镜像；缺则拉预构建镜像，失败回退基础镜像。"""
    if _redroid_image_present(REDROID_IMAGE):
        return REDROID_IMAGE
    for ref in (REDROID_IMAGE, REDROID_BASE):
        try:
            pull = subprocess.run(["docker", "pull", ref],
                                  capture_output=True, text=True, timeout=600)
            if pull.returncode == 0 and _redroid_image_present(ref):
                return ref
        except Exception:
            _log.debug("_resolve_redroid_image failed", exc_info=True)
            continue
    raise RuntimeError(f"无法获取 redroid 镜像（{REDROID_IMAGE} 与回退 {REDROID_BASE} 均失败）")


def _ensure_redroid_dns(net: str, dns1: str) -> None:
    """纠正 redroid Android 层锁死 8.8.8.8 导致的解析失败（幂等、best-effort）。

    redroid 的 netd 把 DNS 固定为 8.8.8.8（容器 --dns 对 Android 层不生效），
    国内直连 8.8.8.8 不通，于是影视域名解析失败。在宿主 nat 表加 DNAT：把来自
    redroid 子网、发往 8.8.8.8:53 的请求改写为可用 DNS。重启后由本函数自愈。
    无 iptables / 非特权时静默跳过（不影响已手工配置的环境）。
    """
    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=8)

    # 取容器所在子网（自定义网络或默认 bridge）
    subnet = ""
    try:
        ins = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .IPAM.Config}}{{.Subnet}}{{end}}", net],
            capture_output=True, text=True, timeout=8).stdout.strip()
        if ins:
            subnet = ins.split()[0]
    except Exception:  # noqa: BLE001
        _log.debug("_ensure_redroid_dns failed", exc_info=True)
        pass
    if not subnet:
        try:
            br = subprocess.run(["docker", "network", "inspect", "-f",
                                 "{{range .IPAM.Config}}{{.Subnet}}{{end}}", "bridge"],
                                capture_output=True, text=True, timeout=8).stdout.strip()
            subnet = (br.split()[0] if br else "172.17.0.0/16")
        except Exception:  # noqa: BLE001
            _log.debug("_ensure_redroid_dns failed", exc_info=True)
            subnet = "172.17.0.0/16"
    for proto in ("udp", "tcp"):
        check = ["iptables", "-t", "nat", "-C", "PREROUTING", "-s", subnet,
                 "-d", "8.8.8.8", "-p", proto, "--dport", "53",
                 "-j", "DNAT", "--to-destination", f"{dns1}:53"]
        if run(check).returncode == 0:
            continue
        add = ["iptables", "-t", "nat", "-A", "PREROUTING", "-s", subnet,
               "-d", "8.8.8.8", "-p", proto, "--dport", "53",
               "-j", "DNAT", "--to-destination", f"{dns1}:53"]
        run(add)


def _create_redroid() -> None:
    """首次一键启动时创建持久化 redroid 容器（之后仅 docker start）。"""
    if _container_exists():
        return
    image = _resolve_redroid_image()
    data_dir = Path(REDROID_DATA)
    data_dir.mkdir(parents=True, exist_ok=True)
    net = os.environ.get("FNM_SPIDER_NET", "fnmedia-spider")
    dns1 = os.environ.get("FNM_REDROID_DNS", "223.5.5.5")
    dns2 = os.environ.get("FNM_REDROID_DNS2", "119.29.29.29")
    # 容器化 runner 经共享网络的固定名访问 redroid；缺失则建（幂等）
    subprocess.run(["docker", "network", "inspect", net],
                   capture_output=True, text=True, timeout=8)
    if subprocess.run(["docker", "network", "create", net],
                      capture_output=True, text=True).returncode != 0:
        pass  # 已存在即可
    run_args = ["docker", "run", "-itd", "--privileged",
                "--name", REDROID_CONTAINER,
                "--restart", REDROID_RESTART_POLICY if REDROID_RESIDENT else "no",
                "-m", REDROID_MEMORY,
                "-p", f"127.0.0.1:{REDROID_BRIDGE_PORT}:9101",
                "--network", net, "--network-alias", "android",
                "--dns", dns1, "--dns", dns2,
                "-v", f"{data_dir}:/data",
                image,
                "androidboot.redroid_gpu_mode=guest",
                "androidboot.redroid_width=720",
                "androidboot.redroid_height=1280"]
    r = subprocess.run(run_args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        # 网络异常时退回默认 bridge，保证本机 systemd runner（127.0.0.1:9110）仍可用
        fallback = ["docker", "run", "-itd", "--privileged",
                    "--name", REDROID_CONTAINER,
                    "--restart", REDROID_RESTART_POLICY if REDROID_RESIDENT else "no",
                    "-m", REDROID_MEMORY, "-p", f"127.0.0.1:{REDROID_BRIDGE_PORT}:9101",
                    "--dns", dns1, "--dns", dns2,
                    "-v", f"{data_dir}:/data", image,
                    "androidboot.redroid_gpu_mode=guest",
                    "androidboot.redroid_width=720",
                    "androidboot.redroid_height=1280"]
        subprocess.run(fallback, check=True, capture_output=True, text=True, timeout=120)
    _ensure_redroid_dns(net, dns1)
    # 等开机后安装 bridge App 并下发加固 jar（幂等，已在持久 data 中则跳过）
    for _ in range(40):
        if _redroid_booted():
            break
        time.sleep(1.5)
    _install_bridge_assets()


def _install_bridge_assets() -> None:
    """安装 catvod-bridge 并把用户自备加固 jar 放到 App 私有目录。

    APK 优先用公开镜像只读层自带的 /fnmedia/catvod-bridge.apk；镜像没有时
    回退完整版 fpk 里的 spider-runner/android/build/catvod-bridge.apk。
    加固 jar 只来自用户在管理页上传的 _USER_JAR（发布包不内置、镜像也不含）。
    """
    has_builtin = False
    try:
        chk = _docker_exec(["sh", "-c", "test -f /fnmedia/catvod-bridge.apk && echo y"], timeout=8)
        has_builtin = chk.stdout.strip() == "y"
    except Exception:
        _log.debug("_install_bridge_assets failed", exc_info=True)
        has_builtin = False
    if has_builtin:
        _docker_exec(["pm", "install", "-r", "/fnmedia/catvod-bridge.apk"], timeout=60)
    else:
        apk = (Path(__file__).resolve().parent.parent / "spider-runner" / "android"
               / "build" / "catvod-bridge.apk")
        if apk.is_file():
            subprocess.run(["docker", "cp", str(apk),
                            f"{REDROID_CONTAINER}:/data/local/tmp/bridge.apk"],
                           capture_output=True, text=True, timeout=60)
            _docker_exec(["chmod", "644", "/data/local/tmp/bridge.apk"], timeout=10)
            _docker_exec(["pm", "install", "-r", "/data/local/tmp/bridge.apk"], timeout=60)
    _push_user_jar()


def _push_user_jar() -> bool:
    """把用户自备 jar 下发到正在运行的 redroid 设备固定路径。容器未运行则只等下次。"""
    if not _USER_JAR.is_file():
        return False
    return push_device_jar(_USER_JAR.read_bytes(), DEVICE_JAR)


# 设备端普通爬虫 jar 的根目录（与 guard.jar 同级，bridge 按请求的 jar 路径加载）。
DEVICE_SPIDER_JAR_DIR = "/data/data/com.fnmedia.catvodbridge/files/jars"


def push_device_jar(data: bytes, device_path: str) -> bool:
    """把任意 spider jar 字节流下发到 redroid 设备内指定绝对路径。

    用于普通 csp_ 爬虫的多 jar 支持（每个订阅 spider jar 一个文件，bridge 的
    DexClassLoader 按 jar 路径缓存）。会广播到【全部】运行中的 redroid 池容器；
    无可用容器返回 False（下次启动可补推）。
    """
    if not data:
        return False
    containers = pool_containers()
    if not containers:
        return False
    tmp_name = os.path.basename(device_path) or "spider.jar"
    host_tmp = f"/tmp/{tmp_name}.{os.getpid()}"
    d = os.path.dirname(device_path) or DEVICE_SPIDER_JAR_DIR
    try:
        with open(host_tmp, "wb") as fh:
            fh.write(data)
        os.chmod(host_tmp, 0o644)
        ok_any = False
        for cname in containers:
            # 每台池容器都要一份：runner 会把调用负载到不同设备，缺 jar 的设备会
            # 抛 ClassNotFoundException(DexPathList 空)。
            subprocess.run(["docker", "cp", host_tmp,
                            f"{cname}:/data/local/tmp/{tmp_name}"],
                           capture_output=True, text=True, timeout=180)
            # 关键：jar 属主必须为 bridge App uid/gid（与 files 目录一致）。仅 666
            # 时文件仍 root:root，部分 redroid/SELinux 下 App 能 stat 却无法把 dex
            # mmap 进 ClassLoader（DexPathList 为空）。
            r = subprocess.run(
                ["docker", "exec", cname, "sh", "-c",
                 f"mkdir -p {d}; cp /data/local/tmp/{tmp_name} {device_path}; "
                 f"OWN=$(stat -c '%u:%g' /data/data/com.fnmedia.catvodbridge 2>/dev/null); "
                 f"chown \"$OWN\" {device_path} 2>/dev/null; "
                 f"chmod 600 {device_path} && echo ok"],
                capture_output=True, text=True, timeout=40)
            if "ok" in (r.stdout or ""):
                ok_any = True
        return ok_any
    finally:
        try:
            os.unlink(host_tmp)
        except OSError:
            pass


def device_jar_present(device_path: str, size: int | None = None) -> bool:
    """设备内某 jar 是否存在（可校验大小）。"""
    if not _container_running():
        return False
    cmd = f"test -f {device_path}"
    if size:
        cmd += f" && test $(stat -c %s {device_path}) -eq {int(size)}"
    r = _docker_exec(["sh", "-c", cmd], timeout=8)
    return r.returncode == 0


def save_user_jar(data: bytes, filename: str = "") -> dict:
    """保存用户上传的加固 jar（校验 zip/jar 魔数与大小），并立即下发到设备。"""
    if not data:
        return {"ok": False, "error": "文件为空"}
    if len(data) > MAX_JAR_BYTES:
        return {"ok": False, "error": f"jar 过大（{len(data)} 字节，上限 60MB）"}
    if data[:2] != b"PK":
        return {"ok": False, "error": "不是有效的 jar/apk（zip）文件"}
    _ASSET.mkdir(parents=True, exist_ok=True)
    tmp = _USER_JAR.with_suffix(".jar.upload")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    tmp.replace(_USER_JAR)
    pushed = _push_user_jar()
    return {"ok": True, "size": len(data), "filename": filename or "guard.jar",
            "pushed": pushed,
            "hint": ("已保存并下发到运行中的 redroid" if pushed else
                     "已保存；redroid 未运行时会在下次启动/部署时自动下发")}


def user_jar_status() -> dict:
    """用户自备 jar 的本地与设备状态（不返回文件内容）。"""
    local = _USER_JAR.is_file()
    size = _USER_JAR.stat().st_size if local else 0
    device = False
    if local and _container_running():
        try:
            r = _docker_exec(["sh", "-c",
                              f"test -f {DEVICE_JAR} && stat -c %s {DEVICE_JAR}"], timeout=8)
            device = bool((r.stdout or "").strip().isdigit())
        except Exception:
            _log.debug("user_jar_status failed", exc_info=True)
            device = False
    return {"present": local, "size": size, "on_device": device,
            "container_running": _container_running()}


def _redroid_bringup(timeout_s: float = 120.0) -> None:
    """等 redroid 开机后拉起 bridge App（App 不自启），轮询直到 bridge 健康。"""
    _ensure_binderfs()
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if _redroid_booted():
            break
        time.sleep(1.5)
    for _ in range(3):
        try:
            _docker_exec(["am", "start", "-n", _BRIDGE_COMPONENT], timeout=10)
        except Exception:
            _log.debug("_redroid_bringup failed", exc_info=True)
            pass
        for _ in range(20):
            if _redroid_bridge_up():
                return
            time.sleep(1.0)


def stop_android() -> dict:
    """手动停止 redroid：写停止标记并关掉自动重启，立即释放约 0.7–0.9GB 内存。

    常驻模式下，停止后看门狗与 Docker 都不会把它拉起，直到用户再次手动启动。
    """
    try:
        _request_manual_stop()
        if not _container_exists():
            return {"ok": True, "log": "redroid 容器不存在", "backend": "redroid"}
        # 手动停止：确保 Docker 不会因 unless-stopped 立刻把它拉回来
        subprocess.run(["docker", "update", "--restart=no", REDROID_CONTAINER],
                       capture_output=True, text=True, timeout=10)
        if _container_running():
            subprocess.run(["docker", "stop", "-t", "8", REDROID_CONTAINER],
                           capture_output=True, text=True, timeout=30)
            return {"ok": True, "log": "redroid 已停止，内存已释放；需要时点“启动运行时”", "backend": "redroid"}
        return {"ok": True, "log": "redroid 本就未运行", "backend": "redroid"}
    except Exception as exc:  # noqa: BLE001
        _log.debug("stop_android failed", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _start_android_blocking() -> None:
    """创建（必要时拉镜像）或启动容器，再等待开机并拉起 bridge。"""
    _SIDECAR_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _clear_manual_stop()
    _ensure_binderfs()
    if not _container_exists():
        _create_redroid()          # 内部解析/拉取镜像（首次较慢）
    else:
        # 手动启动：恢复常驻重启策略（除非用户显式关掉常驻）
        policy = REDROID_RESTART_POLICY if REDROID_RESIDENT else "no"
        subprocess.run(["docker", "update", f"--restart={policy}", REDROID_CONTAINER],
                       capture_output=True, text=True, timeout=10)
        subprocess.run(["docker", "start", REDROID_CONTAINER],
                       capture_output=True, text=True, timeout=30)
        _ensure_redroid_dns(os.environ.get("FNM_SPIDER_NET", "fnmedia-spider"),
                            os.environ.get("FNM_REDROID_DNS", "223.5.5.5"))
    _mark_android_used()
    _redroid_bringup(timeout_s=150)
    try:
        _push_user_jar()
    except Exception:
        _log.debug("_start_android_blocking failed", exc_info=True)
        pass


def start_android(wait_ready: bool = False) -> dict:
    """手动启动 redroid（冷启约10–30秒；首次需拉镜像可能数分钟）。"""
    if not _docker():
        return {"ok": False, "error": "宿主机无 docker"}
    if _container_running():
        return {"ok": True, "starting": False, "backend": "redroid",
                "ready": _redroid_bridge_up(), "hint": "redroid 已在运行"}
    if not _android_start_lock.acquire(blocking=False):
        return {"ok": True, "starting": True, "backend": "redroid",
                "hint": "已有启动任务在进行"}
    try:
        if wait_ready:
            try:
                _start_android_blocking()
                return {"ok": True, "ready": _redroid_bridge_up(), "backend": "redroid"}
            except Exception as exc:  # noqa: BLE001
                _log.debug("start_android failed", exc_info=True)
                return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

        def _bg():
            try:
                _start_android_blocking()
            except Exception:
                _log.debug("_bg failed", exc_info=True)
                pass
            finally:
                try:
                    _android_start_lock.release()
                except RuntimeError:
                    pass
        threading.Thread(target=_bg, name="redroid-start", daemon=True).start()
        pulling = (not _redroid_image_present()) and (not _container_exists())
        hint = ("正在首次拉取 redroid 镜像（约2GB）并启动，可能数分钟，稍后查看状态"
                if pulling else "redroid 冷启约 10–30 秒，稍后查看 android 是否就绪")
        return {"ok": True, "starting": True, "backend": "redroid",
                "pulling": pulling, "hint": hint}
    finally:
        # 同步分支需要立刻释放；异步分支由后台线程释放
        if wait_ready:
            try:
                _android_start_lock.release()
            except RuntimeError:
                pass


def android_ready() -> bool:
    """bridge（宿主9110）是否健康。"""
    return _container_running() and _redroid_bridge_up()


# ----------------------------- 常驻 / 开机自启 -----------------------------
_resident_started = False
_resident_lock = threading.Lock()


def resident_enabled() -> bool:
    return REDROID_RESIDENT


def _resident_should_run() -> bool:
    """常驻模式且容器已预置、且用户未手动停止时，应保持运行。"""
    return (REDROID_RESIDENT and _container_exists()
            and not _manual_stop_requested())


def autostart_android(blocking: bool = False) -> dict:
    """插件启动 / 开机时调用：常驻模式下自动拉起已预置的 redroid。

    只在容器已存在时动作（首次部署仍由“一键部署”完成，避免插件开机即拉 2GB 镜像）；
    用户手动停止后（存在停止标记）不自启。
    """
    if not _resident_should_run():
        return {"ok": True, "started": False,
                "reason": ("resident_off" if not REDROID_RESIDENT
                           else "manual_stop" if _manual_stop_requested()
                           else "not_provisioned")}
    if _container_running() and _redroid_bridge_up():
        return {"ok": True, "started": False, "reason": "already_ready"}
    if blocking:
        try:
            _start_android_blocking()
            return {"ok": True, "started": True, "ready": _redroid_bridge_up()}
        except Exception as exc:  # noqa: BLE001
            _log.debug("autostart_android failed", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    # 后台拉起，不阻塞插件启动
    start_android(wait_ready=False)
    return {"ok": True, "started": True, "reason": "background"}


def _resident_watchdog() -> None:
    """常驻看门狗：容器异常退出自动拉起；容器在跑但 bridge 掉了则重新拉起 App。

    用户手动停止（停止标记存在）期间完全不干预，直到用户再次手动启动。
    """
    time.sleep(20)  # 插件开机给 redroid/boot 一点缓冲
    consecutive_fail = 0
    while True:
        time.sleep(20)
        try:
            if not _resident_should_run():
                continue
            if not _container_running():
                # 容器退出：恢复常驻策略并后台启动（含开机 + bridge + jar）
                _log.info("resident: redroid 未运行，自动拉起")
                start_android(wait_ready=False)
                continue
            if not _redroid_bridge_up():
                # 容器活着但 bridge App 没起 / 掉了：重新拉起 App
                _log.info("resident: bridge 未就绪，重新拉起 bridge App")
                _redroid_bringup(timeout_s=45)
                if _redroid_bridge_up():
                    consecutive_fail = 0
                else:
                    consecutive_fail += 1
            else:
                consecutive_fail = 0
        except Exception as exc:  # noqa: BLE001
            consecutive_fail += 1
            _log.warning("resident watchdog error: %s", exc)


def start_resident_watchdog() -> None:
    """启动常驻看门狗（进程内单例，daemon 线程）。常驻模式关闭时不启动。"""
    global _resident_started
    if not REDROID_RESIDENT:
        return
    with _resident_lock:
        if _resident_started:
            return
        _resident_started = True
    threading.Thread(target=_resident_watchdog, name="redroid-resident",
                     daemon=True).start()


def maybe_reap_android() -> str:
    """空闲自动回收已关闭（常驻模型下更无需回收），恒为空操作。"""
    return ""



def feature_on() -> bool:
    # 安装包不内置源目录；有内置 sites.json 或用户导入 imported.json 之一即启用。
    if not bool(getattr(features, "GUARD", False)):
        return False
    return _SITES.exists() or _IMPORTED.exists()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.debug("_read_json failed", exc_info=True)
        return {}


def _load_sites() -> dict[str, dict]:
    """合并内置 sites.json 与用户导入 imported.json；同 key 以导入覆盖内置。

    每条带 `_builtin` 标记，便于前端区分与单独删除。
    """
    global _sites
    if _sites is None:
        merged: dict[str, dict] = {}
        base = _read_json(_SITES)
        for s in base.get("sites", []):
            s = dict(s)
            s["_builtin"] = True
            merged[s["key"]] = s
        imp = _read_json(_IMPORTED)
        for s in imp.get("sites", []):
            s = dict(s)
            s["_builtin"] = False
            merged[s["key"]] = s
        _sites = merged
    return _sites


def reload_sites() -> dict[str, dict]:
    """导入/删除后清空缓存重新加载。"""
    global _sites
    with _lock:
        _sites = None
        return _load_sites()


def _invalidate_site_caches(key: str) -> None:
    """源定义更新后，按 key 前缀清该源的详情/浏览缓存。

    _DETAIL_CACHE（TTL 1800s）与 _BROWSE_CACHE（TTL 600s）否则会在源定义
    更新后 30 分钟内仍返回旧详情/旧列表。只清该 key 前缀，不动其它源。
    """
    prefix = f"{key}|"
    with _lock:
        for k in [k for k in _DETAIL_CACHE if k.startswith(prefix)]:
            _DETAIL_CACHE.pop(k, None)
        for k in [k for k in _BROWSE_CACHE if k.startswith(prefix)]:
            _BROWSE_CACHE.pop(k, None)


# ----------------------------------------------------------- TVBox 配置解析

# 风险/重资源加固源：默认关闭，避免拖慢聚合或缺登录态报错；可手动启用。
_RISK_NAME = re.compile(
    r"(P2P|磁力|磁鏈|云盘|雲盤|网盘|網盤|夸克|誇克|阿里|alipan|aliyun|115|"
    r"百度|baiduwp|UC网盘|迅雷|thunder|我的|私人|收藏)", re.I)
_RISK_API = re.compile(r"(quark|uc\.cn|aliyun|alipan|aliyundrive|115|baidu|"
                       r"weiyun|xunlei|cloud189|pan\.)", re.I)


def _is_risky_site(site: dict) -> tuple[bool, str]:
    """返回 (是否风险, 原因)。dict 型 ext 通常是云盘登录态等外部配置。"""
    if isinstance(site.get("ext"), dict):
        return True, "需外部配置/云盘登录态"
    # 已适配的网盘聚合源：其搜索结果直接是夸克/UC 分享链接，且我们内置 pan.py/panuc.py
    # 用【自有登录态】转存原画播放（>2GB 也可），不依赖源站登录态。实测免 token 可搜，
    # 故不算风险源、可默认启用（route3：网盘资源也能在飞牛影视里直接观看）。
    if str(site.get("key", "")) in _PAN_PLAYABLE_KEYS:
        return False, ""
    name = str(site.get("name") or "")
    api = str(site.get("api") or "")
    if _RISK_NAME.search(name) or _RISK_API.search(api):
        return True, "P2P/磁力/云盘类（较慢或缺登录态）"
    return False, ""


# 网盘聚合源白名单（import key）：输出夸克/UC 分享链接、免 token、已由自有网盘模块播放。
# YpanSo=盘她·夸父（vod_id 即 pan.quark.cn 分享，detail 含 fid/sToken 可直接转存）。
_PAN_PLAYABLE_KEYS = {"YpanSo"}


def parse_tvbox(text_or_obj) -> dict:
    """把一份 TVBox 配置（JSON 字符串/对象）解析成可导入的加固源。

    只提取 type=3、api=csp_*Guard 的源（我们的 Android 加固运行时能跑的）。
    需要 dict 型 ext（Cloud-drive 等外部配置）的源标记 need_config=True，
    仍导入但默认不启用。返回 {"sites":[...], "skipped":n, "spider":...}。
    """
    if isinstance(text_or_obj, (bytes, bytearray)):
        text_or_obj = text_or_obj.decode("utf-8", "ignore")
    if isinstance(text_or_obj, str):
        # 去掉文件头 // 注释
        cleaned = re.sub(r"^\s*//.*$", "", text_or_obj, flags=re.M)
        obj = json.loads(cleaned)
    else:
        obj = text_or_obj
    out, seen, skipped = [], set(), 0
    count: dict[str, int] = {}
    for s in obj.get("sites", []) or []:
        api = str(s.get("api", ""))
        if str(s.get("type")) != "3" or not (api.startswith("csp_") and api.endswith("Guard")):
            skipped += 1
            continue
        spider = api[4:]                 # e.g. BttwooGuard / AppSxGuard
        base = spider[:-5]
        # 同 spider 多配置（不同加密 ext）→ 追加序号，保证 key 为 ASCII 且唯一
        count[base] = count.get(base, 0) + 1
        key = base if count[base] == 1 else f"{base}{count[base]}"
        if key in seen:
            continue
        seen.add(key)
        ext = s.get("ext")
        need_config = isinstance(ext, dict)
        risky, risk_reason = _is_risky_site(s)
        out.append({
            "key": key,
            "spider": spider,
            "api": api,
            "class": "com.github.catvod.spider." + spider,
            "name": str(s.get("name", key)).strip() or key,
            "ext": ext if isinstance(ext, str) else "",
            "searchable": bool(s.get("searchable", 1)),
            "quickSearch": bool(s.get("quickSearch", 0)),
            "need_config": need_config,
            "risk": bool(risky),
            "risk_reason": risk_reason,
            # 导入即登记但默认关闭：部分源（P2P/磁力/云盘）很慢或缺外部配置，
            # 全开拖慢聚合；由用户在管理页勾选，或导入时勾“全部启用”。
            "enabled": False,
            "_builtin": False,
        })
    return {"sites": out, "skipped": skipped,
            "spider": obj.get("spider", ""), "total": len(obj.get("sites", []) or [])}


def import_tvbox(text_or_obj, enable=None) -> dict:
    """解析并合并写入 imported.json。

    enable:
      True/False —— 新导入源统一启用/停用；
      None       —— 沿用旧行为（新源默认停用，等待手动勾选）；
      "smart"    —— 智能启用：安全源自动启用、风险源（risk=True）默认停用。
    已存在的源始终保留用户既有启用态，仅更新定义。
    返回 {added,updated,sites,enabled_keys,risk_keys,...}。
    """
    parsed = parse_tvbox(text_or_obj)
    incoming = parsed["sites"]
    imp = _read_json(_IMPORTED)
    cur = {s["key"]: s for s in imp.get("sites", [])}
    # 导入前的“当前生效视图”（含内置源），用于同名 key 时保留既有启用态。
    live = {k: v for k, v in _load_sites().items()}
    added, updated = 0, 0

    def _want_enabled(site: dict) -> bool:
        if enable == "smart":
            return not site.get("risk")
        if enable is None:
            return bool(site.get("enabled", False))
        return bool(enable)

    for s in incoming:
        s.pop("_builtin", None)
        if s["key"] in cur:
            # 已在导入文件中：保留用户既有启用态，仅更新定义
            s["enabled"] = bool(cur[s["key"]].get("enabled", s["enabled"]))
            cur[s["key"]].update(s)
            updated += 1
        elif s["key"] in live:
            # 与内置源同名：用导入定义覆盖 ext/名称，但继承内置/当前启用态
            s["enabled"] = bool(live[s["key"]].get("enabled", s["enabled"]))
            cur[s["key"]] = s
            updated += 1
        else:
            s["enabled"] = _want_enabled(s)
            cur[s["key"]] = s
            added += 1
    imp["sites"] = list(cur.values())
    _ASSET.mkdir(parents=True, exist_ok=True)
    tmp = _IMPORTED.with_suffix(".tmp")
    tmp.write_text(json.dumps(imp, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_IMPORTED)
    reload_sites()
    for _s in incoming:
        _invalidate_site_caches(_s["key"])
    enabled_keys = [k for k, v in cur.items() if v.get("enabled")]
    risk_keys = [s["key"] for s in incoming if s.get("risk")]
    return {**parsed, "added": added, "updated": updated,
            "enabled_keys": enabled_keys, "risk_keys": risk_keys}


def remove_imported(key: str) -> bool:
    imp = _read_json(_IMPORTED)
    sites = [s for s in imp.get("sites", []) if s.get("key") != key]
    if len(sites) == len(imp.get("sites", [])):
        return False
    imp["sites"] = sites
    tmp = _IMPORTED.with_suffix(".tmp")
    tmp.write_text(json.dumps(imp, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_IMPORTED)
    reload_sites()
    _invalidate_site_caches(key)
    return True


def set_enabled(key: str, on: bool) -> bool:
    """启用/停用一个加固源。内置源写 imported.json 的覆盖项；导入源直接改。"""
    sites = _load_sites()
    if key not in sites:
        return False
    imp = _read_json(_IMPORTED)
    cur = {s["key"]: s for s in imp.get("sites", [])}
    if key in cur:
        cur[key]["enabled"] = bool(on)
    else:
        # 内置源：生成一条只含 key/enabled 的覆盖
        base = sites[key]
        cur[key] = {"key": key, "spider": base.get("spider", key + "Guard"),
                    "api": base.get("api", "csp_" + key + "Guard"),
                    "class": base.get("class"), "name": base.get("name"),
                    "ext": base.get("ext", ""),
                    "searchable": base.get("searchable", True),
                    "enabled": bool(on)}
    imp["sites"] = list(cur.values())
    tmp = _IMPORTED.with_suffix(".tmp")
    tmp.write_text(json.dumps(imp, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_IMPORTED)
    reload_sites()
    _invalidate_site_caches(key)
    return True


def site_keys() -> list[str]:
    return list(_load_sites().keys()) if feature_on() else []


def site(key: str) -> dict | None:
    return _load_sites().get(key)


def _runner_post(path: str, payload: dict, timeout: float | None = None):
    r = httpx.post(RUNNER_URL + path, json=payload, timeout=timeout or TIMEOUT,
                   trust_env=False)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(str(j.get("error") or j)[:400])
    return j.get("result")


def health(force: bool = False) -> dict:
    """返回 runner 与 android 后端状态；短时缓存，避免拖垮聚合搜索。"""
    global _health_cache
    now = time.time()
    if not force and now - _health_cache[0] < 10:
        return _health_cache[1]
    if not feature_on():
        out = {"ok": False, "reason": "feature-off"}
    else:
        try:
            # 连接被拒（runner 未部署）应瞬时返回，只有真机响应慢才用满超时。
            r = httpx.get(RUNNER_URL + "/health",
                          timeout=httpx.Timeout(2.0, connect=0.4), trust_env=False)
            j = r.json()
            out = {"ok": bool(j.get("runtimes", {}).get("android")),
                   "runner": RUNNER_URL, "runtimes": j.get("runtimes", {})}
        except Exception as exc:  # noqa: BLE001
            _log.debug("health failed", exc_info=True)
            out = {"ok": False, "runner": RUNNER_URL, "error": str(exc)[:160]}
    _health_cache = (now, out)
    return out


def available() -> bool:
    return feature_on() and bool(health().get("ok"))


def _is_transport_error(exc: BaseException) -> bool:
    """传输层失败（超时/连不上）：才可能说明桥/后端本身坏了。

    `_runner_post` 对业务失败（runner 返回 ok:false）抛 RuntimeError，
    那是源/参数问题，不计入看门狗。"""
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


def _invoke_once(payload: dict):
    """一次 /invoke 调用：有界拿信号量，显式重试超时；信号量一定释放。"""
    if not _bridge_sem.acquire(timeout=_BRIDGE_SEM_TIMEOUT):
        raise TimeoutError(
            f"guard 调用排队超时（{_BRIDGE_SEM_TIMEOUT}s 内未拿到 bridge 许可）")
    try:
        return _runner_post("/invoke", payload, timeout=_GUARD_RETRY_TIMEOUT)
    finally:
        _bridge_sem.release()


def _call(site_conf: dict, method: str, **params):
    global _bridge_fail, _bridge_restart_cooldown, _last_failed_runner_url
    payload = {
        "runtime": "android",
        "jar": DEVICE_JAR,
        "class": site_conf["class"],
        "ext": site_conf.get("ext") or "",
        "method": method,
        **params,
    }
    # 多 redroid 实例池：有界并发，runner 负责把调用分到不同设备并各自串行。
    try:
        res = _invoke_once(payload)
    except Exception as exc:  # noqa: BLE001
        if not _is_transport_error(exc):
            # 业务失败（ok:false 等）：不计数、不重启，保持 debug。
            _log.debug("_call failed", exc_info=True)
            raise
        # 传输失败才进看门狗，且只在探活也失败时计数：runner 还活着说明
        # 只是单次调用抖动/超时，重启整桥是误伤。
        _last_failed_runner_url = RUNNER_URL
        _log.warning("_call transport failed: %s: %s",
                     type(exc).__name__, str(exc)[:160])
        try:
            runner_down = not available()
        except Exception:  # noqa: BLE001
            _log.debug("_call available probe failed", exc_info=True)
            runner_down = False
        if not runner_down:
            raise
        with _lock:
            _bridge_fail += 1
            fail = _bridge_fail
        now = time.time()
        if fail >= _BRIDGE_FAIL_RESTART and now >= _bridge_restart_cooldown:
            # 信号量已在 _invoke_once 内释放：重启只在 _bridge_restart_lock
            # 内串行，不占 bridge 许可；重启本身会写冷却（失败 120s）。
            with _bridge_restart_lock:
                try:
                    _restart_bridge_app()
                except Exception:  # noqa: BLE001
                    _log.debug("_call restart failed", exc_info=True)
                    pass
            # 只重试一次本次调用（重新排队拿许可，同样显式超时）。
            # 再失败也不清零 _bridge_fail，但重启写下的冷却会阻止立即再触发。
            res = _invoke_once(payload)
            with _lock:
                _bridge_fail = 0
        else:
            raise
    else:
        with _lock:
            _bridge_fail = 0
    _mark_android_used()  # 真实加固调用成功，刷新空闲计时
    return res


def home(site_conf: dict) -> dict:
    res = _call(site_conf, "home", filter=True)
    return res if isinstance(res, dict) else {}


def category(site_conf: dict, tid: str, pg: int | str = 1, extend: dict | None = None) -> dict:
    res = _call(site_conf, "category", tid=str(tid), pg=str(pg), filter=False,
                extend=extend or {})
    return res if isinstance(res, dict) else {}


def search(site_conf: dict, keyword: str, quick: bool = False, pg: int | str = 1) -> list[dict]:
    res = _call(site_conf, "search", key=keyword, quick=bool(quick), pg=str(pg))
    if isinstance(res, dict):
        return res.get("list") or []
    return res if isinstance(res, list) else []


_DETAIL_CACHE: dict[str, tuple] = {}
_DETAIL_TTL = 1800


def detail(site_conf: dict, vod_id: str, use_cache: bool = True) -> dict:
    key = f"{site_conf['key']}|{vod_id}"
    now = time.time()
    if use_cache:
        with _lock:
            ent = _DETAIL_CACHE.get(key)
            if ent and now - ent[0] < _DETAIL_TTL:
                return ent[1]
    res = _call(site_conf, "detail", ids=[str(vod_id)])
    out = {}
    if isinstance(res, dict):
        items = res.get("list") or []
        if items:
            out = items[0]
    with _lock:
        if out:
            _DETAIL_CACHE[key] = (now, out)
            if len(_DETAIL_CACHE) > 800:
                for k in list(_DETAIL_CACHE)[:400]:
                    _DETAIL_CACHE.pop(k, None)
    return out


def play(site_conf: dict, flag: str, vod_id: str) -> dict:
    res = _call(site_conf, "play", flag=flag or "", id=str(vod_id), vipFlags=[])
    # 多数 Spider 返回 JSON 字符串；bridge 已尽量解析，这里再兜底一次。
    if isinstance(res, str) and res.strip().startswith("{"):
        try:
            res = json.loads(res)
        except ValueError:
            _log.debug("play failed", exc_info=True)
            return {"url": res}
    if not isinstance(res, dict):
        return {"url": _normalize_url(str(res or ""))}
    # 某些爬虫返回 TVBox 端本地代理占位地址（127.0.0.1:-1/proxy?do=m3u8&url=...）。
    # 插件不是 TVBox，需把内嵌的真实媒体 url 解出来，否则该地址无法连接。
    res["url"] = _normalize_url(res.get("url", ""))
    headers = res.get("headers") if "headers" in res else res.get("header")
    if isinstance(headers, str) and headers:
        try:
            headers = json.loads(headers)
        except ValueError:
            _log.debug("play failed", exc_info=True)
            headers = {}
    res["_headers"] = headers or {}
    return res


def _normalize_url(url: str) -> str:
    if not url:
        return url
    from urllib.parse import parse_qs, urlparse
    p = urlparse(url)
    if p.hostname in ("127.0.0.1", "localhost") and p.path.endswith("/proxy"):
        q = parse_qs(p.query)
        inner = q.get("url") or q.get("target") or q.get("u")
        if inner and inner[0]:
            return inner[0]
    return url


def parse_episodes(detail: dict) -> list[dict]:
    froms = (detail.get("vod_play_from") or "").split("$$$")
    groups = (detail.get("vod_play_url") or "").split("$$$")
    out = []
    for gi, group in enumerate(groups):
        raw_from = froms[gi].strip() if gi < len(froms) else ""
        # 部分源把线路名写成「播放线路=line1」，playerContent 需要等号后的真实 flag
        if "=" in raw_from:
            raw_from = raw_from.split("=", 1)[1].strip()
        line = raw_from or f"线路{gi+1}"
        for item in group.split("#"):
            item = item.strip()
            if not item or "$" not in item:
                continue
            name, vid = item.split("$", 1)
            ep_name = name.strip() or f"第{len(out)+1}集"
            # 选集层去广告：跳过“关注公众号/加微信/下载APP”等引流假选集
            try:
                import adfilter as _adf
                if _adf.is_ad_episode(ep_name):
                    continue
            except Exception:
                _log.debug("parse_episodes failed", exc_info=True)
                pass
            if vid:
                out.append({"line": line, "title": ep_name, "id": vid})
    return out


# ----------------------------------------------------------- 专题栏浏览

# 每个源 home（分类表）短时缓存：key -> (ts, home_dict)，避免专区库每次打开都打真机。
_HOME_CACHE: dict[str, tuple] = {}
# 专区库封面缓存：key -> [poster,...]；仅由后台预热填充，库列表接口绝不为此联网。
_POSTER_CACHE: dict[str, list] = {}
_POSTER_PREFETCHED: set[str] = set()


def cached_posters(key: str, n: int = 8) -> list[str]:
    with _lock:
        return list(_POSTER_CACHE.get(key, [])[:n])


def prefetch_posters(site_conf: dict) -> list[str]:
    """后台调用：取前 3 个分类首页的海报写入缓存（同步、在线程/任务里跑）。"""
    key = site_conf["key"]
    ps: list[str] = []
    try:
        for c in (browse_classes(site_conf) or [])[:3]:
            for v in browse_page(site_conf, c["tid"], 1)[:8]:
                pic = str(v.get("vod_pic") or "").strip()
                if pic and pic not in ps:
                    ps.append(pic)
                if len(ps) >= 8:
                    break
            if len(ps) >= 8:
                break
    except Exception:
        _log.debug("prefetch_posters failed", exc_info=True)
        pass
    with _lock:
        if ps:
            _POSTER_CACHE[key] = ps
        _POSTER_PREFETCHED.add(key)
    return ps


def home_cached(site_conf: dict, force: bool = False) -> dict:
    key = site_conf["key"]
    now = time.time()
    with _lock:
        ent = _HOME_CACHE.get(key)
        if not force and ent and now - ent[0] < 1800:
            return ent[1]
    h = home(site_conf)
    with _lock:
        _HOME_CACHE[key] = (now, h)
    return h


def browse_classes(site_conf: dict) -> list[dict]:
    """该源可浏览分类（过滤掉明显非影视的，如听书/音乐/看球）。"""
    h = home_cached(site_conf)
    out = []
    for c in h.get("class", []) or []:
        tid = str(c.get("type_id", "")).strip()
        name = str(c.get("type_name", "")).strip()
        if not tid or not name:
            continue
        out.append({"tid": tid, "name": name})
    return out


def browse_meta(site_conf: dict) -> dict:
    """一次返回影视分类与各分类的源自带筛选（filters 预设，纯本地解析、秒回）。

    返回 {"classes":[{tid,name}], "filters": {tid: [{key,name,value:[{v,n}]}]}}。
    filters 即源 homeContent() 里按分类预设的地区/年份/题材/语言/排序等，
    与 OK影视/TVBox 分类页顶部的二级筛选一致。"""
    h = home_cached(site_conf)
    classes = []
    for c in h.get("class", []) or []:
        tid = str(c.get("type_id", "")).strip()
        name = str(c.get("type_name", "")).strip()
        if tid and name:
            classes.append({"tid": tid, "name": name})
    raw_filters = h.get("filters") or {}
    filters: dict = {}
    if isinstance(raw_filters, dict):
        for c in classes:
            tid = c["tid"]
            fl = raw_filters.get(tid)
            if isinstance(fl, list):
                norm = []
                for f in fl:
                    if not isinstance(f, dict):
                        continue
                    vals = []
                    for opt in (f.get("value") or []):
                        if isinstance(opt, dict):
                            v = "" if opt.get("v") is None else str(opt.get("v"))
                            n = str(opt.get("n") if opt.get("n") is not None
                                    else opt.get("v") or "")
                            vals.append({"v": v, "n": n})
                    if vals and f.get("key"):
                        norm.append({"key": str(f.get("key")),
                                     "name": str(f.get("name") or f.get("key")),
                                     "value": vals})
                if norm:
                    filters[tid] = norm
    return {"classes": classes, "filters": filters}


_BROWSE_CACHE: dict[str, tuple] = {}
_BROWSE_TTL = 600


def invalidate_browse(key: str, tid: str, pg: int) -> None:
    _BROWSE_CACHE.pop(f"{key}|{tid}|{pg}", None)


def browse_page(site_conf: dict, tid: str, pg: int = 1) -> list[dict]:
    """取某分类一页影片（catvod Vod 列表 dict），短时缓存避免反复打真机。"""
    key = f"{site_conf['key']}|{tid}|{pg}"
    now = time.time()
    with _lock:
        ent = _BROWSE_CACHE.get(key)
        if ent and now - ent[0] < _BROWSE_TTL:
            return ent[1]
    lst = category(site_conf, tid, pg).get("list") or []
    if not lst and int(pg) == 1:
        # 冷启动（模拟器/bridge 刚唤醒）首分类可能偶发空，退避重试两次。
        for delay in (0.8, 1.6):
            time.sleep(delay)
            lst = category(site_conf, tid, pg).get("list") or []
            if lst:
                break
    with _lock:
        if lst:  # 空结果不缓存，避免污染后续请求
            _BROWSE_CACHE[key] = (now, lst)
        if len(_BROWSE_CACHE) > 400:
            for k in list(_BROWSE_CACHE)[:200]:
                _BROWSE_CACHE.pop(k, None)
    return lst


def browse_page_ext(site_conf: dict, tid: str, pg: int = 1,
                    extend: dict | None = None) -> list[dict]:
    """带源自带筛选（地区/年份/题材/语言/排序…）取分类一页。

    extend 即 CatVod categoryContent 的筛选参数（area/year/…）。无 extend 时走
    带缓存的 browse_page；有 extend 时直接实时爬（组合多、不做长期缓存），冷启动
    首分类空则退避重试一次。"""
    if not extend:
        return browse_page(site_conf, tid, pg)
    lst = category(site_conf, tid, pg, extend=extend).get("list") or []
    if not lst and int(pg) == 1:
        time.sleep(1.0)
        lst = category(site_conf, tid, pg, extend=extend).get("list") or []
    return lst


def filters_for(site_conf: dict, tid: str) -> list[dict]:
    """某分类的源自带筛选预设（无则空列表）。"""
    try:
        return (browse_meta(site_conf).get("filters") or {}).get(str(tid)) or []
    except Exception:  # noqa: BLE001
        _log.debug("filters_for failed: tid=%s", tid, exc_info=True)
        return []


def status() -> dict:
    """加固源总体状态。

    state 区分三种情况，不只靠 feature_on() 一个布尔值：
    - "no_sites"：没有任何源定义（干净检出：内置 sites.json 不在版本控制内、
      也未导入过 TVBox 配置，guard 搜索/专区/msearch 全部无源）；
    - "runner_down"：有源但 runner 不通；
    - "ok"：有源且 runner 通。
    """
    sites = _load_sites()
    h = health(force=True)
    if not sites:
        state = "no_sites"
    elif not h.get("ok"):
        state = "runner_down"
    else:
        state = "ok"
    return {"available": available(), "runner": RUNNER_URL,
            "health": h, "state": state,
            "no_sites": not sites,
            "runner_down": bool(sites) and not h.get("ok"),
            "builtin": len([s for s in sites.values() if s.get("_builtin")]),
            "imported": len([s for s in sites.values() if not s.get("_builtin")]),
            "sites": [{"id": "guard-" + k, "key": k, "name": s.get("name"),
                       "enabled": bool(s.get("enabled")),
                       "searchable": bool(s.get("searchable", True)),
                       "builtin": bool(s.get("_builtin")),
                       "need_config": bool(s.get("need_config"))}
                      for k, s in sites.items()]}


# ============================================================
# sidecar（重运行时）安装/状态管理 —— 供管理页“一键安装”调用。
# ============================================================
import shutil
import subprocess

_RUNNER_ROOT = Path(__file__).resolve().parent.parent / "spider-runner"
_BOOTSTRAP = _RUNNER_ROOT / "deploy" / "bootstrap_sidecar.sh"
_SIDECAR_STATE = Path(__file__).resolve().parent.parent / "state"
_INSTALL_LOG = _SIDECAR_STATE / "sidecar-install.log"

_job_lock = threading.Lock()
_job: dict | None = None   # {pid, proc, started, what, log, rc}


def _container_up(name: str = REDROID_CONTAINER) -> bool:
    return _container_running(name)


def sidecar_status() -> dict:
    """重运行时总体状态（轻量、短超时，供管理页轮询）。"""
    out = {
        "kvm": os.path.exists("/dev/kvm"),
        "binderfs": os.path.exists("/dev/binderfs/binder-control"),
        "docker": _docker(),
        "guard_jar": user_jar_status(),
        "redroid_image": _redroid_image_present(),
        "redroid_image_name": REDROID_IMAGE,
        "redroid_base_present": _redroid_image_present(REDROID_BASE),
        "bootstrap_present": _BOOTSTRAP.is_file(),
        "android_backend": "redroid",
        "redroid_container_exists": _container_exists(),
        "redroid_container": _container_running(),
        "android_resident": REDROID_RESIDENT,  # 开机自启 + 常驻保活
        "android_manual_stop": _manual_stop_requested(),
        "android_manual": not REDROID_RESIDENT,  # 兼容旧字段
        "android_last_use": android_last_use(),
        "runner": RUNNER_URL,
        "java": False, "node": False, "android": False,
        "runner_ok": False, "error": "",
    }
    try:
        with httpx.Client(trust_env=False, timeout=2.5) as c:
            r = c.get(RUNNER_URL + "/health")
            j = r.json()
        rt = j.get("runtimes", {})
        out.update(runner_ok=bool(j.get("ok")), java=bool(rt.get("java")),
                   node=bool(rt.get("node")), android=bool(rt.get("android")))
    except Exception as ex:  # noqa: BLE001
        _log.debug("sidecar_status failed", exc_info=True)
        out["error"] = str(ex)[:120]
    with _job_lock:
        out["installing"] = bool(_job and _job.get("proc")
                                 and _job["proc"].poll() is None)
        out["install_what"] = _job.get("what") if _job else ""
    return out


def install_log_tail(n: int = 200) -> str:
    try:
        lines = _INSTALL_LOG.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        _log.debug("install_log_tail failed", exc_info=True)
        return ""


def install_running() -> bool:
    with _job_lock:
        return bool(_job and _job.get("proc") and _job["proc"].poll() is None)


def start_install(what: str = "all") -> dict:
    """异步启动 bootstrap（all|runner-only|android-only）；幂等，重复调用返回忙。"""
    global _job
    if install_running():
        return {"ok": False, "error": "已有安装任务在运行，请等待完成"}
    if not _BOOTSTRAP.is_file():
        return {"ok": False, "error": "完整版未包含 spider-runner 脚本（当前可能是公开构建）"}
    what = what if what in ("all", "runner-only", "android-only") else "all"
    _SIDECAR_STATE.mkdir(parents=True, exist_ok=True)

    # bootstrap 子进程直接写日志文件；Popen 不阻塞请求线程。
    with open(_INSTALL_LOG, "a") as lf:
        lf.write(f"\n===== sidecar install [{what}] {time.strftime('%H:%M:%S')} requested =====\n")
        proc = subprocess.Popen(["bash", str(_BOOTSTRAP), what],
                                stdout=lf, stderr=subprocess.STDOUT,
                                cwd=str(_RUNNER_ROOT.parent))
    with _job_lock:
        _job = {"pid": proc.pid, "proc": proc, "started": time.time(),
                "what": what, "log": str(_INSTALL_LOG)}
    return {"ok": True, "pid": proc.pid, "what": what}


def reconnect_network() -> dict:
    """redroid 容器重启后拉起设备内 bridge App（端口为容器直映，无需 adb forward）。"""
    try:
        if not _container_running():
            return {"ok": False, "error": "redroid 容器未运行，请先启动"}
        _redroid_bringup(timeout_s=60)
        ok = _redroid_bridge_up()
        return {"ok": ok, "out": "bridge 已就绪" if ok else "bridge 尚未就绪，稍后重试"}
    except Exception as ex:  # noqa: BLE001
        _log.debug("reconnect_network failed", exc_info=True)
        return {"ok": False, "error": str(ex)[:160]}
