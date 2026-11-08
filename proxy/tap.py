#!/usr/bin/env python3
"""fnmedia-ext 抓包代理（可逆 Unix Socket 接管）。

用途：在不改变飞牛影视 trim.media 任何行为的前提下，原子换走
/var/run/trim_media.sock，把全部 HTTP 流量透传给官方后端（换名后的
trim_media_upstream.sock），并把请求/响应的头部与关键报文落盘，用于摸清：
  - /v/api/v1 真实请求头里的签名（invalid sign 那套）长什么样
  - 搜索 / 详情 / 播放令牌(play_token) / 流地址 的真实报文结构

设计原则：
  - fail-closed：换 socket 前后用内核 SO_PEERCRED 校验对端 exe 必须是 trim-media
  - 纯透传：HTTP/1.1，对上游统一 Connection: close，逐字节透传响应
  - 只记录：JSON / m3u8 / 文本；视频分片、图片等二进制只记元信息不落盘
  - 自动回滚：进程被 kill / 崩溃 / 异常退出时把官方 socket 原子换回
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import json
import os
import signal
import socket
import stat
import struct
import sys
import time
from pathlib import Path

TARGET = Path("/var/run/trim_media.sock")
UPSTREAM = Path("/var/run/trim_media_upstream.sock")
STATE_DIR = Path("/run/fnmedia-tap")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "http"
OFFICIAL_EXE = "trim-media"

CAPTURE_CTYPES = ("json", "text", "mpegurl", "xml", "javascript", "x-mpegurl", "vnd.apple")
SKIP_BODY_PATH_HINTS = ("/thumb", "/rimg", "/image", "/poster", "/static/", ".png", ".jpg",
                        ".webp", ".ico", ".woff", ".ts", "range/file")
MAX_BODY_BYTES = 2 * 1024 * 1024  # 单条报文最多落盘 2MB
LISTEN_BACKLOG = 256
UPSTREAM_TIMEOUT = 60


def log(msg: str) -> None:
    print(f"[fnmedia-tap] {msg}", flush=True)


# ---------------------------------------------------------------- 身份校验

def _peer_pid(s: socket.socket) -> int:
    return struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]


def snap(path: Path, timeout: float = 0.6) -> dict:
    """返回 {kind: official|proxy|stale|absent|unknown, pid, exe, inode}。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"kind": "absent"}
    ino = [st.st_dev, st.st_ino]
    if not stat.S_ISSOCK(st.st_mode):
        return {"kind": "unknown", "reason": "not-a-socket", "inode": ino}
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        pid = _peer_pid(s)
        exe = os.readlink(f"/proc/{pid}/exe")
        s.close()
    except OSError as exc:
        s.close()
        if exc.errno == errno.ECONNREFUSED:
            return {"kind": "stale", "inode": ino}
        return {"kind": "unknown", "reason": str(exc), "inode": ino}
    kind = "official" if Path(exe).name == OFFICIAL_EXE else "unknown"
    return {"kind": kind, "pid": pid, "exe": exe, "inode": ino}


def move_no_replace(src: Path, dst: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    # AT_FDCWD = -100, RENAME_NOREPLACE = 1
    if renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1) != 0:
        raise OSError(ctypes.get_errno(), f"renameat2 NOREPLACE failed {src} -> {dst}")


def take_over() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    t = snap(TARGET)
    if t["kind"] != "official":
        raise RuntimeError(f"target 不是已验证的官方监听者: {t}")
    if UPSTREAM.exists():
        u = snap(UPSTREAM)
        if u["kind"] not in ("absent", "stale"):
            raise RuntimeError(f"upstream 路径已被占用且存活: {u}")
        UPSTREAM.unlink()
    log(f"官方监听者 pid={t['pid']} exe={t['exe']}，原子换名接管 ...")
    move_no_replace(TARGET, UPSTREAM)
    # 换完立刻复核：upstream 必须仍是那个官方进程
    u = snap(UPSTREAM)
    if u.get("pid") != t["pid"]:
        # 尝试换回，避免残局
        try:
            move_no_replace(UPSTREAM, TARGET)
        except Exception:
            pass
        raise RuntimeError(f"换名后身份变化: {u}")
    log("接管完成：官方 socket 已重命名为 upstream。")


