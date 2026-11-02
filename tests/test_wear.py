from __future__ import annotations

import numpy as np
import pytest

from fleet_wear.wear import (
    QUALITY_ABSENT,
    QUALITY_NOISE,
    QUALITY_OK,
    FilterState,
    Settings,
    effort_quality,
    equivalent_load,
    integrate,
    life_used,
    unit_of,
)

RATE_HZ = 50
T0 = 1_700_000_000_000_000


def _times(seconds, rate=RATE_HZ):
    return T0 + (np.arange(int(seconds * rate)) * (1_000_000 // rate)).astype(np.int64)


def _run(position, effort=None, quality=QUALITY_OK, deadband=1e-3, t=None, state=None, **settings):
    t = _times(len(position) / RATE_HZ) if t is None else t
    effort = np.ones(len(position)) if effort is None else effort
    return integrate(
        t, position, effort, quality, state or FilterState(), deadband, Settings(**settings)
    )


def _sine(amplitude=0.1, hz=0.5, seconds=20.0):
    t = np.arange(int(seconds * RATE_HZ)) / RATE_HZ
    return amplitude * np.sin(2 * np.pi * hz * t)


def test_travel_of_a_sinusoid_is_four_amplitudes_per_cycle():
    usage, _ = _run(_sine(amplitude=0.1, hz=0.5, seconds=20.0))
    assert usage.travel == pytest.approx(4 * 0.1 * 10, rel=0.02)


def test_reversals_count_each_change_of_direction():
    usage, _ = _run(_sine(amplitude=0.1, hz=0.5, seconds=20.0))
    assert usage.reversals == pytest.approx(19, abs=1)  # 10 cycles, 2 turns each, less the start


def test_a_still_noisy_joint_has_not_moved():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 1e-4, 50_000)
    usage, _ = _run(noise, deadband=1e-3)
    raw = np.abs(np.diff(noise)).sum()
    assert raw > 5  # what summing differences would have reported
    assert usage.travel == 0.0 and usage.reversals == 0


def test_a_slow_drift_still_counts_once_it_passes_the_dead_band():
    usage, _ = _run(np.linspace(0, 0.1, 5000), deadband=1e-3)
    assert usage.travel == pytest.approx(0.1, rel=0.02)
    assert usage.reversals == 0


def test_noise_on_real_motion_barely_changes_the_travel():
    rng = np.random.default_rng(1)
    clean = _sine(amplitude=0.2, hz=0.25, seconds=40.0)
    usage, _ = _run(clean + rng.normal(0, 5e-5, len(clean)))
    assert usage.travel == pytest.approx(4 * 0.2 * 10, rel=0.03)


def test_splitting_a_recording_changes_nothing():
    position = _sine()
    effort = 1 + 0.3 * np.sin(np.arange(len(position)) / 7.0)
    t = _times(len(position) / RATE_HZ)
    whole, _ = _run(position, effort, t=t)
    state, parts = FilterState(), []
    for lo, hi in ((0, 137), (137, 500), (500, 501), (501, len(position))):
        usage, state = _run(position[lo:hi], effort[lo:hi], t=t[lo:hi], state=state)
        parts.append(usage)
    assert sum(u.travel for u in parts) == pytest.approx(whole.travel, abs=1e-9)
    assert sum(u.reversals for u in parts) == whole.reversals
    assert sum(u.n_samples for u in parts) == whole.n_samples
    assert sum(u.observed_s for u in parts) == pytest.approx(whole.observed_s)


def test_movement_during_a_gap_in_the_data_is_not_counted():
    t = np.concatenate([_times(10), _times(10) + 3_600_000_000])
    position = np.concatenate([np.zeros(500), np.full(500, 1.0)])
    usage, _ = _run(position, t=t)
    assert usage.travel == 0.0 and usage.gaps == 1
    assert usage.observed_s == pytest.approx(20.0, abs=0.1)


def test_work_and_damage_under_a_constant_load():
    position = np.linspace(0, 1.0, 5000)
    usage, _ = _run(position, np.full(5000, 2.0))
    assert usage.work == pytest.approx(2.0 * usage.travel, rel=1e-6)
    assert usage.damage == pytest.approx(8.0 * usage.travel, rel=1e-6)
    assert usage.peak_effort == pytest.approx(2.0)
    assert usage.travel_loaded == pytest.approx(usage.travel)


def test_doubling_the_load_counts_eight_times_as_much_damage():
    position = _sine()
    low, _ = _run(position, np.full(len(position), 1.0))
    high, _ = _run(position, np.full(len(position), 2.0))
    assert high.damage == pytest.approx(8 * low.damage, rel=1e-6)
    assert high.work == pytest.approx(2 * low.work, rel=1e-6)
    assert high.travel == pytest.approx(low.travel)


def test_a_load_held_still_does_no_wear():
    usage, _ = _run(np.zeros(1000), np.full(1000, 5.0))
    assert usage.work == 0.0 and usage.damage == 0.0


def test_the_sign_of_effort_does_not_matter():
    position = _sine()
    pushing, _ = _run(position, np.full(len(position), 1.5))
    pulling, _ = _run(position, np.full(len(position), -1.5))
    assert pushing.damage == pytest.approx(pulling.damage)


def test_noise_does_not_inflate_damage_much():
    """|tau|^3 of a noisy signal is biased upward; smoothing keeps that small."""
    rng = np.random.default_rng(2)
    position = _sine(seconds=60.0)
    clean, _ = _run(position, np.full(len(position), 1.0))
    noisy, _ = _run(position, 1.0 + rng.normal(0, 0.3, len(position)))
    assert noisy.damage == pytest.approx(clean.damage, rel=0.08)


@pytest.mark.parametrize(
    "quality,field", [(QUALITY_NOISE, "travel_noise"), (QUALITY_ABSENT, "travel_absent")]
)
def test_unusable_effort_keeps_the_travel_and_scores_no_load(quality, field):
    position = _sine()
    usage, _ = _run(position, np.full(len(position), 5.0), quality=quality)
    assert usage.travel > 1 and getattr(usage, field) == pytest.approx(usage.travel)
    assert usage.work == 0.0 and usage.damage == 0.0 and usage.travel_loaded == 0.0


def test_nan_position_or_effort_is_skipped():
    position = _sine()
    position[100:120] = np.nan
    effort = np.ones(len(position))
    effort[300:320] = np.nan
    usage, _ = _run(position, effort)
    assert np.isfinite(usage.travel) and np.isfinite(usage.damage)
    assert usage.travel > 0


def _velocity(position):
    return np.gradient(position, 1 / RATE_HZ)


def test_quality_accepts_friction_that_follows_motion():
    position = _sine(seconds=30.0)
    v = _velocity(position)
    rng = np.random.default_rng(3)
    effort = 0.15 * np.tanh(v / 0.01) + rng.normal(0, 0.015, len(v))
    assert effort_quality(effort, v) == QUALITY_OK


def test_quality_accepts_a_steady_gravity_load_even_on_a_still_joint():
    v = np.zeros(1000)
    rng = np.random.default_rng(4)
    assert effort_quality(0.6 + rng.normal(0, 0.02, 1000), v) == QUALITY_OK


def test_quality_rejects_white_noise_as_older_recordings_hold():
    position = _sine(seconds=30.0)
    rng = np.random.default_rng(5)
    assert effort_quality(rng.normal(0, 0.05, len(position)), _velocity(position)) == QUALITY_NOISE


@pytest.mark.parametrize(
    "effort",
    [np.zeros(1000), np.full(1000, np.nan), np.array([])],
    ids=["zeros", "nan", "empty"],
)
def test_quality_reports_absent_effort(effort):
    assert effort_quality(effort, np.zeros(len(effort))) == QUALITY_ABSENT


def test_too_few_samples_are_not_judged_as_noise():
    rng = np.random.default_rng(6)
    assert effort_quality(rng.normal(0, 0.05, 50), np.zeros(50)) == QUALITY_OK


def test_equivalent_load_of_a_steady_load_is_that_load():
    assert equivalent_load(8.0 * 3.0, 3.0) == pytest.approx(2.0)
    assert np.isnan(equivalent_load(0.0, 0.0))


def test_equivalent_load_weights_heavy_periods_more_than_their_share():
    # half the travel at 1, half at 3: the mean is 2 but the cubic mean is 2.47
    damage = 1.0**3 * 5 + 3.0**3 * 5
    assert equivalent_load(damage, 10.0) == pytest.approx(14 ** (1 / 3), rel=1e-6)


def test_life_used():
    assert life_used(damage=8.0 * 500.0, rated_load=2.0, rated_travel=1000.0) == pytest.approx(0.5)


def test_units():
    assert unit_of("outer_insertion") == "m" and unit_of("outer_pitch") == "rad"
