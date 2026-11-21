"""Background task history diagnostics regression tests."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app


def test_background_task_history_records_completion_without_exception_text():
    async def run():
        async def work():
            return None
        task = app._managed_task(work(), name="history-worker")
        await task
        await asyncio.sleep(0)
        rows = app._background_task_history()
        row = next(item for item in reversed(rows)
                   if item["name"] == "history-worker")
        assert row["state"] == "completed"
        assert "error" not in row

    asyncio.run(run())
