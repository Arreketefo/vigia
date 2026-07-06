"""Shared deal -> text rendering used by every channel (domain rendering;
the transports live in radar_core.notifiers)."""

from __future__ import annotations

from radar_core.stats import drop_display

from vigia.contracts import Deal

__all__ = ["deal_lines", "drop_display", "flight_detail_parts"]


def flight_detail_parts(
    deal: Deal, airline_label: str | None, out_label: str, back_label: str
) -> list[str]:
    """Piezas de la línea de detalle de vuelo (aerolínea, horas) — el único
    sitio que decide QUÉ campos la forman; cada canal las une y escapa a su
    manera. Lista vacía = la línea no se muestra."""
    return [
        part for part in (
            airline_label,
            f"{out_label} {deal.depart_time}" if deal.depart_time else None,
            f"{back_label} {deal.return_time}" if deal.return_time else None,
        ) if part
    ]


def deal_lines(deal: Deal) -> list[str]:
    badge = "LIVE" if deal.confirmed else "signal"
    lines = [
        f"{deal.origin} -> {deal.destination} [{badge}]",
        f"{deal.depart_date} -> {deal.return_date} ({deal.nights} nights)",
        f"Total: {deal.total_price:.0f} EUR",
    ]
    detail = flight_detail_parts(deal, deal.airline, "out", "back")
    if detail:
        lines.append(f"Flight detail: {' · '.join(detail)}")
    if deal.hotel_price_night is not None:
        flights_part = deal.total_price - deal.hotel_price_night * deal.nights
        lines.append(
            f"Flights {flights_part:.0f} EUR + hotel {deal.hotel_price_night:.0f} EUR/night"
        )
    if deal.hotel_name:
        lines.append(f"Hotel: {deal.hotel_name}")
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
