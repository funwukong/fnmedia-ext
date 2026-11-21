"""Read-only background task diagnostics contract."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app


def test_background_task_snapshot_reports_only_running_names():
    async def run():
        gate = asyncio.Event()
        registry = set()
        task = app._managed_task(gate.wait(), name="test-worker", registry=registry)
        rows = app._background_task_snapshot(registry)
        assert rows == [{"name": "test-worker", "state": "running",
                         "running_seconds": rows[0]["running_seconds"]}]
        assert rows[0]["running_seconds"] >= 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert app._background_task_snapshot(registry) == []

    asyncio.run(run())
