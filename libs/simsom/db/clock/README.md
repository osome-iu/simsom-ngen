<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# `simsom_clock`

Installable PostgreSQL extension for the one SimSoM column that needs a strict
globally ordered simulator timestamp.

The extension exposes one hot-path allocator:

- `simsom_clock_nextval() -> bigint`
- `simsom_clock_stamp_timestamp() -> trigger`

It returns the next `timestamp` tick in logical milliseconds.

## Clock Algorithm

The clock keeps one singleton row in `simsom_clock_state`.

Each call to `simsom_clock_nextval()`:

- locks that singleton row
- reads the current logical tick and clock parameters
- samples a positive delta in C
- adds the delta to `current_tick_ms`
- writes the new value back
- returns the new tick

Today the distribution is exponential and mirrors the Python model:

```python
delta = max(int(rng.exponential(1 / lambda_)), 1)
curr_timestamp += delta
return curr_timestamp
```

Calibration:

- `slope = 9.586385781407468e-08`
- `intercept = 3.907543315063294e-05`
- `lambda = slope * num_users + intercept`

`num_users` affects spacing, not ordering. More users means larger `lambda`,
which means smaller average delays between events.

## Timestamp Semantics

`timestamp` is not wall-clock time.

It means:

- a logical simulator timestamp
- stored as `bigint`
- interpreted as milliseconds since a configured origin timestamp

So if the origin is `2020-01-01 00:00:00` and `timestamp = 2500`, the row
represents simulated time `2020-01-01 00:00:02.500`.

The stored column stays as `bigint` on purpose:

- ordering is exact and unambiguous
- it matches the Python model directly
- it avoids floating-point drift in persisted rows

Use `simsom_clock_tick_to_timestamp(...)` when you want to render a logical tick
as a SQL timestamp for reads.

## Why The Clock Uses One Shared State Row

Strict global ordering means every insert must allocate its next simulator tick
from one shared point.

The singleton state row is the explicit source of truth for that allocation.
An index cannot replace it:

- an index orders rows that already exist
- it does not hold mutable clock state
- it cannot apply arbitrary random positive increments safely under concurrency

If you require strict linear simulator time across concurrent inserts, some
serialized shared state is unavoidable.

## Current API

- `simsom_clock_configure_exponential(...)`
  Creates or resets the singleton clock with the calibrated exponential model.
- `simsom_clock_nextval()`
  Allocates the next simulator tick as `bigint`.
- `simsom_clock_stamp_timestamp()`
  `BEFORE INSERT` trigger helper that stamps `NEW.timestamp` server-side.
- `simsom_clock_current_tick_ms()`
  Returns the current logical tick.
- `simsom_clock_tick_to_timestamp(...)`
  Converts a tick to a SQL timestamp using the configured origin.
- `simsom_clock_current_timestamp()`
  Returns the current logical tick rendered as a SQL timestamp.
- `simsom_clock_reset(...)`
  Resets `current_tick_ms` back to `0`.

## Runtime Integration

The simulator runtime wires this extension through the controller-owned schema
setup path in `libs/simsom/db/controller.py`.

The clock extension is required for simulator runs:

- the controller creates the configured Postgres schema
- it drops and recreates `simsom_clock` in the configured simulator schema
- it creates the shared `action` table with
  `timestamp bigint not null default <clock_schema>.simsom_clock_nextval()`
- it configures the exponential clock from the initial user count during
  initial-state population
- worker DB writes omit `timestamp`, so Postgres remains the only owner of
  simulator timestamp allocation

The trigger helper remains available for tables that need to override
caller-provided `timestamp` values, but the main runtime action table uses
the default-based path.

## Where C Is Used

The extension is packaged through SQL, while the hot logic lives in
`libs/simsom/db/clock/simsom_clock.c`:

- `simsom_clock_exponential_lambda(...)`
- `simsom_clock_sample_delta(...)`
- `simsom_clock_nextval(...)`

Those functions are registered by `libs/simsom/db/clock/simsom_clock--1.0.sql`
when you run `CREATE EXTENSION simsom_clock;`.

## Benchmark Design

Benchmarks live under `analyses/research/psql_extension/benchmark`.

There are two comparison versions.

### Version 1

One benchmarked generated column: `timestamp`.

- native:
  `timestamp` uses a deterministic increment-by-1 allocator.
  This is the simple deterministic arithmetic baseline.
- default:
  `timestamp` uses `DEFAULT simsom_clock_nextval()`.
  This is the complex stochastic increment path.

This isolates the cost of generating one bigint column while contrasting
simple `+1` allocation with distribution-based allocation.

### Version 2

Two columns:

- `action_id bigint generated always as identity`
- `timestamp bigint`

Comparison cases:

- native:
  `action_id` is native identity and `timestamp` is a constant
- default:
  `action_id` is native identity and `timestamp` uses
  `DEFAULT simsom_clock_nextval()`
- explicit:
  `action_id` is native identity and `timestamp` comes from an explicit
  `simsom_clock_nextval()` call in the `INSERT`

This isolates the cost of adding the simulator-clock column while keeping row
identity native.

## How To Read The Benchmark Results

- `v1 default - v1 native`
  Cost of replacing native increment-by-1 generation with the extension clock
  for one column.
- `v2 default - v2 native`
  Cost of automatic clock allocation for `timestamp` when `action_id`
  remains native.
- `v2 explicit - v2 native`
  Cost of explicit clock allocation for `timestamp` when `action_id`
  remains native.
- `v2 explicit - v2 default`
  Constant-factor difference between query-owned and schema-owned use of the
  same clock function.

All of those clocked paths stay `O(n)` for `n` inserted rows. The meaningful
difference is constant-factor overhead, because the dominant cost is still the
serialized update of the singleton clock row.

## Build And Install

```bash
cd libs/simsom/db/clock
make
make install
```

Then in Postgres:

```sql
CREATE EXTENSION simsom_clock;
```

Default-based ownership:

```sql
CREATE TABLE action (
    action_id bigint generated always as identity primary key,
    timestamp bigint not null default simsom_clock_nextval()
);
```

Trigger-based ownership:

```sql
CREATE TABLE action (
    action_id bigint generated always as identity primary key,
    timestamp bigint not null
);

CREATE TRIGGER action_stamp_timestamp
BEFORE INSERT ON action
FOR EACH ROW
EXECUTE FUNCTION simsom_clock_stamp_timestamp();
```
