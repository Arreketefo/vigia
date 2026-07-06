"""Cableado del Runtime: lo que promete el bot tiene que llegar a las fuentes.

El push del suelo de calidad es un enlace duck-typed (getattr por nombre):
sin este test, un rename en la fuente dejaría /set aceptando valores que
jamás se aplican, con toda la suite en verde."""

from conftest import make_settings

from vigia.runtime import Runtime
from vigia.store import PriceStore


class _NoFlights:
    name = "none"

    async def search_range(self, *args: object, **kwargs: object) -> list[object]:
        return []

    async def calendar(self, *args: object, **kwargs: object) -> list[object]:
        return []


async def test_run_tick_pushes_hot_floor_override_to_the_source(store: PriceStore):
    cfg = make_settings(
        hotel_source="liteapi", liteapi_key="sand_test",
        hotel_min_rating=7.0, hotel_min_reviews=50,
    )
    runtime = Runtime(cfg, store)
    try:
        runtime.flights = _NoFlights()  # type: ignore[assignment] — sin red
        assert runtime.enricher is not None  # hotel_mode default: candidates
        assert runtime.enricher._quality_filters == {  # type: ignore[attr-defined]
            "minRating": 7.0, "minReviewsCount": 50,
        }

        # override del bot (persistido en DB) -> el siguiente tick lo empuja
        await store.set_override("hotel_min_rating", "8.5")
        await store.set_override("hotel_min_reviews", "0")
        await runtime.run_tick()
        assert runtime.enricher._quality_filters == {"minRating": 8.5}  # type: ignore[attr-defined]

        # /reset -> vuelve al .env en el tick siguiente
        await store.delete_override("hotel_min_rating")
        await store.delete_override("hotel_min_reviews")
        await runtime.run_tick()
        assert runtime.enricher._quality_filters == {  # type: ignore[attr-defined]
            "minRating": 7.0, "minReviewsCount": 50,
        }
    finally:
        await runtime.aclose()
