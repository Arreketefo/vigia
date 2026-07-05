from datetime import date

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: a blank value in .env (VIGIA_X=) means "use the
    # default" instead of crashing numeric fields at startup.
    model_config = SettingsConfigDict(
        env_prefix="VIGIA_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

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
    # Full-trip cap (flight*pax + hotel*nights) applied to enriched candidates
    # in 'candidates' mode. budget_cap governs detection (flight-only totals).
    trip_budget_cap: float = 600.0
    db_path: str = "/data/vigia.db"
    # Notifiers
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    smtp_url: str | None = None
    alert_email_to: str | None = None
    # Layer 2 (optional)
    duffel_token: str | None = None
    enable_price_confirmer: bool = False

    def excluded_countries(self) -> set[str]:
        return {c.strip().upper() for c in self.exclude_countries.split(",") if c.strip()}

    def extra_holiday_dates(self) -> set[date]:
        return {
            date.fromisoformat(d.strip())
            for d in self.extra_holidays.split(",")
            if d.strip()
        }
