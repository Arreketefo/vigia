"""Fallback notifier: logs the deal. Used when no real channel is configured
(e.g. `make tick` while validating sources)."""

from __future__ import annotations

import logging

from vigia.contracts import Deal
from vigia.notifiers.format import deal_lines

log = logging.getLogger(__name__)


class ConsoleNotifier:
    channel = "console"

    async def send(self, deal: Deal) -> None:
        log.info("DEAL | %s", " | ".join(deal_lines(deal)))
