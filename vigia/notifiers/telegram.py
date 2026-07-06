from radar_core.notifiers import TelegramTransport, escape_markdown
from radar_core.stats import drop_display

from vigia.cities import AirlineDirectory, CityDirectory
from vigia.contracts import Deal
from vigia.notifiers.format import flight_detail_parts


class TelegramNotifier:
    channel = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        cities: CityDirectory | None = None,
        airlines: AirlineDirectory | None = None,
    ) -> None:
        self._transport = TelegramTransport(bot_token, chat_id)
        self._cities = cities
        self._airlines = airlines

    async def send(self, deal: Deal) -> None:
        destination = deal.destination
        if self._cities is not None:
            name = await self._cities.name(deal.destination)
            if name:
                destination = f"{escape_markdown(name)} ({deal.destination})"
        badge = "✅ LIVE" if deal.confirmed else "📡 señal"
        lines = [
            f"*{deal.origin} → {destination}* {badge}",
            f"{deal.depart_date} → {deal.return_date} ({deal.nights} noches)",
        ]
        flight_detail = await self._flight_detail(deal)
        if flight_detail:
            lines.append(flight_detail)
        if deal.baseline is not None and deal.drop_pct is not None:
            # With enrichment the baseline refers to flights only.
            label = "vuelos: típico" if deal.hotel_price_night is not None else "típico"
            lines.append(
                f"*Total: {deal.total_price:.0f} €* "
                f"({label} {deal.baseline:.0f} €, {drop_display(deal.drop_pct)})"
            )
        else:
            lines.append(f"*Total: {deal.total_price:.0f} €*")
        if deal.hotel_price_night is not None:
            flights_part = deal.total_price - deal.hotel_price_night * deal.nights
            # "por noche", not "/noche": Telegram renders /word as a bot command.
            lines.append(
                f"vuelos {flights_part:.0f} € + hotel {deal.hotel_price_night:.0f} € por noche"
            )
        if deal.hotel_name:
            lines.append(f"🏨 {escape_markdown(deal.hotel_name)}")
        links = [
            f"[{label}]({url})"
            for label, url in (("vuelo", deal.flight_link), ("hotel", deal.hotel_link))
            if url
        ]
        if links:
            lines.append(" · ".join(links))
        await self._transport.send_text("\n".join(lines))

    async def _flight_detail(self, deal: Deal) -> str | None:
        """"✈️ Ryanair · ida 06:25 · vuelta 21:40" — solo las partes que la
        fuente aportó; sin ninguna, la línea no existe."""
        airline_label = None
        if deal.airline:
            name = None
            if self._airlines is not None:
                name = await self._airlines.name(deal.airline)
            airline_label = escape_markdown(name or deal.airline)
        parts = flight_detail_parts(deal, airline_label, "ida", "vuelta")
        return "✈️ " + " · ".join(parts) if parts else None

    async def aclose(self) -> None:
        await self._transport.aclose()
