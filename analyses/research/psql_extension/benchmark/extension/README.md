<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# `benchmark_clock`

Benchmark-only PostgreSQL extension for measuring DB-owned event ordering.

It allocates two linked fields atomically:

- `action_id bigint`
- `timestamp bigint`

The extension is intentionally local to
`analyses/research/psql_extension/benchmark/extension` so benchmark experiments
do not affect `libs/simsom/db/clock`.

## Modes

- `fixed_step`
  Increments both `action_id` and `timestamp` by `1`.
- `exponential`
  Increments `action_id` by `1` and advances `timestamp` by an exponential
  delta with minimum step `1`.

## DB-Owned Assignment

Use the `benchmark_clock_assign_action_fields()` before-insert trigger when the
benchmark should reflect workers inserting payload only and the database owning
the ordering fields.
