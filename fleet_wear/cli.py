"""Command line: ``rtdaas-fleet-wear sync|report|serve``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from fleet_wear.client import ApiClient
from fleet_wear.fleet import METRICS, enrich, load_ratings
from fleet_wear.ledger import DEFAULT_PATH, Ledger, SettingsMismatch
from fleet_wear.server import DEFAULT_PORT, create_app
from fleet_wear.sync import sync
from fleet_wear.wear import (
    DEFAULT_DEADBAND_M,
    DEFAULT_DEADBAND_RAD,
    DEFAULT_EXPONENT,
    DEFAULT_MAX_GAP_S,
    Settings,
)


def _ledger(args) -> Ledger:
    given = (args.deadband_rad, args.deadband_m, args.max_gap, args.exponent)
    settings = None
    if any(v is not None for v in given):
        settings = Settings(
            args.deadband_rad if args.deadband_rad is not None else DEFAULT_DEADBAND_RAD,
            args.deadband_m if args.deadband_m is not None else DEFAULT_DEADBAND_M,
            args.max_gap if args.max_gap is not None else DEFAULT_MAX_GAP_S,
            args.exponent if args.exponent is not None else DEFAULT_EXPONENT,
        )
    return Ledger(args.db, settings)


def _cmd_sync(args) -> int:
    result = sync(ApiClient(args.api), _ledger(args))
    print(f"read {result.rows} new rows from {len(result.arms)} arm(s)")
    return 0


def _cmd_report(args) -> int:
    ledger = _ledger(args)
    table = enrich(ledger.totals(), ledger.settings.exponent, load_ratings(args.ratings))
    if table.empty:
        print("The ledger is empty; run sync first.", file=sys.stderr)
        return 1
    table = table.sort_values(f"{args.metric}_index", ascending=False)
    columns = ["robot_id", "arm", "joint", "unit", "travel", "reversals", "work", "damage",
               "equivalent_load", "load_coverage", f"{args.metric}_index"]  # fmt: skip
    if args.ratings:
        columns.append("life_used")
    with pd.option_context("display.width", 200, "display.max_rows", None):
        print(table[columns].head(args.limit).round(4).to_string(index=False))
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    client = ApiClient(args.api)
    ledger = _ledger(args)
    app = create_app(client, ledger, load_ratings(args.ratings), args.refresh)
    print(f"reading {client.base_url}; open http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rtdaas-fleet-wear", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--api", help="data API root; default $RTDAAS_API_URL, then http://localhost:29398"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_PATH, help="the ledger file")
    parser.add_argument("--deadband-rad", type=float, help=f"default {DEFAULT_DEADBAND_RAD}")
    parser.add_argument("--deadband-m", type=float, help=f"default {DEFAULT_DEADBAND_M}")
    parser.add_argument("--max-gap", type=float, help=f"seconds; default {DEFAULT_MAX_GAP_S}")
    parser.add_argument(
        "--exponent", type=float, help=f"damage exponent; default {DEFAULT_EXPONENT}"
    )
    parser.add_argument("--ratings", type=Path, help="optional JSON of rated loads and travel")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="read new joint states into the ledger")
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser("report", help="print joints, most worn first")
    p.add_argument("--metric", choices=sorted(METRICS), default="damage")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("serve", help="the dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--refresh", type=float, default=300.0, help="seconds between syncs; 0 for never"
    )
    p.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except SettingsMismatch as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
