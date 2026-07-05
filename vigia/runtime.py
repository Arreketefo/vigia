"""Wiring shared by the daemon (`python -m vigia`) and the one-shot tick
(`python -m vigia.tick`): build sources/notifiers from config and run ticks."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from vigia.cities import CityDirectory
from vigia.config import Settings
from vigia.contracts import HotelSource, PriceConfirmer
from vigia.notifiers import build_notifiers
from vigia.scheduler import TickStats, tick
from vigia.sources.aviasales import AviasalesFlightSource
from vigia.sources.duffel import DuffelPriceConfirmer
from vigia.sources.hotellook import HotellookHotelSource
from vigia.sources.liteapi import LiteApiHotelSource
from vigia.store import PriceStore
from vigia.tripwindows import TripWindowPolicy

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request URL at INFO — noisy for a daemon that makes
    # hundreds of calls per tick, and URLs must never carry secrets anyway.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_hotel_source(cfg: Settings) -> HotelSource | None:
    if cfg.hotel_source == "liteapi":
        if not cfg.liteapi_key:
            raise ValueError("hotel_source=liteapi requires VIGIA_LITEAPI_KEY")
        return LiteApiHotelSource(cfg.liteapi_key, currency=cfg.currency, adults=cfg.pax)
    if cfg.hotel_source == "hotellook":
        log.warning(
            "hotellook was shut down by Travelpayouts on 2025-10-20; "
            "expect failures unless it has been revived"
        )
        return HotellookHotelSource(cfg.travelpayouts_token, cfg.currency)
    if cfg.hotel_source != "none":
        raise ValueError(f"unknown hotel_source: {cfg.hotel_source!r}")
    return None


def _split_sweep_and_enricher(
    cfg: Settings,
) -> tuple[HotelSource | None, HotelSource | None]:
    """Returns (sweep hotel source, candidate enricher) per hotel_mode."""
    source = build_hotel_source(cfg)
    if source is None:
        return None, None
    if cfg.hotel_mode == "candidates":
        return None, source
    if cfg.hotel_mode == "sweep":
        if cfg.hotel_source == "liteapi":
            log.warning(
                "hotel_mode=sweep puts LiteAPI in every Layer-1 scan (up to "
                "~%d POSTs/day at current settings) with no published "
                "look-to-book allowance — 'candidates' mode is the safe default",
                cfg.batch_size * cfg.max_quotes_per_pair * (86400 // cfg.tick_interval_s),
            )
        return source, None
    raise ValueError(f"unknown hotel_mode: {cfg.hotel_mode!r}")


def build_confirmer(cfg: Settings) -> PriceConfirmer | None:
    if not cfg.enable_price_confirmer:
        return None
    if not cfg.duffel_token:
        raise ValueError("enable_price_confirmer=true requires VIGIA_DUFFEL_TOKEN")
    if cfg.duffel_token.startswith("duffel_test"):
        log.warning(
            "Duffel TEST token: offers are fake inventory — confirmations are "
            "meaningless; use a duffel_live_ token in production"
        )
    log.info("Layer 2 active: candidates re-priced live via Duffel before alerting")
    return DuffelPriceConfirmer(cfg.duffel_token, currency=cfg.currency, pax=cfg.pax)


def build_trip_policy(cfg: Settings) -> TripWindowPolicy | None:
    if not cfg.weekend_only_after:
        return None
    policy = TripWindowPolicy(
        weekend_only_after=date.fromisoformat(cfg.weekend_only_after),
        pre_min_nights=cfg.pre_weekend_nights_min,
        pre_max_nights=cfg.pre_weekend_nights_max,
        region=cfg.holidays_region,
        extra=cfg.extra_holiday_dates(),
    )
    log.info(
        "trip windows active: until %s any weekday %d-%d nights; from then on "
        "weekends/puentes only (ES+%s holidays%s)",
        cfg.weekend_only_after, cfg.pre_weekend_nights_min, cfg.pre_weekend_nights_max,
        cfg.holidays_region,
        f" +{len(cfg.extra_holiday_dates())} extra" if cfg.extra_holidays else "",
    )
    return policy


class Runtime:
    def __init__(self, cfg: Settings, store: PriceStore) -> None:
        self.cfg = cfg
        self.store = store
        self.flights = AviasalesFlightSource(
            cfg.travelpayouts_token,
            currency=cfg.currency,
            market=cfg.market,
            trip_min_nights=cfg.trip_min_nights,
            trip_max_nights=cfg.trip_max_nights,
            max_flight_hours=cfg.max_flight_hours,
        )
        self.hotels, self.enricher = _split_sweep_and_enricher(cfg)
        self.confirmer = build_confirmer(cfg)
        if self.hotels is None:
            # Detection runs on flight-only totals (with or without enricher):
            # budget_cap must be calibrated as a FLIGHT budget.
            log.info(
                "detection is flight-only%s: budget_cap=%.0f means hard_steal "
                "fires under %.0f EUR of flights — calibrate VIGIA_BUDGET_CAP "
                "to a flight-only budget or expect alert noise",
                " (hotel priced per candidate)" if self.enricher else "",
                cfg.budget_cap, cfg.budget_cap * cfg.hard_steal_ratio,
            )
        if self.confirmer is not None and self.hotels is not None:
            # Sweep totals embed the hotel with no per-component split in the
            # Deal, so a flight re-price would silently drop the hotel part.
            raise ValueError(
                "enable_price_confirmer requires hotel_mode=candidates or "
                "hotel_source=none; sweep totals cannot be re-priced coherently"
            )
        if self.enricher is not None and cfg.budget_cap >= cfg.trip_budget_cap:
            log.warning(
                "budget_cap (%.0f, flight detection) >= trip_budget_cap (%.0f, "
                "full trip): flights alone can consume the whole trip budget, "
                "so most enriched candidates will be killed — lower "
                "VIGIA_BUDGET_CAP to a flight-only budget (e.g. %.0f)",
                cfg.budget_cap, cfg.trip_budget_cap, cfg.trip_budget_cap / 3,
            )
        self.cities = CityDirectory()
        self.trip_policy = build_trip_policy(cfg)
        self.notifiers = build_notifiers(cfg, cities=self.cities)
        # Serializes ticks and lets shutdown wait for the in-flight one.
        self._tick_lock = asyncio.Lock()

    async def run_tick(self) -> TickStats:
        async with self._tick_lock:
            return await tick(
                flights=self.flights,
                hotels=self.hotels,
                store=self.store,
                cfg=self.cfg,
                notifiers=self.notifiers,
                confirmer=self.confirmer,
                enricher=self.enricher,
                cities=self.cities,
                trip_policy=self.trip_policy,
            )

    async def wait_idle(self) -> None:
        """Blocks until no tick is running (used before closing resources)."""
        async with self._tick_lock:
            pass

    async def aclose(self) -> None:
        # Duck-typed close for every component: providers behind the Protocol
        # interfaces own httpx clients the core must not know concretely.
        for component in (self.flights, self.hotels, self.enricher, self.confirmer,
                          self.cities, *self.notifiers):
            aclose = getattr(component, "aclose", None)
            if aclose is not None:
                await aclose()
