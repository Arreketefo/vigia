from vigia.cities import AirlineDirectory, CityDirectory
from vigia.config import Settings
from vigia.contracts import Notifier
from vigia.notifiers.console import ConsoleNotifier
from vigia.notifiers.email import EmailNotifier
from vigia.notifiers.telegram import TelegramNotifier


def build_notifiers(
    cfg: Settings,
    cities: CityDirectory | None = None,
    airlines: AirlineDirectory | None = None,
) -> list[Notifier]:
    """Fan-out targets from config; falls back to console so deals are never silent."""
    notifiers: list[Notifier] = []
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notifiers.append(
            TelegramNotifier(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                cities=cities, airlines=airlines,
            )
        )
    if cfg.smtp_url and cfg.alert_email_to:
        notifiers.append(EmailNotifier(cfg.smtp_url, cfg.alert_email_to))
    if not notifiers:
        notifiers.append(ConsoleNotifier())
    return notifiers


__all__ = [
    "ConsoleNotifier",
    "EmailNotifier",
    "TelegramNotifier",
    "build_notifiers",
]
