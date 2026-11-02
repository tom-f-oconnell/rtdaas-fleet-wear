"""The dashboard, and the background thread that keeps the ledger current."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from fleet_wear.client import ApiClient
from fleet_wear.fleet import METRICS, fleet_view, history_view
from fleet_wear.ledger import Ledger
from fleet_wear.sync import sync

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 29346


class Syncer:
    """Runs one sync at a time, whether on a timer or on request."""

    def __init__(self, client: ApiClient, ledger: Ledger):
        self.client, self.ledger = client, ledger
        self._lock = threading.Lock()
        self.running = False
        self.last_finished: str | None = None
        self.last_rows = 0
        self.last_error: str | None = None

    def run_once(self) -> bool:
        """Sync now. Returns False if one is already running."""
        if not self._lock.acquire(blocking=False):
            return False
        self.running = True
        try:
            result = sync(self.client, self.ledger)
            self.last_rows = result.rows
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - the dashboard still has the ledger to show
            log.warning("sync failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.last_finished = datetime.now(UTC).isoformat(timespec="seconds")
            self.running = False
            self._lock.release()
        return True

    def start_once(self) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.run_once, daemon=True).start()
        return True

    def run_every(self, seconds: float) -> threading.Thread:
        def loop():
            while True:
                self.run_once()
                time.sleep(seconds)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread


def create_app(
    client: ApiClient, ledger: Ledger, ratings: dict | None = None, refresh_s: float = 0.0
) -> Starlette:
    """The app.

    Args:
        client: The data API to read.
        ledger: Where totals are kept.
        ratings: Optional ratings for life used, from :func:`fleet_wear.fleet.load_ratings`.
        refresh_s: Sync this often, in seconds; 0 syncs only on request.
    """
    syncer = Syncer(client, ledger)
    if refresh_s > 0:
        syncer.run_every(refresh_s)

    def fleet(request: Request):
        return JSONResponse(fleet_view(ledger, ratings))

    def history(request: Request):
        robot_id = request.query_params.get("robot_id")
        metric = request.query_params.get("metric", "travel")
        if not robot_id:
            return JSONResponse(
                {"error": "invalid_parameter", "detail": "robot_id is required"}, 400
            )
        if metric not in METRICS:
            return JSONResponse(
                {
                    "error": "invalid_parameter",
                    "detail": f"metric must be one of {sorted(METRICS)}",
                },
                400,
            )
        return JSONResponse(history_view(ledger, robot_id, metric))

    def status(request: Request):
        return JSONResponse(
            {
                "api_url": client.base_url,
                "running": syncer.running,
                "last_finished": syncer.last_finished,
                "last_rows": syncer.last_rows,
                "last_error": syncer.last_error,
                "refresh_s": refresh_s,
            }
        )

    async def start_sync(request: Request):
        return JSONResponse({"started": syncer.start_once()}, 202)

    async def index(request: Request):
        return FileResponse(STATIC / "index.html")

    async def on_upstream_error(request: Request, exc: httpx.HTTPError):
        return JSONResponse(
            {"error": "upstream_error", "detail": f"data API at {client.base_url}: {exc}"}, 502
        )

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/app/fleet", fleet),
            Route("/app/history", history),
            Route("/app/status", status),
            Route("/app/sync", start_sync, methods=["POST"]),
            Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
        ],
        exception_handlers={httpx.HTTPError: on_upstream_error},
    )
    app.state.syncer = syncer
    return app
