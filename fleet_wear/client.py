"""Read-only client for the data API's joint states."""

from __future__ import annotations

import os

import httpx

DEFAULT_API_URL = "http://localhost:29398"
PAGE = 5000


def default_api_url() -> str:
    return os.environ.get("RTDAAS_API_URL", DEFAULT_API_URL)


class ApiClient:
    """The routes this tool reads.

    Args:
        base_url: API root, e.g. ``http://localhost:29398``.
        transport: Optional httpx transport, for tests.
    """

    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or default_api_url()).rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=120.0, transport=transport)

    def robots(self) -> list[dict]:
        response = self._http.get("/api/robots")
        response.raise_for_status()
        return response.json()["robots"]

    def joints_after(
        self, robot_id: str, arm: str, start: str | None, limit: int = PAGE
    ) -> list[dict]:
        """The next rows of one arm in time order, from ``start`` inclusive."""
        params = {"arm": arm, "limit": limit}
        if start:
            params["start"] = start
        response = self._http.get(f"/api/robots/{robot_id}/joints", params=params)
        response.raise_for_status()
        return response.json()["samples"]
