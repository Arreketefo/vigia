"""Long-running scheduler daemon: `python -m vigia`."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from vigia.config import Settings
from vigia.runtime import Runtime, setup_logging
from vigia.store import open_store

log = logging.getLogger("vigia")


async def run_daemon(cfg: Settings) -> None:
    async with open_store(cfg.db_path) as store:
        await store.init_schema()
        runtime = Runtime(cfg, store)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            runtime.run_tick,
            "interval",
            seconds=cfg.tick_interval_s,
            next_run_time=datetime.now(UTC),  # first tick immediately
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info("vigia daemon started (tick every %ds)", cfg.tick_interval_s)
        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            # Let an in-flight tick finish before closing HTTP clients and the
            # DB: killing it mid-alert would drop the mark_alerted write and
            # re-send the same deal after restart.
            await runtime.wait_idle()
            await runtime.aclose()
            log.info("vigia daemon stopped")


def main() -> None:
    setup_logging()
    cfg = Settings()  # type: ignore[call-arg]
    asyncio.run(run_daemon(cfg))


if __name__ == "__main__":
    main()
