from datetime import date

from conftest import make_settings

from vigia import engine


def test_robust_baseline_median_and_mad():
    totals = [100.0, 110.0, 105.0, 500.0, 95.0]  # one outlier
    median, mad = totals and engine.robust_baseline(totals)
    assert median == 105.0
    assert mad == 5.0  # deviations: 5, 5, 0, 395, 10 -> median 5


def test_robust_baseline_degenerate_window():
    median, mad = engine.robust_baseline([100.0, 100.0, 100.0])
    assert median == 100.0
    assert mad == 0.0


def test_zero_mad_does_not_fire_on_trivial_drop():
    """With an all-identical window (MAD=0) the z-score is undefined; a price
    1 EUR under the median must not fire as an anomaly."""
    cfg = make_settings()
    fire, _ = engine.is_deal(479.0, 480.0, 0.0, sample=20, cfg=cfg)
    assert not fire
    # A genuine >=20% relative drop still fires via the drop test
    fire, _ = engine.is_deal(380.0, 480.0, 0.0, sample=20, cfg=cfg)
    assert fire


def test_fires_on_relative_drop_under_budget():
    cfg = make_settings()
    # median 500, 30% drop, under 600 budget
    fire, drop = engine.is_deal(350.0, 500.0, 20.0, sample=20, cfg=cfg)
    assert fire
    assert drop is not None and abs(drop - 0.30) < 1e-9


def test_no_fire_over_budget_even_with_big_drop():
    cfg = make_settings()
    # 40% drop but 720 > 600 budget and > hard-steal threshold
    fire, _ = engine.is_deal(720.0, 1200.0, 50.0, sample=20, cfg=cfg)
    assert not fire


def test_no_fire_on_normal_price():
    cfg = make_settings()
    fire, _ = engine.is_deal(490.0, 500.0, 30.0, sample=20, cfg=cfg)
    assert not fire


def test_fires_on_robust_z_without_20pct_drop():
    cfg = make_settings()
    # 15% drop (below MIN_DROP_PCT) but tiny MAD -> huge robust z
    fire, _ = engine.is_deal(425.0, 500.0, 5.0, sample=20, cfg=cfg)
    assert fire


def test_small_sample_only_hard_steal():
    cfg = make_settings()
    # sample below MIN_SAMPLE: a 50% anomaly does not fire...
    fire, _ = engine.is_deal(400.0, 800.0, 10.0, sample=3, cfg=cfg)
    assert not fire
    # ...but a hard steal (<= 600 * 0.6 = 360) does
    fire, _ = engine.is_deal(350.0, 800.0, 10.0, sample=3, cfg=cfg)
    assert fire


def test_hard_steal_without_baseline():
    cfg = make_settings()
    fire, drop = engine.is_deal(300.0, None, None, sample=0, cfg=cfg)
    assert fire
    assert drop is None


def test_dedup_key_stable_within_price_bucket():
    d, r = date(2026, 8, 1), date(2026, 8, 5)
    k1 = engine.dedup_key("ALC", "BUD", d, r, 500.0)
    k2 = engine.dedup_key("ALC", "BUD", d, r, 510.0)  # same 25 EUR bucket
    k3 = engine.dedup_key("ALC", "BUD", d, r, 540.0)  # different bucket
    assert k1 == k2
    assert k1 != k3


def test_dedup_key_distinguishes_trips():
    d, r = date(2026, 8, 1), date(2026, 8, 5)
    assert engine.dedup_key("ALC", "BUD", d, r, 500.0) != engine.dedup_key(
        "ALC", "PRG", d, r, 500.0
    )
    assert engine.dedup_key("ALC", "BUD", d, r, 500.0) != engine.dedup_key(
        "ALC", "BUD", d, None, 500.0
    )
