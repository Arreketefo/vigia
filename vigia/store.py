"""PriceStore: domain persistence (observations, baselines, deals, alert
dedup) on top of radar_core's SQLite base layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from radar_core.store import TIMESTAMP_FMT, BaseStore, iso_date, open_db, utcnow_str

from vigia import engine
from vigia.contracts import Deal

__all__ = ["TIMESTAMP_FMT", "PriceStore", "Route", "open_store", "utcnow_str"]

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "schema.sql"

# Trailing window for the robust baseline: 60 observations or 30 days,
# whichever limit is hit first.
_BASELINE_MAX_OBS = 60
_BASELINE_MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class Route:
    id: int
    origin: str
    destination: str


def _month_range(month_bucket: str) -> tuple[str, str]:
    """'YYYY-MM' -> [first day, first day of next month) as sargable bounds."""
    first = datetime.strptime(month_bucket, "%Y-%m").date()
    next_first = (first + timedelta(days=32)).replace(day=1)
    return first.isoformat(), next_first.isoformat()


@asynccontextmanager
async def open_store(db_path: str) -> AsyncIterator[PriceStore]:
    async with open_db(db_path) as conn:
        yield PriceStore(conn)


class PriceStore(BaseStore):
    async def init_schema(self) -> None:
        await self._conn.executescript(_SCHEMA_PATH.read_text())
        await self._conn.commit()

    # -- routes ------------------------------------------------------------

    async def upsert_route(self, origin: str, destination: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO routes (origin, destination) VALUES (?, ?)",
            (origin.upper(), destination.upper()),
        )
        await self._conn.commit()

    async def enabled_routes(self) -> list[Route]:
        cur = await self._conn.execute(
            "SELECT id, origin, destination FROM routes WHERE enabled = 1 ORDER BY id"
        )
        rows = await cur.fetchall()
        return [Route(r["id"], r["origin"], r["destination"]) for r in rows]

    async def stalest_route_month_pairs(
        self,
        month_buckets: list[str],
        limit: int,
        exclude_destinations: set[str] | None = None,
    ) -> list[tuple[Route, str]]:
        """All (enabled route, month bucket) pairs, least recently scanned first.

        Staleness comes from scan_state (attempts), NOT from observations: a
        pair that keeps returning no data must still rotate to the back of the
        queue, or a handful of empty far-future pairs would monopolize every
        batch and starve the data-bearing ones. exclude_destinations filters
        BEFORE the limit so skipped routes never consume batch slots.
        """
        routes = await self.enabled_routes()
        if exclude_destinations:
            routes = [r for r in routes if r.destination not in exclude_destinations]
        cur = await self._conn.execute("SELECT route_id, month_bucket, scanned_at FROM scan_state")
        last_scan = {
            (r["route_id"], r["month_bucket"]): r["scanned_at"] for r in await cur.fetchall()
        }
        pairs = [(route, bucket) for route in routes for bucket in month_buckets]
        # Never-scanned pairs ('' sorts first) get priority.
        pairs.sort(key=lambda p: last_scan.get((p[0].id, p[1])) or "")
        return pairs[:limit]

    async def mark_scanned(self, route_id: int, month_bucket: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO scan_state (route_id, month_bucket, scanned_at)
            VALUES (?, ?, ?)
            ON CONFLICT (route_id, month_bucket) DO UPDATE SET
                scanned_at = excluded.scanned_at
            """,
            (route_id, month_bucket, utcnow_str()),
        )
        await self._conn.commit()

    # -- observations & baseline --------------------------------------------

    async def record_observation(
        self,
        route_id: int,
        depart_date: date,
        return_date: date | None,
        nights: int | None,
        flight_price: float | None,
        hotel_price_night: float | None,
        source: str,
        deep_link: str | None,
        is_live: bool = False,
        commit: bool = True,
    ) -> None:
        """Insert one observation. Pass commit=False to batch several inserts
        into one transaction and call commit() once at the end."""
        await self._conn.execute(
            """
            INSERT INTO price_observations
                (route_id, depart_date, return_date, nights, flight_price,
                 hotel_price_night, source, is_live, deep_link, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route_id,
                depart_date.isoformat(),
                iso_date(return_date),
                nights,
                flight_price,
                hotel_price_night,
                source,
                1 if is_live else 0,
                deep_link,
                utcnow_str(),
            ),
        )
        if commit:
            await self._conn.commit()

    async def prune_observations(self, max_age_days: int) -> int:
        """Drop observations past any baseline window; keeps the table bounded."""
        return await self.prune_older_than("price_observations", "captured_at", max_age_days)

    async def baseline(
        self, route_id: int, month_bucket: str, pax: int, with_hotel: bool
    ) -> tuple[float | None, float | None, int]:
        """Recompute and persist the robust baseline; return (median, mad, sample).

        with_hotel selects ONE observation mode: flight+hotel totals and
        flight-only totals are not comparable, so toggling hotel_source must
        not blend the two into a single median (it would mis-fire or silence
        deals for the whole trailing window).
        """
        month_start, month_end = _month_range(month_bucket)
        hotel_filter = "IS NOT NULL AND nights IS NOT NULL" if with_hotel else "IS NULL"
        total_expr = (
            "flight_price * ? + hotel_price_night * nights" if with_hotel else "flight_price * ?"
        )
        cur = await self._conn.execute(
            f"""
            SELECT {total_expr} AS total
            FROM price_observations
            WHERE route_id = ?
              AND depart_date >= ? AND depart_date < ?
              AND flight_price IS NOT NULL
              AND hotel_price_night {hotel_filter}
              AND captured_at >= datetime('now', '-{_BASELINE_MAX_AGE_DAYS} days')
            ORDER BY captured_at DESC
            LIMIT {_BASELINE_MAX_OBS}
            """,
            (pax, route_id, month_start, month_end),
        )
        totals = [r["total"] for r in await cur.fetchall()]
        if not totals:
            return None, None, 0
        median, mad = engine.robust_baseline(totals)
        await self._conn.execute(
            """
            INSERT INTO baselines (route_id, month_bucket, median_total, mad_total,
                                   sample_size, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (route_id, month_bucket) DO UPDATE SET
                median_total = excluded.median_total,
                mad_total = excluded.mad_total,
                sample_size = excluded.sample_size,
                updated_at = excluded.updated_at
            """,
            (route_id, month_bucket, median, mad, len(totals), utcnow_str()),
        )
        await self._conn.commit()
        return median, mad, len(totals)

    # -- deals & alert dedup -------------------------------------------------

    async def record_deal(self, route_id: int, deal: Deal) -> None:
        await self._conn.execute(
            """
            INSERT INTO deals (route_id, depart_date, return_date, total_price,
                               baseline, drop_pct, confirmed, dedup_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route_id,
                deal.depart_date.isoformat(),
                iso_date(deal.return_date),
                deal.total_price,
                deal.baseline,
                deal.drop_pct,
                1 if deal.confirmed else 0,
                deal.dedup_key,
                utcnow_str(),
            ),
        )
        await self._conn.commit()

    async def should_alert(
        self,
        route_id: int,
        depart_date: date,
        return_date: date | None,
        total: float,
        realert_drop: float,
    ) -> bool:
        """True if this trip was never alerted, or improved >= realert_drop since.

        Matching is by route + exact dates (not by dedup_key): the price bucket
        inside dedup_key changes every 25 EUR, and a fresh key must not bypass
        the "re-alert only on >=10% improvement" rule.
        """
        cur = await self._conn.execute(
            """
            SELECT a.total_price AS total
            FROM alerts_sent a
            JOIN deals d ON d.dedup_key = a.dedup_key
            WHERE d.route_id = ?
              AND d.depart_date = ?
              AND COALESCE(d.return_date, '') = COALESCE(?, '')
            ORDER BY a.sent_at DESC
            LIMIT 1
            """,
            (
                route_id,
                depart_date.isoformat(),
                iso_date(return_date),
            ),
        )
        row = await cur.fetchone()
        if row is None:
            return True
        return total <= float(row["total"]) * (1 - realert_drop)

    async def mark_alerted(self, dedup_key: str, total: float, channels: list[str]) -> None:
        for channel in channels:
            await self._conn.execute(
                """
                INSERT INTO alerts_sent (dedup_key, channel, total_price, sent_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (dedup_key, channel) DO UPDATE SET
                    total_price = excluded.total_price,
                    sent_at = excluded.sent_at
                """,
                (dedup_key, channel, total, utcnow_str()),
            )
        await self._conn.commit()

    # -- meta -----------------------------------------------------------------

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    async def get_meta(self, key: str) -> str | None:
        cur = await self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return None if row is None else str(row["value"])
