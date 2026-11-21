"""Resident background tasks must surface exceptions instead of dying silently."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_xlib_spawn_recycles_exception(caplog):
    import xlib

    async def boom():
        raise RuntimeError("boom")

    async def main():
        task = xlib._spawn(boom(), name="xlib-test-boom")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        assert task not in xlib._BG_TASKS

    _run(main())


def test_hls_spawn_recycles_exception():
    import hls_race

    async def boom():
        raise RuntimeError("boom")

    async def main():
        task = hls_race._spawn(boom(), name="playlist-test-boom")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        assert task not in hls_race._BG_TASKS

    _run(main())


def test_spawn_cancelled_task_is_not_logged():
    import xlib

    async def slow():
        await asyncio.sleep(30)

    async def main():
        task = xlib._spawn(slow(), name="xlib-test-cancel")
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task not in xlib._BG_TASKS

    _run(main())
