"""Wiring shared by the daemon (`python -m vigia`) and the one-shot tick
(`python -m vigia.tick`): build sources/notifiers from config and run ticks.

Tick serialization and graceful shutdown live in radar_core.runtime.
"""

from __future__ import annotations

import logging
from datetime import date

from radar_core.botcontrol import (
    BotCommander,
    DomainHandler,
    Override,
    apply_overrides,
    boolean,
    float_range,
    int_range,
    skip_tick_if_paused,
    text,
)
from radar_core.runtime import setup_logging

from vigia.cities import AirlineDirectory, CityDirectory
from vigia.config import Settings, hhmm_or_empty
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

__all__ = ["OVERRIDES", "Runtime", "build_hotel_source", "setup_logging"]

# Claves ajustables por Telegram EN CALIENTE (aplican al siguiente tick).
# Fuera de la lista a propósito: tokens/keys/db_path (seguridad), cadencias y
# batch (protección de rate limits) y max_flight_hours / trip_*_nights (van
# horneados en la fuente: requieren reinicio). El suelo de calidad del hotel
# SÍ es hot: run_tick lo empuja a la fuente antes de cada tick.
OVERRIDES: dict[str, Override] = {
    "budget_cap": Override("budget_cap", float_range(1, 10000),
                           "presupuesto de detección, vuelos 2 pax (EUR)"),
    "trip_budget_cap": Override("trip_budget_cap", float_range(1, 20000),
                                "tope del viaje completo (EUR)"),
    "min_drop_pct": Override("min_drop_pct", float_range(0.01, 0.9),
                             "caída mínima vs típico (0.2 = 20%)"),
    "z_threshold": Override("z_threshold", float_range(0.5, 10), "sensibilidad z"),
    "hard_steal_ratio": Override("hard_steal_ratio", float_range(0.1, 1.0),
                                 "umbral del chollo absoluto"),
    "realert_drop": Override("realert_drop", float_range(0.01, 0.9),
                             "mejora mínima para re-avisar"),
    "exclude_countries": Override("exclude_countries", text, "países ISO, coma (ES,PT)"),
    "weekend_only_after": Override("weekend_only_after", _date_or_empty := (
        lambda raw: "" if not raw.strip() else str(date.fromisoformat(raw.strip()))
    ), "YYYY-MM-DD o vacío para desactivar"),
    "pre_weekend_nights_min": Override("pre_weekend_nights_min", int_range(1, 14), "noches"),
    "pre_weekend_nights_max": Override("pre_weekend_nights_max", int_range(1, 14), "noches"),
    "discovery": Override("discovery", boolean, "descubrir rutas nuevas (on/off)"),
    "depart_after": Override("depart_after", hhmm_or_empty,
                             "ida no antes de HH:MM ('off' = sin filtro)"),
    "return_before": Override("return_before", hhmm_or_empty,
                              "vuelta no después de HH:MM, misma jornada "
                              "('off' = sin filtro)"),
    "hotel_min_rating": Override("hotel_min_rating", float_range(0, 10),
                                 "nota mínima del hotel 0-10 (0 = sin filtro)"),
    "hotel_min_reviews": Override("hotel_min_reviews", int_range(0, 100000),
                                  "reseñas mínimas del hotel (0 = sin filtro)"),
}


def build_hotel_source(
    cfg: Settings, cities: CityDirectory | None = None
) -> HotelSource | None:
    floor_active = cfg.hotel_min_rating > 0 or cfg.hotel_min_reviews > 0
    if cfg.hotel_source == "liteapi":
        if not cfg.liteapi_key:
            raise ValueError("hotel_source=liteapi requires VIGIA_LITEAPI_KEY")
        # El log del suelo EFECTIVO lo emite set_quality_floor (cubre también
        # overrides persistidos que pisan al .env desde el primer tick).
        return LiteApiHotelSource(
            cfg.liteapi_key, currency=cfg.currency, adults=cfg.pax,
            min_rating=cfg.hotel_min_rating, min_reviews=cfg.hotel_min_reviews,
            city_names=cities,
        )
    if cfg.hotel_source == "hotellook":
        log.warning(
            "hotellook was shut down by Travelpayouts on 2025-10-20; "
            "expect failures unless it has been revived"
        )
        if floor_active:
            log.warning(
                "hotel_min_rating/hotel_min_reviews solo aplican a liteapi; "
                "hotellook los IGNORA — el hotel más barato vuelve a ser sin filtro"
            )
        return HotellookHotelSource(cfg.travelpayouts_token, cfg.currency)
    if cfg.hotel_source != "none":
        raise ValueError(f"unknown hotel_source: {cfg.hotel_source!r}")
    return None


