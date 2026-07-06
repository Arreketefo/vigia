"""LiteAPI (Nuitee) hotel source — live cheapest rates per city + dates.

Verified against the live API (2026-07-05): POST /v3.0/hotels/rates with
X-API-Key auth; `iataCode` resolves airport codes (BUD, PRG) but NOT metro
city codes (LON returns 0 hotels), hence the metro->city fallback map.
`offerRetailRate.amount` is the price for the WHOLE stay. Core endpoints are
free; intended usage here is candidate enrichment (a handful of calls/day),
not the Layer-1 sweep — LiteAPI publishes no look-to-book policy.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, NamedTuple
from urllib.parse import urlencode

import httpx
from radar_core.http import CircuitBreaker, post_json
from radar_core.ratelimit import TokenBucket

from vigia.cities import CityDirectory
from vigia.contracts import HotelQuote

log = logging.getLogger(__name__)

_URL = "https://api.liteapi.travel/v3.0/hotels/rates"
_HOTEL_DATA_URL = "https://api.liteapi.travel/v3.0/data/hotel"
_NAME_LOOKUP_TIMEOUT_S = 8.0

# Hotel prices move slowly; caching (hits AND misses) bounds API spend when
# the same trip keeps re-firing — e.g. a candidate repeatedly killed by
# trip_budget_cap, or a notifier outage — to one POST per trip per TTL.
_CACHE_TTL_S = 6 * 3600.0
_CACHE_MAX_ENTRIES = 1024

class _Offer(NamedTuple):
    """Ganador del barrido de tarifas de un viaje."""

    stay_total: float
    hotel_id: str | None
    hotel_name: str | None      # nombre inline del payload, si vino


class _CachedQuote(NamedTuple):
    expires_at: float           # time.monotonic()
    offer: _Offer | None        # None = miss cacheado (sin tarifas que cualifiquen)


# IATA metro-area codes that LiteAPI's iataCode param cannot resolve
# (it only knows airports). Extend as discovery surfaces more metro codes.
_METRO_CITIES: dict[str, tuple[str, str]] = {
    "LON": ("London", "GB"),
    "PAR": ("Paris", "FR"),
    "MIL": ("Milan", "IT"),
    "ROM": ("Rome", "IT"),
    "STO": ("Stockholm", "SE"),
    "MOW": ("Moscow", "RU"),
}


class LiteApiHotelSource:
    name = "liteapi"

    def __init__(
        self,
        api_key: str,
        currency: str = "eur",
        adults: int = 2,
        guest_nationality: str = "ES",
        min_rating: float = 0.0,
        min_reviews: int = 0,
        city_names: CityDirectory | None = None,
    ) -> None:
        self._currency = currency.upper()
        self._adults = adults
        self._guest_nationality = guest_nationality
        # Para que el deep link diga "Budapest", no "BUD" (opcional).
        self._city_names = city_names
        self._client = httpx.AsyncClient(
            timeout=20.0, headers={"X-API-Key": api_key}
        )
        # Candidate-enrichment volume is tiny; keep the bucket modest anyway.
        self._bucket = TokenBucket(rate=2.0, capacity=4.0)
        self._breaker = CircuitBreaker()
        # location -> body fragment that worked last time (skip failed probes)
        self._resolved: dict[str, dict[str, str]] = {}
        # (location, checkin, checkout) -> quote cacheada
        self._quote_cache: dict[tuple[str, str, str], _CachedQuote] = {}
        # hotelId -> name; "" = el hotel no tiene nombre en la API (miss
        # definitivo, también cacheado — solo los fallos transitorios reintentan)
        self._hotel_names: dict[str, str] = {}
        # Suelo de calidad (0 = sin filtro): sin él, "el hotel más barato" de
        # una ciudad es sistemáticamente un ~6 de nota — barato pero inútil.
        # minRating es nota de huéspedes 0-10 (docs: "e.g. 8.6"). Ajustable
        # en caliente vía set_quality_floor (el runtime lo empuja por tick);
        # va el ÚLTIMO del __init__ porque invalida la caché de quotes.
        self._quality_filters: dict[str, Any] = {}
        self.set_quality_floor(min_rating, min_reviews)

    def set_quality_floor(self, min_rating: float, min_reviews: int) -> None:
        """Aplica el suelo de calidad efectivo (0 = sin filtro).

        Si cambia, la caché de quotes se invalida: sus precios se calcularon
        con el suelo anterior y servirían hoteles que ya no cualifican (o
        esconderían los que ahora sí). El memo de locators sobrevive — la
        resolución de ciudad no depende del filtro."""
        filters: dict[str, Any] = {}
        if min_rating > 0:
            filters["minRating"] = min_rating
        if min_reviews > 0:
            filters["minReviewsCount"] = min_reviews
        if filters != self._quality_filters:
            self._quality_filters = filters
            self._quote_cache.clear()
            # Único log del suelo EFECTIVO: cubre arranque, override
            # persistido que pisa al .env en el primer push, y cambios /set.
            log.info("hotel quality floor → %s (quote cache reset)",
                     filters or "sin filtro")

    async def cheapest(
        self, location: str, check_in: date, check_out: date
    ) -> HotelQuote | None:
        nights = (check_out - check_in).days
        if nights < 1:
            return None
        cache_key = (location, check_in.isoformat(), check_out.isoformat())
        cached = self._quote_cache.get(cache_key)
        if cached is None or cached.expires_at <= time.monotonic():
            cached = _CachedQuote(
                expires_at=time.monotonic() + _CACHE_TTL_S,
                offer=await self._cheapest_stay(location, check_in, check_out),
            )
            self._store_in_cache(cache_key, cached)
        offer = cached.offer
        if offer is None:
            return None
        hotel_name = offer.hotel_name
        if hotel_name is None and offer.hotel_id:
            # El precio cacheado no fija un fallo transitorio del lookup de
            # nombre: se reintenta (barato, un GET sin retries). Los misses
            # definitivos ("" en _hotel_names) no re-consultan.
            hotel_name = await self._hotel_name(offer.hotel_id)
            if hotel_name is not None:
                self._quote_cache[cache_key] = cached._replace(
                    offer=offer._replace(hotel_name=hotel_name)
                )
        place_name = None
        if self._city_names is not None:
            place_name = await self._city_names.name(location)
        return HotelQuote(
            location=location,
            check_in=check_in,
            check_out=check_out,
            price_per_night=offer.stay_total / nights,
            currency=self._currency.lower(),
            is_live=True,
            deep_link=self._deep_link(location, check_in, check_out, hotel_name, place_name),
            source=self.name,
            hotel_name=hotel_name,
        )

    async def _cheapest_stay(
        self, location: str, check_in: date, check_out: date
    ) -> _Offer | None:
        """Oferta más barata del viaje que pasa el suelo de calidad."""
        for locator in self._locators(location):
            body: dict[str, Any] = {
                **locator,
                "checkin": check_in.isoformat(),
                "checkout": check_out.isoformat(),
                "occupancies": [{"adults": self._adults}],
                "currency": self._currency,
                "guestNationality": self._guest_nationality,
                **self._quality_filters,
                "maxRatesPerHotel": 1,
                # The hotel list is NOT price-ordered (verified empirically):
                # a small limit would give the cheapest of an arbitrary
                # subset. One large page per trip is still a single POST.
                "limit": 1000,
                "timeout": 12,
            }
            payload = await post_json(self._client, _URL, body, self._bucket, self._breaker)
            if isinstance(payload, dict) and payload.get("data"):
                # La ciudad resolvió: memorizar el locator aunque ninguna
                # oferta sea usable (probar más formas de nombrar la MISMA
                # ciudad no cambiaría el resultado).
                self._resolved[location] = locator
                offer = _cheapest_offer(payload, self._currency)
                if offer is None:
                    # El suelo filtra en el SERVIDOR (data vendría vacía):
                    # aquí la causa es otra — moneda extranjera u ofertas
                    # malformadas.
                    log.info(
                        "liteapi: rates for %r %s..%s but none usable "
                        "(foreign currency / malformed offers)",
                        location, check_in, check_out,
                    )
                return offer
        # data vacía: ciudad desconocida... o el suelo de calidad filtró todo
        # (el servidor no distingue ambos casos en la respuesta).
        log.info(
            "liteapi: no rates found for %r %s..%s%s",
            location, check_in, check_out,
            f" — floor {self._quality_filters} may be filtering everything"
            if self._quality_filters else "",
        )
        return None

    async def _hotel_name(self, hotel_id: str | None) -> str | None:
        """Nombre vía /data/hotel (el rates response solo trae ids).

        Best effort Y aislado a propósito: un GET directo sin retries, fuera
        del TokenBucket y del CircuitBreaker compartidos — un endpoint
        cosmético caído no puede abrir el breaker ni retrasar el camino
        esencial de tarifas. Cualquier fallo → None y la alerta sale igual."""
        if not hotel_id:
            return None
        cached = self._hotel_names.get(hotel_id)
        if cached is not None:
            return cached or None  # "" = miss definitivo cacheado
        try:
            resp = await self._client.get(
                _HOTEL_DATA_URL, params={"hotelId": hotel_id},
                timeout=_NAME_LOOKUP_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — el nombre es opcional
            log.info("liteapi: hotel name lookup failed for %s: %s", hotel_id, exc)
            return None  # no cachear fallos transitorios
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None  # payload inesperado: tratar como transitorio
        # Respuesta válida: cachear también la ausencia de nombre ("") — es un
        # hecho de la API, no un fallo, y evita un GET por cache-hit 6 horas.
        name = str(data.get("name") or "")
        if len(self._hotel_names) >= _CACHE_MAX_ENTRIES:
            self._hotel_names.clear()  # cache cosmética: vaciar es aceptable
        self._hotel_names[hotel_id] = name
        return name or None

    def _store_in_cache(self, key: tuple[str, str, str], quote: _CachedQuote) -> None:
        if len(self._quote_cache) >= _CACHE_MAX_ENTRIES:
            now = time.monotonic()
            self._quote_cache = {
                k: v for k, v in self._quote_cache.items() if v.expires_at > now
            }
        self._quote_cache[key] = quote

    def _locators(self, location: str) -> list[dict[str, str]]:
        """Location strategies to try in order; a previously successful one wins."""
        cached = self._resolved.get(location)
        if cached is not None:
            return [cached]
        locators: list[dict[str, str]] = [{"iataCode": location}]
        metro = _METRO_CITIES.get(location.upper())
        if metro is not None:
            locators.append({"cityName": metro[0], "countryCode": metro[1]})
        return locators

    def _deep_link(
        self, location: str, check_in: date, check_out: date,
        hotel_name: str | None = None, place_name: str | None = None,
    ) -> str:
        # Best-effort actionable link (LiteAPI itself has no public search UI).
        # Con nombre de hotel: Google Hotels aterriza en LA ficha del hotel,
        # con las fechas del viaje, y compara agencias — el precio de la
        # alerta es el de LiteAPI, así que cualquier enlace externo es
        # orientativo. Sin nombre: búsqueda de ciudad en Booking con fechas.
        metro = _METRO_CITIES.get(location.upper())
        place = place_name or (metro[0] if metro else location)
        if hotel_name:
            query = urlencode({
                "q": f"{hotel_name} {place}",
                "checkin": check_in.isoformat(),
                "checkout": check_out.isoformat(),
            })
            return f"https://www.google.com/travel/search?{query}"
        query = urlencode({
            "ss": place,
            "checkin": check_in.isoformat(),
            "checkout": check_out.isoformat(),
            "group_adults": self._adults,
        })
        return f"https://www.booking.com/searchresults.html?{query}"

    async def aclose(self) -> None:
        await self._client.aclose()


def _cheapest_offer(payload: Any, currency: str) -> _Offer | None:
    """Oferta más barata en data[].roomTypes[].offerRetailRate.

    Offers whose currency differs from the requested one are skipped — a
    supplier-native amount (e.g. HUF) min()'d as EUR would be nonsense.
    """
    if not isinstance(payload, dict):
        return None
    best: _Offer | None = None
    for hotel in payload.get("data") or []:
        if not isinstance(hotel, dict):
            continue
        for room_type in hotel.get("roomTypes") or []:
            if not isinstance(room_type, dict):
                continue
            rate = room_type.get("offerRetailRate") or {}
            amount = rate.get("amount")
            offer_currency = str(rate.get("currency") or currency).upper()
            if not amount or offer_currency != currency.upper():
                continue
            total = float(amount)
            if best is None or total < best.stay_total:
                hotel_id = hotel.get("hotelId") or hotel.get("id")
                inline = hotel.get("name") or hotel.get("hotelName")
                best = _Offer(
                    stay_total=total,
                    hotel_id=str(hotel_id) if hotel_id else None,
                    hotel_name=str(inline) if inline else None,
                )
    return best
