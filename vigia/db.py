"""DB administration: `python -m vigia.db init` applies schema/schema.sql."""

from __future__ import annotations

import asyncio
import sys

from vigia.config import Settings
from vigia.store import open_store


async def _init() -> None:
    cfg = Settings()  # type: ignore[call-arg]
    async with open_store(cfg.db_path) as store:
        await store.init_schema()
    print(f"schema applied to {cfg.db_path}")


def main(argv: list[str]) -> int:
    if argv != ["init"]:
        print("usage: python -m vigia.db init", file=sys.stderr)
        return 2
    asyncio.run(_init())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
