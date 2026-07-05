# Vigía

Travel deal radar. Continuously scans flight and hotel prices for trips
departing from Alicante (ALC), detects meaningful price drops with robust
anomaly detection, and delivers curated deal alerts to end users over
Telegram and email.

## How it works

- **Layer 1 — breadth.** A rolling ~11-month window of (route, month) pairs
  is swept round-robin against cached market prices (Travelpayouts/Aviasales
  data APIs), with per-provider token-bucket rate limiting, exponential
  backoff and circuit breakers.
- **Deal engine.** Per route and month, a robust baseline (median + MAD over
  a trailing window) turns raw prices into signal: alerts fire on significant
  relative drops under a configurable budget, plus an absolute "steal"
  threshold. Deduplication ensures one alert per trip, with re-alerts only on
  further significant improvement.
- **Layer 2 — precision.** Candidates that fire are optionally re-priced
  against live inventory before alerting: cheapest real hotel rates (LiteAPI)
  and live bookable flight offers (Duffel), so alerts reflect actual prices.
- **Calendar-aware windows.** Optional trip policies: seasonal stay lengths,
  weekend-only escapes, and automatic long-weekend detection from the
  official holiday calendar (Spain + region), including local fiestas.

## Stack

Python 3.12 (asyncio) · httpx · APScheduler · SQLite (aiosqlite) ·
pydantic-settings · Docker Compose for deployment.

## Quickstart

```bash
cp .env.example .env     # fill in your API tokens
make install && make db-init && make seed
make tick                # one manual sweep to validate data sources
```

Run as a daemon: `make dev` (local, foreground) or `make build && make up`
(Docker, with persistent volume and healthcheck). `make help` lists all
targets, including remote deployment.

## Configuration

Everything is environment-driven — see [`.env.example`](.env.example):
origin airport, detection and full-trip budgets, party size, stay length,
destination country filters, maximum flight duration, calendar trip windows,
detection sensitivity and scan cadence.

## Testing

```bash
make test    # pytest
make lint    # ruff + mypy (strict)
```
