"""Load curated ALC routes into the DB: `python -m vigia.seed`.

City-level IATA codes (LON, PAR, MIL...) group all airports of the metro area.
Discovery (search_range under budget) keeps adding routes on top of these.
"""

from __future__ import annotations

import asyncio

from vigia.config import Settings
from vigia.store import open_store

CURATED_DESTINATIONS = [
    # UK & Ireland
    "LON", "MAN", "BHX", "BRS", "EDI", "GLA", "DUB",
    # Benelux / France / Germany
    "AMS", "EIN", "BRU", "PAR", "BER", "DUS", "FRA",
    # Italy
    "MIL", "ROM", "VCE", "NAP",
    # Central & Eastern Europe
    "VIE", "PRG", "BUD", "WAW", "KRK",
    # Nordics & Switzerland
    "CPH", "STO", "OSL", "ZRH", "GVA",
]


async def _seed() -> None:
    cfg = Settings()  # type: ignore[call-arg]
    async with open_store(cfg.db_path) as store:
        await store.init_schema()
        for dest in CURATED_DESTINATIONS:
            await store.upsert_route(cfg.origin, dest)
        routes = await store.enabled_routes()
    print(f"seeded; {len(routes)} enabled routes from {cfg.origin}")


def main() -> None:
    asyncio.run(_seed())


if __name__ == "__main__":
    main()
