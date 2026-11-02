# rtdaas-fleet-wear

A dashboard of every robot in a fleet, showing how far each joint has moved and how worn it is likely
to be compared with the same joint on other robots. It reads joint states from
[robot-telemetry-daas](https://github.com/tom-f-oconnell/robot-telemetry-daas) and keeps its own running
totals. Where effort data exists, it weights movement by load, because moving under load wears a joint
more.

## Use

You need Python 3.11 or newer, and robot-telemetry-daas running at version `0f874ee` or later, which
added the route this reads.

```bash
python3 -m venv venv
venv/bin/pip install -e .

venv/bin/rtdaas-fleet-wear serve    # then open http://127.0.0.1:29346
```

It reads everything new when it starts, then every 5 minutes. The first read of a long history takes a
while; the page fills in when you press "Read new data" after it has finished. Totals are kept in
`data/wear.sqlite`, so they survive restarts and outlive the data in the lake.

The charts load Plotly from a CDN, so they need internet access. Without it, the table still works.

## Options

The API defaults to `http://localhost:29398`. Options go before the command:

| Option | Default | Meaning |
|---|---|---|
| `--api URL` or `RTDAAS_API_URL` | `http://localhost:29398` | The data API |
| `--db FILE` | `data/wear.sqlite` | Where totals are kept |
| `--deadband-rad`, `--deadband-m` | 0.001, 0.0001 | Movement smaller than this does not count until it accumulates |
| `--max-gap S` | 1 | A longer break in the data resets the count, rather than assuming what happened in it |
| `--exponent P` | 3 | Damage exponent: 3 for ball bearings, 10/3 for roller bearings |
| `--ratings FILE` | none | Rated loads and travel, to show the share of life used |

A ledger keeps the settings it started with. To change them, delete the file and read the data again.

```bash
rtdaas-fleet-wear sync                                  # read new data once
rtdaas-fleet-wear report --metric damage --limit 20     # most worn first, in the terminal
rtdaas-fleet-wear serve --refresh 60 --port 29346
```

Nothing published says how far a dVRK joint should last, so no ratings ship with this. If you have
them for your hardware, a ratings file adds a "Life used" column:

```json
{"PSM": {"outer_pitch": {"rated_load": 2.0, "rated_travel": 1000000}}}
```

## Development

```bash
python3 -m venv venv
venv/bin/pip install -e ".[dev]"
```

[design.md](design.md) explains the wear measures, the checks on effort data, how totals stay correct
across reads, and what these numbers cannot tell you.

```bash
venv/bin/pytest
venv/bin/ruff check . && venv/bin/ruff format --check .
RTDAAS_API_URL=http://localhost:29398 venv/bin/pytest -m integration   # against a running API
```
