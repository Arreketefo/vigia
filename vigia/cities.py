"""IATA city code -> human-readable name, from Travelpayouts' public data
files (~9.6k cities). Loaded lazily once per process; on failure alerts simply
show bare codes. The es locale has gaps (e.g. IBZ/BUD), hence the en fallback
inside each entry's name_translations.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_URL = "https://api.travelpayouts.com/data/{lang}/cities.json"
_RETRY_COOLDOWN_S = 900.0


class CityDirectory:
    def __init__(self, lang: str = "es") -> None:
        self._lang = lang
        self._client = httpx.AsyncClient(timeout=30.0)
        self._names: dict[str, str] = {}
        self._countries: dict[str, str] = {}
        self._loaded = False
        self._next_attempt = 0.0

    async def name(self, code: str) -> str | None:
        await self._ensure_loaded()
        return self._names.get(code.upper())

    async def country(self, code: str) -> str | None:
        """ISO country code of a city (e.g. PMI -> 'ES'), None if unknown."""
        await self._ensure_loaded()
        return self._countries.get(code.upper())

    async def _ensure_loaded(self) -> None:
        # Retry with a cooldown, not per call: a transient startup failure
        # must not permanently disable city names AND the country-exclusion
        # filter, but a persistent one must not add a retry to every alert.
        if self._loaded or time.monotonic() < self._next_attempt:
            return
        await self._load()

    async def _load(self) -> None:
        try:
            resp = await self._client.get(_URL.format(lang=self._lang))
            resp.raise_for_status()
            entries = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._next_attempt = time.monotonic() + _RETRY_COOLDOWN_S
            log.warning(
                "cities dataset unavailable (%s): alerts show bare IATA codes and "
                "exclude_countries CANNOT be applied; retrying in %.0f min",
                exc, _RETRY_COOLDOWN_S / 60,
            )
            return
        self._loaded = True
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            code = entry.get("code")
            if not code:
                continue
            code = str(code).upper()
            name = entry.get("name") or (entry.get("name_translations") or {}).get("en")
            if name:
                self._names[code] = str(name)
            country = entry.get("country_code")
            if country:
                self._countries[code] = str(country).upper()
        log.info("loaded %d city names (lang=%s)", len(self._names), self._lang)

    async def aclose(self) -> None:
        await self._client.aclose()
