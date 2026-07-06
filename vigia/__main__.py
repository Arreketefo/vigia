"""Long-running scheduler daemon: `python -m vigia`."""

from __future__ import annotations

import asyncio

from radar_core.runtime import run_daemon, setup_logging

from vigia.config import Settings
from vigia.runtime import Runtime
from vigia.store import open_store


async def _run(cfg: Settings) -> None:
    async with open_store(cfg.db_path) as store:
        await store.init_schema()
        runtime = Runtime(cfg, store)
        await run_daemon(
            "vigia", runtime.run_tick, cfg.tick_interval_s, cleanup=runtime.aclose
        )


def main() -> None:
    setup_logging()
    cfg = Settings()  # type: ignore[call-arg]
    asyncio.run(_run(cfg))


if __name__ == "__main__":
    main()
