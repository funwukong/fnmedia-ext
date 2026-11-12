#!/usr/bin/env python3
"""Sidecar 进程 IO 助手（guardfix-e，catvod.py / drpy.py 共用）。

解决旧 Sidecar.call 的两个缺陷：
  1. `timeout` 参数是摆设——`stdout.readline()` 无超时，卡死的爬虫永久
     wedge 调用线程（锁被占住，reaper 也救不了）；
  2. stderr 是 DEVNULL，崩溃原因直接丢弃；stop() 只有 terminate()，
     顽固进程收不回来。

用法：
    line = sidecar_io.readline_timeout(proc, timeout, name)
    sidecar_io.stop_proc(proc)          # terminate → wait(5s) → kill
    tail = sidecar_io.StderrTail(proc.stderr); tail.start()
"""
from __future__ import annotations

import collections
import queue
import threading

from runner_errors import RunnerTimeoutError


def readline_timeout(proc, timeout: float, name: str) -> str:
    """在读线程里读一行，`queue.get(timeout=...)` 实现真超时。

    超时后读线程仍阻塞在 readline 上——调用方随后 stop_proc() 杀掉进程，
    stdout 关闭，读线程即退出，不泄漏。
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _read():
        try:
            q.put(("line", proc.stdout.readline()))
        except Exception as exc:  # noqa: BLE001
            q.put(("err", exc))

    t = threading.Thread(target=_read, name=f"{name}-read", daemon=True)
    t.start()
    try:
        kind, val = q.get(timeout=timeout)
    except queue.Empty:
        raise RunnerTimeoutError(
            f"{name} sidecar 超时（{timeout:g}s 未返回）") from None
    if kind == "err":
        raise val
    return val


def stop_proc(proc, wait_s: float = 5.0) -> None:
    """关 stdin → terminate → wait(5s) → 还活着就 kill → 再 wait。"""
    if proc is None:
        return
    try:
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=wait_s)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass


class StderrTail:
    """stderr PIPE 的排空线程：只保留最后 N 行，避免子进程写满管道阻塞。"""

    def __init__(self, stream, limit: int = 20):
        self.lines: collections.deque = collections.deque(maxlen=limit)
        self._stream = stream

    def start(self) -> "StderrTail":
        t = threading.Thread(target=self._drain, name="sidecar-stderr",
                             daemon=True)
        t.start()
        return self

    def _drain(self):
        try:
            for line in self._stream:
                self.lines.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            pass

    def text(self) -> str:
        return " | ".join(self.lines)
