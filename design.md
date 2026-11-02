# Design

## Measures of wear

Each builds on the one before, and each is a standard engineering approximation:

| Measure | Formula | Basis |
|---|---|---|
| Total movement | `sum(|dq|)` after a dead band | Baseline: more movement, more wear |
| Direction changes | Count of reversals | Cables fatigue by bending back and forth over pulleys; the da Vinci's joints are cable-driven |
| Load × movement | `sum(|effort| * |dq|)` | Archard's wear law: sliding wear is proportional to load times distance. For a joint this is the energy passed through it, in joules |
| Bearing-life damage | `sum(|effort|^3 * |dq|)` | Rolling-bearing life: `L10 = (C / P)^3`. Doubling the load cuts life by eight, so a varying load is summed cubed (Palmgren-Miner) |

Bearing damage also gives an **equivalent load**: `(damage / travel)^(1/3)`, the steady load that would
do the same damage over the same travel. It reads in N·m, and shows when a joint with average travel has
been working unusually hard.

Your intuition holds: a joint moved under more load does wear more, and the two formulas differ in how
much more. Which is closer depends on what wears first. Bearings and gears follow the cubic rule more
closely, sliding contact follows the linear one, and cable fatigue depends on reversals and tension. The
dashboard therefore keeps all of them, rather than blending them into one number.

A load held still adds nothing to any of them, since nothing moves. Holding load does matter for some
failures (motor heating, for one), but that is not wear from movement.

## Comparing to peers, not to a rating

Nothing published says how far a dVRK joint should last, and made-up ratings would look authoritative.
So the headline number is an **index**: a joint's value over the median of the same joint on arms of the
same kind. A PSM's pitch joint is only compared with other PSMs' pitch joints. 1.0 means typical for the
fleet, and 3.0 is worth a look. With a ratings file, it also shows life used.

Comparing against the fleet's median has limits. It says nothing if every robot is worn, and with a small
fleet the median itself moves. Joints that have moved less than a full turn (10 cm for insertion) get no
index, since ratios of tiny numbers look alarming and mean nothing.

## Why not sum raw differences

Summing `|q[t] - q[t-1]|` counts sensor noise as movement, so a joint at rest would accumulate distance
forever. In the tests, a still joint with 0.1 mrad of noise accumulates over 5 rad of raw "movement"
and none through the dead band. On simulated motion, the dead band changes the total by 2-4 %.

Undersampling works the other way: it hides reversals and cuts corners. So the tool reads every sample,
never a downsampled view. Movement during a break in the data (over a second by default) is not
guessed at, so totals are lower bounds, and the page reports breaks.

## Effort data may not mean anything

`effort` can look like data without being load. All zeros come from a kinematic simulation; and until
2026-11-02, `rtdaas-sim` wrote white noise there. Scoring noise would produce confident, meaningless wear
numbers. So each page of each joint's data is checked: real load either has a steady part (gravity) or
follows motion (friction). Movement with effort that does neither is still counted as travel, but scores
no load, and the page says what share of movement had usable load data.

The simulator now produces effort from a sketch of gravity and friction, scaled per robot and arm.
That is a model, not a measurement. On a real dVRK, effort comes from motor current: it includes gravity
and friction, but is not force at the bearing or tension in the cable. Sensorless readings like this
are good enough to compare one joint with another of the same kind, which is the only comparison made
here.

## Totals across reads

Reads move forward from a watermark per robot and arm, by time rather than page offset, so rows arriving
during a read cannot shift the pages. Each page commits its usage, the dead band's state and the watermark
in one transaction, so an interrupted read resumes without counting anything twice. Tests check that
reading in one go, in small pages or in several sittings gives the same totals.

Usage is stored per hour, not per session: not every row belongs to a session yet, as sessions come from
batch runs and the stream writes rows sooner. The cost is that rows arriving late, with a timestamp before
the watermark, are missed. A batch backfill of an old gap would be one. Deleting the ledger and reading
again recounts everything.

## How far to trust it

Checks run on the simulator's data (2026-11-02, 459,000 joint rows):

- Travel and reversals match known signals (four amplitudes per cycle of a sinusoid, to 2 %).
- Across 12 simulated robots, the equivalent load tracked each joint's known load scale with r >= 0.993.
  That shows the pipeline recovers a load that is in the data; since the simulator's effort is itself a
  simple model, it says nothing about real wear.

**None of these measures has been checked against a worn or failed joint**, because no such data exists
here. They rank joints by exposure. They do not predict failure, and a high index is a reason to inspect
a joint, not a finding that it is worn.

## Not done

- Resetting a joint's count when a part is replaced.
- Separating wrist joints by instrument. On a da Vinci the wrist is part of the instrument, which gets
  swapped, so wear on those joints belongs partly to the instrument.
- Using commanded position, or anything else not in `measured_js`.
