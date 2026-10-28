<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Analytics Algorithm

## Termination Inputs

`termination.criteria_functions` maps registered names to explicit inputs:
`max_avg_actions_per_user` requires a finite positive `threshold`;
`exponential_moving_average_action_quality` requires a finite positive
`threshold` and `rho` in `(0,1]`. Defaults 3.0, 0.01 and 0.8 preserve the earlier
presets. These are experiment/convergence controls with no empirical
calibration asserted. See [function configuration](configuration.md#function-inputs) for syntax.

Every configured criterion is evaluated in insertion order; the first matching
criterion supplies the reported reason and all results remain available.

Analytics is a separate process that measures the database state and controls
termination requests. The controller remains responsible for the authoritative
termination decision and shutdown coordination.

## Recurring Checks

- `analytics.evaluation_interval` defines when recurring Analytics work runs.
  Its `unit` is either `average_actions_per_user` or `simulated_hours`, and its
  positive `value` is the distance between checkpoints.
- The default is `unit: "simulated_hours"` with `value: 1`: one check per
  complete simulated hour.
- `average_actions_per_user` divides all persisted actions by all initialized
  users, including lurkers. For example, an interval of `0.1` is approximately
  one check per additional `0.1 * user_count` persisted actions.
- `simulated_hours` reads the current millisecond tick from `simsom_clock` and
  converts it to hours. It is simulator time, not operating-system wall time.
- The first recurring pass runs after the first complete interval. There is no
  pass at zero progress.
- One checkpoint controls the whole recurring pass: Analytics reads exact
  aggregate metrics, runs `analytics.recurring_functions`, and evaluates every
  configured `termination.criteria_functions` function.
- Each criterion reports its function name, whether it was met, its reason, and
  optional details.
- When one or more criteria are met, Analytics writes a termination request and
  the complete list of criteria met to the shared lifecycle row.
- Analytics logs each recurring termination check with its termination status,
  reason, and criterion results. Full metrics and report properties are
  reserved for the final report.
- Analytics records one lightweight summary row for each recurring check. These
  rows retain the termination inputs and met criteria; full report properties
  are written only by the final pass.

The process uses a two-level database read. Between checkpoints, it reads the
singleton simulator clock row and the indexed maximum `action.action_id`. When
that inexpensive watermark indicates a possible checkpoint, Analytics runs the
exact action-count and average-quality query. The exact count must confirm the
checkpoint before any recurring function or termination criterion is evaluated.
This keeps the common progress check from repeatedly scanning the full action
table. Progress is probed again as soon as the preceding database call returns;
there is no wall-clock delay in the recurring schedule.

If database progress crosses more than one interval between observations,
Analytics evaluates the latest exact snapshot once and advances past all
completed checkpoints. It does not apply stateful functions such as the EMA
multiple times to the same snapshot.

Example configurations:

```jsonc
"evaluation_interval": {
    "unit": "average_actions_per_user",
    "value": 0.1
}
```

```jsonc
"evaluation_interval": {
    "unit": "simulated_hours",
    "value": 6
}
```

Only one interval unit is selected for a run. A simulated-hour cadence also
controls termination evaluation; it does not add a second independent cadence.

The interval affects stateful termination functions such as the EMA. A smaller
value runs the EMA more often and can satisfy its stability threshold after
fewer persisted actions. A larger value runs it less often and can allow more
actions before it stabilizes. Therefore, action counts from runs should only be
compared when the interval unit and value are held constant. The interval does
not change the EMA formula or its configured `threshold` and `rho` values; it
changes how often the formula receives a new aggregate snapshot.

## Configurable Outputs

- `analytics.recurring_functions` maps registered names to explicit input
  objects in evaluation order. Each function receives the lightweight `AnalyticsMetrics` value
  used by the termination check.
- Recurring functions run at every confirmed progress checkpoint. Their results
  are keyed by selector name and written to one flushed JSON Lines (JSONL)
  record in `analytics.analytics_output_file`.
- `analytics.final_functions` is a separate ordered function-input mapping. These
  functions run once, after workers and policy have finished and Analytics has
  read the stable final database state.
- Final function results are written to one final JSONL record and persisted in
  the final `analytics_summary.other_properties.analytics_outputs` object.
- The two output files are separate. The termination metrics file contains
  only action count, average quality, average actions per user, termination
  status, reason, and criterion results. The analytics output file contains
  recurring and final function results plus the built-in record fields below.
- The termination metrics path is configured by
  `termination.termination_metrics_file`; the analytics-function output path
  is configured by `analytics.analytics_output_file`.
- Relative output paths are resolved below `system.log.path`. Both files are
  truncated at simulation start, created with missing parent directories, and
  flushed after every record so another process can read them during a run.
- Empty recurring and final groups (`{}`) are valid. An unknown selector or a
  function result that cannot be represented as JSON is an error.

Every analytics output record includes `action_count`, `avg_action_quality`,
and `avg_actions_per_user`, as well as `record_type`, `phase`, and `recorded_at`.
These built-in fields describe the snapshot used for that record. They are
written regardless of the configured function groups. Average actions per user
uses the total initialized population, including lurkers. These are descriptive
metrics, with no calibration parameters.

The `outputs` object contains only results from configured functions.
`"recurring_functions": {}` and `"final_functions": {}` select no additional
functions, so their records have `"outputs": {}` while retaining the built-in
fields. To select a registered function with no configurable inputs, use
`{"function_name": {}}`. The simulator still supplies its `AnalyticsMetrics`
runtime argument; the empty input object does not mean an empty metrics snapshot.
Functions with configurable inputs use the same mapping with those inputs
filled in. See [function inputs](configuration.md#function-inputs).

The average-quality criterion uses an Exponential Moving Average (EMA). It
updates quality as:

`new_quality = rho * current_quality + (1 - rho) * overall_avg_quality`

The relative change is:

`quality_diff = abs(new_quality - current_quality) / current_quality`

The criterion is met when `quality_diff` is at or below its configured
threshold. This is one termination criterion, not a separate lifecycle state.

Average actions per user uses the total initialized user count, including
lurkers. A run may terminate when this criterion is met before quality is stable.
Termination can be detected only at a configured progress checkpoint, so a
criterion may be exceeded by progress made between two checks.

## Shutdown And Final Report

- The controller reads and validates Analytics' lifecycle request.
- The controller sends `STOP_REQUEST` through Message Passing Interface (MPI).
- Workers and policy finish their current operation, close their Database (DB)
  connection, send `PROCESS_TERMINATED`, and exit.
- Analytics finishes its current recurring check and sends
  `ANALYTICS_READY_FOR_FINAL`.
- Analytics remains alive while it waits for `FINAL_ANALYSIS` from the
  controller.
- After receiving authorization, Analytics queries the final stable database
  state, writes one final `analytics_summary` row, and sends
  `FINAL_ANALYTICS_COMPLETE`.
- The controller waits for that acknowledgment before dumping the database and
  exiting.

The final analytics row must match the final persisted database state, including
the final action count and average action quality. The final row's
`termination_criteria` contains every criterion that caused termination.
Final analytics is not gated by `analytics.evaluation_interval`; it runs once
after shutdown coordination and waits for the end-state action count to settle.

## Logged And Stored Output

Each analytics report is logged as structured output. Recurring checks write
lightweight `analytics_summary` rows and JSONL records; after shutdown is
authorized, the final report is written as the last row. The final row may include action-type ratios,
repost ratios, policy coverage, network averages, runtime, and inter-activity
timing, plus final analytics-function outputs. These values describe the final
database state.

On the final report, `termination_criteria` preserves the criteria accepted by
the controller, and the reason comes from the same accepted lifecycle decision.
Metrics such as action count describe the final database state after batches
finish. `other_properties.final_termination_criteria` records the criteria
re-evaluated on that final state; it does not replace the accepted cause.
`other_properties.termination_reason` and `termination_source` identify the
accepted stop decision in the stored final summary. Later recurring Analytics
signals cannot change that attribution. There is no separate
stopping-versus-convergence result in the analytics or lifecycle contract.
