"""One-shot tick for manual validation: `python -m vigia.tick`.

Exercises Aviasales + Hotellook once and prints what came back — the fast
loop to confirm the sources return usable data before committing to the daemon.
"""

from __future__ import annotations

import asyncio

from vigia.config import Settings
from vigia.runtime import Runtime, setup_logging
from vigia.store import open_store


async def run_once() -> None:
    cfg = Settings()  # type: ignore[call-arg]
    async with open_store(cfg.db_path) as store:
        await store.init_schema()
        runtime = Runtime(cfg, store)
        try:
            stats = await runtime.run_tick()
        finally:
            await runtime.aclose()
    print(
        f"tick: {stats.pairs_scanned} pairs scanned, "
        f"{stats.observations} observations, "
        f"{stats.deals_fired} deals fired, "
        f"{stats.alerts_sent} alerts sent, "
        f"{stats.errors} errors"
        + (f", discovered: {', '.join(stats.discovered_routes)}"
           if stats.discovered_routes else "")
    )


def main() -> None:
    setup_logging()
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
