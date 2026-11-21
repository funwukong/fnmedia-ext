import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import panpool


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_run_returns_result_and_counts():
    panpool.shutdown()
    assert _run(panpool.run(lambda a, b=0: a + b, 2, b=3)) == 5
    st = panpool.stats()
    assert st["submitted"] >= 1 and st["completed"] >= 1
    assert st["max_workers"] == panpool.MAX_WORKERS
    assert st["pending"] == 0


def test_timeout_is_counted_and_propagated():
    panpool.shutdown()

    async def main():
        with_slow = panpool.run(time.sleep, 0.3, timeout=0.05)
        try:
            await with_slow
        except asyncio.TimeoutError:
            return True
        return False

    assert _run(main()) is True
    st = panpool.stats()
    assert st["timeouts"] >= 1
    assert st["pending"] >= 1
    assert st["running"] >= 1
    time.sleep(0.35)
    assert panpool.stats()["pending"] == 0


def test_queue_limit_rejects_overload():
    panpool.shutdown()

    async def main():
        gate = threading.Event()
        tasks = [asyncio.create_task(panpool.run(gate.wait))
                 for _ in range(panpool.MAX_WORKERS + panpool.MAX_QUEUE)]
        await asyncio.sleep(0.05)
        try:
            await panpool.run(lambda: None)
        except panpool.PoolBusyError:
            rejected = True
        else:
            rejected = False
        gate.set()
        await asyncio.gather(*tasks)
        return rejected

    assert _run(main()) is True
    st = panpool.stats()
    assert st["rejected"] >= 1
    assert st["pending"] == 0
    assert st["running"] == 0
    assert st["queued"] == 0


def test_failure_is_counted():
    panpool.shutdown()

    def boom():
        raise ValueError("x")

    async def main():
        try:
            await panpool.run(boom)
        except ValueError:
            return True
        return False

    assert _run(main()) is True
    assert panpool.stats()["failed"] >= 1
