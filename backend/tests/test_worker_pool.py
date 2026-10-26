"""Three signal workers share one queue and do not wait on each other.

A fourth post stays queued until one of those three finishes. The channel reviewer
is not part of this pool; see test_channel_reviewer.py.
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.config import settings as app_settings
from app.events import EventBus
from app.telegram.ingest import Ingestor


def test_ingest_worker_default_is_three() -> None:
    assert Settings.model_fields["INGEST_WORKERS"].default == 3


async def test_a_free_worker_starts_the_next_post_while_the_first_is_busy() -> None:
    """Three posts overlap. The fourth cannot enter until one of them leaves."""
    previous = app_settings.INGEST_WORKERS
    app_settings.INGEST_WORKERS = 3
    entered: list[int] = []
    release = asyncio.Event()

    class _Runtime:
        def __init__(self) -> None:
            self.settings = app_settings
            self.tasks: dict[str, asyncio.Task[None]] = {}
            self.bus = EventBus()

    runtime = _Runtime()
    ingestor = Ingestor(runtime)  # type: ignore[arg-type]

    async def _occupy(row_id: int) -> None:
        entered.append(int(row_id))
        await release.wait()

    ingestor._process = _occupy  # type: ignore[method-assign]
    try:
        assert ingestor._worker_count() == 3
        await ingestor.start()
        assert len([task for task in ingestor._tasks if not task.done()]) == 3
        await asyncio.sleep(0)
        for row_id in (1, 2, 3, 4):
            await ingestor.enqueue(row_id)
        for _ in range(50):
            if len(entered) >= 3:
                break
            await asyncio.sleep(0.02)
        assert sorted(entered) == [1, 2, 3], entered
        assert 4 not in entered
        release.set()
        for _ in range(50):
            if 4 in entered:
                break
            await asyncio.sleep(0.02)
        assert 4 in entered
    finally:
        release.set()
        await ingestor.stop()
        app_settings.INGEST_WORKERS = previous
