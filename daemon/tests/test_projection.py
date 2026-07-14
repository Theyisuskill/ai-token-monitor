"""project_depletion: when does the real % hit 100 at the current pace."""

from ai_token_monitor.live.projection import project_depletion

T0 = 100_000.0


def _samples(*points):
    return [(T0 + m * 60.0, pct) for m, pct in points]


def test_steady_burn_projects_depletion():
    # 50% -> 60% over 20 min = 0.5%/min; 40% left -> depletes in 80 min.
    samples = _samples((0, 50.0), (10, 55.0), (20, 60.0))
    now = T0 + 20 * 60.0
    out = project_depletion(samples, now, resets_at=now + 10_000_000.0)
    assert out is not None
    assert abs(out - (now + 80 * 60.0)) < 1.0


def test_flat_usage_projects_nothing():
    assert project_depletion(_samples((0, 50.0), (20, 50.0)),
                             T0 + 20 * 60.0, None) is None


def test_decreasing_usage_projects_nothing():
    assert project_depletion(_samples((0, 60.0), (20, 50.0)),
                             T0 + 20 * 60.0, None) is None


def test_single_sample_is_not_a_rate():
    assert project_depletion(_samples((0, 50.0)), T0, None) is None


def test_samples_too_close_together():
    # 3 minutes apart is under MIN_SPAN_S: jitter, not a rate.
    assert project_depletion(_samples((0, 50.0), (3, 51.0)),
                             T0 + 3 * 60.0, None) is None


def test_depletion_after_reset_is_uninteresting():
    # Same pace as the steady case (depletes in 80 min) but the window
    # resets in 30 — the rollover wins, nothing to warn about.
    samples = _samples((0, 50.0), (20, 60.0))
    now = T0 + 20 * 60.0
    assert project_depletion(samples, now, resets_at=now + 30 * 60.0) is None


def test_old_samples_fall_out_of_the_horizon():
    # The only early sample is >1h old at `now`; just one remains -> no rate.
    samples = _samples((0, 10.0), (90, 60.0))
    now = T0 + 90 * 60.0
    assert project_depletion(samples, now, None) is None


def test_already_at_100_percent():
    samples = _samples((0, 90.0), (20, 100.0))
    assert project_depletion(samples, T0 + 20 * 60.0, None) is None
