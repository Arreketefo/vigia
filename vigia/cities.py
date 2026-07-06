"""IATA code -> human-readable name (cities and airlines), from Travelpayouts'
public data files (~9.6k cities). Loaded lazily once per process; on failure
alerts simply show bare codes. The es locale has gaps (e.g. IBZ/BUD), hence
the en fallback inside each entry's name_translations.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_CITIES_URL = "https://api.travelpayouts.com/data/{lang}/cities.json"
_AIRLINES_URL = "https://api.travelpayouts.com/data/{lang}/airlines.json"
_RETRY_COOLDOWN_S = 900.0


class _TravelpayoutsDirectory:
    """Carga perezosa de un dataset público code->name. Política compartida:
    un intento por cooldown (no por llamada), y un 200 con cuerpo inesperado
    cuenta como fallo — marcar 'cargado' un directorio vacío lo dejaría mudo
    hasta el reinicio del proceso."""

    _url_template: str
    _what: str

    def __init__(self, lang: str = "es") -> None:
        self._lang = lang
        self._client = httpx.AsyncClient(timeout=30.0)
        self._names: dict[str, str] = {}
        self._loaded = False
        self._next_attempt = 0.0

    async def name(self, code: str) -> str | None:
        await self._ensure_loaded()
        return self._names.get(code.upper())

    async def _ensure_loaded(self) -> None:
        # Retry with a cooldown, not per call: a transient startup failure
        # must not permanently disable names, but a persistent one must not
        # add a retry to every alert.
        if self._loaded or time.monotonic() < self._next_attempt:
            return
        await self._load()

    async def _load(self) -> None:
        entries: object = None
        try:
            resp = await self._client.get(self._url_template.format(lang=self._lang))
            resp.raise_for_status()
            entries = resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        if not isinstance(entries, list):
            self._next_attempt = time.monotonic() + _RETRY_COOLDOWN_S
            log.warning(
                "%s dataset unavailable: alerts degrade to bare IATA codes; "
                "retrying in %.0f min", self._what, _RETRY_COOLDOWN_S / 60,
            )
            return
        self._loaded = True
        for entry in entries:
            if isinstance(entry, dict):
                self._ingest(entry)
        log.info("loaded %d %s names (lang=%s)", len(self._names), self._what, self._lang)

    def _ingest(self, entry: dict[str, object]) -> None:
        code = entry.get("code")
        name = entry.get("name")
        if not name:
            translations = entry.get("name_translations")
            if isinstance(translations, dict):
                name = translations.get("en")
        if code and name:
            self._names[str(code).upper()] = str(name)

    async def aclose(self) -> None:
        await self._client.aclose()


class CityDirectory(_TravelpayoutsDirectory):
    _url_template = _CITIES_URL
    _what = "cities"

    def __init__(self, lang: str = "es") -> None:
        super().__init__(lang)
        self._countries: dict[str, str] = {}

    async def country(self, code: str) -> str | None:
        """ISO country code of a city (e.g. PMI -> 'ES'), None if unknown."""
        await self._ensure_loaded()
        return self._countries.get(code.upper())

    def _ingest(self, entry: dict[str, object]) -> None:
        super()._ingest(entry)
        code = entry.get("code")
        country = entry.get("country_code")
        if code and country:
            self._countries[str(code).upper()] = str(country).upper()

    async def _load(self) -> None:
        await super()._load()
        if not self._loaded:
            log.warning(
                "without the cities dataset, exclude_countries CANNOT be applied"
            )


class AirlineDirectory(_TravelpayoutsDirectory):
    """IATA airline code -> name ("FR" -> "Ryanair")."""

    _url_template = _AIRLINES_URL
    _what = "airlines"
