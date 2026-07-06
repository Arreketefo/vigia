from datetime import date, datetime

from pydantic import field_validator
from pydantic_settings import BaseSettings
from radar_core.config import csv_dates, csv_set, radar_settings_config


def hhmm_or_empty(raw: str) -> str:
    """"HH:MM" normalizado ("7:30" → "07:30"), o "" = desactivado.

    "off" también desactiva: el bot de Telegram no puede enviar un valor
    vacío con /set, así que necesita una palabra para apagar el filtro.
    Valida en el borde (env y overrides del bot): un horario malformado debe
    fallar al configurarse, no compararse en silencio como string en el tick.
    """
    raw = raw.strip()
    if not raw or raw.lower() == "off":
        return ""
    return f"{datetime.strptime(raw, '%H:%M'):%H:%M}"


class Settings(BaseSettings):
    model_config = radar_settings_config("VIGIA_")

    travelpayouts_token: str
    origin: str = "ALC"
    currency: str = "eur"
    market: str = "es"  # Travelpayouts data-source market (defaults to 'ru' server-side)
    budget_cap: float = 600.0
    pax: int = 2
    min_drop_pct: float = 0.20
    z_threshold: float = 2.0
    hard_steal_ratio: float = 0.60
    realert_drop: float = 0.10
    min_sample: int = 8
    tick_interval_s: int = 300
    batch_size: int = 20
    # Cheapest flight-days per (route, month) that get a hotel lookup. Bounds
    # Hotellook volume: batch_size * max_quotes_per_pair requests per tick.
    max_quotes_per_pair: int = 5
    window_days: int = 330
    discovery: bool = True
    trip_min_nights: int = 2
    trip_max_nights: int = 14
    # Destination filters: ISO country codes to skip (comma-separated, e.g.
    # "ES") and a one-way flight-time bound in hours (quotes whose duration
    # is unknown pass through).
    exclude_countries: str = ""
    max_flight_hours: float | None = None
    # Filtro de horario OPCIONAL (vacío u "off" = sin filtro, comportamiento
    # de siempre). OJO: la Capa 1 trae EL vuelo más barato de cada día, así
    # que esto DESCARTA los días cuyo chollo sale a horas malas — no
    # encuentra horas mejores. Los quotes sin hora conocida PASAN el filtro.
    # La comparación es de misma jornada: return_before="23:30" = "la vuelta
    # no sale después de las 23:30"; NO expresa "hasta pasada la medianoche"
    # (un valor de madrugada como "00:30" filtraría casi todo). Al activarlo,
    # la población de observaciones cambia: la baseline tarda unos 30-45 días
    # en re-asentarse con solo días de horario bueno.
    depart_after: str = ""      # "HH:MM": la ida no sale antes de esta hora
    return_before: str = ""     # "HH:MM": la vuelta no sale después de esta
    # Calendar trip windows (see vigia/tripwindows.py). Empty = disabled.
    # Before this date: any weekday, pre_weekend nights; from it on: only
    # weekends/puentes per the ES + holidays_region calendar.
    weekend_only_after: str = ""            # 'YYYY-MM-DD'
    pre_weekend_nights_min: int = 4
    pre_weekend_nights_max: int = 5
    holidays_region: str = "VC"
    extra_holidays: str = ""                # comma 'YYYY-MM-DD' (local fiestas)
    # Hotel source: 'none' (flight-only), 'liteapi' (recommended), or
    # 'hotellook' (dead upstream since 2025-10-20; kept in case it returns).
    hotel_source: str = "none"
    # How the hotel source is used: 'candidates' prices the hotel only for
    # deals that already fired (a handful of calls/day; detection stays
    # flight-only); 'sweep' puts the hotel in every Layer-1 scan (heavy).
    hotel_mode: str = "candidates"
    liteapi_key: str | None = None
    # Suelo de calidad del hotel (filtros server-side de LiteAPI; 0 = sin
    # filtro). min_rating = nota de huéspedes 0-10; min_reviews evita notas
    # altas con 3 reseñas. Ajustables EN CALIENTE por el bot (/set); el
    # runtime empuja el valor efectivo a la fuente antes de cada tick.
    hotel_min_rating: float = 7.0
    hotel_min_reviews: int = 50
    # Full-trip cap (flight*pax + hotel*nights) applied to enriched candidates
    # in 'candidates' mode. budget_cap governs detection (flight-only totals).
    trip_budget_cap: float = 600.0
    digest_hour: int = 9                    # hora UTC del digest diario (/digest on)
    db_path: str = "/data/vigia.db"
    # Notifiers
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    smtp_url: str | None = None
    alert_email_to: str | None = None
    # Layer 2 (optional)
    duffel_token: str | None = None
    enable_price_confirmer: bool = False

    @field_validator("depart_after", "return_before")
    @classmethod
    def _valid_hhmm(cls, value: str) -> str:
        return hhmm_or_empty(value)

    def excluded_countries(self) -> set[str]:
        return csv_set(self.exclude_countries)

    def extra_holiday_dates(self) -> set[date]:
        return csv_dates(self.extra_holidays)
