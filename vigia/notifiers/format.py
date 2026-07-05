"""Shared deal -> text rendering used by every channel."""

from __future__ import annotations

from vigia.contracts import Deal


def drop_display(drop_pct: float) -> str:
    """Signed price-vs-baseline delta: drop 0.30 -> '-30%', -0.17 -> '+17%'.

    A hard-steal can fire ABOVE baseline; a hardcoded '-' would render that
    as a bogus double-negative discount. Rounding to int first avoids the
    IEEE negative zero ('-0%') that float formatting produces.
    """
    return f"{round(-drop_pct * 100):+d}%"


def deal_lines(deal: Deal) -> list[str]:
    badge = "LIVE" if deal.confirmed else "signal"
    lines = [
        f"{deal.origin} -> {deal.destination} [{badge}]",
        f"{deal.depart_date} -> {deal.return_date} ({deal.nights} nights)",
        f"Total: {deal.total_price:.0f} EUR",
    ]
    if deal.hotel_price_night is not None:
        flights_part = deal.total_price - deal.hotel_price_night * deal.nights
        lines.append(
            f"Flights {flights_part:.0f} EUR + hotel {deal.hotel_price_night:.0f} EUR/night"
        )
    if deal.baseline is not None:
        drop = f", {drop_display(deal.drop_pct)}" if deal.drop_pct is not None else ""
        # When a hotel was priced on top, the baseline refers to flights only.
        label = "Flight baseline" if deal.hotel_price_night is not None else "Baseline"
        lines.append(f"{label}: {deal.baseline:.0f} EUR{drop}")
    if deal.flight_link:
        lines.append(f"Flight: {deal.flight_link}")
    if deal.hotel_link:
        lines.append(f"Hotel: {deal.hotel_link}")
    return lines
