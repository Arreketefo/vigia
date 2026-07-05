"""SMTP notifier. smtp_url format: smtp://user:pass@host:587 or smtps://user:pass@host:465."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit

from vigia.contracts import Deal
from vigia.notifiers.format import deal_lines


class EmailNotifier:
    channel = "email"

    def __init__(self, smtp_url: str, to_addr: str) -> None:
        parts = urlsplit(smtp_url)
        if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
            raise ValueError(f"invalid smtp_url: {smtp_url!r}")
        self._host = parts.hostname
        self._port = parts.port or (465 if parts.scheme == "smtps" else 587)
        self._ssl = parts.scheme == "smtps"
        self._user = unquote(parts.username) if parts.username else None
        self._password = unquote(parts.password) if parts.password else None
        self._from = self._user or f"vigia@{self._host}"
        self._to = to_addr

    async def send(self, deal: Deal) -> None:
        await asyncio.to_thread(self._send_sync, deal)

    def _send_sync(self, deal: Deal) -> None:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = self._to
        msg["Subject"] = (
            f"[vigia] {deal.origin}->{deal.destination} {deal.total_price:.0f} EUR"
        )
        msg.set_content("\n".join(deal_lines(deal)))
        smtp_cls = smtplib.SMTP_SSL if self._ssl else smtplib.SMTP
        with smtp_cls(self._host, self._port, timeout=15) as smtp:
            if not self._ssl:
                smtp.ehlo()
                # Plain local relays (port 25) may not offer STARTTLS.
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)
