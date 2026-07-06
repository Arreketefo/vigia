"""Container healthcheck: exit 0 if the last tick is recent, 1 otherwise."""

from __future__ import annotations

import sys

from radar_core.runtime import healthcheck_main

from vigia.config import Settings
from vigia.scheduler import LAST_TICK_KEY


def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    return healthcheck_main(cfg.db_path, LAST_TICK_KEY, cfg.tick_interval_s)


if __name__ == "__main__":
    sys.exit(main())