def rollback() -> None:
    try:
        if not UPSTREAM.exists():
            return
        u = snap(UPSTREAM)
        if u["kind"] != "official":
            log(f"回滚中止：upstream 不是官方监听者 {u}")
            return
        if TARGET.exists():
            t = snap(TARGET)
            if t["kind"] in ("stale", "absent"):
                TARGET.unlink()
            else:
                log(f"回滚中止：target 被其他存活进程占用 {t}")
                return
        move_no_replace(UPSTREAM, TARGET)
        v = snap(TARGET)
        if v.get("pid") == u.get("pid"):
            log("已回滚：官方 socket 恢复原状。")
        else:
            log(f"回滚后身份异常: {v}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"回滚失败: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- HTTP 透传

def classify(ctype: str) -> str:
    c = (ctype or "").lower()
    if "json" in c:
        return "json"
    if "mpegurl" in c or "x-mpegurl" in c or "vnd.apple" in c:
        return "mpegurl"
    if c.startswith("text/") or "xml" in c or "javascript" in c:
        return "text"
    return "binary"


def interesting_path(path: str) -> bool:
    p = path.split("?", 1)[0].lower()
    if any(h in p for h in SKIP_BODY_PATH_HINTS):
        return False
    return True


def parse_head(raw: bytes) -> tuple[str, str, dict] | None:
    try:
        head = raw.split(b"\r\n\r\n", 1)[0].decode("latin1")
    except Exception:
        return None
    lines = head.split("\r\n")
    if not lines:
        return None
    parts = lines[0].split(" ")
    if len(parts) < 3:
        return None
    method, target, version = parts[0], parts[1], parts[2]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, target, headers


async def read_headers(r: asyncio.StreamReader) -> bytes:
    return await r.readuntil(b"\r\n\r\n")


async def relay_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        seq: int) -> None:
    # 1) 读请求头
    try:
        head = await asyncio.wait_for(read_headers(reader), timeout=30)
    except asyncio.IncompleteReadError:
        return
    except asyncio.TimeoutError:
        return
    parsed = parse_head(head)
    if parsed is None:
        writer.close()
        return
    method, target, headers = parsed
    path = target.split("?", 1)[0]

    # 2) 读请求体（按 content-length；chunked 少见，做兜底）
    body = b""
    clen = headers.get("content-length")
    if clen:
        try:
            n = int(clen)
            if 0 < n <= MAX_BODY_BYTES:
                body = await reader.readexactly(n)
            else:
                # 超大或上传：透传剩余（抓包阶段直接丢弃后续并关闭即可）
                body = b""
        except (asyncio.IncompleteReadError, ValueError):
            body = b""
    # 抓包阶段对无 Content-Length 的 POST 不做特殊处理

    started = time.time()
    rec = {
        "seq": seq, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "method": method, "target": target, "path": path,
        "request_headers": headers,
    }
    if body:
        try:
            rec["request_body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            rec["request_body"] = f"<binary {len(body)} bytes>"

    # 3) 连上游，发送（统一 HTTP/1.1 + Connection: close，逐字节透传）
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_unix_connection(str(UPSTREAM)),
                                        timeout=10)
    except Exception as exc:
        log(f"#{seq} 连接上游失败 {target}: {exc}")
        writer.close()
        _dump(rec, error=f"upstream connect: {exc}")
        return

    req_headers = dict(headers)
    req_headers["connection"] = "close"
    # 还原相对路径为 origin-form，去掉 hop 字段
    for h in ("proxy-connection",):
        req_headers.pop(h, None)
    out = [f"{method} {target} HTTP/1.1".encode()]
    for k, v in req_headers.items():
        out.append(f"{k}: {v}".encode())
    uw.write(b"\r\n".join(out) + b"\r\n\r\n" + body)
    await uw.drain()

    # 4) 读响应头
    try:
        resp_head = await asyncio.wait_for(read_headers(ur), timeout=UPSTREAM_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.ConnectionError) as exc:
        log(f"#{seq} 读响应头失败 {target}: {exc}")
        uw.close()
        writer.close()
        _dump(rec, error=f"resp head: {exc}")
        return

    writer.write(resp_head)
    await writer.drain()

    rparsed = parse_head(resp_head)
    r_status = rparsed[0] if rparsed else "?"
    r_headers = rparsed[2] if rparsed else {}
    ctype = r_headers.get("content-type", "")
    kind = classify(ctype)
    rec["status"] = r_status
    rec["response_content_type"] = ctype

    capture = (interesting_path(target) and kind in ("json", "mpegurl", "text"))
    chunks: list[bytes] = []
    total = 0
    truncated = False

    # 上游是 Connection: close，读到 EOF 即为完整响应体
    while True:
        try:
            chunk = await ur.read(65536)
        except (asyncio.IncompleteReadError, asyncio.ConnectionError):
            break
        if not chunk:
            break
        writer.write(chunk)
        await writer.drain()
        total += len(chunk)
        if capture and not truncated and sum(map(len, chunks)) < MAX_BODY_BYTES:
            chunks.append(chunk)
            if sum(map(len, chunks)) >= MAX_BODY_BYTES:
                truncated = True

    uw.close()
    await writer.drain()
    writer.close()

    rec["elapsed_ms"] = int((time.time() - started) * 1000)
    rec["response_body_bytes"] = total
    if capture:
        payload = b"".join(chunks)[:MAX_BODY_BYTES]
        try:
            text = payload.decode("utf-8")
            rec["response_body_truncated"] = truncated or total > len(payload)
            rec["response_body"] = text
        except UnicodeDecodeError:
            rec["response_body"] = f"<decode-fail {len(payload)} bytes>"
        _dump(rec)
    else:
        rec["note"] = "body not captured (binary/static/stream)"
        _dump(rec, meta_only=True)
    log(f"#{seq} {method} {path} -> {r_status} {ctype[:40]} {total}B {rec['elapsed_ms']}ms")


