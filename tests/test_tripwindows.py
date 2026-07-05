"""TripWindowPolicy: 2026 real calendar — 9-oct (CV day) is a Friday and
12-oct (national) is a Monday, so the autumn has genuine puentes to test."""

from datetime import date

from vigia.tripwindows import TripWindowPolicy

SWITCH = date(2026, 9, 6)


def policy(**overrides) -> TripWindowPolicy:
    defaults = dict(
        weekend_only_after=SWITCH, pre_min_nights=4, pre_max_nights=5, region="VC",
    )
    defaults.update(overrides)
    return TripWindowPolicy(**defaults)


def test_before_switch_any_weekday_4_5_nights():
    p = policy()
    # Tue 2026-07-14 -> Sat 2026-07-18: 4 nights, weekdays -> allowed
    assert p.allows(date(2026, 7, 14), date(2026, 7, 18))
    assert p.allows(date(2026, 7, 14), date(2026, 7, 19))  # 5 nights
    assert not p.allows(date(2026, 7, 14), date(2026, 7, 16))  # 2 nights
    assert not p.allows(date(2026, 7, 14), date(2026, 7, 21))  # 7 nights


def test_after_switch_weekend_trips():
    p = policy()
    fri, sat, sun = date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)
    mon = date(2026, 9, 14)
    assert p.allows(fri, sun)   # out Friday evening, back Sunday
    assert p.allows(sat, sun)
    assert not p.allows(fri, mon)   # plain Monday is a working day
    assert not p.allows(date(2026, 9, 15), date(2026, 9, 17))  # midweek


def test_after_switch_puente_with_monday_holiday():
    p = policy()
    # 12-oct-2026 (Fiesta Nacional) is a Monday
    assert p.allows(date(2026, 10, 10), date(2026, 10, 12))  # Sat -> holiday Mon
    assert p.allows(date(2026, 10, 9), date(2026, 10, 12))   # Fri -> holiday Mon


def test_after_switch_puente_with_friday_holiday():
    p = policy()
    # 9-oct-2026 (día de la Comunitat Valenciana) is a Friday
    assert p.allows(date(2026, 10, 8), date(2026, 10, 11))  # Thu evening -> Sun


def test_extra_local_holiday_creates_bridge():
    # Wed 2026-09-23 declared a local fiesta: Tue evening -> Wed only
    p = policy(extra={date(2026, 9, 23)})
    assert p.allows(date(2026, 9, 22), date(2026, 9, 23))
    assert not policy().allows(date(2026, 9, 22), date(2026, 9, 23))


def test_one_way_or_inverted_dates_rejected():
    p = policy()
    assert not p.allows(date(2026, 10, 10), date(2026, 10, 10))
