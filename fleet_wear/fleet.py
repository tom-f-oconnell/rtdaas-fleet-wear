"""Turn the ledger's totals into what the dashboard shows."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from fleet_wear.ledger import Ledger
from fleet_wear.wear import equivalent_load, life_used, unit_of

METRICS: dict[str, dict] = {
    "travel": {
        "label": "Total movement",
        "needs_load": False,
        "explain": "How far the joint has moved, ignoring sensor noise.",
    },
    "reversals": {
        "label": "Direction changes",
        "needs_load": False,
        "explain": "How often it changed direction. Cables fatigue by bending back and forth.",
    },
    "work": {
        "label": "Load × movement",
        "needs_load": True,
        "explain": "Effort times movement (Archard's wear law): sliding wear grows with both.",
    },
    "damage": {
        "label": "Bearing-life damage",
        "needs_load": True,
        "explain": "Effort cubed times movement, as bearing life is rated: doubling the load "
        "counts eight times as much.",
    },
}
LOW_COVERAGE = 0.9
MIN_TRAVEL_TO_COMPARE = {"rad": 2 * np.pi, "m": 0.1}  # a full turn, or 10 cm


def arm_type(arm: str) -> str:
    """Arms of one kind have comparable joints: PSM1 and PSM2, but not PSM1 and ECM."""
    return arm[:3]


def load_ratings(path: Path | None) -> dict:
    """Optional ``{arm_type: {joint: {"rated_load": N, "rated_travel": N}}}``."""
    return json.loads(Path(path).read_text()) if path else {}


def enrich(totals: pd.DataFrame, exponent: float, ratings: dict | None = None) -> pd.DataFrame:
    """Add units, coverage, equivalent load, indices against the fleet, and any life used.

    Each index is the joint's value over the median of the same joint on arms of the same kind, so
    1.0 means typical for the fleet. Comparing to its peers is what makes a number useful without
    a rating: nothing published says how far a dVRK joint should last. A joint that has hardly
    moved gets no index, because ratios of tiny numbers look alarming and mean nothing.
    """
    if totals.empty:
        return totals.assign(unit=[], load_coverage=[], equivalent_load=[], life_used=[])
    out = totals.copy()
    out["arm_type"] = out["arm"].map(arm_type)
    out["unit"] = out["joint"].map(unit_of)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["load_coverage"] = np.where(
            out["travel"] > 0, out["travel_loaded"] / out["travel"], np.nan
        )
    out["equivalent_load"] = [
        equivalent_load(d, t, exponent)
        for d, t in zip(out["damage"], out["travel_loaded"], strict=True)
    ]
    out["comparable"] = out["travel"] >= out["unit"].map(MIN_TRAVEL_TO_COMPARE)
    for metric, spec in METRICS.items():
        values = out[metric].astype(float)
        if spec["needs_load"]:
            values = values.where(out["travel_loaded"] > 0)
        out[metric] = values
        values = values.where(out["comparable"])
        medians = values.groupby([out["arm_type"], out["joint"]]).transform("median")
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"{metric}_index"] = np.where(medians > 0, values / medians, np.nan)
    rated = []
    for row in out.itertuples():
        rating = (ratings or {}).get(row.arm_type, {}).get(row.joint)
        used = np.nan
        if rating and row.travel_loaded > 0:
            used = life_used(row.damage, rating["rated_load"], rating["rated_travel"], exponent)
        rated.append(used)
    out["life_used"] = rated
    return out


def _json_value(value):
    if value is None:
        return None
    if isinstance(value, (float, np.floating)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    return value


def records(frame: pd.DataFrame) -> list[dict]:
    """Rows as JSON-ready dicts, with NaN as null."""
    return [{k: _json_value(v) for k, v in row.items()} for row in frame.to_dict("records")]


def fleet_view(ledger: Ledger, ratings: dict | None = None) -> dict:
    """Everything the fleet page needs."""
    table = enrich(ledger.totals(), ledger.settings.exponent, ratings)
    if table.empty:
        return {
            "cells": [],
            "summary": None,
            "metrics": METRICS,
            "exponent": ledger.settings.exponent,
        }
    robots = table.groupby("robot_id")
    # Per joint, so the most observed arm counts, not the sum across arms running at the same time.
    observed = table.groupby(["robot_id", "arm"])["observed_s"].max().groupby("robot_id").max()
    total_travel = float(table["travel"].sum())
    summary = {
        "robots": int(robots.ngroups),
        "arms": int(table.groupby(["robot_id", "arm"]).ngroups),
        "observed_hours": float(observed.sum() / 3600),
        "load_coverage": float(table["travel_loaded"].sum() / total_travel)
        if total_travel
        else None,
        "noise_share": float(table["travel_noise"].sum() / total_travel) if total_travel else 0.0,
        "absent_share": float(table["travel_absent"].sum() / total_travel) if total_travel else 0.0,
        "gaps_per_robot": int(table.groupby("robot_id")["gaps"].max().max()),
    }
    return {
        "cells": records(table),
        "summary": summary,
        "metrics": METRICS,
        "exponent": ledger.settings.exponent,
        "low_coverage": LOW_COVERAGE,
        "has_ratings": bool(ratings),
    }


def history_view(ledger: Ledger, robot_id: str, metric: str) -> dict:
    """Cumulative totals over time for one robot, per arm and joint."""
    usage = ledger.usage()
    usage = usage[usage["robot_id"] == robot_id]
    series: dict[str, dict[str, dict]] = {}
    for (joint, arm), part in usage.groupby(["joint", "arm"], sort=True):
        ordered = part.sort_values("hour")
        series.setdefault(joint, {})[arm] = {
            "t": ordered["hour"].tolist(),
            "y": [float(v) for v in ordered[metric].cumsum()],
        }
    return {
        "robot_id": robot_id,
        "metric": metric,
        "units": {joint: unit_of(joint) for joint in series},
        "series": series,
    }
