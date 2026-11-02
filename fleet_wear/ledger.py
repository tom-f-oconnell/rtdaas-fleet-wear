"""The wear ledger: a local SQLite file of joint usage per hour.

It keeps running totals independently of the data API, so they survive the lake's retention, and
each read continues where the last stopped.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, astuple
from pathlib import Path

import pandas as pd

from fleet_wear.wear import FilterState, Settings, Usage

DEFAULT_PATH = Path("data/wear.sqlite")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
    robot_id TEXT NOT NULL, arm TEXT NOT NULL, joint TEXT NOT NULL, hour TEXT NOT NULL,
    n_samples INTEGER NOT NULL, observed_s REAL NOT NULL, travel REAL NOT NULL,
    reversals INTEGER NOT NULL, gaps INTEGER NOT NULL, travel_loaded REAL NOT NULL,
    travel_noise REAL NOT NULL, travel_absent REAL NOT NULL, work REAL NOT NULL,
    damage REAL NOT NULL, peak_effort REAL NOT NULL,
    PRIMARY KEY (robot_id, arm, joint, hour)
);
CREATE TABLE IF NOT EXISTS progress (
    robot_id TEXT NOT NULL, arm TEXT NOT NULL, watermark_us INTEGER NOT NULL,
    PRIMARY KEY (robot_id, arm)
);
CREATE TABLE IF NOT EXISTS filter_state (
    robot_id TEXT NOT NULL, arm TEXT NOT NULL, joint TEXT NOT NULL,
    reference REAL, direction INTEGER NOT NULL, last_us INTEGER,
    PRIMARY KEY (robot_id, arm, joint)
);
"""
_USAGE_COLUMNS = tuple(Usage.__dataclass_fields__)
_SUM_COLUMNS = tuple(c for c in _USAGE_COLUMNS if c != "peak_effort")


class SettingsMismatch(ValueError):
    """The ledger was computed with other settings, which cannot be mixed in one total."""


class Ledger:
    """Joint usage per hour.

    Args:
        path: The SQLite file.
        settings: What the totals are computed with. Raises :class:`SettingsMismatch` if the file
            was started with different ones.
    """

    def __init__(self, path: Path = DEFAULT_PATH, settings: Settings | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'settings'").fetchone()
            if row is None:
                self.settings = settings or Settings()
                db.execute(
                    "INSERT INTO meta VALUES ('settings', ?)", (json.dumps(asdict(self.settings)),)
                )
            else:
                stored = Settings(**json.loads(row[0]))
                if settings is not None and settings != stored:
                    raise SettingsMismatch(
                        f"{self.path} was computed with {stored}; delete it to recompute with "
                        f"{settings}"
                    )
                self.settings = stored

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30.0)
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def watermark(self, robot_id: str, arm: str) -> int | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT watermark_us FROM progress WHERE robot_id = ? AND arm = ?", (robot_id, arm)
            ).fetchone()
        return row[0] if row else None

    def filter_state(self, robot_id: str, arm: str) -> dict[str, FilterState]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT joint, reference, direction, last_us FROM filter_state "
                "WHERE robot_id = ? AND arm = ?",
                (robot_id, arm),
            ).fetchall()
        return {joint: FilterState(ref, direction, last) for joint, ref, direction, last in rows}

    def record(
        self,
        robot_id: str,
        arm: str,
        watermark_us: int,
        hourly: dict[tuple[str, str], Usage],
        states: dict[str, FilterState],
    ) -> None:
        """Add a read's usage and move on, all at once: a crash cannot count anything twice.

        Args:
            robot_id: The robot.
            arm: The arm.
            watermark_us: The last sample time this read covered.
            hourly: Usage by ``(joint, hour)``.
            states: Each joint's dead band, to continue from.
        """
        columns = ", ".join(_USAGE_COLUMNS)
        marks = ", ".join("?" for _ in _USAGE_COLUMNS)
        add = ", ".join(f"{c} = {c} + excluded.{c}" for c in _SUM_COLUMNS)
        sql = (
            f"INSERT INTO usage (robot_id, arm, joint, hour, {columns}) "
            f"VALUES (?, ?, ?, ?, {marks}) "
            f"ON CONFLICT (robot_id, arm, joint, hour) DO UPDATE SET {add}, "
            "peak_effort = MAX(peak_effort, excluded.peak_effort)"
        )
        with self._connect() as db:
            for (joint, hour), usage in hourly.items():
                db.execute(sql, (robot_id, arm, joint, hour, *astuple(usage)))
            db.executemany(
                "INSERT OR REPLACE INTO filter_state VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (robot_id, arm, joint, s.reference, s.direction, s.last_us)
                    for joint, s in states.items()
                ],
            )
            db.execute(
                "INSERT OR REPLACE INTO progress VALUES (?, ?, ?)", (robot_id, arm, watermark_us)
            )

    def usage(self) -> pd.DataFrame:
        """Every hourly row."""
        with self._connect() as db:
            return pd.read_sql_query("SELECT * FROM usage ORDER BY hour", db)

    def totals(self) -> pd.DataFrame:
        """All-time totals per robot, arm and joint."""
        sums = ", ".join(f"SUM({c}) AS {c}" for c in _SUM_COLUMNS)
        with self._connect() as db:
            return pd.read_sql_query(
                f"SELECT robot_id, arm, joint, {sums}, MAX(peak_effort) AS peak_effort, "
                "MIN(hour) AS first_hour, MAX(hour) AS last_hour FROM usage "
                "GROUP BY robot_id, arm, joint ORDER BY robot_id, arm, joint",
                db,
            )
