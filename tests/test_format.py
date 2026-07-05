from datetime import date

from vigia.contracts import Deal
from vigia.notifiers.format import deal_lines, drop_display


def _deal(**overrides):
    base = dict(
        origin="ALC", destination="BUD",
        depart_date=date(2026, 8, 1), return_date=date(2026, 8, 5), nights=4,
        total_price=350.0, baseline=300.0, drop_pct=-1 / 6, confirmed=False,
        dedup_key="k", flight_link="https://f", hotel_link=None,
    )
    base.update(overrides)
    return Deal(**base)


def test_drop_display_signs():
    assert drop_display(0.30) == "-30%"   # price below baseline
    assert drop_display(-1 / 6) == "+17%"  # hard steal above baseline
    assert drop_display(0.0) == "+0%"      # never the IEEE '-0%'
    assert drop_display(-1e-9) == "+0%"


def test_deal_lines_above_baseline_not_shown_as_discount():
    lines = deal_lines(_deal())
    baseline_line = next(line for line in lines if "Baseline" in line)
    assert "+17%" in baseline_line
    assert "--" not in baseline_line


def test_deal_lines_enriched_shows_split_and_flight_baseline():
    lines = deal_lines(_deal(
        total_price=520.0, baseline=600.0, drop_pct=0.8, hotel_price_night=100.0,
    ))
    assert any("Flights 120 EUR + hotel 100 EUR/night" in line for line in lines)
    assert any(line.startswith("Flight baseline: 600 EUR") for line in lines)


def test_deal_lines_without_baseline():
    lines = deal_lines(_deal(baseline=None, drop_pct=None))
    assert not any("Baseline" in line for line in lines)
    assert not any("None" in line for line in lines if "hotel" in line.lower())
