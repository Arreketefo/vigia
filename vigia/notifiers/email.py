"""Email channel: domain rendering over the core SMTP transport."""

from __future__ import annotations

from radar_core.notifiers import SmtpSender

from vigia.contracts import Deal
from vigia.notifiers.format import deal_lines


class EmailNotifier:
    channel = "email"

    def __init__(self, smtp_url: str, to_addr: str) -> None:
        self._sender = SmtpSender(smtp_url, to_addr, app_name="vigia")

    async def send(self, deal: Deal) -> None:
        subject = f"[vigia] {deal.origin}->{deal.destination} {deal.total_price:.0f} EUR"
        await self._sender.send(subject, "\n".join(deal_lines(deal)))
