"""Read new joint states from the API into the ledger."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fleet_wear.client import PAGE, ApiClient
from fleet_wear.ledger import Ledger
from fleet_wear.wear import FilterState, Usage, effort_quality, integrate

log = logging.getLogger(__name__)

ARMS = ("PSM1", "PSM2", "PSM3", "ECM", "MTML", "MTMR")
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


@dataclass
class SyncResult:
    rows: int = 0
    arms: set[tuple[str, str]] = field(default_factory=set)


def _us(values: list[str]) -> np.ndarray:
    parsed = pd.to_datetime(pd.Series(values), utc=True, format="ISO8601")
    return parsed.dt.as_unit("us").astype("int64").to_numpy()


def _format_us(value: int) -> str:
    return pd.Timestamp(value, unit="us", tz="UTC").strftime(TS_FORMAT)


def _matrix(rows: list[dict], key: str, width: int) -> np.ndarray:
    out = np.full((len(rows), width), np.nan)
    for i, row in enumerate(rows):
        values = row.get(key) or []
        if len(values) == width:
            out[i] = [np.nan if v is None else v for v in values]
    return out


def _add(total: Usage, part: Usage) -> None:
    for name in Usage.__dataclass_fields__:
        if name == "peak_effort":
            total.peak_effort = max(total.peak_effort, part.peak_effort)
        else:
            setattr(total, name, getattr(total, name) + getattr(part, name))


def process_page(
    rows: list[dict], states: dict[str, FilterState], ledger: Ledger
) -> tuple[dict[tuple[str, str], Usage], dict[str, FilterState], int]:
    """Usage per joint and hour for one page of an arm's rows, in time order."""
    names = list(rows[0]["joint_names"])
    t_us = _us([r["ts"] for r in rows])
    position = _matrix(rows, "position", len(names))
    velocity = _matrix(rows, "velocity", len(names))
    effort = _matrix(rows, "effort", len(names))
    hours = pd.to_datetime(t_us, unit="us", utc=True).floor("h").strftime("%Y-%m-%dT%H:00:00+00:00")
    edges = np.flatnonzero(np.r_[True, hours[1:] != hours[:-1]])
    hourly: dict[tuple[str, str], Usage] = {}
    new_states = dict(states)
    settings = ledger.settings
    for j, joint in enumerate(names):
        # Judged per page: an hour or a session could mix sources, and a page is short.
        quality = effort_quality(effort[:, j], velocity[:, j])
        state = new_states.get(joint, FilterState())
        for k, lo in enumerate(edges):
            hi = edges[k + 1] if k + 1 < len(edges) else len(t_us)
            usage, state = integrate(
                t_us[lo:hi],
                position[lo:hi, j],
                effort[lo:hi, j],
                quality,
                state,
                settings.deadband(joint),
                settings,
            )
            total = hourly.setdefault((joint, hours[lo]), Usage())
            _add(total, usage)
        new_states[joint] = state
    return hourly, new_states, int(t_us[-1])


def sync_arm(client: ApiClient, ledger: Ledger, robot_id: str, arm: str, page: int = PAGE) -> int:
    """Read everything new for one arm. Returns the number of rows read."""
    watermark = ledger.watermark(robot_id, arm)
    states = ledger.filter_state(robot_id, arm)
    total = 0
    while True:
        # From the watermark, not by offset: rows arriving meanwhile cannot shift the pages.
        start = _format_us(watermark) if watermark is not None else None
        rows = client.joints_after(robot_id, arm, start, page)
        fresh = rows
        if watermark is not None:
            times = _us([r["ts"] for r in rows]) if rows else np.empty(0, dtype=np.int64)
            fresh = [r for r, t in zip(rows, times, strict=True) if t > watermark]
        if not fresh:
            return total
        hourly, states, watermark = process_page(fresh, states, ledger)
        ledger.record(robot_id, arm, watermark, hourly, states)
        total += len(fresh)
        log.info("%s %s: %d rows, up to %s", robot_id, arm, total, _format_us(watermark))
        if len(rows) < page:
            return total


def sync(client: ApiClient, ledger: Ledger) -> SyncResult:
    """Bring the ledger up to date with every robot's arms."""
    result = SyncResult()
    for robot in client.robots():
        robot_id = robot["robot_id"]
        for arm in ARMS:
            rows = sync_arm(client, ledger, robot_id, arm)
            if rows:
                result.rows += rows
                result.arms.add((robot_id, arm))
    return result
