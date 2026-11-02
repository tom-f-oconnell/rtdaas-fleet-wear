from __future__ import annotations

import math
import os

import httpx
import numpy as np
import pytest
from starlette.testclient import TestClient

from fleet_wear.client import ApiClient
from fleet_wear.fleet import enrich, fleet_view, history_view
from fleet_wear.ledger import Ledger, SettingsMismatch
from fleet_wear.server import create_app
from fleet_wear.sync import _format_us, sync, sync_arm
from fleet_wear.wear import FilterState, Settings

JOINTS = ["outer_pitch", "outer_insertion"]
T0 = 1_761_984_000_000_000  # 2025-11-01T08:00:00Z, on the hour


def _rows(seconds=120.0, load=1.0, start_us=T0, amplitude=0.1, rate=50, noise_effort=False, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(int(seconds * rate)):
        t = i / rate
        q = amplitude * math.sin(2 * math.pi * 0.5 * t)
        v = amplitude * 2 * math.pi * 0.5 * math.cos(2 * math.pi * 0.5 * t)
        effort = float(rng.normal(0, 0.05)) if noise_effort else load
        ts = _format_us(start_us + i * 1_000_000 // rate)
        out.append(
            {
                "arm": "PSM1",
                "ts": ts,
                "recv_ts": ts,
                "joint_names": JOINTS,
                "position": [q, q * 0.5],
                "velocity": [v, v * 0.5],
                "effort": [effort, effort * 2],
            }
        )
    return out


class FakeApi:
    """The joints route as the real API behaves: time order, ``start`` inclusive, ``limit``."""

    def __init__(self, data: dict[tuple[str, str], list[dict]]):
        self.data = data
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, request.url.params
        if path == "/api/robots":
            robots = sorted({robot for robot, _ in self.data})
            return httpx.Response(200, json={"robots": [{"robot_id": r} for r in robots]})
        self.requests += 1
        robot = path.split("/")[3]
        rows = self.data.get((robot, q["arm"]), [])
        if "start" in q:
            rows = [r for r in rows if r["ts"] >= q["start"]]
        rows = rows[: int(q["limit"])]
        return httpx.Response(200, json={"samples": rows, "count": len(rows)})


def _client(data):
    api = FakeApi(data)
    return ApiClient("http://api.test", transport=httpx.MockTransport(api)), api


def test_sync_totals_match_the_signal(tmp_path):
    client, _ = _client({("dv_001", "PSM1"): _rows(seconds=120.0, load=2.0)})
    ledger = Ledger(tmp_path / "w.sqlite")
    assert sync(client, ledger).rows == 6000
    totals = ledger.totals().set_index("joint")
    assert totals.loc["outer_pitch", "travel"] == pytest.approx(4 * 0.1 * 60, rel=0.02)
    assert totals.loc["outer_insertion", "travel"] == pytest.approx(4 * 0.05 * 60, rel=0.02)
    assert totals.loc["outer_pitch", "damage"] == pytest.approx(
        8 * totals.loc["outer_pitch", "travel"], rel=1e-3
    )
    assert totals.loc["outer_insertion", "damage"] == pytest.approx(
        64 * totals.loc["outer_insertion", "travel"], rel=1e-3
    )


def test_syncing_twice_counts_nothing_twice(tmp_path):
    client, _ = _client({("dv_001", "PSM1"): _rows()})
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    before = ledger.totals()
    assert sync(client, ledger).rows == 0
    assert ledger.totals().equals(before)


def test_new_data_continues_the_same_totals(tmp_path):
    rows = _rows(seconds=100.0)
    whole, _ = _client({("dv_001", "PSM1"): rows})
    one_go = Ledger(tmp_path / "a.sqlite")
    sync(whole, one_go)

    stored: dict = {("dv_001", "PSM1"): rows[:1234]}
    partial, _ = _client(stored)
    stepwise = Ledger(tmp_path / "b.sqlite")
    sync(partial, stepwise)
    stored[("dv_001", "PSM1")] = rows[:3000]
    sync(partial, stepwise)
    stored[("dv_001", "PSM1")] = rows
    sync(partial, stepwise)
    a, b = one_go.totals(), stepwise.totals()
    np.testing.assert_allclose(a["travel"], b["travel"])
    np.testing.assert_array_equal(a["reversals"], b["reversals"])
    np.testing.assert_array_equal(a["n_samples"], b["n_samples"])


def test_a_small_page_size_gives_the_same_totals(tmp_path):
    rows = _rows(seconds=30.0)
    client, _ = _client({("dv_001", "PSM1"): rows})
    big, small = Ledger(tmp_path / "a.sqlite"), Ledger(tmp_path / "b.sqlite")
    sync_arm(client, big, "dv_001", "PSM1", page=5000)
    sync_arm(client, small, "dv_001", "PSM1", page=100)
    np.testing.assert_allclose(big.totals()["travel"], small.totals()["travel"])
    assert big.totals()["n_samples"].tolist() == small.totals()["n_samples"].tolist()


def test_usage_is_split_by_hour_without_losing_any(tmp_path):
    start = T0 + 3_540_000_000  # 08:59:00, so two minutes crosses into 09:00
    client, _ = _client({("dv_001", "PSM1"): _rows(seconds=120.0, start_us=start)})
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    usage = ledger.usage()
    pitch = usage[usage["joint"] == "outer_pitch"]
    assert len(pitch) == 2
    assert pitch["n_samples"].sum() == 6000
    assert pitch["travel"].sum() == pytest.approx(4 * 0.1 * 60, rel=0.02)


def test_effort_that_is_only_noise_scores_no_load_but_keeps_travel(tmp_path):
    client, _ = _client({("dv_001", "PSM1"): _rows(noise_effort=True)})
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    table = enrich(ledger.totals(), 3.0)
    assert (table["travel"] > 0).all()
    assert (table["load_coverage"] == 0).all()
    assert table["work"].isna().all() and table["damage"].isna().all()


def test_a_ledger_refuses_other_settings(tmp_path):
    path = tmp_path / "w.sqlite"
    Ledger(path, Settings(exponent=3.0))
    assert Ledger(path, Settings(exponent=3.0)).settings.exponent == 3.0
    assert Ledger(path).settings.exponent == 3.0
    with pytest.raises(SettingsMismatch, match="delete it"):
        Ledger(path, Settings(exponent=10 / 3))


def test_a_crash_between_reads_loses_nothing(tmp_path):
    """Usage, dead band and watermark commit together; the next run continues from them."""
    path = tmp_path / "w.sqlite"
    rows = _rows(seconds=10.0)
    client, _ = _client({("dv_001", "PSM1"): rows})
    sync_arm(client, Ledger(path), "dv_001", "PSM1", page=100)
    reopened = Ledger(path)
    assert isinstance(reopened.filter_state("dv_001", "PSM1")["outer_pitch"], FilterState)
    assert reopened.watermark("dv_001", "PSM1") is not None


def test_indices_compare_a_joint_with_the_same_joint_on_arms_of_the_same_kind(tmp_path):
    data = {
        ("dv_001", "PSM1"): _rows(load=1.0),
        ("dv_002", "PSM1"): _rows(load=1.0),
        ("dv_003", "PSM1"): _rows(load=2.0),
    }
    client, _ = _client(data)
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    table = enrich(ledger.totals(), 3.0).set_index(["robot_id", "joint"])
    assert table.loc[("dv_001", "outer_pitch"), "damage_index"] == pytest.approx(1.0)
    assert table.loc[("dv_003", "outer_pitch"), "damage_index"] == pytest.approx(8.0, rel=0.01)
    assert table.loc[("dv_003", "outer_pitch"), "work_index"] == pytest.approx(2.0, rel=0.01)
    assert table.loc[("dv_003", "outer_pitch"), "travel_index"] == pytest.approx(1.0, rel=0.01)
    assert table.loc[("dv_003", "outer_pitch"), "equivalent_load"] == pytest.approx(2.0, rel=0.01)


def test_a_joint_that_has_hardly_moved_gets_no_index(tmp_path):
    data = {
        ("dv_001", "PSM1"): _rows(load=1.0),
        ("dv_002", "PSM1"): _rows(load=1.0, amplitude=0.001),
    }
    client, _ = _client(data)
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    table = enrich(ledger.totals(), 3.0).set_index(["robot_id", "joint"])
    assert not table.loc[("dv_002", "outer_pitch"), "comparable"]
    assert np.isnan(table.loc[("dv_002", "outer_pitch"), "damage_index"])
    assert table.loc[("dv_001", "outer_pitch"), "damage_index"] == pytest.approx(1.0)
    assert table.loc[("dv_002", "outer_pitch"), "travel"] > 0  # still on the ledger


def test_life_used_needs_a_rating(tmp_path):
    client, _ = _client({("dv_001", "PSM1"): _rows(load=2.0)})
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    ratings = {"PSM": {"outer_pitch": {"rated_load": 2.0, "rated_travel": 240.0}}}
    table = enrich(ledger.totals(), 3.0, ratings).set_index("joint")
    assert table.loc["outer_pitch", "life_used"] == pytest.approx(0.1, rel=0.02)
    assert np.isnan(table.loc["outer_insertion", "life_used"])


@pytest.fixture
def app_client(tmp_path):
    client, _ = _client({("dv_001", "PSM1"): _rows(load=1.0), ("dv_002", "PSM1"): _rows(load=2.0)})
    ledger = Ledger(tmp_path / "w.sqlite")
    with TestClient(create_app(client, ledger)) as c:
        c.ledger = ledger
        c.upstream = client
        yield c


def test_an_empty_ledger_is_a_page_not_an_error(app_client):
    body = app_client.get("/app/fleet").json()
    assert body["cells"] == [] and body["summary"] is None


def test_fleet_and_history_routes(app_client):
    sync(app_client.upstream, app_client.ledger)
    body = app_client.get("/app/fleet").json()
    assert body["summary"]["robots"] == 2 and body["summary"]["load_coverage"] == pytest.approx(1.0)
    cell = next(
        c for c in body["cells"] if c["robot_id"] == "dv_002" and c["joint"] == "outer_pitch"
    )
    assert cell["damage_index"] > 1 and cell["unit"] == "rad"
    history = app_client.get(
        "/app/history", params={"robot_id": "dv_001", "metric": "travel"}
    ).json()
    line = history["series"]["outer_pitch"]["PSM1"]
    assert line["y"] == sorted(line["y"]) and history["units"]["outer_insertion"] == "m"
    assert app_client.get("/app/history").status_code == 400
    assert (
        app_client.get("/app/history", params={"robot_id": "x", "metric": "no"}).status_code == 400
    )


def test_no_load_data_is_null_in_json_not_nan(tmp_path):
    client, _ = _client({("dv_001", "PSM1"): _rows(noise_effort=True)})
    ledger = Ledger(tmp_path / "w.sqlite")
    sync(client, ledger)
    view = fleet_view(ledger)
    assert all(c["damage"] is None and c["damage_index"] is None for c in view["cells"])
    assert view["summary"]["noise_share"] == pytest.approx(1.0)
    with TestClient(create_app(client, ledger)) as c:
        assert c.get("/app/fleet").status_code == 200  # NaN would have failed to encode


def test_history_of_an_unknown_robot_is_empty(tmp_path):
    assert history_view(Ledger(tmp_path / "w.sqlite"), "nobody", "travel")["series"] == {}


def test_sync_route_runs_in_the_background_and_reports(app_client):
    assert app_client.post("/app/sync").status_code == 202
    syncer = app_client.app.state.syncer
    for _ in range(200):
        if syncer.last_finished:
            break
        import time

        time.sleep(0.05)
    status = app_client.get("/app/status").json()
    assert status["last_error"] is None and status["last_rows"] == 12000


def test_a_failing_api_is_reported_and_the_ledger_stays_usable(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    client = ApiClient("http://api.test", transport=httpx.MockTransport(refuse))
    with TestClient(create_app(client, Ledger(tmp_path / "w.sqlite"))) as c:
        c.app.state.syncer.run_once()
        assert "ConnectError" in c.get("/app/status").json()["last_error"]
        assert c.get("/app/fleet").status_code == 200


def test_ui_is_served(app_client):
    assert "Robot fleet" in app_client.get("/").text
    assert app_client.get("/static/app.js").status_code == 200


@pytest.mark.integration
@pytest.mark.skipif("RTDAAS_API_URL" not in os.environ, reason="needs RTDAAS_API_URL")
def test_against_a_running_api(tmp_path):
    client = ApiClient()
    robot = client.robots()[0]["robot_id"]
    rows = client.joints_after(robot, "PSM1", None, limit=2000)
    assert rows and {"ts", "joint_names", "position", "velocity", "effort"} <= set(rows[0])
