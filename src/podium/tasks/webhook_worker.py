"""In-process webhook delivery loop, started from the app's lifespan."""

import asyncio
import contextlib
import logging

from starlette.concurrency import run_in_threadpool

from podium.db import get_sessionmaker
from podium.services import webhooks

log = logging.getLogger("podium.webhooks")


def tick() -> int:
    with get_sessionmaker()() as db:
        return webhooks.deliver_pending(db)


async def run(stop: asyncio.Event, interval: int) -> None:
    while not stop.is_set():
        try:
            await run_in_threadpool(tick)
        except Exception:  # keep the loop alive; the failure is logged
            log.exception("webhook delivery tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