def _dump(rec: dict, error: str | None = None, meta_only: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y%m%d")
    if error:
        rec["error"] = error
    safe = "".join(c if c.isalnum() else "_" for c in rec.get("path", "x"))[:60]
    name = f"{day}-{rec.get('seq',0):05d}-{safe}"
    if meta_only:
        name += ".meta.json"
    else:
        name += ".json"
    try:
        (LOG_DIR / name).write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    except Exception as exc:
        log(f"落盘失败: {exc}")


async def handle(reader, writer, counter):
    seq = next(counter)
    try:
        await relay_request(reader, writer, seq)
    except Exception as exc:
        log(f"#{seq} 连接异常: {type(exc).__name__}: {exc}")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def amain() -> None:
    take_over()

    loop = asyncio.get_running_loop()

    def stop(signame):
        log(f"收到 {signame}，准备回滚并退出 ...")
        for t in asyncio.all_tasks(loop):
            t.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop, sig)

    # 监听一个私有暂存 socket，chmod 后换名到 target
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    staged = STATE_DIR / "listen.sock"
    if staged.exists():
        staged.unlink()

    counter = iter(range(1, 10 ** 9))
    server = await asyncio.start_unix_server(
        lambda r, w: handle(r, w, counter), path=str(staged),
        backlog=LISTEN_BACKLOG)
    os.chmod(staged, 0o666)
    move_no_replace(staged, TARGET)
    log(f"抓包代理已上线：{TARGET} -> {UPSTREAM}")
    log(f"报文落盘目录：{LOG_DIR}")

    try:
        async with server:
            await server.serve_forever()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        server.close()
        rollback()


def main() -> int:
    try:
        asyncio.run(amain())
        return 0
    except Exception as exc:
        log(f"致命错误: {type(exc).__name__}: {exc}")
        rollback()
        return 1


if __name__ == "__main__":
    sys.exit(main())
