"""How far a joint moved, and how much that movement may have worn it. Pure functions.

**Travel** sums a joint's movement after a dead band, because summing raw differences counts sensor
noise as motion: a joint at rest would rack up distance indefinitely.

Three wear approximations build on it, each a standard engineering rule of thumb. All of them count
only movement under load, because a load held still slides nothing:

- **Reversals**: changes of direction. Cables fatigue by bending back and forth over pulleys.
- **Load work**, ``sum(|effort| * |dq|)``: Archard's wear law says sliding wear grows with load
  times distance. For a joint this is also the energy that went through it, in joules.
- **Damage**, ``sum(|effort|^p * |dq|)`` with p = 3: how rolling-bearing life is rated. L10 life
  goes as ``(C / P)^3``, so doubling the load cuts life by eight; the same exponent gives the
  equivalent steady load for a varying one (Palmgren-Miner, the cubic mean load).

``effort`` on a dVRK comes from motor current, so it includes gravity and friction but is not
force at the bearing. These numbers compare joints; they do not predict when one will fail.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_DEADBAND_RAD = 1e-3
DEFAULT_DEADBAND_M = 1e-4
DEFAULT_MAX_GAP_S = 1.0
DEFAULT_EXPONENT = 3.0
PRISMATIC_MARKERS = ("insertion",)
EFFORT_SMOOTHING_S = 0.1
MIN_SAMPLES_TO_JUDGE_EFFORT = 200

QUALITY_OK = "ok"
QUALITY_NOISE = "noise"
QUALITY_ABSENT = "absent"


def unit_of(joint: str) -> str:
    """``m`` for a prismatic joint, otherwise ``rad``."""
    return "m" if any(marker in joint for marker in PRISMATIC_MARKERS) else "rad"


@dataclass
class Settings:
    """What the totals were computed with; changing any of it means recomputing them."""

    deadband_rad: float = DEFAULT_DEADBAND_RAD
    deadband_m: float = DEFAULT_DEADBAND_M
    max_gap_s: float = DEFAULT_MAX_GAP_S
    exponent: float = DEFAULT_EXPONENT

    def deadband(self, joint: str) -> float:
        return self.deadband_m if unit_of(joint) == "m" else self.deadband_rad


@dataclass
class FilterState:
    """Where a joint's dead band stands, kept between reads so that totals continue seamlessly."""

    reference: float | None = None
    direction: int = 0
    last_us: int | None = None


@dataclass
class Usage:
    """What one joint did over some samples."""

    n_samples: int = 0
    observed_s: float = 0.0
    travel: float = 0.0
    reversals: int = 0
    gaps: int = 0
    travel_loaded: float = 0.0
    travel_noise: float = 0.0
    travel_absent: float = 0.0
    work: float = 0.0
    damage: float = 0.0
    peak_effort: float = 0.0


def effort_quality(effort: np.ndarray, velocity: np.ndarray) -> str:
    """Whether a joint's effort readings can be believed as a load.

    Some sources report no real effort: all zeros from a kinematic simulation, or white noise, as
    recordings from before 2026-11-02 hold. A real load either has a steady part (gravity) or
    follows motion (friction). A signal with neither is scored as no load data, not as a low load.

    Returns:
        :data:`QUALITY_OK`, :data:`QUALITY_NOISE` or :data:`QUALITY_ABSENT`.
    """
    finite = np.isfinite(effort)
    if len(effort) == 0 or finite.mean() < 0.5 or np.nanmax(np.abs(effort[finite])) < 1e-9:
        return QUALITY_ABSENT
    tau = effort[finite]
    if len(tau) < MIN_SAMPLES_TO_JUDGE_EFFORT:
        return QUALITY_OK
    steady = abs(tau.mean()) >= 0.5 * tau.std()
    v = np.abs(np.nan_to_num(velocity[finite]))
    follows_motion = False
    if v.std() > 1e-12 and np.abs(tau).std() > 1e-12:
        follows_motion = abs(float(np.corrcoef(np.abs(tau), v)[0, 1])) >= 0.1
    return QUALITY_OK if steady or follows_motion else QUALITY_NOISE


def smooth_effort(t_us: np.ndarray, effort: np.ndarray) -> np.ndarray:
    """Average effort over about 0.1 s.

    Noise only ever adds to ``|effort|^3``, so without this a noisier sensor would look like more
    wear.
    """
    if len(effort) < 3:
        return effort
    dt = float(np.median(np.diff(t_us))) * 1e-6
    window = max(1, int(round(EFFORT_SMOOTHING_S / max(dt, 1e-6))))
    series = pd.Series(effort)
    return series.rolling(window, min_periods=1, center=True).mean().to_numpy()


def integrate(
    t_us: np.ndarray,
    position: np.ndarray,
    effort: np.ndarray,
    quality: str,
    state: FilterState,
    deadband: float,
    settings: Settings,
) -> tuple[Usage, FilterState]:
    """Add up one joint's movement over consecutive samples.

    Args:
        t_us: Sample times, ascending, in microseconds.
        position: Joint position per sample.
        effort: Joint effort per sample.
        quality: How far to believe ``effort``, from :func:`effort_quality`.
        state: Where the previous call left off.
        deadband: Movement smaller than this does not count until it accumulates.
        settings: Gap limit and exponent.

    Returns:
        The usage over these samples, and the state to continue from.
    """
    usage = Usage(n_samples=len(t_us))
    state = FilterState(state.reference, state.direction, state.last_us)
    tau = smooth_effort(t_us, effort) if quality == QUALITY_OK else effort
    max_gap_us = settings.max_gap_s * 1e6
    p = settings.exponent
    for i in range(len(t_us)):
        q = position[i]
        t = int(t_us[i])
        if state.last_us is not None:
            elapsed = t - state.last_us
            if elapsed > max_gap_us:
                # Not observed: the joint may have moved, but by how much is unknown.
                usage.gaps += 1
                state.reference = None
            else:
                usage.observed_s += elapsed * 1e-6
        state.last_us = t
        if not np.isfinite(q):
            continue
        if state.reference is None:
            state.reference = float(q)
            continue
        step = q - state.reference
        size = abs(step)
        if size < deadband:
            continue
        direction = 1 if step > 0 else -1
        if state.direction != 0 and direction != state.direction:
            usage.reversals += 1
        state.direction = direction
        state.reference = float(q)
        usage.travel += size
        load = abs(tau[i]) if np.isfinite(tau[i]) else None
        if quality == QUALITY_OK and load is not None:
            usage.travel_loaded += size
            usage.work += load * size
            usage.damage += load**p * size
            usage.peak_effort = max(usage.peak_effort, load)
        elif quality == QUALITY_NOISE:
            usage.travel_noise += size
        else:
            usage.travel_absent += size
    return usage, state


def equivalent_load(
    damage: float, travel_loaded: float, exponent: float = DEFAULT_EXPONENT
) -> float:
    """The steady load that would do the same damage over the same travel: the cubic mean load."""
    if travel_loaded <= 0:
        return float("nan")
    return (damage / travel_loaded) ** (1 / exponent)


def life_used(
    damage: float, rated_load: float, rated_travel: float, p: float = DEFAULT_EXPONENT
) -> float:
    """Fraction of rated life used, by the same rule a bearing's rating is defined with.

    Args:
        damage: Accumulated damage for the joint.
        rated_load: Load at which the joint is rated.
        rated_travel: Travel it is rated to last at that load.
        p: The exponent ``damage`` was computed with.
    """
    return damage / (rated_load**p * rated_travel)
