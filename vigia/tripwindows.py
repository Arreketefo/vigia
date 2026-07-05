"""Calendar-aware trip windows.

Two phases split by `weekend_only_after`:
- BEFORE: any weekday, stay length within [pre_min, pre_max] nights.
- FROM that date on: weekend/puente escapes only. You may fly out the evening
  of a working day, but every day AFTER the departure day through the return
  day must be free (Saturday, Sunday or holiday). This captures Fri->Sun,
  Sat->Sun, Sat->Mon-holiday, and Thu->Sun when Friday is a holiday — and
  rejects trips that burn working days.

Holidays come from the official ES calendar plus an autonomous-community
subdivision (default VC, Comunitat Valenciana). Municipal fiestas (Hogueras,
Santa Faz...) are not in that calendar: add them via `extra` dates.
"""

from __future__ import annotations

from datetime import date, timedelta

import holidays as holidays_lib


class TripWindowPolicy:
    def __init__(
        self,
        weekend_only_after: date,
        pre_min_nights: int,
        pre_max_nights: int,
        country: str = "ES",
        region: str = "VC",
        extra: set[date] | None = None,
    ) -> None:
        self._switch = weekend_only_after
        self._pre_min = pre_min_nights
        self._pre_max = pre_max_nights
        self._holidays = holidays_lib.country_holidays(country, subdiv=region)
        self._extra = extra or set()

    def allows(self, depart: date, ret: date) -> bool:
        nights = (ret - depart).days
        if nights < 1:
            return False
        if depart < self._switch:
            return self._pre_min <= nights <= self._pre_max
        day = depart + timedelta(days=1)
        while day <= ret:
            if not self._is_free(day):
                return False
            day += timedelta(days=1)
        return True

    def _is_free(self, day: date) -> bool:
        return day.weekday() >= 5 or day in self._holidays or day in self._extra
