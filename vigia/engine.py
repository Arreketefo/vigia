"""DealEngine: anomaly + budget rule and dedup key (domain logic).

The robust statistics live in radar_core.stats; `robust_baseline` is
re-exported here so internal consumers keep one import point.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Protocol

from radar_core.stats import robust_baseline

__all__ = ["DealConfig", "dedup_key", "is_deal", "robust_baseline"]

_BUCKET = 25.0


class DealConfig(Protocol):
    budget_cap: float
    hard_steal_ratio: float
    min_sample: int
    min_drop_pct: float
    z_threshold: float


def is_deal(
    total: float,
    median: float | None,
    mad: float | None,
    sample: int,
    cfg: DealConfig,
) -> tuple[bool, float | None]:
    drop_pct = None if median is None else (median - total) / median
    hard_steal = total <= cfg.budget_cap * cfg.hard_steal_ratio
    if sample < cfg.min_sample or median is None or mad is None:
        # Not enough history for anomaly detection: only the obvious steal fires.
        return hard_steal, drop_pct
    anomaly = total <= median * (1 - cfg.min_drop_pct)
    if mad > 0:
        # With a degenerate window (all totals identical, MAD = 0) the z-score
        # would explode for any total 1 cent below the median; rely on the
        # relative-drop test alone in that case.
        robust_z = 0.6745 * (total - median) / mad
        anomaly = anomaly or robust_z <= -cfg.z_threshold
    fire = (anomaly and total <= cfg.budget_cap) or hard_steal
    return fire, drop_pct


def dedup_key(
    origin: str, dest: str, depart: date, ret: date | None, total: float
) -> str:
    bucketed = round(total / _BUCKET) * _BUCKET
    raw = f"{origin}:{dest}:{depart}:{ret}:{bucketed}"
    return hashlib.sha1(raw.encode()).hexdigest()
