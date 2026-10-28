<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Clock Benchmarks

These benchmarks study DB-owned action ordering where workers insert payload
only and PostgreSQL assigns both:

- `action_id`
- `timestamp`

The benchmark-local C extension lives under `benchmark/extension` so it does
not modify `libs/simsom/db/clock`.

## Cases

Every benchmark version compares the same three cases.

- `native`
  PostgreSQL owns both fields using built-in features:
  `action_id` is identity and `timestamp` is a stored generated copy of
  `action_id`.
- `simple_math`
  Benchmark-local C trigger allocates both fields atomically with fixed step
  `+1`.
- `exponential`
  Benchmark-local C trigger allocates both fields atomically with exponential
  timestamp deltas and minimum step `1`.

## Version 1

Single connection, one row inserted per statement.

This tells you:

- cost of built-in DB-owned assignment
- extra cost of a custom shared-state C allocator
- extra cost of exponential sampling over fixed-step sampling
- single-writer behavior without multi-client contention

## Version 2

Concurrent clients, each inserting one row per statement.

Defaults:

- `4` clients
- same `total_rows` as `v1`, divided evenly across clients
- `batch_size = 1`
- `3` runs

This tells you how the same three designs behave when multiple writers contend
for the DB-owned ordering path.

## Version 3

Concurrent clients, exponential clock only, with a sweep over insert batch
size and client count while keeping total inserted rows fixed.

Defaults:

- `total_rows = 1000000`
- `clients = 4, 8`
- `batch_size = 1, 10, 100, 1000, 10000`
- `3` runs per sweep point

Rows are distributed across clients as evenly as possible. When `total_rows`
is not divisible by the client count, the first few workers insert one extra
row.

This isolates how the current `v2` exponential path behaves as:

- the number of rows per insert statement changes
- the number of concurrent clients changes

while total work stays fixed for fair runtime comparisons.

## Running

Run both versions:

```bash
bash analyses/research/psql_extension/benchmark/run_local_benchmarks.sh
```

Run only the concurrent benchmark:

```bash
bash analyses/research/psql_extension/benchmark/run_local_benchmarks.sh --version v2
```

Run only the new exponential sweep:

```bash
bash analyses/research/psql_extension/benchmark/run_local_benchmarks.sh --version v3
```

Override defaults:

```bash
bash analyses/research/psql_extension/benchmark/run_local_benchmarks.sh \
  --version all \
  --runs 5 \
  --total-rows 10000 \
  --clients 4 \
  --batch-size 1 \
  --v3-total-rows 1000000 \
  --v3-clients 4,8 \
  --v3-batch-sizes 1,10,100,1000,10000
```

The runner writes:

- a timestamped full log under `benchmark/results/`
- a timestamped TSV summary under `benchmark/results/`
