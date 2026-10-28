<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Policy Algorithm

## Post Labeling Inputs

Use named input objects as described in
[configuration.md](configuration.md#function-inputs).
`illegal_probability_n_survival_hours` requires `probability` in `[0,1]` and
finite positive `expected_survival_hours`. It samples illegal content with that
probability, then samples an exponential duration whose mean is
`expected_survival_hours * 3,600,000` milliseconds. Round to an integer and clamp
to at least one millisecond. The action writer adds its database-assigned
timestamp to obtain the absolute survival time. `legal_or_illegal_probability`
requires `probability` and assigns a legality label without a delay.

## Survival Time Calibration

The default 144 hours retains the earlier TikTok-labeled six-day preset. No local calibration report deriving
144 hours or the default illegal probability 0.01 was found in this repository.
Treat them as explicit experiment presets rather than newly calibrated values.
Each user type can now set different inputs without adding Python wrappers.

## Configured Rule Inputs

`below_quality_threshold` requires `threshold` in `[0,1]` and removes eligible
actions below that value. `illegal_content` takes no tunable inputs and removes
eligible actions labeled illegal. The default threshold is 0.3; related
exploratory work is in
[quality moderation calibration](../../analyses/calibration/quality_moderation/calibration_quality_moderation.ipynb).
This reference does not establish that the active 0.3 preset was calibrated;
no verified derivation of that preset is asserted here.

All rules remain gated by survival time. An action whose survival time is in
the future is not reviewed by any rule. The persisted progress cursor is an
action ID, not a delayed-review timestamp. The current simulated timestamp is
recomputed per fetch; the delayed branch has no saved lower timestamp bound.
Consequently, an action reviewed after it became due can remain an index
candidate on later passes. Already removed actions are filtered out; due actions
that produced no removal remain eligible under the current algorithm.

Policy is a separate process that reads eligible supported actions, evaluates the
configured policy rules, and writes moderation interventions back to the shared
database.

`docs/algorithms/simulation.md` is the source of truth for the high-level
simulation algorithm. This document describes policy-owned responsibilities and
implementation-facing details.

## What It Does

- Reads the persisted policy cursor from `simulation_lifecycle`.
- Fetches supported actions that are eligible for policy evaluation.
- Applies every configured `policy.rule_functions` selector to each fetched
  action target.
- Converts matching rule results into moderation intervention entries.
- Appends new intervention entries to `action.moderation_actions`.
- Advances the persisted policy cursor after each pass.
- Stops cleanly after the controller sends `STOP_REQUEST`.

## Polling And Performance

`policy.target_batch_size` is a positive integer. It defaults to `500`. Each
pass selects at most this many actions in total from the new and due branches.
All moderation writes for the pass use one transaction and update at most this
many action IDs. The write takes one transaction advisory lock per distinct
action receiving an intervention. It holds at most `policy.target_batch_size`
such locks per write transaction. PostgreSQL's
[`max_locks_per_transaction`](https://www.postgresql.org/docs/current/runtime-config-locks.html)
sizes a lock pool shared by database sessions; it is not a strict limit on one
transaction. A smaller size takes more passes to clear a backlog, so reviews
may happen later. Measure policy lag and database load before tuning the setting.

`policy.poll_interval_milliseconds` is a finite nonnegative wall-clock delay,
defaulting to `0`; fractional milliseconds are allowed. It applies after an
empty target fetch and after a pass that reviews targets but writes no
interventions. Cursor progress is saved before sleeping. A pass that writes
interventions proceeds without this delay.

As with [worker idle polling](worker.md#idle-polling-and-performance), zero is
the no-delay performance baseline. Positive values may reduce repeated queries
and contention, potentially improving total throughput, at the cost of later
review and stop checks. Compare throughput, CPU/database load and review latency
to choose a value; no calibrated or benchmark-derived optimum is asserted.
The delay does not directly advance simulated time. Workers can continue writing
while Policy sleeps, so it can change moderation timing relative to their work.

## Main Steps

- Create the policy database client.
- Resolve `policy.rule_functions` through the policy-rule registry.
- Read `policy.target_batch_size`.
- Read `policy.poll_interval_milliseconds`.
- Read the persisted policy cursor, if one exists.
- Repeat until `STOP_REQUEST` is received:
  - fetch policy targets from Postgres;
  - calculate the next cursor from fetched targets and the database-reported
    new-action high-water mark;
  - if no targets are returned, persist cursor progress when needed, sleep for
    the configured poll interval, and continue;
  - convert database rows into policy review targets;
  - evaluate the configured rules for every target;
  - append new intervention entries in one bounded database transaction;
  - persist the next policy cursor;
  - sleep for the configured poll interval when no interventions were written.
- Finish database work before acknowledging process termination.

## Cursor And Values Read For Each Pass

There is one persisted policy cursor, `C`, stored as
`simulation_lifecycle.policy_cursor_action_id`. The policy process loads it on
startup and keeps its current value in memory between passes. It marks the
boundary for new-action selection, including actions deferred by survival time;
it does not mean that every action at or below it has been evaluated.

Each fetch reads the last ID in its new-action page and the stored simulator
clock tick in the same SQL statement:

| Value | Meaning | Use |
|---|---|---|
| `H` (`next_cursor_action_id`) | Last supported action ID in the current new-action page | Allows `C` to advance past deferred new actions without skipping later pages |
| `T` (`current_timestamp`) | Stored `simsom_clock_state.current_tick_ms`, or zero if the clock row is absent | Determines whether an action's survival time is due |

The target query reads the stored tick without advancing it. A delayed action
can become due when the clock advances even if no later action is stored.

A pass proceeds as follows:

1. Read `H` and `T` alongside target selection using one statement's database
   snapshot. These reads do not advance simulator time or update stored state.
   Allocate at most `policy.target_batch_size` slots across the two branches.
   Alternate branch priority between passes so both can make progress.
2. Select the allotted new actions with `action_id > C`. Exclude rows with a
   future survival time from rule evaluation. On the first pass, when `C` is
   absent, start with the earliest current actions.
3. Select the allotted older delayed targets with `action_id <= C`, a numeric
   survival time at or before `T`, and no feed-excluding intervention. Resume
   this scan after its previous action ID, restarting at the beginning when it
   reaches the end. On the first pass this branch is empty.
4. Combine targets in ascending action-ID order and evaluate the configured
   rules. Reaching a survival time makes an action eligible; the rules decide
   whether any intervention is written.
5. Write any new interventions in one transaction, then save the next `C`.
   It is the maximum of the previous cursor, returned target IDs, and the
   page's reported `H`. An empty target batch can still advance `C` using `H`.
6. On the next pass, recalculate `H` and `T` and use the updated `C`. A later
   clock tick can make an action due; elapsed wall-clock time alone cannot.

The delayed scan position is in memory and resets on process restart. There is
no saved previous `T` used as a lower bound. An older row that receives a
feed-excluding intervention stops qualifying for that branch; a due row that
receives no such intervention can be returned again on a later scan cycle.
Deferred rows never move `C` backward.

## Configuration Effects

- `policy.rule_functions` configures which intervention rules run after an
  action is eligible for policy evaluation.
- `post_labeling_functions` can also affect policy evaluation. Any labeler that
  writes `other_properties.post_labels.survival_time` creates a policy-wide
  timing gate for that action.
- A future `survival_time` prevents all configured `policy.rule_functions` from
  running for the action until the stored simulator timestamp is due.
- `survival_time` is not an intervention rule selector. It controls when policy
  can evaluate the action; the configured rule functions still decide whether
  an intervention is written.
- Adding or removing a survival-time labeler changes policy target selection and
  rule timing, even if `policy.rule_functions` stays the same.

## Target Selection

- Every supported action type is eligible for policy evaluation:
  - `post`;
  - `reply`;
  - `quote`;
  - `like`;
  - `repost`.
- Each pass selects at most `policy.target_batch_size` action rows in total.
  The new and due branches share this limit. Their priority alternates between
  passes so both make progress. The branches are combined with `UNION ALL`.
  Both use action ID keyset scans.
- The new-action cursor advances to the last action ID in the current page,
  including any row deferred by a future `post_labels.survival_time`.
- The due-action scan keeps an in-memory action ID position. After it reaches
  the end of eligible rows, it starts again from the beginning on the next
  pass. This allows newly due rows behind the previous scan position to be
  revisited without holding all due rows in memory. Its upper action ID stays
  fixed for one scan cycle, so ongoing inserts cannot extend that cycle forever.
- An action row with a future `survival_time` is not returned for rule
  evaluation yet, but it still contributes to its new-action page boundary.
  This lets the cursor advance without re-reading the same deferred row as new
  work on every policy pass.
- The time reference is a scalar read of
  `simsom_clock_state.current_tick_ms`, defaulting to zero if the singleton
  clock row is absent. Both pages use the same database snapshot.
- Due delayed rows are found from the stored
  `other_properties.post_labels.survival_time` value. There is no separate
  delayed-intervention cache table.
- The survival-time lookup is supported by the partial expression index
  `action_post_label_survival_time_idx`. It indexes the stored due timestamp
  cast to `BIGINT`, only for rows whose
  `jsonb_typeof(other_properties #> '{post_labels,survival_time}') = 'number'`.
  The delayed branch repeats that predicate and searches the indexed value
  with `survival_time <= current_simulator_timestamp`. This is a branch of the
  same database request, not a separate round trip.
- The index includes numeric survival-time rows even after moderation. The
  delayed branch also filters by cursor, supported action type, and existing
  feed-excluding interventions; those filters are not part of this index's
  predicate.
- The target query returns the stored simulator-clock tick, so Policy compares
  due times against that tick rather than wall-clock time or the largest action
  timestamp.

## Survival-Time Gate

- `other_properties.post_labels.survival_time`, when present, limits policy
  evaluation for the action.
- If the survival time is in the future, policy emits no intervention for that
  target and does not run any configured rule functions for it.
- If the survival time is absent, the action is eligible for normal rule
  evaluation.
- If the survival time is due, every configured rule function can run. The
  configured rules still decide whether any intervention is written.
- The due-time check is deterministic once the simulator timestamp reaches the
  stored survival time. The stochastic behavior belongs to the labeler that
  assigned the label and sampled the duration.

## Rule Evaluation

- `policy.rule_functions` maps registered policy-rule names to explicit input
  objects in evaluation order.
- The policy evaluator resolves selectors once at startup and evaluates every
  selected rule for every eligible target.
- Rule functions own their metadata requirements. Target validation checks that
  the action type is supported, but it does not require `quality` or any other
  metadata key.
- The quality rule is `below_quality_threshold`:
  - it requires numeric `other_properties.quality`, satisfying
    `0 <= quality <= 1`;
  - it matches when quality is below the configured `threshold` (default `0.3`);
  - it writes `intervention_type = "removed"`;
  - it writes `violation_type = "below_quality_threshold"`;
  - it writes `rule = "below_quality_threshold"`.
- The current illegal-content rule is `illegal_content`:
  - it matches when `other_properties.post_labels.legality = "illegal"`;
  - it does not require `other_properties.quality`;
  - it writes `intervention_type = "removed"`;
  - it writes `violation_type = "illegal_content"`;
  - it writes `rule = "illegal_content"`.
- Multiple matching rules produce multiple intervention entries. Matching rules
  do not need to share the same `intervention_type`; each entry is validated and
  stored independently.
- If the same intervention entry already exists on the action, policy skips
  writing that duplicate entry.

## Intervention Storage

- Moderation interventions are stored directly on the subject row as a JSON
  list:
  `moderation_actions = [{"intervention_type": "...", "violation_type": "...", "rule": "..."}]`.
- Policy writes action-scoped interventions to `action.moderation_actions`.
- Future user-scoped interventions can use `user.moderation_actions`; the row
  that stores the list defines the intervention scope.
- `intervention_type` is the outcome, such as `removed`.
- `violation_type` is the policy reason, such as `illegal_content`.
- `rule` is the persisted cause that produced the intervention entry.
- Policy appends intervention entries to any existing moderation list. It does
  not replace unrelated existing entries.
- All interventions from one pass use one transaction with a bulk
  `UPDATE ... FROM` statement. It updates at most `policy.target_batch_size`
  action IDs. The transaction takes advisory locks before updating moderation
  data. Policy saves the cursor afterward in a separate transaction. If that
  save fails, the next run may revisit the page; the evaluator skips
  intervention entries already stored on an action.

## Source Metadata

- Intervention rows persist `rule`, not `source`.
- Source ownership is derived from decorator registration on the rule or helper
  that produces the intervention.
- Policy rules registered through `@policy_rule(..., source="policy")` map to
  the `policy` source.
- The repeated like/repost cleanup helper is registered with `source="user"`
  because it reflects repeated user actions, even though the worker process
  materializes the database update.
- Registering the same rule name with a different source is invalid because
  persisted rows only store `rule`; a single rule name must map to one source.
- Analytics uses source metadata to count policy-owned interventions.
- Feed filtering uses `intervention_type`, not source metadata, because feed
  visibility depends on the intervention outcome.

## Shutdown Behavior

- Policy checks for `STOP_REQUEST` at the top of each loop iteration.
- After a stop request, policy starts no new fetch/evaluation/write pass.
- If policy is already inside a database operation when the controller requests
  shutdown, it completes the operation before closing its connection.
- Policy sends `PROCESS_TERMINATED` only after its database connection is
  closed.
- Final analytics is authorized only after workers and policy have both
  terminated.

## Implemented In

- `libs/simsom/components/policy.py`
- `libs/simsom/db/policy.py`
- `libs/simsom/subcomponents/policy_rules.py`
- `libs/simsom/subcomponents/policy_annotations.py`