def _split_sweep_and_enricher(
    cfg: Settings, cities: CityDirectory | None = None
) -> tuple[HotelSource | None, HotelSource | None]:
    """Returns (sweep hotel source, candidate enricher) per hotel_mode."""
    source = build_hotel_source(cfg, cities)
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
        # cities se crea ANTES que las fuentes: LiteAPI lo usa para que el
        # deep link del hotel diga "Budapest" y no "BUD".
        self.cities = CityDirectory()
        self.hotels, self.enricher = _split_sweep_and_enricher(cfg, self.cities)
        self._floor_warned = False  # aviso único de suelo-sin-fuente-que-lo-aplique
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
        self.airlines = AirlineDirectory()
        self.notifiers = build_notifiers(cfg, cities=self.cities, airlines=self.airlines)
        self.commander: BotCommander | None = None
        if cfg.telegram_bot_token and cfg.telegram_chat_id:
            self.commander = BotCommander(
                cfg.telegram_bot_token,
                {str(cfg.telegram_chat_id)},
                store,
                "vigia",
                OVERRIDES,
                domain_commands=self._domain_commands(),
                status_provider=self._status,
                digest_provider=self._status,  # el status 24h ES el resumen diario
                digest_hour=cfg.digest_hour,
            )

    async def run_tick(self) -> TickStats:
        # Config efectiva del tick = .env ⊕ overrides del bot (en caliente).
        cfg = apply_overrides(self.cfg, await self.store.get_overrides(), OVERRIDES)
        from vigia.scheduler import LAST_TICK_KEY

        if await skip_tick_if_paused(self.store, LAST_TICK_KEY):
            return TickStats()
        # El suelo de calidad del hotel es hot: empujar el efectivo a las
        # fuentes que lo soporten (duck-typed, como aclose). La fuente
        # invalida su caché de quotes solo si el valor cambió.
        pushed = False
        for source in (self.hotels, self.enricher):
            set_floor = getattr(source, "set_quality_floor", None)
            if set_floor is not None:
                set_floor(cfg.hotel_min_rating, cfg.hotel_min_reviews)
                pushed = True
        if (not pushed and not self._floor_warned
                and (cfg.hotel_min_rating > 0 or cfg.hotel_min_reviews > 0)):
            # /set aceptó el valor pero ninguna fuente lo aplica (hotellook
            # o sin fuente de hotel): decirlo UNA vez, no cada tick.
            self._floor_warned = True
            log.warning(
                "hotel_min_rating/hotel_min_reviews configurados pero la "
                "fuente de hotel actual (%s) no los soporta — sin efecto",
                self.cfg.hotel_source,
            )
        return await tick(
            flights=self.flights,
            hotels=self.hotels,
            store=self.store,
            cfg=cfg,
            notifiers=self.notifiers,
            confirmer=self.confirmer,
            enricher=self.enricher,
            cities=self.cities,
            trip_policy=build_trip_policy(cfg),
        )

    def _domain_commands(self) -> dict[str, DomainHandler]:
        def _eur(raw: object) -> str:
            # "200.0" / 200.0 -> "200 €"; conserva decimales reales (350.5 €).
            try:
                return f"{float(str(raw)):g} €"
            except ValueError:
                return str(raw)

        async def _set_budget(
            cmd: str, key: str, emoji: str, titulo: str, nota: str, args: str
        ) -> str:
            # Sin valor: mostrar el efectivo (override o .env) en vez de
            # parsear "" — float("") reventaba con "could not convert
            # string to float: ''".
            spec = OVERRIDES[key]
            overrides = await self.store.get_overrides()
            if not args.strip():
                if key in overrides:
                    origen = f"{_eur(overrides[key])}  ·  fijado por ti"
                else:
                    origen = f"{_eur(getattr(self.cfg, key))}  ·  por defecto"
                return (f"{emoji} {titulo}\n"
                        f"Actual: {origen}\n"
                        f"{nota}\n"
                        f"✏️ Cambiar:  /{cmd} <€>")
            value = spec.parse(args)
            previo = _eur(overrides[key]) if key in overrides else _eur(getattr(self.cfg, key))
            await self.store.set_override(key, args.strip())
            return (f"✅ {titulo}\n"
                    f"{_eur(value)}  (antes {previo})\n"
                    f"Se aplica en el próximo chequeo.")

        async def presupuesto(args: str) -> str:
            return await _set_budget(
                "presupuesto", "budget_cap", "💶", "Presupuesto de vuelos (2 pax)",
                "Detección de chollos solo-vuelo.", args)

        async def presupuestoviaje(args: str) -> str:
            return await _set_budget(
                "presupuestoviaje", "trip_budget_cap", "🧳",
                "Presupuesto del viaje completo",
                "Vuelos + hotel; tope final del viaje.", args)

        async def paises(args: str) -> str:
            if not args.strip():
                await self.store.delete_override("exclude_countries")
                return "🌍 Países excluidos → por defecto (.env)"
            codigos = args.strip().upper()
            await self.store.set_override("exclude_countries", codigos)
            return f"🚫 Países excluidos:  {codigos}"

        async def rutas(args: str) -> str:
            routes = await self.store.enabled_routes()
            if not routes:
                return "🗺️ No hay rutas activas."
            listado = ", ".join(r.destination for r in routes)
            return f"🗺️ {len(routes)} rutas activas:\n{listado}"

        async def quitaruta(args: str) -> str:
            dest = args.strip().upper()
            if not dest:
                return "✏️ Uso:  /quitaruta BUD"
            ok = await self.store.set_route_enabled(dest, False)
            return f"🚫 {dest} desactivada" if ok else f"⚠️ {dest} no existe (mira /rutas)"

        async def ponruta(args: str) -> str:
            dest = args.strip().upper()
            if not dest:
                return "✏️ Uso:  /ponruta BUD"
            ok = await self.store.set_route_enabled(dest, True)
            return f"✅ {dest} reactivada" if ok else f"⚠️ {dest} no existe (mira /rutas)"

        return {
            "presupuesto": presupuesto,
            "presupuestoviaje": presupuestoviaje,
            "paises": paises,
            "rutas": rutas,
            "quitaruta": quitaruta,
            "ponruta": ponruta,
        }

    async def _status(self) -> str:
        obs, alerts = await self.store.stats_24h()
        routes = len(await self.store.enabled_routes())
        return f"24h: {obs} observaciones, {alerts} alertas · {routes} rutas vigiladas"

    async def aclose(self) -> None:
        # Duck-typed close for every component: providers behind the Protocol
        # interfaces own httpx clients the core must not know concretely.
        for component in (self.flights, self.hotels, self.enricher, self.confirmer,
                          self.cities, self.airlines, self.commander, *self.notifiers):
            aclose = getattr(component, "aclose", None)
            if aclose is not None:
                await aclose()
