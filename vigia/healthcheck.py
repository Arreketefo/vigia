"""Container healthcheck: exit 0 if the last tick is recent, 1 otherwise.

Reads meta.last_tick_at (UTC, written by the scheduler at the end of each tick)
with plain sqlite3 — no async needed for a one-shot probe.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime

from vigia.config import Settings
from vigia.scheduler import LAST_TICK_KEY
from vigia.store import TIMESTAMP_FMT

_MIN_THRESHOLD_S = 900


def main() -> int:
    cfg = Settings()  # type: ignore[call-arg]
    threshold_s = max(3 * cfg.tick_interval_s, _MIN_THRESHOLD_S)
    try:
        conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (LAST_TICK_KEY,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"unhealthy: cannot read {cfg.db_path}: {exc}", file=sys.stderr)
        return 1
    if row is None:
        print("unhealthy: no tick recorded yet", file=sys.stderr)
        return 1
    last_tick = datetime.strptime(row[0], TIMESTAMP_FMT).replace(tzinfo=UTC)
    age_s = (datetime.now(UTC) - last_tick).total_seconds()
    if age_s > threshold_s:
        print(f"unhealthy: last tick {age_s:.0f}s ago (> {threshold_s}s)", file=sys.stderr)
        return 1
    print(f"healthy: last tick {age_s:.0f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
