#!/usr/bin/env python3
"""飞牛桌面应用网关：/app/fnmedia-ext → fnmedia FastAPI Unix socket。

App Center 桌面入口使用同源 HTTPS 路径 `/app/fnmedia-ext/`，并按
ui/config 中的 gatewaySocket 连接应用根目录下的 fnmedia-ext.sock。
本进程负责：
1. 接受网关注入的 HTTP 请求；
2. 去掉 `/app/fnmedia-ext` 前缀；
3. 原样转发到主代理的 Unix socket（默认 /var/run/trim_media.sock）。

媒体播放仍走 nginx 的 /v/media 主 socket；该网关仅服务桌面管理窗口。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

APP_PREFIX = "/app/fnmedia-ext"
SOCK_NAME = "fnmedia-ext.sock"
MAX_HEADER = 1 << 20
MAX_BODY = 64 << 20  # 管理页提交数据很小；留出足够安全余量
RELAY_TIMEOUT = 60


def gateway_socket_path(base: Path) -> Path:
    """FPK 安装形态：repo 与 ui 同级，socket 必须在应用根目录。"""
    parent = base.resolve().parent
    if (parent / "ui").is_dir() and base.name == "repo":
        return parent / SOCK_NAME
    run = Path("/run/fnmedia-ext")
    run.mkdir(parents=True, exist_ok=True)
    return run / SOCK_NAME


def _recv_until_headers(client: socket.socket, initial: bytes = b"") -> tuple[bytes, bytes] | None:
    data = initial
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEADER:
            return None
        chunk = client.recv(8192)
        if not chunk:
            return None
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    return head, body


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    return int(value.strip())
                except ValueError:
                    return -1
    return 0


def _recv_body(client: socket.socket, partial: bytes, length: int) -> bytes | None:
    if length < 0 or length > MAX_BODY:
        return None
    body = partial
    while len(body) < length:
        chunk = client.recv(min(65536, length - len(body)))
        if not chunk:
            return None
        body += chunk
    return body[:length]


def _rewrite_request(head: bytes) -> bytes:
    lines = head.split(b"\r\n")
    parts = lines[0].split()
    if len(parts) >= 2:
        method, target = parts[0], parts[1].decode("latin-1")
        path = target.split("?", 1)[0]
        if path == APP_PREFIX or path == APP_PREFIX + "/":
            target = "/v/online-admin"
        elif path.startswith(APP_PREFIX + "/"):
            target = target[len(APP_PREFIX):]
            if not target.startswith("/"):
                target = "/" + target
        lines[0] = b" ".join([method, target.encode("latin-1"), parts[2]])

    # 逐头重建，移除 Expect 和网关注入的长度外冗余头；其余原样保留。
    rebuilt = [lines[0]]
    for line in lines[1:]:
        name = line.split(b":", 1)[0].strip().lower()
        if name == b"expect":
            continue
        rebuilt.append(line)
    return b"\r\n".join(rebuilt) + b"\r\n\r\n"


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, upstream_sock: Path) -> None:
    remote: socket.socket | None = None
    try:
        received = _recv_until_headers(client)
        if received is None:
            return
        head, partial_body = received
        length = _content_length(head)
        body = _recv_body(client, partial_body, length)
        if body is None:
            return

        # HTTP/1.1 Expect: 100-continue 由我们在解析 body 后直接确认。
        if b"\r\nexpect:" in b"\r\n" + head.lower():
            client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remote.settimeout(RELAY_TIMEOUT)
        remote.connect(str(upstream_sock))
        remote.sendall(_rewrite_request(head) + body)

        t1 = threading.Thread(target=_pump, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=_pump, args=(remote, client), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
    except OSError:
        pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def serve(gateway_sock: Path, upstream_sock: Path) -> None:
    gateway_sock.parent.mkdir(parents=True, exist_ok=True)
    try:
        gateway_sock.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(gateway_sock))
    os.chmod(gateway_sock, 0o666)
    server.listen(64)
    print(f"[fnmedia-gateway] {gateway_sock} -> {upstream_sock}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client, upstream_sock), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    repo = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    upstream = Path(args[1]) if len(args) > 1 else Path("/var/run/trim_media.sock")
    serve(gateway_socket_path(repo), upstream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
